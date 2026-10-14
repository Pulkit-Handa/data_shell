# Data Shell Production Operations

This runbook complements `PRODUCTION_READINESS.md`. It assumes a **single replica and one Uvicorn worker**. The local SQLite credential registry and token-bucket limiter are not a multi-replica coordination system. Runtime storage directories and new files default to private permissions (0700 directories / 0600 files); do not weaken these permissions to share data with downstream services without an explicit access-control design.

## Production configuration

Provision the following through your deployment secret manager, not a committed `.env` file:

- `DATASHELL_API_KEY`: unique, high-entropy bootstrap administrator key (at least 32 characters).
- `DATASHELL_HMAC_SECRET`: independent high-entropy signing secret (at least 32 characters).
- `ALLOWED_ORIGINS`: exact HTTPS origins, comma-separated; no wildcards, paths, query strings, or fragments.
- `FORWARDED_ALLOW_IPS`: explicit ingress/reverse-proxy IP addresses or CIDRs; never use `*`. Trust only proxy sources that network policy prevents untrusted clients from impersonating.
- `MAX_UPLOAD_SIZE_BYTES` (default 20 GiB), `MAX_REQUEST_BODY_BYTES` (derived from the upload limit plus multipart overhead), `MAX_CONTROL_BODY_BYTES`.
- `MAX_CONCURRENT_REQUESTS`, `MAX_CONCURRENT_RUNS`, `DUCKDB_MEMORY_LIMIT`, and `MIN_FREE_DISK_BYTES` sized to the container memory and persistent/temporary-storage limits.
- `DATASHELL_DISK_HEADROOM_MULTIPLIER` (default `2.0`): the per-run free-space preflight requires at least `source_bytes * multiplier` on the target volume.
- `STREAM_AUTO_THRESHOLD_BYTES` (default `67108864`, i.e. 64 MiB) and `STREAM_CHUNK_ROWS` (default `100000`) to tune when inputs switch to out-of-core processing and how large each micro-batch is.
- `DATASHELL_HISTORY_RETENTION_DAYS` (default `0` = keep everything) and, optionally, `DATASHELL_RUN_WEBHOOK_URL` / `DATASHELL_WEBHOOK_TIMEOUT_SECONDS`.

Keep the API on a private network behind a TLS-terminating ingress or reverse proxy. Do not expose `/metrics` publicly. Keep the metrics route behind an authorized API key and restrict it at the network layer as well.

## Large-file handling and temporary storage

Inputs at or above `STREAM_AUTO_THRESHOLD_BYTES` are processed by the out-of-core streaming engine, which reads Parquet, CSV, and JSONL/NDJSON sources one micro-batch at a time, writes staged Parquet incrementally, and evaluates whole-run batch assertions through DuckDB over `pyarrow.dataset`. Peak application memory is therefore a function of `STREAM_CHUNK_ROWS` (and the memory each batch needs for row-level validation), not the size of the file. Explicitly request streaming to process smaller files out of core as well, or raise `MAX_UPLOAD_SIZE_BYTES` and `MAX_REQUEST_BODY_BYTES` for larger inbound payloads.

Size the following together for the largest expected file:

- **Persistent volume:** hold the source upload spool, the staged target, the immutable lineage artifact, and the DuckDB spill beside the delivered artifact. A hard link replaces the byte-for-byte lineage copy when the paths share a filesystem, so budget roughly two to three times the delivered size rather than double-counting the lineage copy.
- **`TMPDIR` / `DATASHELL_TMP_DIR`:** point this at the durable storage volume (the reference manifests use `/app/storage/tmp`). Starlette spools multipart uploads larger than 1 MiB and DuckDB performs external sorts here; leaving it on a small memory-backed `/tmp` tmpfs makes uploads count against container memory and fail once a file is larger than the tmpfs. `/tmp` is intentionally a modest tmpfs for OS scratch only.
- **`MIN_FREE_DISK_BYTES`:** the readiness check refuses to serve when the storage volume has less free space than this.
- **Per-run headroom preflight:** independently of readiness, every run checks free space on the target volume against `source_bytes * DATASHELL_DISK_HEADROOM_MULTIPLIER` (default `2.0`) and refuses to start if it cannot fit the staged target plus worst-case quarantine. Size the volume above your largest expected input times the multiplier.
- **`DUCKDB_MEMORY_LIMIT`:** bound so DuckDB spills to `TMPDIR` instead of being killed by the container OOM killer.

## Managed API credentials

Create and revoke credentials using the administrator-only endpoints:

- `POST /api/v1/admin/keys` with `tenant_id`, `role`, and `description`.
- `DELETE /api/v1/admin/keys/{key_id}` with the 24-character fingerprint returned at creation.

The create response includes the raw key **once**. Transfer it directly to the secret manager; do not paste it into tickets, terminal transcripts, logs, or chat. The persistent SQLite registry stores only SHA-256 fingerprints and records create/revoke events without storing raw keys. The bootstrap key and keys injected through `DATASHELL_API_KEYS` remain deployment-managed and should be rotated through the secret manager and a controlled restart.

For signing-key rotation, first confirm there are no pending commit journals. Set a new `DATASHELL_KEY_ID` and `DATASHELL_HMAC_SECRET`, and retain previous key IDs and secrets in `DATASHELL_HMAC_KEYRING` (a JSON object) for the certificate retention period. The verifier uses the matching key for old certificates. Do not remove a previous key until all certificates signed by it no longer need verification. Commit journals use the active HMAC secret, so recover pending journals before changing it.

## Boundary ledger and run history

Every terminal run is written to a tenant-isolated SQLite ledger at
`<DATASHELL_OUTPUT_DIR>/datashell_history.db` alongside a quarantine index and per-run
usage rows. It is the forensic system of record: it persists rejections and failures, not
only successful deliveries. Query it from the CLI or the tenant-scoped API:

```bash
datashell ledger summary --tenant <tenant>
datashell ledger runs --tenant <tenant> --status REJECTED --limit 50
datashell ledger show <run_id> --tenant <tenant>
datashell ledger quarantine --tenant <tenant> --reason <substring>
datashell ledger usage --tenant <tenant> --since 2026-01-01T00:00:00Z
datashell ledger prune --days <n>        # also runs automatically at startup when retention > 0
```

```bash
curl -H "X-API-Key: $KEY" https://gateway/api/v1/runs/summary
curl -H "X-API-Key: $KEY" https://gateway/api/v1/usage
```

Ledger reads are always tenant-scoped; only `ADMIN` may pass an explicit `tenant_id` to
read another tenant. The ledger is part of the persistent storage root: back it up with
the same atomic snapshot as the delivery and certificate data, and include it in restore
drills. Set `DATASHELL_HISTORY_RETENTION_DAYS` to bound growth in high-volume deployments.
The ledger is local SQLite and shares the single-replica constraint of the credential
registry.

## Run webhooks

Set `DATASHELL_RUN_WEBHOOK_URL` to stream each terminal run to an external runbook, SIEM,
or ticketing endpoint as a signed `run.completed` event:

- Body: JSON with `event_id`, `event`, `schema_version`, `emitted_at`, and the run's
  status, tenant, contract, row counts, and artifact paths.
- Signature: `X-DataShell-Signature: sha256=<hex>` computed as HMAC-SHA256 over
  `<X-DataShell-Timestamp>.<raw body>`; `X-DataShell-Timestamp` is the Unix time and
  `X-DataShell-Delivery` carries the event ID.
- Receivers must recompute the HMAC with the shared secret and reject stale timestamps to
  prevent replay.
- Delivery is bounded (a few retries with backoff) and best-effort; it never fails or
  delays the run. The outbound URL is redacted from logs.

## Recovery and publication guarantees

For each successful delivery, the active target, immutable lineage artifact, and signed audit certificate are published through one journaled transaction. Transaction journals are integrity-protected with the HMAC secret in production. On startup, the service scans the output root and recovers interrupted transactions before it reports ready. If a journal is corrupted or cannot be authenticated, startup fails closed; investigate the storage and journal rather than deleting it blindly.

The transaction mechanism assumes that staged and final versions of each individual artifact reside in the same directory so that rename is atomic. Use a local or block-backed filesystem with tested `fsync`/rename semantics. Do not place the SQLite registry or transaction output on an unsupported network filesystem. Keep one replica until shared-state coordination has been implemented and tested.

## Backup and restoration

1. Define RPO/RTO with the data owner and storage provider.
2. Quiesce the service or use a storage snapshot that is atomic across the complete storage root. A file-by-file live copy is not a consistent backup of the target, certificates, journals, SQLite databases, and WAL files.
3. Encrypt backups at rest and restrict access to operators. Backups contain delivered data, quarantine records, contracts, and credential metadata.
4. Record the backup timestamp, storage snapshot identifier, and checksum in the operations log.
5. Restore to a clean, isolated storage volume. Preserve file ownership/permissions for the non-root service UID. Start the service and confirm startup recovery, `/ready`, certificate verification, and run-history consistency.
6. Perform a restoration drill before launch and on a regular schedule. Do not consider a backup successful merely because an archive exists.

## Monitoring and incident response

Import `deploy/prometheus-alerts.yaml` into Prometheus and tune thresholds to agreed SLOs. The API exports bounded-cardinality HTTP request counters, latency histograms, in-flight requests, and aggregate pipeline counters. Tenant IDs, contract names, and rule IDs are intentionally excluded from metric labels; use access-controlled structured logs and audit certificates for per-tenant investigations.

For a delivery or recovery incident:

1. Capture the request ID, run ID, time window, and tenant ID from authorized logs.
2. Check service health, `/ready`, 5xx/latency alerts, disk capacity, and container restarts.
3. Preserve the journal and related artifacts before attempting manual repair.
4. Verify the audit certificate, immutable run artifact, active target hash, and contract hash, and cross-check the run's ledger record (`datashell ledger show <run_id>`) and quarantine index.
5. If integrity cannot be established, stop further delivery, isolate the service, preserve evidence, and restore from a verified snapshot.
6. Roll back the application image through the deployment platform; do not roll back persistent storage independently of its application/schema compatibility.

## Deployment-specific notes

- **Docker Compose:** the published port binds to `127.0.0.1` by default. Put a reverse proxy in front if remote access is required.
- **Kubernetes:** replace the example HTTPS origin in the ConfigMap and the placeholder image with your organization and immutable image tag before applying the manifest. The PVC uses `ReadWriteOnce`, and the deployment intentionally remains at one replica.
- **Canary release:** begin with non-sensitive test data and a restricted tenant. Validate throughput, failure recovery, alert delivery, and restoration before onboarding critical workloads.


## Release gate and certification evidence

Do not promote an image because it builds successfully. A release candidate is eligible for controlled staging only after the CI workflow passes on every supported Python version, the full suite runs without dependency-related skips, the image smoke test succeeds with a read-only root filesystem, and dependency audit findings are reviewed. Record the source commit, image digest, SBOM, test report, vulnerability scan, deployment configuration, restore-drill evidence, and reviewer approvals for each release.

A passing repository CI run does not certify the production environment. Before customer or regulated-data use, run the crash/load/tenant-isolation exercises on the intended storage and deployment platform, validate TLS and network controls, and complete the security/compliance review required by the actual customer contract and regulatory scope.
