# AgentCore Multi-Agent Architecture Design (V2)

## Background

V1 (current implementation): Main Agent (Runtime A) + Code Executor (Runtime B), two separate runtimes.

V2 (this design): Consolidate into a **single Runtime** — main agent + sub-agents + sandbox execution all in one microVM. Simpler to deploy, lower latency, same multi-tenancy isolation.

---

## Architecture

```
┌─── Single AgentCore Runtime (1 deployment, per-session microVM) ──────────────┐
│                                                                                │
│  @app.entrypoint(payload, context)                                             │
│      │                                                                         │
│      │  resolve tenant_id + session_id                                         │
│      ▼                                                                         │
│  ┌─────────────────────────────────────────────────────────────────────┐       │
│  │  Main Agent (Router)                                                │       │
│  │  system_prompt: "根据用户需求选择合适的 sub-agent"                   │       │
│  │  model: Claude Sonnet                                               │       │
│  │  tools: [analyze_data, research_web, generate_report]               │       │
│  │                                                                     │       │
│  │  职责: 理解意图 → 选 sub-agent → 汇总结果 → 回复用户               │       │
│  │  不做: 具体分析、代码生成、网页操作                                  │       │
│  └────────┬──────────────────┬──────────────────┬──────────────────────┘       │
│           │                  │                  │                               │
│      @tool                @tool             @tool                              │
│   analyze_data()      research_web()    generate_report()                      │
│           │                  │                  │                               │
│           ▼                  ▼                  ▼                               │
│  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐                  │
│  │ Data Analysis   │ │ Web Research    │ │ Report Gen      │                  │
│  │ Sub-Agent       │ │ Sub-Agent       │ │ Sub-Agent       │                  │
│  │                 │ │                 │ │                 │                  │
│  │ model: Sonnet   │ │ model: Sonnet   │ │ model: Sonnet   │                  │
│  │ tools:          │ │ tools:          │ │ tools:          │                  │
│  │  • fetch_s3     │ │  • nova_act     │ │  • fetch_s3     │                  │
│  │  • exec_code    │ │  • save_to_s3   │ │  • render_tmpl  │                  │
│  │  • save_to_s3   │ │                 │ │  • save_to_s3   │                  │
│  │                 │ │                 │ │                 │                  │
│  │ 自己生成代码    │ │ 自己浏览网页    │ │ 自己渲染文档    │                  │
│  │ 自己 exec 执行  │ │ 自己提取数据    │ │ 自己检查输出    │                  │
│  │ 自己看报错修复  │ │ 自己迭代搜索    │ │ 自己迭代优化    │                  │
│  └─────────────────┘ └─────────────────┘ └─────────────────┘                  │
│           │                  │                  │                               │
│           └──────────────────┴──────────────────┘                              │
│                              │                                                 │
│                              ▼                                                 │
│                    ┌────────────────────┐                                      │
│                    │   共享基础设施      │                                      │
│                    │  • S3 Client       │                                      │
│                    │  • exec() sandbox  │                                      │
│                    │  • tenant_prefix() │                                      │
│                    └────────────────────┘                                      │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌────────────────────┐
                    │     S3 Bucket      │
                    │  tenants/          │
                    │  ├── tenant-A/     │
                    │  │   ├── datasets/ │
                    │  │   └── reports/  │
                    │  └── tenant-B/     │
                    │      ├── datasets/ │
                    │      └── reports/  │
                    └────────────────────┘
```

---

## Request Lifecycle

```
用户: "帮我分析Q1各区域销售达成率"
│
▼
Entrypoint:
│  tenant_id = resolve_tenant_id(payload, context)
│  set_tenant(tenant_id, session_id)
│
▼
Main Agent (Router):
│  LLM 判断: 这是数据分析任务
│  调用 tool: analyze_data(task="分析Q1各区域销售达成率")
│
▼
Data Analysis Sub-Agent (同进程):
│
├── fetch_s3_data("datasets/")
│   → 列出 tenants/{tenant_id}/datasets/ 下的文件
│
├── fetch_s3_data([...transactions.csv])
│   → 预览数据结构
│
├── LLM 生成 pandas 分析代码
│
├── exec_code(code)          ← 本地执行，不跨 Runtime
│   → stdout: "华中 64.4%, 西南 56.0%..."
│   → 如果报错 → LLM 修复代码 → 再次 exec_code (循环，最多 3 次)
│
├── save_to_s3(result_csv)
│   → tenants/{tenant_id}/reports/sales/q1_achievement.csv
│
└── return 分析结论 + S3 路径
│
▼
Main Agent: 汇总结果，回复用户
```

---

## Multi-Tenancy

和 V1 一致。单 Runtime 部署，AgentCore 按 session 分配独立 microVM。

```
1 个 Runtime 部署 → N 个 session → N 个 microVM

tenant-A 请求 (session-A) → microVM₁  (独立 Firecracker 实例)
tenant-B 请求 (session-B) → microVM₂  (独立 Firecracker 实例)
tenant-C 请求 (session-C) → microVM₃  (独立 Firecracker 实例)
```

三层隔离不变：

| 层 | 机制 |
|---|---|
| 计算 | AgentCore session → 独立 microVM |
| 数据 | S3 `tenants/{tenant_id}/` 前缀 + code guard |
| 上下文 | `contextvars.ContextVar` 传递 tenant_id 到所有 tool |

---

## Code Skeleton

```python
from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

app = BedrockAgentCoreApp()
model = BedrockModel(model_id="us.anthropic.claude-sonnet-4-20250514-v1:0")

# ──── Shared Infrastructure ────

s3 = boto3.client("s3")

def exec_code(code: str) -> dict:
    """Execute Python code in-process with captured stdout/stderr."""
    ...

def tenant_prefix(path: str) -> str:
    return f"tenants/{get_tenant_id()}/{path.lstrip('/')}"

# ──── Sub-Agent: Data Analysis ────

@tool
def fetch_s3_data(s3_keys: List[str]) -> str:
    """Fetch data from S3 (tenant-scoped)."""
    ...

@tool
def execute_analysis_code(code: str) -> str:
    """Execute Python code locally. Use INPUT_DIR/OUTPUT_DIR for file paths."""
    ...

@tool
def save_to_s3(files: List[dict]) -> str:
    """Save results to tenant-scoped S3 path."""
    ...

data_analyst = Agent(
    model=model,
    tools=[fetch_s3_data, execute_analysis_code, save_to_s3],
    system_prompt="你是数据分析专家。生成 pandas 代码分析数据，执行并返回结论..."
)

# ──── Sub-Agent: Web Research ────

# from nova_act import NovaAct  (if using Nova Act)
# or from strands_tools.browser import AgentCoreBrowser

@tool
def browse_and_extract(url: str, task: str) -> str:
    """Browse a URL and extract information."""
    ...

web_researcher = Agent(
    model=model,
    tools=[browse_and_extract, save_to_s3],
    system_prompt="你是 Web 研究员。浏览网页提取所需信息..."
)

# ──── Sub-Agent: Report Generator ────

report_generator = Agent(
    model=model,
    tools=[fetch_s3_data, save_to_s3],
    system_prompt="你是报告生成专家。根据数据生成结构化报告..."
)

# ──── Main Agent (Router) ────

@tool
def analyze_data(task: str) -> str:
    """将数据分析任务分发给数据分析 sub-agent。"""
    return str(data_analyst(task))

@tool
def research_web(task: str) -> str:
    """将 web 研究任务分发给 web 研究 sub-agent。"""
    return str(web_researcher(task))

@tool
def generate_report(task: str) -> str:
    """将报告生成任务分发给报告 sub-agent。"""
    return str(report_generator(task))

router = Agent(
    model=model,
    tools=[analyze_data, research_web, generate_report],
    system_prompt="""你是任务路由器。根据用户需求选择合适的 sub-agent:
    - analyze_data: 数据分析、统计、可视化
    - research_web: 网页搜索、信息提取
    - generate_report: 报告生成、文档输出
    只做分发，不做具体任务。"""
)

# ──── Entrypoint ────

@app.entrypoint
def invoke(payload: dict, context: RequestContext) -> dict:
    tenant_id = resolve_tenant_id(payload, context)
    session_id = context.session_id or payload.get("session_id", uuid4().hex[:8])
    set_tenant(tenant_id, session_id)

    message = payload.get("message", "")
    result = router(message)

    return {
        "output": str(result),
        "status": "success",
        "tenant_id": tenant_id,
        "session_id": session_id,
    }
```

---

## Evolution Path: Single → Multi-Runtime

如果遇到以下问题，再拆分到独立 Runtime：

| 触发条件 | 拆什么 | 改动 |
|----------|--------|------|
| exec 的代码导致进程崩溃 | exec sandbox → 独立 Runtime | `execute_analysis_code` 改为 `invoke_agent_runtime` |
| 浏览器占内存太多影响其他 agent | web agent → 独立 Runtime | `research_web` 改为 `invoke_agent_runtime` |
| 某个 sub-agent 需要独立扩缩容 | 该 agent → 独立 Runtime | 对应 tool 改为 `invoke_agent_runtime` |
| 需要独立部署更新某个 sub-agent | 该 agent → 独立 Runtime | 同上 |

拆分时的代码改动最小化：

```python
# Before (同进程):
@tool
def analyze_data(task: str) -> str:
    return str(data_analyst(task))       # 本地 Agent 对象调用

# After (独立 Runtime):
@tool
def analyze_data(task: str) -> str:
    resp = agentcore_dp.invoke_agent_runtime(   # 改成远程调用
        agentRuntimeArn=DATA_ANALYST_ARN,
        payload=json.dumps({"task": task, "tenant_id": get_tenant_id()}).encode(),
        contentType="application/json",
    )
    return resp["response"].read().decode()
```

每次只拆一个 sub-agent，其他保持不变。渐进式演进，不需要一次性重构。

---

## Comparison

### V1 (current) vs V2 (this design)

| | V1 (两个 Runtime) | V2 (单个 Runtime) |
|---|---|---|
| 部署数量 | 2 | 1 |
| Sub-Agent | 无 (Runtime A 直接生成代码) | 有 (Router + 专业 Sub-Agents) |
| 代码执行 | 跨 Runtime (invoke_agent_runtime) | 本地 exec (同进程) |
| 修复迭代 | 每轮 2 次网络调用 | 零网络调用 |
| 隔离级别 | microVM 隔离 (Runtime A ≠ Runtime B) | 进程内 (共享 microVM) |
| 冷启动 | 可能 2 次 | 1 次 |

### vs Manus

| | Manus | V2 |
|---|---|---|
| Agent 模式 | 单 Agent + 模块注入 | Router + 专业 Sub-Agents |
| 为什么不同 | 一个 LLM 够强，靠 prompt 切换角色 | 拆分 prompt/tools 降低复杂度 |
| Sandbox | 同进程 (容器内) | 同进程 (microVM 内) |
| 扩展性 | 工具越多 prompt 越长，准确率下降 | 新增 sub-agent 不影响已有的 |

---

## Open Questions

1. **exec 安全**: LLM 生成的代码在同进程 exec，理论上可以访问 agent 的内存/环境变量。是否需要 subprocess + timeout 替代？
2. **资源上限**: 单 microVM 的 CPU/内存能否同时支持 LLM API 调用 + pandas 数据处理 + 浏览器？
3. **Session 管理**: 多轮对话时，sub-agent 的状态（已加载的数据、分析上下文）如何跨 invoke 保持？
4. **Nova Act 集成**: Nova Act 需要本地 Playwright，microVM 里能否安装 Chromium？还是走 AgentCore Browser（远程）？
