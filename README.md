# Data Platform

A laptop-first, cloud-swappable data platform with a management console.
The full design is in [docs/PLATFORM_PLAN.md](docs/PLATFORM_PLAN.md); this repo is built in the
phases listed there.

| Phase | Status |
|---|---|
| 0 — Foundations | ✅ done |
| 1 — Ingestion MVP | next |
| 1b, 2, 3, 3b, 4, 5 | planned |

## Quick start (Windows)

Needs Docker Desktop (WSL2 backend) and about 10–12 GB of RAM for the full stack.

```powershell
./scripts/tasks.ps1 init      # first time only: prints the admin password once
./scripts/tasks.ps1 up        # every later start (unseals Vault)
./scripts/tasks.ps1 up -Lite  # without Kafka Connect/Debezium, for smaller machines
./scripts/tasks.ps1 down
```

On Linux/macOS use `make init`, `make up` (`make up LITE=1`) and `make down`.

Then open the console at http://localhost:3000 and sign in as `admin` with the password
`init` printed.

| Service | URL (127.0.0.1 only) |
|---|---|
| Console | http://localhost:3000 |
| API docs | http://localhost:8000/api/docs |
| Redpanda Console | http://localhost:8080 |
| MinIO console | http://localhost:9001 |
| Vault UI | http://localhost:8200 |

## Layout

```
config/platform.yaml   picks one adapter per port — swapping infrastructure is a config change
backend/dataplat/
  core/ports/          abstract interfaces (ObjectStore, QueryEngine, SecretStore, EventBus, ...)
  adapters/            local adapters: Vault, Postgres, MinIO, DuckDB, Redpanda, Oxigraph, lineage
  api/                 FastAPI app (auth, admin, jobs, health, lineage)
  security/            passwords, login/refresh/lockout, service accounts
  orchestration/       job handlers, worker, scheduler, stream worker
  bootstrap/           Vault init/unseal/configure, per-service policies
  db/, alembic/        metadata schema and migrations
console/               React + TypeScript + Vite + Mantine console
infra/                 Vault server config, Postgres init SQL
scripts/               tasks.ps1 (Windows), e2e.sh (from-scratch validation)
```

## Security model

- **Secrets live only in Vault.** Everything else stores references like
  `vault://kv/dataplat/service-accounts/etl-sftp/sftp#password`. Secret fields in the API are
  write-only, validation errors never echo input, and logs are redacted.
- **No static database passwords.** Vault owns the Postgres superuser password (rotated at
  setup) and gives every service its own short-lived login. Logins expire after 24h, or when
  the service's Vault token is revoked.
- **Per-service AppRoles with least privilege.** The API can write secrets but not read them
  back. Workers hold no service-account access of their own. Only the scheduler can mint job
  tokens.
- **Service accounts.** Each has its own Vault path and policy. A job that runs as a service
  account gets a single-use wrapped token that can read only that account's secrets. The
  scheduler refuses to mint a token if the account's policy was changed outside the platform.
- **Console sessions.** JWTs are signed by Vault transit (ed25519), so the private key never
  leaves Vault. The access token is held in memory only. The refresh token is an httpOnly,
  SameSite=Strict cookie that rotates on every use, and reusing an old one revokes the session.
  Failed logins lock the account and are rate-limited.
- **Bootstrap material** (unseal key, AppRole credentials) lives in `%USERPROFILE%\.dataplat`,
  outside the repo. On Windows the unseal key is encrypted with DPAPI. The Vault root token is
  revoked after every setup run.

## Tests

```bash
cd backend
DATAPLAT_TEST_DATABASE_URL=postgresql+psycopg://postgres:pw@localhost:5432/postgres uv run --extra dev pytest
DATAPLAT_ADMIN_PASSWORD=... uv run --extra dev pytest -m integration   # against the running stack
cd ../console && npm test && DATAPLAT_ADMIN_PASSWORD=... npm run e2e
scripts/e2e.sh                     # everything from scratch (what CI runs)
```

The integration suite checks the plan's Phase 0 guarantees against the live stack:

- every adapter is healthy;
- jobs run on the worker and emit OpenLineage events;
- a job can read only its own service account's secrets, and a rotated secret is used on the
  next run;
- the worker and API AppRoles cannot read service-account secrets;
- a tampered policy is refused;
- database logins are dynamic and expire;
- a scan of the database dump, container logs, lineage, audit and API responses finds no seeded
  secrets.

## Differences from the plan

- **MinIO image.** The upstream MinIO image is no longer published on Docker Hub, so compose uses
  the community build `pgsty/minio` (same server, same S3 API). Any S3-compatible store works
  through the `minio` adapter.
- **Debezium image.** It comes from Docker Hub (`debezium/connect:2.7.3.Final`). Kafka Connect is
  running and has its own AppRole. The Vault config provider for connector credentials arrives
  with CDC in Phase 1b.
- **DuckDB's `httpfs` extension** ships in the image as a pip wheel instead of being downloaded
  at runtime.
