# AgentCore Dual-Runtime Data Analysis with SSE Streaming (V3)

A dual-runtime architecture on AWS Bedrock AgentCore where **Agent A is the sole brain** and **Runtime B is the pure executor** with shell, Python, and LLM report rendering capabilities.

## Architecture

```
Runtime A (Agent A = sole brain, Opus 4.6)
  │  Strands Agent decides everything:
  │    → runtime_b_shell("aws s3 cp ...")
  │    → runtime_b_python("import pandas ...")
  │    → runtime_b_python("import matplotlib ...")
  │
  │  invoke_agent_runtime (same session = same microVM = files persist)
  ▼
Runtime B (pure executor, no decision-making)
  ├── action=shell   → subprocess.run()     → JSON response
  ├── action=python  → exec()               → JSON response
  └── action=report  → converse_stream(Opus 4.6) → SSE streaming
                       ↑ renders report, does not think
```

**SSE end-to-end**: Runtime B streams report → Runtime A forwards → frontend sees tokens in real-time.

See [DESIGN_V3.md](DESIGN_V3.md) for detailed architecture, data flow, and design decisions.

## Prerequisites

- AWS account with Bedrock model access (Claude Opus 4.6)
- AWS credentials configured (`aws configure`)
- Python 3.11+
- `pip install -r requirements.txt`

## Deploy

### 1. Create S3 Bucket and Sample Data

```bash
export DATA_BUCKET=agentcore-demo-$(aws sts get-caller-identity --query Account --output text)
aws s3 mb s3://$DATA_BUCKET --region us-east-1
python3 generate_sample_data.py
```

### 2. Deploy Runtime B (executor)

```bash
cd runtime_b_v3
agentcore configure --create --name "data_workstation_v3" --entrypoint "main.py" --region "us-east-1" --non-interactive
# Enable ecr_auto_create and s3_auto_create in .bedrock_agentcore.yaml
agentcore deploy \
  --env "DATA_BUCKET=$DATA_BUCKET" \
  --env "AWS_REGION=us-east-1" \
  --env "OPUS_MODEL_ID=us.anthropic.claude-opus-4-6-v1"
# Note the ARN from output
```

Add IAM permissions to Runtime B's execution role:
- S3: `GetObject`, `PutObject`, `ListBucket`, `DeleteObject`
- Bedrock: `InvokeModel`, `InvokeModelWithResponseStream`

### 3. Deploy Runtime A (agent brain)

```bash
cd ../runtime_a_v3
agentcore configure --create --name "data_router_v3" --entrypoint "main.py" --region "us-east-1" --non-interactive
# Enable ecr_auto_create and s3_auto_create in .bedrock_agentcore.yaml
agentcore deploy \
  --env "DATA_BUCKET=$DATA_BUCKET" \
  --env "AWS_REGION=us-east-1" \
  --env "RUNTIME_B_ARN=<runtime-b-arn-from-step-2>" \
  --env "MODEL_ID=us.anthropic.claude-opus-4-6-v1"
```

Add IAM permissions to Runtime A's execution role:
- S3: `GetObject`, `ListBucket`
- Bedrock: `InvokeModel`, `InvokeModelWithResponseStream`
- AgentCore: `InvokeAgentRuntime`

### 4. Test

```python
import boto3, json
from botocore.config import Config

client = boto3.client('bedrock-agentcore', region_name='us-east-1',
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

**Expected output:**
```
[analysis] Agent 开始分析...
[analysis] 数据分析完成
[report] 正在生成分析报告...
[report] 正在生成分析报告 (Opus 4.6 streaming)...
# ACME Corp 2026 Q1 各区域销售达成率分析报告
## 一、概述
2026年第一季度，公司整体销售表现严重低于预期...
## 二、关键发现
| 排名 | 区域 | 达成率 | ...
...

Files: ['s3://.../analysis_report.md', 's3://.../q1_region_achievement.csv', ...]
```

## Multi-Tenancy

2 Runtime deployments serve N tenants. AgentCore assigns isolated microVMs per session.

| Layer | Mechanism |
|-------|-----------|
| Compute | AgentCore session → independent microVM (Firecracker) |
| Data | S3 `tenants/{tenant_id}/` prefix + code guard |
| Context | Module-level variable propagated to all tools |

## File Structure

```
.
├── README.md                       # This file
├── DESIGN_V3.md                    # Detailed architecture, data flow, test results
├── ARCHITECTURE.md                 # V1 architecture reference
├── runtime_a_v3/
│   ├── main.py                     # Runtime A: Agent A (brain) + SSE forwarding
│   ├── Dockerfile
│   └── requirements.txt
├── runtime_b_v3/
│   ├── main.py                     # Runtime B: executor (shell, python, report)
│   ├── Dockerfile
│   └── requirements.txt
├── generate_sample_data.py         # Generate sample data for 2 tenants
├── main.py                         # V1 Runtime A (reference)
├── runtime_b/                      # V1 Runtime B (reference)
└── requirements.txt
```

## Version History

| Version | Branch | Description |
|---------|--------|-------------|
| V1 | `main` | Runtime A (Sonnet, generates code) + Runtime B (no LLM, exec only) |
| V2 | `v3-design` | Design doc only — single Runtime with sub-agents |
| **V3** | **`v3-design`** | **Runtime A (Opus, sole brain) + Runtime B (shell + python + Opus report SSE)** |
| V4 | `v4-java-research` | Research: Java + LangGraph feasibility |
