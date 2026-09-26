# Data Platform

A laptop-first, cloud-swappable data platform with a management console.
The full design is in [docs/PLATFORM_PLAN.md](docs/PLATFORM_PLAN.md); this repo is built in the
phases listed there.

| Phase | Status |
|---|---|
| 0 — Foundations | ✅ done |
| 1 — Ingestion MVP | ✅ done |
| 1b — Near-real-time | ✅ done |
| 2 — Vault & Transform | ✅ done |
| 3 — Quality & Governance | next |
| 3b, 4, 5 | planned |

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

## Ingesting data (Phase 1)

1. **Admin → Service accounts:** create one, e.g. `etl`. It owns the credentials of the
   connections you give it.
2. **Connections:** add a source. Supported types:
   - files: local folder, SFTP, FTP/FTPS, SMB share, S3/MinIO;
   - databases: PostgreSQL, MySQL, SQL Server, Oracle;
   - MongoDB;
   - REST API: bearer, basic, API key or OAuth2 client credentials, with page, offset,
     cursor, next-URL or Link-header pagination;
   - Kafka topics.

   Passwords and keys go straight to Vault; **Save and test** checks the connection from the
   worker, running as the service account.
3. **Ingestion Jobs → New:** the wizard walks through:
   1. pick the connection;
   2. choose a table, SQL query, file template (e.g. `/in/orders_{yyyyMMdd}*.csv`),
      collection, endpoint or topic, and preview it;
   3. choose a load mode: full, incremental on a watermark column (new files only for file
      sources), or append;
   4. name the bronze dataset and set a schedule (cron or interval);
   5. save, or save and run.

Every run:
- writes a Delta table at `s3://bronze/<dataset>` with `_load_ts`, `_source`, `_batch_id` and
  `_file` audit columns;
- registers the table in the **Catalog**, where schema changes are tracked;
- profiles every column;
- records lineage for the **Lineage Explorer**, including the files it read and their SHA-256;
- publishes a run event on the `dataplat.runs` topic;
- shows up on the **Home** ops dashboard.

The **App Portal** lists reports and apps. Naming the datasets an app reads puts the app into
lineage.

### Near-real-time (Phase 1b)

In the wizard, two load modes run continuously on the stream worker:
- **Real-time replication (CDC)**, for PostgreSQL, MySQL, SQL Server and MongoDB connections;
- **Continuous stream**, for Kafka topics and webhook streams.

CDC uses Debezium on Kafka Connect: an initial snapshot, then every insert, update and delete,
in two write modes:
- **change log:** append-only history with `_op`, `_source_ts` and `_offset`;
- **mirror:** the current state, applied with Delta MERGE by primary key.

In the dev setup, changes reach bronze within a second or two.

How it works:
- **Credentials:** connector configs hold only `${vault:...}` placeholders. Kafka Connect
  resolves them with its own AppRole through a small Vault config provider
  (`infra/kafka-connect`).
- **No duplicates or losses:** each micro-batch closes at N records or T seconds. It is written
  in a single Delta commit that also records the Kafka offsets it covered (Delta app
  transactions). A crash or kill therefore never duplicates or loses changes, and the
  integration tests SIGKILL the stream worker mid-stream to prove it.
- **Operations:** the **Streams & Replication** page updates live over a WebSocket. It shows
  status, connector state, lag, end-to-end latency (p50/p95), throughput and dead letters,
  with pause, resume and re-snapshot controls. `GET /api/streams/{id}/metrics` exposes the
  same numbers.
- **Webhooks:** create a *Webhook* connection, issue its key (shown once; only the hash is
  stored), then `POST /ingest/events/<stream>` with an `X-API-Key` header.
- **Triggers:** *When files arrive* polls a file source and runs the job when new or changed
  files appear. Interval schedules go down to 10 seconds.
- **Freshness SLAs:** set one per dataset in the catalog. A breach opens an alert (shown on
  Home and published to `dataplat.alerts`), which resolves after the next load.
- **Clean-up:** streaming tables are compacted hourly. Deleting a Postgres CDC job drops its
  replication slot and publication in the source.

## Data Vault, models and lineage (Phase 2)

- **Add to Raw Vault.** In the wizard's last step, choose the business key column(s) (a hub),
  the descriptive columns (a satellite), and optionally relationships to other hubs (links)
  and a status satellite that records deletes. Every run, and every streaming micro-batch,
  then loads the vault. The **Data Vault** page designs hubs, links, satellites (including
  multi-active ones), PIT and bridge tables by hand, maps any bronze/silver dataset onto
  them, and shows the model as a diagram.
- **Loads are insert-only.** Hash keys are SHA-1 over trimmed, upper-cased business keys;
  hashdiffs detect changes. A satellite row is added only when its hashdiff differs from the
  previous row for that key: the one before it in the batch, or else the latest earlier row
  in the vault. Each load reads only rows newer than its high-water mark, holds a per-table
  lock, and can be re-run safely. Rows keep `load_date` (the source commit time for CDC),
  `record_source` and the source `_batch_id`.
- **Models** (Pipelines page) build silver and gold tables:
  - **SQL models** use `{{ source('bronze','orders') }}`, `{{ vault('sat_x') }}`,
    `{{ ref('model') }}`, and `{% if is_incremental() %}` with `{{ this }}`. A model is
    materialized as a table (rebuilt) or incrementally (merged on a unique key, or appended).
  - **SCD2 dimensions** are built from a satellite's history (business keys come from its
    hub), with `valid_from`/`valid_to`/`is_current` and stable surrogate keys. A status
    satellite closes deleted rows.
  - **Facts** look up each dimension's surrogate key as of the fact's event time, or `'-1'`
    when there is no match.
  - A **date dimension** is also available.
  - **Promote to silver** in the wizard keeps a silver copy without the bronze audit columns.
- **Pipelines** build their models in dependency order, on a schedule and/or whenever a
  dataset they watch gets new data. A failed model skips the models downstream of it. So
  a CDC change flows source → bronze → vault → gold on its own; the integration test sees
  it in the gold SCD2 dimension in a few seconds.
- **Serving DB.** Models marked *serve* are copied into the Postgres `serving` database
  (schemas `silver`/`gold`) after every build. Full rebuilds swap in a new copy atomically;
  incremental models upsert.
- **SQL runs in a sandbox.** Model and vault SQL runs in its own DuckDB. Inputs are handed
  over as Arrow datasets, then file, network and extension access is switched off and the
  configuration locked, so a model can't read files, secrets or buckets it wasn't given.
  Only a single `SELECT` is accepted.
- **Lineage Explorer:**
  - Column-level lineage comes from ingestion (source column → bronze), vault mappings, and
    SQL parsed with sqlglot (or the SCD2/fact builders).
  - **Trace** a column upstream ("how does this get its value?") or downstream; each step
    shows the job and its SQL.
  - **Impact analysis** lists the datasets, jobs and reports affected by a table or column,
    with CSV export.
  - **Time travel**: view the graph as of any past moment.
  - **Batch trace**: follow a `_batch_id` from the source files to every table holding its
    rows.
  - Live overlays mark failed jobs, late datasets and stream lag.

### Test sources

`make dev-up` (Windows: `tasks.ps1 dev-up`) starts sample sources next to the platform and
seeds them:

| Source | Host | Credentials |
|---|---|---|
| Postgres | `src-postgres` | `dev` / `devsource` |
| MySQL 8.0 | `src-mysql` | `dev` / `devsource` (CDC: `root` / `devsource`) |
| MongoDB (replica set `rs0`) | `src-mongo` | `dev` / `devsource` |
| SFTP | `src-sftp` | `dev` / `devsource` |
| FTP | `src-ftp` | `dev` / `devsource` |
| SMB | `src-smb` (share `landing`) | `dev` / `devsource` |
| S3 | `src-s3` | `devsource` / `devsource-secret` |
| REST API | `http://mock-api:8090` | see `samples/mock_api.py` |
| Kafka topic | `web.clickstream` | none |
| SQL Server (optional) | `src-mssql` | `sa` / `Dev-Source-2026`; add `MSSQL=1` / `-Mssql` (~2 GB RAM) |

These are throwaway local test systems, so their credentials are public.

The "Local folder" connection type reads `samples/landing` (or `DATAPLAT_LANDING`), which is
mounted read-only into the worker at `/data/landing`.

## Layout

```
config/platform.yaml   picks one adapter per port — swapping infrastructure is a config change
backend/dataplat/
  core/ports/          abstract interfaces (ObjectStore, QueryEngine, TableFormat, SecretStore, EventBus, ...)
  adapters/            local adapters: Vault, Postgres, MinIO, DuckDB, Delta Lake, Redpanda, Oxigraph, lineage
  connectors/          connector SDK + file, database, MongoDB, REST and Kafka connectors
  ingestion/           job spec, runner (source -> bronze Delta), worker handlers
  vault/               Data Vault 2.0 definitions, hashing, SQL generation, insert-only loader, PIT/bridge
  transform/           SQL templating, sandbox, SCD2/fact/date builders, model + pipeline runner, triggers
  catalog/, quality/   catalog registration + schema drift, column profiler
  lineage/             OpenLineage events, table + column lineage graph, trace, impact, batch trace
  api/                 FastAPI app (auth, admin, connections, ingestion, streams, vault, transform,
                       catalog, lineage, ops, portal)
  security/            passwords, login/refresh/lockout, service accounts
  orchestration/       job handlers, worker, scheduler, stream worker
  bootstrap/           Vault init/unseal/configure, per-service policies
  db/, alembic/        metadata schema and migrations
console/               React + TypeScript + Vite + Mantine console
infra/                 Vault server config, Postgres init SQL
scripts/               tasks.ps1 (Windows), e2e.sh (from-scratch validation)
samples/               seed script, REST mock API, local landing folder
docker-compose.dev.yml test sources
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
- **Connection secrets** live under the owning service account's Vault path
  (`kv/dataplat/service-accounts/<sa>/connections/<id>`), not `kv/dataplat/connections/<id>`.
  That way the account's policy covers them and no per-connection policy is needed.
- **Source tasks run on the worker.** "Test connection", table browsing and previews run as
  short worker jobs, because the API is not allowed to read connection secrets. A preview's
  rows are handed out once and then removed from the metadata database.
- **Ingestion writes each batch in a single Delta commit, built in memory.** That is fine for
  laptop-sized loads. Streaming very large sources in chunks comes with Phase 1b.
- **Debezium 2.7 (the last version on Docker Hub) doesn't support MySQL 8.4**, because 8.4
  removed `SHOW MASTER STATUS`. The dev MySQL is 8.0; MySQL 8.4 CDC needs Debezium 3.x.
- **Continuous streams from Kafka clusters that need credentials** aren't supported yet. The
  stream worker has no per-service-account token flow; use scheduled append loads for those.
  CDC (via Kafka Connect), webhooks and credential-free topics stream continuously.
- **Webhook keys** are stored only as SHA-256 hashes, not in Vault, since there's nothing to
  read back. A lost key is replaced by issuing a new one.
- **No Python models.** The plan mentions SQL/Python models. Only SQL models (and the
  declarative builders) are offered, because running user Python on the worker would let a
  model read the worker's Vault credentials. Python models need a separate, credential-free
  sandbox and are deferred.
- **SCD2 dimensions and PIT/bridge tables are rebuilt in full** each run. They're derived
  from the complete satellite history, so this is always correct, and fast at laptop scale.
  Satellites, hubs, links and incremental models do load only new data.
- **Multi-active satellites** detect changes per (key, multi-active key) row, not per whole
  set of rows for a key.
- **Surrogate keys** are MD5 hashes of the business key and `valid_from` (text), not
  integers, so they stay stable when a dimension is rebuilt.
- **Lineage access control** (hiding nodes a user may not see) arrives with the Phase 3
  permission model. Today every signed-in user sees the whole graph.
- **Oracle** is implemented but not covered by the test suite, since there's no free Oracle
  image in the dev profile. Every other connector is tested against a real server.
