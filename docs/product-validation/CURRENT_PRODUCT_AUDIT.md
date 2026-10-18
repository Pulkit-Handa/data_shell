# Current Product Audit

**Audit date:** 2026-10-17
**Scope:** Data Shell 1.0.1 (`data-membrane-production-candidate-1.0.1`)
**Method:** Source inspection, black-box CLI reproduction on the bundled sample
dataset, full test-suite execution, and output review against the product's own
documented claims.
**Auditor:** Product validation workstream (internal)

---

## 1. Executive summary

Data Shell's enforcement core is sound: the four-stage pipeline, zero-deficit
reconciliation, atomic publication, signed certificates, and durable ledger all
work as documented, and 264 automated tests pass. The critical defects were not
in the enforcement arithmetic but at the **edges of the product**: a drift
default that rejected valid feeds because of unrelated baseline history, a
generic rejection message that made failures undiagnosable, and an
authoring-time validation surface that deferred contract mistakes to runtime.

| ID | Severity | Defect | Status |
|----|----------|--------|--------|
| P0-1 | Critical | Drift baseline established from *any* prior delivered run, including tiny test runs → valid production feeds rejected with exit code 2 | **Fixed** |
| P0-2 | Critical | Rejection output named no rule, count, or expectation | **Fixed** |
| P0-3 | High | `validate-contract` accepted/echoed contracts without surfacing authoring problems or warnings | **Fixed** |
| P1-4 | High | No per-run way to bypass drift | **Fixed** (`--no-drift`) |
| P1-5 | Medium | No explicit "was data published?" indicator | **Fixed** |
| P1-6 | Medium | No first-run guide; README assumed background knowledge | **Fixed** (`docs/FIRST_RUN.md`) |

This document records the evidence for each finding and the residual risk that
remains after the fixes.

---

## 2. What works (verified)

| Capability | Evidence |
|------------|----------|
| Four-stage enforcement (structural → semantic → reconciliation → atomic publish) | `datashell demo` completes: 1,000 rows read, 40 refused, 60 held, 900 delivered |
| Zero-deficit reconciliation | Sample run reports `Unaccounted for: 0 · Matched`; unbalanced ledgers raise `ReconciliationFootingError` (tests `test_reconciler_gate.py`) |
| Row rules and batch rules | Sample run holds 35 negative amounts and 25 bad currencies; `not-partial` batch rule evaluated over the 900 admitted rows |
| Atomic, recoverable publication | Write-ahead journal + staged target/lineage/certificate commit; `test_audit_remediation.py::test_atomic_delivery_leaves_no_staged_artifacts` |
| Signed, tamper-evident certificate | Demo flips one byte and `verify` returns `TAMPERED_UNDERLYING_ARTIFACTS`; restoring bytes returns `AUTHENTIC_AND_UNMODIFIED` |
| Durable, tenant-scoped forensic ledger | `datashell ledger runs/summary/trend/quarantine/usage` operate over `datashell_history.db` |
| Streaming out-of-core execution | `test_large_file_streaming.py`; CSV/JSONL/Parquet parity tests |
| Test suite | `python -m pytest tests/ -q` → **264 passed** (2026-10-17, Python 3.14.7) |
| Sample dataset contract | `datashell validate-contract -c examples/vendor_settlements_contract.json` → valid, 3 row rules, 1 batch rule, 1 delivery target |

---

## 3. What was broken (P0) — evidence and fixes

### P0-1. Drift engine false rejections

**Reproduction (before fix):**

```bash
python -m datashell.cli.main run \
  --contract examples/vendor_settlements_contract.json \
  --source examples/samples/vendor_settlements_1k.jsonl \
  --target /tmp/out.parquet --audit /tmp/audit.json
# exit code 2; rows_read=1000, delivered=0
```

The audit certificate showed:

```text
drift-volume-stability   passed  current=900   baseline=5
drift-metric-avg_amount  passed  current=685.75  baseline=1213.65
drift-metric-sum_amount  FAILED  current=617175.0 baseline=6068.25
```

**Root cause.** `HistoricalDriftEngine.evaluate_drift` derives its baseline
from the ledger's most recent delivered run for the contract/tenant
(`BoundaryLedger.get_baseline`). Any prior delivered run counts — including
5-row smoke tests written by the test suite or by a developer experiment. A
normal 900-row delivery then trips volume/metric drift against a meaningless
baseline.

**Fix.**

- New `DRIFT_ENABLED` setting, **default `false`**. When disabled, the runner
  skips drift evaluation entirely in `run_file`, `run_records`, and
  `run_streaming` and records no drift checks.
- New `DRIFT_BASELINE_RESET` setting for use with drift enabled: records a
  reset watermark for the contract/tenant so the current run establishes a
  fresh baseline without deleting forensic history (`BoundaryLedger.reset_baseline`).
- New `--no-drift` CLI flag for one-off bypass.

**Verification (after fix):**

```text
Terminal Status: LOADED
Delivered rows:  900
OK Data was published to: /tmp/out.parquet   # exit code 0
```

### P0-2. Unactionable rejection messages

**Reproduction (before fix):**

```text
[!] FEED REJECTED: Violations exceeded declared contract gate.
```

No rule name, no counts, no expectation. The OpenSearch-style failure mode:
operators know *that* something failed, not *what to change*.

**Root cause.** The CLI printed a constant string and never read
`result.ledger.rejection_reason`; the runner's `DeliveryEngine.deliver` also
set a constant reason (`"Violations or batch assertions exceeded policy gate"`).

**Fix.**

- The runner now builds a specific reason naming every failed gate:
  `not-partial batch rule failed (actual: 2 rows, expected: >= 3)`,
  failing drift metrics with values/thresholds, and `REJECT_RUN` row-policy
  violations.
- The CLI prints the reason, positive-count rule breakdown, structural
  refusals, all batch rule results, failed drift checks, and a pointer to
  `--json`/the audit certificate.

**Verification (after fix):**

```text
[!] FEED REJECTED: not-partial batch rule failed (actual: 2 rows, expected: >= 3)

  Batch rules:
    - not-partial: FAILED (actual=2, expected=>= 3)

  Run with --json for full details.
  Or query the audit certificate for the per-rule breakdown.
```

### P0-3. No contract validation at authoring time

**Reproduction (before fix):** `validate-contract` printed only a validity
line and "Rules: N row checks, M batch checks." Invalid contracts surfaced as a
single `FATAL: <pydantic dump>` at run time; nothing warned about missing batch
rules or empty rule sets.

**Fix.** `validate-contract` now reports a structured summary
(`Contract 'X' v1.0.0 is valid. 3 row rules, 1 batch rule, 1 delivery target.`),
warns about missing row rules / batch rules / delivery targets, and lists every
model or authoring problem per line (missing schema, unknown row or batch
columns, duplicate rule IDs, target outside allowed roots, malformed JSON) with
its field path.

---

## 4. What was broken (P1) — evidence and fixes

### P1-4. `--no-drift`

Drift could only be influenced through environment variables, which is
awkward in CI and scheduled jobs. `datashell run --no-drift` now disables drift
for that run regardless of `DRIFT_ENABLED`, with the human output stating which
mode is active.

### P1-5. Delivery status indicator

The CLI printed `Terminal Status: LOADED` and `Delivered rows: 900`, but
nothing stated the operational fact plainly. The CLI now prints:

- `OK Data was published to: <path>` on `LOADED`
- `X No data was published. All rows were either refused or held.` on `REJECTED`
- `X Engine failure. No data was published.` on `FAILED`

### P1-6. First-run documentation

The README jumps from architecture to API without a guided first run, and the
previous documentation demonstrated `run` against a contract whose drift
behavior was surprising. `docs/FIRST_RUN.md` now covers a 5-minute quickstart,
stage explanation, ledger reading, contract authoring, certificate
verification, and pitfalls.

---

## 5. Residual risk and open follow-ups

1. **Drift is off by default.** Feeds that want cross-run protection must opt
   in. The product risk has shifted from false rejection to silent drift
   blindness; the roadmap tracks a guided enablement flow (see
   `PRIORITIZED_PRODUCT_ROADMAP.md`).
2. **Rejection reason is truncated to the ledger column length** in one place
   (`[:1000]`) for failed-before-delivery runs; the CLI reads the in-memory
   ledger, so it is not affected, but ledger consumers should be aware.
3. **No end-user telemetry.** Usability findings in this repo are derived from
   source review and controlled reproduction, not observed production users.
   See `TARGET_USER_AND_JOB.md` and `CUSTOMER_DISCOVERY_PLAN.md` for the
   evidence gaps and the plan to close them.
4. **Benchmark numbers are unverified on this machine.** The README explicitly
   ships no stored results; `CLAIMS_AND_BENCHMARKS.md` tracks every claim and
   its verification status.
5. **Single replica only.** Credential registry and rate limiter remain
   process-local (documented; unchanged by this workstream).
