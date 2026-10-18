# Verifiable Data Delivery & Settlement

Data Shell answers five questions on one evidence chain, without a spreadsheet
and without trusting a dashboard:

| Question | Answered by |
| --- | --- |
| What was submitted? | Pipeline reconciliation ledger (rows read / refused / held / delivered) |
| What was accepted, held, refused, published? | Boundary ledger + signed delivery certificate |
| Contractual acceptance? | Deterministic acceptance verdict with machine-readable reason codes |
| Amount payable, disputed, penalizable? | Exact minor-unit settlement calculation bound to one immutable contract version |
| Independently verifiable & reproducible? | Signed settlement receipt binding certificate + contract digest + calculation; same stored inputs always reproduce the same result |

Nothing in this subsystem moves money. It calculates obligations, records
draft billing instructions, and produces evidence other parties can verify.

---

## 1. Layer model

```
Data contract (daas/v1)          what a valid batch must contain (existing pipeline)
        │
        ▼
Delivery run (dm_run_…)          one pipeline execution + reconciliation ledger
        │  signed audit certificate (certificate_version 2.0-enterprise)
        ▼
Commercial contract version      pricing, qualification, acceptance, penalties
(ctr_…, v1/v2/…)                 immutable once registered; SHA-256 digest
        │
        ▼
Settlement statement (stl_…)     deterministic calculation for exactly
                                 one run × one contract version
        │
        ├── settlement receipt      signed evidence binding certificate digest,
        │                           contract digest, and full calculation
        └── billing instruction     DRAFT invoice proposal (dry-run or Stripe,
        (bil_…)                     opt-in, never a payment)
```

Key files:

| Component | Module |
| --- | --- |
| Money / arithmetic | `datashell/settlement/money.py` |
| Domain models & schemas | `datashell/settlement/models.py` |
| Pricing calculators | `datashell/settlement/pricing.py` |
| Acceptance evaluation | `datashell/settlement/acceptance.py` |
| Deterministic engine | `datashell/settlement/engine.py` |
| Durable ledger | `datashell/settlement/ledger.py` |
| Contract registry | `datashell/settlement/registry.py` |
| Signed receipts | `datashell/settlement/certificate.py` |
| Orchestration | `datashell/settlement/service.py` |
| Billing abstraction | `datashell/billing/` |

Persistence extends the existing boundary-ledger SQLite database
(`output/datashell_history.db` by default) with `settlements`,
`settlement_events`, `disputes`, `billing_instructions`, and
`contract_versions` tables. Contract *files* remain the registry of record
under `contracts_registry/commercial/tenants/<tenant>/<contract_id>/`, and
every registered version is also recorded in the database so settlements can
bind to a database-backed digest with uniqueness constraints.

---

## 2. Commercial contract schema

A commercial contract is deliberately separate from the `daas/v1` data
contract: the data contract says *what good data looks like*, the commercial
contract says *what it is worth*.

```jsonc
{
  "schema_version": 1,
  "contract_id": "ctr_vendor_settlements_usd",
  "contract_version": 1,
  "buyer_id": "buyer-acme-data",
  "seller_id": "seller-vendor-feed",
  "feed_id": "vendor-settlements",
  "currency": "USD",

  "pricing": {
    "model": "PER_ACCEPTED_UNIT",       // PER_ACCEPTED_UNIT | FIXED_BATCH_FEE |
                                         // TIERED_PRICING | PRORATED_SHORT_DELIVERY |
                                         // HOLD_OR_DISPUTE
    "unit_price_minor": 5,               // integer minor units (USD cents)
    "tiers": [],                         // contiguous half-open [min, max) bands
    "fixed_fee_minor": null,
    "proration_numerator": 1,            // exact rational for short delivery
    "proration_denominator": 1,
    "max_payable_minor": null            // optional contractual cap
  },

  "qualification": {
    "billable_unit": "DELIVERED_ROWS",   // DELIVERED_ROWS | COMPLETED_BATCH |
                                         // UNIQUE_ENTITIES | SOURCE_ROWS_READ
    "unique_key_columns": [],            // required for UNIQUE_ENTITIES
    "include_refused_in_count": false
  },

  "acceptance": {
    "policy": "PARTIAL_WITH_MINIMUM",    // FULL_ACCEPTANCE | MINIMUM_REQUIRED |
                                         // PARTIAL_WITH_MINIMUM | REVIEW_ON_SHORTFALL |
                                         // STRICT_QUANTITY
    "minimum_accepted_units": 1,
    "expected_units": 3,
    "maximum_units": null,
    "allowed_failure_tolerance_bps": 10000,  // basis points of refused+held rows
    "required_validation_rules": [],
    "block_billing_during_dispute": true
  },

  "penalty": {
    "model": "NONE",                     // NONE | FLAT_FEE | PERCENT_OF_GROSS
    "flat_penalty_minor": 0,
    "penalty_percent_bps": 0,
    "applies_to": ["REJECTED", "SHORT_DELIVERY"]
  },

  "effective_at": "2026-01-01T00:00:00Z",
  "expires_at": null,
  "approved_by": ["buyer-acme-data", "seller-vendor-feed"],
  "description": "…"
}
```

### Immutability and the digest

* `contract_digest` is SHA-256 over the canonical JSON payload. `created_at`
  is registration metadata and is excluded, so re-registering the identical
  agreement is idempotent; changing *any* commercial term changes the digest.
* Re-registering the same `(contract_id, contract_version)` with a different
  payload is refused in both the file registry and the database. Corrections
  require a new version.
* A settlement records the concrete version and digest it used. Publishing
  v2 never rewrites a v1 settlement. Resolving “latest” happens **before**
  calculation; after issuance the binding is exact.
* Prices are accepted only as exact values: integer minor units, decimal
  strings, or `Decimal`. Binary floats are rejected outright. A decimal price
  that does not convert exactly at the currency precision fails validation
  instead of silently rounding.

---

## 3. Acceptance semantics

Evaluation is a pure function of run evidence (reconciliation counts, batch
rule outcomes, certificate verification status, drift) and the contract's
acceptance policy.

Integrity gates — any failure here is `REJECTED` with a reason code and
`billing_eligible=False`, regardless of pricing:

| Condition | Reason code |
| --- | --- |
| Reconciliation deficit / footing mismatch | `RECONCILIATION_MISMATCH` |
| Certificate invalid (caught earlier by the service; also guarded here) | `CERTIFICATE_VERIFICATION_FAILED` |
| Run rejected by the policy gate | `BATCH_REJECTED` |
| Engine failure | `CONTRACT_ACCEPTANCE_FAILED` |
| Required artifacts missing | `REQUIRED_ARTIFACT_MISSING` |
| Batch/rule failure | `CONTRACT_ACCEPTANCE_FAILED` |

Quantity and quality gates:

| Condition | Typical outcome |
| --- | --- |
| Qualified < minimum (hard policies) | `REJECTED`, `MINIMUM_QUANTITY_NOT_MET` |
| Qualified > maximum | policy-dependent; `MAXIMUM_QUANTITY_EXCEEDED` |
| Shortfall under `REVIEW_ON_SHORTFALL` / `STRICT_QUANTITY` | `PENDING_REVIEW`, not payable |
| Shortfall under `PARTIAL_WITH_MINIMUM` / `FULL_ACCEPTANCE` | `PARTIALLY_ACCEPTED`, payable, `MANUAL_REVIEW_REQUIRED` |
| Refused+held ratio above tolerance | `PARTIALLY_ACCEPTED`, `MANUAL_REVIEW_REQUIRED` |
| Open dispute | `DISPUTED`, `DISPUTE_OPEN`, not payable |

Statuses:

* Acceptance: `ACCEPTED`, `PARTIALLY_ACCEPTED`, `REJECTED`, `DISPUTED`,
  `PENDING_REVIEW`
* Settlement: `CALCULATED` (reserved), `PENDING_REVIEW`, `READY_FOR_BILLING`,
  `DISPUTED`, `SUPERSEDED`, `CANCELLED`

Only `READY_FOR_BILLING` is billable, and only while no open dispute exists.

Qualified units are derived from evidence by the qualification policy.
Held, refused, duplicated, and unpublished rows are never billable as
`DELIVERED_ROWS`; `COMPLETED_BATCH` bills the batch, `SOURCE_ROWS_READ` bills
reads (opt-in via `include_refused_in_count`, never held rows), and
`UNIQUE_ENTITIES` uses a precomputed distinct count supplied to the service —
the engine never re-scans the dataset.

---

## 4. Money: exact by construction

* All amounts are integer **minor units** (`gross_amount_minor`, USD cents).
  No binary float ever enters a calculation.
* Currency precision is declared per ISO-4217-like table (USD=2, JPY=0,
  BHD=3, default 2).
* Intermediate arithmetic uses exact rationals (`fractions.Fraction`).
* **Rounding happens exactly once**, at the final aggregation stage, using
  `ROUND_HALF_UP` on the rational result — recorded on the statement as
  `rounding_mode` and `currency_precision`.
* Tiers are contiguous half-open `[min, max)` bands; units beyond the last
  bounded band continue at the final band's rate (total, never truncated).
* `max_payable_minor` caps the result; overflow beyond
  `10^15` minor units fails closed with `MoneyValidationError` rather than
  wrapping or approximating.
* Penalties are only those explicitly contracted (`FLAT_FEE`,
  `PERCENT_OF_GROSS`); no implicit SLA penalty exists. `HOLD_OR_DISPUTE`
  pricing can never produce an automatically billing-eligible statement.
* Taxes are **out of scope** for this engine and are not modeled. A deployment
  that needs tax lines must add them downstream of the calculated liability
  and must not mutate the signed statement.

A worked example (the default demo):

```
985,000 qualified delivered rows × 0.05 USD = 4,925,000 minor = 49,250.00 USD
```

The number is derived from verified run evidence; the demo asserts it, it is
never hardcoded into the statement.

---

## 5. Evidence and signed receipts

The delivery certificate cannot contain a settlement that did not exist yet,
so the engine issues a **settlement receipt**: the same signature envelope and
trust semantics as audit certificates, binding:

* the delivery certificate by digest (recomputed, excluded signature block),
* the commercial contract id / version / digest,
* the full settlement statement (calculation inputs and result), and
* a `signature_header` that is part of the signed content.

Verification (`settlement verify`, `POST /api/v1/settlements/{id}/verify`)
performs, in order:

1. signature algorithm + trusted key resolution and signature validity
   (same rules as certificate verification);
2. delivery-certificate digest recomputation and re-verification of the
   certificate through the existing artifact verifier;
3. if the expected statement is supplied, field-by-field equality of every
   settlement-critical field (amounts, units, statuses, contract digest,
   certificate digest, run id).

Tampering with **any** settlement-critical field invalidates the receipt
(`TAMPERED_OR_INVALID_SIGNATURE`), a swapped certificate fails
(`DELIVERY_CERTIFICATE_DIGEST_MISMATCH`), and a forged statement fails
(`SETTLEMENT_EVIDENCE_MISMATCH`).

Algorithms, stated truthfully:

* **Ed25519** when `DATASHELL_ED25519_PRIVATE_KEY` is provisioned — third-party
  verifiable with the published public key.
* **HMAC-SHA256** otherwise — requires the shared secret and is **never**
  labelled third-party verifiable (`third_party_verifiable: false`,
  `requires_shared_secret: true` in both the envelope and verification
  reports).

Reproducibility: the calculation is a pure function of (contract version,
run evidence, certificate status). Given the stored statement row, the
contract version, and the ledger run record, anyone with the same engine
version reproduces the identical calculation. `created_at` is issuance
metadata, not a calculation input.

---

## 6. Settlement invariants

These are enforced in code and covered by tests
(`tests/test_settlement_invariants.py`, `tests/test_settlement_integration.py`):

1. **One immutable contract version per settlement.** The statement records
   id, version, and digest; later versions never rewrite it.
2. **Reproducibility.** The same stored inputs reproduce the same financial
   result; the engine has no clock, network, randomness, or global state.
3. **Idempotency.** `(tenant, idempotency_key)` returns the original
   statement. Statement IDs are deterministic (`stl_<run>_<contract>_v<n>`),
   and `save_settlement` is a no-op for an existing statement (`created=false`).
4. **No duplicate financial actions.** Unique constraints back the billing
   instruction store: `(tenant, provider, provider_invoice_id)` and
   `(tenant, idempotency_key)`. A retry after an ambiguous provider outcome is
   refused pending reconciliation, never re-attempted blindly.
5. **Audit trail.** Every lifecycle transition appends a
   `settlement_events` row (actor, reason, from/to status, payload); issued
   financial fields are never `UPDATE`d.
6. **Tenant isolation.** Every read and write is scoped by tenant; a second
   tenant cannot see a run, contract, settlement, dispute, or billing
   instruction.
7. **Failed reconciliation is never payable.** `REJECTED`,
   `PENDING_REVIEW`, or zero gross; billing refuses.
8. **Invalid certificate is never payable.** The service verifies the
   certificate before any calculation and raises; nothing is persisted.
9. **No binary floats for money.** 10. **Single rounding stage.**
  11. **Overflow fails closed.** 12. **Supersession preserves history**
   (a correction links to the original; the original row keeps its amounts).

---

## 7. Disputes

A dispute is a first-class commercial object (`dsp_…`), not a status flag:

* **Open** (`dispute-open` / `POST …/disputes`): requires disputing party and
  reason; the statement moves to `DISPUTED`, an event is appended, and
  automatic billing is blocked immediately. The original calculation and
  signed evidence are preserved untouched.
* **Resolve** (`dispute-resolve`, CLI): one of `ACCEPT_AS_IS`,
  `REJECT_SETTLEMENT` (→ `CANCELLED`), `CORRECT_CALCULATION`,
  `REPLACE_WITH_NEW_SETTLEMENT` (→ `READY_FOR_BILLING`); the resolution
  actor, reason, timestamp, and optional adjustment settlement reference are
  recorded. Resolving twice is refused.
* Corrections use **supersession**: a new statement references the old one
  (`SUPERSEDED` event with `replacement_settlement_id`); history is preserved.
* Disputes on `SUPERSEDED`/`CANCELLED` statements are refused.

`block_billing_during_dispute` is `true` for all flows in this release: an
open dispute always blocks billing regardless of the acceptance spec.

---

## 8. Known limitations (stated, not hidden)

* **Application-level immutability.** Settlement rows are never updated by
  the application, but a privileged SQLite operator can still edit the file.
  There is no externally anchored hash chain; tamper-evidence comes from the
  signed receipt plus the delivery certificate, which is why verification
  recomputes bindings rather than trusting the row.
* **Single-file SQLite storage.** Settlement and boundary ledgers share one
  SQLite database with WAL and a 5-second busy timeout. Concurrency is fine
  for the reference single-replica deployment; horizontal scaling requires
  moving the ledger to shared infrastructure.
* **`UNIQUE_ENTITIES` qualification** consumes a precomputed distinct count
  supplied by the caller (`unique_entities_delivered`); the engine does not
  scan delivered datasets.
* **Taxes, discounts, multi-currency conversion** are not modeled. Contracts
  are single-currency; currency conversion must happen outside the engine.
* **Billing is draft-only.** The Stripe adapter creates draft invoices; it is
  disabled by default and has not been exercised against live Stripe
  credentials in this environment (see the operations guide for the exact
  configuration and the blocked-status note). The dry-run adapter is fully
  offline and deterministic.
* **Dispute resolution is CLI-only** in this release; the API exposes open
  but not resolve.
* **Webhook dedup is in-process** (`WebhookDeduplicator`); the interface is
  deliberately side-effect-free so deployments can persist the seen-event
  decision in the ledger transactionally.
* **`CALCULATED` status is reserved** for future use; current issuances are
  `READY_FOR_BILLING`, `PENDING_REVIEW`, `DISPUTED`, `SUPERSEDED`, or
  `CANCELLED`.

---

## 9. Verification checklist for a third party

Given a settlement ID and read access (CLI or API):

```bash
datashell settlement show    stl_<run>_<contract>_v1 --json   # the statement
datashell settlement explain stl_<run>_<contract>_v1          # doing the arithmetic
datashell settlement verify  stl_<run>_<contract>_v1 --json   # the evidence
```

1. Re-run `explain` and check `qualified units × unit price = gross` and the
   stated rounding mode.
2. Run `verify`: `AUTHENTIC_AND_UNMODIFIED` means the receipt signature is
   valid, the referenced delivery certificate verifies, and the bound
   statement is unchanged. With Ed25519 provisioned, anyone with the public
   key can do this without the shared secret.
3. Cross-check `run_id` against the boundary ledger
   (`GET /api/v1/runs/{run_id}`) and the delivered artifact digest.
4. Confirm the contract version and digest via
   `datashell contract show <contract_id> --version <n> --json`.

See [SETTLEMENT_OPERATIONS.md](SETTLEMENT_OPERATIONS.md) for the full CLI/API
reference, billing configuration, environment variables, and the
production-readiness checklist.
