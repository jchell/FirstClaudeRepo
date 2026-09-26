# Vault server mode with integrated raft storage (not dev mode): data survives restarts.
# TLS for internal services comes later via the PKI engine; the listener is only
# reachable on the compose network and on 127.0.0.1 of the laptop.
ui            = true
disable_mlock = true

storage "raft" {
  path    = "/vault/file"
  node_id = "dataplat-vault-1"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

api_addr     = "http://vault:8200"
cluster_addr = "http://vault:8201"
