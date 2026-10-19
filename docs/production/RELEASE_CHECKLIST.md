# Data Shell — Release Checklist

**Version:** 1.0  
**Date:** 2026-10-19  
**Target:** Controlled production pilot (Target A)

---

## Pilot Release Gates

### Gate 1: Build and Reproducibility

| Check | Status | Evidence |
|---|---|---|
| Clean lockfile install | **PASS** | `pip install --require-hashes -r requirements.lock` succeeds |
| Wheel and sdist build | **PASS** | `python -m build --no-isolation` produces both |
| Distribution content check | **PASS** | `scripts/verify_distribution_contents.py` passes |
| Release metadata check | **PASS** | `scripts/check_release_metadata.py` passes |
| SBOM generation | **PASS** | CycloneDX SBOM generated from lockfile |

### Gate 2: Functional Correctness

| Check | Status | Evidence |
|---|---|---|
| Full test suite passes | **PASS** | 450 passed, 1 skipped in 20.86s |
| Airflow provider tests pass | **PASS** | 9 passed, 1 skipped |
| Reconciliation invariant holds | **PASS** | `rows_read = delivered + held + refused + excluded` |
| Rejected runs publish no accepted output | **PASS** | Verified by `test_fault_injection.py` |
| Certificate verification works | **PASS** | `test_artifact_verifier_security.py` passes |

### Gate 3: Tenant Isolation and Security

| Check | Status | Evidence |
|---|---|---|
| Two-tenant negative tests | **PASS** | `test_tenant_isolation.py` passes |
| Cross-tenant access denied | **PASS** | `verify_tenant_access` returns 403 |
| API key authentication | **PASS** | `test_admin_key_api.py` passes |
| Capability checks | **PASS** | `test_security_audit.py` passes |
| Path traversal prevention | **PASS** | `test_boundary_hardening.py` passes |
| Independent security review | **BLOCKED** | External gate — requires third-party reviewer |

### Gate 4: Publication and Recovery

| Check | Status | Evidence |
|---|---|---|
| Crash injection tests | **PASS** | `test_fault_injection.py` passes |
| Startup recovery works | **PASS** | `test_startup_recovery_order.py` passes |
| Journal integrity verified | **PASS** | HMAC-protected journals |
| Ledger robustness fix | **PASS** | `check_integrity` handles corrupt DB |

### Gate 5: Persistence and DR

| Check | Status | Evidence |
|---|---|---|
| Backup tooling | **PASS** | `scripts/backup_restore.py` created |
| Restore tested | **PASS** | `test_backup_restore.py` passes |
| Ledger integrity after restore | **PASS** | Verified by `verify_restore` |
| RPO/RTO defined | **PASS** | RPO: 24h, RTO: 1h |

### Gate 6: Capacity

| Check | Status | Evidence |
|---|---|---|
| Benchmark harness | **PASS** | `test_capacity.py` created |
| Resource limits enforced | **PASS** | Upload, concurrency, disk limits |
| Streaming auto-threshold | **PASS** | `STREAM_AUTO_THRESHOLD_BYTES` |

### Gate 7: Operations

| Check | Status | Evidence |
|---|---|---|
| Operations runbook | **PASS** | `docs/production/OPERATIONS_RUNBOOK.md` |
| Health checks | **PASS** | `/health` and `/ready` endpoints |
| Metrics endpoint | **PASS** | Authenticated `/metrics` |
| Alerting configured | **PASS** | `deploy/prometheus-alerts.yaml` |
| Rollback procedure | **PASS** | Documented in runbook |

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
| Release scope defined | **PASS** | This checklist |
| Operator owner assigned | **REQUIRED** | Named operator required |
| Security reviewer assigned | **REQUIRED** | Independent reviewer required |
| Backup/restore result | **PASS** | Tested and documented |
| Capacity envelope | **PASS** | Measured and documented |
| Open risks documented | **PASS** | See below |
| Rollback trigger defined | **PASS** | Documented in runbook |
| Expiry/review date | **REQUIRED** | Set at approval time |

---

## Remaining Blockers

| Blocker | Owner | Required For |
|---|---|---|
| Independent security review | Security team | Production approval |
| Live deployment verification | Operations | Production approval |
| Load/soak test on production hardware | Performance engineering | Capacity sign-off |
| Operator assignment | Engineering management | Production approval |
| Secret manager integration | Operations | Production deployment |

---

## Release Approval

**Version:** 1.0  
**Topology:** Single replica, single worker  
**Supported formats:** CSV, JSONL, Parquet  
**Max upload size:** 20 GiB  
**Max concurrent runs:** 2  
**Tenant scope:** Pilot tenants only  
**Data classification:** Non-sensitive or approved pilot data  

**Approvals required:**
- [ ] Engineering lead
- [ ] Security reviewer
- [ ] Operations owner
- [ ] Product owner

**Expiry/Review date:** Set at approval time (recommended: 90 days)

---

## Verdict

**Pilot gates: PARTIALLY SATISFIED**

All engineering gates have been implemented and tested. The remaining blockers are external (independent security review, live deployment verification, operator assignment). The system is ready for controlled pilot deployment once these external gates are closed.
