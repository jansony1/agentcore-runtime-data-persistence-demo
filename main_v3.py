"""
Runtime A V3 — Router + SSE Forwarding

Receives user requests, routes to Runtime B V3, and forwards SSE stream back to user.
Uses Sonnet for lightweight task classification.
"""

import os
import json
import logging
import uuid
import contextvars
from typing import Optional

import boto3
from botocore.config import Config
from strands import Agent
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

# ---------- Config ----------
REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("DATA_BUCKET", "")
SONNET_MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
RUNTIME_B_ARN = os.environ.get("RUNTIME_B_ARN", "")

if not BUCKET:
    raise RuntimeError("DATA_BUCKET environment variable is required")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()
agentcore_dp = boto3.client("bedrock-agentcore", region_name=REGION,
                            config=Config(read_timeout=600))

# ---------- Tenant Context ----------
_tenant_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("tenant_id", default="default")

def set_tenant(tenant_id: str):
    _tenant_id_var.set(tenant_id)

def get_tenant_id() -> str:
    return _tenant_id_var.get()


# ---------- Task Classification ----------

CLASSIFY_PROMPT = """你是一个任务分类器。根据用户输入，判断任务类型。只返回一个分类标签，不要解释。

分类:
- data_analysis: 数据分析、统计、报表、达成率、趋势分析
- general: 其他

用户输入: {message}

分类标签:"""


def classify_task(message: str) -> str:
    """Lightweight Sonnet call to classify task type."""
    try:
        bedrock = boto3.client("bedrock-runtime", region_name=REGION)
        response = bedrock.converse(
            modelId=SONNET_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": CLASSIFY_PROMPT.format(message=message)}]}],
        )
        label = response["output"]["message"]["content"][0]["text"].strip().lower()
        if "data" in label or "analysis" in label:
            return "data_analysis"
        return label
    except Exception as e:
        logger.warning(f"Classification failed, defaulting to data_analysis: {e}")
        return "data_analysis"


# ---------- SSE Forwarding ----------

def invoke_runtime_b_streaming(tenant_id: str, task: str):
    """Call Runtime B V3 and yield SSE events as they arrive."""

    payload = json.dumps({
        "tenant_id": tenant_id,
        "task": task,
        "s3_data_prefix": f"tenants/{tenant_id}/datasets/",
    }).encode("utf-8")

    if not RUNTIME_B_ARN:
        # Local dev: HTTP call to localhost
        yield from _invoke_local_streaming(payload)
        return

    try:
        response = agentcore_dp.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_B_ARN,
            payload=payload,
            contentType="application/json",
            accept="text/event-stream",
            qualifier="DEFAULT",
        )

        # StreamingBody → parse SSE lines → yield events
        for line in response["response"].iter_lines():
            if line:
                line_str = line.decode("utf-8") if isinstance(line, bytes) else line
                if line_str.startswith("data: "):
                    event_data = line_str[6:]
                    try:
                        yield json.loads(event_data)
                    except json.JSONDecodeError:
                        yield {"type": "raw", "content": event_data}

    except Exception as e:
        logger.error(f"Runtime B invocation failed: {e}", exc_info=True)
        yield {"type": "error", "message": f"Runtime B 调用失败: {str(e)}"}


def _invoke_local_streaming(payload: bytes):
    """Local dev: call Runtime B via HTTP and parse SSE response."""
    import urllib.request

    runtime_b_url = os.environ.get("RUNTIME_B_URL", "http://localhost:8081/invocations")
    req = urllib.request.Request(
        runtime_b_url, data=payload,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )

    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("data: "):
                    event_data = line[6:]
                    try:
                        yield json.loads(event_data)
                    except json.JSONDecodeError:
                        yield {"type": "raw", "content": event_data}
    except Exception as e:
        yield {"type": "error", "message": f"Local Runtime B call failed: {str(e)}"}


# ---------- Tenant Resolution ----------

CUSTOM_TENANT_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-tenant-id"

def resolve_tenant_id(payload: dict, context: RequestContext) -> str:
    if context.request_headers:
        for k, v in context.request_headers.items():
            if k.lower() == CUSTOM_TENANT_HEADER:
                return v
    if payload.get("tenant_id"):
        return payload["tenant_id"]
    return "default"


# ---------- Entrypoint ----------

@app.entrypoint
def invoke(payload: dict, context: RequestContext):
    """Generator entrypoint → SSE streaming response."""
    tenant_id = resolve_tenant_id(payload, context)
    session_id = context.session_id or payload.get("session_id", uuid.uuid4().hex[:8])
    message = payload.get("message", payload.get("task", ""))

    if not message:
        yield {"type": "error", "message": "请提供分析请求"}
        return

    set_tenant(tenant_id)
    logger.info(f"Tenant: {tenant_id} | Session: {session_id} | Query: {message[:80]}...")

    # Classify task
    task_type = classify_task(message)
    logger.info(f"Task type: {task_type}")

    if task_type == "data_analysis":
        yield {"type": "status", "stage": "router", "message": f"任务类型: 数据分析，正在转发到分析工作站..."}
        yield from invoke_runtime_b_streaming(tenant_id, message)
    else:
        yield {"type": "status", "stage": "router", "message": f"暂不支持的任务类型: {task_type}"}


if __name__ == "__main__":
    app.run(port=8080)
