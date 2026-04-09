# V4: Java Runtime A (LangGraph4j + LangChain4j + Bedrock)

V4 将 Runtime A 从 Python (Strands) 改为 **Java** (Spring Boot + LangGraph4j + LangChain4j)。Runtime B 不变（Python 执行器）。

## Architecture

```
Runtime A — Java (Spring Boot + LangGraph4j)
  │  AgentExecutor (ReACT pattern) with BedrockChatModel (sync)
  │  Tools: runtime_b_shell, runtime_b_python (@Tool annotation)
  │  /invocations → SSE (Phase 1 graph.stream + heartbeat + Phase 2 report forward)
  │  /ping → {"status": "Healthy"}
  │
  │  invoke_agent_runtime (same session = same microVM)
  ▼
Runtime B — Python (unchanged from V3)
  ├── action=shell   → subprocess
  ├── action=python  → exec
  └── action=report  → converse_stream(Opus 4.6) → SSE
```

## Tech Stack

| Component | Technology | Version |
|-----------|-----------|---------|
| Runtime A framework | Spring Boot | 3.4.3 |
| Agent framework | LangGraph4j (AgentExecutor) | 1.8.11 |
| LLM integration | LangChain4j Bedrock | 1.12.2 |
| AWS SDK | AWS SDK Java v2 | 2.42.30 |
| AgentCore invoke | `software.amazon.awssdk:bedrockagentcore` | via BOM |
| Java | Amazon Corretto | 17 |
| Runtime B | Python + bedrock-agentcore SDK | 3.11 |

## Deployed Test Results

### Deployed Runtimes

- **Runtime A (Java)**: `arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/data_router_v4_java-EuNF2wBDaL`
- **Runtime B (Python)**: `arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/data_workstation_v3-k2h6743g84`

### E2E Test Output (actual deployed result)

```
[analysis] Agent 开始分析...
[analysis] 节点: __START__
[analysis] Agent 思考中...
[analysis] 正在执行工具...
[analysis] Agent 思考中...
[analysis] 正在执行工具...
[heartbeat]
[analysis] Agent 思考中...
[analysis] 正在执行工具...
[heartbeat]
[analysis] Agent 思考中...
[analysis] 正在执行工具...
[heartbeat]
[analysis] Agent 思考中...
[analysis] 正在执行工具...
[analysis] Agent 思考中...
[analysis] 正在执行工具...
[heartbeat]
[analysis] Agent 思考中...
[analysis] 正在执行工具...
[analysis] 节点: __END__
[analysis] 数据准备完成
[report] 正在分析数据并生成报告...
[report] 正在生成分析报告 (Opus 4.6 streaming)...
           (1209 chunks, 4987 chars 流式输出)
[done] 3 files → S3
```

### Report Preview (deployed output)

```markdown
# 2026年第一季度各区域销售达成率分析报告

## 一、概述
本报告基于2026年第一季度全公司五大销售区域的业绩数据进行系统性分析。

| 指标 | 数值 |
|:---|---:|
| Q1全公司销售目标 | 18,500,000 元 |
| Q1全公司实际销售额 | 9,195,062 元 |
| Q1全公司总达成率 | 49.70% |
| 达成率最高区域 | 华中（64.42%） |
| 达成率最低区域 | 华东（37.01%） |
| 目标缺口总额 | 9,304,938 元 |
```

### S3 Persisted Output

```
tenants/acme-corp/reports/
├── analysis_report.md                     ← Opus 4.6 深度分析报告
├── q1_region_achievement_ranking.csv      ← Agent A 指挥 Runtime B 计算
└── q1_region_achievement_ranking.png      ← Agent A 指挥 Runtime B 生成
```

## V3 (Python) vs V4 (Java) — Deployed Comparison

| | V3 (Python) | V4 (Java) |
|---|---|---|
| Runtime A language | Python | **Java** |
| Agent framework | Strands (`stream_async`) | **LangGraph4j (`graph.stream()`)** |
| LLM model | BedrockModel (Strands) | **BedrockChatModel (LangChain4j, sync)** |
| Tool definition | `@tool` decorator | **`@Tool` annotation** |
| AgentCore protocol | `BedrockAgentCoreApp` (SDK) | **Spring Boot Controller (手动实现)** |
| SSE streaming | async generator | **SseEmitter + CompletableFuture + heartbeat** |
| Phase 1 tool calls | 多轮 | **7 轮** |
| Phase 2 report | 806 chunks / 3358 chars | **1209 chunks / 4987 chars** |
| 报告包含真实数据 | ✅ | **✅ (华中 64.42%, 总达成率 49.70%)** |
| Runtime B | Python (shell/python/report) | **不变** |

## Deploy

### 1. Deploy Runtime B (reuse V3, if not already deployed)

```bash
cd runtime_b_v3
agentcore deploy \
  --env "DATA_BUCKET=your-bucket" \
  --env "AWS_REGION=us-east-1" \
  --env "OPUS_MODEL_ID=us.anthropic.claude-opus-4-6-v1"
```

### 2. Deploy Runtime A (Java)

```bash
cd runtime_a_v4
agentcore configure --create --name "data_router_v4_java" --entrypoint "Dockerfile" --region "us-east-1" --non-interactive
# Enable ecr_auto_create and s3_auto_create in .bedrock_agentcore.yaml
agentcore deploy \
  --env "DATA_BUCKET=your-bucket" \
  --env "AWS_REGION=us-east-1" \
  --env "RUNTIME_B_ARN=<runtime-b-arn>" \
  --env "MODEL_ID=us.anthropic.claude-opus-4-6-v1" \
  --env "OPUS_MODEL_ID=us.anthropic.claude-opus-4-6-v1"
```

### 3. Add IAM Permissions

Runtime A execution role needs:
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
        print(line.decode('utf-8'))
```

## V4 Pitfalls (fixed)

| 问题 | 原因 | 修复 |
|------|------|------|
| 迭代上限 25 次 | LangGraph4j 默认 `recursionLimit=25` | `CompileConfig.builder().recursionLimit(100)` |
| Netty ReadTimeout | `BedrockStreamingChatModel` 用异步 Netty client | 改用同步 `BedrockChatModel` |
| SSE 连接超时 | 同步模型思考时无数据输出 | 心跳线程每 15s 发 `{"type":"heartbeat"}` |
| 报告为空模板 | `analysisContext` 硬编码占位符 | 从 `graph.stream()` 最后状态提取 `state.finalResponse()` |
| AWS SDK 超时 | 默认 API 超时太短 | `ClientOverrideConfiguration` 设 5min timeout |

## V3 Lessons Applied

| V3 Pitfall | V4 How We Handle It |
|------------|-------------------|
| `runtimeSessionId` min 33 chars | `UUID.randomUUID().toString()` (36 chars) |
| `contextvars` thread propagation | Java field in RuntimeBTools (passed via constructor) |
| Port must be 8080 | `application.yml: server.port=${PORT:8080}` |
| `converse_stream` needs inference profile ID | Configured via `MODEL_ID` env var |
| ECR base image (avoid Docker Hub rate limit) | `public.ecr.aws/docker/library/amazoncorretto:17` |

## File Structure

```
runtime_a_v4/
├── pom.xml                                    # Maven: Spring Boot + LangGraph4j + LangChain4j + AWS SDK
├── Dockerfile                                 # Multi-stage: Maven build → Corretto 17 runtime
└── src/main/java/com/demo/agentcore/
    ├── Application.java                       # Spring Boot entry
    ├── config/AppConfig.java                  # Bedrock clients + ChatModel beans (5min timeout)
    ├── controller/AgentCoreController.java    # /invocations (SSE + heartbeat) + /ping
    └── tools/RuntimeBTools.java               # @Tool: runtime_b_shell, runtime_b_python
```
