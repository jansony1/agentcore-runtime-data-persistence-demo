"""
Runtime B V3 — Autonomous Analysis Workstation

A self-contained analysis environment with:
  1. Shell (CLI): basic data ops (S3 download, file inspection, ETL)
  2. Python Executor: pandas/numpy/matplotlib analysis
  3. Agent (Opus 4.6): orchestrates 1+2, generates streaming analysis report

Returns SSE events throughout the pipeline:
  {"type":"status", ...}  — progress updates
  {"type":"chunk", ...}   — streaming report content
  {"type":"done", ...}    — final results with S3 keys
"""

import os
import io
import sys
import json
import logging
import subprocess
import traceback
import uuid
from typing import List

import boto3
from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

# ---------- Config ----------
REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("DATA_BUCKET", "")
OPUS_MODEL_ID = os.environ.get("OPUS_MODEL_ID", "us.anthropic.claude-opus-4-6-v1")
WORKSPACE_ROOT = "/tmp/workspace"

if not BUCKET:
    raise RuntimeError("DATA_BUCKET environment variable is required")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()
s3 = boto3.client("s3", region_name=REGION)


# ---------- Workspace ----------

def setup_workspace(tenant_id: str) -> str:
    ws = os.path.join(WORKSPACE_ROOT, tenant_id, uuid.uuid4().hex[:8])
    for d in ["input", "output"]:
        os.makedirs(os.path.join(ws, d), exist_ok=True)
    return ws


# ---------- Tool: Shell ----------

@tool
def shell_exec(command: str) -> str:
    """Execute a shell command and return stdout/stderr.

    Use this for: aws s3 cp, head, tail, wc, csvtool, jq, sort, file inspection.
    Working directory is set to the current workspace.

    Args:
        command: Shell command to execute

    Returns:
        JSON with stdout, stderr, exit_code
    """
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=120, cwd=os.environ.get("CURRENT_WORKSPACE", "/tmp"),
        )
        output = {
            "stdout": result.stdout[-5000:] if len(result.stdout) > 5000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
            "exit_code": result.returncode,
        }
        logger.info(f"Shell: {command[:80]}... exit={result.returncode}")
        return json.dumps(output, ensure_ascii=False)
    except subprocess.TimeoutExpired:
        return json.dumps({"stdout": "", "stderr": "Command timed out (120s)", "exit_code": -1})
    except Exception as e:
        return json.dumps({"stdout": "", "stderr": str(e), "exit_code": -1})


# ---------- Tool: Python Executor ----------

@tool
def exec_python(code: str) -> str:
    """Execute Python code and return stdout/stderr.

    Pre-defined variables available:
    - WORKSPACE: root workspace directory
    - INPUT_DIR: directory with downloaded input files
    - OUTPUT_DIR: directory for output files (will be uploaded to S3)

    Args:
        code: Python code to execute

    Returns:
        JSON with stdout, stderr, exit_code
    """
    workspace = os.environ.get("CURRENT_WORKSPACE", "/tmp")
    input_dir = os.path.join(workspace, "input")
    output_dir = os.path.join(workspace, "output")

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    exec_globals = {
        "__builtins__": __builtins__,
        "WORKSPACE": workspace,
        "INPUT_DIR": input_dir,
        "OUTPUT_DIR": output_dir,
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
    logger.info(f"Python exec: exit={exit_code}, stdout={len(stdout_str)} chars")

    return json.dumps({
        "stdout": stdout_str[-8000:] if len(stdout_str) > 8000 else stdout_str,
        "stderr": stderr_str[-3000:] if len(stderr_str) > 3000 else stderr_str,
        "exit_code": exit_code,
    }, ensure_ascii=False)


# ---------- Tool: Read File ----------

@tool
def read_file(path: str) -> str:
    """Read a file from the workspace.

    Args:
        path: Relative path from workspace root, or absolute path

    Returns:
        File content (first 10000 chars for large files)
    """
    workspace = os.environ.get("CURRENT_WORKSPACE", "/tmp")
    if not os.path.isabs(path):
        path = os.path.join(workspace, path)

    if not os.path.isfile(path):
        return json.dumps({"error": f"File not found: {path}"})

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read(10000)

    return json.dumps({"path": path, "content": content, "truncated": os.path.getsize(path) > 10000})


# ---------- Tool: List Files ----------

@tool
def list_files(directory: str = "") -> str:
    """List files in a workspace directory.

    Args:
        directory: Relative path from workspace root. Empty = workspace root.

    Returns:
        JSON with file list
    """
    workspace = os.environ.get("CURRENT_WORKSPACE", "/tmp")
    target = os.path.join(workspace, directory) if directory else workspace

    if not os.path.isdir(target):
        return json.dumps({"files": [], "error": f"Not a directory: {target}"})

    files = []
    for root, dirs, filenames in os.walk(target):
        for fname in filenames:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, workspace)
            files.append({"path": rel, "size": os.path.getsize(full)})

    return json.dumps({"files": files, "count": len(files)})


# ---------- Pipeline ----------

ANALYSIS_PROMPT = """你是一个数据分析专家，运行在一个自治的分析工作站中。

## 你的环境
- 工作目录: {workspace}
- 输入文件目录: {workspace}/input/ (已下载好的数据文件)
- 输出目录: {workspace}/output/ (分析结果放这里)
- S3 桶: {bucket}

## 你的工具
1. **shell_exec**: 执行 shell 命令（文件检查、格式转换等）
2. **exec_python**: 执行 Python 代码（pandas 分析、matplotlib 图表等）
3. **read_file**: 读取文件内容
4. **list_files**: 列出目录中的文件

## 工作流程
1. 用 list_files 和 shell_exec("head ...") 了解数据结构
2. 用 exec_python 编写 pandas 代码进行分析
3. 如果代码出错，阅读 stderr，修复后重试（最多 3 次）
4. 将分析结果（CSV、图表）保存到 output/ 目录
5. 用 print() 输出所有关键发现和数字，这些会被用于生成最终报告

## 重要
- 始终 print() 关键分析结论
- 图表保存到 output/ 目录
- 汇总 CSV 也保存到 output/ 目录
"""

REPORT_PROMPT = """你是一个专业的数据分析报告撰写者。

基于以下分析数据和发现，撰写一份深度分析报告。

## 任务
{task}

## 数据分析结果
{analysis_output}

## 生成的文件
{generated_files}

## 报告要求
- 使用 Markdown 格式
- 包含: 概述、关键发现、详细分析、数据表格、改进建议
- 引用具体数字，不要含糊
- 如果有图表文件，在报告中引用它们的 S3 路径
- 语言专业但易读
"""


def save_outputs_to_s3(tenant_id: str, workspace: str) -> list:
    """Upload all output files to tenant-scoped S3."""
    output_dir = os.path.join(workspace, "output")
    uploaded = []

    if not os.path.isdir(output_dir):
        return uploaded

    for fname in os.listdir(output_dir):
        local_path = os.path.join(output_dir, fname)
        if not os.path.isfile(local_path):
            continue
        s3_key = f"tenants/{tenant_id}/reports/{fname}"
        try:
            s3.upload_file(local_path, BUCKET, s3_key)
            uploaded.append({"s3_key": s3_key, "s3_uri": f"s3://{BUCKET}/{s3_key}"})
            logger.info(f"Uploaded {fname} → s3://{BUCKET}/{s3_key}")
        except Exception as e:
            uploaded.append({"s3_key": s3_key, "error": str(e)})

    return uploaded


def run_analysis_pipeline(tenant_id: str, task: str, s3_data_prefix: str):
    """Three-stage analysis pipeline, yields SSE events."""

    workspace = setup_workspace(tenant_id)
    os.environ["CURRENT_WORKSPACE"] = workspace
    logger.info(f"Workspace: {workspace}")

    # ── Stage 1: Shell data preparation ──
    yield {"type": "status", "stage": "shell", "message": "正在从 S3 下载数据..."}

    # Download all files under the data prefix
    download_cmd = f'aws s3 cp s3://{BUCKET}/{s3_data_prefix} {workspace}/input/ --recursive --region {REGION}'
    result = subprocess.run(download_cmd, shell=True, capture_output=True, text=True, timeout=60)
    logger.info(f"S3 download: exit={result.returncode}")

    if result.returncode != 0:
        yield {"type": "error", "stage": "shell", "message": f"S3 下载失败: {result.stderr}"}
        return

    # List downloaded files
    downloaded = []
    input_dir = os.path.join(workspace, "input")
    for root, dirs, files in os.walk(input_dir):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, input_dir)
            downloaded.append(rel)

    yield {"type": "status", "stage": "shell", "message": f"已下载 {len(downloaded)} 个文件: {', '.join(downloaded)}"}

    # ── Stage 2: Agent-driven analysis ──
    yield {"type": "status", "stage": "python", "message": "正在执行数据分析 (Opus 4.6)..."}

    analysis_agent = Agent(
        model=BedrockModel(model_id=OPUS_MODEL_ID, streaming=False),
        tools=[shell_exec, exec_python, read_file, list_files],
        system_prompt=ANALYSIS_PROMPT.format(workspace=workspace, bucket=BUCKET),
    )

    analysis_result = str(analysis_agent(
        f"已下载到 input/ 的文件: {downloaded}\n\n任务: {task}\n\n"
        f"请分析数据并将结果保存到 output/ 目录。确保 print() 所有关键发现。"
    ))

    yield {"type": "status", "stage": "python", "message": "数据分析完成"}

    # Gather output file list
    output_dir = os.path.join(workspace, "output")
    generated_files = []
    if os.path.isdir(output_dir):
        for f in os.listdir(output_dir):
            generated_files.append(f)

    # ── Stage 3: Report generation (streaming) ──
    yield {"type": "status", "stage": "report", "message": "正在生成分析报告 (Opus 4.6 streaming)..."}

    report_model = BedrockModel(model_id=OPUS_MODEL_ID, streaming=True)

    report_prompt = REPORT_PROMPT.format(
        task=task,
        analysis_output=analysis_result[:15000],
        generated_files=json.dumps(generated_files, ensure_ascii=False),
    )

    # Stream report via Bedrock converse_stream
    report_full = ""
    try:
        bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
        logger.info(f"Report streaming with model: {OPUS_MODEL_ID}, region: {REGION}")
        response = bedrock_client.converse_stream(
            modelId=OPUS_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": report_prompt}]}],
            system=[{"text": "你是专业数据分析报告撰写者。用 Markdown 格式输出。"}],
        )

        for event in response["stream"]:
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                text = delta.get("text", "")
                if text:
                    report_full += text
                    yield {"type": "chunk", "content": text}

    except Exception as e:
        logger.error(f"Report streaming failed: {e}", exc_info=True)
        yield {"type": "error", "stage": "report", "message": str(e)}

    # Save report to output
    if report_full:
        report_path = os.path.join(output_dir, "analysis_report.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_full)

    # ── Stage 4: Persist to S3 ──
    s3_keys = save_outputs_to_s3(tenant_id, workspace)
    yield {"type": "done", "s3_keys": s3_keys}


# ---------- Entrypoint ----------

@app.entrypoint
def invoke(payload: dict, context: RequestContext):
    """Generator entrypoint → automatic SSE streaming response."""
    tenant_id = payload.get("tenant_id", "default")
    task = payload.get("task", payload.get("message", ""))
    s3_data_prefix = payload.get("s3_data_prefix", f"tenants/{tenant_id}/datasets/")

    logger.info(f"Tenant: {tenant_id} | Task: {task[:80]}...")

    return run_analysis_pipeline(tenant_id, task, s3_data_prefix)


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    app.run(port=port)
