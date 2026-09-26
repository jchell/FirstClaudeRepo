# syntax=docker/dockerfile:1
# Kafka Connect with Debezium plus the platform's Vault config provider, so connector
# configs hold only ${vault:...} placeholders and never plaintext credentials.
FROM debezium/connect:2.7.3.Final AS base

# Compile the provider against the exact Kafka/Jackson jars the worker runs with.
FROM eclipse-temurin:21-jdk-alpine AS build
WORKDIR /src
COPY --from=base /kafka/libs/kafka-clients-*.jar /kafka/libs/jackson-*.jar /deps/
COPY VaultConfigProvider.java io/dataplat/connect/
RUN javac --release 17 -cp "/deps/*" -d /out io/dataplat/connect/VaultConfigProvider.java \
    && cd /out && jar cf /dataplat-vault-config-provider.jar io

FROM base
COPY --from=build /dataplat-vault-config-provider.jar /kafka/libs/
ENV CONNECT_CONFIG_PROVIDERS=vault \
    CONNECT_CONFIG_PROVIDERS_VAULT_CLASS=io.dataplat.connect.VaultConfigProvider \
    CONNECT_CONFIG_PROVIDERS_VAULT_PARAM_ADDR=http://vault:8200 \
    CONNECT_CONFIG_PROVIDERS_VAULT_PARAM_APPROLE_DIR=/run/secrets/dataplat
