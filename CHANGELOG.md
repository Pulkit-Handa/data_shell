# Changelog

All notable changes to Data Shell are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.2] - 2026-10-15

### Added
- **Durable boundary ledger (Moat #10)**: Tenant-isolated, append-only ledger recording every terminal run (including rejections and failures), quarantine index, per-run usage metering, and rolling drift baselines. Exposed via `datashell ledger` CLI subcommands (`summary`, `runs`, `show`, `quarantine`, `usage`, `trend`, `prune`) and tenant-scoped HTTP API endpoints (`/api/v1/runs`, `/api/v1/runs/summary`, `/api/v1/runs/{run_id}`, `/api/v1/quarantine`, `/api/v1/usage`). Readiness now fails if the ledger is unavailable. Retention via `DATASHELL_HISTORY_RETENTION_DAYS`.
- **Signed run webhooks (Moat #5)**: `run.completed` events signed with HMAC-SHA256 over `timestamp.body` when `DATASHELL_RUN_WEBHOOK_URL` is set. Includes headers `X-DataShell-Event`, `X-DataShell-Delivery`, `X-DataShell-Timestamp`, `X-DataShell-Signature`. Bounded retries, best-effort emission.
- **Productized CLI**: New `run`, `validate-contract`, `verify`, `ledger`, `demo` subcommands with `--json` output. Preserves `main()` semantics and exit code `2` on policy rejection.
- **Apache Airflow provider**: `integrations/airflow` with `DataShellEnforceOperator` (gateway API), `DataShellLocalEnforceOperator` (CLI for multi-GB inputs), `DataShellHook`, dependency-free `DataShellApiClient`, and packaging/entry point metadata.
- **Disk headroom preflight**: `ensure_disk_headroom()` checks free space against `source_bytes * DATASHELL_DISK_HEADROOM_MULTIPLIER` (default 2.0) before staging; raises `InsufficientStorageError` to refuse starting runs that cannot fit transient footprint.
- **20 GiB default upload ceiling**: `DEFAULT_MAX_UPLOAD_SIZE_BYTES = 20 GiB`, `MAX_REQUEST_BODY_BYTES` derived from it (+multipart overhead). New `disk_headroom_multiplier` setting (`DATASHELL_DISK_HEADROOM_MULTIPLIER`).
- **Certificate quarantine path recording**: Quarantine artifacts now stored as `{sha256, path}` in manifests; verifier accepts both shapes. Fixes verification for contracts with custom `quarantinePath` outside tenant root.
- **Lifespan hardening**: Ledger `prune()` runs best-effort on startup; logs startup/shutdown with version.
- **Packaging hygiene**: `py.typed`, package-data configured, `integrations*` excluded from core wheel.

### Changed
- **Renamed the product to Data Shell.** Distribution/repo `data-shell`, Python package `datashell`, environment prefix `DATASHELL_`, webhook headers `X-DataShell-*`, Prometheus metrics `datashell_*`, and Airflow classes `DataShellEnforceOperator`/`DataShellLocalEnforceOperator`/`DataShellHook`/`DataShellApiClient`.
- `/ready` now checks ledger readiness (503 if not ready).
- Webhook payloads gain `event_id`, `event`, `schema_version`, `emitted_at`.
- Manifests (docker-compose.yml, k8s-deployment.yaml, render-api.yaml) sized for 20 GiB + 120 GiB volumes and include headroom/multiplier settings. `render.yaml` is now the free interactive demo (Dockerfile.web).
- Version bumped to 1.0.2.

### Fixed
- Quarantine manifest shape regression (custom quarantine paths) — verification now succeeds.
- CLI exit code semantics preserved; cross-tenant certificate access explicitly returns 403.
