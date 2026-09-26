package io.dataplat.connect;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import org.apache.kafka.common.config.ConfigData;
import org.apache.kafka.common.config.ConfigException;
import org.apache.kafka.common.config.provider.ConfigProvider;

/**
 * Resolves {@code ${vault:<kv v2 data path>:<key>}} placeholders in connector configs.
 *
 * <p>Logs in with the Kafka Connect service's own AppRole, whose role_id/secret_id
 * files are mounted from outside the repo. Connector configs therefore only ever
 * contain placeholders such as
 * {@code ${vault:kv/data/dataplat/service-accounts/etl/connections/42:password}};
 * the values exist only in Vault and in this worker's memory.
 *
 * <p>Worker config:
 * <pre>
 * config.providers=vault
 * config.providers.vault.class=io.dataplat.connect.VaultConfigProvider
 * config.providers.vault.param.addr=http://vault:8200
 * config.providers.vault.param.approle.dir=/run/secrets/dataplat
 * </pre>
 */
public class VaultConfigProvider implements ConfigProvider {
    private static final ObjectMapper JSON = new ObjectMapper();
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
    private String addr;
    private Path approleDir;
    private String token;
    private Instant tokenExpires = Instant.EPOCH;

    @Override
    public void configure(Map<String, ?> configs) {
        addr = param(configs, "addr", "http://vault:8200").replaceAll("/+$", "");
        approleDir = Path.of(param(configs, "approle.dir", "/run/secrets/dataplat"));
    }

    private static String param(Map<String, ?> configs, String key, String fallback) {
        Object v = configs.get(key);
        return v == null ? fallback : String.valueOf(v);
    }

    private synchronized String token() {
        if (token != null && Instant.now().isBefore(tokenExpires)) {
            return token;
        }
        try {
            String roleId = Files.readString(approleDir.resolve("role_id")).trim();
            String secretId = Files.readString(approleDir.resolve("secret_id")).trim();
            String body = JSON.writeValueAsString(Map.of("role_id", roleId, "secret_id", secretId));
            HttpResponse<String> r = http.send(
                    HttpRequest.newBuilder(URI.create(addr + "/v1/auth/approle/login"))
                            .timeout(Duration.ofSeconds(15))
                            .POST(HttpRequest.BodyPublishers.ofString(body))
                            .build(),
                    HttpResponse.BodyHandlers.ofString());
            if (r.statusCode() != 200) {
                throw new ConfigException("Vault AppRole login failed with HTTP " + r.statusCode());
            }
            JsonNode auth = JSON.readTree(r.body()).path("auth");
            token = auth.path("client_token").asText();
            long ttl = auth.path("lease_duration").asLong(3600);
            tokenExpires = Instant.now().plusSeconds(Math.max(30, ttl * 8 / 10));
            return token;
        } catch (IOException e) {
            throw new ConfigException("Vault AppRole login failed: " + e.getMessage());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ConfigException("interrupted during Vault login");
        }
    }

    private JsonNode read(String path) {
        if (path == null || path.isBlank() || path.contains("..")) {
            throw new ConfigException("invalid Vault path");
        }
        for (int attempt = 0; attempt < 2; attempt++) {
            try {
                HttpResponse<String> r = http.send(
                        HttpRequest.newBuilder(URI.create(addr + "/v1/" + path.replaceAll("^/+", "")))
                                .timeout(Duration.ofSeconds(15))
                                .header("X-Vault-Token", token())
                                .GET()
                                .build(),
                        HttpResponse.BodyHandlers.ofString());
                if (r.statusCode() == 403 && attempt == 0) {
                    synchronized (this) {
                        token = null; // expired or revoked token: log in again once
                    }
                    continue;
                }
                if (r.statusCode() != 200) {
                    // Never include the response body: it could contain secret material.
                    throw new ConfigException("reading " + path + " from Vault failed with HTTP " + r.statusCode());
                }
                JsonNode data = JSON.readTree(r.body()).path("data");
                return data.has("data") && data.path("data").isObject() ? data.path("data") : data; // KV v2 or v1
            } catch (IOException e) {
                throw new ConfigException("reading " + path + " from Vault failed: " + e.getClass().getSimpleName());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new ConfigException("interrupted while reading Vault");
            }
        }
        throw new ConfigException("reading " + path + " from Vault failed");
    }

    @Override
    public ConfigData get(String path) {
        Map<String, String> out = new HashMap<>();
        read(path).fields().forEachRemaining(e -> out.put(e.getKey(), e.getValue().asText()));
        return new ConfigData(out);
    }

    @Override
    public ConfigData get(String path, Set<String> keys) {
        JsonNode data = read(path);
        Map<String, String> out = new HashMap<>();
        for (String key : keys) {
            if (!data.has(key)) {
                throw new ConfigException("key " + key + " not found at " + path);
            }
            out.put(key, data.get(key).asText());
        }
        return new ConfigData(out);
    }

    @Override
    public void close() {}
}
