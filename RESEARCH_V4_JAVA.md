# V4 Research: Java + LangGraph on AgentCore Runtime

## Summary

调研 Runtime A 使用 Java + LangGraph 的可行性。Runtime B 不受影响（仍为 Python 执行器）。

**结论：可行，但需要自己实现 AgentCore HTTP 协议层（Python 里现成的 `BedrockAgentCoreApp` 无 Java 对等物）。**

---

## Java AI Agent 生态现状

### LangGraph

| | Python | JavaScript/TypeScript | Java |
|---|---|---|---|
| 官方 SDK | `langgraph` (28,648 stars) | `langgraphjs` (2,754 stars) | **无官方支持** |
| 社区替代 | N/A | N/A | **LangGraph4j** (1,524 stars) |

**LangGraph 官方不支持 Java，无计划支持。**

### LangGraph4j (社区项目)

- **GitHub**: langgraph4j/langgraph4j
- **Stars**: 1,524 | **Contributors**: 20 | **License**: MIT
- **Latest**: v1.8.11 (2026-03-30)，2026 年已发布 11 个版本，开发活跃
- **Java 版本要求**: Java 17+
- **Maven**: `org.bsc.langgraph4j:langgraph4j-core`

**功能对比 Python LangGraph：**

| 功能 | Python LangGraph | LangGraph4j |
|------|-----------------|-------------|
| StateGraph + AgentState | Yes | Yes |
| 循环图（非 DAG） | Yes | Yes |
| 条件边 | Yes | Yes |
| Checkpoint 持久化 | Postgres, SQLite, Redis | Postgres, MySQL, Oracle, Redis |
| Human-in-the-loop | Yes | Yes |
| Async / Streaming | Yes | CompletableFuture |
| 可视化 | Mermaid | PlantUML + Mermaid |
| 调试 UI | LangGraph Studio | 自带 Studio (Jetty/Quarkus/Spring Boot) |
| 与 LLM 框架集成 | LangChain | **LangChain4j + Spring AI** |

**评估：功能基本对等，适合生产使用。**

### LangChain4j

- **GitHub**: langchain4j/langchain4j
- **Stars**: 11,496 | **Latest**: v1.12.2 (2026-03)
- **状态**: 生产级，活跃开发

**关键能力：**

| 能力 | 支持 |
|------|------|
| Tool/Function calling | Yes |
| MCP (Model Context Protocol) | Yes (`langchain4j-agentic-mcp`) |
| A2A Protocol | Yes (`langchain4j-agentic-a2a`) |
| Agent loops | Yes (`langchain4j-agentic`) |
| 多 Agent 工作流 | Yes (Sequential, Parallel, Conditional, Loop) |
| Amazon Bedrock | Yes (`langchain4j-bedrock`) |
| Spring Boot 集成 | Yes |
| Quarkus 集成 | Yes |

### Spring AI

- **GitHub**: spring-projects/spring-ai
- **Stars**: 8,442 | **Latest**: v1.1.4 (2026-03)
- VMware/Broadcom 官方项目
- 支持 Bedrock，支持 tool calling，与 LangGraph4j 集成

---

## AgentCore + Java 可行性

### AWS SDK Java v2 支持

AWS SDK Java v2 (v2.42.30) 包含 AgentCore 模块：

```xml
<!-- 数据面 (invoke_agent_runtime 等) -->
<dependency>
    <groupId>software.amazon.awssdk</groupId>
    <artifactId>bedrockagentcore</artifactId>
</dependency>

<!-- 控制面 (create/get/list runtime 等) -->
<dependency>
    <groupId>software.amazon.awssdk</groupId>
    <artifactId>bedrockagentcorecontrol</artifactId>
</dependency>
```

### AgentCore Runtime HTTP 协议

AgentCore Runtime 只关心两个 HTTP 端点：

```
POST /invocations  →  处理请求，返回 JSON 或 SSE
GET  /ping         →  返回 {"status": "Healthy"}
```

请求头中包含：
- `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` → session_id
- `X-Amzn-Bedrock-AgentCore-Runtime-Request-Id` → request_id
- `X-Amzn-Bedrock-AgentCore-Runtime-Custom-*` → 自定义 header

**任何能实现这两个端点的 HTTP 框架都可以作为 AgentCore Runtime。** 已有先例：Adam Bien 用 Quarkus 实现过。

### Python vs Java SDK 差异

| 能力 | Python | Java |
|------|--------|------|
| `BedrockAgentCoreApp` (高级封装) | **有** | **无**，需自己实现 HTTP server |
| `@app.entrypoint` 装饰器 | **有** | **无**，自己写 Controller |
| generator → SSE 自动转换 | **有** | **无**，用 `SseEmitter` / `Flux<ServerSentEvent>` |
| `invoke_agent_runtime` | boto3 | **有**，AWS SDK Java v2 `bedrockagentcore` |
| Strands `@tool` | **有** | **无**，用 LangChain4j `@Tool` 注解 |
| `agentcore deploy` CLI | **有** | **有**（container 部署，需自己写 Dockerfile） |

---

## 推荐技术栈

```
Runtime A (Java):
  Spring Boot 3.x / Quarkus
    + LangChain4j (Bedrock 模型调用 + Tool calling)
    + LangGraph4j (图执行引擎, 可选)
    + AWS SDK Java v2 bedrockagentcore (调 Runtime B)
    + 自己实现 /invocations + /ping
    + Container 部署到 AgentCore

Runtime B (Python, 不变):
  bedrock-agentcore SDK
    + shell_exec / exec_python / report rendering
```

### Java Runtime A 代码骨架

```java
@RestController
public class AgentCoreController {

    private final BedrockAgentCoreClient agentCoreClient;
    private final ChatLanguageModel model;  // LangChain4j Bedrock model

    @GetMapping("/ping")
    public Map<String, String> ping() {
        return Map.of("status", "Healthy");
    }

    @PostMapping(value = "/invocations", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter invoke(
            @RequestBody InvokeRequest request,
            @RequestHeader(value = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id",
                          required = false) String sessionId) {

        SseEmitter emitter = new SseEmitter(600_000L);

        CompletableFuture.runAsync(() -> {
            try {
                String tenantId = request.getTenantId();
                String sid = sessionId != null ? sessionId : UUID.randomUUID().toString();

                // Phase 1: Agent analysis via LangChain4j + tool calling
                emitter.send(SseEmitter.event().data(Map.of(
                    "type", "status", "stage", "analysis", "message", "Agent 开始分析...")));

                // Agent with tools that invoke Runtime B
                var agent = AiServices.builder(AnalysisAgent.class)
                    .chatLanguageModel(model)
                    .tools(new RuntimeBTools(agentCoreClient, RUNTIME_B_ARN, sid))
                    .build();

                String analysisResult = agent.analyze(request.getMessage());

                emitter.send(SseEmitter.event().data(Map.of(
                    "type", "status", "stage", "analysis", "message", "数据分析完成")));

                // Phase 2: Forward report SSE from Runtime B
                emitter.send(SseEmitter.event().data(Map.of(
                    "type", "status", "stage", "report", "message", "正在生成报告...")));

                var response = agentCoreClient.invokeAgentRuntime(req -> req
                    .agentRuntimeArn(RUNTIME_B_ARN)
                    .payload(SdkBytes.fromUtf8String(reportPayload(analysisResult, tenantId)))
                    .contentType("application/json")
                    .accept("text/event-stream")
                    .runtimeSessionId(sid)
                    .qualifier("DEFAULT"));

                // Parse and forward SSE
                try (var reader = new BufferedReader(
                        new InputStreamReader(response.response()))) {
                    String line;
                    while ((line = reader.readLine()) != null) {
                        if (line.startsWith("data: ")) {
                            emitter.send(SseEmitter.event().data(line.substring(6)));
                        }
                    }
                }
                emitter.complete();

            } catch (Exception e) {
                emitter.completeWithError(e);
            }
        });

        return emitter;
    }
}

// LangChain4j Tool class for Runtime B invocation
public class RuntimeBTools {

    private final BedrockAgentCoreClient client;
    private final String runtimeBArn;
    private final String sessionId;

    @Tool("Execute a shell command on the remote workspace")
    public String runtimeBShell(@P("Shell command to execute") String command) {
        return invokeRuntimeB("shell", Map.of("command", command));
    }

    @Tool("Execute Python code on the remote workspace")
    public String runtimeBPython(@P("Python code to execute") String code) {
        return invokeRuntimeB("python", Map.of("code", code));
    }

    private String invokeRuntimeB(String action, Map<String, String> extra) {
        var payload = new HashMap<>(extra);
        payload.put("action", action);
        var response = client.invokeAgentRuntime(req -> req
            .agentRuntimeArn(runtimeBArn)
            .payload(SdkBytes.fromUtf8String(toJson(payload)))
            .contentType("application/json")
            .runtimeSessionId(sessionId)
            .qualifier("DEFAULT"));
        return response.response().asUtf8String();
    }
}
```

---

## 工程量评估

| 工作项 | 预估 |
|--------|------|
| 实现 /invocations + /ping HTTP 协议 | 0.5 天 |
| 集成 LangChain4j + Bedrock 模型 | 0.5 天 |
| 实现 Runtime B tool calling (shell/python) | 1 天 |
| SSE 流式转发 | 0.5 天 |
| 租户隔离 + session 管理 | 0.5 天 |
| Dockerfile + AgentCore 部署 | 0.5 天 |
| 测试调试 | 1 天 |
| **总计** | **约 4-5 天** |

对比 Python 方案（已实现）约 1-2 天，Java 多出约 2-3 天，主要在 AgentCore HTTP 协议实现和 SSE 处理上。

---

## 风险

1. **LangGraph4j 是社区项目**：非 LangChain 官方，长期维护性有风险
2. **无 `BedrockAgentCoreApp` 封装**：HTTP 协议细节（header 解析、健康检查、SSE 格式）需要自己正确实现
3. **AgentCore session header 传递**：Java 框架需要正确提取和传递 `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` 等 header
4. **调试困难**：`agentcore dev` 本地调试工具目前只支持 Python
