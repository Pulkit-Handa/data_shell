# Usability Audit

**Date:** 2026-10-17
**Method:** Heuristic review + controlled reproduction of first-run and
failure paths against Data Shell 1.0.1, using the bundled sample dataset and a
clean checkout. Severity uses a modified Nielsen scale.

| Severity | Meaning |
|----------|---------|
| S1 — Blocks task | User cannot complete the core job or is misled into a wrong decision |
| S2 — Major friction | Task completes but only with source-code-level knowledge or significant error recovery |
| S3 — Moderate friction | Slows the task; workaround exists |
| S4 — Cosmetic | Polish |

---

## 1. Workflow map and friction points

### Stage 0 — Install and orient (0–10 minutes)

| ID | Finding | Severity | Status |
|----|---------|----------|--------|
| U0.1 | README begins with a full architecture diagram before any guided task; a newcomer cannot tell what to type first | S3 | Partially addressed: `docs/FIRST_RUN.md` provides a 5-minute path |
| U0.2 | `datashell demo` is excellent but self-contained; it never shows a real file-based run with a certificate on disk | S3 | Addressed by FIRST_RUN quickstart (Steps 3–4) |
| U0.3 | No statement of expected exit codes until line 225 of README | S3 | Addressed: FIRST_RUN documents 0/1/2 |

### Stage 1 — Authoring a contract

| ID | Finding | Severity | Status |
|----|---------|----------|--------|
| U1.1 | Invalid contracts failed at run time with a multi-line pydantic dump prefixed `FATAL:` — no field path a non-Python user recognizes | S2 | **Fixed**: per-field, per-line errors with paths |
| U1.2 | No warning when a contract had zero batch rules (the exact gap that makes silent partial feeds possible) or zero row rules | S3 | **Fixed**: explicit warnings |
| U1.3 | `validate-contract` printed no summary useful for review ("is this what I meant?") | S3 | **Fixed**: counts of rules and targets |
| U1.4 | Delivery-target sandbox violations surfaced only at run time | S2 | **Fixed**: validated at authoring time with the offending path named |

### Stage 2 — Running enforcement

| ID | Finding | Severity | Status |
|----|---------|----------|--------|
| U2.1 | A valid sample delivery was rejected because of an unrelated 5-row test baseline; the user saw only "Violations exceeded declared contract gate" | **S1** | **Fixed**: drift opt-in (`DRIFT_ENABLED=false` default) + `--no-drift` |
| U2.2 | Rejection gave no rule, count, or expectation — the single most expensive friction point in the audit | **S1** | **Fixed**: specific reason + rule/batch/structural breakdown |
| U2.3 | Human output never stated plainly whether data was published | S2 | **Fixed**: `OK Data was published to: ...` / `X No data was published...` |
| U2.4 | Drift mode was invisible; users could not tell whether the rejection came from quality or history | S2 | **Fixed**: mode line (`Drift: disabled (DRIFT_ENABLED default is false)`) and the reason names drift checks |
| U2.5 | `--json` omitted the rejection reason and batch/drift evaluations, so CI had to parse certificates | S2 | **Fixed**: JSON now includes `rejection_reason`, `batch_evaluations`, `drift_evaluations` |

### Stage 3 — Diagnosing and recovering

| ID | Finding | Severity | Status |
|----|---------|----------|--------|
| U3.1 | No documented path from "run rejected" to "which rows are in quarantine and why" | S2 | Addressed: rejection output points to `--json`/certificate; FIRST_RUN explains quarantine artifacts; ledger `quarantine` search exists |
| U3.2 | Re-baselining drift required deleting the history DB (destructive and undocumented) | S2 | **Fixed**: `DRIFT_BASELINE_RESET` watermark, non-destructive |
| U3.3 | Structural failures (`amount:Float64: 3`) were visible in demo output but not on a rejected `run` | S3 | **Fixed**: structural refusal section in rejection output |

### Stage 4 — Proving delivery

| ID | Finding | Severity | Status |
|----|---------|----------|--------|
| U4.1 | `verify` output is terse but sufficient; no friction found | — | No change |
| U4.2 | First-time users do not know where certificates live or how to keep them | S3 | FIRST_RUN documents default audit paths and explicit `--audit` |

### Cross-cutting

| ID | Finding | Severity | Status |
|----|---------|----------|--------|
| U5.1 | Errors printed to stderr and summaries to stdout is correct for scripting, but rejection *details* previously appeared only as one stderr line | S3 | Structured details remain on stderr; status indicator on stdout; both scripts and humans served |
| U5.2 | Sandbox denial messages can be long lists of roots | S4 | No change (security clarity preferred over brevity) |
| U5.3 | Timestamps and row counts use inconsistent separators (`1,000` vs `1000`) across outputs | S4 | Backlog |

---

## 2. The four highest-friction scenarios (before/after)

### Scenario A — "My valid feed was rejected"

**Before (S1):**

```text
[!] FEED REJECTED: Violations exceeded declared contract gate.
```

Cause: `sum_amount` drift vs a 5-row test baseline. Time to diagnosis without
source access: unbounded.

**After:**

```text
Drift: disabled (DRIFT_ENABLED default is false)

Terminal Status: LOADED
Delivered rows:  900
OK Data was published to: /tmp/out.parquet
```

and, when drift is explicitly enabled and trips:

```text
[!] FEED REJECTED: drift-metric-sum_amount failed (Metric 'sum_amount' drifted by 10070.6% (baseline: 6068.25, current: 617175.00).)

  Drift checks:
    - drift-metric-sum_amount: FAILED — ...
```

### Scenario B — "Why did my batch rule reject?"

**Before (S1):** generic sentence.

**After:**

```text
[!] FEED REJECTED: not-partial batch rule failed (actual: 2 rows, expected: >= 3)
```

### Scenario C — "Is my contract valid before I schedule it?"

**Before (S2):** `FATAL: ...` only at run time.

**After:**

```bash
$ datashell validate-contract -c contract.json
✓ Contract 'my-feed' v1.0.0 is valid. 3 row rules, 1 batch rule, 1 delivery target.
  WARNING: No batch rules defined — consider adding MIN_ROW_COUNT for feed volume validation
```

### Scenario D — "Did anything actually get published?"

**Before (S2):** infer from `Terminal Status: LOADED` + row counts.

**After:** explicit `OK`/`X` line.

---

## 3. Recommended next usability work (not in this release)

1. **Interactive drift onboarding**: after N stable deliveries, suggest
   enabling drift with thresholds derived from observed variance, instead of
   leaving it globally off.
2. **`datashell explain --run <id>`**: render the certificate's rejection and
   rule breakdown without the user opening JSON.
3. **Quarantine convenience**: `datashell ledger show <run>` already lists
   paths; add a `--open`/`--export` affordance in docs and a CSV export.
4. **Consistent number formatting** across CLI surfaces.
5. **Error catalog**: stable error codes (for example `DS-DRIFT-001`) so CI
   can branch without string matching.

## 4. Method limitations

- Findings come from internal reproduction and code review, not observed users.
  Severity assignments are educated estimates.
- No accessibility review (screen reader, color contrast) was performed.
- The CLI was tested on Linux only; Windows path behavior is unverified.
