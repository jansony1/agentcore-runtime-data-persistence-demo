package com.demo.agentcore.controller;

import com.demo.agentcore.tools.RuntimeBTools;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.SystemMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.model.bedrock.BedrockChatModel;
import dev.langchain4j.model.bedrock.BedrockStreamingChatModel;
import org.bsc.langgraph4j.agentexecutor.AgentExecutor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.services.bedrockagentcore.BedrockAgentCoreClient;
import software.amazon.awssdk.services.bedrockagentcore.model.InvokeAgentRuntimeRequest;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.util.*;
import java.util.concurrent.CompletableFuture;

/**
 * AgentCore HTTP protocol: POST /invocations (SSE) + GET /ping.
 *
 * Uses LangGraph4j AgentExecutor (ReACT pattern) with BedrockStreamingChatModel
 * for Phase 1 streaming, and forwards Runtime B report SSE for Phase 2.
 */
@RestController
public class AgentCoreController {

    private static final Logger log = LoggerFactory.getLogger(AgentCoreController.class);
    private static final ObjectMapper mapper = new ObjectMapper();

    private final BedrockAgentCoreClient agentCoreClient;
    private final BedrockChatModel chatModel;
    private final BedrockStreamingChatModel streamingChatModel;

    @Value("${app.runtime-b-arn}")
    private String runtimeBArn;

    @Value("${app.data-bucket}")
    private String dataBucket;

    private static final String SYSTEM_PROMPT = """
            你是一个数据分析 Agent，你是唯一的决策者。

            ## 你的工具
            1. **runtime_b_shell** — 在远程工作站上执行 shell 命令
               - 工作目录: /tmp/workspace（持久化，跨调用共享）
               - 用于: aws s3 cp, head, wc, sort, ls, cat 等
            2. **runtime_b_python** — 在远程工作站上执行 Python 代码
               - 可用变量: WORKSPACE = "/tmp/workspace"
               - 输出文件写到: WORKSPACE + "/output/"
               - 可用库: pandas, numpy, matplotlib

            ## 工作流程
            1. 用 runtime_b_shell 从 S3 下载数据到工作站
            2. 用 runtime_b_shell 预览数据结构 (head, wc 等)
            3. 用 runtime_b_python 执行 pandas 数据处理和计算
            4. 如果代码报错，阅读 stderr，修正代码，重试（最多 3 次）
            5. 用 runtime_b_python 生成 matplotlib 图表，保存到 /tmp/workspace/output/
            6. 将处理后的数据（CSV）保存到 /tmp/workspace/output/

            ## 重要规则
            - 总是 print() 关键分析结论和数据表格
            - 图表和 CSV 保存到 /tmp/workspace/output/
            - 你负责数据准备和计算，最终的深度分析和报告将由下游完成
            """;

    public AgentCoreController(BedrockAgentCoreClient agentCoreClient,
                               BedrockChatModel chatModel,
                               BedrockStreamingChatModel streamingChatModel) {
        this.agentCoreClient = agentCoreClient;
        this.chatModel = chatModel;
        this.streamingChatModel = streamingChatModel;
    }

    @GetMapping("/ping")
    public Map<String, String> ping() {
        return Map.of("status", "Healthy");
    }

    @PostMapping(value = "/invocations", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter invoke(@RequestBody Map<String, Object> payload,
                             @RequestHeader(value = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id",
                                     required = false) String headerSessionId) {

        SseEmitter emitter = new SseEmitter(600_000L);

        String tenantId = (String) payload.getOrDefault("tenant_id", "default");
        String sessionId = headerSessionId != null ? headerSessionId : UUID.randomUUID().toString();
        String message = (String) payload.getOrDefault("message",
                payload.getOrDefault("input", payload.getOrDefault("prompt", "")));

        log.info("Tenant: {} | Session: {} | Query: {}", tenantId, sessionId,
                message.length() > 80 ? message.substring(0, 80) + "..." : message);

        CompletableFuture.runAsync(() -> {
            try {
                String s3Prefix = "tenants/" + tenantId + "/datasets/";
                String s3OutputPrefix = "tenants/" + tenantId + "/reports/";

                // === Phase 1: LangGraph4j AgentExecutor with streaming ===
                sendEvent(emitter, Map.of("type", "status", "stage", "analysis",
                        "message", "Agent 开始分析..."));

                RuntimeBTools tools = new RuntimeBTools(agentCoreClient, runtimeBArn, sessionId);

                // Build graph with StreamingChatModel for Phase 1 streaming
                var graph = AgentExecutor.builder()
                        .chatModel(streamingChatModel)
                        .systemMessage(SystemMessage.from(SYSTEM_PROMPT))
                        .toolsFromObject(tools)
                        .build()
                        .compile();

                String userPrompt = String.format(
                        "租户数据在 S3: s3://%s/%s\n请下载数据并完成以下分析任务: %s\n输出文件保存到 /tmp/workspace/output/",
                        dataBucket, s3Prefix, message);

                var inputs = Map.<String, Object>of("messages", List.of(UserMessage.from(userPrompt)));

                // graph.stream() yields NodeOutput at each step (agent think, tool exec)
                String lastNodeName = "";
                for (var output : graph.stream(inputs)) {
                    String nodeName = output.node();
                    if (!nodeName.equals(lastNodeName)) {
                        String statusMsg = switch (nodeName) {
                            case "agent" -> "Agent 思考中...";
                            case "tools", "action" -> "正在执行工具...";
                            default -> "节点: " + nodeName;
                        };
                        sendEvent(emitter, Map.of("type", "status", "stage", "analysis",
                                "message", statusMsg));
                        lastNodeName = nodeName;
                    }
                }

                sendEvent(emitter, Map.of("type", "status", "stage", "analysis",
                        "message", "数据准备完成"));

                // === Phase 2: Report streaming from Runtime B ===
                sendEvent(emitter, Map.of("type", "status", "stage", "report",
                        "message", "正在分析数据并生成报告..."));

                String analysisContext = "数据已准备完成，请基于 workspace 中的文件进行分析和报告生成。";
                streamReportFromRuntimeB(emitter, sessionId, analysisContext, s3OutputPrefix);

                emitter.complete();

            } catch (Exception e) {
                log.error("Error: {}", e.getMessage(), e);
                try {
                    sendEvent(emitter, Map.of("type", "error", "message",
                            e.getMessage() != null ? e.getMessage() : e.toString()));
                    emitter.complete();
                } catch (Exception ignored) {
                }
            }
        });

        return emitter;
    }

    private void streamReportFromRuntimeB(SseEmitter emitter, String sessionId,
                                          String context, String s3OutputPrefix) throws Exception {
        String payload = mapper.writeValueAsString(Map.of(
                "action", "report",
                "context", context,
                "s3_output_prefix", s3OutputPrefix
        ));

        InputStream responseStream = agentCoreClient.invokeAgentRuntime(
                InvokeAgentRuntimeRequest.builder()
                        .agentRuntimeArn(runtimeBArn)
                        .payload(SdkBytes.fromUtf8String(payload))
                        .contentType("application/json")
                        .accept("text/event-stream")
                        .runtimeSessionId(sessionId)
                        .qualifier("DEFAULT")
                        .build()
        );

        try (var reader = new BufferedReader(new InputStreamReader(responseStream))) {
            String line;
            while ((line = reader.readLine()) != null) {
                if (line.startsWith("data: ")) {
                    String eventData = line.substring(6);
                    try {
                        @SuppressWarnings("unchecked")
                        Map<String, Object> event = mapper.readValue(eventData, Map.class);
                        sendEvent(emitter, event);
                    } catch (Exception e) {
                        sendEvent(emitter, Map.of("type", "raw", "content", eventData));
                    }
                }
            }
        }
    }

    private void sendEvent(SseEmitter emitter, Map<String, Object> data) {
        try {
            emitter.send(SseEmitter.event().data(mapper.writeValueAsString(data)));
        } catch (Exception e) {
            log.warn("Failed to send SSE event: {}", e.getMessage());
        }
    }
}
