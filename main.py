"""
Runtime A — Main Agent (Framework Worker) with Tenant Isolation

Tenant identity:
  - From custom header: X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id
  - Or from payload: tenant_id field
  - Fallback: "default"

Data isolation:
  - S3 paths are auto-prefixed: tenants/{tenant_id}/datasets/  tenants/{tenant_id}/reports/
  - Each tenant can only see and operate on their own data

Invocation to Runtime B:
  - Deployed: via boto3 invoke_agent_runtime (set RUNTIME_B_ARN)
  - Local dev: via HTTP POST to localhost:8081 (set RUNTIME_B_URL, default http://localhost:8081/invocations)
"""

import os
import json
import logging
import uuid
import contextvars
from typing import List, Optional

import boto3
from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

# ---------- Config ----------
REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("DATA_BUCKET", "")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
RUNTIME_B_ARN = os.environ.get("RUNTIME_B_ARN", "")

if not BUCKET:
    raise RuntimeError("DATA_BUCKET environment variable is required")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()
s3 = boto3.client("s3", region_name=REGION)
agentcore_dp = boto3.client("bedrock-agentcore", region_name=REGION)

# ---------- Tenant Context (contextvars — propagates through executor threads) ----------
_tenant_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("tenant_id", default="default")
_session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="unknown")

def set_tenant(tenant_id: str, session_id: str):
    _tenant_id_var.set(tenant_id)
    _session_id_var.set(session_id)

def get_tenant_id() -> str:
    return _tenant_id_var.get()

def get_session_id() -> str:
    return _session_id_var.get()

def tenant_prefix(path: str) -> str:
    """Prepend tenant-scoped prefix to an S3 path."""
    return f"tenants/{get_tenant_id()}/{path.lstrip('/')}"


# ---------- Tools ----------

@tool
def list_s3_data(prefix: str = "datasets/") -> str:
    """List available data files in S3 for the current tenant.

    Args:
        prefix: Relative prefix under the tenant's namespace, defaults to "datasets/"

    Returns:
        JSON string with list of available files and their sizes
    """
    full_prefix = tenant_prefix(prefix)
    try:
        paginator = s3.get_paginator("list_objects_v2")
        files = []
        for page in paginator.paginate(Bucket=BUCKET, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                files.append({
                    "key": obj["Key"],
                    "size_bytes": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                })
        return json.dumps({"status": "ok", "tenant": get_tenant_id(), "bucket": BUCKET,
                           "files": files, "count": len(files)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


@tool
def fetch_s3_data(s3_keys: List[str]) -> str:
    """Fetch files from S3 and return contents for preview.

    The s3_keys should be full paths as returned by list_s3_data (already tenant-scoped).

    Args:
        s3_keys: List of S3 object keys from list_s3_data results

    Returns:
        JSON with file contents (first 50 lines for large files)
    """
    tenant_id = get_tenant_id()
    results = {}
    for key in s3_keys:
        # Guard: ensure the key belongs to this tenant
        if not key.startswith(f"tenants/{tenant_id}/"):
            results[key] = {"status": "error", "error": f"Access denied: key does not belong to tenant {tenant_id}"}
            logger.warning(f"Cross-tenant access blocked: tenant={tenant_id}, key={key}")
            continue
        try:
            resp = s3.get_object(Bucket=BUCKET, Key=key)
            body = resp["Body"].read().decode("utf-8")
            lines = body.split("\n")
            if len(lines) > 50:
                results[key] = {"status": "ok", "preview": "\n".join(lines[:50]),
                                "total_lines": len(lines),
                                "note": "First 50 lines. Runtime B has access to the full file."}
            else:
                results[key] = {"status": "ok", "content": body, "total_lines": len(lines)}
        except Exception as e:
            results[key] = {"status": "error", "error": str(e)}
    return json.dumps(results, ensure_ascii=False)


@tool
def execute_on_runtime_b(
    code: str,
    s3_inputs: List[str],
    s3_output_prefix: str,
) -> str:
    """Execute Python code on Runtime B.

    Runtime B will pull s3_inputs, execute the code, and upload OUTPUT_DIR files to S3.
    All paths are already tenant-scoped — use the keys exactly as returned by list_s3_data.
    For s3_output_prefix, use a relative path like "reports/sales/2026-Q1/".

    Args:
        code: Python code. Use INPUT_DIR to read data, OUTPUT_DIR to write results.
        s3_inputs: S3 keys (full paths from list_s3_data).
        s3_output_prefix: Relative output prefix — will be auto-scoped to this tenant.

    Returns:
        JSON with stdout, stderr, exit_code, uploaded_files
    """
    tenant_id = get_tenant_id()
    session_id = get_session_id()

    # Guard: all s3_inputs must belong to this tenant
    for key in s3_inputs:
        if not key.startswith(f"tenants/{tenant_id}/"):
            logger.warning(f"Cross-tenant access blocked in execute: tenant={tenant_id}, key={key}")
            return json.dumps({"status": "error", "error": f"Access denied: {key} not in tenant {tenant_id}"})

    # Output prefix is always tenant-scoped
    full_output_prefix = tenant_prefix(s3_output_prefix)

    invoke_payload = {
        "action": "execute",
        "code": code,
        "s3_inputs": s3_inputs,
        "s3_output_prefix": full_output_prefix,
        "tenant_id": tenant_id,
        "session_id": session_id,
    }

    if not RUNTIME_B_ARN:
        return _execute_local(invoke_payload)

    try:
        response = agentcore_dp.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_B_ARN,
            payload=json.dumps(invoke_payload).encode("utf-8"),
            contentType="application/json",
            qualifier="DEFAULT",
        )
        body = response["response"].read().decode("utf-8")
        return body
    except Exception as e:
        logger.error(f"Runtime B invocation failed: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)})


def _execute_local(invoke_payload):
    """Local dev fallback: call Runtime B via HTTP POST.

    When RUNTIME_B_ARN is not set, Runtime A calls Runtime B directly
    at RUNTIME_B_URL (default: http://localhost:8081/invocations).
    This is used for local development and was verified in testing.
    """
    import urllib.request
    runtime_b_url = os.environ.get("RUNTIME_B_URL", "http://localhost:8081/invocations")
    data = json.dumps(invoke_payload).encode("utf-8")
    req = urllib.request.Request(runtime_b_url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        return json.dumps({"status": "error", "error": f"Local Runtime B call failed: {e}"})


# ---------- System Prompt (built per-request with tenant context) ----------

def build_system_prompt(tenant_id: str) -> str:
    return f"""你是一个数据分析 Agent，运行在 AWS AgentCore Runtime A 中。
当前租户: {tenant_id}
你只能访问和操作属于当前租户的数据。

## 工具
1. **list_s3_data** — 列出当前租户可用的数据文件
2. **fetch_s3_data** — 预览数据内容（用于了解结构）
3. **execute_on_runtime_b** — 发送 Python 代码到 Runtime B 执行

## 工作流程
1. list_s3_data 查看可用数据
2. fetch_s3_data 预览结构
3. 生成 Python 分析代码
4. execute_on_runtime_b 执行:
   - s3_inputs: 使用 list_s3_data 返回的完整 key（已含租户前缀）
   - s3_output_prefix: 相对路径如 "reports/sales/2026-Q1/"（会自动加租户前缀）
   - code: 用 INPUT_DIR 读数据, OUTPUT_DIR 写结果
5. 如果有错误，修正代码重试（最多 3 次）
6. 汇报结论和结果文件 S3 位置

## 代码规则
- `pd.read_csv(f"{{INPUT_DIR}}/filename.csv")`
- `df.to_csv(f"{{OUTPUT_DIR}}/result.csv", index=False)`
- 可用库: pandas, numpy, matplotlib
- 用 print() 输出关键结论

## 数据说明
- 数据桶: s3://{BUCKET}/
- 本租户数据: tenants/{tenant_id}/datasets/
- 本租户结果: tenants/{tenant_id}/reports/
"""


# ---------- Entrypoint ----------

CUSTOM_TENANT_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-tenant-id"

def resolve_tenant_id(payload: dict, context: RequestContext) -> str:
    """Extract tenant_id from: custom header > payload > fallback 'default'."""
    # 1. AgentCore custom header
    if context.request_headers:
        for k, v in context.request_headers.items():
            if k.lower() == CUSTOM_TENANT_HEADER:
                return v
    # 2. Payload field
    if payload.get("tenant_id"):
        return payload["tenant_id"]
    # 3. Fallback
    return "default"


@app.entrypoint
def invoke(payload: dict, context: RequestContext) -> dict:
    tenant_id = resolve_tenant_id(payload, context)
    session_id = context.session_id or payload.get("session_id", str(uuid.uuid4())[:8])
    message = payload.get("message", payload.get("input", payload.get("prompt", "")))

    if not message:
        return {"output": "请提供分析请求。", "status": "error"}

    # Set tenant context for this request
    set_tenant(tenant_id, session_id)
    logger.info(f"Tenant: {tenant_id} | Session: {session_id} | Query: {message[:80]}...")

    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID, streaming=True),
        tools=[list_s3_data, fetch_s3_data, execute_on_runtime_b],
        system_prompt=build_system_prompt(tenant_id),
    )

    try:
        response = agent(message)
        return {
            "output": str(response),
            "status": "success",
            "tenant_id": tenant_id,
            "session_id": session_id,
        }
    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        return {"output": f"分析过程出错: {str(e)}", "status": "error",
                "tenant_id": tenant_id, "session_id": session_id}


if __name__ == "__main__":
    app.run(port=8080)
