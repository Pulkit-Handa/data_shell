# Changelog

All notable changes to Data Shell are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Alert delivery to a human-operated endpoint.** `DataShellPipelineFailures`,
  `DataShellPendingLedgerEntries`, and `DataShellPublicationRecoveryErrors` are
  now emitted by the normal pipeline failure/startup-recovery paths, not only
  as Prometheus signals. Alerts are signed with HMAC-SHA256, include the alert
  ID, severity, timestamp, source, condition, evidence, recommended action, and
  runbook link, and are sent to `DATASHELL_ALERT_WEBHOOK_URL` with a bounded
  timeout, retry budget, exponential backoff, and `Retry-After` support.
  Delivery is durable (`<output_dir>/alerts/alert-deliveries.jsonl`), idempotent
  (deterministic alert IDs), and recoverable (failed alerts remain in
  `<output_dir>/alerts/pending/` and are retried at startup). New metrics:
  `datashell_alert_deliveries_total`, `datashell_alert_delivery_duration_seconds`,
  `datashell_alert_delivery_pending`.
- **Executable alert-delivery verification.** `scripts/verify_alert_delivery.py`
  drives a real failing run through the normal pipeline path, waits for the
  actual transport outcome, and optionally sends a synthetic
  `DataShellDeliveryVerification` alert. `--local-sink` proves the local
  lifecycle/transport; configured-endpoint mode is the target O7 procedure.
  Regression coverage is in `tests/test_alert_delivery.py` (31 tests). The
  target endpoint/recipient gate remains outstanding; no target credentials or
  recipient are included in this repository.

### Security
- **Tenant-scoped quarantine paths.** `spec.delivery.quarantinePath` is now
  validated with the same tenant boundary as delivery targets. In production it
  must resolve inside the authenticated tenant's quarantine namespace; the
  legacy shared `./output/quarantine` root is rebased into that namespace, while
  explicit paths under another tenant's quarantine or delivery directory are
  refused. Database files, lock files, and SQLite sidecars (`.db-wal`,
  `.db-shm`, `-journal`, ...) are rejected for both quarantine and delivery
  paths, so a contract can no longer create a directory at a live database
  path and take the ledger or credential store offline. Regression coverage:
  `tests/test_quarantine_sandbox.py`.

## [2.0.1] - 2026-10-22

### Added
- **The project is publishable on PyPI.** The distribution is now
  `datashell-engine` (see the rename below), carries complete project
  metadata (PEP 639 SPDX license expression, license file, authors,
  keywords, classifiers, project URLs), and is published by a new
  `publish-pypi.yml` workflow that uses **PyPI Trusted Publishing (OIDC)** —
  there is no long-lived API token in this repository. The workflow builds
  the wheel and sdist with the pinned build backend, runs the same
  distribution-content gate CI runs, and refuses to publish a tag that does
  not match the package version. The Airflow provider
  (`datashell-airflow`) is published by the same workflow. See
  [`docs/PUBLISHING.md`](docs/PUBLISHING.md) for the one-time PyPI
  publisher registration and the release procedure.

### Changed
- **Distribution name `datashell` → `datashell-engine`.** The `datashell`
  name on PyPI belongs to an unrelated project, so this project could not be
  published under the name its import package uses — the reason the README
  said "not published under this name; install from source". The distribution
  is renamed; **the import package (`import datashell`) and the console
  script (`datashell <command>`) keep their names**, so no operator command
  or code change is required. The release-metadata gate now pins all three
  names and fails if the distribution name ever collides with the import
  package again, and the distribution-content gate reads the expected
  metadata directory from `pyproject.toml` instead of carrying a copy of the
  name.
- **OpenTelemetry export is now verified end to end in CI.** The previous
  release shipped the tracing bridge with an in-process activation test only:
  the claim "tracing activates" was proven, but "spans reach a collector" was
  not. The new `otel-collector-e2e` CI job starts a real OpenTelemetry
  collector (pinned `otel/opentelemetry-collector-contrib` image, OTLP/gRPC
  receiver, file exporter), points the gateway at it through the standard
  `OTEL_EXPORTER_OTLP_ENDPOINT`, drives one gateway exchange, and asserts the
  collector received the span with the expected name, route, status code, and
  `service.name` resource attribute. The check fails when the endpoint is not a
  real collector, so it cannot pass vacuously. `scripts/run_otel_collector_e2e.sh`
  runs the identical check locally, and the collector log plus the captured
  spans are archived as CI evidence.
- **FastAPI's own spans travel with the gateway's.** The pinned FastAPI ships
  OpenTelemetry instrumentation, so once tracing is active a collector also
  receives the framework's `fastapi.*` spans in the same trace as Data Shell's
  server span. Nothing changed in the gateway to cause this — it was simply
  never observable before, because nothing ever verified the export path. The
  end-to-end check therefore asserts on the gateway's own span, selected by its
  instrumentation scope (`datashell`).
- **The tracing extra is now hash-pinned.** `requirements-otel.in` /
  `requirements-otel.lock` pin the `datashell-engine[otel]` dependency set
  (OpenTelemetry SDK, OTLP/gRPC exporter, gRPC) with sha256 hashes, the same
  way the core and development lockfiles are pinned, and the set is included
  in the blocking `pip-audit` gate. The core install and the
  `datashell-engine[otel]` extra are unchanged: tracing still activates only
  with `OTEL_EXPORTER_OTLP_ENDPOINT` **and** the extra installed.

### Fixed
- **Install hints no longer point at the wrong PyPI project.** The CLI, the
  DuckDB engine, the Stripe adapter, the event dispatcher and the tracing
  bridge all told operators to run `pip install datashell[<extra>]`. On PyPI
  that name belongs to an unrelated project, so following the project's own
  instructions installed the wrong software. Every hint now names the
  distribution that actually exists (`datashell-engine[<extra>]`).
- **`datashell doctor` no longer misreports installed extras.** The capability
  probes imported distribution names instead of module paths, so
  `python-multipart`, `apache-airflow` and the entire tracing extra were
  reported as missing even when they were installed. The probes now use the
  module the capability actually loads (`opentelemetry.exporter.otlp.proto.grpc`
  for the tracing extra — the same module the tracing bridge imports), and
  `tests/test_cli_doctor.py` pins both properties: an installed distribution is
  never reported as missing, and the report always agrees with what the
  interpreter can import.

## [2.0.0] - 2026-10-20

Production-readiness release: everything below was shipped incrementally and
gated by CI at each step (lint, strict typing, 519 tests, zero-findings
bandit, OSV clean dependency audit, wheel/sdist content checks, and a
CI-verified OpenAPI schema). One version number now covers all of it.

### Migration notes (1.0.x → 2.0.0)

- **Upgrades are configuration-only.** The boundary model, defaults, ledger
  format, and contract schema (`daas/v1`) are unchanged; no data or ledger
  migration is required.
- **New optional things stay off until asked for:** tracing activates only
  with `OTEL_EXPORTER_OTLP_ENDPOINT` **and** `datashell[otel]`; the benchmark
  output lives in a fresh temp directory unless `--out` is given; plugin
  rule checks are inert unless a package registers them via the
  `datashell.rule_checks.row`/`batch` entry-point groups.
- **Removed/renamed:** none. (`py.typed` was already shipped; extras gained
  `otel` — core installs and every existing extra are unchanged, and
  `all` still excludes `airflow` for its own Python-range support reasons.)
- **Operators should read** `docs/SCALING.md` (backend policy env knobs),
  `docs/production/CONFIGURATION_REFERENCE.md` (OTEL + LOG_FORMAT rows),
  `docs/SDK.md` (client regeneration truth table), and `docs/THREAT_MODEL.md`
  before promotion; CI now *fails* on new `bandit` findings or new known
  dependency vulnerabilities, so vendored dependency drift is visible.

### Added
- **Scaling backend controls + `docs/SCALING.md`.** The ingest backend is now a
  documented policy: `DATASHELL_INGEST_MODE` (`auto|memory|stream`) resolves in
  one place (`choose_execution_backend`) after explicit CLI/API requests, and
  size-threshold auto-promotion stays the default. Three new environment
  knobs: `DATASHELL_INGEST_MODE`, `DATASHELL_DELIVERY_COMPRESSION`
  (`snappy|zstd|gzip|lz4|brotli|uncompressed`, applied to delivered targets and
  quarantine snapshots on both the in-memory Polars writer and the streaming
  pyarrow writers — unified on `zstd`, the codec in-memory 1.0 deliveries
  already used), and `DATASHELL_DUCKDB_THREADS` (batch-rule evaluation
  parallelism). `docs/SCALING.md` documents the five scaling surfaces, worked
  deployment profiles, and how to reproduce the capacity numbers.
- **Modular gateway + a console that shows the real system.** The FastAPI app
  assembly shrank from 1,400 lines to ~320: endpoint groups moved to
  `datashell/server/routers/` (contracts, runs, ledger, settlements, admin),
  with shared process state in `datashell/server/state.py`. Route paths,
  payloads, and auth semantics are unchanged. The interactive console is now a
  self-contained static document (`datashell/server/static/console.html`,
  shipped in the wheel, no external requests) that renders the live run
  ledger, Prometheus metrics, the tenant settlement portfolio, and the
  interactive enforcement gate.
- **`GET /api/v1/contracts`** — tenant-scoped contract-name catalog (own +
  shared) powering client discovery and the console selector. Every read role
  may list names; writes still require `contracts:write`.
- **`GET /api/v1/settlement/preview`** — a bounded read-only roll-up of the
  settlement ledger (totals by currency, status distribution, newest
  statements) for dashboards. Requires `settlements:read`; performs no
  calculation, transition, or billing action.
- **API-surface hardening.** Every operation now carries an OpenAPI `summary`,
  and the reviewable contract document `docs/openapi.yaml` is committed and
  enforced in sync with the code in CI (`scripts/export_openapi.py`). The
  gateway introspects its routes at startup
  (`assert_routes_require_scope`): an `/api` endpoint that ships without a
  `require_scope(...)` authorization dependency fails the process at boot
  instead of serving unauthenticated data. Operators can choose
  `LOG_FORMAT=pretty` for human-readable startup/shutdown logs
  (`structured` JSON remains the default for log shippers).
- **OpenTelemetry tracing (optional extra).** With `datashell[otel]` installed
  and the standard `OTEL_EXPORTER_OTLP_ENDPOINT` set, every gateway exchange
  is exported as a server span (named after the resolved route, with status
  attributes) over batched OTLP/gRPC. Without either half the bridge is a
  no-op — `datashell doctor` reports both halves so "requested but missing
  extra" is diagnosable.
- **`datashell benchmark`.** A self-contained throughput measurement:
  generates a deterministic synthetic settlement feed, enforces the real
  contract through the real delivery gate and ledger, and reports rows/s,
  reconciliation counts, zero-deficit status, and peak RSS (JSON with
  `--json`). Minimum-row-count batch rules make small runs work; no invented
  numbers ship in the docs.
- **Extension points for quality rules.** New row/batch check types can arrive
  as plugins: a package registers compilers through the
  `datashell.rule_checks.row` / `datashell.rule_checks.batch` entry-point
  groups, and `datashell rules` lists every check type with its provenance.
  Built-in checks cannot be shadowed (a plugin named after a built-in is
  rejected and reported), unknown check names are rejected at contract
  validation with the group name spelled out, and broken plugins are logged
  instead of taking enforcement down.

- **Security gates (bandit + pip-audit).** CI enforces a zero-findings bandit
  policy (every suppression is an inline commented `# nosec` with a written
  reason) and audits `requirements.lock`, `requirements-dev.lock`, and the
  generated Python client's requirements against the OSV database. The
  reviewed SQL builders (batch-rule templates, ledger filters) carry the
  suppression rationale inline, and the batch/ledger query results are
  pinned by tests, so the builders stay behavior-verified.

### Added
- **Generated client SDKs (via OpenAPI Generator, pinned image).** Both the
  Python (`urllib3`) and TypeScript (`fetch`) clients under `sdks/` are
  regenerated deterministically from the CI-synced `docs/openapi.yaml` with
  the pinned `openapitools/openapi-generator-cli:v7.12.0` image
  (`scripts/generate_sdks.sh` — Docker is the only prerequisite; the script
  fails with an explicit message when the schema or the generator is
  missing). The Python client carries `py.typed`; the TypeScript client is
  validated to compile clean with `tsc --noEmit`. `docs/SDK.md` documents
  regeneration, install, and the versioning truth table: the clients track
  the API contract, so schema changes, SDK diffs, and the regenerated
  artifacts land together.

### Changed
- **Distribution name `data-shell` → `datashell`.** The import package and the
  pip distribution now share one name, so `pip install datashell` is all a new
  user needs. Service identity in `/health` and `--version` banners follow the
  distribution. The Airflow helper distribution is likewise renamed
  `datashell-airflow`.
- **Capability extras replace the monolithic dependency set.** The base install
  is the enforcement core only (polars, pyarrow, pydantic, cryptography,
  prometheus-client). Optional features move to extras: `server` (FastAPI
  gateway + uvicorn + python-multipart), `duckdb` (SQL batch-rule engine),
  `billing` (httpx for the signed Stripe adapter), `airflow`, and `all`.
  Even-sized service nodes (deterministic-boundary scrape nodes) no longer pull
  a web framework or an in-process SQL engine; `numpy` moved to the dev extra
  (nothing in the package imports it).
- **`datashell doctor`.** New CLI command that reports which capabilities the
  current environment has (core, extras, storage, production posture) — JSON
  via `--json` — and exits 1 only when the enforcement core is impaired.

### Fixed
- **CLI `run` → `verify` continuity without explicit secrets.** Non-production runs
  without `DATASHELL_HMAC_SECRET` now reuse a per-workspace development HMAC secret
  (`<output>/.datashell-dev-hmac`, created `0600`), so a certificate produced by
  `datashell run` verifies in a separate `datashell verify` process. Previously each
  process generated an ephemeral key and cross-process `verify` always reported
  `TAMPERED_OR_INVALID_SIGNATURE`. Explicit `DATASHELL_HMAC_SECRET` always wins;
  production never uses the persisted file (explicit high-entropy secret required).
- **Production placeholder bypass.** The secret gate rejected placeholder values only
  when shorter than 40 characters, so the long documented `.env.example` placeholders
  booted production. Any value containing a placeholder keyword or example marker
  (`replace_with_`, `__min_32_chars`, `example`, …) is now refused regardless of length.
- **Baseline HTTP hardening headers.** The gateway now emits `X-Content-Type-Options:
  nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a default
  `Cache-Control: no-store` (handlers that set their own policy keep it); the public
  demo emits `nosniff` / `SAMEORIGIN` / `no-referrer` plus `no-store` on `/api/*` JSON.
- **Broken documentation references.** `README.md`, `TUTORIAL.md`, `docs/FIRST_RUN.md`,
  `docs/THREAT_MODEL.md`, and `docs/DESIGN_RECORD.md` pointed at `docs/OPERATIONS.md`,
  `docs/PRODUCTION_READINESS.md`, `docs/product-validation/`, and a Colab notebook that
  do not exist; all links now resolve to the real `docs/production/*` runbook, checklist,
  verification report, configuration reference, and `docs/archive/PRODUCT_VALIDATION.md`.
- **PUB-001 — the generation pointer was write-only, and recovery never repaired it.** The
  pointer is the single reader-visible commit point of a publication, but no production
  reader resolved it and `_recover_locked` ignored it: a crash between the artifact
  renames and the pointer swap left the pointer naming the *previous* generation while
  the files already held the new one. Recovery now rewrites the pointer from the
  durable journal's `run_id` when the journal is in the `committed` state, and
  `datashell verify` reports whether the certificate is the currently committed
  generation (`this certificate: yes|no`).
- **LEDGER-001 — replay duplicated quarantine records.** `quarantine_index` had no unique
  identity and `record_quarantine` used a plain `INSERT`, so replaying a pending ledger
  entry duplicated forensic rows. The table now has a unique identity on
  `(tenant_id, run_id, kind, artifact_path)`; a migration collapses legacy duplicates
  before adding the constraint.
- **LEDGER-001 — a run could be marked `COMMITTED` with incomplete accounting.** The run
  row, its quarantine index rows, and its usage meter were three separate writes. They
  are now written in a single SQLite transaction (`BoundaryLedger.apply_run_outcome`),
  so a failure can never leave a committed run without its usage basis.
- **LEDGER-001 — a failed publication could be replayed as a successful delivery.** The
  pending outbox is written *before* publication, so an outbox outliving a failed
  publication described artifacts that were never published. Replay now verifies the
  run's own artifacts exist before committing it; a record whose artifacts are absent is
  skipped, left for operator review, and logged at ERROR.
- **Reconciliation failures are no longer silently swallowed.** Every skipped pending
  entry is logged at ERROR, and startup recovery reports `skipped` and `journal_errors`
  so unreconciled state is observable instead of invisible.
- **The HTTP certificate endpoint now reports the committed generation.** Only the CLI
  verifier applied the generation contract; the API left an API consumer unable to tell
  a current certificate from a superseded one. Both surfaces now share one
  implementation (`datashell.core.commit.committed_generation_status`), so they cannot
  drift apart, and a regression test asserts they agree.
- **A failed publication no longer leaves a pending ledger record.** The pending record
  is written before publication so a crash between publication and the ledger write
  stays replayable; if publication itself fails, the record is removed, because nothing
  was published and a lingering record would make every later recovery re-report a
  delivery that never happened.
- **Pending ledger records are now per-run.** The record name includes the `run_id`, so
  two runs pointed at the same audit certificate path cannot overwrite each other's
  payload. The legacy naming is still produced and still discovered, so records written
  by earlier versions recover normally.
- **Unreconciled state is alertable, not log-only.** The startup recovery path exports
  `datashell_pending_ledger_entries`, `datashell_publication_recovery_errors`, and
  `datashell_publication_recovery_journals`, and `deploy/prometheus-alerts.yaml` gains
  the matching `DataShellPendingLedgerEntries` and `DataShellPublicationRecoveryErrors`
  rules with runbook links.
- **The volumetric benchmark could not run.** It wrote to a relative `./output` that no
  longer resolved under a configured storage root and aborted with `ValueError`. It now
  uses the configured output/audit directories.
- **The API benchmark measured the wrong thing.** It offered concurrency 8 against the
  configured limit of 2, so 48/50 requests were rejected with HTTP 503 capacity
  rejections. It now caps offered concurrency to the configured limit, retries capacity
  rejections with backoff, reports them separately, and fails if any request does not
  succeed.

### Added
- **`.env.example`** with placeholders only: every supported environment variable,
  secret-generation guidance, and production requirements. The published placeholder
  values are rejected by the production secret gate.
- **Publication-coherence and ledger-durability regression suites**
  (`tests/test_publication_coherence.py`, `tests/test_ledger_outbox.py`): pointer
  repair on recovery, the reader contract, serial publication convergence, pending
  ledger state, idempotent replay, quarantine de-duplication, legacy-row migration,
  and refusal to commit an unpublished run.
- **Large-file capacity probe** (`benchmarks/large_file_capacity_test.py`): measures
  the out-of-core streaming path against a target file size and reports throughput,
  peak RSS, and storage growth, failing if the run does not foot to zero.
- **Backup/restore drill** (`scripts/backup_restore_drill.py`): executes real runs,
  backs up, restores into an isolated directory, re-verifies the restored certificates
  against the restored artifacts, and reports measured recovery duration.
- **Production documentation**: `docs/production/RELEASE_VALIDATION_PACKAGE.md`
  (index and release-gate matrix), `docs/production/DEPLOYMENT_VERIFICATION.md`
  (executable deployment verification runbook), `docs/production/BENCHMARK_PROCEDURE.md`
  (procedure plus measured results), `docs/production/BACKUP_RESTORE_DRILL.md` (drill
  record and target drill), `docs/production/SECURITY_REVIEW_PACKAGE.md` (review
  package for an independent reviewer), `docs/production/INCIDENT_RESPONSE.md`
  (incident runbooks), `docs/production/GENERATION_CONTRACT.md` (the commit point and
  the reader contract), and `docs/production/RESIDUAL_RISK_REGISTER.md`.
- **Alert-signal verification** (`scripts/alert_signal_test.py`): triggers each
  documented alert condition and proves the corresponding metric moves, so the
  alert rules are checked against real signals rather than only being present.
- **Cross-process certificate verification regression suite**
  (`tests/test_cli_verify_roundtrip.py`): run→verify continuity, per-workspace
  dev-secret persistence (0600, env-override precedence), production rejection of the
  published placeholders, and security-header assertions for both HTTP surfaces.
- **Release-validation package finalisation.** `docs/production/RELEASE_VALIDATION_PACKAGE.md`
  now carries a § "Local verification record" with the exact interpreter, OS, SQLite
  version, import path, and the 17 commands behind every `LOCAL-VERIFIED` gate row,
  together with an explicit statement of what the local run does *not* evidence.
  `docs/production/DEPLOYMENT_VERIFICATION.md` gains a consolidated 19-step
  target-host session checklist (action, command, expected result, evidence to
  retain) and 14 numbered **stop conditions** that abort the session, so the
  authorised operator session is executable without re-deriving the order.
- **Residual-risk register closure entries.** RR-F08 (reader-contract asymmetry
  between the CLI and the API certificate endpoint), RR-F09 (two runs sharing an
  audit path overwriting each other's pending ledger record), and RR-F10 (a failed
  publication leaving a pending record describing a delivery that never happened)
  are recorded with their fix and their regression test.
- **The test-count badge now matches the suite.** The README badge claimed 445
  passing tests; the suite contains 466. The badge and the quick-start section
  were corrected, and the count is now stated as measured rather than estimated.
- **Verifiable data delivery & settlement**: versioned commercial contracts (`datashell contract validate|create|show|versions`), a deterministic exact-arithmetic settlement engine (integer minor units, single `ROUND_HALF_UP` stage, tiers, caps, overflow refusal, no binary floats), contractual acceptance verdicts with machine-readable reason codes, and settlement statements bound to exactly one immutable contract version. New CLI surface `datashell settlement calculate|show|list|explain|verify|invoice-preview|invoice-export|dispute-open|dispute-resolve|billing-preview` with a dedicated refusal exit code `4`.
- **Signed settlement receipts**: a versioned extension of the audit-certificate signature envelope binding the delivery-certificate digest, contract id/version/digest, and the full calculation. Ed25519 when provisioned (third-party verifiable); HMAC-SHA256 otherwise, never mislabelled as third-party verifiable. Tampering with any settlement-critical field fails verification.
- **Durable settlement ledger**: `settlements`, `settlement_events` (append-only lifecycle transitions), `disputes`, `billing_instructions`, and `contract_versions` tables extending the boundary-ledger SQLite database, with tenant scoping, deterministic statement IDs, and uniqueness constraints backing idempotency and duplicate-prevention guarantees.
- **Dispute workflow** (`OPEN`/`UNDER_REVIEW`/`RESOLVED`/`WITHDRAWN`): statements move to `DISPUTED`, automatic billing and approvals are blocked, the original calculation and evidence are preserved, and resolutions (`ACCEPT_AS_IS`, `REJECT_SETTLEMENT` → `CANCELLED`, correction/replacement with preserved history) are recorded with actor and reason.
- **Billing provider abstraction**: default offline deterministic **dry-run** adapter, plus an opt-in **Stripe draft-invoice** adapter (disabled by default, test mode by default, `auto_advance=false`, per-call idempotency keys, timeout recorded as `UNRESOLVED` with retries refused pending reconciliation, never finalizes or collects). Stripe webhook signature verification with replay dedup is implemented. No live money movement exists in any code path.
- **HTTP API**: tenant-scoped `/api/v1/commercial-contracts*` and `/api/v1/settlements*` endpoints (calculate, list, show, explanation, verify, disputes, approvals, billing-preview) with `SETTLEMENTS_READ`/`SETTLEMENTS_WRITE`/`BILLING_MANAGE` capabilities; billing preview requires an `Idempotency-Key` header.
- **End-to-end demo and benchmark**: `examples/settlement_demo.py` (1,000,000 source rows, 10,000 refused, 5,000 held, 985,000 delivered at USD 0.05 → derived USD 49,250.00; scenarios A–H) and `examples/settlement_benchmark.py` (pipeline throughput vs. settlement latency/memory). No new runtime dependencies (httpx and cryptography were already locked).
- **Documentation**: `docs/SETTLEMENT.md` (architecture, schemas, semantics, invariants, limitations) and `docs/SETTLEMENT_OPERATIONS.md` (CLI/API reference, billing configuration, idempotency, dispute runbook, threat model, production checklist, measured performance).
- **Tests**: settlement primitives, integration (real pipeline + certificates + API), invariant/property, and CLI suites (387 total in the suite, all pre-existing tests preserved).

### Fixed
- **Production startup with bundled settlement example**: startup contract auto-seeding now only parses files carrying the data-contract envelope (`metadata` + `spec`) from `examples/*_contract.json`. Previously the settlement example `examples/commercial_delivery_contract.json` matched the glob, failed `DataContract` validation, and — in production mode — aborted application startup with a `RuntimeError`, preventing the container from ever becoming ready (caught by the CI production-container check).

### Notes
- The Stripe adapter is implemented and exercised against a faked HTTP transport (draft-only semantics, timeout/`UNRESOLVED`, retry refusal, webhook verification). It has **not** been exercised against live Stripe test-mode credentials, which are not provisioned in this environment; the provider remains disabled by default and live money movement is explicitly out of scope.

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
- **Metric-aware drift policy with zero-baseline semantics.** Drift now compares each metric by kind instead of applying one relative rule: null/rate metrics use an absolute delta in proportion points (`DRIFT_MAX_RATE_ABS_DELTA`, default `0.10`), while sums/means keep the relative threshold (`DRIFT_MAX_METRIC_DRIFT_PCT`). A zero/near-zero baseline that stays near zero reports no drift; a metric that *emerges* from a zero baseline reports 100%; a vanished, missing, invalid or non-finite metric is reported explicitly and fails. Absent historical metrics can no longer silently suppress a comparison. Volume drift, initial-baseline and `DRIFT_METRIC_MIN_BASELINE_SAMPLES` semantics are preserved, as is the `DriftCheckResult` certificate schema.
- **`scripts/check_release_metadata.py` + `tests/test_release_metadata.py`.** Automated release-identity check preserving the distribution (`data-shell`), import package (`datashell`) and product name ("Data Shell") distinctions: the version copies in `pyproject.toml`, `datashell/__init__.py`, `web/main.py` and `CHANGELOG.md` must agree, the release tag must match the package version, and every declared runtime dependency must be hash-pinned in both lockfiles. Runs in the suite, in CI, and in the release workflow.
- **`scripts/verify_distribution_contents.py`.** Built artifacts are inspected against an explicit allowlist: the wheel may contain only `datashell/` plus its own metadata, and the sdist must carry the code, license/metadata, lockfiles, the demo application (`web/` engine + templates + static assets) and the test fixtures (`tests/`, `examples/`), while never shipping VCS data, caches, local databases, delivered/quarantine/audit state, credentials, request staging or non-fixture Parquet.

### Changed
- **Renamed the product to Data Shell.** Distribution/repo `data-shell`, Python package `datashell`, environment prefix `DATASHELL_`, webhook headers `X-DataShell-*`, Prometheus metrics `datashell_*`, and Airflow classes `DataShellEnforceOperator`/`DataShellLocalEnforceOperator`/`DataShellHook`/`DataShellApiClient`.
- `/ready` now checks ledger readiness (503 if not ready).
- Webhook payloads gain `event_id`, `event`, `schema_version`, `emitted_at`.
- Manifests (docker-compose.yml, k8s-deployment.yaml, render-api.yaml) sized for 20 GiB + 120 GiB volumes and include headroom/multiplier settings. `render.yaml` is now the free interactive demo (Dockerfile.web).
- **Version bumped to 1.0.2.**
- **The public demo runs its production path for real.** `web/main.py` stages uploads under `settings.temp_dir` and delivers inside an isolated, explicitly authorized tenant namespace (`settings.get_tenant_delivery_dir("public-demo-<tenant>")`); `datashell demo` delivers to the `datashell-demo` tenant directory with a durable audit certificate and tenant quarantine directory in production mode. The sandbox is unchanged — the demo is integrated with it rather than granted an exemption.
- **Bounded, streamed uploads with explicit capacity limits.** The demo stages uploads in bounded chunks (never `read()`-whole-file), enforces the source limit, the smaller contract/control-body limit and the total request-body limit including multipart overhead, validates filename extensions, never turns a user-supplied filename into a path, applies a process-local token-bucket rate limit, and bounds concurrent CLI executions with a bounded wait. Outcomes map to explicit responses: `413` oversize, `415` unsupported type, `422` policy rejection/invalid input, `429` rate limited, `503` at capacity, `504` timeout, `507` staging/disk failure, `500` engine failure. Staging is always removed; durable delivery, quarantine and signed audit artifacts persist.
- **Reproducible installs for CI, release and both images.** `requirements.lock`, `requirements-dev.lock` and the new `requirements-build.lock` are resolved on the lowest supported interpreter (3.11) and installed with `--require-hashes`; PEP 517 builds run through `python -m build --no-isolation` against the pinned build backend. The lock/interpreter incompatibility that pinned `numpy==2.5.4` (requires Python ≥3.12) while `requires-python = ">=3.11"` is fixed by pinning `numpy==2.4.6`, verified for 3.11–3.14.
- **SBOM generated from the resolved release set.** CI and the release workflow now build the SBOM with `cyclonedx-py requirements requirements.lock --pyproject pyproject.toml` — the application as root component plus exactly the hash-pinned runtime dependency set — replacing `cyclonedx-py environment`, which inventoried the runner including dev-only and tooling packages. Generation or validation failure fails the workflow.
- **Explicit packaging allowlist.** `MANIFEST.in` defines the sdist contents explicitly (code, license/metadata, lockfiles, `web/` demo source, `tests/`, `examples/`) and prunes runtime state and caches; the wheel remains limited to `datashell*` by `[tool.setuptools.packages.find]`. Both are verified in CI.
- The demo container image (`Dockerfile.web`) now runs as a non-root user with a healthcheck and lock-pinned dependencies, matching the hardened API image.
- `render.yaml` sets an explicit `ALLOWED_ORIGINS` (required in production), plus demo timeouts, concurrency settings, the per-client rate limit and its identity-independent global ceiling, `DATASHELL_DEMO_TRUST_FORWARDED_HEADERS=false`, and the artifact retention/size quota.

### Fixed
- Quarantine manifest shape regression (custom quarantine paths) — verification now succeeds.
- CLI exit code semantics preserved; cross-tenant certificate access explicitly returns 403.
- **The public demo no longer reports success when enforcement fails.** It previously returned HTTP `200` with a status taken from the engine's JSON even when the CLI exited non-zero, masking policy rejections (exit code `2`). It now always checks the engine exit status, verifies the reported artifact exists inside the tenant delivery directory, and maps failure, rejection and timeout to explicit error responses while preserving exit code `2` semantics.
- **The demo's client-IP rate limit could be bypassed by forging `X-Forwarded-For`.** `Dockerfile.web` shipped `--forwarded-allow-ips="*"`, so uvicorn took the client address from the leftmost, client-controlled header value and `_client_identity()` keyed the token bucket on it; rotating the header defeated the limit. The image now defaults that to loopback (matching the API image), the app consults the header only when `DATASHELL_DEMO_TRUST_FORWARDED_HEADERS=true` and then takes the rightmost hop, and an identity-independent global ceiling (`DATASHELL_DEMO_GLOBAL_RATE_LIMIT_REQUESTS_PER_MINUTE`) bounds requests even when the client key is spoofed. The per-client limit is documented as a fairness control, not a security boundary.
- **The public demo had no artifact quota.** Delivered parquet, lineage copies, quarantine records and audit certificates accumulated for the life of the container. They are now evicted oldest-first after `DATASHELL_DEMO_ARTIFACT_RETENTION_MINUTES` (default 120) or beyond `DATASHELL_DEMO_MAX_ARTIFACT_BYTES` (default 512 MiB), scoped strictly to the `public-demo-*` and `datashell-demo` namespaces plus demo-tenant audit certificates; API/other tenants' artifacts and unreadable certificates are never deleted, and cleanup never fails a request.
- **Drift silently skipped zero-baseline metrics.** Relative comparison divided by a zero baseline, so null-rate, mean and sum checks were suppressed and only volume drift was emitted — the run looked clean. Metrics are now compared per kind, suppressed metrics are reported honestly, and invalid/non-finite/missing history fails explicitly instead of passing silently.
- **Corrected the 1.0.2 release notes.** `cryptography` was never removed: it is the Ed25519 certificate-signing backend (`datashell/core/keys.py`), declared in `pyproject.toml` and pinned in `requirements.lock`. An automated metadata check now fails the build if the changelog contradicts the declared dependencies.
- CI: `setuptools`/`wheel` are installed from hash-pinned `requirements-build.lock` before `--no-build-isolation` builds, so the pinned build backend is used on every interpreter.
- CI: add `pyyaml` to the `dev` extra so the deployment-manifest YAML validation step can run.
