# Data Shell — Publication Generation Contract

**Version:** 1.0
**Date:** 2026-10-20
**Purpose:** state precisely what the publication model guarantees, which
single operation is the commit point, and which readers are bound by it.

This document exists because the guarantee must not exceed the
implementation. Everything below is enforced by code and covered by
`tests/test_publication_coherence.py` and `tests/test_ledger_outbox.py`.

---

## 1. The publication model in one paragraph

A run publishes three artifacts: the **delivered dataset** at the contract's
delivery target, the **immutable lineage copy** named `<run_id>_<target_name>`,
and the **audit certificate** named `<run_id>_audit_certificate.json`. They are
staged, fsynced, and renamed into place under a write-ahead journal and an
`fcntl` lock, so a crash mid-publication is rolled back to the last committed
state on the next attempt. After the renames are durable, a single
**generation pointer** file is swapped into place; that swap is the commit
point.

---

## 2. The commit point

**The commit point is one `os.replace` of the file `<delivery-target>.generation.json`.**

That file records the committed `run_id` (plus the tenant, contract
name/version, audit path, and lineage path). It is written:

- only after every artifact of the new generation is renamed and fsynced, and
- only once, at the end of the transaction, and
- atomically, so a reader never observes a partially written pointer.

`datashell/core/commit.py` provides the mechanism:

| Function | Role |
|---|---|
| `generation_pointer_path(target)` | The pointer path for a delivery target |
| `read_generation_pointer(target)` | Read the committed generation |
| `resolve_committed_generation(target)` | The committed `run_id`, or `None` |
| `committed_generation_status(certificate)` | The shared reader contract used by **every** supported surface |
| `_recover_locked(journal_path)` | On recovery, repairs the pointer from the durable journal when the journal is in the `committed` state |

**What the pointer is NOT:** it is not a lock, and it is not enforced against
readers that ignore it. It is a *commit marker* that lets a reader determine
which generation is current.

---

## 3. What each artifact means

| Artifact | Mutability | Meaning |
|---|---|---|
| Immutable lineage copy `<run_id>_<target_name>` | Never overwritten — named by `run_id` | **The durable evidence of one run.** The certificate binds its digest. It survives any later run. |
| Audit certificate `<run_id>_audit_certificate.json` | Never overwritten — named by `run_id` | The signed statement about that run. Survives any later run. |
| Delivery target (contract-defined path) | **Overwritten by each successful run** | A convenience pointer to the *latest* delivery. Reading it directly may yield a superseded generation. |
| Generation pointer `<target>.generation.json` | Replaced atomically per run | Names the committed generation. |

**Consequence:** a run's evidence is stable. A certificate for run A remains
`AUTHENTIC_AND_UNMODIFIED` after run B publishes to the same target, because
the certificate binds the immutable lineage copy. The verifier reports the
shared mutable target as `SUPERSEDED_OR_MODIFIED` rather than failing — that
is correct behaviour, not a weakened check.

---

## 4. The reader contract

**Every supported reader resolves the committed generation before treating a
certificate as "the current delivery".**

| Surface | Resolves the pointer? | How |
|---|---|---|
| `datashell verify <cert>` | **Yes** | Prints `committed generation: <run_id> (this certificate: yes\|no)` |
| `GET /api/v1/runs/{run_id}/certificate` | **Yes** | Returns `committed_generation` and `is_committed_generation` alongside the certificate and verification report |
| Settlement calculation | Yes (by construction) | Reads the run's *own* immutable certificate and lineage evidence from the ledger; the certificate is verified before any amount is calculated |
| Ledger query APIs | Yes (by construction) | Return per-run, immutable artifact paths — never a mutable "current" path as a substitute for a run's evidence |
| Airflow operators | Yes (by construction) | Return the run's ledger record (status, counts, footing, per-run artifact paths) |
| Public demo UI | Yes (by construction) | Reports the run's own outcome and manifest |

The CLI and the HTTP endpoint share **one implementation**
(`committed_generation_status`), so they cannot drift apart; a regression test
asserts they agree.

**What "yes (by construction)" means:** those readers consume a specific
run's immutable evidence, so the question "is this the current generation?"
does not arise for them. The two surfaces that can be handed a certificate and
asked "is this current?" both answer it explicitly.

---

## 5. The documented limitation

**An external consumer that reads the delivery target by path — without
resolving the generation pointer — can observe a superseded generation.**

This is not a defect in the commit protocol; it is a consequence of the
delivery target being a "latest delivery" convenience path while the durable
evidence lives in the per-run lineage copy. The product cannot enforce how an
external consumer reads a file it has filesystem access to.

**Mitigations available to such a consumer:**

1. Resolve `<target>.generation.json` first (the supported protocol).
2. Verify the certificate before consuming the data. The verifier compares the
   certificate's digest against the bytes actually present, so a consumer that
   reads the active target but verifies the certificate will **detect** that it
   is holding a superseded set rather than silently accepting it.
3. Consume the immutable lineage copy `<run_id>_<target_name>`, which is bound
   to the certificate and never changes.

**This limitation is accepted for the controlled pilot** and is recorded as
RR-008 in `docs/production/RESIDUAL_RISK_REGISTER.md`. It is a customer-facing
contract statement, not an internal defect, and it must appear in the
onboarding material for any consumer of delivered data.

---

## 6. What is *not* claimed

- **Not claimed:** that the three artifacts are renamed atomically. They are
  three separate renames; the *pointer* is the atomic commit point.
- **Not claimed:** that a reader which ignores the pointer is protected.
- **Not claimed:** that the pointer is a lock or prevents concurrent writers.
  Writers are serialised by the journal lock; the pointer only records which
  generation won.
- **Not claimed:** that a certificate proves the data was truthful or the
  computation correct. See the cryptographic evidence model in
  `docs/production/SECURITY_REVIEW_PACKAGE.md` § 5.

---

## 7. Verification

```bash
python -m pytest tests/test_publication_coherence.py tests/test_ledger_outbox.py -q
```

Covers: the pointer as commit point, crash-before-swap leaving the old
generation visible, recovery repairing a stale pointer, serial publication
convergence, the CLI/API reader contract and their agreement, per-run
immutable evidence surviving supersession, per-run outbox isolation, and
refusal to commit a run whose artifacts were never published.
