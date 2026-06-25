"""
Runtime A V5 — Agent A (The Only Brain) + MicroVM lifecycle + Report rendering

Changes from V3:
  1. Runtime B moved from AgentCore Runtime to a Lambda MicroVM.
     Runtime A now manages the MicroVM lifecycle per request:
       run_microvm -> poll RUNNING -> create_microvm_auth_token
         -> tools call the MicroVM endpoint over HTTPS (X-aws-proxy-auth)
         -> finally terminate_microvm
  2. Report generation moved from Runtime B into Runtime A.
     Agent A finishes data prep (Phase 1), then Runtime A itself calls
     Bedrock converse_stream (Opus) to render the report and streams it
     to the frontend (Phase 2), then uploads report.md to S3.

Runtime A still runs on AgentCore Runtime (@app.entrypoint, SSE generator).
Agent A remains the sole decision-maker; Runtime B is a pure code executor
(shell / python only — no report action anymore).

Lifecycle model (chosen): one MicroVM per request, started at the top of the
entrypoint and terminated in finally. Simplest, no cross-request state, and
lets us measure cold-start latency directly. Long-running (>8h) work is NOT
handled here by design — see DESIGN_V5.md "8-hour limit & roadmap".

Local dev (bastion): set RUNTIME_B_URL to a locally running Runtime B v5
(http://localhost:8080). Lifecycle/auth-token are skipped in that path.
"""

import os
import json
import time
import logging
import uuid

import boto3
from botocore.config import Config
from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

# ---------- Config ----------
REGION = os.environ.get("AWS_REGION", "us-west-2")
BUCKET = os.environ.get("DATA_BUCKET", "")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-opus-4-6-v1")
OPUS_MODEL_ID = os.environ.get("OPUS_MODEL_ID", "us.anthropic.claude-opus-4-6-v1")

# MicroVM (deployed path)
MICROVM_IMAGE_ARN = os.environ.get("MICROVM_IMAGE_ARN", "")
MICROVM_EXEC_ROLE_ARN = os.environ.get("MICROVM_EXEC_ROLE_ARN", "")
MICROVM_MAX_DURATION = int(os.environ.get("MICROVM_MAX_DURATION", "1800"))  # 30 min
INGRESS_CONNECTOR = os.environ.get(
    "INGRESS_CONNECTOR",
    f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:ALL_INGRESS")
EGRESS_CONNECTOR = os.environ.get(
    "EGRESS_CONNECTOR",
    f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS")

# Local dev fallback
RUNTIME_B_URL = os.environ.get("RUNTIME_B_URL", "")

if not BUCKET:
    raise RuntimeError("DATA_BUCKET environment variable is required")
if not MICROVM_IMAGE_ARN and not RUNTIME_B_URL:
    raise RuntimeError("MICROVM_IMAGE_ARN or RUNTIME_B_URL is required")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("runtime_a_v5")

app = BedrockAgentCoreApp()
microvms = boto3.client("lambda-microvms", region_name=REGION,
                        config=Config(read_timeout=300))

# ---------- Per-request MicroVM connection (module-level: single request per microVM) ----------
_mvm = {"id": None, "endpoint": None, "token": None, "tenant": "default", "session": "default"}


def get_tenant_id() -> str:
    return _mvm["tenant"]


# ---------- MicroVM lifecycle ----------

def microvm_start() -> dict:
    """Run a MicroVM, wait until RUNNING, mint an auth token. Returns dict with id/endpoint/token."""
    resp = microvms.run_microvm(
        imageIdentifier=MICROVM_IMAGE_ARN,
        ingressNetworkConnectors=[INGRESS_CONNECTOR],
        egressNetworkConnectors=[EGRESS_CONNECTOR],
        idlePolicy={"autoResumeEnabled": True, "maxIdleDurationSeconds": 900,
                    "suspendedDurationSeconds": 300},
        maximumDurationInSeconds=MICROVM_MAX_DURATION,
        **({"executionRoleArn": MICROVM_EXEC_ROLE_ARN} if MICROVM_EXEC_ROLE_ARN else {}),
    )
    mvm_id = resp["microvmId"]
    endpoint = resp["endpoint"]

    # Poll until RUNNING
    t0 = time.time()
    while True:
        st = microvms.get_microvm(microvmIdentifier=mvm_id).get("state")
        if st == "RUNNING":
            break
        if st in ("TERMINATING", "TERMINATED"):
            raise RuntimeError(f"MicroVM {mvm_id} entered {st} before RUNNING")
        if time.time() - t0 > 120:
            raise TimeoutError(f"MicroVM {mvm_id} not RUNNING after 120s (state={st})")
        time.sleep(1)
    startup_s = round(time.time() - t0, 2)

    tok = microvms.create_microvm_auth_token(
        microvmIdentifier=mvm_id, expirationInMinutes=30,
        allowedPorts=[{"allPorts": {}}],
    )
    # Token shape: {"authToken": {"X-aws-proxy-auth": "..."}} (be defensive)
    auth = tok.get("authToken", tok)
    token = auth.get("X-aws-proxy-auth") if isinstance(auth, dict) else auth

    logger.info(f"MicroVM {mvm_id} RUNNING in {startup_s}s, endpoint={endpoint}")
    return {"id": mvm_id, "endpoint": endpoint, "token": token, "startup_s": startup_s}


def microvm_terminate(mvm_id: str):
    if not mvm_id:
        return
    try:
        microvms.terminate_microvm(microvmIdentifier=mvm_id)
        logger.info(f"MicroVM {mvm_id} terminated")
    except Exception as e:
        logger.error(f"terminate {mvm_id} failed: {e}")


def call_runtime_b(payload: dict) -> dict:
    """POST an action to Runtime B (MicroVM endpoint or local URL). Returns parsed JSON."""
    import urllib.request
    data = json.dumps(payload).encode("utf-8")

    if RUNTIME_B_URL:
        url = RUNTIME_B_URL
        headers = {"Content-Type": "application/json"}
    else:
        url = f"https://{_mvm['endpoint']}/"
        headers = {"Content-Type": "application/json", "X-aws-proxy-auth": _mvm["token"]}

    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=150) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------- Agent Tools (remote control Runtime B) ----------

@tool
def runtime_b_shell(command: str) -> str:
    """Execute a shell command on Runtime B's workspace.

    Use for: aws s3 cp, head, tail, wc, sort, ls, cat, file inspection.
    Working directory is /tmp/workspace (persists across calls in this session).

    Args:
        command: Shell command to execute

    Returns:
        JSON with stdout, stderr, exit_code
    """
    result = call_runtime_b({"action": "shell", "command": command})
    logger.info(f"Shell [{result.get('exit_code')}]: {command[:80]}")
    return json.dumps(result, ensure_ascii=False)


@tool
def runtime_b_python(code: str) -> str:
    """Execute Python code on Runtime B's workspace.

    The WORKSPACE variable is available as '/tmp/workspace'.
    Write output files to WORKSPACE + '/output/'. Files persist across calls.
    Use for: pandas analysis, numpy computation, matplotlib charts.

    Args:
        code: Python code to execute. Use WORKSPACE variable for file paths.

    Returns:
        JSON with stdout, stderr, exit_code, output_files
    """
    result = call_runtime_b({"action": "python", "code": code})
    logger.info(f"Python [{result.get('exit_code')}]: {len(code)} chars")
    return json.dumps(result, ensure_ascii=False)


# ---------- System Prompt ----------

def build_system_prompt(tenant_id: str, s3_prefix: str, s3_output_prefix: str) -> str:
    return f"""你是一个数据分析 Agent，你是唯一的决策者。

## 你的工具
1. **runtime_b_shell** — 在远程工作站(MicroVM)上执行 shell 命令
   - 工作目录: /tmp/workspace（持久化，跨调用共享）
   - 已装 aws cli，用于: aws s3 cp, head, wc, sort, ls, cat 等
2. **runtime_b_python** — 在远程工作站上执行 Python 代码
   - 可用变量: WORKSPACE = "/tmp/workspace"
   - 输出文件写到: WORKSPACE + "/output/"
   - 可用库: pandas, numpy, matplotlib

## 环境已就绪（不要重复安装/配置）
- pandas / numpy / matplotlib / aws cli **已预装**，直接 import 使用，**禁止 pip install**。
- matplotlib 中文字体**已配置好**，直接画图即可，**不要**自己处理字体、扫描字体或设置 rcParams。
- 多花时间安装/配置只会拖慢任务，且通常会失败。

## 工作流程
1. 用 runtime_b_shell 从 S3 下载数据到工作站:
   aws s3 cp s3://{BUCKET}/{s3_prefix} /tmp/workspace/ --recursive
2. 用 runtime_b_shell 预览数据结构 (head, wc 等)
3. 用 runtime_b_python 执行 pandas 数据处理和计算
4. 如果代码报错，阅读 stderr，修正代码，重试（最多 3 次）
5. 用 runtime_b_python 生成 matplotlib 图表，保存到 /tmp/workspace/output/
6. 把处理后的数据(CSV)和图表(PNG)保存到 /tmp/workspace/output/
7. 用 runtime_b_shell 把产出上传回 S3:
   aws s3 cp /tmp/workspace/output/ s3://{BUCKET}/{s3_output_prefix} --recursive
8. 用文字总结你的关键发现（这些发现将作为下游报告的依据）

## 重要规则
- 数据桶: s3://{BUCKET}/
- 你负责数据准备、计算、图表、上传产出
- 最终报告由下游 LLM 基于你产出的发现生成，你只需把发现讲清楚
"""


# ---------- Report (rendered in Runtime A now) ----------

def stream_report(analysis_result: str, s3_output_prefix: str):
    """Generator: Runtime A renders the report via Bedrock converse_stream, yields SSE chunks,
    then uploads report.md to S3."""
    bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    full_prompt = ("基于以下数据分析结果，撰写一份专业的 Markdown 格式分析报告。\n\n"
                   f"## 数据分析结果\n\n{analysis_result}")
    report_full = ""
    try:
        response = bedrock.converse_stream(
            modelId=OPUS_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": full_prompt}]}],
            system=[{"text": "你是专业数据分析报告撰写者。用 Markdown 格式输出。"
                            "包含概述、关键发现、详细分析、改进建议。引用具体数字。"}],
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
        return

    # Upload report.md to S3 (Runtime A owns this now)
    uploaded = []
    if report_full:
        s3 = boto3.client("s3", region_name=REGION)
        key = f"{s3_output_prefix.rstrip('/')}/analysis_report.md"
        try:
            s3.put_object(Bucket=BUCKET, Key=key,
                          Body=report_full.encode("utf-8"),
                          ContentType="text/markdown")
            uploaded.append({"s3_key": key, "s3_uri": f"s3://{BUCKET}/{key}"})
        except Exception as e:
            uploaded.append({"s3_key": key, "error": str(e)})
    yield {"type": "done", "s3_keys": uploaded}


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
    """Async generator -> SSE. Phase 1: Agent stream_async. Phase 2: report streaming."""
    tenant_id = resolve_tenant_id(payload, context)
    session_id = context.session_id or payload.get("session_id", str(uuid.uuid4()))
    message = payload.get("message", "")

    if not message:
        yield {"type": "error", "message": "请提供分析请求"}
        return

    _mvm["tenant"] = tenant_id
    _mvm["session"] = session_id
    s3_prefix = f"tenants/{tenant_id}/datasets/"
    s3_output_prefix = f"tenants/{tenant_id}/reports/"

    logger.info(f"Tenant: {tenant_id} | Session: {session_id} | Query: {message[:80]}")

    # === Start MicroVM (deployed path only) ===
    started = None
    try:
        if not RUNTIME_B_URL:
            yield {"type": "status", "stage": "provision", "message": "正在启动 MicroVM 工作站..."}
            started = microvm_start()
            _mvm["id"] = started["id"]
            _mvm["endpoint"] = started["endpoint"]
            _mvm["token"] = started["token"]
            yield {"type": "status", "stage": "provision",
                   "message": f"MicroVM 就绪 ({started['startup_s']}s)"}

        # === Phase 1: Agent stream_async (every step visible) ===
        yield {"type": "status", "stage": "analysis", "message": "Agent 开始分析..."}

        agent = Agent(
            model=BedrockModel(model_id=MODEL_ID, streaming=True),
            tools=[runtime_b_shell, runtime_b_python],
            system_prompt=build_system_prompt(tenant_id, s3_prefix, s3_output_prefix),
        )

        analysis_result = ""
        current_tool_name = None

        async for event in agent.stream_async(
            f"租户数据在 S3: s3://{BUCKET}/{s3_prefix}\n"
            f"请下载数据并完成以下分析任务: {message}\n"
            f"输出文件保存到 /tmp/workspace/output/ 并上传回 s3://{BUCKET}/{s3_output_prefix}"
        ):
            if not isinstance(event, dict):
                continue
            event_type = event.get("type", "")

            if event_type == "tool_use_stream":
                tool_info = event.get("current_tool_use", {})
                tool_name = tool_info.get("name", "")
                if tool_name and tool_name != current_tool_name:
                    current_tool_name = tool_name
                    yield {"type": "status", "stage": "analysis",
                           "message": f"正在执行: {tool_name}"}
            elif event_type == "tool_result":
                tr = event.get("tool_result", {})
                status = tr.get("status", "unknown")
                yield {"type": "status", "stage": "analysis",
                       "message": f"{current_tool_name} 完成 (status={status})"}
                current_tool_name = None
            elif "data" in event and event.get("data"):
                analysis_result += str(event["data"])
            elif "result" in event:
                result_obj = event.get("result")
                if result_obj and hasattr(result_obj, "message"):
                    msg = result_obj.message
                    if hasattr(msg, "content"):
                        for block in msg.content:
                            if hasattr(block, "text"):
                                analysis_result = block.text

        logger.info(f"Agent analysis complete: {len(analysis_result)} chars")
        yield {"type": "status", "stage": "analysis", "message": "数据准备完成"}

        # === Phase 2: Report streaming (rendered in Runtime A) ===
        yield {"type": "status", "stage": "report", "message": "正在生成分析报告 (Opus streaming)..."}
        for ev in stream_report(analysis_result, s3_output_prefix):
            yield ev

    except Exception as e:
        logger.error(f"Runtime A error: {e}", exc_info=True)
        yield {"type": "error", "message": str(e)}
    finally:
        # Always release the MicroVM
        if started:
            microvm_terminate(started["id"])
            _mvm["id"] = _mvm["endpoint"] = _mvm["token"] = None


if __name__ == "__main__":
    app.run(port=8080)
