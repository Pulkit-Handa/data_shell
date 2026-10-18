# Settlement Operations — CLI, API, Billing, and Production Checklist

This is the operational reference for the settlement subsystem described in
[SETTLEMENT.md](SETTLEMENT.md). Everything here is implemented in this
release; anything that is not is explicitly marked **blocked** or
**not implemented**.

---

## 1. Quickstart (5 minutes, no network)

```bash
# 1. Work in an isolated demo storage root (keeps your real output/ clean).
python examples/settlement_demo.py --records 20000

# 2. Walk the same flow with the CLI, against the demo storage.
export DATASHELL_STORAGE_ROOT="$PWD/output/settlement_demo"
export DATASHELL_HMAC_SECRET="a-stable-secret-so-certs-verify-across-invocations"

datashell contract create examples/commercial_delivery_contract.json --tenant tenant-alpha
datashell settlement calculate --run-id <run_id> --contract-id ctr_vendor_settlements_usd \
    --tenant tenant-alpha --idempotency-key my-first-settlement
datashell settlement explain <settlement_id> --tenant tenant-alpha
datashell settlement verify  <settlement_id> --tenant tenant-alpha --json
datashell settlement invoice-preview <settlement_id> --tenant tenant-alpha   # DRAFT, not a bill
datashell settlement billing-preview <settlement_id> --tenant tenant-alpha   # dry-run provider
```

Expected: the calculation prints the qualified units × unit price = gross in
minor units, `verify` returns `AUTHENTIC_AND_UNMODIFIED`, and the billing
preview reports `PROVIDER_INVOICE_CREATED` with a deterministic synthetic
`dryrun_inv_…` reference. No money moves anywhere.

For the full 1,000,000-row scenario (985,000 delivered × USD 0.05 =
USD 49,250.00, derived from evidence):

```bash
python examples/settlement_demo.py            # ~5 s pipeline + settlement scenarios A–H
python examples/settlement_benchmark.py       # measured latency/memory comparison
```

---

## 2. CLI reference

All commands default to tenant `default-tenant`. Every command is
tenant-scoped; no command moves money.

### Commercial contracts — `datashell contract …`

| Command | Purpose |
| --- | --- |
| `contract validate <file.json>` | Schema validation + digest; exit 1 with actionable errors |
| `contract create <file.json> --tenant … --actor … [--json]` | Register an immutable version (idempotent on identical payload) |
| `contract show <contract_id> [--version N] --tenant … [--json]` | Show latest or exact version (JSON includes `contract_digest`) |
| `contract versions <contract_id> --tenant … [--json]` | List all registered versions with digests |

### Settlements — `datashell settlement …`

| Command | Purpose |
| --- | --- |
| `settlement calculate --run-id R --contract-id C [--contract-version N] [--tenant T] [--actor A] [--idempotency-key K] [--json]` | Verify certificate → resolve exact contract version → evaluate acceptance → calculate → persist. Replays return the original statement. |
| `settlement show <settlement_id> [--tenant T] [--json]` | One statement |
| `settlement list [--status S] [--contract-id C] [--tenant T] [--json]` | Tenant-scoped listing |
| `settlement explain <settlement_id> [--tenant T] [--json]` | Human-readable arithmetic: inputs, qualified units, pricing, rounding, reason codes |
| `settlement verify <settlement_id> [--tenant T] [--json]` | Verify signed receipt + certificate binding + statement equality |
| `settlement invoice-preview <settlement_id> [--tenant T] [--actor A] [--json]` | Draft invoice payload (never a bill, never sent) |
| `settlement invoice-export <settlement_id> --output FILE [--format json] [--tenant T]` | Write the draft-invoice preview to a file |
| `settlement dispute-open <settlement_id> --party P --reason R [--explanation E] [--evidence …] [--tenant T] [--actor A] [--json]` | Open a dispute; statement → `DISPUTED`, billing blocked |
| `settlement dispute-resolve <dispute_id> --resolution {ACCEPT_AS_IS,REJECT_SETTLEMENT,CORRECT_CALCULATION,REPLACE_WITH_NEW_SETTLEMENT} --reason R --actor A [--tenant T] [--json]` | Record the resolution and transition the statement |
| `settlement billing-preview <settlement_id> [--provider {dry-run,stripe}] [--idempotency-key K] [--tenant T] [--json]` | Create/replay a **draft** billing instruction via the configured provider |

Exit codes: `0` success, `1` operational/validation failure, `3` verification
failure, `4` calculation or billing refusal (`EXIT_CALCULATION_REFUSED` —
non-billable settlement, missing run, invalid evidence). Non-payable results
are always refusals, never zero-dollar bills.

---

## 3. HTTP API reference

Base path `/api/v1`. All endpoints require the `X-API-Key` header; capabilities
are enforced per endpoint:

* `SETTLEMENTS_READ` — read endpoints + verify + billing preview
* `SETTLEMENTS_WRITE` — contract registration, calculate, disputes
* `BILLING_MANAGE` — approvals

| Method & path | Capability | Notes |
| --- | --- | --- |
| `POST /commercial-contracts` | `SETTLEMENTS_WRITE` | 201; body validated (`contract_id` pattern-safe) |
| `GET /commercial-contracts/{id}` | `SETTLEMENTS_READ` | `?version=` optional; 404 cross-tenant |
| `GET /commercial-contracts/{id}/versions` | `SETTLEMENTS_READ` | All versions + digests |
| `POST /settlements/calculate` | `SETTLEMENTS_WRITE` | Body: `run_id`, `contract_id`, optional `contract_version`, `idempotency_key`. 200 with `created: true/false`; 422 on refusal |
| `GET /settlements` | `SETTLEMENTS_READ` | `status`, `contract_id`, `limit`, `offset` |
| `GET /settlements/{id}` | `SETTLEMENTS_READ` | 404 when not visible to the tenant |
| `GET /settlements/{id}/explanation` | `SETTLEMENTS_READ` | Explainability payload |
| `POST /settlements/{id}/verify` | `SETTLEMENTS_READ` | `AUTHENTIC_AND_UNMODIFIED` / `TAMPERED_OR_INVALID_SIGNATURE` / … |
| `POST /settlements/{id}/disputes` | `SETTLEMENTS_WRITE` | 201; 409 when not disputable |
| `POST /settlements/{id}/approvals` | `BILLING_MANAGE` | Explicit billing approval; 409 when disputed/non-billable |
| `POST /settlements/{id}/billing-preview` | `SETTLEMENTS_READ` | **Requires `Idempotency-Key` header**; 400 without, 409 when not billable |

Example:

```bash
curl -s -X POST http://localhost:8000/api/v1/settlements/calculate \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"run_id":"dm_run_…","contract_id":"ctr_vendor_settlements_usd","idempotency_key":"inv-2026-10"}'
```

Dispute resolution is CLI-only in this release; the API exposes open, not
resolve. This is a deliberate scope boundary, not an accidental gap.

---

## 4. Billing configuration

Nothing in this system moves money. Providers create **draft invoices** only;
finalization and collection remain an explicit human action outside Data Shell.

| Env var | Default | Meaning |
| --- | --- | --- |
| `DATASHELL_BILLING_PROVIDER` | `dry-run` | `dry-run` (offline, deterministic) or `stripe` (opt-in) |
| `DATASHELL_BILLING_STRIPE_SECRET_KEY` | — | Required for Stripe; absent → adapter is disabled |
| `DATASHELL_BILLING_STRIPE_WEBHOOK_SECRET` | — | Required to verify Stripe webhooks |
| `DATASHELL_BILLING_STRIPE_CUSTOMER_ID` | — | Customer the draft invoice is created for |
| `DATASHELL_BILLING_STRIPE_TEST_MODE` | `true` | Refuses to run with a live key unless explicitly flipped |

### Dry-run adapter (default, fully tested)

Deterministic, offline, side-effect free. The synthetic provider reference is
derived from the idempotency key, so replaying an instruction returns the same
reference and never records a second row.

### Stripe adapter (opt-in, draft-only)

* Creates an invoice item + draft invoice over HTTPS via `httpx` (no new
  runtime dependency); the invoice is created with `auto_advance=false` so it
  **cannot be finalized or charged by this code**.
* Every external call carries an idempotency key (`<key>-item`,
  `<key>-invoice`).
* **Timeout is not "not created".** An ambiguous outcome is recorded as
  `UNRESOLVED`. A retry with the same key is refused with
  `RECONCILIATION_REQUIRED` until an operator reconciles against the provider;
  the system never blindly creates a second invoice.
* The API never logs the secret key; `request_meta` stores only non-secret
  identifiers.
* **Status: implemented and exercised against a faked HTTP transport (timeout,
  dedup, draft-only semantics). Not exercised against live Stripe test-mode
  credentials — blocked in this environment (no credentials provisioned).
  Live money movement (Phase 3) is not implemented and intentionally so.**

### Webhooks

Stripe's `t=…,v1=…` signature scheme (HMAC-SHA256 over `timestamp.body`,
constant-time compare, replay tolerance window). Malformed, stale, or
wrong-secret payloads fail closed; event IDs are deduplicated so a replayed
event never applies twice. The deduplicator is in-process by design;
deployments that need cross-process replay protection should persist the
decision in the same transaction as the state change (noted in
SETTLEMENT.md limitations).

---

## 5. Idempotency

| Surface | Key | Behavior |
| --- | --- | --- |
| Calculation | `(tenant, idempotency_key)` or the deterministic statement ID `stl_<run>_<contract>_v<n>` | Replay returns the original statement; `created=false`; no second row |
| CLI | `--idempotency-key` (default: derived from tenant/run/contract/version) | Safe to re-run the same command |
| API | `idempotency_key` body field | Replay returns `created: false` |
| Billing preview | `Idempotency-Key` header (required) + `(tenant, provider, provider_invoice_id)` unique index | Replay returns the same instruction; `request_meta.deduplicated=true` |
| Stripe | same-key retry after UNRESOLVED | Refused (`RECONCILIATION_REQUIRED`) until reconciled |

Engine requirement: `idempotency_key` never changes the calculated amount —
it only names the request (`test_idempotency_key_does_not_change_amount`).

---

## 6. Dispute runbook

1. **Open** — `datashell settlement dispute-open <id> --party buyer --reason "…"`.
   The statement becomes `DISPUTED`; automatic billing and approvals are
   blocked; the original calculation, receipt, and certificate are untouched.
2. **Review** — inspect `settlement explain`, `settlement verify`, the boundary
   ledger run, and the delivered artifacts. Everything needed is referenced by
   digest.
3. **Resolve** —
   * `ACCEPT_AS_IS` → statement returns to `READY_FOR_BILLING`.
   * `REJECT_SETTLEMENT` → statement `CANCELLED`; it can never be billed.
   * `CORRECT_CALCULATION` / `REPLACE_WITH_NEW_SETTLEMENT` → resolution records
     the adjustment reference; the original statement remains in history.
4. Every step appends a `settlement_events` row (actor, reason, from/to
   status). Resolving twice is refused. Disputes on cancelled/superseded
   statements are refused.

---

## 7. Environment variables

| Env var | Purpose |
| --- | --- |
| `DATASHELL_STORAGE_ROOT` / `DATASHELL_OUTPUT_DIR` | Storage root; settlement + boundary ledger at `output/datashell_history.db` |
| `DATASHELL_CONTRACTS_DIR` | Contract registry root (`contracts_registry/commercial/…`) |
| `DATASHELL_HMAC_SECRET`, `DATASHELL_KEY_ID` | HMAC signing; required stable value to verify certificates across processes |
| `DATASHELL_ED25519_PRIVATE_KEY`, `DATASHELL_ED25519_KEY_ID`, `DATASHELL_ED25519_KEYRING` | Provision Ed25519 so receipts are third-party verifiable |
| `DATASHELL_BILLING_*` | Billing provider configuration (section 4) |
| `DATASHELL_API_KEY`, `AUTH_ENABLED` | API auth (unchanged from the core product) |

---

## 8. Settlement threat model

Extends [THREAT_MODEL.md](THREAT_MODEL.md). Assets: calculated liability,
contract terms in force, evidence chain, billing instructions.

| Threat | Mitigation |
| --- | --- |
| Silent contract edit after agreement | Immutable registry + digest bound into every settlement and receipt |
| Recalculating to a different amount | Deterministic pure engine + signed receipt binding the calculated fields; verification re-checks statement equality |
| Forging a statement or swapping evidence | Signature over the full envelope including `signature_header`; certificate binding is recomputed, not trusted |
| Replaying a settlement to double-bill | Deterministic IDs + `(tenant, idempotency_key)` unique index + provider invoice uniqueness |
| Timeout treated as failure, then double creation | Ambiguous outcomes recorded `UNRESOLVED`; retries refused pending reconciliation |
| Billing a failed/tampered delivery | Certificate verification precedes calculation; failed reconciliation yields `REJECTED`, zero gross, non-billable |
| Cross-tenant financial leakage | Tenant scoping on every read/write; cross-tenant reads 404 |
| Operator editing the SQLite file | Signed receipts + delivery certificates make post-hoc edits detectable; verification recomputes bindings (no external anchor — see limitations) |
| Secret leakage in logs | Only key IDs and non-secret references are recorded |

---

## 9. Production-readiness checklist

- [ ] Stable `DATASHELL_HMAC_SECRET` in a secret manager, or provision
      Ed25519 (`DATASHELL_ED25519_PRIVATE_KEY` + published trust store) so
      receipts are third-party verifiable.
- [ ] Contract registry and `output/` on durable storage with the same
      backup/retention policy as the boundary ledger (they are one database).
- [ ] Decide and document the authoritative currency precision per contract
      (`currency` field; taxes and FX explicitly out of scope).
- [ ] Billing provider left at `dry-run` until an operator deliberately
      configures Stripe; grant `BILLING_MANAGE` only to named approvers.
- [ ] Review `settlement explain` output for one real contract and confirm the
      acceptance policy matches the agreement.
- [ ] Run `python -m pytest tests/ -q` (full suite) and
      `python examples/settlement_demo.py` before promoting an environment.
- [ ] Confirm webhook secret configured if Stripe webhooks will be consumed.
- [ ] Accept the documented single-replica SQLite limitation, or migrate the
      ledger to shared infrastructure first.

---

## 10. Measured performance (this repository, Python 3.14, this machine)

From `examples/settlement_benchmark.py --rows 100000 --iterations 200`:

| Operation | Result |
| --- | --- |
| Pipeline (ingest → quality → reconcile → certificate → deliver), 100k rows | ~0.62 s (~161k rows/s) |
| Settlement calculation, 100k qualified units | ~0.16 ms mean (~10 KiB peak Python allocations) |
| Settlement calculation, 1M qualified units | ~0.11 ms mean (~13 KiB peak) |
| Receipt verification (signature + certificate + binding) | ~0.9 ms mean |

The engine consumes compact run summaries, so settlement cost is independent
of feed size (verified by the benchmark's scale-independence check). Numbers
are machine-specific; re-run the script rather than quoting these blindly.

---

## 11. Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| `exit 4` on calculate | Missing run for tenant, invalid certificate, invalid contract reference | Read the message; confirm run id/tenant and re-verify the certificate |
| `verify` → `TAMPERED_OR_INVALID_SIGNATURE` | Statement/receipt edited, or signing key not provisioned | Compare against the stored row; ensure the key ID is in the trust store |
| `verify` → `DELIVERY_CERTIFICATE_DIGEST_MISMATCH` | A different/edited certificate supplied | Use the certificate referenced by the receipt |
| Billing preview 409 / exit 4 | Status not `READY_FOR_BILLING` or open dispute | Show the statement and resolve the dispute |
| Billing retry returns `RECONCILIATION_REQUIRED` | Prior ambiguous provider outcome | Reconcile with the provider dashboard, then record the outcome |
| Cross-tenant 404 | Correct isolation behavior | Re-check the tenant header/key mapping |
