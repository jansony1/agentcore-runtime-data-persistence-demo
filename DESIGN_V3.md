# AgentCore Multi-Agent Architecture Design (V3)

## Design Philosophy

**大脑与手脚分离。**

- **Runtime A (Agent A)**：唯一的大脑。做所有决策，不执行任何具体工作。
- **Runtime B**：纯手脚。被 Agent A 遥控，执行 shell、Python、报告渲染，自身不做任何决策。

Runtime B 中的 LLM 调用不是用来"思考"的，是用来"渲染"的——Agent A 把分析好的数据和指令传过去，Runtime B 只负责调 Opus 4.6 流式输出报告文本。

| | Runtime A | Runtime B |
|---|---|---|
| 角色 | 经理（决策） | 员工（执行） |
| LLM 用途 | **想**（下一步该干嘛） | **做**（写报告） |
| 决策能力 | 有（Strands Agent） | 无 |
| 工具 | runtime_b_shell, runtime_b_python | shell, python, report |
| 返回方式 | SSE（转发） | JSON（shell/python）或 SSE（report） |

---

## Architecture

```
┌─── Runtime A（Agent A = 唯一大脑）────────────────────────────────────┐
│                                                                       │
│  Strands Agent (Opus 4.6)                                             │
│  system_prompt: 数据分析专家，使用远程工作站的 shell/python 能力       │
│                                                                       │
│  tools:                                                               │
│    @tool runtime_b_shell(command)  → invoke_agent_runtime(B, shell)   │
│    @tool runtime_b_python(code)    → invoke_agent_runtime(B, python)  │
│                                                                       │
│  Agent 自己决定:                                                      │
│    "先下载数据" → runtime_b_shell("aws s3 cp ...")                    │
│    "看看结构"   → runtime_b_shell("head -5 data.csv")                 │
│    "分析数据"   → runtime_b_python("import pandas...")                │
│    "代码报错"   → runtime_b_python("修正后的代码")                     │
│    "再看结果"   → runtime_b_shell("cat output/result.csv")            │
│                                                                       │
│  @app.entrypoint (generator → SSE):                                   │
│    Phase 1: Agent tool loop (阻塞，多次调 Runtime B)                  │
│    Phase 2: 调 Runtime B report → 转发 SSE 流给前端                   │
│                                                                       │
└──────────┬────────────────────────────────────────────────────────────┘
           │
           │ invoke_agent_runtime (同一个 session_id)
           │ 多次调用，文件系统持久化
           │
           ▼
┌─── Runtime B（纯手脚，无决策）───────────────────────────────────────┐
│                                                                       │
│  @app.entrypoint: 接收 action → 执行 → 返回                          │
│                                                                       │
│  action = "shell":                                                    │
│    subprocess.run(command) → return {stdout, stderr, exit_code}       │
│    (JSON 同步返回)                                                    │
│                                                                       │
│  action = "python":                                                   │
│    exec(code) → return {stdout, stderr, exit_code, output_files}     │
│    (JSON 同步返回)                                                    │
│                                                                       │
│  action = "report":                                                   │
│    converse_stream(Opus 4.6, context from Agent A)                   │
│    → yield SSE chunks (每个 token 实时推送)                           │
│    → 保存 report.md + output/* 到 S3                                 │
│    (SSE 流式返回)                                                     │
│                                                                       │
│  文件系统: /tmp/workspace/ (同 session 持久，跨调用共享)              │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

---

## Request Lifecycle

```
用户: "分析各区域Q1销售达成率，给出排名和改进建议"
│
▼
Runtime A entrypoint (generator → SSE):
│
│  yield {"type":"status","stage":"analysis","message":"Agent 开始分析..."}
│
│  === Phase 1: Agent A tool loop (阻塞) ===
│
│  Agent A (Opus 4.6) 思考: "需要先下载数据"
│    → runtime_b_shell("aws s3 cp s3://bucket/tenants/acme-corp/datasets/ /tmp/workspace/ --recursive")
│      → invoke_agent_runtime(B, session=X, action=shell) → {stdout: "download completed"}
│
│  Agent A 思考: "看看数据结构"
│    → runtime_b_shell("head -5 /tmp/workspace/transactions.csv")
│      → invoke_agent_runtime(B, session=X, action=shell) → {stdout: "header,row1,row2..."}
│
│  Agent A 思考: "用 pandas 分析"
│    → runtime_b_python("import pandas as pd\ndf = pd.read_csv(...)...")
│      → invoke_agent_runtime(B, session=X, action=python) → {stdout: "华中 64.4%..."}
│
│  Agent A 思考: "生成图表"
│    → runtime_b_python("import matplotlib...")
│      → invoke_agent_runtime(B, session=X, action=python) → {stdout: "chart saved"}
│
│  Agent A 完成分析，返回所有发现 (analysis_result)
│
│  yield {"type":"status","stage":"analysis","message":"数据分析完成"}
│
│  === Phase 2: Report streaming (转发) ===
│
│  yield {"type":"status","stage":"report","message":"正在生成分析报告..."}
│
│  invoke_agent_runtime(B, session=X, action=report, context=analysis_result)
│    → Runtime B 调 converse_stream(Opus 4.6)
│    → SSE chunks 逐个返回:
│      yield {"type":"chunk","content":"# Q1 销售分析报告\n\n"}
│      yield {"type":"chunk","content":"## 区域排名\n\n1. 华中 64.4%..."}
│      yield {"type":"chunk","content":"## 改进建议\n\n..."}
│
│  → Runtime B 保存 report.md + charts 到 S3
│
│  yield {"type":"done","s3_keys":[...]}
│
▼
前端: 实时看到进度状态 → 实时看到报告内容 → 拿到 S3 文件路径
```

---

## Key Design Decisions

### 1. Phase 1 全程流式（使用 stream_async）

使用 Strands Agent 的 `agent.stream_async(task)` 替代 `agent(task)` 阻塞调用。`stream_async` 是 async generator，在 agent tool loop 的每一步都会 yield 事件（tool_use_stream、text chunk 等），entrypoint 将这些事件转换为 SSE 推送给前端。

Phase 1 和 Phase 2 都是流式的，前端全程无黑盒。

### 2. 为什么报告要在 Runtime B 渲染而不是 Runtime A？

Runtime A 的原则：**只做决策，不做具体产出**。报告是"具体产出"（内容生成），属于执行层的工作。Agent A 只需要告诉 Runtime B "用这些数据写报告"，不需要自己写。

### 3. 同 session 状态保持

Agent A 多次调用 Runtime B（shell → python → python → report），通过 `runtimeSessionId` 保证命中同一个 microVM：

```
invoke_agent_runtime(session=X, action=shell)  → files on disk
invoke_agent_runtime(session=X, action=python) → reads files, writes results
invoke_agent_runtime(session=X, action=report) → reads all outputs, streams report
```

同 session = 同 microVM = 文件系统持久。

### 4. stream_async vs agent(task)

| | `agent(task)` | `agent.stream_async(task)` |
|---|---|---|
| 返回方式 | 阻塞，等全部完成 | async generator，逐步 yield 事件 |
| Phase 1 可见性 | 黑盒 | **每个 tool call 实时推送** |
| 内部 agent loop | 相同 | 相同 |
| 框架能力保留 | 是 | 是（tool 定义、模型抽象、重试等不变） |

关键认知：`agent(task)` 和 `stream_async` 执行的 agent loop 完全一样，区别只在于你是等比赛结束看比分，还是看直播。

---

## Multi-Tenancy

与 V1/V2 一致。2 个 Runtime 部署，AgentCore 按 session 分配 microVM。

```
Runtime A (1 个部署)          Runtime B (1 个部署)
├── tenant-1 → microVM A₁    ├── tenant-1 → microVM B₁
├── tenant-2 → microVM A₂    ├── tenant-2 → microVM B₂
└── tenant-3 → microVM A₃    └── tenant-3 → microVM B₃
```

---

## Verified Test Results

### Deployed E2E Test (AgentCore, us-east-1)

**Deployed Runtimes:**
- Runtime A: `arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/data_router_v3-FBOLqODKUr`
- Runtime B: `arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/data_workstation_v3-k2h6743g84`

```python
resp = client.invoke_agent_runtime(
    agentRuntimeArn=RUNTIME_A_ARN,
    payload={"tenant_id": "acme-corp", "message": "分析各区域Q1销售达成率，给出排名和改进建议"},
    accept="text/event-stream",
)
```

**SSE Output (actual, stream_async — Phase 1 每步可见):**
```
[analysis] Agent 开始分析...
[analysis] 正在执行: runtime_b_shell           ← Phase 1 实时推送
[analysis] 正在执行: runtime_b_python          ← Phase 1 实时推送
[analysis] 正在执行: runtime_b_shell           ← Phase 1 实时推送
[analysis] 正在执行: runtime_b_python          ← Phase 1 实时推送
[analysis] 正在执行: runtime_b_shell           ← Phase 1 实时推送
[analysis] 正在执行: runtime_b_python          ← Phase 1 实时推送
[analysis] 正在执行: runtime_b_shell           ← Phase 1 实时推送
[analysis] 数据准备完成
[report]   正在分析数据并生成报告...
[report]   正在生成分析报告 (Opus 4.6 streaming)...
[chunk]    # 📊 各区域 Q1 销售达成率分析报告 ...   ← Phase 2 逐 token
[chunk]    ## 一、概述 ...
[chunk]    ## 二、关键发现 ...
           (806 chunks, 3358 chars 流式输出)
[done]     3 files → S3
```

**Report Preview (deployed output, Runtime B Opus 4.6 分析+生成):**
```markdown
# 📊 各区域 Q1 销售达成率分析报告

## 一、概述
本报告基于 2026 年第一季度各区域销售交易数据与目标数据，对全国 5 大区域
的销售达成情况进行全面分析。

核心结论：Q1 全国整体达成率仅为 49.7%，所有区域均未完成季度目标。

## 二、关键发现
1. 全军未达标：5 个区域无一完成 Q1 销售目标
2. 华中领跑全国：华中区域以 64.42% 的达成率位居首位
3. 华东垫底堪忧：华东区域达成率仅 37.01%，缺口最大（约 315 万）
```

**S3 Persisted Output:**
```
tenants/acme-corp/reports/
├── analysis_report.md                      ← Runtime B Opus 4.6 分析+生成的深度报告
├── q1_region_achievement.csv               ← Agent A 指挥 Runtime B 计算
└── q1_region_achievement_chart.png         ← Agent A 指挥 Runtime B 生成
```

### Deployment Pitfalls (fixed)

| 问题 | 原因 | 修复 |
|------|------|------|
| `runtimeSessionId` validation error | 最少 33 字符，短 UUID 不够 | 改用完整 UUID `str(uuid.uuid4())` |
| `contextvars` 在 tool 线程中丢失 | Strands Agent 在线程池执行 tool，ContextVar 不传播 | 改用模块级变量（单请求 microVM 安全） |
| `converse_stream` model ID invalid | 需要 inference profile ID，不是直接 model ID | 使用 `us.anthropic.claude-opus-4-6-v1` |
| Runtime B 502 | 未监听 AgentCore 默认端口 8080 | 修正端口为 8080 |
| CodeBuild Docker Hub 429 | 限流 | 改用 `public.ecr.aws` 基础镜像 |
| AWS_REGION 被覆盖 | 环境变量 `AWS_DEFAULT_REGION` 优先级 | 部署时显式设置 `AWS_REGION` |

---

## V1 → V2 → V3 Evolution

| | V1 | V2 | V3 (final) |
|---|---|---|---|
| 大脑在哪 | Runtime A | 同进程 | **Runtime A (唯一)** |
| Runtime B 角色 | 无 LLM 执行器 | N/A | **执行器 + LLM 分析报告** |
| Runtime B 的 LLM | 无 | N/A | **Opus 4.6（分析数据+生成报告）** |
| Agent A 怎么控制 B | 一次性传代码 | 直接 exec | **多次 tool call 遥控 (stream_async)** |
| 代码修复 | 跨 Runtime 来回 | 本地 | **Agent A 决定修复 → 再调 B** |
| 报告生成 | Agent A 自己 | 同进程 | **Runtime B 读 workspace 文件 → Opus 分析+报告** |
| Phase 1 流式 | 无 | 无 | **stream_async: 每个 tool call 实时推送** |
| Phase 2 流式 | 无 | 无 | **converse_stream: 报告逐 token 推送** |
| B 的文件状态 | 每次调用独立 | N/A | **同 session 持久** |

---

## File Structure

```
.
├── DESIGN_V3.md                    # This file
├── runtime_a_v3/
│   ├── main.py                     # Runtime A: Agent A (brain) + tool loop + SSE forwarding
│   ├── Dockerfile
│   └── requirements.txt
├── runtime_b_v3/
│   ├── main.py                     # Runtime B: pure executor (shell, python, report rendering)
│   ├── Dockerfile
│   └── requirements.txt
└── generate_sample_data.py         # Generate sample data for testing
```

---

## Open Questions

1. ~~**Phase 1 中间状态推送**~~ → **已解决**：使用 `agent.stream_async()` 实现全程流式
2. **Session 超时**：Agent A 分析耗时较长时，Runtime B 的 session 可能因 idle timeout 被回收
   - 建议：设置 `idle_runtime_session_timeout` 为较长值（如 1800s）
3. **Agent A 的模型选择**：当前 Agent A 用 Opus 4.6 做数据准备决策，可优化为 Sonnet（更快更便宜）
   - Opus 只在 Runtime B 的 report action 中使用（真正需要强推理的分析+报告）
4. **多工作站扩展**：未来加 Web 研究、文档生成等能力
   - Agent A 加新 tools（runtime_c_browse, runtime_d_docgen），每个指向不同 Runtime
