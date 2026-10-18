# Customer Discovery Plan

**Goal:** Replace the hypotheses in `TARGET_USER_AND_JOB.md` with evidence from
12–15 problem interviews within four weeks, and decide whether the write-gate
wedge (refuse + prove) is a budgeted pain for the target segments.

**Non-goals:** Demo/sales calls, feature prioritization by committee, or
pitching. These are discovery interviews about past behavior. We do not show
the product until the final 5 minutes (if at all), and only after the problem
has been described in the participant's own words.

---

## 1. Hypotheses to test

| # | Hypothesis | Falsified if... | Primary questions |
|---|------------|-----------------|-------------------|
| H1 | A rejected/quarantined-row workflow is owned by someone in data platform teams | Fewer than 3 of 12 can describe one they own | Section A |
| H2 | Proving feed integrity to a partner/auditor is a recurring, painful task | Fewer than 2 of 12 describe a specific incident | Section B |
| H3 | Current tooling (dbt tests, scripts) fails to prevent bad rows from landing | Participants cannot name a time bad rows reached a report | Section A/C |
| H4 | Teams will accept a boundary service (not just a library) | Infrastructure/security veto described as prohibitive | Section C |
| H5 | Certificate/evidence, not row-dropping, is the differentiator worth paying for | All value assigned to blocking alone | Section B |
| H6 | CLI/scheduler fit is acceptable; UI is not a blocker | CLI-only is a hard veto | Section C |

Record each hypothesis as **supported / partially supported / falsified** with
verbatim quotes (anonymized).

---

## 2. Screening criteria

**Must have (all):**

- Works on a team that ingests at least one external or partner data feed
  (vendor, customer, agency, affiliate), **or** files outbound data to a
  regulator/partner client.
- Has a say in or detailed knowledge of how that feed is validated and loaded
  (data engineer, analytics engineer, platform lead, data quality engineer,
  compliance engineer).
- Can describe a concrete process (not a hypothetical) they personally ran in
  the last 12 months.

**Target mix (12–15 total):**

| Segment | Target count | Notes |
|---------|--------------|-------|
| A — Data platform, inbound vendor feeds | 6–7 | Primary wedge |
| C — Regulated outbound filers | 3–4 | Highest-value hypothesis; hardest to schedule |
| B — Data quality owners | 3–4 | Tests overlap/adoption blocker |
| D — Analytics engineers automating feeds | 1–2 | Tests whether self-serve is viable |

**Exclusions:**

- Pure students/hobbyists.
- Vendors selling competing tooling (unless positioned as design partners for
  ecosystem integration).
- Anyone who cannot discuss specifics due to NDA (ask for a sanitized version).

**Sourcing channels:** warm intros via existing contacts; data engineering
Slack/Discord communities; local meetups; LinkedIn outreach with a specific
problem statement (not "would you like to try our product"); referrals from
early interviewees ("who else has this problem?").

**Compensation:** $50–$100 gift card or charitable donation per 45-minute
interview where appropriate and legal.

---

## 3. Interview protocol (45 minutes)

Start recording only with explicit consent. Take notes in this structure.

### Opening (3 min)

1. "Thanks — I'm researching how teams handle [external feed integrity]. I'm
   not here to pitch anything. Tell me a bit about your role and the data your
   team handles."

### Section A — Recent concrete incident (12 min)

2. "Think about the last time a partner/vendor file showed up broken or
   wrong. Walk me through what happened, step by step."
   - What did you notice first? Who noticed?
   - What did you do in the first hour? The first day?
   - Where did the data end up?
3. "How often does something like that happen? Has it ever reached a
   production report or a customer?"
4. "What would have had to be true for it never to land?"

**Listen for:** ownership, time spent, downstream blast radius, workarounds.

### Section B — Proof and evidence (10 min)

5. "Tell me about the last time you had to show someone — a partner, auditor,
   customer, or exec — exactly what data you received or sent."
6. "What did you hand them? How long did assembling it take?"
7. "What happens if someone questions whether the data changed after the
   fact?"

**Listen for:** artifacts (spreadsheets, screenshots, emails), time cost,
disputes, regulatory pressure.

### Section C — Workflow, tooling, constraints (12 min)

8. "Walk me through how a feed goes from landing zone to a table someone
   trusts. What checks run, and where?"
9. "Which checks block, and which just alert? Who decided?"
10. "What's in your CI/scheduler today — dbt, Airflow, cron, something else?
    Where would a new validation step need to live?"
11. "If a tool refused to publish a bad feed at 2 a.m., what should happen
    next — page someone, quarantine, retry?"
12. "What's the security review process for a new service? What would kill it?"

**Listen for:** block-vs-alert reality, integration surface, operational
tolerance for refusals.

### Section D — Willingness and priority (5 min)

13. "If you had a magic wand to fix one thing in this whole area, what would
    it be? Why isn't it fixed already?"
14. "Who owns the budget for this? What would you have to show them?"
15. (Optional, only after all of the above) "We're building a write-gate that
    refuses non-conforming rows and issues a signed certificate. Is that
    solving anything you just described, or something else?"

### Closing (3 min)

16. "Is there anything I should have asked and didn't?"
17. "Who else should I talk to who deals with this?"

---

## 4. Analysis

- Transcribe within 24 hours; tag quotes with hypothesis IDs (H1–H6).
- Score each hypothesis after 6 interviews and again after 15:
  **Supported** (≥5 of 12 with concrete evidence), **Partial** (3–4),
  **Falsified** (<3).
- Disconfirming evidence gets equal weight; a single vivid quote is not
  evidence.
- Separate **problem frequency** (how often) from **problem severity** (cost
  when it happens) — either can justify action, but they imply different
  products (automation vs. proof).
- Decisions gate on thresholds, not vibes:
  - If H1 and H3 are supported, keep the write-gate wedge.
  - If H5 is supported but H1 is not, reposition around evidence/attestation
    rather than prevention.
  - If H4 is falsified (service unacceptable), evaluate a library/CI-action
    packaging.

## 5. Logistics

| Item | Detail |
|------|--------|
| Interview length | 45 minutes |
| Cadence | 3–4 per week for 4 weeks |
| Roles | One interviewer, one notetaker (rotating) |
| Tools | Consent script, recording (optional), shared notes template |
| Ethics | Ask permission to record; anonymize names/companies in the repo; no confidential numbers without written approval |
| Output | `docs/product-validation/DISCOVERY_FINDINGS.md` (to be created) with per-hypothesis verdicts and top 10 verbatim quotes |

**Do not** schedule product demos during discovery. If a participant asks to
try it, offer a separate session after the research phase.
