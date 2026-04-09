package com.demo.agentcore.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import software.amazon.awssdk.core.client.config.ClientOverrideConfiguration;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.bedrockagentcore.BedrockAgentCoreClient;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient;

import dev.langchain4j.model.bedrock.BedrockChatModel;
import dev.langchain4j.model.bedrock.BedrockStreamingChatModel;

import java.time.Duration;

@Configuration
public class AppConfig {

    @Value("${app.aws-region}")
    private String awsRegion;

    @Value("${app.model-id}")
    private String modelId;

    private static final Duration API_TIMEOUT = Duration.ofMinutes(5);

    @Bean
    public BedrockAgentCoreClient bedrockAgentCoreClient() {
        return BedrockAgentCoreClient.builder()
                .region(Region.of(awsRegion))
                .overrideConfiguration(ClientOverrideConfiguration.builder()
                        .apiCallTimeout(Duration.ofMinutes(10))
                        .apiCallAttemptTimeout(API_TIMEOUT)
                        .build())
                .build();
    }

    @Bean
    public BedrockRuntimeClient bedrockRuntimeClient() {
        return BedrockRuntimeClient.builder()
                .region(Region.of(awsRegion))
                .overrideConfiguration(ClientOverrideConfiguration.builder()
                        .apiCallTimeout(API_TIMEOUT)
                        .apiCallAttemptTimeout(API_TIMEOUT)
                        .build())
                .build();
    }

    @Bean
    public BedrockChatModel chatModel() {
        return BedrockChatModel.builder()
                .modelId(modelId)
                .region(Region.of(awsRegion))
                .timeout(API_TIMEOUT)
                .build();
    }

    @Bean
    public BedrockStreamingChatModel streamingChatModel() {
        return BedrockStreamingChatModel.builder()
                .modelId(modelId)
                .region(Region.of(awsRegion))
                .timeout(API_TIMEOUT)
                .build();
    }
}
