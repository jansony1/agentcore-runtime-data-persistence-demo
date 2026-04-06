"""
Runtime B V3 — Pure Executor (No Decision-Making)

Three capabilities, all commanded by Agent A (Runtime A):
  1. shell:   Execute shell commands (aws cli, head, wc, sort, etc.)
  2. python:  Execute Python code (pandas, numpy, matplotlib)
  3. report:  Call Opus 4.6 converse_stream to render a report → SSE

Runtime B never decides what to do. It only executes what Agent A tells it.
Filesystem persists within the same session (same session_id = same microVM).
"""

import os
import io
import sys
import json
import logging
import subprocess
import traceback

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

# ---------- Config ----------
REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("DATA_BUCKET", "")
OPUS_MODEL_ID = os.environ.get("OPUS_MODEL_ID", "us.anthropic.claude-opus-4-6-v1")
WORKSPACE = os.environ.get("WORKSPACE_DIR", "/tmp/workspace")

if not BUCKET:
    raise RuntimeError("DATA_BUCKET environment variable is required")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()
s3 = boto3.client("s3", region_name=REGION)

os.makedirs(WORKSPACE, exist_ok=True)


# ---------- Action: Shell ----------

def handle_shell(payload: dict) -> dict:
    command = payload.get("command", "")
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=120, cwd=WORKSPACE,
        )
        return {
            "status": "success" if result.returncode == 0 else "error",
            "stdout": result.stdout[-5000:] if len(result.stdout) > 5000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "stdout": "", "stderr": "Command timed out (120s)", "exit_code": -1}
    except Exception as e:
        return {"status": "error", "stdout": "", "stderr": str(e), "exit_code": -1}


# ---------- Action: Python ----------

def handle_python(payload: dict) -> dict:
    code = payload.get("code", "")

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    exec_globals = {
        "__builtins__": __builtins__,
        "WORKSPACE": WORKSPACE,
    }

    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = stdout_buf
        sys.stderr = stderr_buf
        exec(code, exec_globals)
        exit_code = 0
    except Exception:
        stderr_buf.write(traceback.format_exc())
        exit_code = 1
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    stdout_str = stdout_buf.getvalue()
    stderr_str = stderr_buf.getvalue()

    # List output files if any were created
    output_files = []
    output_dir = os.path.join(WORKSPACE, "output")
    if os.path.isdir(output_dir):
        for f in os.listdir(output_dir):
            fp = os.path.join(output_dir, f)
            if os.path.isfile(fp):
                output_files.append({"name": f, "size": os.path.getsize(fp)})

    return {
        "status": "success" if exit_code == 0 else "error",
        "stdout": stdout_str[-8000:] if len(stdout_str) > 8000 else stdout_str,
        "stderr": stderr_str[-3000:] if len(stderr_str) > 3000 else stderr_str,
        "exit_code": exit_code,
        "output_files": output_files,
    }


# ---------- Action: Report (SSE streaming) ----------

def handle_report(payload: dict):
    """Generator: call Opus 4.6 converse_stream, yield SSE chunks."""
    context = payload.get("context", "")
    report_prompt = payload.get("prompt", "基于以下数据分析结果，撰写一份专业的 Markdown 格式分析报告。")
    save_to_s3_prefix = payload.get("s3_output_prefix", "")

    yield {"type": "status", "stage": "report", "message": "正在生成分析报告 (Opus 4.6 streaming)..."}

    full_prompt = f"{report_prompt}\n\n## 数据分析结果\n\n{context}"

    bedrock_client = boto3.client("bedrock-runtime", region_name=REGION)
    report_full = ""

    try:
        response = bedrock_client.converse_stream(
            modelId=OPUS_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": full_prompt}]}],
            system=[{"text": "你是专业数据分析报告撰写者。用 Markdown 格式输出。包含概述、关键发现、详细分析、改进建议。引用具体数字。"}],
        )
        for event in response["stream"]:
            if "contentBlockDelta" in event:
                text = event["contentBlockDelta"]["delta"].get("text", "")
                if text:
                    report_full += text
                    yield {"type": "chunk", "content": text}
    except Exception as e:
        logger.error(f"Report streaming failed: {e}", exc_info=True)
        yield {"type": "error", "stage": "report", "message": str(e)}

    # Save report to workspace and optionally to S3
    uploaded = []
    if report_full:
        os.makedirs(os.path.join(WORKSPACE, "output"), exist_ok=True)
        report_path = os.path.join(WORKSPACE, "output", "analysis_report.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_full)

        if save_to_s3_prefix:
            # Upload all output files to S3
            output_dir = os.path.join(WORKSPACE, "output")
            for fname in os.listdir(output_dir):
                local_path = os.path.join(output_dir, fname)
                if os.path.isfile(local_path):
                    s3_key = f"{save_to_s3_prefix.rstrip('/')}/{fname}"
                    try:
                        s3.upload_file(local_path, BUCKET, s3_key)
                        uploaded.append({"s3_key": s3_key, "s3_uri": f"s3://{BUCKET}/{s3_key}"})
                    except Exception as e:
                        uploaded.append({"s3_key": s3_key, "error": str(e)})

    yield {"type": "done", "s3_keys": uploaded}


# ---------- Entrypoint ----------

@app.entrypoint
def invoke(payload: dict, context: RequestContext):
    action = payload.get("action", "")
    logger.info(f"Action: {action} | Session: {context.session_id}")

    if action == "shell":
        return handle_shell(payload)
    elif action == "python":
        return handle_python(payload)
    elif action == "report":
        # Generator → SSE streaming
        return handle_report(payload)
    else:
        return {"status": "error", "error": f"Unknown action: {action}. Supported: shell, python, report"}


if __name__ == "__main__":
    import sys as _sys
    port = int(_sys.argv[1]) if len(_sys.argv) > 1 else 8080
    app.run(port=port)
