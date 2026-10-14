# Production Readiness Gate

**Current status: hardened for single-replica production use; not independently certified.**
The repository carries security, reliability, and large-file hardening plus a durable
forensic ledger, but code-level checks are not a substitute for a clean CI run in your
environment, a deployment-specific threat model, operational testing, and independent
review.

## Implemented in this revision

- **Boundary ledger (system of record).** Every terminal run — delivered, rejected, or
  failed — is persisted to a tenant-isolated SQLite ledger with a quarantine index and
  per-run usage metering. It backs rolling drift baselines, incident forensics, and
  outcome metering, and is exposed through the CLI (`datashell ledger ...`) and
  tenant-scoped HTTP endpoints (`/api/v1/runs`, `/api/v1/runs/summary`,
  `/api/v1/quarantine`, `/api/v1/usage`). Readiness fails if the ledger cannot be opened.
  Retention is enforced at startup via `DATASHELL_HISTORY_RETENTION_DAYS`.
- **Signed run webhooks.** When `DATASHELL_RUN_WEBHOOK_URL` is set, each terminal run is
  pushed as a `run.completed` event signed with HMAC-SHA256 over `timestamp.body`
  (`X-DataShell-Signature`, `X-DataShell-Timestamp`, `X-DataShell-Event`,
  `X-DataShell-Delivery`). Delivery retries a bounded number of times and can never fail
  the run.
- **20 GiB default upload ceiling.** `DEFAULT_MAX_UPLOAD_SIZE_BYTES` is 20 GiB and
  `MAX_REQUEST_BODY_BYTES` is derived from it (plus multipart overhead), so the product
  default accepts large files without an environment override.
- **Storage-headroom preflight.** Before staging, each run checks free space on the
  target volume against `source_bytes * DATASHELL_DISK_HEADROOM_MULTIPLIER` (default
  `2.0`) and refuses to start with `InsufficientStorageError` rather than failing
  mid-run.
- **Productized CLI and demo.** `datashell run|validate-contract|verify|ledger|demo`,
  including a self-contained `datashell demo` that proves refusal, publish, and
  tamper-detection end to end. Exit code `2` signals a policy rejection.
- **Apache Airflow provider.** `integrations/airflow` ships `DataShellEnforceOperator`
  (gateway API) and `DataShellLocalEnforceOperator` (local CLI for multi-gigabyte inputs)
  plus the dependency-free `DataShellApiClient`.
- Production startup rejects missing/insecure master API and HMAC secrets, identical
  authentication/signing secrets, wildcard CORS, non-HTTPS production origins, invalid
  resource-limit settings, and malformed tenant-key configuration.
- Interactive API documentation is disabled in production; health responses disclose
  minimal service state. Startup recovery runs before bundled contract seeding, and a
  recovery failure prevents startup/readiness. Production contract-seeding failures are
  fatal rather than silently leaving a partially initialized catalog.
- The console does not pre-fill a known/demo API key, and run-certificate IDs are
  validated before filesystem use.
- Tenant storage paths use collision-resistant encoding. Multi-directory publication
  commits the active target, immutable lineage artifact, and signed audit certificate
  together. Journals are HMAC-protected in production, rollback is durable, and startup
  recovers interrupted transactions before readiness.
- API credentials and capabilities are checked before request bodies are parsed;
  credentials and capabilities are checked before work, upload/control body sizes are
  bounded and counted in chunks, and concurrency/memory are capped. Streaming switches
  on automatically at `STREAM_AUTO_THRESHOLD_BYTES`.
- Certificates record each quarantine artifact by hash **and** path, so contracts that
  direct quarantine outside the tenant root still verify. Verification validates the
  signature before dereferencing paths and restricts artifact paths to allowed roots.
- Docker runs one worker with a read-only root filesystem, non-root user, and persistent
  storage; Kubernetes ships a readiness probe, non-root/read-only security context, PVC,
  and a single replica.
- CI compiles and tests the package on Python 3.11–3.13, builds a wheel, audits
  dependencies, and builds the container image.

## Required before a real production launch

- [ ] A clean CI run passes on every supported Python version, including the main suite,
      the Airflow provider tests (`integrations/airflow/tests`), and the container smoke
      test under a read-only root filesystem (`/ready`, `/docs` disabled, `/metrics`
      authenticated).
- [ ] Generate and commit a dependency lockfile with resolved versions and hashes; do not
      treat the ranges in `pyproject.toml` as a lockfile.
- [ ] Review `docs/THREAT_MODEL.md` and complete an independent security review covering
      authentication, authorization, tenant boundaries, upload denial-of-service controls,
      the boundary ledger, and certificate/artifact verification.
- [ ] For multiple replicas, replace the single-replica SQLite credential registry **and**
      the boundary ledger with a supported shared store, and validate concurrent
      revocation and concurrent ledger writes.
- [ ] Replace the process-local token-bucket rate limiter with a shared rate limiter before
      running multiple replicas or promising global quotas.
- [ ] Define and test backup, restore, retention, and disaster-recovery semantics for the
      ledger (`output/datashell_history.db`) alongside the delivery and certificate storage.
- [ ] Configure TLS at a trusted ingress/reverse proxy; restrict network access and protect
      `/metrics`; provision unique secrets from a secret manager and set
      `ALLOWED_ORIGINS`/`FORWARDED_ALLOW_IPS` explicitly.
- [ ] Run realistic load and soak tests on production-like hardware with native RSS
      measurement, disk-full tests, crash injection, concurrent writers, and expected
      row-count assertions.
- [ ] Confirm webhook receivers verify the HMAC timestamped signature and reject stale
      deliveries; treat webhook emission as best-effort, not a delivery guarantee.
- [ ] Define operational SLOs, alert thresholds, incident response, data-retention policy,
      and a tested rollback procedure.

## Current validation evidence

In the authoring environment (Python 3.14; Polars, PyArrow, and DuckDB installed), the main
suite passes **156 tests**, the Airflow provider's dependency-free client tests pass (and
operator tests skip when Airflow is absent), and `python -m compileall` succeeds. The
out-of-core streaming path has been exercised on a 20-million-row CSV at a peak RSS of
roughly 369 MB. The quickstart demo verifies cleanly, detects a one-byte tamper, and
verifies again after restoration. The clean-room CI run, container build/runtime, and
dependency-locked build referenced above must still pass before release; the current
results are from a single environment, not a release gate.

## Certification boundary

This repository is not independently certified. A certification claim requires an agreed
standard and scope, a reproducible build/dependency inventory, successful release
evidence, deployment-specific verification, and independent review/approval. Do not label a
build as certified until that process completes.
