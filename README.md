# AgentCore + Lambda MicroVMs Data Analysis with SSE Streaming (V5)

A dual-runtime architecture where **Agent A is the sole brain** (on AWS Bedrock
AgentCore) and **Runtime B is a pure code executor** running on an **AWS Lambda
MicroVM**. Runtime A manages the MicroVM lifecycle per request and renders the
final report itself.

> Changes from V3: Runtime B moved from AgentCore Runtime to a Lambda MicroVM,
> and report rendering moved out of Runtime B into Runtime A. V3 remains on the
> `main` branch. See [DESIGN_V5.md](DESIGN_V5.md) and [FLOWCHART_V5.md](FLOWCHART_V5.md).

## Architecture

```
Runtime A (AgentCore; Agent A = sole brain, Opus 4.6)
  │  ① run_microvm → poll RUNNING (~2.4s) → create_microvm_auth_token
  │
  │  ② Strands Agent decides everything (Phase 1, stream_async):
  │    → runtime_b_shell("aws s3 cp ...")     ┐ HTTPS POST to MicroVM endpoint
  │    → runtime_b_python("import pandas ...") │ (X-aws-proxy-auth)
  │    → runtime_b_python("import matplotlib") ┘
  │
  │  ③ Phase 2: Runtime A renders report itself (converse_stream Opus → SSE)
  │             → upload analysis_report.md to S3
  │
  │  ④ finally: terminate_microvm
  ▼
Runtime B (Lambda MicroVM; pure executor, no decision-making)
  ├── :8080 POST action=shell   → subprocess.run()  → JSON response
  ├── :8080 POST action=python  → exec()            → JSON response
  └── :9000 lifecycle hooks: /ready /run /resume /suspend /terminate
       /run resets /tmp/workspace (persists across calls within one MicroVM)
```

**SSE end-to-end**: Phase 1 streams every tool call as it happens; Phase 2
streams the report token-by-token from Runtime A. No black box.

**Report moved to Runtime A**: Runtime B no longer has an LLM/report action —
it is a pure shell+python executor. Runtime A owns report rendering and upload.

See [DESIGN_V5.md](DESIGN_V5.md) for architecture, the 8-hour limit & roadmap
notes, and the storage comparison vs AgentCore.

## Prerequisites

- AWS account with Bedrock model access (Claude Opus 4.6)
- A Lambda MicroVMs region: `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`, `ap-northeast-1`
- **boto3 ≥ 1.43.36** (for the `lambda-microvms` / `lambda-core` clients)
- AWS credentials configured (`aws configure`)
- Docker (to build the MicroVM image artifact)

## Deploy

### 1. Create S3 bucket and sample data

```bash
export REGION=us-west-2
export DATA_BUCKET=agentcore-zoom-demo-$(aws sts get-caller-identity --query Account --output text)
aws s3 mb s3://$DATA_BUCKET --region $REGION
DATA_BUCKET=$DATA_BUCKET AWS_REGION=$REGION python3 generate_sample_data.py
```

> Note: the artifact bucket used to build the MicroVM image must be in the
> **same region** as the build (a cross-region artifact fails the build).

### 2. Build the Runtime B MicroVM image + IAM roles

```bash
REGION=$REGION DATA_BUCKET=$DATA_BUCKET ./deploy_v5.sh
```

`deploy_v5.sh` creates the build/execution IAM roles, packages `runtime_b_v5/`,
uploads it to S3, and calls `create-microvm-image`. Poll until `CREATED`:

```bash
aws lambda-microvms get-microvm-image --region $REGION \
  --image-identifier arn:aws:lambda:$REGION:<acct>:microvm-image:data_workstation_v5
```

### 3. Deploy Runtime A (agent brain) to AgentCore

```bash
cd runtime_a_v5
agentcore configure --create --name data_router_v5 --entrypoint main.py --region $REGION --non-interactive
# Enable ecr_auto_create and s3_auto_create in .bedrock_agentcore.yaml
agentcore deploy \
  --env DATA_BUCKET=$DATA_BUCKET \
  --env AWS_REGION=$REGION \
  --env MODEL_ID=us.anthropic.claude-opus-4-6-v1 \
  --env OPUS_MODEL_ID=us.anthropic.claude-opus-4-6-v1 \
  --env MICROVM_IMAGE_ARN=<image-arn-from-step-2> \
  --env MICROVM_EXEC_ROLE_ARN=<exec-role-arn-from-step-2>
```

Runtime A's AgentCore execution role needs:
- `lambda-microvms`: `RunMicrovm`, `GetMicrovm`, `CreateMicrovmAuthToken`, `TerminateMicrovm`
- `iam:PassRole` on the MicroVM execution role
- Bedrock: `InvokeModel`, `InvokeModelWithResponseStream`

### 4. Test

```python
import boto3, json
from botocore.config import Config

client = boto3.client('bedrock-agentcore', region_name='us-west-2',
                      config=Config(read_timeout=600))

resp = client.invoke_agent_runtime(
    agentRuntimeArn='<runtime-a-arn>',
    payload=json.dumps({
        'tenant_id': 'acme-corp',
        'message': '分析各区域Q1销售达成率，给出排名和改进建议'
    }).encode(),
    contentType='application/json',
    accept='text/event-stream',
    qualifier='DEFAULT',
)

for line in resp['response'].iter_lines():
    if line:
        line_str = line.decode('utf-8')
        if line_str.startswith('data: '):
            event = json.loads(line_str[6:])
            if event.get('type') == 'status':
                print(f"[{event['stage']}] {event['message']}")
            elif event.get('type') == 'chunk':
                print(event['content'], end='')
            elif event.get('type') == 'done':
                print(f"\n\nFiles: {[f['s3_uri'] for f in event['s3_keys']]}")
```

**Expected output (verified on AWS us-west-2 — 756 events, 184s):**
```
[provision] 正在启动 MicroVM 工作站...            ← run_microvm
[provision] MicroVM 就绪 (2.25s)                  ← cold start PENDING→RUNNING
[analysis]  Agent 开始分析...
[analysis]  正在执行: runtime_b_shell             ← Phase 1 tool call 实时推送
[analysis]  正在执行: runtime_b_python
[analysis]  正在执行: runtime_b_shell
[analysis]  数据准备完成
[report]    正在生成分析报告 (Opus streaming)...  ← Phase 2 报告在 Runtime A 渲染
# 2026 Q1 各区域销售达成率分析报告               ← 逐 token
## 概述
本季度全国综合达成率仅为 49.70%，所有区域均未完成既定目标...
...
                                                  ← MicroVM 在 finally 中 terminate
Files: ['s3://.../analysis_report.md', 's3://.../q1_region_achievement.csv', ...]
```

For the full local-then-real test workflow (Docker A+B, real MicroVM build,
cold-start measurement) and pitfalls, see [TESTING_V5.md](TESTING_V5.md).

## Multi-Tenancy

Runtime A (one AgentCore deployment) + one MicroVM image serve N tenants.

| Layer | Mechanism |
|-------|-----------|
| Compute | One MicroVM per request (Firecracker), terminated after — full isolation |
| Data | S3 `tenants/{tenant_id}/` prefix (prompt-enforced; for strong isolation, scope the MicroVM execution role per tenant) |
| Context | Module-level variable (single request per microVM) |

## File Structure

```
.
├── README.md                       # This file (V5)
├── DESIGN_V5.md                    # Architecture, 8h-limit & roadmap, storage compare
├── FLOWCHART_V5.md                 # End-to-end flow + MicroVM lifecycle
├── TESTING_V5.md                   # Bastion bypass test workflow + pitfalls
├── deploy_v5.sh                    # IAM roles + MicroVM image build + deploy steps
├── runtime_a_v5/
│   ├── main.py                     # Runtime A: Agent A (brain) + MicroVM lifecycle + report
│   ├── Dockerfile
│   └── requirements.txt
├── runtime_b_v5/
│   ├── main.py                     # Runtime B: MicroVM executor (shell, python) + hooks + CJK fonts
│   ├── Dockerfile                  # MicroVM base image (al2023-minimal)
│   └── requirements.txt
├── generate_sample_data.py         # Generate sample data for 2 tenants
└── (V3 reference: runtime_a_v3/, runtime_b_v3/, DESIGN_V3.md — on main branch)
```

## Version History

| Version | Branch | Description |
|---------|--------|-------------|
| V1 | `main` | Runtime A (Sonnet, generates code) + Runtime B (no LLM, exec only) |
| V2 | `v3-design` | Design doc only — single Runtime with sub-agents |
| V3 | `main` | Runtime A (Opus, sole brain) + Runtime B (shell + python + Opus report SSE) |
| V4 | `v4-java-research` | Research: Java + LangGraph feasibility |
| **V5** | **`v5-lambda-microvms`** | **Runtime B → Lambda MicroVM (shell/python only); report rendering moved into Runtime A** |
