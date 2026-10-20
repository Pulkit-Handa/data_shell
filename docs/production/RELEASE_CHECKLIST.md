# Data Shell — Release Checklist

**Version:** 1.1
**Date:** 2026-10-20
**Target:** Controlled production pilot (Target A)

> **Status legend.** **LOCAL-VERIFIED** = executed in the development
> environment against this working tree, with a recorded result.
> **TARGET** = must be executed on the target host; not yet evidenced.
> **EXTERNAL** = requires a party outside this repository; not yet evidenced.
> **REQUIRED** = an organisational decision or a mandatory operator action.
>
> The authoritative gate matrix, with the exact evidence for every row, is
> [`RELEASE_VALIDATION_PACKAGE.md`](RELEASE_VALIDATION_PACKAGE.md). This
> checklist is the sign-off sheet; the package is the evidence.

---

## Pilot Release Gates

### Gate 1: Build and Reproducibility

| Check | Status | Evidence |
|---|---|---|
| Clean lockfile install | **LOCAL-VERIFIED** | `pip install --require-hashes -r requirements.lock` succeeds |
| Wheel and sdist build | **LOCAL-VERIFIED** | `python -m build --no-isolation` produces both |
| Distribution content check | **LOCAL-VERIFIED** | `scripts/verify_distribution_contents.py` passes |
| Release metadata check | **LOCAL-VERIFIED** | `scripts/check_release_metadata.py` passes |
| SBOM generation | **LOCAL-VERIFIED** | CycloneDX SBOM generated from lockfile |

### Gate 2: Functional Correctness

| Check | Status | Evidence |
|---|---|---|
| Full test suite passes | **LOCAL-VERIFIED** | **466 passed** in 24.99 s, exit 0 (`python -m pytest tests -q`) |
| Airflow provider tests pass | **LOCAL-VERIFIED** | 9 passed, 1 skipped (pre-existing skip) |
| Publication/ledger regression suites | **LOCAL-VERIFIED** | `test_publication_coherence.py` (11) + `test_ledger_outbox.py` (9) |
| Reconciliation invariant holds | **LOCAL-VERIFIED** | `rows_read = delivered + held + refused + excluded` |
| Rejected runs publish no accepted output | **LOCAL-VERIFIED** | Verified by `test_fault_injection.py` and `test_publication_coherence.py` |
| Certificate verification works | **LOCAL-VERIFIED** | `test_artifact_verifier_security.py` passes; end-to-end `datashell verify` returns `AUTHENTIC_AND_UNMODIFIED` |

### Gate 3: Tenant Isolation and Security

| Check | Status | Evidence |
|---|---|---|
| Two-tenant negative tests | **LOCAL-VERIFIED** | `test_tenant_isolation.py` passes |
| Cross-tenant access denied | **LOCAL-VERIFIED** | `verify_tenant_access` returns 403 |
| API key authentication | **LOCAL-VERIFIED** | `test_admin_key_api.py` passes |
| Capability checks | **LOCAL-VERIFIED** | `test_security_audit.py` passes |
| Path traversal prevention | **LOCAL-VERIFIED** | `test_boundary_hardening.py` passes |
| Static analysis and dependency audit | **LOCAL-VERIFIED** | `bandit -ll` clean; `pip-audit` reports no known vulnerabilities |
| Independent security review | **EXTERNAL — BLOCKED** | Requires a qualified third-party reviewer; see `SECURITY_REVIEW_PACKAGE.md` |

### Gate 4: Publication and Recovery

| Check | Status | Evidence |
|---|---|---|
| Crash injection tests | **LOCAL-VERIFIED** | `test_fault_injection.py` passes |
| Startup recovery works | **LOCAL-VERIFIED** | `test_startup_recovery_order.py` passes |
| Journal integrity verified | **LOCAL-VERIFIED** | HMAC-protected journals |
| Ledger robustness fix | **LOCAL-VERIFIED** | `check_integrity` handles corrupt DB |
| Generation pointer is the commit point | **LOCAL-VERIFIED** | `test_publication_coherence.py` — pointer is atomic, repairable, and read by the CLI and the API |
| Pending ledger is per-run and transactional | **LOCAL-VERIFIED** | `test_ledger_outbox.py` — independent records, single-transaction accounting, no fabricated replay |

### Gate 5: Persistence and DR

| Check | Status | Evidence |
|---|---|---|
| Backup tooling | **LOCAL-VERIFIED** | `scripts/backup_restore.py` created |
| Restore tested | **LOCAL-VERIFIED** | `test_backup_restore.py` passes |
| Ledger integrity after restore | **LOCAL-VERIFIED** | Verified by `verify_restore` |
| Backup/restore drill executed | **LOCAL-VERIFIED** | `scripts/backup_restore_drill.py` — `DRILL PASSED`, counts preserved, 2/2 certificates re-verified, pointers restored |
| Backup encryption at rest | **REQUIRED** | Operator must encrypt backups at rest |
| **Production RPO/RTO** | **TARGET — BLOCKED** | RPO = the chosen backup interval; RTO = the measured target-host restore time. Neither has been measured on the target host. The 24 h / 1 h values are *design targets*, not evidence. |
| **Production backup/restore drill** | **TARGET — BLOCKED** | The local drill is not a production drill. Execute `BACKUP_RESTORE_DRILL.md` § 4 on the target host. |

### Gate 6: Capacity

| Check | Status | Evidence |
|---|---|---|
| Benchmark harness | **LOCAL-VERIFIED** | `test_capacity.py`; `benchmarks/large_file_capacity_test.py` |
| Resource limits enforced | **LOCAL-VERIFIED** | Upload, concurrency, disk limits |
| Streaming auto-threshold | **LOCAL-VERIFIED** | `STREAM_AUTO_THRESHOLD_BYTES` |
| 1 GiB / 27.55M rows capacity run | **LOCAL-VERIFIED** | 173.14 s, ~159k rows/s, 752.8 MiB peak RSS, footing matched, 0 unaccounted |
| **Target-host capacity qualification** | **TARGET — BLOCKED** | Re-measure on the pilot host per `BENCHMARK_PROCEDURE.md` § 7; then set `MAX_UPLOAD_SIZE_BYTES` to the largest passing size. |

### Gate 7: Operations

| Check | Status | Evidence |
|---|---|---|
| Operations runbook | **LOCAL-VERIFIED** | `docs/production/OPERATIONS_RUNBOOK.md` |
| Health checks | **LOCAL-VERIFIED** | `/health` and `/ready` endpoints |
| Metrics endpoint | **LOCAL-VERIFIED** | Authenticated `/metrics` |
| Alert rules and metrics defined | **LOCAL-VERIFIED** | `deploy/prometheus-alerts.yaml`; `scripts/alert_signal_test.py` — 10/10 signals verified |
| Rollback procedure | **LOCAL-VERIFIED** | `DEPLOYMENT_VERIFICATION.md` § 13 + `INCIDENT_RESPONSE.md` |
| **Alert delivery to a human** | **TARGET — BLOCKED** | A generated signal is not a delivered alert. Confirm receipt in the on-call channel. |
| **Rollback rehearsal** | **TARGET — BLOCKED** | Redeploy the previous image on the target host and confirm readiness. |

### Gate 8: Data Handling

| Check | Status | Evidence |
|---|---|---|
| Retention policy | **PASS** | `DATASHELL_HISTORY_RETENTION_DAYS` |
| Artifact access control | **PASS** | Tenant-scoped storage roots |
| Backup encryption | **REQUIRED** | Operator must encrypt backups at rest |

### Gate 9: Certificate Semantics

| Check | Status | Evidence |
|---|---|---|
| Certificate meaning documented | **PASS** | See below |
| Verification fails closed | **PASS** | `test_artifact_verifier_security.py` |
| Key rotation tested | **PASS** | `test_fault_injection.py::TestKeyRotation` |

**What a valid certificate proves:**
- The signed artifacts have not been modified since signing
- The signing key was in the possession of the signer
- The reconciliation arithmetic footed correctly at run time

**What a valid certificate does NOT prove:**
- The input data was truthful or accurate
- The computation was semantically correct
- The contract was the correct business contract
- Non-repudiation (without customer-controlled key lifecycle)

### Gate 10: Release Scope and Approval

| Check | Status | Evidence |
|---|---|---|
| Release scope defined | **LOCAL-VERIFIED** | This checklist + `RELEASE_VALIDATION_PACKAGE.md` § 6 |
| Operator owner assigned | **REQUIRED** | Named operator required |
| Security reviewer assigned | **REQUIRED** | Independent reviewer required |
| **Live deployment verification** | **TARGET — BLOCKED** | Execute `DEPLOYMENT_VERIFICATION.md` § "Session checklist" on the target host |
| **Production backup/restore drill** | **TARGET — BLOCKED** | As above, Gate 5 |
| **Target-host capacity envelope** | **TARGET — BLOCKED** | As above, Gate 6 |
| Open risks documented | **LOCAL-VERIFIED** | `RESIDUAL_RISK_REGISTER.md` (6 blocking, 10 pilot-accepted) |
| Rollback trigger defined | **LOCAL-VERIFIED** | Documented in runbook; rehearsed on the target host before promotion |
| Expiry/review date | **REQUIRED** | Set at approval time |

---

## Remaining Blockers

Ordered by dependency. Steps 1–5 are one target-host session; step 6 needs an
external party.

| # | Blocker | Owner | Required For | Status |
|---|---|---|---|---|
| 1 | Assign service owner, data owner, security owner, release authority | Engineering management | Everything below | **REQUIRED** |
| 2 | Target-host deployment, startup, migration, rollback rehearsal | Operations | Production approval | **TARGET — BLOCKED** |
| 3 | Target-host capacity qualification; set `MAX_UPLOAD_SIZE_BYTES` | Performance engineering | Capacity sign-off | **TARGET — BLOCKED** |
| 4 | Production backup/restore drill; measure RPO and RTO | Operations | DR sign-off | **TARGET — BLOCKED** |
| 5 | Alert delivery confirmed to the on-call channel | Operations | Alerting sign-off | **TARGET — BLOCKED** |
| 6 | Independent security review (signed report) | Security team | Production approval | **EXTERNAL — BLOCKED** |
| 7 | Secret manager integration | Operations | Production deployment | **REQUIRED** |

---

## Release Approval

| Item | Value |
|---|---|
| Version | 1.0 |
| Topology | Single replica, single worker |
| Supported formats | CSV, JSONL, Parquet |
| Max upload size | Code default 20 GiB; the **pilot value is the largest size that passes on the target host** (1 GiB provisional, to be replaced by the measured value in `MAX_UPLOAD_SIZE_BYTES`) |
| Max concurrent runs | One active processing job; API request concurrency bounded by `MAX_CONCURRENT_REQUESTS` (default 2) |
| Tenant scope | Pilot tenants only |
| Data classification | Non-sensitive or approved pilot data |

**Approvals required:**
- [ ] Engineering lead
- [ ] Security reviewer
- [ ] Operations owner
- [ ] Product owner

**Expiry/Review date:** Set at approval time (recommended: 90 days)

---

## Verdict

**NOT APPROVED FOR GENERAL PRODUCTION.**

**CONTROLLED-PILOT CANDIDATE**, subject to the pilot prerequisites in
`RELEASE_VALIDATION_PACKAGE.md` § 6.

All engineering gates are **LOCAL-VERIFIED** against this working tree: 466
tests, 9 Airflow tests, clean `bandit` and `pip-audit`, a reproducible build, a
green end-to-end run→verify, a green backup/restore drill, and 10/10 alert
*signals*. Six gates remain unevidenced — the five target-host steps above and
the independent security review — and none of them may be recorded as passed
until real evidence from the target host or the reviewer exists.
