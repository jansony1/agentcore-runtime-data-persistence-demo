"""
Sample Orchestrator Runtime — MySQL→analysis demo.

Same shape as runtime_a_v5 (Agent A = sole brain, manages the MicroVM lifecycle,
renders the report itself), but the task is: pull data from an RDS MySQL,
analyze with pandas, chart, and report. The model connects to MySQL from inside
the sandbox via pymysql (baked into the sample image).

Deployed path: set MICROVM_IMAGE_ARN + MICROVM_EXEC_ROLE_ARN.
Local path: set RUNTIME_B_URL to a locally-running sample sandbox.

MySQL connection is passed to the agent via env: MYSQL_HOST / MYSQL_PORT /
MYSQL_USER / MYSQL_PASSWORD / MYSQL_DB.
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

REGION = os.environ.get("AWS_REGION", "us-west-2")
BUCKET = os.environ.get("DATA_BUCKET", "")          # where the report is uploaded
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-opus-4-6-v1")
OPUS_MODEL_ID = os.environ.get("OPUS_MODEL_ID", "us.anthropic.claude-opus-4-6-v1")

MICROVM_IMAGE_ARN = os.environ.get("MICROVM_IMAGE_ARN", "")
MICROVM_EXEC_ROLE_ARN = os.environ.get("MICROVM_EXEC_ROLE_ARN", "")
MICROVM_MAX_DURATION = int(os.environ.get("MICROVM_MAX_DURATION", "1800"))
INGRESS_CONNECTOR = os.environ.get(
    "INGRESS_CONNECTOR", f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:ALL_INGRESS")
EGRESS_CONNECTOR = os.environ.get(
    "EGRESS_CONNECTOR", f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS")
RUNTIME_B_URL = os.environ.get("RUNTIME_B_URL", "")

# MySQL connection (handed to the model)
MYSQL = {
    "host": os.environ.get("MYSQL_HOST", ""),
    "port": os.environ.get("MYSQL_PORT", "3306"),
    "user": os.environ.get("MYSQL_USER", "admin"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "db": os.environ.get("MYSQL_DB", "salesdb"),
}

if not MICROVM_IMAGE_ARN and not RUNTIME_B_URL:
    raise RuntimeError("MICROVM_IMAGE_ARN or RUNTIME_B_URL is required")
if MICROVM_IMAGE_ARN and not MICROVM_EXEC_ROLE_ARN:
    raise RuntimeError("MICROVM_EXEC_ROLE_ARN is required when MICROVM_IMAGE_ARN is set")
if not MYSQL["host"]:
    raise RuntimeError("MYSQL_HOST is required")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("sample_orchestrator")

app = BedrockAgentCoreApp()
microvms = boto3.client("lambda-microvms", region_name=REGION, config=Config(read_timeout=300))
_mvm = {"id": None, "endpoint": None, "token": None}


def microvm_start() -> dict:
    resp = microvms.run_microvm(
        imageIdentifier=MICROVM_IMAGE_ARN,
        ingressNetworkConnectors=[INGRESS_CONNECTOR],
        egressNetworkConnectors=[EGRESS_CONNECTOR],
        idlePolicy={"autoResumeEnabled": True, "maxIdleDurationSeconds": 900, "suspendedDurationSeconds": 300},
        maximumDurationInSeconds=MICROVM_MAX_DURATION,
        executionRoleArn=MICROVM_EXEC_ROLE_ARN,
    )
    mid, ep = resp["microvmId"], resp["endpoint"]
    t0 = time.time()
    while True:
        st = microvms.get_microvm(microvmIdentifier=mid).get("state")
        if st == "RUNNING":
            break
        if st in ("TERMINATING", "TERMINATED"):
            raise RuntimeError(f"MicroVM {mid} entered {st} before RUNNING")
        if time.time() - t0 > 120:
            raise TimeoutError(f"MicroVM {mid} not RUNNING after 120s ({st})")
        time.sleep(0.5)
    tok = microvms.create_microvm_auth_token(microvmIdentifier=mid, expirationInMinutes=30,
                                             allowedPorts=[{"allPorts": {}}])
    auth = tok.get("authToken", tok)
    token = auth.get("X-aws-proxy-auth") if isinstance(auth, dict) else auth
    logger.info(f"MicroVM {mid} RUNNING in {round(time.time()-t0,2)}s")
    return {"id": mid, "endpoint": ep, "token": token, "startup_s": round(time.time() - t0, 2)}


def microvm_terminate(mid: str):
    if not mid:
        return
    try:
        microvms.terminate_microvm(microvmIdentifier=mid)
        logger.info(f"MicroVM {mid} terminated")
    except Exception as e:
        logger.error(f"terminate failed: {e}")


def call_runtime_b(payload: dict) -> dict:
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    if RUNTIME_B_URL:
        url, headers = RUNTIME_B_URL, {"Content-Type": "application/json"}
    else:
        url = f"https://{_mvm['endpoint']}/"
        headers = {"Content-Type": "application/json", "X-aws-proxy-auth": _mvm["token"]}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=150) as r:
        return json.loads(r.read().decode("utf-8"))


@tool
def runtime_b_shell(command: str) -> str:
    """Run a shell command in the sandbox (/tmp/workspace, persists across calls)."""
    result = call_runtime_b({"action": "shell", "command": command})
    logger.info(f"Shell [{result.get('exit_code')}]: {command[:80]}")
    return json.dumps(result, ensure_ascii=False)


@tool
def runtime_b_python(code: str) -> str:
    """Run Python in the sandbox. WORKSPACE='/tmp/workspace'. pymysql/pandas/numpy/matplotlib available."""
    result = call_runtime_b({"action": "python", "code": code})
    logger.info(f"Python [{result.get('exit_code')}]: {len(code)} chars")
    return json.dumps(result, ensure_ascii=False)


def build_system_prompt() -> str:
    return f"""你是一个数据分析 Agent，你是唯一的决策者。

## 你的工具
1. **runtime_b_shell** — 在远程沙箱执行 shell
2. **runtime_b_python** — 在远程沙箱执行 Python（已装 pymysql, pandas, numpy, matplotlib）
   - WORKSPACE = "/tmp/workspace"，输出写到 WORKSPACE + "/output/"

## 数据源：MySQL (RDS)
- host: {MYSQL['host']}
- port: {MYSQL['port']}
- user: {MYSQL['user']}
- password: {MYSQL['password']}
- database: {MYSQL['db']}
- 表: `sales`(id, txn_date, region, product, sales_rep, amount)、`region_targets`(region, q1_target)

## 环境已就绪（不要重复安装/配置）
- pymysql / pandas / numpy / matplotlib 已预装，直接 import，**禁止 pip install**。
- matplotlib 中文字体已配置好，直接画图。

## 工作流程
1. 用 runtime_b_python + pymysql 连接 MySQL，把 sales / region_targets 读进 pandas DataFrame
2. 计算各区域 Q1 销售额、达成率（销售额 / q1_target），排名
3. 用 matplotlib 画图，保存到 WORKSPACE + "/output/"
4. 处理后的数据(CSV)也保存到 output/
5. 如代码报错，读 stderr，修正重试（最多 3 次）
6. 用文字总结关键发现（作为下游报告依据）
"""


def stream_report(analysis_result: str, s3_output_prefix: str):
    bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    prompt = ("基于以下数据分析结果，撰写一份专业的 Markdown 格式分析报告。\n\n"
              f"## 数据分析结果\n\n{analysis_result}")
    full = ""
    try:
        resp = bedrock.converse_stream(
            modelId=OPUS_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": "你是专业数据分析报告撰写者。Markdown 输出，含概述、关键发现、详细分析、改进建议，引用具体数字。"}],
        )
        for ev in resp["stream"]:
            if "contentBlockDelta" in ev:
                t = ev["contentBlockDelta"]["delta"].get("text", "")
                if t:
                    full += t
                    yield {"type": "chunk", "content": t}
    except Exception as e:
        logger.error(f"report failed: {e}", exc_info=True)
        yield {"type": "error", "stage": "report", "message": str(e)}
        return

    uploaded = []
    if full and BUCKET:
        s3 = boto3.client("s3", region_name=REGION)
        key = f"{s3_output_prefix.rstrip('/')}/analysis_report.md"
        try:
            s3.put_object(Bucket=BUCKET, Key=key, Body=full.encode("utf-8"), ContentType="text/markdown")
            uploaded.append({"s3_key": key, "s3_uri": f"s3://{BUCKET}/{key}"})
        except Exception as e:
            uploaded.append({"s3_key": key, "error": str(e)})
    yield {"type": "done", "s3_keys": uploaded}


@app.entrypoint
async def invoke(payload: dict, context: RequestContext):
    tenant_id = payload.get("tenant_id", "demo")
    message = payload.get("message", "分析各区域Q1销售达成率，给出排名和改进建议")
    s3_output_prefix = f"tenants/{tenant_id}/reports/"
    logger.info(f"Tenant: {tenant_id} | Query: {message[:80]}")

    started = None
    try:
        if not RUNTIME_B_URL:
            yield {"type": "status", "stage": "provision", "message": "正在启动 MicroVM 沙箱..."}
            started = microvm_start()
            _mvm.update(id=started["id"], endpoint=started["endpoint"], token=started["token"])
            yield {"type": "status", "stage": "provision", "message": f"沙箱就绪 ({started['startup_s']}s)"}

        yield {"type": "status", "stage": "analysis", "message": "Agent 开始分析..."}
        agent = Agent(model=BedrockModel(model_id=MODEL_ID, streaming=True),
                      tools=[runtime_b_shell, runtime_b_python],
                      system_prompt=build_system_prompt())

        analysis_result = ""
        current_tool = None
        async for event in agent.stream_async(f"请完成以下分析任务: {message}"):
            if not isinstance(event, dict):
                continue
            if event.get("type") == "tool_use_stream":
                name = event.get("current_tool_use", {}).get("name", "")
                if name and name != current_tool:
                    current_tool = name
                    yield {"type": "status", "stage": "analysis", "message": f"正在执行: {name}"}
            elif "data" in event and event.get("data"):
                analysis_result += str(event["data"])
            elif "result" in event:
                ro = event.get("result")
                if ro and hasattr(ro, "message") and hasattr(ro.message, "content"):
                    for b in ro.message.content:
                        if hasattr(b, "text"):
                            analysis_result = b.text

        logger.info(f"analysis complete: {len(analysis_result)} chars")
        yield {"type": "status", "stage": "analysis", "message": "数据准备完成"}

        yield {"type": "status", "stage": "report", "message": "正在生成分析报告 (Opus streaming)..."}
        for ev in stream_report(analysis_result, s3_output_prefix):
            yield ev
    except Exception as e:
        logger.error(f"orchestrator error: {e}", exc_info=True)
        yield {"type": "error", "message": str(e)}
    finally:
        if started:
            microvm_terminate(started["id"])
            _mvm.update(id=None, endpoint=None, token=None)


if __name__ == "__main__":
    app.run(port=8080)
