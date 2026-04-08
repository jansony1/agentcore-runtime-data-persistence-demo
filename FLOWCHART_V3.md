# V3 Complete Workflow Flowchart

## End-to-End Flow

```
用户请求: "分析各区域Q1销售达成率"
│
▼
┌─── Runtime A ─────────────────────────────────────────────────────────┐
│                                                                       │
│  @app.entrypoint (generator → SSE)                                    │
│  │                                                                    │
│  │  ① resolve tenant_id + session_id                                  │
│  │                                                                    │
│  │  ② yield {"type":"status","message":"Agent 开始分析..."}           │
│  │     └→ 前端立刻收到                                                │
│  │                                                                    │
│  │  ③ ┌─── Agent A tool loop (Sonnet, converse API) ──────────┐      │
│  │    │                                                        │      │
│  │    │  LLM 思考: "需要下载数据"                               │      │
│  │    │    │                                                   │      │
│  │    │    ▼ yield {"type":"status","message":"Shell: s3 cp"}  │      │
│  │    │    │  └→ 前端收到                                      │      │
│  │    │    ▼ invoke_runtime_b("shell", cmd) ───────────────────┼──┐   │
│  │    │    │                                                   │  │   │
│  │    │    ▼ yield {"type":"status","message":"Shell 完成"}     │  │   │
│  │    │    │  └→ 前端收到                                      │  │   │
│  │    │    ▼                                                   │  │   │
│  │    │  LLM 思考: "看看数据结构"                               │  │   │
│  │    │    │                                                   │  │   │
│  │    │    ▼ yield {"type":"status","message":"Shell: head"}   │  │   │
│  │    │    ▼ invoke_runtime_b("shell", cmd) ───────────────────┼──┤   │
│  │    │    ▼ yield {"type":"status","message":"Shell 完成"}     │  │   │
│  │    │    ▼                                                   │  │   │
│  │    │  LLM 思考: "写 pandas 分析代码"                         │  │   │
│  │    │    │                                                   │  │   │
│  │    │    ▼ yield {"type":"status","message":"Python: 分析"}  │  │   │
│  │    │    ▼ invoke_runtime_b("python", code) ─────────────────┼──┤   │
│  │    │    ▼ yield {"type":"status","message":"Python 完成"}    │  │   │
│  │    │    ▼                                                   │  │   │
│  │    │  LLM 思考: "生成图表"                                   │  │   │
│  │    │    │                                                   │  │   │
│  │    │    ▼ yield {"type":"status","message":"Python: 图表"}  │  │   │
│  │    │    ▼ invoke_runtime_b("python", code) ─────────────────┼──┤   │
│  │    │    ▼ yield {"type":"status","message":"Python 完成"}    │  │   │
│  │    │    ▼                                                   │  │   │
│  │    │  LLM: stop_reason=end_turn → 退出循环                  │  │   │
│  │    │                                                        │  │   │
│  │    └────────────────────────────────────────────────────────┘  │   │
│  │                                                                │   │
│  │  ④ yield {"type":"status","message":"数据准备完成"}             │   │
│  │     └→ 前端收到                                                │   │
│  │                                                                │   │
│  │  ⑤ yield {"type":"status","message":"正在分析并生成报告..."}    │   │
│  │     └→ 前端收到                                                │   │
│  │                                                                │   │
│  │  ⑥ invoke_runtime_b("report") ────────────────────────────────┼──┤ │
│  │    │                                                           │  │ │
│  │    │  ← SSE: {"type":"chunk","content":"# Q1 报告\n"}         │  │ │
│  │    │  yield → 前端                                             │  │ │
│  │    │                                                           │  │ │
│  │    │  ← SSE: {"type":"chunk","content":"## 关键发现\n"}        │  │ │
│  │    │  yield → 前端                                             │  │ │
│  │    │                                                           │  │ │
│  │    │  ← SSE: {"type":"chunk","content":"华中 64.4%..."}       │  │ │
│  │    │  yield → 前端                                             │  │ │
│  │    │                                                           │  │ │
│  │    │  ... (数百个 chunks) ...                                  │  │ │
│  │    │                                                           │  │ │
│  │    │  ← SSE: {"type":"done","s3_keys":[...]}                  │  │ │
│  │    │  yield → 前端                                             │  │ │
│  │                                                                │   │
└──┼────────────────────────────────────────────────────────────────┼───┘
   │                                                                │
   │  invoke_agent_runtime (同一个 session_id)                      │
   │                                                                │
   ▼                                                                │
┌─── Runtime B ─────────────────────────────────────────────────────┘──┐
│                                                                      │
│  @app.entrypoint                                                     │
│  │                                                                   │
│  ├── action="shell"                                                  │
│  │   subprocess.run(command, cwd="/tmp/workspace")                   │
│  │   return {"stdout": "...", "exit_code": 0}                        │
│  │   ↑ 文件落盘到 /tmp/workspace，同 session 内持久                   │
│  │                                                                   │
│  ├── action="python"                                                 │
│  │   exec(code)  # WORKSPACE = "/tmp/workspace"                      │
│  │   return {"stdout": "...", "exit_code": 0, "output_files": [...]} │
│  │   ↑ 读之前 shell 下载的文件，结果写到 /tmp/workspace/output/        │
│  │                                                                   │
│  └── action="report"  ← 唯一使用 LLM 的 action                      │
│      │                                                               │
│      │  读 /tmp/workspace/output/ 里的 CSV/图表                      │
│      │  ↑ 直接读文件，不依赖 Agent A 的文字总结                       │
│      │                                                               │
│      │  converse_stream(Opus 4.6, 原始数据 + 任务描述)               │
│      │  ↑ 分析 + 报告生成合一                                        │
│      │                                                               │
│      │  for token in stream:                                         │
│      │      yield {"type":"chunk","content": token}                  │
│      │  ↑ 逐 token SSE 推送                                         │
│      │                                                               │
│      │  save report.md + upload output/* to S3                       │
│      │  yield {"type":"done","s3_keys":[...]}                        │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

## Key Code: Runtime A (手写 tool loop，全程流式)

```python
@app.entrypoint
def invoke(payload: dict, context: RequestContext):
    tenant_id = resolve_tenant_id(payload, context)
    session_id = context.session_id or str(uuid.uuid4())
    message = payload.get("message", "")

    # ── Phase 1: 手写 tool loop（每步可 yield 状态）──

    yield {"type": "status", "stage": "analysis", "message": "Agent 开始分析..."}

    bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    messages = [{"role": "user", "content": [{"text":
        f"租户数据在 S3: s3://{BUCKET}/tenants/{tenant_id}/datasets/\n"
        f"任务: {message}\n"
        f"输出文件保存到 /tmp/workspace/output/"
    }]}]

    analysis_context = ""

    while True:
        # ① 调 LLM（Sonnet）决定下一步
        response = bedrock.converse(
            modelId=MODEL_ID,
            messages=messages,
            system=[{"text": SYSTEM_PROMPT}],
            toolConfig=TOOL_CONFIG,
        )

        assistant_message = response["output"]["message"]
        messages.append(assistant_message)

        # ② 检查是否结束
        if response["stopReason"] == "end_turn":
            # 提取 LLM 最终文字输出
            for block in assistant_message["content"]:
                if "text" in block:
                    analysis_context = block["text"]
            break

        # ③ 处理 tool calls
        tool_results = []
        for block in assistant_message["content"]:
            if "toolUse" not in block:
                continue

            tool_use = block["toolUse"]
            tool_name = tool_use["name"]
            tool_input = tool_use["input"]
            tool_use_id = tool_use["toolUseId"]

            # yield 状态 → 前端实时看到
            yield {"type": "status", "stage": "analysis",
                   "message": f"正在执行: {tool_name}"}

            # 调 Runtime B
            result = invoke_runtime_b(tool_name, tool_input, session_id)

            yield {"type": "status", "stage": "analysis",
                   "message": f"{tool_name} 完成 (exit_code={result.get('exit_code',0)})"}

            tool_results.append({
                "toolUseId": tool_use_id,
                "content": [{"text": json.dumps(result, ensure_ascii=False)}],
            })

        # ④ 把 tool results 送回 LLM 继续思考
        messages.append({"role": "user", "content": [
            {"toolResult": tr} for tr in tool_results
        ]})

    yield {"type": "status", "stage": "analysis", "message": "数据准备完成"}

    # ── Phase 2: 报告（Runtime B 的 LLM 分析+生成）──

    yield {"type": "status", "stage": "report", "message": "正在分析数据并生成报告..."}

    yield from invoke_runtime_b_report_stream(
        context=analysis_context,
        s3_output_prefix=f"tenants/{tenant_id}/reports/",
        session_id=session_id,
    )


# ── Tool dispatch ──

def invoke_runtime_b(tool_name: str, tool_input: dict, session_id: str) -> dict:
    """Route tool call to Runtime B action."""
    action_map = {
        "runtime_b_shell": ("shell", {"command": tool_input.get("command", "")}),
        "runtime_b_python": ("python", {"code": tool_input.get("code", "")}),
    }
    action, payload = action_map[tool_name]
    payload["action"] = action

    response = agentcore_dp.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_B_ARN,
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
        runtimeSessionId=session_id,
        qualifier="DEFAULT",
    )
    return json.loads(response["response"].read().decode("utf-8"))


# ── Tool definitions for Bedrock Converse API ──

TOOL_CONFIG = {"tools": [
    {
        "toolSpec": {
            "name": "runtime_b_shell",
            "description": "Execute a shell command on the remote workspace (/tmp/workspace). "
                           "Use for: aws s3 cp, head, tail, wc, sort, ls, cat, pip install.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"}
                },
                "required": ["command"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "runtime_b_python",
            "description": "Execute Python code on the remote workspace. "
                           "WORKSPACE='/tmp/workspace'. Write outputs to WORKSPACE+'/output/'. "
                           "Available: pandas, numpy, matplotlib. Use print() for key findings.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"}
                },
                "required": ["code"],
            }},
        }
    },
]}
```

## Key Code: Runtime B report action (分析 + 报告合一)

```python
def handle_report(payload: dict):
    """读 workspace 原始数据 → Opus 分析+生成报告 → SSE 流式输出"""
    task_context = payload.get("context", "")
    s3_output_prefix = payload.get("s3_output_prefix", "")

    yield {"type": "status", "stage": "report",
           "message": "正在分析数据并生成报告 (Opus 4.6 streaming)..."}

    # ① 读 workspace 里的原始数据文件（不依赖 Agent A 的文字总结）
    data_context = ""
    output_dir = os.path.join(WORKSPACE, "output")
    if os.path.isdir(output_dir):
        for fname in os.listdir(output_dir):
            if fname.endswith(".csv"):
                filepath = os.path.join(output_dir, fname)
                with open(filepath, "r") as f:
                    content = f.read(5000)
                data_context += f"\n\n### {fname}\n```csv\n{content}\n```"

    # ② 组装 prompt：任务描述 + Agent A 的上下文 + 原始数据
    full_prompt = (
        f"## 任务\n{task_context}\n\n"
        f"## 原始数据文件（来自 workspace）\n{data_context}\n\n"
        f"请基于以上数据进行深度分析，撰写专业的 Markdown 分析报告。"
    )

    # ③ Opus 4.6 流式生成（分析 + 报告合一）
    bedrock_client = boto3.client("bedrock-runtime", region_name=REGION)
    report_full = ""

    response = bedrock_client.converse_stream(
        modelId=OPUS_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": full_prompt}]}],
        system=[{"text": "你是专业数据分析师。基于原始数据进行深度分析，"
                         "撰写 Markdown 报告。包含概述、关键发现、详细分析、"
                         "数据表格、改进建议。引用具体数字。"}],
    )

    for event in response["stream"]:
        if "contentBlockDelta" in event:
            text = event["contentBlockDelta"]["delta"].get("text", "")
            if text:
                report_full += text
                yield {"type": "chunk", "content": text}

    # ④ 保存报告 + 上传 S3
    if report_full:
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, "analysis_report.md"), "w") as f:
            f.write(report_full)

    uploaded = upload_outputs_to_s3(s3_output_prefix)
    yield {"type": "done", "s3_keys": uploaded}
```

## 用户视角（全程流式，零黑盒）

```
[analysis] Agent 开始分析...
[analysis] 正在执行: runtime_b_shell (aws s3 cp ...)     ← Phase 1 不再是黑盒
[analysis] runtime_b_shell 完成 (exit_code=0)
[analysis] 正在执行: runtime_b_shell (head -5 ...)
[analysis] runtime_b_shell 完成 (exit_code=0)
[analysis] 正在执行: runtime_b_python (pandas 分析)
[analysis] runtime_b_python 完成 (exit_code=0)
[analysis] 正在执行: runtime_b_python (matplotlib 图表)
[analysis] runtime_b_python 完成 (exit_code=0)
[analysis] 数据准备完成
[report]   正在分析数据并生成报告...                       ← Phase 2 逐 token
[chunk]    # Q1 各区域销售达成率深度分析报告
[chunk]    ## 一、概述
[chunk]    根据对 500 条交易记录的分析...
[chunk]    ## 二、关键发现
[chunk]    | 排名 | 区域 | 达成率 | ...
           ...（数百个 chunks）...
[done]     5 files → S3
```
