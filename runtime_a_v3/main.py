"""
Runtime A V3 — Agent A (The Only Brain) with Full SSE Streaming

Agent A controls Runtime B through tools (shell, python).
Uses Strands agent.stream_async() to yield events during the tool loop,
so every tool call and LLM thinking step is visible to the frontend.

Flow:
  Phase 1 (streaming): Agent A stream_async → yield tool call/result/thinking events
  Phase 2 (streaming): Forward Runtime B report SSE → frontend
  = Full SSE end-to-end, zero black box
"""

import os
import json
import logging
import uuid
import asyncio

import boto3
from botocore.config import Config
from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

# ---------- Config ----------
REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("DATA_BUCKET", "")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-opus-4-6-v1")
RUNTIME_B_ARN = os.environ.get("RUNTIME_B_ARN", "")

RUNTIME_B_URL = os.environ.get("RUNTIME_B_URL", "")  # Local dev fallback

if not BUCKET:
    raise RuntimeError("DATA_BUCKET environment variable is required")
if not RUNTIME_B_ARN and not RUNTIME_B_URL:
    raise RuntimeError("RUNTIME_B_ARN or RUNTIME_B_URL environment variable is required")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()
agentcore_dp = boto3.client("bedrock-agentcore", region_name=REGION,
                            config=Config(read_timeout=300))

# ---------- Context (module-level, safe for single-request-per-microVM) ----------
_current_tenant_id = "default"
_current_session_id = "default"

def get_tenant_id() -> str:
    return _current_tenant_id

def get_session_id() -> str:
    return _current_session_id


# ---------- Runtime B Invocation ----------

def invoke_runtime_b(action: str, payload_extra: dict) -> dict:
    """Invoke Runtime B with an action. Returns parsed JSON."""
    session_id = get_session_id()
    payload = {"action": action, **payload_extra}
    data = json.dumps(payload).encode("utf-8")

    if RUNTIME_B_URL:
        import urllib.request
        req = urllib.request.Request(RUNTIME_B_URL, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))

    response = agentcore_dp.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_B_ARN,
        payload=data,
        contentType="application/json",
        runtimeSessionId=session_id,
        qualifier="DEFAULT",
    )
    body = response["response"].read().decode("utf-8")
    return json.loads(body)


def invoke_runtime_b_report_stream(context: str, s3_output_prefix: str):
    """Invoke Runtime B report action and yield SSE events."""
    session_id = get_session_id()
    payload = json.dumps({
        "action": "report",
        "context": context,
        "s3_output_prefix": s3_output_prefix,
    }).encode("utf-8")

    if RUNTIME_B_URL:
        import urllib.request
        req = urllib.request.Request(RUNTIME_B_URL, data=payload,
                                    headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("data: "):
                    try:
                        yield json.loads(line[6:])
                    except json.JSONDecodeError:
                        yield {"type": "raw", "content": line[6:]}
        return

    response = agentcore_dp.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_B_ARN,
        payload=payload,
        contentType="application/json",
        accept="text/event-stream",
        runtimeSessionId=session_id,
        qualifier="DEFAULT",
    )
    for line in response["response"].iter_lines():
        if line:
            line_str = line.decode("utf-8") if isinstance(line, bytes) else line
            if line_str.startswith("data: "):
                try:
                    yield json.loads(line_str[6:])
                except json.JSONDecodeError:
                    yield {"type": "raw", "content": line_str[6:]}


# ---------- Agent Tools (remote control Runtime B) ----------

@tool
def runtime_b_shell(command: str) -> str:
    """Execute a shell command on Runtime B's workspace.

    Use for: aws s3 cp, head, tail, wc, sort, ls, cat, file inspection, pip install.
    The workspace persists across calls within the same session.
    Working directory is /tmp/workspace.

    Args:
        command: Shell command to execute

    Returns:
        JSON with stdout, stderr, exit_code
    """
    result = invoke_runtime_b("shell", {"command": command})
    logger.info(f"Shell [{result.get('exit_code')}]: {command[:80]}")
    return json.dumps(result, ensure_ascii=False)


@tool
def runtime_b_python(code: str) -> str:
    """Execute Python code on Runtime B's workspace.

    The WORKSPACE variable is available as '/tmp/workspace'.
    Files written to WORKSPACE persist across calls.
    Write output files to WORKSPACE + '/output/' for S3 upload.

    Use for: pandas analysis, numpy computation, matplotlib charts.

    Args:
        code: Python code to execute. Use WORKSPACE variable for file paths.

    Returns:
        JSON with stdout, stderr, exit_code, output_files
    """
    result = invoke_runtime_b("python", {"code": code})
    logger.info(f"Python [{result.get('exit_code')}]: {len(code)} chars")
    return json.dumps(result, ensure_ascii=False)


# ---------- System Prompt ----------

SYSTEM_PROMPT = f"""你是一个数据分析 Agent，你是唯一的决策者。

## 你的工具
1. **runtime_b_shell** — 在远程工作站上执行 shell 命令
   - 工作目录: /tmp/workspace（持久化，跨调用共享）
   - 用于: aws s3 cp, head, wc, sort, ls, cat 等
2. **runtime_b_python** — 在远程工作站上执行 Python 代码
   - 可用变量: WORKSPACE = "/tmp/workspace"
   - 输出文件写到: WORKSPACE + "/output/"
   - 可用库: pandas, numpy, matplotlib

## 工作流程
1. 用 runtime_b_shell 从 S3 下载数据到工作站
2. 用 runtime_b_shell 预览数据结构 (head, wc 等)
3. 用 runtime_b_python 执行 pandas 数据处理和计算
4. 如果代码报错，阅读 stderr，修正代码，重试（最多 3 次）
5. 用 runtime_b_python 生成 matplotlib 图表，保存到 /tmp/workspace/output/
6. 将处理后的数据（CSV）保存到 /tmp/workspace/output/

## 重要规则
- 数据桶: s3://{BUCKET}/
- 图表和 CSV 保存到 /tmp/workspace/output/
- 你负责数据准备和计算，最终的深度分析和报告将由下游 LLM 基于你产出的数据文件完成
"""


# ---------- Tenant Resolution ----------

CUSTOM_TENANT_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-tenant-id"

def resolve_tenant_id(payload: dict, context: RequestContext) -> str:
    if context.request_headers:
        for k, v in context.request_headers.items():
            if k.lower() == CUSTOM_TENANT_HEADER:
                return v
    return payload.get("tenant_id", "default")


# ---------- Entrypoint ----------

@app.entrypoint
async def invoke(payload: dict, context: RequestContext):
    """Async generator → SSE. Full streaming via agent.stream_async()."""
    tenant_id = resolve_tenant_id(payload, context)
    session_id = context.session_id or payload.get("session_id", str(uuid.uuid4()))
    message = payload.get("message", "")

    if not message:
        yield {"type": "error", "message": "请提供分析请求"}
        return

    global _current_tenant_id, _current_session_id
    _current_tenant_id = tenant_id
    _current_session_id = session_id
    s3_prefix = f"tenants/{tenant_id}/datasets/"
    s3_output_prefix = f"tenants/{tenant_id}/reports/"

    logger.info(f"Tenant: {tenant_id} | Session: {session_id} | Query: {message[:80]}")

    # === Phase 1: Agent stream_async (every step visible) ===

    yield {"type": "status", "stage": "analysis", "message": "Agent 开始分析..."}

    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID, streaming=True),
        tools=[runtime_b_shell, runtime_b_python],
        system_prompt=SYSTEM_PROMPT,
    )

    analysis_result = ""
    current_tool_name = None

    try:
        async for event in agent.stream_async(
            f"租户数据在 S3: s3://{BUCKET}/{s3_prefix}\n"
            f"请下载数据并完成以下分析任务: {message}\n"
            f"输出文件保存到 /tmp/workspace/output/"
        ):
            if not isinstance(event, dict):
                continue

            event_type = event.get("type", "")

            # Tool call starting
            if event_type == "tool_use_stream":
                tool_info = event.get("current_tool_use", {})
                tool_name = tool_info.get("name", "")
                if tool_name and tool_name != current_tool_name:
                    current_tool_name = tool_name
                    yield {"type": "status", "stage": "analysis",
                           "message": f"正在执行: {tool_name}"}

            # Tool result returned
            elif event_type == "tool_result":
                tr = event.get("tool_result", {})
                status = tr.get("status", "unknown")
                yield {"type": "status", "stage": "analysis",
                       "message": f"{current_tool_name} 完成 (status={status})"}
                current_tool_name = None

            # LLM text output (thinking / final answer)
            elif "data" in event and event.get("data"):
                text = str(event["data"])
                analysis_result += text

            # Final result
            elif "result" in event:
                result_obj = event.get("result")
                if result_obj and hasattr(result_obj, "message"):
                    # Extract final text from result message
                    msg = result_obj.message
                    if hasattr(msg, "content"):
                        for block in msg.content:
                            if hasattr(block, "text"):
                                analysis_result = block.text

        logger.info(f"Agent analysis complete: {len(analysis_result)} chars")

    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        yield {"type": "error", "stage": "analysis", "message": str(e)}
        return

    yield {"type": "status", "stage": "analysis", "message": "数据准备完成"}

    # === Phase 2: Report streaming (Runtime B analyzes data + generates report) ===

    yield {"type": "status", "stage": "report", "message": "正在分析数据并生成报告..."}

    # invoke_runtime_b_report_stream is sync generator, wrap for async
    for event in invoke_runtime_b_report_stream(
        context=analysis_result,
        s3_output_prefix=s3_output_prefix,
    ):
        yield event


if __name__ == "__main__":
    app.run(port=8080)
