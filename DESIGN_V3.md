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
{"type": "status", "stage": "router", "message": "任务类型: 数据分析，正在转发到分析工作站..."}
{"type": "status", "stage": "shell", "message": "正在从 S3 下载数据..."}
{"type": "status", "stage": "shell", "message": "已下载 2 个文件: transactions.csv, region_targets.csv"}
{"type": "status", "stage": "python", "message": "正在执行数据分析 (Opus 4.6)..."}
{"type": "status", "stage": "python", "message": "数据分析完成"}
{"type": "status", "stage": "report", "message": "正在生成分析报告 (Opus 4.6 streaming)..."}

// 2. 报告内容（流式，边生成边推送）
{"type": "chunk", "content": "# 2026年Q1各区域销售达成率深度分析报告\n\n"}
{"type": "chunk", "content": "### 整体表现\n\n根据对500条交易记录的分析..."}
{"type": "chunk", "content": "### 区域排名\n\n1. **华中** 64.4%\n2. ..."}

// 3. 完成
{"type": "done", "s3_keys": [{"s3_key": "...", "s3_uri": "s3://..."}]}

// 4. 错误
{"type": "error", "stage": "python", "message": "pandas 代码执行失败"}
```

---

## Request Lifecycle (Detailed)

```
用户: "帮我深度分析Q1各区域销售数据，出个分析报告"
│
▼
Runtime A entrypoint (generator → SSE):
│  resolve tenant_id + session_id
│  LLM (Sonnet) classify_task → "data_analysis"
│  yield {"type":"status","stage":"router","message":"正在转发到分析工作站..."}
│  → invoke_agent_runtime(Runtime B ARN, SSE)
│  → StreamingBody.iter_lines() → yield each event to user
│
▼
Runtime B entrypoint (generator → SSE):
│
│  ── Stage 1: Shell 数据准备 ──
│
├── yield {"type":"status", "stage":"shell", "message":"正在下载数据..."}
├── subprocess: aws s3 cp s3://bucket/tenants/{tenant}/datasets/ /tmp/workspace/input/ --recursive
├── os.walk(input/) → list downloaded files
├── yield {"type":"status", "stage":"shell", "message":"已下载 2 个文件: ..."}
│
│  ── Stage 2: Opus 4.6 Agent 数据分析 ──
│
├── yield {"type":"status", "stage":"python", "message":"正在执行数据分析 (Opus 4.6)..."}
├── Agent(Opus 4.6, tools=[shell_exec, exec_python, read_file, list_files])
│   → Agent 自主决定:
│     shell_exec("head -5 input/transactions.csv") → 预览结构
│     exec_python("import pandas as pd; df = pd.read_csv(...)") → 分析
│     → 如果 stderr → 修复代码 → 再次 exec_python（自动迭代）
│     exec_python("import matplotlib...") → 生成图表到 output/
│     print() 关键发现
├── yield {"type":"status", "stage":"python", "message":"数据分析完成"}
│
│  ── Stage 3: Opus 4.6 流式报告生成 ──
│
├── yield {"type":"status", "stage":"report", "message":"正在生成分析报告..."}
├── gather_outputs(workspace) → 收集 Stage 2 所有产出
├── bedrock_client.converse_stream(Opus 4.6, report_prompt + analysis_context)
│   → for each token:
│     yield {"type":"chunk", "content": token}
├── save report.md to output/
│
│  ── Stage 4: S3 持久化 ──
│
├── upload_file(output/*) → s3://bucket/tenants/{tenant}/reports/
├── yield {"type":"done", "s3_keys": [...]}
│
▼
Runtime A: SSE 流已逐步转发完毕
```

---

## Runtime B Internal Architecture

```python
# Runtime B 核心结构 (runtime_b_v3/main.py)

@app.entrypoint
def invoke(payload: dict, context: RequestContext):
    """返回 generator → 自动 SSE 流式响应"""
    tenant_id = payload.get("tenant_id")
    task = payload.get("task")
    s3_data_prefix = payload.get("s3_data_prefix")
    return run_analysis_pipeline(tenant_id, task, s3_data_prefix)


def run_analysis_pipeline(tenant_id, task, s3_data_prefix):
    """四阶段分析流水线"""

    workspace = setup_workspace(tenant_id)

    # ── Stage 1: Shell 数据准备 ──
    yield {"type": "status", "stage": "shell", "message": "正在下载数据..."}
    subprocess.run(f"aws s3 cp s3://{BUCKET}/{s3_data_prefix} {workspace}/input/ --recursive", ...)
    downloaded = [list files in input/]
    yield {"type": "status", "stage": "shell", "message": f"已下载 {len(downloaded)} 个文件"}

    # ── Stage 2: Agent 驱动的数据分析 ──
    yield {"type": "status", "stage": "python", "message": "正在执行数据分析 (Opus 4.6)..."}
    analysis_agent = Agent(
        model=BedrockModel(model_id="us.anthropic.claude-opus-4-6-v1", streaming=False),
        tools=[shell_exec, exec_python, read_file, list_files],
        system_prompt=ANALYSIS_PROMPT,
    )
    analysis_result = str(analysis_agent(f"任务: {task}\n已下载文件: {downloaded}"))
    yield {"type": "status", "stage": "python", "message": "数据分析完成"}

    # ── Stage 3: 流式报告生成 (直接调 LLM，不走 Agent) ──
    yield {"type": "status", "stage": "report", "message": "正在生成分析报告 (Opus 4.6 streaming)..."}
    analysis_context = gather_outputs(workspace)
    bedrock_client = boto3.client("bedrock-runtime", region_name=REGION)
    response = bedrock_client.converse_stream(
        modelId=OPUS_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": report_prompt}]}],
    )
    report_full = ""
    for event in response["stream"]:
        if "contentBlockDelta" in event:
            text = event["contentBlockDelta"]["delta"].get("text", "")
            if text:
                report_full += text
                yield {"type": "chunk", "content": text}

    # ── Stage 4: 持久化 ──
    save report_full to output/analysis_report.md
    s3_keys = upload output/* to S3
    yield {"type": "done", "s3_keys": s3_keys}
```

### Stage 2 用 Agent，Stage 3 不用 Agent

| Stage | 用 Agent? | 原因 |
|-------|----------|------|
| Stage 2 (分析) | **是** | 需要 tool use 循环：生成代码 → 执行 → 看结果 → 可能修复重试 |
| Stage 3 (报告) | **否** | 纯文本生成，不需要 tool，直接流式调 `converse_stream` 更高效 |

Stage 3 直接用 `converse_stream()` 而不是 `Agent()`，原因：
- Agent 的 streaming 会被 tool use 循环打断，不适合纯文本流式输出
- 报告生成的输入（分析结果）已经在 Stage 2 完成，不需要再调工具
- 直接 stream = 每个 token 实时推送，用户体验最好

---

## Runtime A Code Skeleton

```python
# Runtime A: 路由 + SSE 转发 (runtime_a_v3/main.py)

app = BedrockAgentCoreApp()
agentcore_dp = boto3.client("bedrock-agentcore", region_name=REGION,
                            config=Config(read_timeout=600))

@app.entrypoint
def invoke(payload: dict, context: RequestContext):
    """Generator → SSE 流式响应"""
    tenant_id = resolve_tenant_id(payload, context)
    message = payload.get("message", "")

    task_type = classify_task(message)  # Sonnet 轻量调用

    if task_type == "data_analysis":
        yield {"type": "status", "stage": "router", "message": "正在转发到分析工作站..."}
        yield from invoke_runtime_b_streaming(tenant_id, message)


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

    for line in response["response"].iter_lines():
        if line:
            line_str = line.decode("utf-8") if isinstance(line, bytes) else line
            if line_str.startswith("data: "):
                event_data = line_str[6:]
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

## Deployed Test Results

### Environment

- **Region**: us-east-1
- **Runtime A**: `arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/data_router_v3-FBOLqODKUr`
- **Runtime B**: `arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/data_workstation_v3-k2h6743g84`
- **Invocation**: `boto3.client('bedrock-agentcore').invoke_agent_runtime()`
- **Tenant**: acme-corp (Chinese cloud products, 500 txns)

### E2E Test: Full Pipeline on Deployed AgentCore

```
invoke_agent_runtime(Runtime A V3, {
    tenant_id: "acme-corp",
    message: "分析各区域Q1销售达成率，给出排名和改进建议"
})
```

**SSE Event Stream (actual output):**
```
[router]  任务类型: 数据分析，正在转发到分析工作站...
[shell]   正在从 S3 下载数据...
[shell]   已下载 2 个文件: region_targets.csv, transactions.csv
[python]  正在执行数据分析 (Opus 4.6)...
[python]  数据分析完成
[report]  正在生成分析报告 (Opus 4.6 streaming)...
          ... 1372 chunks streamed (5548 chars) ...
[done]    4 files uploaded to S3
```

**Report Preview (first 300 chars of the streamed report):**
```markdown
# 2026年Q1各区域销售达成率深度分析报告

> **报告周期**：2026年第一季度（1月–3月）

## 一、概述

2026年第一季度，公司整体销售表现**严重低于预期**。全公司Q1实际销售收入为
**¥919.5万**，仅完成 **¥1,850万** 年度Q1目标的 **49.7%**，产生高达
**¥930.5万** 的目标缺口。更值得警醒的是，**五大区域无一达标**...
```

**S3 Persisted Outputs:**
```
tenants/acme-corp/reports/
├── analysis_report.md              ← Opus 4.6 生成的深度分析报告
├── q1_region_achievement.csv       ← 区域达成率排名数据
├── q1_region_analysis.png          ← 综合分析图表
└── q1_gap_analysis.png             ← 缺口分析图表
```

### Local Test (also verified)

Same pipeline verified locally with:
- Runtime B V3 on localhost:8081
- Runtime A V3 on localhost:8080
- SSE streaming end-to-end via HTTP

### Deployment Notes

- Base image: `public.ecr.aws/docker/library/python:3.11-slim` (avoid Docker Hub rate limits)
- Runtime B Dockerfile includes `awscli` for shell-based S3 operations
- Both runtimes listen on port 8080 (AgentCore default)
- IAM roles need: S3 (read/write), Bedrock (InvokeModel), AgentCore (InvokeAgentRuntime for Runtime A)
- `converse_stream` requires inference profile model ID (`us.anthropic.claude-opus-4-6-v1`), not direct model ID
- Strands Agent also uses inference profile model ID for `converse` API
- `AWS_REGION` env var must be explicitly set — AgentCore microVM may have different default region

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

## File Structure

```
.
├── DESIGN_V3.md                    # This file
├── runtime_a_v3/
│   ├── main.py                     # Runtime A V3: Sonnet router + SSE forwarding
│   ├── Dockerfile
│   └── requirements.txt
├── runtime_b_v3/
│   ├── main.py                     # Runtime B V3: Opus 4.6 agent + shell + python + streaming report
│   ├── Dockerfile
│   └── requirements.txt
├── main_v3.py                      # Runtime A V3 source (same as runtime_a_v3/main.py)
├── generate_sample_data.py         # Generate sample data for testing
└── requirements.txt
```

---

## Deployment

```bash
# 1. Deploy Runtime B V3
cd runtime_b_v3
agentcore configure --create --name "data_workstation_v3" --entrypoint "main.py" --region "us-east-1" --non-interactive
# Enable ecr_auto_create and s3_auto_create in .bedrock_agentcore.yaml
agentcore deploy \
  --env "DATA_BUCKET=your-bucket" \
  --env "AWS_REGION=us-east-1" \
  --env "OPUS_MODEL_ID=us.anthropic.claude-opus-4-6-v1"

# 2. Add IAM permissions to Runtime B role
# S3: GetObject, PutObject, ListBucket
# Bedrock: InvokeModel, InvokeModelWithResponseStream

# 3. Deploy Runtime A V3 with Runtime B ARN
cd ../runtime_a_v3
agentcore configure --create --name "data_router_v3" --entrypoint "main.py" --region "us-east-1" --non-interactive
# Enable ecr_auto_create and s3_auto_create in .bedrock_agentcore.yaml
agentcore deploy \
  --env "DATA_BUCKET=your-bucket" \
  --env "AWS_REGION=us-east-1" \
  --env "RUNTIME_B_ARN=arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:runtime/RUNTIME_B_ID" \
  --env "MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0"

# 4. Add IAM permissions to Runtime A role
# S3: GetObject, ListBucket
# Bedrock: InvokeModel, InvokeModelWithResponseStream
# AgentCore: InvokeAgentRuntime

# 5. Test
python3 -c "
import boto3, json
from botocore.config import Config
client = boto3.client('bedrock-agentcore', region_name='us-east-1', config=Config(read_timeout=600))
resp = client.invoke_agent_runtime(
    agentRuntimeArn='RUNTIME_A_ARN',
    payload=json.dumps({'tenant_id':'acme-corp','message':'分析Q1销售达成率'}).encode(),
    contentType='application/json',
    accept='text/event-stream',
    qualifier='DEFAULT',
)
for line in resp['response'].iter_lines():
    if line:
        print(line.decode() if isinstance(line, bytes) else line)
"
```

---

## Open Questions

1. **Shell 安全**: Runtime B 的 shell_exec 是否需要白名单？LLM 生成的 shell 命令如果执行 `rm -rf /` 怎么办？
   - 建议: subprocess + timeout + 非 root 用户 + 命令白名单
2. **Opus 成本**: 每次分析任务都调 Opus 4.6，成本是否可接受？
   - 可选: Stage 2 (代码生成) 用 Sonnet，Stage 3 (报告) 才用 Opus
3. **Runtime B 冷启动**: 包含 pandas/numpy/matplotlib + awscli 的容器镜像较大，冷启动时间？
   - 可选: 预热 Session、或用 AgentCore 的 session 保活
4. **SSE 断连**: 如果 Runtime A ↔ Runtime B 的 SSE 连接中断，如何恢复？
   - 可选: Runtime B 同时将中间结果写 S3，支持从断点恢复
5. **多 Runtime B 扩展**: 如果未来需要 Web 研究、报告生成等其他类型的工作站？
   - Runtime A 作为路由器天然支持多后端: `invoke_agent_runtime(WEB_RESEARCHER_ARN)` 等
