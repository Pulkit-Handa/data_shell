# Data Shell — Configuration Reference

**Version:** 1.0  
**Date:** 2026-10-19  
**Scope:** All environment variables, CLI options, API endpoints, and configuration parameters

This document is the **authoritative source** for all Data Shell configuration. For operational procedures, see `OPERATIONS_RUNBOOK.md`. For release gates, see `RELEASE_CHECKLIST.md`.

---

## 1. Environment Variables

### 1.1 Core Configuration

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `development` | Environment mode. Production values: `production`, `prod`, `live`. Development values: `development`, `dev`, `local`. |
| `LOG_LEVEL` | `INFO` | Logging verbosity. |
| `DATASHELL_STORAGE_ROOT` | `.` | Root directory for all persistent storage. All other paths derive from this. |
| `DATASHELL_DATA_DIR` | `<storage_root>/data` | Directory for uploaded source files. |
| `DATASHELL_CONTRACTS_DIR` | `<storage_root>/contracts_registry` | Directory for contract registry files. |
| `DATASHELL_OUTPUT_DIR` | `<storage_root>/output` | Root directory for all output artifacts. |
| `DATASHELL_AUDIT_DIR` | `<storage_root>/output/audits` | Directory for audit certificates. |
| `DATASHELL_QUARANTINE_DIR` | `<storage_root>/output/quarantine` | Root directory for quarantine artifacts. |
| `DATASHELL_TMP_DIR` | System temp | Scratch space for spooled uploads and DuckDB spills. Point at durable storage volume. |
| `TMPDIR` | — | Fallback for `DATASHELL_TMP_DIR` if not set. |

### 1.2 Security Credentials

| Variable | Default | Description |
|---|---|---|
| `DATASHELL_API_KEY` | Ephemeral (dev) / **Required** (prod) | Master API key. Must be >= 32 characters with high entropy in production. |
| `DATASHELL_HMAC_SECRET` | Ephemeral (dev) / **Required** (prod) | HMAC signing secret. Must be >= 32 characters with high entropy in production. Must be distinct from `DATASHELL_API_KEY`. |
| `DATASHELL_KEY_ID` | `datashell-hmac-v1` | Active HMAC signing key ID. Must match `^[A-Za-z0-9._-]{1,64}$`. |
| `DATASHELL_HMAC_KEYRING` | `{}` | JSON object mapping key IDs to secrets for key rotation. |
| `DATASHELL_ED25519_PRIVATE_KEY` | — | Ed25519 private key (PEM, base64/hex DER, or raw 32-byte seed). Enables asymmetric certificate signing. |
| `DATASHELL_ED25519_KEY_ID` | `datashell-ed25519-v1` | Active Ed25519 key ID. Must match `^[A-Za-z0-9._-]{1,64}$`. |
| `DATASHELL_ED25519_KEYRING` | `{}` | JSON object mapping key IDs to public keys for key rotation. |
| `DATASHELL_API_KEYS` | — | JSON array of `{key, tenant_id, role, description}` objects. Production keys must be >= 32 characters. |
| `AUTH_ENABLED` | `true` | Enable API authentication. **Cannot be `false` in production.** |

### 1.3 CORS and Proxy

| Variable | Default | Description |
|---|---|---|
| `ALLOWED_ORIGINS` | `*` (dev) / **Required** (prod) | Comma-separated HTTPS origins. No wildcards, paths, queries, or fragments in production. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Comma-separated IPs/CIDRs of trusted proxies. **Cannot use `*` in production.** |

### 1.4 Resource Limits

| Variable | Default | Description |
|---|---|---|
| `MAX_UPLOAD_SIZE_BYTES` | `21474836480` (20 GiB) | Maximum upload file size in bytes. |
| `MAX_REQUEST_BODY_BYTES` | `MAX_UPLOAD_SIZE_BYTES + 1048576` | Maximum request body size (multipart overhead included). |
| `MAX_CONTROL_BODY_BYTES` | `1048576` (1 MiB) | Maximum body size for control-plane endpoints (contracts, admin). |
| `MAX_CONCURRENT_RUNS` | `2` | Maximum concurrent pipeline executions. |
| `MAX_CONCURRENT_REQUESTS` | `2` | Maximum concurrent HTTP requests being processed. |
| `MIN_FREE_DISK_BYTES` | `1073741824` (1 GiB) | Minimum free disk space for readiness. |
| `DATASHELL_DISK_HEADROOM_MULTIPLIER` | `2.0` | Multiplier for per-run free-space preflight. |
| `DUCKDB_MEMORY_LIMIT` | `1GB` | Memory limit for DuckDB. Must be a positive size (e.g., `512MB`, `1GB`). |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `120` | Token bucket rate limit per API key. |
| `STREAM_CHUNK_ROWS` | `100000` | Rows per micro-batch in streaming mode. Must be >= 1. |
| `STREAM_AUTO_THRESHOLD_BYTES` | `67108864` (64 MiB) | Files at or above this size use streaming automatically. |

### 1.5 Drift Configuration

| Variable | Default | Description |
|---|---|---|
| `DRIFT_ENABLED` | `false` | Enable historical drift evaluation. |
| `DRIFT_BASELINE_RESET` | `false` | Clear drift baseline before evaluation (one-shot use). |
| `DRIFT_BASELINE_WINDOW` | `1` | Number of prior delivered runs for rolling baseline. |
| `DRIFT_MAX_VOLUME_DROP_PCT` | `30.0` | Maximum delivered-row drop percentage before drift fails. |
| `DRIFT_MAX_METRIC_DRIFT_PCT` | `50.0` | Maximum metric drift percentage before drift fails. |
| `DRIFT_MAX_RATE_ABS_DELTA` | `0.10` | Maximum absolute delta for null/rate metrics (proportion points). |
| `DRIFT_METRIC_MIN_BASELINE_SAMPLES` | `1` | Minimum baseline samples before metric drift is evaluated. |

### 1.6 Webhook Configuration

| Variable | Default | Description |
|---|---|---|
| `DATASHELL_RUN_WEBHOOK_URL` | — | HTTPS URL for signed `run.completed` events. Required HTTPS in production. |
| `DATASHELL_WEBHOOK_TIMEOUT_SECONDS` | `5` | Timeout for webhook delivery. Must be > 0. |

### 1.7 Billing Configuration

| Variable | Default | Description |
|---|---|---|
| `DATASHELL_BILLING_PROVIDER` | `dry-run` | Billing provider: `dry-run` or `stripe`. |
| `DATASHELL_BILLING_STRIPE_SECRET_KEY` | — | Stripe secret key. Required for `stripe` provider. |
| `DATASHELL_BILLING_STRIPE_WEBHOOK_SECRET` | — | Stripe webhook signing secret. |
| `DATASHELL_BILLING_STRIPE_CUSTOMER_ID` | — | Stripe customer ID. |
| `DATASHELL_BILLING_STRIPE_TEST_MODE` | `true` | Stripe test mode. **Cannot be `true` in production.** |

### 1.8 History Retention

| Variable | Default | Description |
|---|---|---|
| `DATASHELL_HISTORY_RETENTION_DAYS` | `0` | Days to retain ledger history. `0` = keep forever. Pruned at startup. |

### 1.9 Demo-Specific (web/main.py only)

| Variable | Default | Description |
|---|---|---|
| `DATASHELL_DEMO_GLOBAL_RATE_LIMIT_REQUESTS_PER_MINUTE` | 600 | Identity-independent process-wide rate limit ceiling. |
| `DATASHELL_DEMO_ARTIFACT_RETENTION_MINUTES` | 120 | Age window for demo artifact eviction. |
| `DATASHELL_DEMO_MAX_ARTIFACT_BYTES` | 536870912 (512 MiB) | Total size cap for demo artifacts. |
| `DATASHELL_DEMO_TRUST_FORWARDED_HEADERS` | `false` | Trust `X-Forwarded-For` header for client identity. |

---

## 2. CLI Commands

### 2.1 `datashell run`

```bash
datashell run --contract <path> --source <path> --target <path> [--audit <path>] [--no-drift] [--json]
```

| Option | Required | Description |
|---|---|---|
| `--contract` | Yes | Path to contract JSON file. |
| `--source` | Yes | Path to input data file (CSV, JSONL, Parquet). |
| `--target` | Yes | Path to output Parquet file. |
| `--audit` | No | Path to audit certificate JSON. Default: `<output_dir>/audits/<run_id>_audit_certificate.json`. |
| `--no-drift` | No | Bypass drift evaluation for this run. |
| `--json` | No | Output JSON summary to stdout. |

**Exit codes:** `0` = delivered, `2` = rejected by policy, `1` = engine failure.

### 2.2 `datashell validate-contract`

```bash
datashell validate-contract -c <path>
```

Validates a contract at authoring time. Reports summary, warnings, and per-field errors.

### 2.3 `datashell verify`

```bash
datashell verify --certificate <path> [--artifacts <dir>]
```

Verifies a signed audit certificate and its bound artifacts.

**Exit codes:** `0` = authentic and unmodified, `3` = tampered or invalid.

### 2.4 `datashell ledger`

```bash
datashell ledger summary --tenant <tenant>
datashell ledger runs --tenant <tenant> [--status <status>] [--limit <n>]
datashell ledger show <run_id> --tenant <tenant>
datashell ledger quarantine --tenant <tenant> [--reason <substring>]
datashell ledger usage --tenant <tenant> [--since <iso8601>]
datashell ledger prune --days <n> --tenant <tenant>
```

All ledger commands are tenant-scoped. Only `ADMIN` role can query other tenants.

### 2.5 `datashell demo`

```bash
datashell demo [--json]
```

Self-contained demonstration: refusal, publish, tamper detection, and recovery.

---

## 3. API Endpoints

### 3.1 Health and Observability

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Liveness probe. |
| `GET` | `/ready` | None | Readiness probe. Returns 503 if storage, ledger, or security registry is unavailable. |
| `GET` | `/metrics` | `metrics:read` | Prometheus metrics endpoint. |
| `GET` | `/docs` | None | API documentation (disabled in production). |
| `GET` | `/openapi.json` | None | OpenAPI schema (disabled in production). |

### 3.2 Contract Management

| Method | Path | Capability | Description |
|---|---|---|---|
| `POST` | `/api/v1/contracts` | `contracts:write` | Register a new contract version. |
| `GET` | `/api/v1/contracts/{name}` | `contracts:read` | Get latest contract by name. |
| `GET` | `/api/v1/contracts/{name}?version=<v>` | `contracts:read` | Get specific contract version. |

### 3.3 Boundary Enforcement

| Method | Path | Capability | Description |
|---|---|---|---|
| `POST` | `/api/v1/enforce` | `pipeline:enforce` | Upload and enforce data against a contract. Returns reconciliation ledger and certificate. |

### 3.4 Run History and Ledger

| Method | Path | Capability | Description |
|---|---|---|---|
| `GET` | `/api/v1/runs/summary` | `audit:read` | Aggregate delivery health for a tenant. |
| `GET` | `/api/v1/runs` | `audit:read` | List run records (paginated, filterable). |
| `GET` | `/api/v1/runs/{run_id}` | `audit:read` | Full ledger record for one run. |
| `GET` | `/api/v1/runs/{run_id}/certificate` | `audit:read` | Retrieve and verify audit certificate. |
| `GET` | `/api/v1/quarantine` | `audit:read` | Search quarantine index. |
| `GET` | `/api/v1/usage` | `metrics:read` | Metering totals for a tenant. |

### 3.5 Commercial Contracts and Settlement

| Method | Path | Capability | Description |
|---|---|---|---|
| `POST` | `/api/v1/commercial-contracts` | `settlements:write` | Register a commercial contract version. |
| `GET` | `/api/v1/commercial-contracts/{id}` | `settlements:read` | Get commercial contract by ID. |
| `GET` | `/api/v1/commercial-contracts/{id}/versions` | `settlements:read` | List all versions of a commercial contract. |
| `POST` | `/api/v1/settlements/calculate` | `settlements:write` | Calculate (or idempotently replay) settlement for a run. |
| `GET` | `/api/v1/settlements` | `settlements:read` | List settlement statements. |
| `GET` | `/api/v1/settlements/{id}` | `settlements:read` | Get one settlement statement. |
| `GET` | `/api/v1/settlements/{id}/explanation` | `settlements:read` | Full calculation explanation. |
| `POST` | `/api/v1/settlements/{id}/verify` | `settlements:read` | Verify settlement receipt. |
| `POST` | `/api/v1/settlements/{id}/disputes` | `settlements:write` | Open a dispute. |
| `POST` | `/api/v1/settlements/{id}/approvals` | `billing:manage` | Approve settlement for billing. |
| `POST` | `/api/v1/settlements/{id}/billing-preview` | `settlements:read` | Create draft billing instruction. Requires `Idempotency-Key` header. |

### 3.6 Admin

| Method | Path | Capability | Description |
|---|---|---|---|
| `POST` | `/api/v1/admin/keys` | `admin:manage` | Create a managed API credential. Returns the raw key once. |
| `DELETE` | `/api/v1/admin/keys/{key_id}` | `admin:manage` | Revoke a managed API credential by 24-char fingerprint. |

---

## 4. Roles and Capabilities

| Role | Capabilities |
|---|---|
| `ADMIN` | All capabilities |
| `DATA_ENGINEER` | `contracts:read`, `contracts:write`, `pipeline:enforce`, `audit:read`, `metrics:read`, `settlements:read`, `settlements:write`, `billing:manage` |
| `PRODUCER` | `contracts:read`, `pipeline:enforce` |
| `AUDITOR` | `contracts:read`, `audit:read`, `metrics:read`, `settlements:read` |

---

## 5. Supported Input Formats

| Format | Extension | Description |
|---|---|---|
| CSV | `.csv` | Comma-separated values with header row. |
| JSONL | `.jsonl`, `.ndjson` | JSON Lines — one JSON object per line. |
| Parquet | `.parquet` | Apache Parquet columnar format. |

---

## 6. Deployment Topology

**Maximum credible topology:** One process, one worker, one replica, one trusted storage root.

The following components constrain the topology to a single replica:
- SQLite boundary ledger (`datashell_history.db`)
- SQLite credential registry (`security_registry.sqlite3`)
- Process-local token-bucket rate limiter
- Process-local concurrency semaphore

**Enforced by:**
- `Dockerfile`: `--workers 1`
- `k8s-deployment.yaml`: `replicas: 1`
- `docker-compose.yml`: Single service instance

---

## 7. Certificate Fields

| Field | Description |
|---|---|
| `certificate_version` | Certificate schema version (currently `2.0-enterprise`). |
| `run_id` | Unique run identifier. |
| `tenant_id` | Tenant that executed the run. |
| `contract.name` | Contract name. |
| `contract.version` | Contract version. |
| `contract.sha256` | Contract canonical hash. |
| `cryptographic_manifest.source_payload_sha256` | SHA-256 of the source input. |
| `cryptographic_manifest.delivered_payload_sha256` | SHA-256 of the delivered artifact. |
| `cryptographic_manifest.delivered_status` | `DELIVERED` or `REJECTED_ZERO_DELIVERED`. |
| `cryptographic_manifest.merkle_root` | Merkle root over source, delivered, quarantine, and contract hashes. |
| `cryptographic_manifest.quarantine_artifacts` | Map of quarantine artifact names to `{sha256, path}`. |
| `reconciliation.*` | Row counts: `rows_read`, `refused_structural`, `held_quality`, `rows_delivered`, `unaccounted_for`, `footing_matched`. |
| `digital_signature.algorithm` | `HMAC-SHA256` or `Ed25519`. |
| `digital_signature.key_id` | Signing key identifier. |
| `digital_signature.integrity_type` | `SYMMETRIC_HMAC_AUTHENTICATION` or `ASYMMETRIC_ED25519_SIGNATURE`. |
| `digital_signature.third_party_verifiable` | `true` for Ed25519, `false` for HMAC. |
| `digital_signature.requires_shared_secret` | `true` for HMAC, `false` for Ed25519. |

---

## 8. Reconciliation Invariant

```
rows_read = rows_delivered + rows_held + rows_refused + explicitly_excluded
```

Every terminal run must satisfy this invariant. If `unaccounted_for != 0`, delivery is refused.
