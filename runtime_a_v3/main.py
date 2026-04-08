"""
Runtime A V3 — Agent A (The Only Brain)

Agent A is the sole decision-maker. It controls Runtime B through tools:
  - runtime_b_shell: execute shell commands on Runtime B
  - runtime_b_python: execute Python code on Runtime B

After Agent A completes analysis, the entrypoint invokes Runtime B's
report action and forwards the SSE stream to the frontend.

Flow:
  Phase 1 (blocking): Agent A tool loop → shell/python on Runtime B
  Phase 2 (streaming): Forward Runtime B report SSE → frontend
"""

import os
import json
import logging
import uuid
from typing import Optional

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

if not BUCKET:
    raise RuntimeError("DATA_BUCKET environment variable is required")
if not RUNTIME_B_ARN:
    raise RuntimeError("RUNTIME_B_ARN environment variable is required")

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
    """Invoke Runtime B with an action via invoke_agent_runtime. Returns parsed JSON."""
    session_id = get_session_id()
    payload = {"action": action, **payload_extra}

    response = agentcore_dp.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_B_ARN,
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
        runtimeSessionId=session_id,
        qualifier="DEFAULT",
    )
    body = response["response"].read().decode("utf-8")
    return json.loads(body)


def invoke_runtime_b_report_stream(context: str, s3_output_prefix: str):
    """Invoke Runtime B report action via invoke_agent_runtime and yield SSE events."""
    session_id = get_session_id()
    payload = json.dumps({
        "action": "report",
        "context": context,
        "s3_output_prefix": s3_output_prefix,
    }).encode("utf-8")

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
3. 用 runtime_b_python 执行 pandas 分析
4. 如果代码报错，阅读 stderr，修正代码，重试（最多 3 次）
5. 用 runtime_b_python 生成 matplotlib 图表，保存到 /tmp/workspace/output/
6. 用 print() 输出所有关键发现（数字、排名、结论），这些会被用于生成最终报告

## 重要规则
- 数据桶: s3://{BUCKET}/
- 总是 print() 关键分析结论和数据表格
- 图表和 CSV 保存到 /tmp/workspace/output/
- 你的输出会被传给报告生成器，所以要尽可能详细和有数据支撑
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
def invoke(payload: dict, context: RequestContext):
    """Generator → SSE. Phase 1: Agent analysis. Phase 2: Report streaming."""
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

    # === Phase 1: Agent-driven analysis (blocking) ===
    yield {"type": "status", "stage": "analysis", "message": "Agent 开始分析..."}

    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID, streaming=False),
        tools=[runtime_b_shell, runtime_b_python],
        system_prompt=SYSTEM_PROMPT,
    )

    try:
        analysis_result = str(agent(
            f"租户数据在 S3: s3://{BUCKET}/{s3_prefix}\n"
            f"请下载数据并完成以下分析任务: {message}\n"
            f"输出文件保存到 /tmp/workspace/output/"
        ))
        logger.info(f"Agent analysis complete: {len(analysis_result)} chars")
    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        yield {"type": "error", "stage": "analysis", "message": str(e)}
        return

    yield {"type": "status", "stage": "analysis", "message": "数据分析完成"}

    # === Phase 2: Report streaming (forward SSE from Runtime B) ===
    yield {"type": "status", "stage": "report", "message": "正在生成分析报告..."}

    yield from invoke_runtime_b_report_stream(
        context=analysis_result,
        s3_output_prefix=s3_output_prefix,
    )


if __name__ == "__main__":
    app.run(port=8080)
