"""
Runtime B — Code Executor with Tenant Isolation

Workspace isolation: /tmp/workspace/{tenant_id}/{session_id}/
S3 paths: tenant-scoped keys are passed in from Runtime A (already prefixed).
Runtime B trusts the tenant_id from Runtime A (same trust boundary).

Supported actions via /invocations:
  - execute:      pull S3 data → exec code → push results to S3 → return summary
  - write_files:  write content to local workspace (for multi-step workflows)
  - read_files:   read files from local workspace
  - list_files:   list workspace contents
"""

import os
import io
import sys
import json
import logging
import traceback

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("DATA_BUCKET", "")
WORKSPACE = os.environ.get("WORKSPACE_DIR", "/tmp/workspace")

if not BUCKET:
    raise RuntimeError("DATA_BUCKET environment variable is required")

app = BedrockAgentCoreApp()
s3 = boto3.client("s3", region_name=REGION)


def ensure_workspace(tenant_id: str, session_id: str) -> str:
    """Get or create a tenant+session scoped workspace directory."""
    ws = os.path.join(WORKSPACE, tenant_id, session_id)
    os.makedirs(ws, exist_ok=True)
    return ws


# ---------- Action handlers ----------

def handle_execute(payload: dict, workspace: str) -> dict:
    """Pull S3 data → exec Python code → push results to S3."""
    code = payload.get("code", "")
    s3_inputs = payload.get("s3_inputs", [])
    s3_output_prefix = payload.get("s3_output_prefix", "")

    # 1. Pull input files from S3 to workspace
    input_dir = os.path.join(workspace, "input")
    output_dir = os.path.join(workspace, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    for s3_key in s3_inputs:
        filename = os.path.basename(s3_key)
        local_path = os.path.join(input_dir, filename)
        try:
            s3.download_file(BUCKET, s3_key, local_path)
            logger.info(f"Downloaded s3://{BUCKET}/{s3_key} → {local_path}")
        except Exception as e:
            return {"status": "error", "error": f"Failed to download {s3_key}: {e}"}

    # 2. Execute code
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    exec_globals = {
        "__builtins__": __builtins__,
        "INPUT_DIR": input_dir,
        "OUTPUT_DIR": output_dir,
        "WORKSPACE": workspace,
    }

    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = stdout_capture
        sys.stderr = stderr_capture
        exec(code, exec_globals)
        exit_code = 0
    except Exception:
        stderr_capture.write(traceback.format_exc())
        exit_code = 1
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    stdout_str = stdout_capture.getvalue()
    stderr_str = stderr_capture.getvalue()
    logger.info(f"Code executed. exit_code={exit_code}, stdout={len(stdout_str)} chars")

    # 3. Push output files to S3
    uploaded = []
    if s3_output_prefix and exit_code == 0:
        for fname in os.listdir(output_dir):
            local_path = os.path.join(output_dir, fname)
            if os.path.isfile(local_path):
                s3_key = f"{s3_output_prefix.rstrip('/')}/{fname}"
                try:
                    s3.upload_file(local_path, BUCKET, s3_key)
                    uploaded.append({"s3_key": s3_key, "s3_uri": f"s3://{BUCKET}/{s3_key}"})
                    logger.info(f"Uploaded {local_path} → s3://{BUCKET}/{s3_key}")
                except Exception as e:
                    uploaded.append({"s3_key": s3_key, "error": str(e)})

    return {
        "status": "success" if exit_code == 0 else "error",
        "exit_code": exit_code,
        "stdout": stdout_str,
        "stderr": stderr_str,
        "uploaded_files": uploaded,
    }


def handle_write_files(payload: dict, workspace: str) -> dict:
    """Write files to workspace from direct content or S3."""
    written = []

    # From direct content
    for f in payload.get("files", []):
        path = os.path.join(workspace, f["path"].lstrip("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f["content"])
        written.append({"path": f["path"], "size": len(f["content"])})

    # From S3
    for s3_key in payload.get("s3_keys", []):
        filename = os.path.basename(s3_key)
        local_path = os.path.join(workspace, "input", filename)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        s3.download_file(BUCKET, s3_key, local_path)
        written.append({"path": f"input/{filename}", "source": s3_key})

    return {"status": "ok", "written": written}


def handle_read_files(payload: dict, workspace: str) -> dict:
    """Read files from workspace."""
    results = {}
    for path in payload.get("paths", []):
        full_path = os.path.join(workspace, path.lstrip("/"))
        if os.path.isfile(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                results[path] = {"content": f.read(), "size": os.path.getsize(full_path)}
        else:
            results[path] = {"error": "file not found"}
    return {"status": "ok", "files": results}


def handle_list_files(payload: dict, workspace: str) -> dict:
    """List files in workspace."""
    target = os.path.join(workspace, payload.get("path", "").lstrip("/"))
    if not os.path.isdir(target):
        return {"status": "ok", "files": []}

    files = []
    for root, dirs, filenames in os.walk(target):
        for fname in filenames:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, workspace)
            files.append({"path": rel, "size": os.path.getsize(full)})
    return {"status": "ok", "files": files}


# ---------- Entrypoint ----------

ACTION_MAP = {
    "execute": handle_execute,
    "write_files": handle_write_files,
    "read_files": handle_read_files,
    "list_files": handle_list_files,
}

@app.entrypoint
def invoke(payload: dict, context: RequestContext) -> dict:
    tenant_id = payload.get("tenant_id", "default")
    session_id = context.session_id or payload.get("session_id", "default")
    action = payload.get("action", "execute")

    logger.info(f"Tenant: {tenant_id} | Session: {session_id} | Action: {action}")

    workspace = ensure_workspace(tenant_id, session_id)
    handler = ACTION_MAP.get(action)
    if not handler:
        return {"status": "error", "error": f"Unknown action: {action}. Supported: {list(ACTION_MAP.keys())}"}

    try:
        return handler(payload, workspace)
    except Exception as e:
        logger.error(f"Action {action} failed: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    app.run(port=port)
