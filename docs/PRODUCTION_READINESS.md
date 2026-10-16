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
- CI compiles and tests the package on Python 3.11–3.13, builds a wheel and an sdist from a
  hash-pinned build backend, verifies the distribution contents, generates the SBOM, audits
  dependencies, and builds the container image.
- **Public demo runs the production path.** The unauthenticated demo/playground
  (`web/main.py`) stages uploads under `settings.temp_dir` in bounded chunks and delivers
  inside an isolated, explicitly authorized tenant namespace
  (`settings.get_tenant_delivery_dir("public-demo-<tenant>")`); `datashell demo` delivers to
  the `datashell-demo` tenant directory with a durable audit certificate and tenant
  quarantine directory in production mode. The sandbox is not weakened — the demo is
  integrated with it. Uploads are bounded by the source limit, a smaller contract/control
  limit and the total request-body limit; extensions are validated; user filenames are never
  used as paths; a process-local token bucket bounds request rate; and concurrent CLI runs
  are capped with a bounded wait. Outcomes map to explicit responses: `413` oversize, `415`
  unsupported type, `422` policy rejection/invalid input, `429` rate limited, `503` at
  capacity, `504` timeout, `507` staging/disk failure, `500` engine failure. Staging is
  always deleted; delivered artifacts, quarantine and audit records persist.
- **Two-tier demo rate limiting and a bounded artifact quota.** The per-client token
  bucket is a fairness control, not a trust boundary: `X-Forwarded-For` is ignored unless
  `DATASHELL_DEMO_TRUST_FORWARDED_HEADERS` is explicitly enabled, and the demo image
  defaults `--forwarded-allow-ips` to loopback instead of `*`. An identity-independent
  process ceiling bounds requests even when the client key is forged, and durable demo
  artifacts are evicted oldest-first after an age window and under a total-size cap scoped
  to the demo namespaces only.
- **Reproducible installs.** `requirements.lock`, `requirements-dev.lock` and
  `requirements-build.lock` are resolved on the lowest supported interpreter (3.11) and
  installed with `--require-hashes` in CI, the release workflow and both images; PEP 517
  builds run through `python -m build --no-isolation` against the pinned build backend. No
  build step re-resolves dependencies from `pyproject.toml` ranges.
- **SBOM from the resolved release set.** CI and the release workflow generate a CycloneDX
  SBOM from `requirements.lock` with the application as root component
  (`cyclonedx-py requirements requirements.lock --pyproject pyproject.toml`), so it covers
  the application and its runtime dependencies and excludes dev/runner packages.
- **Explicit packaging allowlist.** `MANIFEST.in` defines the sdist contents (code, legal
  files, lockfiles, demo app `web/`, `tests/`, `examples/`, `scripts/`) and prunes runtime
  state and caches; the wheel is limited to `datashell*` by
  `[tool.setuptools.packages.find]`. `scripts/verify_distribution_contents.py` fails the
  build on any violation.
- **Release-identity consistency check.** `scripts/check_release_metadata.py` (also run as
  `tests/test_release_metadata.py`) keeps the distribution (`data-shell`), import package
  (`datashell`) and product name ("Data Shell") distinct while requiring the version in
  `pyproject.toml`, `datashell/__init__.py`, `web/main.py` and `CHANGELOG.md` to agree, the
  release tag to match the package version, and every declared runtime dependency to be
  hash-pinned in both lockfiles. It also fails if the changelog claims a live dependency was
  removed.
- **Feed controls.** Contracts can express single-column and composite unique keys
  (`UNIQUE`, `COMPOSITE_UNIQUE`), batch control totals (`SUM_EQUALS`, `SUM_BETWEEN`),
  configurable parser options (delimiter, quote character, header presence, encoding,
  null tokens), and an explicit schema-compatibility policy (`STRICT` refuses rows that
  omit a declared column; `EVOLVING` back-fills nullable columns). Uniqueness and control
  totals are evaluated over the **delivered** rows only, never the held/quarantine set.
- **Real drift metrics with metric-aware thresholds.** Every execution mode (in-memory,
  in-memory records, and out-of-core streaming) extracts per-feed metrics — null rate per
  column plus sum/mean for numerics — persists them in the boundary ledger, and compares
  them run-over-run. Comparison is **per metric kind**, not one relative rule:
  rates/proportions (`null_rate_*`) use an absolute delta in proportion points
  (`DRIFT_MAX_RATE_ABS_DELTA`, default `0.10`); sums and means use a relative percentage
  (`DRIFT_MAX_METRIC_DRIFT_PCT`); volume uses `DRIFT_MAX_VOLUME_DROP_PCT`. Zero-baseline
  semantics are explicit: a metric whose baseline is zero/near-zero (ε = 1e-9) and stays
  there reports no drift, while a metric that *emerges* from a zero baseline reports 100%
  and fails; a vanished, missing, invalid or non-finite metric is reported explicitly and
  fails rather than being silently skipped. Metric drift stays dormant until
  `DRIFT_METRIC_MIN_BASELINE_SAMPLES` history exists. The `DriftCheckResult` certificate
  schema is unchanged.
- **Source-payload binding in every mode.** Every audit certificate binds the SHA-256 of
  the actual source payload. File-backed runs hash the source on disk; in-memory runs
  hash the canonical record set. The prior `IN_MEMORY_PAYLOAD` placeholder is gone —
  certificate generation now raises rather than emitting an unbound certificate.
- **Asymmetric certificate signatures (Ed25519).** When `DATASHELL_ED25519_PRIVATE_KEY`
  is provisioned, audit certificates are signed with Ed25519 (through the declared
  `cryptography` runtime dependency) so a third party holding
  only the published public key can verify authenticity without sharing a secret.
  Verification fails closed for unprovisioned key IDs, supports key rotation via
  `DATASHELL_ED25519_KEYRING`, and the certificate's `integrity_type`,
  `third_party_verifiable`, and `requires_shared_secret` claims state honestly whether
  asymmetric or symmetric (HMAC fallback) signing was used. HMAC remains for internal
  webhook signing.
- **Dependency lockfiles and static scan.** `requirements.lock`, `requirements-dev.lock`
  and `requirements-build.lock` pin the full resolved dependency set with hashes
  (`pip-compile --generate-hashes`), and installations use `--require-hashes`. CI audits the
  resolved dependency sets with `pip-audit` and runs Bandit (medium severity and above)
  against `datashell/` and the shipped demo app `web/`, with reviewed, documented waivers
  in `.bandit`.

## Required before a real production launch

- [ ] A clean CI run passes on every supported Python version, including the main suite,
      the Airflow provider tests (`integrations/airflow/tests`), and the container smoke
      test under a read-only root filesystem (`/ready`, `/docs` disabled, `/metrics`
      authenticated).
- [x] Generate and commit dependency lockfiles with resolved versions and hashes; do not
      treat the ranges in `pyproject.toml` as a lockfile. (`requirements.lock`,
      `requirements-dev.lock`, `requirements-build.lock`; regenerate on the lowest supported
      interpreter — see `docs/OPERATIONS.md` § "Regenerating the dependency lockfiles" —
      and verify with `pip download --require-hashes --only-binary=:all: --python-version
      <3.11|3.12|3.13> -r requirements.lock`.)
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

This revision was verified in a single Linux environment. What was actually executed:

- **Full suite: 247 passed** (Python 3.14 developer environment) and **247 passed** on a
  clean Python 3.11 virtual environment installed from
  `pip install --require-hashes -r requirements-dev.lock` plus the built wheel.
- **Reinstalled from the built sdist: 247 passed**; the sdist's `datashell/` sources are
  byte-identical to the wheel, confirming the sdist is self-contained and independently
  testable.
- **Deployment shape (wheel + `web/`, no engine source on `PYTHONPATH`): 247 passed**, with
  `import datashell` resolving to `site-packages`.
- **Lockfile/interpreter compatibility**: `pip download --require-hashes --only-binary=:all:
  --python-version {311,312,313,314}` succeeds for all three lockfiles; the previously
  pinned `numpy==2.5.4` (which requires Python ≥3.12) was replaced by `numpy==2.4.6`.
- **Container images build and run**: both `Dockerfile` and `Dockerfile.web` build; the API
  image passes the CI smoke test under a read-only root filesystem (`/ready` 200, `/docs`
  404, `/metrics` 401 without a key and 200 with one) as uid `10001:10001`; the demo image
  serves `/health`, `/`, `/playground` and, in production mode, returns HTTP 200 with a real
  `LOADED` result from `/api/demo/run` and delivers a namespaced tenant artifact via
  `/api/enforce`. In both images the engine is imported from the installed wheel, not a
  checkout.
- **Dependency audit**: `pip-audit -r requirements.lock -r requirements-dev.lock -r
  requirements-build.lock` reports no known vulnerabilities. **Bandit** 1.9.4 reports no
  medium-or-higher findings against `datashell/` and `web/`.
- **Rate-limit and quota behaviour**: unit/integration tests prove that rotating a forged
  `X-Forwarded-For` value cannot bypass the identity-independent global ceiling, that the
  per-client bucket is ignored unless header trust is explicitly enabled, and that the demo
  artifact quota evicts only stale/oversized demo artifacts (never API or other tenants'
  data, never oversized or unparseable audit certificates).
- **Distribution inspection**: `scripts/verify_distribution_contents.py` passes for the
  wheel (35 entries, `datashell/` + metadata only) and the sdist (code, legal files,
  lockfiles, `web/`, `tests/`, `examples/`, `scripts/`; no VCS data, caches, databases,
  delivered/quarantine/audit state, credentials or non-fixture Parquet).
- The out-of-core streaming path was exercised on a 20-million-row CSV at a peak RSS of
  roughly 369 MB. The quickstart demo verifies cleanly, detects a one-byte tamper, and
  verifies again after restoration.

## What remains externally gated

These gate items require execution outside this repository and are **not** satisfied by the
evidence above. Documentation is not evidence.

| Gate | Status | Why it is still open |
| --- | --- | --- |
| Clean CI run on 3.11/3.12/3.13 | **Not run** | The workflow was updated to install from the hash-pinned lockfiles, build wheel+sdist with the pinned backend, verify distribution contents and generate the SBOM from the lock; it must execute on GitHub-hosted runners. |
| Live Render deployment | **Not run** | `render.yaml` now sets `ALLOWED_ORIGINS`, `MAX_CONCURRENT_RUNS`, demo timeouts and the rate limit (production boot fails without `ALLOWED_ORIGINS`). See `docs/OPERATIONS.md` § "Deploying the public demo on Render" for the manual verification steps. |
| Proxy client-identity verification | **Not run** | Per-client rate limiting must not be treated as a security control until it is shown from the live service that a visitor cannot forge the address used for the bucket. The image and app now fail safe (header trust off, loopback-only proxy trust) and an identity-independent ceiling bounds abuse meanwhile; see `docs/OPERATIONS.md` § "Process-local limits and client-IP trust". |
| Backup / restore / DR for the ledger | **Not run** | Requires a target environment, a backup medium and a timed restore test. |
| Load / soak / disk-full / crash injection | **Not run** | Requires production-like hardware and a native RSS harness. |
| Independent security review | **Not run** | Requires an external reviewer; `docs/THREAT_MODEL.md` is the input. |
| Monitoring, alerting, rollback drill | **Not run** | Requires the live deployment and an operator runbook rehearsal. |

## Certification boundary

This repository is not independently certified. A certification claim requires an agreed
standard and scope, a reproducible build/dependency inventory, successful release
evidence, deployment-specific verification, and independent review/approval. Do not label a
build as certified until that process completes.
