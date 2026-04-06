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

### 1. 为什么 Phase 1 是阻塞的？

Strands Agent 的 tool call 是同步的——每次调用 `runtime_b_shell` 或 `runtime_b_python`，必须等 Runtime B 返回结果后 Agent 才能决定下一步。无法在 tool loop 过程中 yield SSE。

这意味着 Phase 1 期间前端只看到 "Agent 开始分析..." 状态。分析完成后才进入 Phase 2 的流式报告。

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

### 4. Phase 1 能否也推中间状态？

目前不行（Agent tool call 是同步的）。如果需要，可以：
- 用 `threading` + `queue`：Agent 在后台线程跑，tool 调用时往 queue 推状态，主线程从 queue yield
- 用 Strands 的 `callback_handler`：捕获 tool_use 事件推送

这是 future improvement，不影响核心流程。

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

### Local E2E Test

```
curl -N -X POST http://localhost:8080/invocations \
  -d '{"tenant_id":"acme-corp","message":"分析各区域Q1销售达成率，给出排名和改进建议"}'
```

**SSE Output:**
```
[analysis] Agent 开始分析...
           (Phase 1: Agent A 多次调 Runtime B shell/python, 阻塞约60-90秒)
[analysis] 数据分析完成
[report]   正在生成分析报告...
[report]   正在生成分析报告 (Opus 4.6 streaming)...
[chunk]    # 📊 Q1 各区域销售达成率专业分析报告
[chunk]    ## 一、概述 ...
[chunk]    ## 二、关键发现 ...
[chunk]    ## 三、区域排名 ...
[chunk]    ## 四、改进建议 ...
           (约1300+ chunks 流式输出)
[done]     4 files → S3
```

**S3 Output:**
```
tenants/acme-corp/reports/
├── analysis_report.md                      ← 流式生成的完整报告
├── q1_region_achievement_summary.csv       ← Agent A 指挥 Runtime B 生成
├── q1_sales_achievement_analysis.png       ← Agent A 指挥 Runtime B 生成
└── q1_q2_comparison.png                    ← Agent A 指挥 Runtime B 生成
```

---

## V1 → V2 → V3 Evolution

| | V1 | V2 | V3 (final) |
|---|---|---|---|
| 大脑在哪 | Runtime A | 同进程 | **Runtime A (唯一)** |
| Runtime B 角色 | 无 LLM 执行器 | N/A | **无决策执行器 + 报告渲染器** |
| Agent A 怎么控制 B | 一次性传代码 | 直接 exec | **多次 tool call 遥控** |
| 代码修复 | 跨 Runtime 来回 | 本地 | **Agent A 决定修复 → 再调 B** |
| 报告生成 | Agent A 自己 | 同进程 | **Agent A 指挥 → Runtime B 渲染 → SSE** |
| 流式体验 | 无 | 无 | **Phase 2 报告逐 token 推送** |
| B 的文件状态 | 每次调用独立 | N/A | **同 session 持久** |

---

## File Structure

```
.
├── DESIGN_V3.md                    # This file
├── main_v3.py                      # Runtime A V3 source
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

1. **Phase 1 中间状态推送**：Agent tool loop 过程中的 shell/python 结果能否实时推给前端？
   - 可选方案：threading + queue，或 Strands callback_handler
2. **Session 超时**：Agent A 分析耗时较长时，Runtime B 的 session 可能因 idle timeout 被回收
   - 建议：设置 `idle_runtime_session_timeout` 为较长值（如 1800s）
3. **Agent A 的模型选择**：当前用 Opus 4.6 做分析决策，成本较高
   - 可选：分析用 Sonnet，只在报告渲染时用 Opus
4. **多工作站扩展**：未来加 Web 研究、文档生成等能力
   - Agent A 加新 tools（runtime_c_browse, runtime_d_docgen），每个指向不同 Runtime
