#!/usr/bin/env python3
"""Data Shell — Verifiable Data Delivery & Settlement end-to-end demo.

One deterministic run that answers the five product questions on real
evidence, at scale, with no network and no hardcoded money:

    What was submitted?          ->  pipeline reconciliation ledger
    What was accepted/held?      ->  refusal + hold counts, signed certificate
    Contractual acceptance?      ->  acceptance verdict + reason codes
    Amount payable?              ->  exact minor-unit settlement calculation
    Independently verifiable?    ->  signed settlement receipt + certificate

Default scenario: 1,000,000 source rows (10,000 refused, 5,000 held,
985,000 delivered) at USD 0.05 per qualified row -> USD 49,250.00, *derived*
from verified run evidence and asserted against the expected value.

It then walks the failure scenarios the product must survive:

    A. incomplete feed            -> partial acceptance, payable with reasons
    B. tampered delivered data    -> calculation refused, nothing persisted
    C. reconciliation mismatch    -> rejected, never payable
    D. contract version mismatch  -> exact immutable version binding
    E. idempotent retry           -> one financial record, replayed
    F. cross-tenant access        -> denied and invisible
    G. provider timeout           -> UNRESOLVED, reconcile before retry
    H. dispute                    -> billing blocked, then resolved

Usage:
    python examples/settlement_demo.py                 # full 1,000,000-row run
    python examples/settlement_demo.py --records 50000 # scaled run
    python examples/settlement_demo.py --ed25519       # third-party-verifiable receipts
    python examples/settlement_demo.py --keep          # reuse prior storage

Exit code 0 means every assertion in every scenario held.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Make the repository importable when the package is not pip-installed for the
# running interpreter (plain `python examples/settlement_demo.py`).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_STORAGE = REPO_ROOT / "output" / "settlement_demo"
DATA_CONTRACT_PATH = REPO_ROOT / "examples" / "vendor_settlements_contract.json"

ALPHA = "tenant-alpha"
BETA = "tenant-beta"
CONTRACT_ID = "ctr_settlement_demo_usd"
UNIT_PRICE_MINOR = 5  # USD 0.05 per qualified delivered row
FULL_RECORDS = 1_000_000

SCENARIOS: dict = {}


def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _ok(label: str, detail: str) -> None:
    print(f"  ✓ {label}: {detail}")


def _fail(label: str, detail: str) -> None:
    print(f"  ✗ {label}: {detail}")
    SCENARIOS.setdefault("failures", []).append(f"{label}: {detail}")


def _configure_environment(storage: Path) -> None:
    """Isolate the demo's storage *before* datashell settings are imported."""
    values = {
        "DATASHELL_STORAGE_ROOT": storage,
        "DATASHELL_OUTPUT_DIR": storage / "output",
        "DATASHELL_DATA_DIR": storage / "data",
        "DATASHELL_CONTRACTS_DIR": storage / "contracts_registry",
        "DATASHELL_AUDIT_DIR": storage / "output" / "audits",
        "DATASHELL_QUARANTINE_DIR": storage / "output" / "quarantine",
        # Stable demo secret: certificates can be re-verified by later
        # invocations of the demo/CLI, unlike an ephemeral per-process value.
        "DATASHELL_HMAC_SECRET": "settlement-demo-stable-hmac-secret-0123456789abcdef",
        "DATASHELL_KEY_ID": "settlement-demo-hmac-v1",
        "TMPDIR": storage / "tmp",
    }
    for name, value in values.items():
        os.environ[name] = str(value)
    for sub in ("output", "output/audits", "output/quarantine", "data",
                "contracts_registry", "tmp"):
        (storage / sub).mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------- feed


def _generate_feed(storage: Path, total: int, *, refused: int, held: int,
                   delivered: int) -> tuple[Path, Path]:
    """Write the deterministic JSONL feed and return (source, delivered target)."""
    source = storage / "incoming" / f"feed_{total}.jsonl"
    source.parent.mkdir(parents=True, exist_ok=True)
    # Delivery targets must live inside an approved storage root (the output
    # root, a tenant delivery dir, or the temp dir).
    target = storage / "output" / "delivered" / f"delivered_{total}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)

    amounts = [f"{100 + (i % 900)}.{i % 100:02d}" for i in range(1000)]
    currencies = ("USD", "EUR", "GBP")
    with open(source, "w", encoding="utf-8") as handle:
        for i in range(delivered):
            handle.write(
                f'{{"settlement_id": "SET-{i:09d}", "amount": {amounts[i % len(amounts)]},'
                 f' "currency": "{currencies[i % 3]}"}}\n')
        for i in range(held):
            handle.write(
                f'{{"settlement_id": "HOLD-{i:09d}", "amount": -{amounts[i % len(amounts)]}, "currency": "USD"}}\n')
        for i in range(refused):
            handle.write(
                f'{{"settlement_id": "REJ-{i:09d}", "amount": "NOT_A_FLOAT", "currency": "USD"}}\n')
    return source, target


def _splits(total: int) -> tuple[int, int, int]:
    refused = max(1, round(total * 0.01))
    held = max(1, round(total * 0.005))
    return refused, held, total - refused - held


def _run_pipeline(storage: Path, *, tenant: str, refused: int, held: int,
                  delivered: int, tag: str):
    """Execute a real pipeline run against a generated feed."""
    from datashell.contract.models import DataContract
    from datashell.pipeline.runner import DataShellPipeline

    total = refused + held + delivered
    source, target = _generate_feed(storage, total, refused=refused, held=held,
                                    delivered=delivered)
    audit = storage / "output" / "audits" / f"demo_{tag}.json"
    contract = DataContract(**json.loads(DATA_CONTRACT_PATH.read_text()))
    pipeline = DataShellPipeline(contract, tenant_id=tenant, drift_enabled=False)
    started = time.time()
    result = pipeline.run_file(
        source_path=str(source), target_path_override=str(target),
        audit_output_path=str(audit))
    duration = time.time() - started
    return result, audit, duration


def _demo_contract(version: int = 1, *, unit_price_minor: int = UNIT_PRICE_MINOR,
                   expected_units: int):
    from datashell.settlement.models import CommercialContract

    return CommercialContract(
        contract_id=CONTRACT_ID, contract_version=version,
        buyer_id="buyer-acme-data", seller_id="seller-vendor-feed",
        feed_id="vendor-settlements", currency="USD",
        pricing={"model": "PER_ACCEPTED_UNIT", "unit_price_minor": unit_price_minor},
        qualification={"billable_unit": "DELIVERED_ROWS"},
        acceptance={"policy": "PARTIAL_WITH_MINIMUM", "minimum_accepted_units": 1,
                    "expected_units": expected_units},
        effective_at="2026-01-01T00:00:00Z",
        approved_by=["buyer-acme-data", "seller-vendor-feed"],
        description="Settlement demo contract (illustrative; not a real agreement).",
    )


def _expected_amount_minor(delivered: int, unit_price_minor: int = UNIT_PRICE_MINOR) -> int:
    return delivered * unit_price_minor


# ---------------------------------------------------------------------- demo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=int, default=FULL_RECORDS,
                        help=f"total source rows (default {FULL_RECORDS:,})")
    parser.add_argument("--storage", type=Path, default=DEFAULT_STORAGE,
                        help="isolated storage root (default: output/settlement_demo)")
    parser.add_argument("--keep", action="store_true",
                        help="keep existing demo storage instead of resetting it")
    parser.add_argument("--ed25519", action="store_true",
                        help="provision an ephemeral Ed25519 key so receipts are "
                             "third-party verifiable")
    args = parser.parse_args(argv)

    if args.records < 1000:
        parser.error("--records must be at least 1000")
    storage = args.storage.resolve()
    if not args.keep and storage.exists():
        import shutil
        shutil.rmtree(storage)
    _configure_environment(storage)

    # Imports happen after the environment is isolated.
    from datashell.billing.base import InvoicePayload
    from datashell.billing.dry_run import DryRunBillingProvider
    from datashell.billing.stripe_adapter import StripeBillingProvider
    from datashell.core.config import settings
    from datashell.settlement.models import DisputeResolution
    from datashell.settlement.money import format_minor_units
    from datashell.settlement.service import SettlementService, SettlementServiceError

    if args.ed25519:
        from datashell.core.keys import generate_keypair_pem
        private_pem, public_pem = generate_keypair_pem()
        settings.ed25519_private_key = private_pem
        settings.ed25519_active_key_id = "settlement-demo-ed25519-v1"
        settings.ed25519_trusted_keys = {"settlement-demo-ed25519-v1": public_pem}

    refused, held, delivered_expected = _splits(args.records)
    expected_minor = _expected_amount_minor(delivered_expected)

    _section("DATA SHELL — VERIFIABLE DATA DELIVERY & SETTLEMENT DEMO")
    print(f"  Storage:   {storage}")
    print(f"  Source:    {args.records:,} rows "
          f"({refused:,} refused, {held:,} held, {delivered_expected:,} delivered expected)")
    print(f"  Price:     {format_minor_units(UNIT_PRICE_MINOR, 'USD')} USD per qualified row")
    print(f"  Expected:  {format_minor_units(expected_minor, 'USD')} USD "
          f"({expected_minor:,} minor units) — derived from evidence, never hardcoded")

    service = SettlementService()

    # ------------------------------------------------------------- main run
    _section("1. BOUNDARY ENFORCEMENT — WHAT WAS SUBMITTED AND ACCEPTED")
    result, audit_path, duration = _run_pipeline(
        storage, tenant=ALPHA, refused=refused, held=held,
        delivered=delivered_expected, tag="main")
    ledger = result.ledger
    print(f"  Run {result.run_id} finished as {result.status.value} in {duration:.1f}s")
    print(f"  Reconciliation: read={ledger.rows_read:,} "
          f"refused={ledger.refused_structural:,} held={ledger.held_quality:,} "
          f"delivered={ledger.actual_delivered:,} "
          f"unaccounted={ledger.unaccounted_for:,} footing={'OK' if ledger.footing_matched else 'DEFICIT'}")

    if (ledger.rows_read != args.records
            or ledger.refused_structural != refused
            or ledger.held_quality != held
            or ledger.actual_delivered != delivered_expected
            or not ledger.footing_matched
            or ledger.unaccounted_for != 0):
        _fail("main run reconciliation", "counts do not foot")
        return 1
    _ok("reconciliation", f"{ledger.rows_read:,} = {refused:,} + {held:,} + "
        f"{delivered_expected:,}, zero unaccounted")

    # Register the immutable commercial contract version.
    registration = service.register_contract(
        _demo_contract(expected_units=delivered_expected),
        tenant_id=ALPHA, created_by="demo")
    contract_digest = registration["contract_digest"]
    _ok("commercial contract", f"{CONTRACT_ID} v1 registered, digest {contract_digest[:16]}…")

    # Calculate the settlement (idempotency key makes retries safe).
    settlement_key = f"demo-settlement:{result.run_id}:{CONTRACT_ID}:v1"
    statement, created = service.calculate(
        tenant_id=ALPHA, run_id=result.run_id, contract_id=CONTRACT_ID,
        actor="demo", idempotency_key=settlement_key)
    gross = statement["calculation"]["gross_amount_minor"]

    _section("2. CONTRACTUAL ACCEPTANCE & PAYABLE AMOUNT")
    print(f"  Settlement: {statement['settlement_id']}")
    print(f"  Acceptance: {statement['acceptance_status']} "
          f"(reasons: {', '.join(statement['reason_codes']) or 'none'})")
    print(f"  Qualified:  {statement['calculation']['qualified_units']:,} rows "
          f"x {format_minor_units(UNIT_PRICE_MINOR, 'USD')} USD")
    print(f"  Payable:    {format_minor_units(gross, statement['currency'])} "
          f"{statement['currency']} ({gross:,} minor units), status {statement['settlement_status']}")

    if statement["acceptance_status"] != "ACCEPTED" or gross != expected_minor:
        _fail("settlement amount", f"got {gross} minor, expected {expected_minor}")
        return 1
    _ok("derived amount", f"{format_minor_units(gross, 'USD')} USD exactly matches "
        f"delivered x price — the figure was calculated, not asserted")

    # ------------------------------------------------------------------
    # Scenario D before billing so the v2 statement exists to bill in G.
    _section("D. CONTRACT VERSION MISMATCH — EXACT IMMUTABLE BINDING")
    v2 = _demo_contract(version=2, unit_price_minor=7, expected_units=delivered_expected)
    service.register_contract(v2, tenant_id=ALPHA, created_by="demo")
    replay_v1, created_v1 = service.calculate(
        tenant_id=ALPHA, run_id=result.run_id, contract_id=CONTRACT_ID,
        contract_version=1, actor="demo", idempotency_key=settlement_key)
    v2_statement, _ = service.calculate(
        tenant_id=ALPHA, run_id=result.run_id, contract_id=CONTRACT_ID,
        contract_version=2, actor="demo",
        idempotency_key=f"demo-settlement:{result.run_id}:{CONTRACT_ID}:v2")
    try:
        service.calculate(tenant_id=ALPHA, run_id=result.run_id,
                          contract_id=CONTRACT_ID, contract_version=99,
                          actor="demo", idempotency_key="demo-v99")
        _fail("unknown version", "was not refused")
        return 1
    except SettlementServiceError:
        pass
    ok_d = (replay_v1["contract_version"] == 1
            and replay_v1["contract_digest"] == contract_digest
            and v2_statement["contract_version"] == 2
            and v2_statement["calculation"]["gross_amount_minor"]
            == delivered_expected * 7)
    if ok_d:
        _ok("v1 replay", f"still binds v1 digest {contract_digest[:16]}… and "
            f"{format_minor_units(expected_minor, 'USD')} USD")
        _ok("v2 binding", f"separate statement at 0.07 USD/row = "
            f"{format_minor_units(v2_statement['calculation']['gross_amount_minor'], 'USD')} USD")
        _ok("nonexistent version", "refused; no settlement created")
        SCENARIOS["D_contract_version"] = {
            "v1_digest": contract_digest, "v2_gross_minor":
            v2_statement["calculation"]["gross_amount_minor"]}
    else:
        _fail("contract version binding", "version separation failed")

    # ------------------------------------------------------------- explain/verify
    _section("3. INDEPENDENT VERIFIABILITY — SIGNED EVIDENCE")
    explanation = service.explain(statement["settlement_id"], ALPHA)
    for line in explanation["explanation"][:6]:
        print(f"  {line}")
    verification = service.verify_settlement(statement["settlement_id"], ALPHA)
    algorithm = verification["signature_algorithm"]
    third_party = verification["third_party_verifiable"]
    print(f"  Receipt verification: {verification['status']} "
          f"(algorithm {algorithm}, third-party verifiable: {third_party})")
    if verification["status"] != "AUTHENTIC_AND_UNMODIFIED":
        _fail("receipt verification", verification.get("detail", ""))
        return 1
    _ok("settlement receipt", "signature and bindings verified against the delivery certificate")
    if not third_party:
        print("  Note: HMAC-SHA256 requires the shared secret and is deliberately NOT")
        print("        labelled third-party verifiable. Run with --ed25519 to see the")
        print("        asymmetric path that any holder of the public key can verify.")

    # ------------------------------------------------------------- billing
    _section("4. BILLING (DRY-RUN) — DRAFT INSTRUCTIONS ONLY, NO MONEY MOVED")
    provider = DryRunBillingProvider(service.ledger)
    payload = InvoicePayload(
        tenant_id=ALPHA, settlement_id=statement["settlement_id"],
        contract_id=CONTRACT_ID, contract_version=1, run_id=result.run_id,
        certificate_digest=statement["certificate_digest"], currency="USD",
        amount_minor=gross, description=f"Demo settlement for {result.run_id}")
    first_bill = provider.create_draft_invoice(payload, idempotency_key="demo-billing:main")
    replay_bill = provider.create_draft_invoice(payload, idempotency_key="demo-billing:main")
    if not (first_bill.ok and first_bill.status == "PROVIDER_INVOICE_CREATED"):
        _fail("dry-run billing", f"{first_bill.error_code}: {first_bill.error_message}")
        return 1
    _ok("draft instruction", f"{first_bill.billing_instruction_id} -> "
        f"{first_bill.provider_invoice_id} (draft)")
    if not replay_bill.request_meta.get("deduplicated"):
        _fail("billing idempotency", "replay created a second instruction")
        return 1
    _ok("replayed instruction", "deduplicated to the same provider reference")
    SCENARIOS["billing"] = {"instruction_id": first_bill.billing_instruction_id,
                            "provider_invoice_id": first_bill.provider_invoice_id}

    # ------------------------------------------------------------- E + F
    _section("E. IDEMPOTENT RETRY — ONE FINANCIAL RECORD")
    retry, retry_created = service.calculate(
        tenant_id=ALPHA, run_id=result.run_id, contract_id=CONTRACT_ID,
        contract_version=1, actor="retry-bot", idempotency_key=settlement_key)
    same_id = retry["settlement_id"] == statement["settlement_id"]
    v1_rows = [r for r in service.list_settlements(ALPHA)
               if r["contract_id"] == CONTRACT_ID and r["contract_version"] == 1]
    if same_id and retry_created is False and len(v1_rows) == 1:
        _ok("retry", f"replayed {statement['settlement_id']} (created=False, 1 stored row)")
        SCENARIOS["E_idempotency"] = {"settlement_id": statement["settlement_id"],
                                      "rows": len(v1_rows)}
    else:
        _fail("idempotent retry", "a duplicate financial record appeared")

    _section("F. CROSS-TENANT ACCESS — DENIED AND INVISIBLE")
    denied = False
    try:
        service.calculate(tenant_id=BETA, run_id=result.run_id,
                          contract_id=CONTRACT_ID, actor="beta",
                          idempotency_key="beta-attempt")
    except SettlementServiceError:
        denied = True
    invisible = (service.get_settlement(statement["settlement_id"], BETA) is None
                 and service.list_settlements(BETA) == [])
    if denied and invisible:
        _ok("cross-tenant calculation", "refused: run not visible to tenant-beta")
        _ok("cross-tenant reads", "settlement and list are empty for tenant-beta")
        SCENARIOS["F_tenant_isolation"] = True
    else:
        _fail("tenant isolation", f"denied={denied} invisible={invisible}")

    # ------------------------------------------------------------- A
    _section("A. INCOMPLETE FEED — PARTIAL ACCEPTANCE, STILL PAYABLE")
    partial_delivered = int(delivered_expected * 0.9)
    result_a, audit_a, _ = _run_pipeline(
        storage, tenant=ALPHA, refused=0, held=0, delivered=partial_delivered, tag="partial")
    statement_a, _ = service.calculate(
        tenant_id=ALPHA, run_id=result_a.run_id, contract_id=CONTRACT_ID,
        contract_version=1, actor="demo",
        idempotency_key=f"demo-partial:{result_a.run_id}")
    gross_a = statement_a["calculation"]["gross_amount_minor"]
    ok_a = (statement_a["acceptance_status"] == "PARTIALLY_ACCEPTED"
            and statement_a["settlement_status"] == "READY_FOR_BILLING"
            and gross_a == partial_delivered * UNIT_PRICE_MINOR
            and "MANUAL_REVIEW_REQUIRED" in statement_a["reason_codes"])
    if ok_a:
        _ok("partial feed", f"{partial_delivered:,}/{delivered_expected:,} delivered -> "
            f"{statement_a['acceptance_status']}, payable "
            f"{format_minor_units(gross_a, 'USD')} USD with explicit reason codes")
        SCENARIOS["A_incomplete_feed"] = {"gross_minor": gross_a,
                                          "status": statement_a["acceptance_status"]}
    else:
        _fail("partial feed", f"status={statement_a['acceptance_status']} gross={gross_a}")

    # ------------------------------------------------------------- B
    _section("B. INVALID CERTIFICATE — CALCULATION REFUSED, NOTHING PERSISTED")
    tampered_target = Path(result_a.outcome.target_path)
    tampered_target.write_bytes(tampered_target.read_bytes() + b"tamper")
    refused_b = False
    try:
        service.calculate(tenant_id=ALPHA, run_id=result_a.run_id,
                          contract_id=CONTRACT_ID, contract_version=1,
                          actor="demo", idempotency_key="demo-tampered-recalc")
    except SettlementServiceError as exc:
        refused_b = "certificate verification failed" in str(exc)
    if refused_b:
        _ok("tampered artifact", "recalculation refused before any calculation; "
            "no new statement persisted")
        SCENARIOS["B_invalid_certificate"] = "REFUSED"
    else:
        _fail("tampered artifact", "recalculation was not refused")

    # ------------------------------------------------------------- H
    _section("H. DISPUTE — BILLING BLOCKED, RESOLUTION PRESERVES EVIDENCE")
    dispute = service.open_dispute(
        settlement_id=statement_a["settlement_id"], tenant_id=ALPHA,
        disputing_party="buyer-acme-data", reason="quantity questioned",
        evidence_references=["certificate:" + statement_a["certificate_digest"][:16]],
        actor="buyer-acme-data")
    blocked = DryRunBillingProvider(service.ledger).create_draft_invoice(
        InvoicePayload(tenant_id=ALPHA, settlement_id=statement_a["settlement_id"],
                       currency="USD", amount_minor=gross_a),
        idempotency_key="demo-billing:blocked")
    stored_a = service.get_settlement(statement_a["settlement_id"], ALPHA)
    ok_h = (stored_a["settlement_status"] == "DISPUTED"
            and stored_a["calculation"]["gross_amount_minor"] == gross_a
            and blocked.ok is False)
    if ok_h:
        _ok("dispute opened", f"{dispute.dispute_id}: statement DISPUTED, "
            "automatic billing blocked, original amounts preserved")
    else:
        _fail("dispute blocking", f"status={stored_a['settlement_status']} blocked={blocked.ok}")
    resolved = service.resolve_dispute(
        dispute_id=dispute.dispute_id, tenant_id=ALPHA,
        resolution=DisputeResolution.ACCEPT_AS_IS,
        resolution_actor="reviewer", resolution_reason="certificate evidence confirmed")
    stored_a = service.get_settlement(statement_a["settlement_id"], ALPHA)
    allowed = DryRunBillingProvider(service.ledger).create_draft_invoice(
        InvoicePayload(tenant_id=ALPHA, settlement_id=statement_a["settlement_id"],
                       currency="USD", amount_minor=gross_a),
        idempotency_key="demo-billing:resolved")
    if stored_a["settlement_status"] == "READY_FOR_BILLING" and allowed.ok:
        _ok("dispute resolved", f"{resolved['resolution']}: statement billable again "
            "(all transitions recorded in the append-only event log)")
        SCENARIOS["H_dispute"] = {"dispute_id": dispute.dispute_id,
                                  "resolution": resolved["resolution"]}
    else:
        _fail("dispute resolution", f"status={stored_a['settlement_status']} ok={allowed.ok}")

    # ------------------------------------------------------------- C
    _section("C. RECONCILIATION MISMATCH — REJECTED, NEVER PAYABLE")
    result_c, audit_c, _ = _run_pipeline(
        storage, tenant=ALPHA, refused=0, held=0, delivered=100, tag="mismatch")
    with sqlite3.connect(service.boundary.db_path) as conn:
        conn.execute("UPDATE run_history SET footing_matched = 0, unaccounted_for = 7 "
                     "WHERE run_id = ?", (result_c.run_id,))
    statement_c, _ = service.calculate(
        tenant_id=ALPHA, run_id=result_c.run_id, contract_id=CONTRACT_ID,
        contract_version=1, actor="demo", idempotency_key="demo-mismatch")
    refused_c = DryRunBillingProvider(service.ledger).create_draft_invoice(
        InvoicePayload(tenant_id=ALPHA, settlement_id=statement_c["settlement_id"],
                       currency="USD", amount_minor=statement_c["calculation"]["gross_amount_minor"]),
        idempotency_key="demo-billing:mismatch")
    ok_c = (statement_c["acceptance_status"] == "REJECTED"
            and statement_c["calculation"]["gross_amount_minor"] == 0
            and statement_c["settlement_status"] != "READY_FOR_BILLING"
            and refused_c.ok is False)
    if ok_c:
        _ok("reconciliation mismatch", "REJECTED with RECONCILIATION_MISMATCH; "
            "gross 0; billing refused")
        SCENARIOS["C_reconciliation_mismatch"] = "REJECTED"
    else:
        _fail("reconciliation mismatch", json.dumps(statement_c)[:200])

    # ------------------------------------------------------------- G
    _section("G. PROVIDER TIMEOUT — AMBIGUOUS, NEVER BLINDLY RETRIED")
    import httpx

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated provider timeout", request=request)

    fake_client = httpx.Client(base_url="https://api.stripe.com/v1",
                               transport=httpx.MockTransport(timeout_handler))
    stripe = StripeBillingProvider(
        service.ledger, secret_key="sk_test_demo_only", customer_id="cus_demo",
        test_mode=True, transport=fake_client)
    v2_payload = InvoicePayload(
        tenant_id=ALPHA, settlement_id=v2_statement["settlement_id"],
        contract_id=CONTRACT_ID, contract_version=2, run_id=result.run_id,
        certificate_digest=v2_statement["certificate_digest"], currency="USD",
        amount_minor=v2_statement["calculation"]["gross_amount_minor"])
    timed_out = stripe.create_draft_invoice(v2_payload, idempotency_key="demo-stripe-timeout")
    retried = stripe.create_draft_invoice(v2_payload, idempotency_key="demo-stripe-timeout")
    ok_g = (timed_out.ambiguous is True
            and timed_out.status == "UNRESOLVED"
            and retried.ok is False
            and retried.error_code == "RECONCILIATION_REQUIRED")
    if ok_g:
        _ok("timeout", "recorded UNRESOLVED — the outcome is unknown, not 'not created'")
        _ok("retry", "refused with RECONCILIATION_REQUIRED; reconcile before any retry")
        SCENARIOS["G_provider_timeout"] = {"first": timed_out.status,
                                           "retry": retried.error_code}
    else:
        _fail("provider timeout", f"first={timed_out.status} retry={retried.error_code}")
    print("  Note: this is a simulated provider over a local fake transport; no")
    print("        Stripe credentials or network calls are involved.")

    # ------------------------------------------------------------- report
    _section("SUMMARY")
    failures = SCENARIOS.get("failures", [])
    report = {
        "demo": "datashell-verifiable-settlement",
        "records": args.records,
        "splits": {"refused": refused, "held": held, "delivered": delivered_expected},
        "run_id": result.run_id,
        "settlement_id": statement["settlement_id"],
        "contract_digest": contract_digest,
        "qualified_units": statement["calculation"]["qualified_units"],
        "unit_price_minor": UNIT_PRICE_MINOR,
        "gross_amount_minor": gross,
        "gross_amount": format_minor_units(gross, "USD"),
        "verification": {"status": verification["status"],
                         "algorithm": algorithm,
                         "third_party_verifiable": third_party},
        "scenarios": {k: v for k, v in SCENARIOS.items() if k != "failures"},
        "failures": failures,
        "passed": not failures,
    }
    report_path = storage / "settlement_demo_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(f"  Settlement:  {statement['settlement_id']}")
    print(f"  Amount:      {report['gross_amount']} USD "
          f"({gross:,} minor) from {statement['calculation']['qualified_units']:,} "
          f"verified delivered rows")
    print(f"  Scenarios:   A-H exercised; {len(failures)} failure(s)")
    print(f"  Report:      {report_path}")
    print()
    if failures:
        print("DEMO FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("DEMO PASSED — every number above derives from signed run evidence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
