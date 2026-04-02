# AgentCore Multi-Agent Architecture Design (V3)

## Design Philosophy

V3 重新回到双 Runtime 架构，但与 V1 有本质区别：

| | V1 | V3 |
|---|---|---|
| Runtime B | 无 LLM，纯执行器（接收代码 → exec → 返回） | **有 LLM（Opus 4.6），自治 Agent + 执行环境** |
| 谁生成代码 | Runtime A 的 LLM | **Runtime B 自己的 LLM** |
| 谁决定执行策略 | Runtime A | **Runtime B 自己** |
| 返回方式 | JSON 一次性返回 | **SSE 流式返回** |
| Runtime B 的角色 | 手脚 | **完整的分析工作站** |

V3 的 Runtime B 类似 Manus 的 Sandbox，但**内置了 Agent 智能**——它不是等指令的工具人，而是拿到任务后自己闭环完成全部工作。

---

## Architecture

```
┌─── Runtime A（主 Agent / 路由器）──────────────────────────────────────┐
│                                                                        │
│  Model: Claude Sonnet (轻量、快速)                                     │
│  职责:                                                                 │
│    • 理解用户意图                                                      │
│    • 路由到对应的 Runtime B                                            │
│    • 转发 SSE 流给用户                                                │
│    • 不做具体分析、不生成代码                                          │
│                                                                        │
│  @app.entrypoint → generator (SSE)                                     │
│      │                                                                 │
│      │ invoke_agent_runtime(Runtime B, accept="text/event-stream")     │
│      │ → StreamingBody → iter_lines() → yield to user                 │
│      │                                                                 │
└──────┼─────────────────────────────────────────────────────────────────┘
       │
       │  invoke_agent_runtime (SSE stream)
       │
       ▼
┌─── Runtime B（自治分析工作站）─────────────────────────────────────────┐
│                                                                        │
│  Model: Claude Opus 4.6 (强推理、长上下文)                             │
│                                                                        │
│  三项能力合一:                                                         │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────┐      │
│  │  1. Shell (命令行)                                           │      │
│  │     • aws s3 cp / aws s3 ls                                  │      │
│  │     • csvtool, jq, head, tail, wc, sort, uniq                │      │
│  │     • 文件格式转换、基础 ETL                                  │      │
│  │     • pip install (按需安装依赖)                              │      │
│  └──────────────────────────────────────────────────────────────┘      │
│  ┌──────────────────────────────────────────────────────────────┐      │
│  │  2. Python Executor                                          │      │
│  │     • pandas / numpy / matplotlib                            │      │
│  │     • 数据清洗、统计分析、可视化                               │      │
│  │     • 生成中间结果文件                                        │      │
│  └──────────────────────────────────────────────────────────────┘      │
│  ┌──────────────────────────────────────────────────────────────┐      │
│  │  3. Report Agent (Opus 4.6)                                  │      │
│  │     • 基于 1/2 产出的数据撰写分析报告                         │      │
│  │     • 流式生成，边写边 SSE 推送                               │      │
│  │     • 包含数据结论、图表引用、建议                            │      │
│  └──────────────────────────────────────────────────────────────┘      │
│                                                                        │
│  @app.entrypoint → generator (SSE)                                     │
│                                                                        │
│  内部流程:                                                             │
│    shell_exec("aws s3 cp ...") → 拉数据                              │
│    shell_exec("head -5 data.csv") → 预览结构                         │
│    exec_python(pandas 代码) → 分析计算                                │
│    exec_python(matplotlib 代码) → 生成图表                            │
│    Agent(Opus 4.6) → 阅读所有产出 → 流式生成报告                     │
│    save_to_s3(报告 + 图表) → 持久化                                   │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Why Opus 4.6 in Runtime B, Not Sonnet?

Runtime B 的 Agent 需要：
- 阅读大量数据（pandas 输出、多个文件内容）→ 需要长上下文
- 决定用 shell 还是 Python、先做什么后做什么 → 需要强推理
- 基于数据写深度分析报告 → 需要强生成能力

Sonnet 适合 Runtime A 的路由（判断意图、选择 sub-agent、转发结果），这是简单任务。
Opus 适合 Runtime B 的深度分析和报告生成，这是复杂任务。

**成本优化：快的事用 Sonnet，难的事用 Opus。**

---

## SSE Streaming End-to-End

```
用户                    Runtime A               Runtime B
 │                         │                         │
 │ POST /invocations       │                         │
 │ ───────────────────→    │                         │
 │                         │ invoke_agent_runtime     │
 │                         │ accept: text/event-stream│
 │                         │ ───────────────────────→ │
 │                         │                         │ shell: 下载数据...
 │                         │ ← SSE: {"type":"status",│   "msg":"正在下载数据"}
 │ ← SSE (forward)        │                         │
 │                         │                         │ python: 分析计算...
 │                         │ ← SSE: {"type":"status",│   "msg":"正在执行分析"}
 │ ← SSE (forward)        │                         │
 │                         │                         │ Opus 4.6: 生成报告
 │                         │ ← SSE: {"type":"chunk", │   "content":"## Q1 分析\n\n"}
 │ ← SSE (forward)        │                         │
 │                         │ ← SSE: {"type":"chunk", │   "content":"华中达成率最高..."}
 │ ← SSE (forward)        │                         │
 │                         │ ← SSE: {"type":"done",  │   "s3_keys":[...]}
 │ ← SSE (forward)        │                         │
 │                         │                         │
```

### SSE Event Types

```json
// 1. 进度状态
{"type": "status", "stage": "shell", "message": "正在从 S3 下载数据..."}
{"type": "status", "stage": "python", "message": "正在执行数据分析代码..."}
{"type": "status", "stage": "report", "message": "正在生成分析报告..."}

// 2. 报告内容（流式，边生成边推送）
{"type": "chunk", "content": "## 2026年Q1各区域销售达成率分析\n\n"}
{"type": "chunk", "content": "### 整体表现\n\n根据对500条交易记录的分析..."}
{"type": "chunk", "content": "### 区域排名\n\n1. **华中** 64.4%\n2. ..."}

// 3. 完成
{"type": "done", "s3_keys": ["tenants/.../report.md", "tenants/.../chart.png"]}

// 4. 错误
{"type": "error", "stage": "python", "message": "pandas 代码执行失败", "stderr": "..."}
```

---

## Request Lifecycle (Detailed)

```
用户: "帮我深度分析Q1各区域销售数据，出个分析报告"
│
▼
Runtime A entrypoint:
│  resolve tenant_id + session_id
│  LLM (Sonnet) 判断: 这是数据分析 + 报告任务
│  → invoke_agent_runtime(Runtime B ARN, SSE)
│  → 流式转发给用户
│
▼
Runtime B entrypoint (generator, SSE):
│
│  ── Stage 1: Shell 数据准备 ──
│
├── yield {"type":"status", "stage":"shell", "message":"正在下载数据..."}
├── shell_exec("aws s3 cp s3://bucket/tenants/{tenant}/datasets/... /tmp/work/")
├── shell_exec("wc -l /tmp/work/transactions.csv")
│   → "501 行"
├── shell_exec("head -3 /tmp/work/transactions.csv")
│   → 预览数据结构
│
│  ── Stage 2: Python 数据分析 ──
│
├── yield {"type":"status", "stage":"python", "message":"正在执行数据分析..."}
├── Agent (Opus 4.6) 生成 pandas 代码
├── exec_python(code)
│   → 执行成功? → 继续
│   → 执行失败? → Agent 读 stderr → 修复代码 → 重试 (最多3次)
├── exec_python(matplotlib 图表代码)
│   → /tmp/work/output/chart.png
│
│  ── Stage 3: Agent 生成报告 (流式) ──
│
├── yield {"type":"status", "stage":"report", "message":"正在生成分析报告..."}
├── Agent (Opus 4.6) 读取:
│     • pandas 的 stdout (统计数据)
│     • 生成的 CSV 文件内容
│     • 图表路径
│   → 流式生成报告:
│     yield {"type":"chunk", "content": "## Q1 销售分析报告\n\n"}
│     yield {"type":"chunk", "content": "### 1. 整体概况\n\n..."}
│     yield {"type":"chunk", "content": "### 2. 区域排名\n\n..."}
│     yield {"type":"chunk", "content": "### 3. 改进建议\n\n..."}
│
│  ── Stage 4: 持久化 ──
│
├── save report.md + chart.png → S3
├── yield {"type":"done", "s3_keys": [...]}
│
▼
Runtime A: 流已经逐步转发给用户了，无需额外处理
```

---

## Runtime B Internal Architecture

```python
# Runtime B 的核心结构

@app.entrypoint
def invoke(payload: dict, context: RequestContext):
    """返回 generator → 自动 SSE 流式响应"""
    tenant_id = payload.get("tenant_id")
    task = payload.get("task")
    s3_data_prefix = payload.get("s3_data_prefix")

    # 整个函数是 generator，yield = SSE event
    yield from run_analysis_pipeline(tenant_id, task, s3_data_prefix)


def run_analysis_pipeline(tenant_id, task, s3_data_prefix):
    """三阶段分析流水线"""

    workdir = setup_workspace(tenant_id)

    # ── Stage 1: Shell 数据准备 ──
    yield {"type": "status", "stage": "shell", "message": "正在下载数据..."}
    shell_results = prepare_data_with_shell(workdir, s3_data_prefix)

    # ── Stage 2: Python 分析 ──
    yield {"type": "status", "stage": "python", "message": "正在执行数据分析..."}
    analysis_agent = Agent(
        model=BedrockModel(model_id="global.anthropic.claude-opus-4-6-v1"),
        tools=[exec_python, shell_exec, read_file, list_files],
        system_prompt=ANALYSIS_PROMPT,
    )
    analysis_result = analysis_agent(
        f"工作目录: {workdir}\n"
        f"已下载的文件: {shell_results['files']}\n"
        f"任务: {task}\n"
        f"请分析数据并将结果保存到 {workdir}/output/"
    )

    # ── Stage 3: 报告生成 (流式) ──
    yield {"type": "status", "stage": "report", "message": "正在生成分析报告..."}

    # 收集 Stage 2 的所有产出作为报告的上下文
    analysis_context = gather_outputs(workdir)

    report_model = BedrockModel(
        model_id="global.anthropic.claude-opus-4-6-v1",
        streaming=True
    )
    # 直接流式调用 LLM 生成报告（不通过 Agent，减少开销）
    for chunk in stream_report(report_model, task, analysis_context):
        yield {"type": "chunk", "content": chunk}

    # ── Stage 4: 持久化 ──
    s3_keys = save_outputs_to_s3(tenant_id, workdir)
    yield {"type": "done", "s3_keys": s3_keys}
```

### Stage 2 为什么用 Agent，Stage 3 为什么不用？

| Stage | 用 Agent? | 原因 |
|-------|----------|------|
| Stage 2 (分析) | **是** | 需要 tool use 循环：生成代码 → 执行 → 看结果 → 可能修复重试 |
| Stage 3 (报告) | **否** | 纯文本生成，不需要 tool，直接流式调 LLM 更高效 |

Stage 3 直接用 `model.converse_stream()` 而不是 `Agent()`，原因：
- Agent 的 streaming 会被 tool use 循环打断，不适合纯文本流式输出
- 报告生成的输入（分析结果）已经在 Stage 2 完成，不需要再调工具
- 直接 stream = 每个 token 实时推送，用户体验最好

---

## Runtime A Code Skeleton

```python
# Runtime A: 路由 + SSE 转发

from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

app = BedrockAgentCoreApp()
agentcore_dp = boto3.client("bedrock-agentcore", region_name=REGION,
                            config=Config(read_timeout=300))

@app.entrypoint
def invoke(payload: dict, context: RequestContext):
    """Generator → SSE 流式响应"""
    tenant_id = resolve_tenant_id(payload, context)
    message = payload.get("message", "")

    # Router LLM 判断任务类型（轻量 Sonnet 调用）
    task_type = classify_task(message)  # "data_analysis" | "web_research" | ...

    if task_type == "data_analysis":
        yield from invoke_runtime_b_streaming(tenant_id, message)
    else:
        yield {"type": "error", "message": f"暂不支持任务类型: {task_type}"}


def invoke_runtime_b_streaming(tenant_id, task):
    """调用 Runtime B 并逐行转发 SSE 事件"""
    response = agentcore_dp.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_B_ARN,
        payload=json.dumps({
            "tenant_id": tenant_id,
            "task": task,
            "s3_data_prefix": f"tenants/{tenant_id}/datasets/",
        }).encode("utf-8"),
        contentType="application/json",
        accept="text/event-stream",
        qualifier="DEFAULT",
    )

    # StreamingBody → 逐行读取 SSE → yield 转发
    for line in response["response"].iter_lines():
        if line:
            line_str = line.decode("utf-8") if isinstance(line, bytes) else line
            if line_str.startswith("data: "):
                event_data = line_str[6:]  # strip "data: " prefix
                try:
                    yield json.loads(event_data)
                except json.JSONDecodeError:
                    yield {"type": "raw", "content": event_data}
```

---

## Multi-Tenancy

与 V1/V2 一致。2 个 Runtime 部署，AgentCore 按 session 分配 microVM。

```
Runtime A (1 个部署)          Runtime B (1 个部署)
├── tenant-1 → microVM A₁    ├── tenant-1 → microVM B₁
├── tenant-2 → microVM A₂    ├── tenant-2 → microVM B₂
└── tenant-3 → microVM A₃    └── tenant-3 → microVM B₃
```

数据隔离不变：
- S3 路径前缀 `tenants/{tenant_id}/`
- Runtime A code guard 检查
- Runtime B 工作目录 `/tmp/workspace/{tenant_id}/`

---

## V1 → V2 → V3 Evolution

```
V1: Runtime A (Sonnet, 做所有决策)  ──→  Runtime B (无 LLM, exec 代码)
    问题: Runtime A 又要理解用户、又要生成代码、又要分析结果
          单 Agent 职责过重，代码修复需要跨 Runtime 来回

V2: 单 Runtime (所有 Agent 同进程)
    问题: 安全隔离不够，exec 的代码能影响 Agent 进程
          资源竞争（LLM 调用 vs 数据处理）

V3: Runtime A (Sonnet, 路由)  ──SSE──→  Runtime B (Opus 4.6, 自治工作站)
    解决:
    ├── 关注点分离: A 做路由，B 做分析
    ├── 安全隔离: 代码执行在独立 microVM
    ├── 模型分级: 路由用 Sonnet (快/便宜)，分析用 Opus (强/深度)
    ├── 本地迭代: B 内部生成代码→执行→修复，零网络延迟
    └── 流式体验: SSE 端到端，用户实时看到进度和报告
```

## Comparison

| | V1 | V2 | V3 |
|---|---|---|---|
| 部署数 | 2 | 1 | 2 |
| Runtime B 有 LLM | 否 | N/A (合并) | **是 (Opus 4.6)** |
| 代码生成在哪 | Runtime A | 同进程 | **Runtime B** |
| 代码修复迭代 | 跨 Runtime | 本地 | **本地** |
| 报告生成 | Runtime A | 同进程 | **Runtime B (流式)** |
| 返回方式 | JSON | JSON | **SSE 流式** |
| 用户体验 | 等完了才看到 | 等完了才看到 | **边做边看** |
| 隔离 | microVM | 进程内 | **microVM** |
| 成本 | Sonnet x2 | Sonnet x1 | **Sonnet (路由) + Opus (分析)** |

---

## Open Questions

1. **Shell 安全**: Runtime B 的 shell_exec 是否需要白名单？LLM 生成的 shell 命令如果执行 `rm -rf /` 怎么办？
   - 建议: subprocess + timeout + 非 root 用户 + 命令白名单
2. **Opus 成本**: 每次分析任务都调 Opus 4.6，成本是否可接受？
   - 可选: Stage 2 (代码生成) 用 Sonnet，Stage 3 (报告) 才用 Opus
3. **Runtime B 冷启动**: 包含 pandas/numpy/matplotlib 的容器镜像较大，冷启动时间？
   - 可选: 预热 Session、或用 AgentCore 的 session 保活
4. **SSE 断连**: 如果 Runtime A ↔ Runtime B 的 SSE 连接中断，如何恢复？
   - 可选: Runtime B 同时将中间结果写 S3，支持从断点恢复
5. **多 Runtime B 扩展**: 如果未来需要 Web 研究、报告生成等其他类型的工作站？
   - Runtime A 作为路由器天然支持多后端: `invoke_agent_runtime(WEB_RESEARCHER_ARN)` 等
