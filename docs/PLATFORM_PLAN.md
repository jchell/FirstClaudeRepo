# Plan: Laptop-first, cloud-swappable Data Platform + Management Console

## Context
The user wants a full data platform that runs on a Windows laptop today but can grow into the cloud. Every infrastructure piece (storage, query engine, event bus, scheduler, secrets, identity) sits behind an API, so it can be swapped out later. What it needs to do: ingest from files, APIs and events. Store data in a medallion layout (bronze/silver/gold) with an optional Data Vault 2.0 raw vault. Let users build simple ETL jobs by picking options instead of writing code. Profile data automatically. Run steward-defined data-quality rules on a schedule. Govern data with a catalog, classification, tags, access control and service accounts. Model a dimensional consumption layer, support near-real-time loads, and deliver data as extracts, generated REST APIs, direct connections and later data products. A central console shows operations and links to business apps. Later requirements added: end-to-end lineage viewable in the console, near-real-time ingestion and replication, all secrets held in a vault, and ontology-based semantic processing of ingested data.

The repo (`C:\Users\johnh\repos\FirstClaudeRepo`) is empty apart from `.vscode/settings.json`, so this is a new build.

**Decisions made:** Python 3.12 + FastAPI backend, React + TypeScript console, Docker Compose runtime, DuckDB over Parquet/Delta for the lakehouse, and a phased build that gets a working MVP first.

## Architecture: ports & adapters
- Package `dataplat/core/ports/` defines the abstract interfaces (Python `Protocol`s/ABCs). Each one has a local adapter and room for cloud adapters.
- A single `config/platform.yaml` picks the adapter for each port, and `dataplat/core/registry.py` loads them via entry points, so a swap means changing config, not code.

| Port | Local adapter (now) | Future swaps |
|---|---|---|
| `ObjectStore` | MinIO (S3 API) via `fsspec`/`s3fs` | S3, ADLS, GCS |
| `QueryEngine` | DuckDB (+ `delta-rs`, `httpfs`) | Snowflake, Databricks, BigQuery, Trino |
| `TableFormat` | Delta Lake (`deltalake`) | Iceberg (`pyiceberg`) |
| `MetadataStore` | PostgreSQL (SQLAlchemy 2 + Alembic) | RDS/Azure PG, Unity/Glue catalog sync |
| `EventBus` | Redpanda (Kafka API, `confluent-kafka`) | MSK, Event Hubs, Confluent, Pub/Sub |
| `Orchestrator`/`JobQueue` | APScheduler + Postgres `SKIP LOCKED` queue + worker processes | Airflow, Dagster, Step Functions |
| `SecretStore` | **HashiCorp Vault** container (OpenBao works as a drop-in open-source alternative), KV v2 + database + transit engines | HCP Vault, AWS Secrets Manager/KMS, Azure Key Vault, GCP Secret Manager |
| `IdentityProvider` | Built-in users/groups + JWT; optional Keycloak (OIDC) profile | Entra ID, Okta, Cognito |
| `ServingStore` | Postgres `serving` DB (gold replicas for direct SQL/BI) | Snowflake/Synapse/Redshift |
| `ChangeCapture` | Debezium on Kafka Connect (Redpanda Connect-compatible) | AWS DMS, Azure Data Factory CDC, Fivetran/Qlik, Confluent managed connectors |
| `StreamProcessor` | In-house Python micro-batch consumers (Arrow + `deltalake`) | Spark Structured Streaming, Flink, Databricks DLT, Kinesis Analytics |
| `KnowledgeGraphStore` | Oxigraph server container (RDF triple store, SPARQL 1.1); `pyoxigraph` embedded for tests | Amazon Neptune, GraphDB, Stardog, Apache Jena Fuseki, Neo4j (via n10s) |
| `EntityResolver` | Splink on DuckDB | Splink on Spark/Athena, Senzing, AWS Entity Resolution |
| `LineageSink` | Built-in lineage store in Postgres (OpenLineage event model) | Marquez, DataHub, OpenMetadata, Microsoft Purview, Unity Catalog |

## Repository layout
```
docker-compose.yml, docker-compose.dev.yml (test sources: sftp, ftp, samba, mysql, mongo), .env.example, Makefile/tasks.ps1
config/platform.yaml
backend/  (pyproject.toml, uv)
  dataplat/
    core/        ports/, registry.py, config.py, models (pydantic), audit, logging
    adapters/    minio_store.py, duckdb_engine.py, delta_format.py, pg_metadata.py, redpanda_bus.py, pg_queue.py, vault_secrets.py (hvac), jwt_identity.py, pg_serving.py
    connectors/  base.py (Connector SDK), registry, files/ (local, smb, ftp, sftp, s3/adls/gcs), rdbms/ (SQLAlchemy: postgres, mysql, mssql, oracle), nosql/ (mongodb, cassandra*, dynamodb*), lake/ (delta, iceberg*, snowflake*, databricks*), api/ (REST: auth, pagination, JSONPath), events/ (kafka, webhook, mqtt*)
    ingestion/   job spec, runner, load modes (full, incremental-watermark, append), file templates (glob + date tokens), bronze writer w/ audit cols (_load_ts, _source, _batch_id, _file)
    vault/       DV2 metadata (hubs, links, sats, multi-active sats), hash key/hashdiff (SHA-1/MD5, normalized business keys), SQL generator + loader (insert-only), PIT & bridge builders (business vault)
    transform/   SQL/Python model pipelines (dbt-like: ref(), DAG), layer promotion bronze→silver→gold, dimensional builders (SCD1/SCD2 dims, surrogate keys, facts, date dim)
    quality/     profiler (runs after every ingestion), rule engine (not_null, unique, range, regex, allowed_values, referential, freshness, row_count, custom SQL), DQ dimensions, scores, trend/degradation detection + alerts
    ontology/    ontology registry (OWL/RDFS/SKOS import+authoring, versioning), semantic mappings (dataset/column → class/property, RML/R2RML-style), semantic annotation suggester, KG materializer (batch + streaming), reasoner (owlrl), SHACL validation (pyshacl), entity resolution, SPARQL/GraphQL query services. See "Ontology & semantic processing"
    lineage/     OpenLineage-compatible emitter + store, graph API (upstream/downstream, impact analysis), column-level parser (sqlglot), see "End-to-end lineage"
    catalog/     datasets, columns, glossary, tags, classification (auto PII detection from profile patterns + manual), search
    security/    RBAC (roles on domains/datasets), tag-based policies (column masking/row filters applied in QueryEngine layer), service accounts (credentials only in Vault, referenced from connections), API keys (hashed; the secret itself is kept in Vault), audit log. See "Secrets management"
    delivery/    extracts (CSV/JSON/Parquet/XLSX to any ObjectStore/file connector, scheduled), generated REST APIs (dynamic FastAPI routes per gold dataset: filter/sort/page, API key auth, OpenAPI), serving sync (gold → Postgres read-only roles for direct JDBC/ODBC), data products (phase 5)
    streaming/   CDC connector management (Debezium via Kafka Connect REST), stream consumers → micro-batch Delta MERGE/append, incremental vault + model propagation, serving replication, latency/lag metrics (see "Near-real-time")
    orchestration/ scheduler service, worker, run/state model, retries, dependencies, event triggers
    api/         FastAPI app + routers per domain; WebSocket for live run status
  alembic/, tests/ (unit, integration w/ testcontainers or compose)
console/  React + TS + Vite, TanStack Query, Mantine or MUI, React Flow (lineage/vault diagrams), ECharts (DQ trends)
  pages: Home/Ops dashboard, Connections, Ingestion Jobs (wizard), Streams & Replication (live lag/throughput), Pipelines, Data Vault designer, Catalog/Search, Lineage Explorer, Ontology Studio (model editor, mappings, knowledge-graph explorer, SPARQL workbench, entity resolution review), Data Quality (profiles, rules, scorecards), Governance (tags, classifications, policies), Delivery (extracts, APIs, serving), App Portal (links to reports/dashboards/apps), Admin (users, groups, roles, service accounts, secrets)
samples/  demo source data + seed script
```
(* = later phase)

## Key flows
1. **Simple ETL wizard.** Pick a connection. The UI then asks for the right thing for that source type: a table picker or SQL query for queryable sources, a path + file template (e.g. `/in/orders_{yyyyMMdd}*.csv`) with format options for file stores, or an endpoint + pagination for APIs. Then set the load mode, target bronze dataset, schedule (cron/interval/event), an optional **"Add to Raw Vault"** mapping (choose business keys → hub, relationships → link, descriptive columns → satellite), and optional auto-promotion to silver. Saving creates a `JobSpec`, which is versioned in metadata.
2. **Run.** Scheduler → queue → worker: connector `read()` → Arrow batches → Delta bronze → register/refresh in the catalog (schema, schema-drift detection) → auto-profile → optional vault load → trigger downstream models → emit lineage + run metrics → events on the bus.
3. **Steward DQ.** Stewards define rules on catalog datasets/columns, group them into scorecards, and schedule them. Results are stored as time series. The console shows trends and flags degradation (e.g. a score drops more than X% against its rolling baseline) and sends alerts (email/webhook).
4. **Delivery from gold.** Extract jobs, one-click "Publish as API", or "Publish to serving DB". Every one runs under access policies and records who accessed what (audit).

## End-to-end lineage (source → report)
Every step that moves data emits an **OpenLineage** `RunEvent` (job, run, input/output datasets, facets) to the `LineageSink` port. Because OpenLineage is a standard, the same events can later go to Marquez, DataHub or Purview without code changes.

- **Graph nodes** (all catalog entities with stable URNs):
  - source system → source object (DB table/query, file path/template, API endpoint, Kafka topic)
  - ingestion job → bronze dataset
  - vault hub/link/sat → silver/gold models → dimensions/facts
  - delivery outputs (extract file, generated API, serving table, data product)
  - **consuming reports/dashboards/apps**, registered in the App Portal
- **Where each step's lineage comes from:**
  - **Ingestion:** the connector records the exact source object, the query text and the file names actually read (with checksums), mapped column by column to bronze.
  - **Vault loads:** business-key → hub hash key, and column → satellite attribute, taken from the vault mapping metadata.
  - **SQL/Python transforms:** SQL models are parsed with `sqlglot` for column-level lineage. Python models declare their inputs and outputs.
  - **Delivery:** each extract, API route and serving-sync job emits its gold input → output.
  - **Reports:** a portal entry for a report or dashboard declares which gold/serving datasets and columns it uses. Where the serving DB can be queried for this, `pg_stat_statements` usage is also harvested to suggest those links. Adapters for Power BI, Tableau and Superset metadata APIs come later.
- **Run-level lineage:** each record carries `_batch_id` / `_source` / `_file` audit columns through bronze and the vault (`record_source`, `load_date`). This lets a user trace a gold row back to the batch and file it came from.
- **Lineage in the console.** A top-level **Lineage Explorer** page (React Flow + ELK auto-layout), also embedded as a "Lineage" tab on every catalog dataset, job, pipeline, delivery and App Portal report page:
  - **Graph view.** Pick any node (source, job, dataset, column, report) and expand upstream/downstream step by step or fully. Toggle between table and column level. Filter by layer (source/bronze/vault/silver/gold/delivery/report) and by domain/tag. Nodes are color-coded by layer.
  - **Node details panel.** Schema, owner, classification tags, last run status, freshness/latency, DQ score, and the transformation logic behind the edge (SQL, vault mapping, connector query). Links jump to the related job, run or catalog page.
  - **Path trace.** "How does this report column get its value?" highlights the full path back to the source column(s) and shows each transformation along the way.
  - **Impact analysis.** "Which pipelines, APIs, extracts and reports are affected if this source column/table changes or fails?" Results export to CSV.
  - **Run/time travel.** View lineage as of a past run or date, and trace a specific `_batch_id` from gold back to the source files/queries.
  - **Live overlays.** Failed or late nodes and streaming lag show in real time over WebSocket. The Ops dashboard's "failed job" alerts link directly to the downstream-impact view.
  - Access-controlled: users only see nodes they have catalog permission for, and restricted nodes are shown as masked placeholders.
- **Phasing:**
  - Phase 1 emits table-level lineage for ingestion and ships a basic Lineage Explorer (table level).
  - Phase 2 adds vault, transform, streaming and column-level lineage plus the full explorer features.
  - Phase 4 adds delivery and report linkage, and impact analysis.
  - Phase 5 adds external lineage adapters and BI tool harvesters.

## Ontology & semantic processing
Goal: ingested data is described in shared business concepts (Customer, Product, Order, Supplier…) instead of source-specific column names. It can be linked across sources, validated against the business model, reasoned over, and queried as a knowledge graph.

- **Ontology registry.**
  - **Import** standard ontologies such as schema.org, FIBO, FHIR RDF, GS1 or your own OWL/Turtle files. You can also **author** classes, properties, relationships and constraints in Ontology Studio, which has a form-and-graph editor, so nobody has to write OWL by hand.
  - Ontologies are versioned with change history, and a change is published only after steward approval.
  - The business glossary is stored as SKOS concepts linked to the ontology, so glossary terms, tags and ontology classes are a single model.
- **Semantic mapping of ingested data.**
  - The ingestion wizard gets an optional **"Map to ontology"** step, and existing catalog datasets can be mapped later.
  - A dataset maps to a class, columns map to data properties, and foreign keys or join columns map to object properties (relationships). An IRI template is built from the business keys.
  - **Automatic suggestions** come from matching column names, profile patterns, classification tags and existing mappings (fuzzy and embedding similarity). A pluggable `SemanticSuggester` port can add an optional LLM-backed matcher later. Stewards accept or reject each suggestion.
  - Mappings are stored as RML-style metadata and exported as standard RML/R2RML.
- **Alignment with Data Vault 2.0.** Hubs line up with ontology classes (business keys ↔ identifying properties), links with object properties, and satellites with data properties.
  - The vault designer can **generate a hub/link/sat design from the ontology**, or suggest ontology mappings from an existing vault. That keeps the raw vault, silver models and knowledge graph on one business model.
- **Knowledge graph materialization.**
  - Mapped silver/gold (or business vault) data is converted to RDF and loaded into the `KnowledgeGraphStore`.
  - Batch mode loads after pipeline runs. Near-real-time mode applies incremental triple updates from CDC/stream micro-batches.
  - Named graphs per source/batch give provenance (the `_batch_id` link back to lineage).
- **Entity resolution.**
  - Splink (probabilistic record linkage on DuckDB) matches the same real-world entity across sources, for example the same customer in the CRM, ERP and the SFTP files.
  - Match rules are configured per class. Low-confidence matches go to a steward review queue in the console.
  - Resolved entities get `owl:sameAs` links in the graph and a golden-record table in gold, which can feed dimensions such as `dim_customer`.
- **Reasoning & semantic validation.**
  - The OWL-RL/RDFS reasoner (`owlrl`) infers class membership, inverse and transitive relationships and hierarchies. For example, a `PremiumCustomer` class can be defined by a rule.
  - Inferred facts are materialized into a separate named graph, so they can be told apart from the source data.
  - **SHACL shapes** generated from the ontology (cardinality, datatypes, value ranges, required relationships) run as a **new DQ rule type**. Their results feed the same DQ scorecards and degradation trends as the steward rules.
- **Semantic consumption.**
  - A SPARQL endpoint (read-only, RBAC-filtered) and a SPARQL workbench in the console.
  - A **generated GraphQL API** over ontology classes and relationships, alongside the generated REST APIs.
  - Semantic search in the catalog: "find all datasets containing Customer email" regardless of what a source calls the column.
  - Ontology-aware extracts (JSON-LD, Turtle, CSV by class).
  - Data products (mesh) describe their output ports using ontology terms.
- **Governance & lineage integration.**
  - Classification tags can be set on ontology properties and inherited by every mapped column. For example, tag `schema:email` as PII once and every mapped email column is masked automatically.
  - Lineage adds a **semantic view** (source column → bronze → vault → ontology property → report) and a "where is this concept stored?" query.
- **Console: Ontology Studio.**
  - Class/relationship graph editor, mapping workbench with suggestions, and a knowledge-graph explorer to browse entities and their links.
  - Entity-resolution review queue, SPARQL workbench, and SHACL validation results.

## Secrets management (Vault)
**Rule:** passwords, keys, tokens, connection strings with credentials, certificates and other secrets are stored **only in Vault**. They never go in Postgres metadata, `platform.yaml`, job specs, Delta tables, logs, lineage events, the Git repo or the browser.

- **Secret references, not values.** Connections, service accounts, delivery targets and CDC connectors store a reference such as `vault://kv/dataplat/connections/<id>#password`.
  - The `SecretStore` port resolves the reference at runtime, inside the worker/API process, only for the moment it is needed.
  - Pydantic models use `SecretStr`, and the API schema makes secret fields write-only. A test fails the build if a secret field is ever serialized.
- **Vault engines used:**
  - **KV v2:** static secrets such as FTP/SFTP passwords and keys, API tokens, SMB creds and cloud keys. KV v2 keeps versions, which gives rotation history.
  - **Database secrets engine:** short-lived, auto-expiring **dynamic credentials**. Used where supported for the platform's own Postgres (metadata/serving) and for source databases.
    - Serving-DB consumer logins for direct connections are also issued dynamically, per user or app, with a time-to-live.
  - **Transit engine:** encryption keys for field-level encryption/tokenization of sensitive columns (driven by classification tags), and for signing JWTs and generated-API keys.
  - **PKI** (later): TLS certs for internal services.
- **How platform services authenticate to Vault.** Each service (api, worker, stream-worker, scheduler, kafka-connect) has its own **AppRole** with a least-privilege policy. For example, only workers can read connection secrets, and the console/API can write secrets but cannot read them back.
  - Cloud swap: Kubernetes, AWS IAM or Azure MSI auth methods.
- **Service accounts.** A platform service account maps to a Vault path plus a policy. Jobs run as a service account, and the worker fetches only that account's secrets.
- **Kafka Connect/Debezium.** Uses a Vault config provider, so connector configs contain only `${vault:...}` placeholders and never plaintext DB passwords.
- **Bootstrap on the laptop.**
  - Vault runs in server mode with integrated raft storage (not dev mode), so data persists across restarts.
  - `scripts/vault-init.ps1` initializes Vault, enables the engines, and creates the policies and AppRoles.
  - It writes the unseal key and root token to a user-profile location **outside the repo** (`%USERPROFILE%\.dataplat\vault-init.json`, protected with Windows DPAPI), then revokes the root token after setup.
  - A `vault-unseal` helper runs at `compose up`. Cloud deployments use KMS auto-unseal.
  - `.env` holds only non-secret settings and the AppRole role IDs. Secret IDs are delivered to containers through file mounts. `.gitignore` plus a pre-commit secret scanner (`gitleaks`) guard the repo.
- **Rotation & audit.**
  - Rotating a secret from the console writes a new KV version, and dependent connections pick it up on their next run.
  - Dynamic creds auto-expire.
  - The Vault audit device is enabled, and its log is surfaced in the console's audit view, showing who accessed which secret, without values.
  - Log redaction filters mask anything that looks like a secret.
- **Console (Admin → Secrets).** Create/rotate/delete secrets and see metadata only: path, owner, last rotated, which connections use it. Values are write-only and are never displayed after entry. Access is RBAC-restricted.

## Near-real-time ingestion & replication
Target: changes in a source appear in bronze/raw vault within **seconds**, and in gold/serving within **under a minute** on a laptop. Latency is configurable per flow.

- **Ways data comes in:**
  - **Log-based CDC (RDBMS/NoSQL).** In the wizard, choose load mode **"Real-time replication (CDC)"** on a Postgres, MySQL, SQL Server, Oracle or MongoDB connection and pick the tables/collections. The platform configures a Debezium connector through the Kafka Connect REST API, using service-account credentials pulled from the SecretStore. It takes an initial snapshot, then streams changes into Redpanda topics (`cdc.<source>.<schema>.<table>`), with schemas tracked in the Redpanda schema registry.
  - **Event sources.** Kafka topics, HTTP webhook ingest endpoint (`POST /ingest/events/{stream}`), MQTT (later).
  - **Files and APIs.** A file-arrival watcher (SFTP/FTP/SMB/object-store polling and MinIO bucket notifications) and short-interval API polling (seconds) give near-real-time for sources that don't stream.
- **Stream processing (`StreamProcessor` port):** the worker runs long-lived consumer groups that:
  1. Micro-batch on whichever comes first, N records or T seconds (default 5s).
  2. Write to bronze, either as an append-only change log with `_op`, `_source_ts`, `_lsn/_offset` audit columns, or as a Delta `MERGE` for a current-state mirror. Both are selectable.
  3. Load the raw vault incrementally: hub/link inserts and satellite hashdiff comparison, keeping the insert-only DV2 rules. Deletes are recorded in effectivity/status-tracking satellites.
  4. Trigger **incremental** downstream models: silver/gold models declared `materialized: incremental` or `streaming` process only the new changes. SCD2 dimensions and facts update continuously.
  - Delivery is exactly-once or idempotent: Kafka offsets commit only after the Delta write commits, and a batch ID is recorded so replays are safe.
- **Near-real-time replication to consumers:**
  - **Serving DB replication.** Gold (and chosen silver) tables replicate continuously into the Postgres serving DB via upserts, so BI tools and direct connections see fresh data.
  - **Outbound change streams.** Gold changes can be published to outbound Kafka topics for downstream apps and data-mesh consumers.
  - **Generated APIs** read the freshest data and can optionally expose an SSE/WebSocket "subscribe to changes" endpoint.
  - **Lake-to-lake replication.** Mirror datasets to another object store/lake (e.g. the future cloud S3/ADLS) for hybrid migration.
- **Ops & governance:**
  - Per-stream dashboards on the **Streams & Replication** page show end-to-end latency (source commit → bronze → gold → serving), consumer lag, throughput, errors, dead-letter queue contents, pause/resume/re-snapshot controls, and schema-change events.
  - Freshness SLAs per dataset alert when breached.
  - DQ rules can run in streaming mode on each micro-batch (row-level validity checks routing bad records to quarantine), alongside scheduled aggregate rules.
  - Streaming flows emit lineage like batch flows do.
- **Compose additions:**
  - `kafka-connect` (Debezium image) against Redpanda.
  - Source DBs in the dev profile configured for CDC: Postgres `wal_level=logical`, MySQL binlog, SQL Server CDC enabled.
- **Laptop tuning.** Stream workers are separate processes with a `max_streams` setting. Delta tables are compacted/optimized on a schedule to deal with the small files that micro-batching creates.

## Phases
- **Phase 0: Foundations.** `git init`, compose stack (postgres, minio, redpanda + console + schema registry, kafka-connect/Debezium, **vault**, oxigraph, api, worker, stream-worker, scheduler, console-ui), Vault init/policies/AppRoles + secret-reference plumbing before any connector code, ports + local adapters, config registry, Alembic metadata schema, auth (users/roles/JWT signed via Vault transit), service accounts bound to Vault policies, console shell with login and nav.
- **Phase 1: Ingestion MVP.** Connector SDK + connectors: local/SMB/FTP/SFTP files, Postgres/MySQL/SQL Server, MongoDB, REST API, Kafka. ETL wizard, scheduler, bronze Delta, catalog registration, auto-profiling, ops dashboard (runs, failures, durations, volumes), app portal links.
- **Phase 1b: Near-real-time.** CDC replication mode in the wizard (Postgres, MySQL, SQL Server, MongoDB via Debezium), Kafka/webhook streams, file-arrival triggers, micro-batch bronze writer, Streams & Replication console page with lag/latency, freshness SLAs.
- **Phase 2: Vault & Transform.** DV2 designer + loader wired into ingestion ("add to raw vault", batch and streaming), SQL model pipelines through silver/gold with incremental/streaming materializations, business vault PIT/bridge, dimensional builders (SCD2), continuous serving DB replication, full Lineage Explorer.
- **Phase 3: Quality & Governance.** Steward rule engine, scorecards, scheduling, degradation alerts; classification/tagging with auto-PII suggestions, glossary, tag-based masking/row filters, audit log.
- **Phase 3b: Ontology & Knowledge Graph.** Ontology registry/import/editor, SKOS glossary unification, mapping step in the wizard with auto-suggestions, KG materialization (batch then streaming), OWL-RL reasoning, SHACL → DQ rules, Splink entity resolution + review queue + golden records, SPARQL endpoint, ontology-driven vault generation, semantic lineage view.
- **Phase 4: Delivery.** (includes the generated GraphQL API over the ontology, and JSON-LD/Turtle extracts) Scheduled extracts, generated REST APIs, serving DB sync with read-only roles, delivery catalog entries.
- **Phase 5: Scale-out & mesh.** Outbound change streams, change-subscription APIs, lake-to-lake replication, Oracle CDC, data products (owner, domain, contract, SLA, output ports), extra connectors (Iceberg, Snowflake, Databricks, Cassandra, ADLS/GCS, MQTT), Keycloak/OIDC, first cloud adapter example (S3 + Snowflake) to prove swappability.

## Verification
- **Unit tests** (pytest) for each port adapter against a shared contract test suite (`tests/contracts/`), so every future cloud adapter must pass the same tests. Also unit tests for the hash keys/hashdiff, the SCD2 builder, the DQ rules and the SQL lineage parsing.
- **Integration tests** (`pytest -m integration`) run against `docker compose -f docker-compose.yml -f docker-compose.dev.yml up`, which includes the test SFTP/FTP/Samba/MySQL/Mongo sources.
- **End-to-end demo script** (`samples/demo.ps1`):
  1. Seed the sources.
  2. Create a Postgres table job and an SFTP file-template job through the API (the same calls the console makes).
  3. Run them.
  4. Assert the bronze Delta tables exist, the profiles are stored, and the hub/sat rows are loaded.
  5. Assert the gold SCD2 dimension is built.
  6. Run a DQ rule to produce a score.
  7. Assert a CSV extract is written to MinIO.
  8. Assert the generated `GET /data/gold/customers` returns rows with the API key.
  9. Register a portal report on `gold.dim_customer`, then query `GET /lineage/{report}/upstream?level=column`. Assert the path runs through serving → gold dim → silver → sat/hub → bronze → the source Postgres column and SFTP file. Assert impact analysis on a source column lists that report.
  10. Assert a masked PII column is hidden for a restricted role.
  11. **NRT test:** create a CDC replication flow on the seeded Postgres `customers` table, then INSERT/UPDATE/DELETE rows in the source. Poll and assert:
      - the changes show up in bronze within 10s,
      - the hub/sat and gold `dim_customer` SCD2 rows update within 60s,
      - the serving DB row reflects the update,
      - `GET /streams/{id}/metrics` reports lag and latency figures.
      Then kill the stream worker mid-batch and restart it. Assert there are no duplicates and no lost changes.
- **Ontology test:**
  1. Import a small sample ontology (Customer, Order, Product, `placedBy`, `contains`).
  2. Accept the auto-suggested mappings for the Postgres customers and SFTP orders datasets and run the pipelines.
  3. Assert that a SPARQL query for "orders placed by customers in state X" returns the expected results, and that the same query through the generated GraphQL API matches.
  4. Assert that a duplicated customer seeded across two sources resolves to one golden record with `owl:sameAs`.
  5. Assert that an inferred class membership appears in the inferred graph.
  6. Assert that a seeded SHACL violation (an order with no customer) lowers the DQ score.
  7. Assert that tagging `email` as PII on the ontology masks the mapped column for a restricted role.
  8. Assert that a CDC update to a customer is reflected in the knowledge graph within 60s.
- **Secrets test:**
  - After the demo runs, grep the Postgres metadata dump, Delta files, container logs, lineage events and API responses for the seeded source passwords. Assert zero matches.
  - Assert that a connection GET returns only `vault://` references.
  - Rotate the SFTP password in Vault and assert the next run succeeds with the new version.
  - Assert the worker's AppRole cannot read another service account's path, and that dynamic serving-DB creds expire after their time-to-live.
  - `gitleaks` passes on the repo.
- **Console:** Playwright smoke tests of the wizard, the Lineage Explorer (open a report → expand upstream → trace a column back to the source) and the Streams page (live lag updates). Then a manual walkthrough at `http://localhost:3000`.
- **Prerequisite on the laptop:** Docker Desktop (WSL2 backend), about 10–12 GB RAM for the full stack including Kafka Connect. A `lite` compose profile leaves out CDC for smaller machines.
