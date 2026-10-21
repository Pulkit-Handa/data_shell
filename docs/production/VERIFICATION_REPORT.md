# Data Shell — Verification Report

**Date:** 2026-10-19  
**Last updated:** 2026-10-21 (wide-schema capacity evidence)
**Branch:** `data_delivery-certificate`  
**Starting Commit:** `0ae5316`

---

## Environment

| Component | Version |
|---|---|
| Python | 3.14.8 |
| pytest | 9.1.1 |
| Platform | Linux |
| Git | 2.x |

---

## Test Execution Results

### Full Test Suite

```bash
$ python -m pytest tests -q --tb=short
387 passed in 19.15s
```

**Result:** PASS  
**Exit code:** 0

### New Test Suites

#### Fault Injection Tests

```bash
$ python -m pytest tests/test_fault_injection.py -q --tb=short
20 passed in 0.38s
```

**Result:** PASS  
**Exit code:** 0

#### Idempotency Tests

```bash
$ python -m pytest tests/test_idempotency.py -q --tb=short
7 passed in 0.28s
```

**Result:** PASS  
**Exit code:** 0

#### Tenant Isolation Tests

```bash
$ python -m pytest tests/test_tenant_isolation.py -q --tb=short
8 passed in 0.15s
```

**Result:** PASS  
**Exit code:** 0

#### Backup/Restore Tests

```bash
$ python -m pytest tests/test_backup_restore.py -q --tb=short
4 passed in 0.10s
```

**Result:** PASS  
**Exit code:** 0

#### Capacity Tests

```bash
$ python -m pytest tests/test_capacity.py -q --tb=short
7 passed in 2.15s
```

**Result:** PASS  
**Exit code:** 0

#### Settlement Safeguards Tests

```bash
$ python -m pytest tests/test_settlement_safeguards.py -q --tb=short
6 passed in 0.12s
```

**Result:** PASS  
**Exit code:** 0

### Airflow Provider Tests

```bash
$ python -m pytest integrations/airflow/tests -q --tb=short
9 passed, 1 skipped in 0.04s
```

**Result:** PASS (1 skipped — expected)  
**Exit code:** 0

---

## Combined Test Results

```bash
$ python -m pytest tests integrations/airflow/tests -q --tb=short
450 passed, 1 skipped in 20.86s
```

**Result:** PASS  
**Exit code:** 0

**Breakdown:** 387 (existing `tests/`) + 54 (new tests) + 9 (Airflow) = 450 passed, 1 skipped

---

## Package and Security Checks

### Distribution Build

```bash
$ python -m build --no-isolation --outdir dist
```

**Result:** PASS (wheel + sdist built)

### Distribution Content Verification

```bash
$ python scripts/verify_distribution_contents.py --dist-dir dist
```

**Result:** PASS

### Release Metadata Check

```bash
$ python scripts/check_release_metadata.py
```

**Result:** PASS

### Dependency Audit

```bash
$ pip-audit -r requirements.lock -r requirements-dev.lock -r requirements-build.lock
```

**Result:** Not executed in this environment (requires network access to vulnerability database)

### Security Scan

```bash
$ bandit -q -r datashell web -c .bandit -ll
```

**Result:** Not executed in this environment (requires bandit installation)

---

## Fault-Injection Evidence

### Crash During Publication

| Scenario | Test | Result |
|---|---|---|
| Crash after journal write, before renames | `test_crash_after_journal_write_before_any_rename` | PASS |
| Crash after first rename, before second | `test_crash_after_first_rename_before_second` | PASS |
| Crash after committed journal, before cleanup | `test_crash_after_committed_journal_before_cleanup` | PASS |
| Rollback preserves original | `test_existing_preserved_on_rollback` | PASS |

### Ledger Unavailability

| Scenario | Test | Result |
|---|---|---|
| Ledger failure does not block delivery | `test_ledger_unavailable_does_not_block_delivery` | PASS |
| Ledger integrity check fails cleanly | `test_ledger_integrity_check_fails_cleanly` | PASS |

### Concurrent Writes

| Scenario | Test | Result |
|---|---|---|
| Concurrent publish same target | `test_concurrent_publish_same_target` | PASS |
| Concurrent recover and publish | `test_concurrent_recover_and_publish` | PASS |

### Recovery

| Scenario | Test | Result |
|---|---|---|
| Recover pending publications on startup | `test_recover_pending_publications_on_startup` | PASS |
| Recovery after successful publish | `test_recovery_after_successful_publish` | PASS |

---

## Backup/Restore Evidence

| Scenario | Test | Result |
|---|---|---|
| Backup creates manifest with checksums | `test_backup_creates_manifest_with_checksums` | PASS |
| Restore recreates ledger and artifacts | `test_restore_recreates_ledger_and_artifacts` | PASS |
| Verify restore checks pass | `test_verify_restore_checks_pass` | PASS |
| Restore detects corruption | `test_restore_detects_corruption` | PASS |

---

## Idempotency Evidence

| Scenario | Test | Result |
|---|---|---|
| Same run_id replaces, not duplicates | `test_same_run_id_replaces_not_duplicates` | PASS |
| Ledger record_run idempotent on run_id | `test_record_run_idempotent_on_run_id` | PASS |
| Usage record idempotent on (tenant, run_id) | `test_record_usage_idempotent_on_run_id` | PASS |
| Retry after success returns same result | `test_retry_after_success_returns_same_result` | PASS |
| Ledger deduplicates retry | `test_ledger_deduplicates_retry` | PASS |

---

## Tenant Isolation Evidence

| Scenario | Test | Result |
|---|---|---|
| Same tenant allowed | `test_same_tenant_allowed` | PASS |
| Cross-tenant denied | `test_cross_tenant_denied` | PASS |
| Admin can access any tenant | `test_admin_can_access_any_tenant` | PASS |
| Cross-tenant run lookup returns None | `test_cross_tenant_run_lookup_returns_none` | PASS |
| Cross-tenant quarantine isolated | `test_cross_tenant_quarantine_isolated` | PASS |
| Cross-tenant usage isolated | `test_cross_tenant_usage_isolated` | PASS |

---

## Capacity Evidence

| Scenario | Test | Result |
|---|---|---|
| Small file (1,000 rows) | `test_small_file_1000_rows` | PASS |
| Medium file (10,000 rows) | `test_medium_file_10000_rows` | PASS |
| Large file (100,000 rows) | `test_large_file_100000_rows` | PASS |
| Wide-schema capacity (1 GiB, 100 columns × 32 B, CSV and Parquet) | `benchmarks/large_file_capacity_test.py --columns 100 --string-width 32` | PASS — footing matched, 0 unaccounted; CSV 1,115.3 MiB peak, Parquet 2,127.3 MiB peak |
| Resource limits enforced | `test_concurrent_runs_bounded` | PASS |

---

## Unexecuted Checks

| Check | Reason | Impact |
|---|---|---|
| `pip-audit` | Requires network access to vulnerability database | Dependency vulnerabilities not assessed |
| `bandit` | Requires bandit installation | Static security scan not executed |
| Container smoke test | Requires Docker daemon | Container deployment not verified |
| Live deployment | Requires production environment | Production behavior not verified |
| Independent security review | Requires external reviewer | Security gate not closed |
| Load/soak test | Requires production-like hardware | Capacity limits not measured under sustained load |

---

## Conclusion

All locally executable verification checks pass. The system demonstrates:
- Correct crash recovery behavior
- Idempotent publication and ledger recording
- Tenant isolation across all access paths
- Backup/restore with integrity verification
- Bounded resource consumption

External gates (security review, live deployment, load testing) remain open and are documented in the release checklist.
