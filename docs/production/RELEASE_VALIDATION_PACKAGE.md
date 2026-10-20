# Data Shell — Target-Host Release Validation Package

**Version:** 1.0
**Date:** 2026-10-20
**Branch:** `production_hardening`

This is the **index and gate matrix** for qualifying a deployment of Data
Shell in a target environment. It states, for every mandatory gate: what
must be proven, the exact command or procedure that produces the evidence, and
where the result is recorded.

**Nothing here is a claim that the gate has passed.** Gates marked
**LOCAL-VERIFIED** were executed in the development environment; gates marked
**TARGET** require the target host; gates marked **EXTERNAL** require a party
or system outside this repository.

---

## 1. Package contents

| Component | Document / script | Covers |
|---|---|---|
| Deployment and startup verification | [`DEPLOYMENT_VERIFICATION.md`](DEPLOYMENT_VERIFICATION.md) | Clean deploy, startup, secrets, health/readiness, protected endpoints, valid run, rejected run, certificate verification, recovery, restart, migration, rollback. **§ "Session checklist"** adds the 19-step ordered checklist and the 14 stop conditions. |
| Production configuration and secret checks | `DEPLOYMENT_VERIFICATION.md` § 0, § 2 | Required env vars, placeholder/entropy rejection, wildcard CORS/proxy refusal, signer selection |
| Capacity and concurrency | [`BENCHMARK_PROCEDURE.md`](BENCHMARK_PROCEDURE.md) + `benchmarks/large_file_capacity_test.py`, `benchmarks/volumetric_stress_test.py`, `benchmarks/api_latency_load_test.py` | Throughput, latency percentiles, peak RSS, storage growth, concurrency ceiling, pass criteria |
| Backup / restore drill | [`BACKUP_RESTORE_DRILL.md`](BACKUP_RESTORE_DRILL.md) + `scripts/backup_restore_drill.py` | Backup coverage, isolated restore, certificate re-verification, ledger reconciliation |
| RPO / RTO measurement | `BACKUP_RESTORE_DRILL.md` § 3, § 4 | Recovery point (backup interval), recovery duration, restoration drill worksheet |
| Alert delivery | `DEPLOYMENT_VERIFICATION.md` § 11 + `scripts/alert_signal_test.py` + `deploy/prometheus-alerts.yaml` | Signal generation (locally verified) and external delivery (target) |
| Rollback and recovery | `DEPLOYMENT_VERIFICATION.md` § 13 + [`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md) | Rollback procedure, trigger conditions, per-incident recovery |
| Independent security review | [`SECURITY_REVIEW_PACKAGE.md`](SECURITY_REVIEW_PACKAGE.md) | Architecture, trust boundaries, authz matrix, data flow, crypto model, key lifecycle, dependencies, test map, reviewer output template |
| Publication semantics | [`GENERATION_CONTRACT.md`](GENERATION_CONTRACT.md) | The commit point, the reader contract, and what is explicitly not claimed |
| Residual risks | [`RESIDUAL_RISK_REGISTER.md`](RESIDUAL_RISK_REGISTER.md) | Every open risk with severity, consequence, evidence gap, owner dependency |

---

## 2. Release-gate matrix

Legend: **LOCAL-VERIFIED** = executed in this environment with a recorded
result. **TARGET** = must be executed on the target host. **EXTERNAL** =
requires a party or system outside this repository.

### 2.1 Correctness and integrity

| # | Gate | Must be proven | Evidence | How to produce | Status |
|---|---|---|---|---|---|
| C1 | Row accounting | `rows_read = delivered + held + refused` on every run | Footing matched, 0 unaccounted rows | `pytest tests/test_reconciler_gate.py`; every benchmark run asserts it | **LOCAL-VERIFIED** |
| C2 | Rejected data never published | A rejected run writes no delivery target and leaves the committed generation untouched | Pointer unchanged after a rejected run | `pytest tests/test_publication_coherence.py` | **LOCAL-VERIFIED** |
| C3 | Output/certificate/ledger agree | One run, one generation, one ledger row, one usage row | Atomic `apply_run_outcome` transaction | `pytest tests/test_ledger_outbox.py` | **LOCAL-VERIFIED** |
| C4 | Retry idempotency | Repeated replay creates no duplicate quarantine/usage rows | Unique identities + `INSERT OR REPLACE` | `pytest tests/test_ledger_outbox.py::test_replay_does_not_duplicate_quarantine_records` | **LOCAL-VERIFIED** |
| C5 | No fabricated delivery | A run whose artifacts were never published is never recorded as delivered | Replay skips it and logs ERROR | `pytest tests/test_ledger_outbox.py::test_replay_refuses_to_commit_a_run_whose_artifacts_were_never_published` | **LOCAL-VERIFIED** |
| C6 | End-to-end success | A real run delivers, foots, and verifies | `AUTHENTIC_AND_UNMODIFIED`, `this certificate: yes` | `datashell run` then `datashell verify` | **LOCAL-VERIFIED** |

### 2.2 Publication and recovery

| # | Gate | Must be proven | Evidence | How to produce | Status |
|---|---|---|---|---|---|
| P1 | One commit point | The pointer swap is the commit point, written only after all artifacts are durable | Code + test | `pytest tests/test_publication_coherence.py` | **LOCAL-VERIFIED** |
| P2 | Recovery repairs the pointer | A crash between rename and pointer swap converges to the committed generation | Recovery rewrites the pointer from the journal | `pytest .../test_publication_coherence.py::test_recovery_repairs_stale_generation_pointer` | **LOCAL-VERIFIED** |
| P3 | No mixed generation observed | A reader resolving the pointer sees one complete generation | Pointer + immutable lineage per run | `GENERATION_CONTRACT.md`; reader-contract tests | **LOCAL-VERIFIED** |
| P4 | Interrupted publication | Kill at each boundary → old or new valid generation | Journal recovery suite | `pytest tests/test_fault_injection.py` | **LOCAL-VERIFIED** |
| P5 | Ledger divergence is durable and repairable | Pending records persist and replay idempotently | Outbox + reconciliation | `pytest tests/test_ledger_outbox.py` | **LOCAL-VERIFIED** |
| P6 | Pending work is observable | Skipped/errored recovery is counted, logged, and metric-exposed | `datashell_pending_ledger_entries`, `datashell_publication_recovery_errors` | `scripts/alert_signal_test.py` | **LOCAL-VERIFIED** (signal); **TARGET** (delivery) |
| P7 | Concurrency on the supported topology | One replica, one job; writers serialised by the journal lock | Manifests + lock | `k8s-deployment.yaml`, `docker-compose.yml` | **LOCAL-VERIFIED** |

### 2.3 Security

| # | Gate | Must be proven | Evidence | How to produce | Status |
|---|---|---|---|---|---|
| S1 | Route-level authz | Every protected route enforces capability + tenant | Route matrix + tests | `SECURITY_REVIEW_PACKAGE.md` § 3; `pytest tests/test_security_audit.py` | **LOCAL-VERIFIED** |
| S2 | Tenant isolation | Cross-tenant access denied on every object type | Negative tests | `pytest tests/test_tenant_isolation.py` | **LOCAL-VERIFIED** |
| S3 | Path / traversal / symlink | Writes confined to the sandbox; reserved namespaces denied | Sandbox tests | `pytest tests/test_boundary_hardening.py` | **LOCAL-VERIFIED** |
| S4 | Certificate verification fails closed | Tampered artifacts, wrong key, unknown key all rejected | Verifier tests | `pytest tests/test_artifact_verifier_security.py` | **LOCAL-VERIFIED** |
| S5 | Production secret hygiene | Missing/placeholder/weak secrets refused; wildcard CORS/proxy refused | Startup refusal tests | `pytest tests/test_production_runtime_hardening.py tests/test_cli_verify_roundtrip.py` | **LOCAL-VERIFIED** |
| S6 | Static analysis + dependency audit | No actionable findings | bandit clean; pip-audit clean | `bandit -q -r datashell web -c .bandit -ll`; `pip-audit -r requirements*.lock` | **LOCAL-VERIFIED** |
| S7 | **Independent security review** | A qualified reviewer independent of the implementation issues a signed report | Review report | `SECURITY_REVIEW_PACKAGE.md` § 9, § 11 | **EXTERNAL — BLOCKED** |

### 2.4 Persistence, recovery, operations

| # | Gate | Must be proven | Evidence | How to produce | Status |
|---|---|---|---|---|---|
| O1 | Backup captures everything needed | Ledger, artifacts, pointers, certificates, contracts restored | Isolated restore + re-verification | `scripts/backup_restore_drill.py`; `BACKUP_RESTORE_DRILL.md` | **LOCAL-VERIFIED** (small dataset) |
| O2 | **Production RPO/RTO** | Measured recovery point and duration on the target host | Filled-in worksheet | `BACKUP_RESTORE_DRILL.md` § 4 | **TARGET — BLOCKED** |
| O3 | Health / readiness | `/health` and `/ready` behave as documented | 200/503 semantics | `DEPLOYMENT_VERIFICATION.md` § 3 | **LOCAL-VERIFIED** (behaviour) / **TARGET** (deployment) |
| O4 | Metrics protected | `/metrics` requires a credential | 401 without key | `DEPLOYMENT_VERIFICATION.md` § 3 | **LOCAL-VERIFIED** |
| O5 | Correlation identifiers | Every request carries a request id | `X-Request-ID` response header | `DEPLOYMENT_VERIFICATION.md` | **LOCAL-VERIFIED** |
| O6 | Alert signals | Each documented condition produces its signal | 10/10 signal checks | `scripts/alert_signal_test.py` | **LOCAL-VERIFIED** (signal) |
| O7 | **Alert delivery** | The notification reaches the on-call channel | Delivered notification | `DEPLOYMENT_VERIFICATION.md` § 11 | **TARGET — BLOCKED** |
| O8 | Runbooks are executable | Each incident has detection → containment → recovery → verification | `INCIDENT_RESPONSE.md` | Review + target drill | **LOCAL-VERIFIED** (written) / **TARGET** (exercised) |
| O9 | **Named operator / authority** | Service owner, data owner, security owner, release authority assigned | Signed checklist | `INCIDENT_RESPONSE.md` § 0 | **EXTERNAL — BLOCKED** |

### 2.5 Performance and deployment

| # | Gate | Must be proven | Evidence | How to produce | Status |
|---|---|---|---|---|---|
| F1 | Capacity envelope | 1 GiB completes with bounded memory, footing matched, 0 unaccounted | 1,024.3 MiB / 27.55 M rows → 173.14 s, 752.8 MiB peak RSS, 467.8 MiB storage growth | `benchmarks/large_file_capacity_test.py --size-mb 1024` | **LOCAL-VERIFIED** (dev container, tmpfs) |
| F2 | Throughput | Sustained rate recorded with the environment | 27k–1.94 M r/s across 10 k–1 M rows | `benchmarks/volumetric_stress_test.py` | **LOCAL-VERIFIED** |
| F3 | API latency under the configured concurrency | Percentiles at the enforced concurrency | 50/50 HTTP 200, 20.9 r/s, P50 78.6 / P95 183.9 / P99 411.0 ms | `benchmarks/api_latency_load_test.py` | **LOCAL-VERIFIED** |
| F4 | **Production capacity** | The same measurements on the target host, against the pass criteria | Filled-in worksheet | `BENCHMARK_PROCEDURE.md` § 7 | **TARGET — BLOCKED** |
| F5 | Clean install and startup | Install from the lockfile; container starts and becomes ready | Build + startup logs | `DEPLOYMENT_VERIFICATION.md` § 1 | **LOCAL-VERIFIED** (build) / **TARGET** (container) |
| F6 | Migration and rollback | Schema migrates in place; previous image remains compatible | Rollback rehearsal | `DEPLOYMENT_VERIFICATION.md` § 10, § 13 | **TARGET — BLOCKED** |
| F7 | Reproducible build | Wheel + sdist from the pinned backend; contents verified | Build + content check | `python -m build --no-isolation`; `scripts/verify_distribution_contents.py` | **LOCAL-VERIFIED** |
| F8 | Release metadata consistency | Package, app, changelog, locks agree | Metadata check | `scripts/check_release_metadata.py` | **LOCAL-VERIFIED** |
| F9 | Supported topology documented and enforced | One replica, one job, one tenant | Manifests + docs | `k8s-deployment.yaml`, `docker-compose.yml`, `Dockerfile` | **LOCAL-VERIFIED** |

---

## 3. Local verification record (the "LOCAL-VERIFIED" evidence)

This is the evidence behind every `LOCAL-VERIFIED` row in § 2. It was produced
by executing the commands below against the **exact working tree** that is
being handed off. It is a record of a local run, not of a target deployment.

**Environment**

| Item | Value |
|---|---|
| Date (UTC) | 2026-10-20 |
| Interpreter | CPython 3.14.8 (`/usr/bin/python`) |
| Test runner | pytest 9.1.1 |
| OS / kernel | Fedora Linux 44 (Workstation Edition), Linux 7.2.11-200.fc44.x86_64 |
| SQLite | 3.51.2 |
| Import path | `datashell` resolved to the working tree (`…/data-membrane-1.0.1/datashell/__init__.py`), not an installed copy |
| Topology | Local single process, single tenant, no container, no cluster |
| Worktree | Branch `production_hardening` at `2126447` plus **uncommitted** changes; the diff was reviewed in full before this record was written |

**Executed commands and results**

| # | Command | Result | Exit |
|---|---|---|---|
| 1 | `python -m compileall -q datashell web tests benchmarks scripts` | clean | 0 |
| 2 | `python -m pytest tests -q --tb=short` | **466 passed** in 24.99 s | 0 |
| 3 | `python -m pytest integrations/airflow/tests -q` | **9 passed, 1 skipped** (pre-existing skip) | 0 |
| 4 | `python -m pytest tests/test_publication_coherence.py -v` | **11 passed** | 0 |
| 5 | `python -m pytest tests/test_ledger_outbox.py -v` | **9 passed** | 0 |
| 6 | `python -m pytest tests/test_startup_recovery_order.py -v` | **2 passed** | 0 |
| 7 | `bandit -q -r datashell web -c .bandit -ll` | no findings at or above medium | 0 |
| 8 | `pip-audit -r requirements.lock -r requirements-dev.lock -r requirements-build.lock` | No known vulnerabilities | 0 |
| 9 | `python scripts/check_release_metadata.py` | `datashell 2.0.0` — package, web app, changelog, lockfiles agree | 0 |
| 10 | `python -m build --no-isolation` | wheel + sdist built | 0 |
| 11 | `python scripts/verify_distribution_contents.py` | allowlisted content only, required files present (wheel + sdist) | 0 |
| 12 | `datashell run --contract examples/vendor_settlements_contract.json --source … --target … --audit … --json` | `LOADED`, 1000 read, 900 delivered, 40 structural refused, 60 held, **0 unaccounted**, footing matched | 0 |
| 13 | `datashell verify <audit certificate>` (separate process) | `AUTHENTIC_AND_UNMODIFIED`, all artifacts `VERIFIED`, `committed generation: <run_id> (this certificate: yes)` | 0 |
| 14 | `python scripts/backup_restore_drill.py` | `DRILL PASSED` — ledger counts identical before/after, 2/2 certificates re-verified, generation pointers restored | 0 |
| 15 | `python scripts/alert_signal_test.py` | **10/10** signal checks passed (signals only — **not** external delivery) | 0 |
| 16 | Markdown link + path-reference resolver | 53 relative links, **0 broken**; 0 missing referenced files | 0 |
| 17 | `git diff --check` | clean (no whitespace errors) | 0 |

**What this record does not cover.** The local run is a single process on a
developer workstation with no container, no ingress, no shared filesystem, no
load balancer, and no real users. It therefore does **not** evidence any row
marked `TARGET` or `EXTERNAL` in § 2, and it is not a substitute for the
security review. In particular, the 1 GiB capacity and 50/50 API results below
were measured on this host and **must be re-measured on the target host**:

| Local measurement | Value | Target-host status |
|---|---|---|
| 1 GiB / 27.55M rows | 173.14 s, ~159k rows/s, 752.8 MiB peak RSS, footing matched, 0 unaccounted | **Re-measure required** |
| API latency | 50/50 verified, ~20.9 req/s at concurrency 2, P50 78.6 ms, P95 183.9 ms, P99 411.0 ms | **Re-measure required** |
| Backup/restore recovery time | ~0.01 s local restore | **Not an RTO figure** — a local filesystem restore is not a host-level recovery |

---

## 4. Execution order on the target host

The order matters: later steps depend on earlier ones being clean. The
consolidated, copy-pasteable version of this order — with the expected result
and the evidence to retain for each step — is
[`DEPLOYMENT_VERIFICATION.md`](DEPLOYMENT_VERIFICATION.md) § "Session checklist",
which also carries the 14 **stop conditions** that abort the session.

1. **Prerequisites** — `DEPLOYMENT_VERIFICATION.md` § 0 (secrets injected, volume
   mounted, filesystem confirmed local POSIX).
2. **Deploy and start** — § 1. Confirm the startup recovery summary shows
   `skipped: 0` and `journal_errors: 0`.
3. **Secrets and configuration** — § 2. The service must refuse placeholders
   and wildcards.
4. **Health, readiness, protected endpoints** — § 3, § 4.
5. **Valid run, rejected run, certificate verification** — § 5, § 6, § 7.
6. **Crash / recovery, restart, persistence** — § 8, § 9.
7. **Capacity and concurrency** — `BENCHMARK_PROCEDURE.md` § 7; record the
   environment alongside the numbers; set `MAX_UPLOAD_SIZE_BYTES` to the
   largest size that passes.
8. **Backup / restore drill and RPO/RTO** — `BACKUP_RESTORE_DRILL.md` § 4;
   fill in every blank in the worksheet.
9. **Alert signals and delivery** — `scripts/alert_signal_test.py` locally, then
   `DEPLOYMENT_VERIFICATION.md` § 11 on the target.
10. **Migration and rollback rehearsal** — § 10, § 13.
11. **Evidence capture** — § 14: attach every command's output to the release
    checklist.
12. **External gate** — issue `SECURITY_REVIEW_PACKAGE.md` to an independent
    reviewer; record the signed report.

---

## 5. Release decision rule

**General production is approved only when every row in § 2 is
`LOCAL-VERIFIED` or `TARGET`-verified with recorded evidence, and no
`EXTERNAL — BLOCKED` row remains.** Six rows are fully blocked today — **S7**
(independent security review), **O2** (production RPO/RTO), **O7** (alert
delivery), **O9** (named operator / authority), **F4** (production capacity),
and **F6** (migration and rollback) — with a further target-host component on
**F5** (container start). None of them has been executed on a target host or by
an external party, so the verdict is:

> **NOT APPROVED FOR GENERAL PRODUCTION.**
> **CONTROLLED PILOT CANDIDATE** within the scope in § 6, once the pilot's own
> prerequisites (owner assignment, alert delivery, and a capacity figure from
> the target host) are met.

---

## 6. The pilot scope this package qualifies

| Dimension | Value |
|---|---|
| Topology | One Linux host, one replica, one worker |
| Tenancy | One tenant |
| Concurrency | One active processing job; API request concurrency bounded by `MAX_CONCURRENT_REQUESTS` |
| Formats | CSV, Parquet, JSONL |
| Max file size | 1 GiB provisional — **must be set to the measured value** (`MAX_UPLOAD_SIZE_BYTES`) |
| Storage | Local POSIX filesystem with `fcntl.flock` and atomic `os.replace` |
| Signing | Ed25519 (public-key verification); HMAC only for an agreed internal trust model |
| Billing | Dry-run only; no money movement |
| Authentication | API key + capability RBAC; no SSO/SCIM |
| Availability | Business-hours operator, defined escalation, no 24×7 SLA |
| Recovery | Backup interval defines the RPO; drill defines the RTO |
| Excluded | Multi-replica, HA, SSO/SCIM, live payments, regulatory claims, wide-schema capacity |
