# Claims and Benchmarks

**Date:** 2026-10-17
**Scope:** Every externally visible claim in the README, CLI help, and
`docs/`, with the evidence that supports (or fails to support) it at this
commit. This register exists so that sales, docs, and release notes never
repeat an unverified number.

**Rule:** If a claim is not in this table, it is not verified. Do not quote it.

---

## 1. How to re-verify

```bash
python -m pytest tests/ -q                                   # automated claims
python -m datashell.cli.main demo                            # refusal/tamper/verify
python -m datashell.cli.main run \
  --contract examples/vendor_settlements_contract.json \
  --source   examples/samples/vendor_settlements_1k.jsonl \
  --target   /tmp/out.parquet --audit /tmp/audit.json        # sample delivery
python -m datashell.cli.main validate-contract \
  -c examples/vendor_settlements_contract.json
python benchmarks/volumetric_stress_test.py                  # throughput/RSS harness
python benchmarks/api_latency_load_test.py                   # HTTP latency harness
```

Record the date, Python version, hardware class, and observed output for any
number you intend to publish.

---

## 2. Claim register

| # | Claim | Source | Evidence | Status |
|---|-------|--------|----------|--------|
| C1 | "Bad data never lands, and never leaves" — non-conforming rows are not published | README, product identity | Row/batch failures produce `REJECTED` with `rows_delivered=0`; structural + semantic tests; atomic publish tests | **Verified (automated)** |
| C2 | `datashell demo` refuses bad rows, publishes good rows, detects tampering and recovery | README quickstart | `python -m datashell.cli.main demo` exit 0; `test_cli_demo.py` asserts 1,000 read / 40 refused / 60 held / 900 delivered, `AUTHENTIC_AND_UNMODIFIED` → `TAMPERED_UNDERLYING_ARTIFACTS` → `AUTHENTIC_AND_UNMODIFIED` | **Verified (manual + automated)** |
| C3 | The bundled sample (1,000 rows, 100 seeded defects) delivers 900 rows with exit code 0 under defaults | This workstream's acceptance criteria | 2026-10-17, Python 3.14.7, in-memory: `rows_read=1000, refused=40, held=60, delivered=900`, exit 0, 0.21s | **Verified (manual)** |
| C4 | Test suite passes | README badge | `python -m pytest tests/ -q` → **264 passed in 17.73s** (2026-10-17, Python 3.14.7, Linux) | **Verified (automated)** |
| C5 | Zero-deficit reconciliation (`Read = Refused + Held + Delivered`) and refusal to publish on mismatch | README architecture, `Reconciler` docs | `test_reconciler_gate.py`; unbalanced ledgers raise `ReconciliationFootingError`; every run summary reports unaccounted rows | **Verified (automated)** |
| C6 | Cryptographically signed, independently verifiable certificate (HMAC-SHA256; Ed25519 optional) | README badges/docs | `test_ed25519_signing.py`; demo tamper/recovery; `verify` exit codes 0/3 | **Verified (automated)** |
| C7 | Durable, tenant-isolated ledger of every terminal run, with quarantine index and usage metering | README §3 | `BoundaryLedger` tests (`test_ledger_api.py`, `test_ledger_cli.py`); tenant scoping enforced in query clauses | **Verified (automated)** |
| C8 | Streaming keeps application memory bounded by batch size, not input size; exercised on a 20M-row CSV with ~369 MB peak RSS | README §5 and benchmark note | Streaming tests cover CSV/JSONL/Parquet; the 20M/369MB figure was **not** reproduced in this audit (no run of that scale performed here) | **Documented, not re-verified** |
| C9 | Built-in upload ceiling is 20 GiB and request-body limit derives from it | README §5 | `DEFAULT_MAX_UPLOAD_SIZE_BYTES = 20 GiB`; `MAX_REQUEST_BODY_BYTES` derived + multipart overhead; config tests | **Verified (automated, configuration)** |
| C10 | Row throughput up to 1,000,000 rows measurable via the volumetric harness | README benchmark section | `benchmarks/volumetric_stress_test.py` exists and asserts delivered counts and file contents; no stored results in the repo | **Harness available, not run here** |
| C11 | Gateway latency P50/P95/P99 measurable via the API harness | README benchmark section | `benchmarks/api_latency_load_test.py` exists with authenticated end-to-end verification; no stored results | **Harness available, not run here** |
| C12 | Historical drift catches silently shifting feeds | README §2b | `test_drift_zero_baseline.py` (metric-aware policy), `test_metric_drift_wiring.py` (runner persists and compares metrics), `DRIFT_BASELINE_RESET` and `--no-drift` tests | **Verified (automated)** |
| C13 | Drift is opt-in and defaults off; a small test run cannot reject a production delivery | This workstream's fix | `test_product_validation_fixes.py::test_drift_disabled_by_default_ignores_prior_test_baseline`; sample delivery exit 0 | **Verified (automated)** |
| C14 | Rejection output names the failing rule, counts, and expectation | This workstream's fix | `test_product_validation_fixes.py::test_cli_rejection_output_is_actionable`; manual sample above | **Verified (automated)** |
| C15 | `validate-contract` reports a summary, warnings, and per-field errors at authoring time | This workstream's fix | `test_product_validation_fixes.py::test_validate_contract_summary_and_warnings` and `..._reports_clear_errors`; manual run: `✓ Contract 'vendor-settlements' v1.0.0 is valid. 3 row rules, 1 batch rule, 1 delivery target.` | **Verified (automated + manual)** |
| C16 | Delivery status indicator states whether data was published (`OK`/`X`) | This workstream's fix | `test_product_validation_fixes.py::test_cli_no_drift_flag_overrides_environment_default` asserts the `OK` line | **Verified (automated)** |
| C17 | Four roles, six capabilities, tenant delivery isolation, production restrictions | README §4 | RBAC/sandbox tests (`test_security_audit.py`, `test_boundary_hardening.py`) | **Verified (automated)** |
| C18 | Single-replica reference deployment; credential registry and rate limiter are process-local | README release-readiness | Code inspection; intentionally disclosed limitation | **Documented limitation** |
| C19 | Production startup requires explicit high-entropy API key/HMAC secrets and exact HTTPS origins | README release-readiness | `EnterpriseSettings` validation; `test_production_runtime_hardening.py` | **Verified (automated)** |
| C20 | Run webhook emits signed `run.completed` events with bounded retries and never fails a run | README §3 | Event dispatcher tests; best-effort exception handling in `_record_run_outcome` | **Verified (automated)** |
| C21 | CLI exit codes: 0 delivered, 2 rejected, 1 engine failure | README quickstart; CLI behavior | `test_audit_remediation.py::test_cli_exit_code_on_rejection` (2); verify uses 3 for tamper; manual runs | **Verified (automated + manual)** |
| C22 | Test count "247" previously displayed in README | README | Stale count (pre-existing); test suite actually had 256 passing before this workstream and 264 after | **Superseded** (README updated to 264) |

---

## 3. Benchmark harnesses (what they measure, how they fail safe)

| Harness | Measures | Anti-flattery design | Last stored result |
|---------|----------|----------------------|--------------------|
| `benchmarks/volumetric_stress_test.py` | Duration, rows/sec, peak RSS, delivered counts up to 1,000,000 rows | Asserts expected delivered counts, reconciliation footing, and physical row count in the output Parquet; deletes artifacts after each run | None in repo |
| `benchmarks/api_latency_load_test.py` | RPS, P50/P95/P99 latency through authenticated HTTP with delivery verification | Verifies each response delivered the expected rows; dedicated tenant/contract | None in repo |

The absence of stored results is deliberate and documented in the README:
capacity numbers are hardware-dependent and must be measured by the deploying
team.

---

## 4. Environment used for manual verification (2026-10-17)

| Item | Value |
|------|-------|
| OS | Linux |
| Python | 3.14.7 (`/usr/bin/python`) |
| Packages | Repository lockfile environment as checked out |
| Source | `examples/samples/vendor_settlements_1k.jsonl` (1,000 rows) |
| Contract | `examples/vendor_settlements_contract.json` |
| Mode | in-memory vectorized (source below `STREAM_AUTO_THRESHOLD_BYTES`) |
| Result | read 1,000; refused 40; held 60; delivered 900; unaccounted 0; exit 0; 0.21s |

These timings are single observations on a developer machine, not benchmarks.

---

## 5. Claims deliberately NOT made

The following statements must not appear in external material without new
evidence:

- Any throughput/latency/RSS figure not measured on the audience's hardware.
- "Production certified", "SOC 2", "HIPAA", or similar attestations.
- "Horizontally scalable" — the reference deployment is single-replica.
- "Detects all anomalies" — drift is threshold-based and opt-in.
- "Replaces dbt/Great Expectations" — the comparison is complementary
  (see `COMPETITIVE_ALTERNATIVES.md`).
- "No data loss ever" without the reconciliation gate context — the gate
  refuses to publish on mismatch; the claim is *refusal*, not silent loss.
