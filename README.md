# AgentCore Runtime Data Persistence & Tenant Isolation Demo

Demonstrates how to build a **dual-runtime** data analysis system on AWS Bedrock AgentCore with:

- **Data persistence via S3** (not relying on AgentCore's 14-day session storage)
- **Tenant isolation** (S3 prefix-based data separation + code-level guards)
- **Runtime-to-Runtime invocation** (main agent delegates code execution to a separate runtime)

## Architecture

```
User → Runtime A (Strands Agent, generates analysis code)
           │
           ├── S3: fetch tenant-scoped data
           ├── LLM: generate Python analysis code
           ├── Runtime B: execute code (via invoke_agent_runtime or HTTP)
           └── S3: persist results to tenant-scoped path

       Runtime B (stateless code executor, no LLM)
           └── pull S3 data → exec(code) → push results to S3
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed diagrams, data flow, and test results.

## Prerequisites

- Python 3.11+
- AWS credentials configured (`aws configure`)
- AWS account with Bedrock model access (Claude Sonnet or other supported model)
- `pip install -r requirements.txt`

## Quick Start

### 1. Create S3 Bucket

```bash
# Replace with your account ID
export DATA_BUCKET=agentcore-demo-$(aws sts get-caller-identity --query Account --output text)
aws s3 mb s3://$DATA_BUCKET --region us-east-1
```

### 2. Generate Sample Data

Creates sales data for two tenants (`acme-corp` and `globex-inc`):

```bash
export DATA_BUCKET=your-bucket-name
python3 generate_sample_data.py
```

S3 layout after:
```
tenants/acme-corp/datasets/sales/2026-H1/transactions.csv      (500 rows, Chinese cloud products)
tenants/acme-corp/datasets/sales/2026-H1/region_targets.csv
tenants/globex-inc/datasets/sales/2026-H1/transactions.csv     (300 rows, English SaaS products)
tenants/globex-inc/datasets/sales/2026-H1/region_targets.csv
```

### 3. Run Locally

```bash
export DATA_BUCKET=your-bucket-name
export AWS_REGION=us-east-1

# Terminal 1: Start Runtime B (code executor)
python3 runtime_b/main.py

# Terminal 2: Start Runtime A (main agent)
python3 main.py
```

### 4. Test

```bash
# Tenant: acme-corp — list available data
curl -s -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"acme-corp","message":"列出我有哪些数据文件"}' | python3 -m json.tool

# Tenant: acme-corp — run analysis
curl -s -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"acme-corp","session_id":"demo-001","message":"分析各区域Q1销售达成率"}' | python3 -m json.tool

# Tenant: globex-inc — different tenant, isolated data
curl -s -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"globex-inc","message":"analyze Q1 regional sales achievement"}' | python3 -m json.tool

# Verify results in S3
aws s3 ls s3://$DATA_BUCKET/tenants/ --recursive
```

## Tenant Isolation

Three layers of enforcement:

| Layer | Location | Mechanism |
|-------|----------|-----------|
| System Prompt | Runtime A | LLM is told it can only access current tenant's data |
| Code Guard | `fetch_s3_data`, `execute_on_runtime_b` | `key.startswith(f"tenants/{tenant_id}/")` check |
| Path Prefix | `tenant_prefix()` | `list_s3_data` and `s3_output_prefix` auto-prepend tenant path |

Tenant identity comes from (in priority order):
1. AgentCore custom header: `X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id`
2. Payload field: `tenant_id`
3. Fallback: `"default"`

## Why Not Session Storage?

AgentCore Runtime's built-in session storage (14-day TTL) is designed for ephemeral compute state, not business data:

| | Session Storage | S3 (this demo) |
|---|---|---|
| TTL | 14 days, then deleted | You control retention |
| Version update | **Reset to clean state** | Unaffected |
| Backup/export | Not supported | Standard S3 tools |
| Cross-session access | No | Yes (via S3 keys) |
| Tenant isolation | Per-session only | S3 prefix + code guards |

**Rule of thumb**: Session Storage = cache (ok to lose). S3 = your data (you own the lifecycle).

## Deploy to AgentCore

```bash
# Deploy Runtime B first
cd runtime_b
agentcore deploy
# Note the ARN from output

# Deploy Runtime A with Runtime B ARN
cd ..
RUNTIME_B_ARN=arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:runtime/RUNTIME_B_ID agentcore deploy
```

## File Structure

```
.
├── README.md                   # This file
├── ARCHITECTURE.md             # Detailed architecture, data flow, test results
├── main.py                     # Runtime A: Strands Agent + tenant isolation
├── runtime_b/
│   └── main.py                 # Runtime B: stateless code executor
├── generate_sample_data.py     # Generate sample data for 2 tenants
├── requirements.txt            # Python dependencies
├── .env.example                # Environment variable template
└── .gitignore
```
