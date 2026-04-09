FROM public.ecr.aws/docker/library/maven:3.9-amazoncorretto-17 AS builder

WORKDIR /app
COPY pom.xml .
RUN mvn dependency:go-offline -B

COPY src/ src/
RUN mvn package -DskipTests -B

FROM public.ecr.aws/docker/library/amazoncorretto:17-al2023-headless

WORKDIR /app
COPY --from=builder /app/target/*.jar app.jar

ENV JAVA_OPTS="-Xmx512m" \
    PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "java $JAVA_OPTS -jar app.jar --server.port=${PORT}"]
