# Target User and Job

**Status:** Hypotheses, not findings. No customer interviews have been conducted
for this document. Every statement below is a falsifiable hypothesis with an
explicit evidence gap; treat it as a discovery plan input, not market research.

---

## 1. Product in one sentence

Data Shell is a contract-driven boundary gateway: it validates partner/vendor
feeds (and outbound regulatory submissions) **before** they land, withholds
non-conforming rows, and produces a signed, independently verifiable
certificate of what crossed the boundary.

The product's wedge is not "another data quality tool" — it is **the write
gate**: bad data never lands, and the proof is cryptographic.

---

## 2. Segmented hypotheses

### Segment A — Data platform teams enforcing inbound vendor feeds (primary)

- **Profile:** 50–2,000 employees; a central data platform team (2–15 people);
  Snowflake/BigQuery/Databricks warehouse; ingestion via Fivetran/Airbyte/
  custom scripts; 5–100 external partner feeds.
- **Trigger:** A partner feed corrupts a downstream dashboard or settlement
  total; the team is asked "why did this get in?" and cannot answer quickly.
- **Job to be done:** "When a partner feed arrives, I want to block bad rows
  and prove what happened, so that downstream numbers stay correct and I can
  answer an auditor or partner without a forensic project."
- **Current workaround:** dbt tests + ad-hoc scripts + manual Slack
  reconciliation; failures are discovered downstream in reports.
- **Willingness to pay hypothesis:** High if the certificate shortens partner
  disputes; medium for pure row-drop.
- **Evidence gap:** No interviews. Unknown whether partner disputes are a
  budget-owning pain or an occasional nuisance.

### Segment B — Data quality / governance engineers (secondary)

- **Profile:** Owns data quality tooling (Great Expectations, Monte Carlo,
  Soda, custom frameworks); measured on incident count and mean time to
  detect.
- **Job to be done:** "I want quality checks that are contract-defined, run at
  ingest, and produce evidence, not another dashboard of red squares."
- **Adoption blocker hypothesis:** Overlap with existing tooling; value must
  be the *refusal + proof* rather than metrics.
- **Evidence gap:** Unknown willingness to replace or complement incumbent
  tools.

### Segment C — Regulated outbound filers / compliance engineering (tertiary)

- **Profile:** Financial services, insurance, healthcare; submissions to
  regulators (returns, claims, trade reports) with strict deadlines and
  personal liability for accuracy.
- **Job to be done:** "Before I file, I need to know that every record was
  validated and be able to hand an examiner a verifiable record of the
  filing gate."
- **Current workaround:** Four-eyes reviews, spreadsheets of control totals,
  bespoke reconciliation scripts.
- **Evidence gap:** Procurement/compliance requirements (retention, key
  custody, audit format) are unvalidated; likely the highest-value but
  slowest-moving segment.

### Segment D — Analytics/BI engineers automating a scheduled feed (broader, low intent)

- **Job to be done:** "My nightly CSV load stopped working; give me a clearer
  error."
- **Fit:** Uses only `run` + `--json`; unlikely to buy standalone, but this is
  the population that reaches the product through search and evaluates it in
  the first 10 minutes. Usability for this segment is table stakes.

---

## 3. Prioritized jobs-to-be-done

| # | When... | I want to... | So I can... | Hypothesis strength |
|---|---------|--------------|-------------|---------------------|
| JTBD-1 | a partner feed arrives | block rows that violate a declared contract | keep bad data out of the warehouse | Strong (core product) |
| JTBD-2 | a run is rejected | see exactly which rule and how many rows failed | fix the feed or the contract without reading source code | Strong (observed friction) |
| JTBD-3 | data was delivered | prove what was delivered, when, and unchanged | answer auditors/partners and detect tampering | Strong (core differentiator) |
| JTBD-4 | my feed shape changes legitimately | re-baseline without deleting audit history | keep drift useful instead of turning it off | Medium (new capability) |
| JTBD-5 | I write a contract | know it is valid before a scheduled run | avoid 2 a.m. failures | Strong (observed friction) |
| JTBD-6 | a scheduled job fails | get a machine-readable diagnosis into CI/Slack | automate response | Medium |
| JTBD-7 | my team grows | run isolated tenants with per-tenant roots | host multiple teams safely | Weak (enterprise pull unverified) |

---

## 4. Anti-personas (not the target right now)

- Teams looking for a data catalog or lineage graph (Data Shell records lineage
  per run, but it is not a catalog).
- Teams wanting transformation/ELT (the product deliberately refuses to
  transform; tagging/transformations happen downstream).
- Streaming/real-time pipelines (the product is batch/micro-batch at the
  boundary).
- Teams without any contractable external boundary (internal tables only).

---

## 5. Evidence gaps and how to close them

| Gap | Question that closes it | Method |
|-----|------------------------|--------|
| Do teams actually reject-and-quarantine, or only alert? | "Walk me through the last bad feed. What happened to the rows?" | 12–15 interviews (see `CUSTOMER_DISCOVERY_PLAN.md`) |
| Is the cryptographic certificate a budget driver or a nice-to-have? | "Tell me about the last time you had to prove what you sent a partner/regulator." | Interviews + artifact review |
| Does drift have real utility after the default change? | "How do you catch volume anomalies today?" | Interviews; watch opt-in rate after release |
| Who signs and who operates? | "Who owns the ingestion job? Who gets paged?" | Two-persona interviews |
| Willingness to run a boundary service vs a library? | "What new infrastructure is acceptable for this problem?" | Interviews; procurement constraints |
| Is CLI acceptable or is UI required? | "Where do you run validation today — CI, scheduler, notebook?" | Interviews + usability test |

---

## 6. Working positioning (to be validated)

> **For** data platform teams who ingest external feeds, **Data Shell** is a
> contract-driven write gate **that** refuses non-conforming rows before they
> land and publishes a signed certificate of exactly what crossed the boundary.
> **Unlike** dbt tests or observability platforms, which report after data is
> already in the warehouse, Data Shell blocks at ingestion and proves the
> result.

**Falsification criteria.** Abandon or reposition if, across 10+ interviews,
fewer than 3 participants can describe a rejected/quarantined-row workflow
they own, and fewer than 2 can describe a time they had to *prove* feed
integrity to a third party. In that case the certificate is a feature of an
observability product, not a product of its own.
