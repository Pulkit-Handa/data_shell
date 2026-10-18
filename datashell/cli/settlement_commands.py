"""CLI commands for commercial contracts and settlement lifecycle.

All commands are read-only or calculate-and-persist: none of them charge a
payment method or transfer funds. Exit codes follow the existing CLI
conventions (0 success, 1 operational failure, 3 verification failure), with
a dedicated code (4) for settlement calculation refusals.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from datashell.core.ledger import BoundaryLedger
from datashell.settlement.ledger import SettlementLedger, SettlementLedgerError
from datashell.settlement.models import (
    CommercialContract,
    DisputeResolution,
    DisputeStatus,
    SettlementStatus,
)
from datashell.settlement.money import format_minor_units
from datashell.settlement.registry import (
    CommercialContractRegistry,
    CommercialContractRegistryError,
)
from datashell.settlement.service import SettlementService, SettlementServiceError

EXIT_CALCULATION_REFUSED = 4


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _load_contract_payload(path: str) -> Dict[str, Any]:
    contract_path = Path(path)
    if not contract_path.is_file():
        raise SettlementServiceError(f"contract file not found: {contract_path}")
    try:
        with open(contract_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise SettlementServiceError(f"contract file could not be read: {type(exc).__name__}: {exc}") from exc


# ------------------------------------------------------------------- contract


def command_contract_validate(args: argparse.Namespace) -> int:
    payload = _load_contract_payload(args.contract)
    try:
        contract = CommercialContract(**payload)
    except ValidationError as exc:
        errors = exc.errors()
        print(f"✗ Commercial contract is invalid ({len(errors)} error{'s' if len(errors) != 1 else ''}):", file=sys.stderr)
        for err in errors:
            location = ".".join(str(part) for part in err.get("loc", ())) or "(contract)"
            message = str(err.get("msg", "invalid value")).replace("Value error, ", "")
            print(f"  - {location}: {message}", file=sys.stderr)
        return 1
    print(f"✓ Commercial contract '{contract.contract_id}' v{contract.contract_version} is valid.")
    print(f"  Buyer:        {contract.buyer_id}")
    print(f"  Seller:       {contract.seller_id}")
    print(f"  Feed:         {contract.feed_id}")
    print(f"  Pricing:      {contract.pricing.model.value} in {contract.currency}")
    print(f"  Acceptance:   {contract.acceptance.policy.value} "
          f"(min={contract.acceptance.minimum_accepted_units:,}"
          + (f", expected={contract.acceptance.expected_units:,}" if contract.acceptance.expected_units else "")
          + ")")
    print(f"  Digest:       {contract.contract_digest}")
    return 0


def command_contract_create(args: argparse.Namespace) -> int:
    payload = _load_contract_payload(args.contract)
    try:
        contract = CommercialContract(**payload)
        registry = CommercialContractRegistry()
        result = registry.register(contract, tenant_id=args.tenant, created_by=args.actor)
    except ValidationError as exc:
        errors = exc.errors()
        print(f"✗ Commercial contract is invalid ({len(errors)} error{'s' if len(errors) != 1 else ''}):", file=sys.stderr)
        for err in errors:
            location = ".".join(str(part) for part in err.get("loc", ())) or "(contract)"
            message = str(err.get("msg", "invalid value")).replace("Value error, ", "")
            print(f"  - {location}: {message}", file=sys.stderr)
        return 1
    except (CommercialContractRegistryError, SettlementLedgerError) as exc:
        print(f"✗ Contract registration failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json({k: v for k, v in result.items() if k != "path"})
    else:
        verb = "Registered" if result.get("created") else "Already registered"
        print(f"✓ {verb} commercial contract '{result['contract_id']}' v{result['contract_version']}.")
        print(f"  Digest: {result['contract_digest']}")
        print("  This version is immutable. Changes require a new contract version.")
    return 0


def command_contract_show(args: argparse.Namespace) -> int:
    registry = CommercialContractRegistry()
    contract = registry.resolve_version(args.contract_id, args.tenant, args.version)
    if contract is None:
        print(f"✗ Contract '{args.contract_id}'"
              + (f" v{args.version}" if args.version else "") + f" not found for tenant '{args.tenant}'",
              file=sys.stderr)
        return 1
    if args.json:
        _print_json({**contract.model_dump(mode="json"),
                     "contract_digest": contract.contract_digest})
        return 0
    print(f"Commercial contract '{contract.contract_id}' v{contract.contract_version}")
    print(f"  Digest:        {contract.contract_digest}")
    print(f"  Buyer/Seller:  {contract.buyer_id} / {contract.seller_id}")
    print(f"  Feed:          {contract.feed_id}")
    print(f"  Currency:      {contract.currency}")
    print(f"  Pricing:       {contract.pricing.model.value}")
    if contract.pricing.unit_price_minor is not None:
        print(f"  Unit price:    {format_minor_units(contract.pricing.unit_price_minor, contract.currency)} {contract.currency}")
    if contract.pricing.tiers:
        for tier in contract.pricing.tiers:
            bound = f"[{tier.min_units:,}, {tier.max_units or '∞'})"
            print(f"  Tier {bound:<18} {format_minor_units(tier.unit_price_minor, contract.currency)}/unit")
    print(f"  Qualification: {contract.qualification.billable_unit.value}")
    print(f"  Acceptance:    {contract.acceptance.policy.value} "
          f"(min={contract.acceptance.minimum_accepted_units:,}"
          + (f", expected={contract.acceptance.expected_units:,}" if contract.acceptance.expected_units else "")
          + (f", max={contract.acceptance.maximum_units:,}" if contract.acceptance.maximum_units else "")
          + ")")
    print(f"  Penalty:       {contract.penalty.model.value}")
    print(f"  Effective:     {contract.effective_at}"
          + (f" -> {contract.expires_at}" if contract.expires_at else ""))
    return 0


def command_contract_versions(args: argparse.Namespace) -> int:
    registry = CommercialContractRegistry()
    versions = registry.list_versions(args.contract_id, args.tenant)
    if not versions:
        print(f"✗ No versions of contract '{args.contract_id}' found for tenant '{args.tenant}'", file=sys.stderr)
        return 1
    if args.json:
        _print_json({"contract_id": args.contract_id, "tenant_id": args.tenant, "versions": versions})
        return 0
    print(f"Versions of '{args.contract_id}' for tenant '{args.tenant}':")
    for version in versions:
        print(f"  v{version['contract_version']:<4} {version['contract_digest'][:24]}...  {version['created_at']}")
    return 0


# ----------------------------------------------------------------- settlement


def command_settlement_calculate(args: argparse.Namespace) -> int:
    service = SettlementService()
    key = args.idempotency_key or f"{args.tenant}:{args.run_id}:{args.contract_id}:{args.contract_version or 'latest'}"
    try:
        row, created = service.calculate(
            tenant_id=args.tenant,
            run_id=args.run_id,
            contract_id=args.contract_id,
            contract_version=args.contract_version,
            actor=args.actor,
            idempotency_key=key,
        )
    except SettlementServiceError as exc:
        print(f"✗ Settlement calculation refused: {exc}", file=sys.stderr)
        return EXIT_CALCULATION_REFUSED
    if args.json:
        _print_json({**row, "created": created})
        return 0
    calc = row["calculation"]
    verb = "Calculated" if created else "Replayed (idempotent)"
    print(f"{verb} settlement {row['settlement_id']}")
    print(f"  Run:                {row['run_id']}")
    print(f"  Contract:           {row['contract_id']} v{row['contract_version']}")
    print(f"  Contract digest:    {row['contract_digest']}")
    print(f"  Certificate digest: {row['certificate_digest']}")
    print(f"  Pricing model:      {calc['pricing_model']}")
    print(f"  Qualified units:    {calc['qualified_units']:,}")
    if calc.get("unit_price_minor") is not None:
        print(f"  Unit price:         {format_minor_units(calc['unit_price_minor'], row['currency'])} {row['currency']}")
    for tier in calc.get("tiers_applied") or []:
        print(f"  Tier: {tier['units']:,} units x {tier['unit_price_minor']} = {tier['amount_minor']:,} minor")
    if calc.get("proration_ratio"):
        print(f"  Proration ratio:    {calc['proration_ratio']}")
    print(f"  Gross amount:       {format_minor_units(calc['gross_amount_minor'], row['currency'])} {row['currency']}")
    if calc.get("penalty_minor"):
        print(f"  Penalty:            {format_minor_units(calc['penalty_minor'], row['currency'])} ({calc.get('penalty_reason')})")
    print(f"  Acceptance:         {row['acceptance_status']}")
    print(f"  Settlement status:  {row['settlement_status']}")
    if row.get("reason_codes"):
        print(f"  Reason codes:       {', '.join(row['reason_codes'])}")
    if row.get("explanation"):
        print(f"  Explanation:        {row['explanation']}")
    if row["settlement_status"] != SettlementStatus.READY_FOR_BILLING.value:
        print("\nNote: this settlement is not eligible for automatic billing in its current state.")
    return 0


def command_settlement_show(args: argparse.Namespace) -> int:
    service = SettlementService()
    row = service.get_settlement(args.settlement_id, args.tenant)
    if row is None:
        print(f"✗ Settlement '{args.settlement_id}' not found for tenant '{args.tenant}'", file=sys.stderr)
        return 1
    if args.json:
        _print_json(row)
        return 0
    calc = row["calculation"]
    print(f"Settlement {row['settlement_id']}")
    for key in ("run_id", "contract_id", "contract_version", "contract_digest",
                "certificate_digest", "currency", "acceptance_status",
                "settlement_status", "actor", "created_at"):
        print(f"  {key:<20} {row.get(key)}")
    print(f"  {'gross_amount_minor':<20} {calc['gross_amount_minor']:,} ({format_minor_units(calc['gross_amount_minor'], row['currency'])} {row['currency']})")
    print(f"  {'qualified_units':<20} {calc['qualified_units']:,}")
    print(f"  {'pricing_model':<20} {calc['pricing_model']}")
    print(f"  {'calculation_version':<20} {calc['calculation_version']}")
    if row.get("reason_codes"):
        print(f"  {'reason_codes':<20} {', '.join(row['reason_codes'])}")
    if row.get("superseding_settlement_id"):
        print(f"  {'superseded_by':<20} {row['superseding_settlement_id']}")
    return 0


def command_settlement_list(args: argparse.Namespace) -> int:
    service = SettlementService()
    rows = service.list_settlements(args.tenant, status=args.status, contract_id=args.contract_id)
    if args.json:
        _print_json({"tenant_id": args.tenant, "count": len(rows), "settlements": rows})
        return 0
    if not rows:
        print("(no settlements)")
        return 0
    print(f"Settlements for tenant '{args.tenant}' ({len(rows)})")
    for row in rows:
        calc = row["calculation"]
        print(f"  {row['settlement_id']:<44} {row['settlement_status']:<18} "
              f"{format_minor_units(calc['gross_amount_minor'], row['currency']):>14} {row['currency']}")
    return 0


def command_settlement_explain(args: argparse.Namespace) -> int:
    service = SettlementService()
    try:
        explanation = service.explain(args.settlement_id, args.tenant)
    except SettlementServiceError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(explanation)
        return 0
    for line in explanation["explanation"]:
        print(line)
    return 0


def command_settlement_verify(args: argparse.Namespace) -> int:
    service = SettlementService()
    try:
        report = service.verify_settlement(args.settlement_id, args.tenant)
    except SettlementServiceError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(report)
    else:
        status = report.get("status", "UNKNOWN")
        marker = "✓" if status == "AUTHENTIC_AND_UNMODIFIED" else "✗"
        print(f"{marker} Settlement receipt verification: {status}")
        print(f"    signature valid:      {report.get('signature_valid')}")
        print(f"    signature algorithm:  {report.get('signature_algorithm')}")
        print(f"    third-party verifiable: {report.get('third_party_verifiable')}")
        print(f"    artifacts:            {report.get('artifacts_status')}")
        if report.get("detail"):
            print(f"    detail:               {report['detail']}")
    return 0 if report.get("status") == "AUTHENTIC_AND_UNMODIFIED" else 3


# -------------------------------------------------------------------- invoice


def _invoice_payload(service: SettlementService, settlement_id: str, tenant_id: str,
                     actor: str) -> Dict[str, Any]:
    """Build a stable, versioned invoice/billing-instruction representation.

    This is a draft billing document — never a paid invoice and never proof of
    money movement.
    """
    row = service.get_settlement(settlement_id, tenant_id)
    if row is None:
        raise SettlementServiceError(f"settlement '{settlement_id}' not found for tenant '{tenant_id}'")
    calc = row["calculation"]
    return {
        "document_version": "1",
        "document_kind": "DRAFT_INVOICE_PREVIEW",
        "status": "DRAFT — proposed billing document; not a paid invoice",
        "settlement_id": row["settlement_id"],
        "run_id": row["run_id"],
        "contract_id": row["contract_id"],
        "contract_version": row["contract_version"],
        "contract_digest": row["contract_digest"],
        "certificate_digest": row["certificate_digest"],
        "currency": row["currency"],
        "amount_minor": calc["gross_amount_minor"],
        "amount": format_minor_units(calc["gross_amount_minor"], row["currency"]),
        "settlement_status": row["settlement_status"],
        "acceptance_status": row["acceptance_status"],
        "eligible_for_billing": row["settlement_status"] == SettlementStatus.READY_FOR_BILLING.value,
        "generated_at": datetime_now(),
        "generated_by": actor,
        "disclaimer": (
            "This document is a calculated liability preview. It is not an invoice "
            "issued by the seller, not a payment confirmation, and not evidence "
            "that any money has moved."
        ),
    }


def datetime_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def command_invoice_preview(args: argparse.Namespace) -> int:
    service = SettlementService()
    try:
        payload = _invoice_payload(service, args.settlement_id, args.tenant, args.actor)
    except SettlementServiceError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(payload)
        return 0
    print(f"Invoice preview (DRAFT — not a paid invoice)")
    print(f"  Settlement:      {payload['settlement_id']}")
    print(f"  Contract:        {payload['contract_id']} v{payload['contract_version']}")
    print(f"  Amount:          {payload['amount']} {payload['currency']} ({payload['amount_minor']:,} minor)")
    print(f"  Settlement:      {payload['settlement_status']}")
    print(f"  Acceptance:      {payload['acceptance_status']}")
    print(f"  Billable:        {'yes' if payload['eligible_for_billing'] else 'no'}")
    print(f"  {payload['disclaimer']}")
    return 0


def command_invoice_export(args: argparse.Namespace) -> int:
    if args.format != "json":
        print(f"✗ unsupported format '{args.format}' (only json is supported)", file=sys.stderr)
        return 1
    service = SettlementService()
    try:
        payload = _invoice_payload(service, args.settlement_id, args.tenant, args.actor)
    except SettlementServiceError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    print(f"✓ Draft invoice exported to {output}")
    print("  This is a draft billing document, not a paid invoice.")
    return 0


# --------------------------------------------------------------------- dispute


def command_dispute_open(args: argparse.Namespace) -> int:
    service = SettlementService()
    try:
        dispute = service.open_dispute(
            settlement_id=args.settlement_id, tenant_id=args.tenant,
            disputing_party=args.party, reason=args.reason,
            evidence_references=args.evidence or [],
            supporting_explanation=args.explanation or "", actor=args.actor)
    except SettlementServiceError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(dispute.model_dump(mode="json"))
        return 0
    print(f"✓ Opened dispute {dispute.dispute_id} against settlement {dispute.settlement_id}")
    print(f"  Party:   {dispute.disputing_party}")
    print(f"  Reason:  {dispute.reason}")
    print(f"  Status:  {dispute.status.value}")
    print("  The settlement is now DISPUTED and ineligible for automatic billing.")
    return 0


def command_dispute_resolve(args: argparse.Namespace) -> int:
    service = SettlementService()
    try:
        resolution = DisputeResolution(args.resolution)
    except ValueError:
        print(f"✗ invalid resolution '{args.resolution}' "
              f"(choose from: {', '.join(r.value for r in DisputeResolution)})", file=sys.stderr)
        return 1
    try:
        result = service.resolve_dispute(
            dispute_id=args.dispute_id, tenant_id=args.tenant, resolution=resolution,
            resolution_actor=args.actor, resolution_reason=args.reason)
    except SettlementServiceError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(result)
        return 0
    print(f"✓ Resolved dispute {result['dispute_id']}: {result['resolution']}")
    print(f"  Settlement {result['settlement_id']} is now {service.get_settlement(result['settlement_id'], args.tenant)['settlement_status']}")
    return 0


# -------------------------------------------------------------------- billing


def command_billing_preview(args: argparse.Namespace) -> int:
    """Create (or replay) a draft billing instruction via the provider."""
    from datashell.billing.dry_run import DryRunBillingProvider
    from datashell.billing.stripe_adapter import StripeBillingProvider
    from datashell.billing.base import InvoicePayload
    from datashell.settlement.models import SettlementStatus

    service = SettlementService()
    row = service.get_settlement(args.settlement_id, args.tenant)
    if row is None:
        print(f"✗ Settlement '{args.settlement_id}' not found for tenant '{args.tenant}'", file=sys.stderr)
        return 1
    if row["settlement_status"] != SettlementStatus.READY_FOR_BILLING.value:
        print(f"✗ Settlement is not eligible for billing (status: {row['settlement_status']})", file=sys.stderr)
        return EXIT_CALCULATION_REFUSED
    if service.ledger.has_open_dispute(args.settlement_id, args.tenant):
        print("✗ An open dispute blocks automatic billing actions.", file=sys.stderr)
        return EXIT_CALCULATION_REFUSED

    calc = row["calculation"]
    payload = InvoicePayload(
        tenant_id=args.tenant,
        settlement_id=row["settlement_id"],
        contract_id=row["contract_id"],
        contract_version=row["contract_version"],
        run_id=row["run_id"],
        certificate_digest=row["certificate_digest"],
        currency=row["currency"],
        amount_minor=calc["gross_amount_minor"],
        description=f"Data delivery settlement {row['settlement_id']} (run {row['run_id']})",
        buyer_id=row.get("contract_id", ""),
        seller_id="",
        idempotency_key=args.idempotency_key or f"billing:{args.tenant}:{args.settlement_id}",
    )
    provider_name = args.provider
    if provider_name == "stripe":
        provider = StripeBillingProvider(service.ledger)
    else:
        provider = DryRunBillingProvider(service.ledger)
    try:
        result = provider.create_draft_invoice(payload, idempotency_key=payload.idempotency_key)
    except RuntimeError as exc:
        print(f"✗ Billing provider error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(result.as_dict())
    else:
        if result.ok:
            print(f"✓ Draft billing instruction {result.billing_instruction_id} ({result.status})")
            print(f"  Provider:       {result.provider}")
            print(f"  Provider ref:   {result.provider_invoice_id or '—'}")
            print(f"  Amount:         {format_minor_units(payload.amount_minor, payload.currency)} {payload.currency}")
            print("  This is a draft invoice proposal. No payment was collected and no money moved.")
        else:
            print(f"✗ Billing instruction failed: {result.error_code}: {result.error_message}", file=sys.stderr)
    if result.ambiguous:
        print("  WARNING: external outcome is ambiguous — reconcile before any retry.")
        return EXIT_CALCULATION_REFUSED
    return 0 if result.ok else 1
