# Prioritized Product Roadmap

**Date:** 2026-10-17
**Baseline:** Data Shell 1.0.1 after the product-validation fixes.
**Prioritization rule:** correctness of the core job first (P0), then
usability of the core job (P1), then expansion (P2). Rejections are explicit,
with a reason and a revisit trigger.

Effort scale: S (≤1 day), M (2–5 days), L (1–3 weeks).

---

## P0 — Correctness (done in this workstream)

| # | Item | Why | Effort | Status |
|---|------|-----|--------|--------|
| P0-1 | Drift opt-in (`DRIFT_ENABLED=false`), `DRIFT_BASELINE_RESET`, runner gating in `run_file`/`run_records`/`run_streaming` | A valid 900-row sample delivery was rejected against a 5-row test baseline | S | **Done** |
| P0-2 | Actionable rejection output: reason, rule breakdown, structural refusals, batch results, drift failures, `--json` hint | Rejections were undiagnosable from CLI output | S | **Done** |
| P0-3 | Authoring-time contract validation: summary, warnings, per-field errors | Contract mistakes surfaced at run time as pydantic dumps | S | **Done** |

**Exit criteria met:** sample dataset delivers 900 rows, exit 0; rejection
message names the failing rule and counts; `validate-contract` reports summary
and warnings; `pytest tests/ -q` → 264 passed.

---

## P1 — Usability (done in this workstream)

| # | Item | Why | Effort | Status |
|---|------|-----|--------|--------|
| P1-1 | `datashell run --no-drift` | Per-run bypass without env juggling | S | **Done** |
| P1-2 | Delivery status indicator (`OK`/`X` lines) | Users could not tell if data was published | S | **Done** |
| P1-3 | `docs/FIRST_RUN.md` | Newcomers had no guided path; drift was a trap | S | **Done** |
| P1-4 | JSON summary includes `rejection_reason`, `batch_evaluations`, `drift_evaluations` | CI had to parse certificates for failure detail | S | **Done** |

---

## P2 — Next (candidate, prioritized)

| # | Item | User problem | Effort | Prerequisites / notes |
|---|------|--------------|--------|-----------------------|
| P2-1 | Drift enablement assistant: after N stable deliveries, suggest `DRIFT_ENABLED=true` with thresholds estimated from observed variance; `datashell drift suggest` | Drift is now safe but invisible; risk shifts to silent blindness | M | Requires retained metrics (already persisted) and a CLI/report surface |
| P2-2 | `datashell explain --run <id>` / `certificate explain` | Rejection and rule breakdown live in JSON/certificates; humans must open files | S | Data already in certificate + ledger |
| P2-3 | Quarantine export and preview: `datashell quarantine export --format csv` and row preview | Triage currently requires opening Parquet/JSONL manually | M | Ledger quarantine index exists |
| P2-4 | Stable error codes (`DS-DRIFT-001`, `DS-SANDBOX-003`, …) in CLI and API errors | CI must string-match to branch | S | Define catalog; keep human text |
| P2-5 | Contract authoring helpers: `datashell contract init --from sample.jsonl` and a JSON schema for editors | Hand-writing schema + rules is error-prone | M | Must not weaken validation |
| P2-6 | Multi-artifact audit pack: one command to zip certificate + artifacts + verify instructions for external auditors | "Prove it" is core; assembling evidence is manual | M | Sandbox and retention rules apply |
| P2-7 | Warehouse delivery targets (Snowflake/BigQuery/S3) behind the same gate | Users must currently stage to local Parquet then move it themselves | L | Security review; credential storage; atomicity semantics per sink |
| P2-8 | Head-to-head benchmark vs dbt tests and Great Expectations on identical data | Claims need third-party-relevant evidence | M | Harness exists; define fair methodology |
| P2-9 | Windows path/sandbox support verification | Untested platform | M | CI matrix; sandbox review |
| P2-10 | Customer-facing onboarding telemetry (opt-in) for first-run completion | No evidence about where users drop off | S | Privacy review; off by default |

---

## P3 — Longer term (tracked, not scheduled)

- Shared credential registry and distributed rate limiting (remove the
  single-replica constraint; split-brain safe design required).
- Streaming/Kafka boundary ingestion with the same reconciliation and
  certificate model.
- Contract versioning and diff review (`contract diff v1 v2`) tied to ledger
  history.
- Policy bundles for multi-tenant platform teams (per-tenant thresholds and
  notifications).
- SOC 2 / external security attestation if Segment C (regulated filers)
  validates.

---

## Rejected / deliberately deferred

| Item | Decision | Reason | Revisit trigger |
|------|----------|--------|-----------------|
| Auto-delete rejecting rows from sources | **Reject** | Violates immutability and the audit model; sources are not the boundary's property | Never |
| Turning drift back on by default | **Reject** | Reintroduces false rejections; the fix is enablement guidance, not a default flip | A per-contract drift opt-in with sane warm-up could change this |
| ML anomaly detection | **Defer** | High complexity; threshold drift plus batch controls cover the observed cases | When ≥3 users ask after drift enablement |
| Full transformation/ELT engine | **Reject** | Contradicts the "tag and transform downstream" architecture and competitive position | Never (product identity) |
| Multi-replica without shared credential store | **Reject** | Split-brain auth/rate-limit; correctness over convenience | When shared store lands |
| GUI-first redesign | **Defer** | CLI is the operator surface; console exists for review | Usability-test data shows CLI blocks a target segment |
| Auto-fix invalid contracts (guess columns) | **Reject** | Silent coercion is the failure mode the product exists to prevent | Never |

---

## Sequencing rationale

1. **Trust first (P0):** rejections must mean something and delivery must not
   be sabotaged by history. Without this, no other feature matters.
2. **Diagnosis second (P1):** every rejection and contract mistake must be
   explainable without reading source.
3. **Enablement third (P2-1/P2-2):** restore the value of drift and make
   evidence consumable now that the gate is trustworthy.
4. **Reach last (P2-7+):** warehouse targets and platform parity only after the
   core loop is dependable and user-validated; these are the expensive,
   high-risk bets.

Metrics to watch after release: first-run completion rate, rejection →
resolution time proxy (re-run within 24h after rejection), drift opt-in rate,
certificate verification rate, and support questions about rejections.
