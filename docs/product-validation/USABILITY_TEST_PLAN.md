# Usability Test Plan

**Purpose:** Measure whether a first-time user can complete the core Data Shell
jobs without source-code knowledge, and whether the P0/P1 fixes actually
changed outcomes. This plan replaces assertion with observation.

**Version under test:** 1.0.1 with product-validation fixes.
**Test type:** Moderated, task-based, think-aloud.
**Sessions per round:** 5–8 (small-sample formative testing; iterate, don't
generalize).

---

## 1. Research questions

| RQ | Question | Tied finding |
|----|----------|--------------|
| RQ1 | Can a new user run the sample dataset and correctly state whether data was published? | P1-5 delivery indicator |
| RQ2 | Given a rejected run, can the user name the failing rule and the expected condition within 2 minutes? | P0-2 rejection messages |
| RQ3 | Can a user validate and repair a broken contract before running it? | P0-3 validate-contract |
| RQ4 | Do users understand when drift applies and what `--no-drift` does? | P0-1 / P1-4 drift changes |
| RQ5 | Can a user verify a certificate and interpret tampering? | Existing verify flow |
| RQ6 | Where do users get stuck in the documented first-run guide? | `docs/FIRST_RUN.md` |

---

## 2. Participants and screening

Recruit 5–8 participants per round matching Segment D or A from
`TARGET_USER_AND_JOB.md`:

- **Required:** writes or maintains data pipelines (Python/SQL), uses a
  scheduler (Airflow/cron/CI), has not used Data Shell before.
- **Desired variation:** at least one participant with no Polars/DuckDB
  familiarity; at least one who primarily uses a GUI.
- **Exclude:** anyone who has contributed to the repository.

Compensate time at market rate. Obtain recording consent; anonymize.

---

## 3. Setup

- Clean checkout in a fresh container; participant's own machine if remote.
- Facilitator gives the setup prompt only:

  > "Data Shell enforces data contracts at your ingestion boundary. Your
  > goal is to get the bundled sample data through the gate and understand
  > what happened. `docs/FIRST_RUN.md` is available. Think aloud as you go.
  > I can't help with answers, but I can help if something is broken."

- Provide task cards one at a time. Do not demonstrate anything first.
- Record screen, audio, and time stamps. Note every stall > 30 seconds.

---

## 4. Tasks

| # | Task card | Success criterion | RQ |
|---|-----------|-------------------|----|
| T1 | "Install Data Shell and run the sample dataset through the vendor-settlements contract." | Run completes; participant sees the result | RQ1 |
| T2 | "Is the delivered file available? Where is it?" | Points to the `OK Data was published to:` path and can open/describe it | RQ1 |
| T3 | "This feed was rejected. Find out which rule failed and what was expected." | States `not-partial`, actual 2, expected ≥ 3 within 2 minutes | RQ2 |
| T4 | "Fix the contract in `/tmp/broken_contract.json` so validation passes." | `validate-contract` exits 0; participant explains the fix | RQ3 |
| T5 | "A scheduled job started rejecting after last week's small test run. Explain why and make it stop, without disabling safeguards permanently." | States baseline cause; uses `--no-drift` or `DRIFT_BASELINE_RESET` appropriately | RQ4 |
| T6 | "Prove the delivered file has not been modified since delivery." | Runs `verify`; explains `AUTHENTIC_AND_UNMODIFIED` | RQ5 |
| T7 | "Someone changed one byte of the delivered file. Detecting it is your job." | Runs `verify`; identifies `TAMPERED_UNDERLYING_ARTIFACTS` | RQ5 |
| T8 | "Show the last five runs for this tenant and tell me how many rows were delivered yesterday." | Uses `ledger runs` / `ledger summary` correctly | RQ1 |

**Synthetic fixtures for T3–T5** (created by the facilitator, not the
participant):

- `broken_contract.json`: batch rule references an undeclared column.
- Two-row feed + `MIN_ROW_COUNT: 3` contract for the rejection task.
- A seeded 5-row history followed by a 900-row feed for the drift task.

---

## 5. Metrics

### Behavioral (primary)

| Metric | Definition | Target |
|--------|------------|--------|
| Task success | Completed without facilitator intervention | ≥ 80% per task (T1, T3, T6) |
| Time on task | Median seconds from card read to correct outcome | T1 ≤ 300s, T3 ≤ 120s, T6 ≤ 90s |
| Error recovery | Participant recovers from an induced error without help | ≥ 70% for T4 |
| Critical confusion | Participant cannot state whether data was published (T2) | 0 participants |
| Help-seeking | Explicit requests for the answer | ≤ 1 per task |

### Subjective

- Single Ease Question (SEQ, 1–7) after each task; target median ≥ 5.5 for
  T1/T3/T6.
- NASA-TLX (short) only if sessions allow.
- Preference: "Would you use this in a scheduled pipeline? Why/why not?"

### Qualitative

- Verbatim confusion quotes, tagged by UI element.
- Documentation gaps: each unassisted failure mapped to a missing sentence or
  unclear output.

---

## 6. Analysis and reporting

- Report per-task success and median times with ranges (n is small; no
  p-values).
- Classify failures: **product** (output/index/flag missing), **docs**
  (missing/incorrect sentence), or **participant skill** (out of target
  profile).
- Any product failure on T1–T3 is a release blocker regardless of averages.
- Findings feed `docs/product-validation/USABILITY_AUDIT.md`; fixes are
  scheduled in `PRIORITIZED_PRODUCT_ROADMAP.md`.

### Results template

```markdown
## Round <N> — <date> — <n> participants

| Task | Success | Median time | SEQ (median) | Top failure mode |
|------|---------|-------------|--------------|------------------|
| T1   |         |             |              |                  |
| ...  |         |             |              |                  |

### Blockers
1. ...

### Documentation fixes
1. ...

### Quotes
> "..."
```

---

## 7. Ethics and logistics

- No personal or customer data in test fixtures; use the bundled sample and
  synthetic feeds only.
- Recording is optional; delete on request.
- Facilitator must not lead the witness ("try `--no-drift`") — only clarify
  the task card.
- Run T5 last among the drift tasks; it is the most product-specific.

## 8. Pre-flight checklist (run before every round)

- [ ] `pytest tests/ -q` passes on the build under test
- [ ] `datashell demo` completes
- [ ] Sample run delivers 900 rows with exit code 0
- [ ] Fixtures for T3–T5 created and verified
- [ ] Screen/audio recording works; timer visible
- [ ] Consent script ready
