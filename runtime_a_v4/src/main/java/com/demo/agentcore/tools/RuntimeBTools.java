package com.demo.agentcore.tools;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.langchain4j.agent.tool.P;
import dev.langchain4j.agent.tool.Tool;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.services.bedrockagentcore.BedrockAgentCoreClient;
import software.amazon.awssdk.services.bedrockagentcore.model.InvokeAgentRuntimeRequest;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

/**
 * LangChain4j tools that remote-control Runtime B via invoke_agent_runtime.
 * Each tool call sends an action to Runtime B and returns the result.
 */
public class RuntimeBTools {

    private static final Logger log = LoggerFactory.getLogger(RuntimeBTools.class);
    private static final ObjectMapper mapper = new ObjectMapper();

    private final BedrockAgentCoreClient agentCoreClient;
    private final String runtimeBArn;
    private final String sessionId;

    public RuntimeBTools(BedrockAgentCoreClient agentCoreClient, String runtimeBArn, String sessionId) {
        this.agentCoreClient = agentCoreClient;
        this.runtimeBArn = runtimeBArn;
        this.sessionId = sessionId;
    }

    @Tool("Execute a shell command on Runtime B's workspace (/tmp/workspace). " +
          "Use for: aws s3 cp, head, tail, wc, sort, ls, cat, file inspection, pip install. " +
          "The workspace persists across calls within the same session.")
    public String runtime_b_shell(@P("Shell command to execute") String command) {
        log.info("Shell: {}", command.length() > 80 ? command.substring(0, 80) + "..." : command);
        return invokeRuntimeB("shell", Map.of("command", command));
    }

    @Tool("Execute Python code on Runtime B's workspace. " +
          "WORKSPACE='/tmp/workspace'. Write output files to WORKSPACE+'/output/'. " +
          "Available libraries: pandas, numpy, matplotlib. Use print() for key findings.")
    public String runtime_b_python(@P("Python code to execute") String code) {
        log.info("Python: {} chars", code.length());
        return invokeRuntimeB("python", Map.of("code", code));
    }

    private String invokeRuntimeB(String action, Map<String, String> extra) {
        try {
            Map<String, Object> payload = new HashMap<>(extra);
            payload.put("action", action);
            String payloadJson = mapper.writeValueAsString(payload);

            // invokeAgentRuntime returns ResponseInputStream
            try (InputStream responseStream = agentCoreClient.invokeAgentRuntime(
                    InvokeAgentRuntimeRequest.builder()
                            .agentRuntimeArn(runtimeBArn)
                            .payload(SdkBytes.fromUtf8String(payloadJson))
                            .contentType("application/json")
                            .runtimeSessionId(sessionId)
                            .qualifier("DEFAULT")
                            .build()
            )) {
                String body = new String(responseStream.readAllBytes(), StandardCharsets.UTF_8);
                log.info("{} result: {} chars", action, body.length());
                return body;
            }

        } catch (Exception e) {
            log.error("Runtime B {} failed: {}", action, e.getMessage(), e);
            return String.format("{\"status\":\"error\",\"error\":\"%s\"}", e.getMessage());
        }
    }
}
