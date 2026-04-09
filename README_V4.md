# V4: Java Runtime A (LangGraph4j + LangChain4j + Bedrock)

V4 将 Runtime A 从 Python (Strands) 改为 **Java** (Spring Boot + LangGraph4j + LangChain4j)。Runtime B 不变（Python 执行器）。

## Architecture

```
Runtime A — Java (Spring Boot + LangGraph4j)
  │  AgentExecutor (ReACT pattern) with BedrockStreamingChatModel
  │  Tools: runtime_b_shell, runtime_b_python (@Tool annotation)
  │  /invocations → SSE (Phase 1 graph.stream + Phase 2 report forward)
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

## V3 (Python) vs V4 (Java)

| | V3 (Python) | V4 (Java) |
|---|---|---|
| Runtime A language | Python | **Java** |
| Agent framework | Strands (`stream_async`) | **LangGraph4j (`graph.stream()`)** |
| LLM SDK | Strands BedrockModel | **LangChain4j BedrockStreamingChatModel** |
| Tool definition | `@tool` decorator | **`@Tool` annotation** |
| AgentCore protocol | `BedrockAgentCoreApp` (SDK) | **Spring Boot Controller (手动实现)** |
| SSE streaming | Strands async generator | **SseEmitter + CompletableFuture** |
| Runtime B | Python (shell/python/report) | **不变** |

## Build & Test

### On bastion (us-east-1):

```bash
# Compile
cd runtime_a_v4
mvn compile

# Package
mvn package -DskipTests

# Run (requires Bedrock IAM permissions)
DATA_BUCKET=your-bucket \
AWS_REGION=us-east-1 \
RUNTIME_B_ARN=arn:aws:bedrock-agentcore:... \
MODEL_ID=us.anthropic.claude-opus-4-6-v1 \
java -jar target/agentcore-runtime-a-v4-1.0.0.jar

# Test
curl http://localhost:8080/ping
curl -N -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"acme-corp","message":"分析Q1销售达成率"}'
```

### Deploy to AgentCore:

```bash
cd runtime_a_v4
agentcore configure --create --name "data_router_v4_java" --entrypoint "Dockerfile" --region "us-east-1" --non-interactive
agentcore deploy \
  --env "DATA_BUCKET=your-bucket" \
  --env "AWS_REGION=us-east-1" \
  --env "RUNTIME_B_ARN=<runtime-b-arn>" \
  --env "MODEL_ID=us.anthropic.claude-opus-4-6-v1"
```

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
    ├── config/AppConfig.java                  # Bedrock clients + ChatModel beans
    ├── controller/AgentCoreController.java    # /invocations (SSE) + /ping
    └── tools/RuntimeBTools.java               # @Tool: runtime_b_shell, runtime_b_python
```
