package com.demo.agentcore.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.bedrockagentcore.BedrockAgentCoreClient;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient;

import dev.langchain4j.model.bedrock.BedrockChatModel;
import dev.langchain4j.model.bedrock.BedrockStreamingChatModel;

@Configuration
public class AppConfig {

    @Value("${app.aws-region}")
    private String awsRegion;

    @Value("${app.model-id}")
    private String modelId;

    @Bean
    public BedrockAgentCoreClient bedrockAgentCoreClient() {
        return BedrockAgentCoreClient.builder()
                .region(Region.of(awsRegion))
                .build();
    }

    @Bean
    public BedrockRuntimeClient bedrockRuntimeClient() {
        return BedrockRuntimeClient.builder()
                .region(Region.of(awsRegion))
                .build();
    }

    @Bean
    public BedrockChatModel chatModel() {
        return BedrockChatModel.builder()
                .modelId(modelId)
                .region(Region.of(awsRegion))
                .build();
    }

    @Bean
    public BedrockStreamingChatModel streamingChatModel() {
        return BedrockStreamingChatModel.builder()
                .modelId(modelId)
                .region(Region.of(awsRegion))
                .build();
    }
}
