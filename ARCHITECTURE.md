# AgentCore Data Analysis Demo — Architecture & Test Report (V1)

## Overview

A dual-runtime AgentCore architecture that provides tenant-isolated data analysis:
- **Runtime A** (Framework Worker): Strands Agent that understands user requests, generates Python analysis code
- **Runtime B** (Code Executor): Stateless executor that runs Python code in isolation, no LLM calls

S3 serves as the shared filesystem (analogous to E2B's sandbox filesystem), with tenant-scoped prefixes for data isolation.

---

## Architecture

```
                         ┌─────────────────────────────────────────┐
                         │              User Request               │
                         │  { tenant_id, session_id, message }     │
                         └──────────────────┬──────────────────────┘
                                            │
                    ┌───────────────────────────────────────────────────┐
                    │         AgentCore Runtime A                       │
                    │         Framework Worker — Strands Agent          │
                    │                                                   │
                    │  ┌─────────────┐                                  │
                    │  │ Entrypoint  │                                  │
                    │  │ resolve     │─── tenant_id from:               │
                    │  │ tenant_id   │    1. Custom Header              │
                    │  └──────┬──────┘    2. Payload field              │
                    │         │           3. Fallback "default"         │
                    │         ▼                                         │
                    │  ┌─────────────┐                                  │
                    │  │ set_tenant()│──────────────────────────┐       │
                    │  └──────┬──────┘                          │       │
                    │         │                                 ▼       │
                    │         ▼                          ┌────────────┐ │
                    │  ┌──────────────┐                  │ All tools  │ │
                    │  │ Strands Agent│──── tools ──────→│ auto-scope │ │
                    │  │ (Claude LLM) │                  │ to tenant  │ │
                    │  └──────────────┘                  └────────────┘ │
                    │         │                                         │
                    │    Tool calls:                                    │
                    │    ① list_s3_data ─────────── S3 ListObjects     │
                    │    ② fetch_s3_data ────────── S3 GetObject       │
                    │    ③ execute_on_runtime_b ──┐                    │
                    │                             │                    │
                    └─────────────────────────────┼────────────────────┘
                                                  │
                          invoke_agent_runtime     │
                                                  │
                    ┌─────────────────────────────┼────────────────────┐
                    │         AgentCore Runtime B                       │
                    │         Code Executor — No LLM                   │
                    │                             │                    │
                    │                             ▼                    │
                    │  ┌──────────────────────────────────────────┐    │
                    │  │  Workspace: /tmp/workspace/{tenant}/{session} │
                    │  │                                          │    │
                    │  │  1. Download s3_inputs → input/          │    │
                    │  │  2. exec(code)                           │    │
                    │  │  3. Upload output/ → S3                  │    │
                    │  └──────────────────────────────────────────┘    │
                    │                                                   │
                    └───────────────────────────────────────────────────┘
                                            │
                                            ▼
                    ┌───────────────────────────────────────────────────┐
                    │                    S3 Bucket                      │
                    │                                                   │
                    │   tenants/                                        │
                    │   ├── acme-corp/                                  │
                    │   │   ├── datasets/sales/2026-H1/                │
                    │   │   │   ├── transactions.csv                   │
                    │   │   │   └── region_targets.csv                 │
                    │   │   └── reports/sales/2026-Q1-achievement/     │
                    │   │       ├── q1_sales_achievement_analysis.csv  │
                    │   │       └── q1_sales_achievement_charts.png    │
                    │   └── globex-inc/                                 │
                    │       ├── datasets/sales/2026-H1/                │
                    │       │   ├── transactions.csv                   │
                    │       │   └── region_targets.csv                 │
                    │       └── reports/  (empty — not yet analyzed)    │
                    └───────────────────────────────────────────────────┘
```

---

## Data Flow

### Request Lifecycle

```
User: "分析各区域Q1销售达成率"  (tenant_id=acme-corp, session_id=acme-002)
│
▼
Runtime A entrypoint:
│  resolve_tenant_id() → "acme-corp"
│  set_tenant("acme-corp", "acme-002")
│  build_system_prompt("acme-corp") → tenant-aware prompt
│  create Strands Agent with tools
│
├── Agent calls: list_s3_data("datasets/")
│   │  tenant_prefix() → "tenants/acme-corp/datasets/"
│   │  S3 ListObjectsV2(Prefix="tenants/acme-corp/datasets/")
│   └── returns: [transactions.csv, region_targets.csv] with full keys
│
├── Agent calls: fetch_s3_data(["tenants/acme-corp/datasets/.../transactions.csv"])
│   │  guard: key.startswith("tenants/acme-corp/") ✓
│   │  S3 GetObject → preview first 50 lines
│   └── returns: CSV header + sample rows
│
├── Agent (LLM): generates pandas analysis code
│
├── Agent calls: execute_on_runtime_b(
│   │   code="import pandas as pd...",
│   │   s3_inputs=["tenants/acme-corp/datasets/.../transactions.csv",
│   │              "tenants/acme-corp/datasets/.../region_targets.csv"],
│   │   s3_output_prefix="reports/sales/2026-Q1-achievement/"
│   │ )
│   │  guard: all s3_inputs start with "tenants/acme-corp/" ✓
│   │  full_output_prefix → "tenants/acme-corp/reports/sales/2026-Q1-achievement/"
│   │
│   └── invoke_agent_runtime(Runtime B):
│       │  payload: { action, code, s3_inputs, s3_output_prefix, tenant_id, session_id }
│       ▼
│       Runtime B:
│       │  workspace = /tmp/workspace/acme-corp/acme-002/
│       │  S3 download → input/transactions.csv, input/region_targets.csv
│       │  exec(code) with INPUT_DIR, OUTPUT_DIR
│       │  S3 upload output/* → tenants/acme-corp/reports/sales/2026-Q1-achievement/
│       └── return: { stdout, stderr, exit_code, uploaded_files }
│
▼
Runtime A: return { output: "分析完成...", tenant_id, session_id }
```

### Data Isolation Enforcement (Three Layers)

```
Layer 1 — System Prompt (soft)
│  LLM is told: "当前租户: acme-corp, 你只能访问属于当前租户的数据"
│  Effect: LLM self-restricts (won't attempt cross-tenant access)
│
Layer 2 — Code Guard (hard)
│  fetch_s3_data:        key.startswith(f"tenants/{tenant_id}/") check
│  execute_on_runtime_b: same check on all s3_inputs
│  Effect: even if LLM tries, blocked at tool level with "Access denied"
│
Layer 3 — Path Prefix (structural)
│  list_s3_data:     auto-prepends tenants/{tenant_id}/ to prefix
│  s3_output_prefix: auto-prepends tenants/{tenant_id}/ before sending to Runtime B
│  Effect: LLM never even sees other tenants' paths
```

### Tenant Identity Resolution

```
Priority:
1. AgentCore custom header: X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id
2. Payload field: tenant_id
3. Fallback: "default"

Propagation:
  Request → resolve_tenant_id() → all @tool functions
  Runtime A → Runtime B: tenant_id passed explicitly in invoke payload
```

---

## Comparison: AgentCore vs E2B

| Aspect | E2B | AgentCore (this demo) |
|--------|-----|----------------------|
| Sandbox isolation | Firecracker microVM | Firecracker microVM |
| Data in | `sandbox.files.write()` (dedicated API) | S3 as shared filesystem |
| Code execution | `sandbox.run_code()` (dedicated API) | `invoke_agent_runtime` (single endpoint) |
| Data out | `sandbox.files.read()` (dedicated API) | S3 upload from Runtime B |
| File listing | `sandbox.files.list()` (dedicated API) | S3 ListObjects |
| Multi-step interaction | Multiple SDK calls, sandbox stays alive | Multiple invokes with same session_id |
| Data persistence | Ephemeral (sandbox lifetime) | S3 (you control retention) |
| API surface | 6-7 specialized APIs | 1 generic `/invocations` endpoint |
| Tenant isolation | Per-sandbox (one sandbox per user) | S3 prefix + code guards |

Key difference: E2B provides rich, file-system-like APIs through its SDK. AgentCore provides a single invoke endpoint — we use **S3 as the shared filesystem** to bridge this gap.

---

## Deployed Test Results

### Deployed Runtimes

- **Runtime A**: `arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/data_analysis_agent-LJIlqsCf7q`
- **Runtime B**: `arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/data_executor-mrBCURA7BI`
- **Invocation**: `boto3.client('bedrock-agentcore').invoke_agent_runtime()`
- **Tenants**: acme-corp (Chinese cloud products, 500 txns), globex-inc (English SaaS products, 300 txns)

### Test 1: Tenant Data Isolation

**acme-corp** sees only its own files:
```
invoke_agent_runtime(Runtime A, { tenant_id: "acme-corp", message: "列出我有哪些数据文件" })

Result:
  tenants/acme-corp/datasets/sales/2026-H1/region_targets.csv  (148 bytes)
  tenants/acme-corp/datasets/sales/2026-H1/transactions.csv    (34,163 bytes)
```

**globex-inc** sees only its own files:
```
Result:
  tenants/globex-inc/datasets/sales/2026-H1/region_targets.csv  (144 bytes)
  tenants/globex-inc/datasets/sales/2026-H1/transactions.csv    (21,287 bytes)
```

### Test 2: Cross-Tenant Access Block

Code guard verified:
```python
# tenant = globex-inc, accessing acme-corp key:
key.startswith("tenants/globex-inc/")  →  False
>>> BLOCKED: Access denied

# tenant = globex-inc, accessing own key:
key.startswith("tenants/globex-inc/")  →  True
>>> ALLOWED
```

### Test 3: Runtime B Direct Invocation

```python
invoke_agent_runtime(
    agentRuntimeArn='...runtime/data_executor-mrBCURA7BI',
    payload={ action: "execute", code: "import pandas as pd...",
              s3_inputs: ["tenants/acme-corp/datasets/..."], ... }
)

Result:
  status: "success"
  exit_code: 0
  stdout: "Rows: 500\nColumns: ['transaction_id', 'date', ...]\nDone"
  uploaded_files: [{ s3_key: "tenants/acme-corp/reports/cloud-test/sample.csv" }]
```

### Test 4: Full E2E — Runtime A → Runtime B

```python
invoke_agent_runtime(
    agentRuntimeArn='...runtime/data_analysis_agent-LJIlqsCf7q',
    payload={ tenant_id: "acme-corp", message: "分析各区域Q1销售达成率，给出排名" }
)
```

**Agent workflow on deployed runtimes**:
1. Runtime A: `list_s3_data` → found 2 files under `tenants/acme-corp/`
2. Runtime A: `fetch_s3_data` → previewed CSV structure
3. Runtime A: LLM generated pandas code
4. Runtime A: `execute_on_runtime_b` → **invoked Runtime B via `invoke_agent_runtime` API**
5. Runtime B: pulled data from S3, exec'd code, uploaded results to S3
6. Runtime A: returned analysis to user

**Analysis output**:
```
排名  区域   Q1目标     Q1实际     达成率
1    华中   ¥300万    ¥193.3万   64.4%
2    西南   ¥200万    ¥112.0万   56.0%
3    华南   ¥400万    ¥213.6万   53.4%
4    华北   ¥450万    ¥215.6万   47.9%
5    华东   ¥500万    ¥185.1万   37.0%

Overall: 49.7% (target: ¥1,850万, actual: ¥919.5万)
```

**Results persisted to tenant-scoped S3**:
```
tenants/acme-corp/reports/sales/2026-Q1/
├── q1_regional_achievement_ranking.csv   (214 bytes)
└── q1_product_sales_by_region.csv        (989 bytes)
```

### Deployment Notes

- Base image: `public.ecr.aws/docker/library/python:3.11-slim` (avoid Docker Hub rate limits)
- Runtime B must listen on port 8080 (AgentCore default)
- IAM roles need explicit S3, Bedrock, and `bedrock-agentcore:InvokeAgentRuntime` permissions
- Cold start: ~10-15s for first invocation; subsequent invocations within idle timeout are fast

---

## Deployment

```bash
# 1. Create S3 bucket and upload sample data
export DATA_BUCKET=your-bucket-name
python3 generate_sample_data.py

# 2. Deploy Runtime B
cd runtime_b
agentcore configure --create --name "data_executor" --entrypoint "main.py" --region "us-east-1" --non-interactive
agentcore deploy --env "DATA_BUCKET=$DATA_BUCKET" --env "AWS_REGION=us-east-1"
# Note the ARN from output

# 3. Add IAM permissions to Runtime B role
# S3: GetObject, PutObject, ListBucket | Bedrock: InvokeModel

# 4. Deploy Runtime A with Runtime B ARN
cd ../
agentcore configure --create --name "data_analysis_agent" --entrypoint "main.py" --region "us-east-1" --non-interactive
agentcore deploy --env "DATA_BUCKET=$DATA_BUCKET" --env "AWS_REGION=us-east-1" --env "RUNTIME_B_ARN=<runtime-b-arn>"

# 5. Add IAM permissions to Runtime A role
# S3: GetObject, ListBucket | Bedrock: InvokeModel | AgentCore: InvokeAgentRuntime

# 6. Test
python3 -c "
import boto3, json
from botocore.config import Config
client = boto3.client('bedrock-agentcore', region_name='us-east-1', config=Config(read_timeout=300))
resp = client.invoke_agent_runtime(
    agentRuntimeArn='RUNTIME_A_ARN',
    payload=json.dumps({'tenant_id':'acme-corp','message':'分析Q1销售达成率'}).encode(),
    contentType='application/json', qualifier='DEFAULT')
print(json.loads(resp['response'].read().decode()))
"
```

---

## Future Improvements
- **SDK Wrapper**: Wrap `invoke_agent_runtime` into E2B-style `sandbox.execute()` / `sandbox.files.write()` API
- **DynamoDB metadata**: Task records with tenant → S3 key mapping for historical queries
- **S3 Bucket Policy**: IAM-level tenant isolation (in addition to code-level guards)
- **S3 Lifecycle Policy**: Auto-archive reports after 90 days to Glacier
- See **DESIGN_V3.md** for the evolved architecture with SSE streaming and report generation
