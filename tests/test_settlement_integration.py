"""Integration tests for the settlement domain.

These tests exercise the real production path end to end: the actual pipeline
(with real signed audit certificates), the durable SQLite settlement ledger,
service orchestration, signed settlement receipts, the dispute lifecycle,
billing adapters (dry-run + Stripe against a faked HTTP transport), and the
HTTP API. Storage is isolated per test; nothing on the critical path is
mocked.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi.testclient import TestClient

from datashell.billing.base import InvoicePayload
from datashell.billing.dry_run import DryRunBillingProvider
from datashell.billing.stripe_adapter import StripeBillingProvider
from datashell.contract.models import DataContract
from datashell.core.config import settings
from datashell.core.keys import generate_keypair_pem
from datashell.core.ledger import BoundaryLedger
from datashell.core.security import APIKeyMetadata, Role, register_api_key
from datashell.pipeline.runner import DataShellPipeline
from datashell.server.app import app
from datashell.server.routers import settlements as settlements_router
from datashell.settlement.certificate import (
    build_settlement_receipt,
    certificate_digest,
    verify_settlement_receipt,
)
from datashell.settlement.ledger import SettlementLedger
from datashell.settlement.models import (
    CommercialContract,
    DisputeResolution,
    SettlementStatement,
)
from datashell.settlement.registry import (
    CommercialContractRegistry,
    CommercialContractRegistryError,
)
from datashell.settlement.service import SettlementService, SettlementServiceError

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_CONTRACT_PATH = REPO_ROOT / "examples" / "vendor_settlements_contract.json"

ALPHA = "tenant-alpha"
BETA = "tenant-beta"


# --------------------------------------------------------------------- harness


class Harness:
    """Isolated settlement stack backed by one SQLite file and one registry."""

    def __init__(self, storage: Path):
        self.storage = storage
        self.output = storage / "output"
        self.contracts = storage / "contracts_registry"
        self.db = self.output / "datashell_history.db"
        self.ledger = SettlementLedger(self.db)
        self.boundary = BoundaryLedger(self.db)
        self.registry = CommercialContractRegistry(
            self.contracts / "commercial", ledger=self.ledger)
        self.service = SettlementService(
            ledger=self.ledger, boundary_ledger=self.boundary,
            contract_registry=self.registry)

    # -- storage helpers ------------------------------------------------

    def fresh_service(self) -> SettlementService:
        """Simulate a process restart: brand-new objects over the same storage."""
        ledger = SettlementLedger(self.db)
        boundary = BoundaryLedger(self.db)
        registry = CommercialContractRegistry(
            self.contracts / "commercial", ledger=ledger)
        return SettlementService(
            ledger=ledger, boundary_ledger=boundary, contract_registry=registry)

    def tamper_run_footing(self, run_id: str) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE run_history SET footing_matched = 0, unaccounted_for = 3 "
                "WHERE run_id = ?", (run_id,))


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    for sub in ("output", "output/audits", "output/quarantine", "contracts_registry", "data"):
        (storage / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "output_dir", storage / "output")
    monkeypatch.setattr(settings, "contracts_dir", storage / "contracts_registry")
    monkeypatch.setattr(settings, "data_dir", storage / "data")
    monkeypatch.setattr(settings, "audit_dir", storage / "output" / "audits")
    monkeypatch.setattr(settings, "quarantine_dir", storage / "output" / "quarantine")
    monkeypatch.setattr(settings, "billing_provider", "dry-run")
    return Harness(storage)


# --------------------------------------------------------------------- records


def _records(delivered: int = 5, held: int = 3, refused: int = 2) -> list:
    """Deterministic feed: conforming rows plus held/refused defects."""
    rows = [
        {"settlement_id": f"S-{i:04d}", "amount": 100.0 + i, "currency": "USD"}
        for i in range(delivered)
    ]
    rows += [
        {"settlement_id": f"H-{i:04d}", "amount": -1.0, "currency": "USD"}
        for i in range(held)
    ]
    rows += [
        {"settlement_id": f"R-{i:04d}", "amount": "not-a-number", "currency": "USD"}
        for i in range(refused)
    ]
    return rows


def _run_delivery(harness: Harness, records: list, *, tenant: str = ALPHA,
                  drift: bool = False):
    """Execute a real pipeline run; returns (run_id, audit_path, result)."""
    contract = DataContract(**json.loads(DATA_CONTRACT_PATH.read_text()))
    tag = uuid.uuid4().hex[:10]
    source = harness.storage / f"feed_{tag}.jsonl"
    source.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    target = harness.storage / "targets" / f"delivered_{tag}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    audit = harness.storage / "audits" / f"audit_{tag}.json"
    pipeline = DataShellPipeline(contract, tenant_id=tenant, drift_enabled=drift)
    result = pipeline.run_file(
        source_path=str(source), target_path_override=str(target),
        audit_output_path=str(audit))
    return result.run_id, audit, result


def _commercial(contract_id: str = "ctr_test", version: int = 1, *,
                unit_price_minor: int = 5, expected_units: int = 5,
                minimum_accepted_units: int = 1, policy: str = "PARTIAL_WITH_MINIMUM",
                max_units=None, tolerance_bps: int = 10000,
                penalty: dict | None = None, pricing: dict | None = None,
                qualification: dict | None = None, currency: str = "USD",
                **extra) -> CommercialContract:
    return CommercialContract(
        contract_id=contract_id, contract_version=version, buyer_id="buyer-x",
        seller_id="seller-y", feed_id="vendor-settlements", currency=currency,
        pricing=pricing or {
            "model": "PER_ACCEPTED_UNIT", "currency": currency,
            "unit_price_minor": unit_price_minor},
        qualification=qualification or {"billable_unit": "DELIVERED_ROWS"},
        acceptance={
            "policy": policy,
            "minimum_accepted_units": minimum_accepted_units,
            "expected_units": expected_units,
            "maximum_units": max_units,
            "allowed_failure_tolerance_bps": tolerance_bps,
        },
        penalty=penalty or {"model": "NONE"},
        effective_at="2026-01-01T00:00:00Z",
        **extra,
    )


def _register(harness: Harness, contract: CommercialContract, tenant: str = ALPHA):
    return harness.service.register_contract(contract, tenant_id=tenant, created_by="test")


# --------------------------------------------------------------- service flow


class TestSettlementCalculationFlow:
    def test_calculate_from_real_run_and_persist(self, harness):
        run_id, audit, result = _run_delivery(harness, _records())
        assert result.ledger.actual_delivered == 5
        contract = _register(harness, _commercial())

        row, created = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test",
            actor="test", idempotency_key=f"{ALPHA}:{run_id}")

        assert created is True
        assert row["settlement_id"] == f"stl_{run_id}_ctr_test_v1"
        assert row["acceptance_status"] == "ACCEPTED"
        assert row["settlement_status"] == "READY_FOR_BILLING"
        calc = row["calculation"]
        assert calc["qualified_units"] == 5          # delivered rows only
        assert calc["gross_amount_minor"] == 25      # 5 x 0.05 USD, exact
        assert calc["pricing_model"] == "PER_ACCEPTED_UNIT"
        assert calc["rounding_mode"] == "ROUND_HALF_UP"
        assert row["contract_digest"] == contract["contract_digest"]
        assert row["certificate_digest"] == certificate_digest(
            json.loads(audit.read_text()))

        # Durably persisted and replayable from storage.
        stored = harness.ledger.get_settlement(row["settlement_id"], ALPHA)
        assert stored is not None and stored["gross_amount_minor"] == 25
        events = harness.ledger.get_events(row["settlement_id"], ALPHA)
        assert [e["event_type"] for e in events] == ["CREATED"]

    def test_explanation_and_receipt_verification(self, harness):
        run_id, _, _ = _run_delivery(harness, _records())
        _register(harness, _commercial())
        row, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")

        explanation = harness.service.explain(row["settlement_id"], ALPHA)
        text = "\n".join(explanation["explanation"])
        assert "Qualified units:    5" in text
        assert "Gross amount:       0.25 USD (25 minor)" in text
        assert "ROUND_HALF_UP applied once" in text

        report = harness.service.verify_settlement(row["settlement_id"], ALPHA)
        assert report["status"] == "AUTHENTIC_AND_UNMODIFIED"
        assert report["signature_valid"] is True
        # HMAC is a shared-secret mechanism and is never labelled third-party
        # verifiable.
        assert report["signature_algorithm"] == "HMAC-SHA256"
        assert report["third_party_verifiable"] is False

    def test_held_and_refused_rows_are_never_billable(self, harness):
        run_id, _, _ = _run_delivery(harness, _records(delivered=5, held=3, refused=2))
        _register(harness, _commercial())
        row, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")
        # 10 source rows, 5 delivered: the billable quantity is 5, not 10.
        assert row["calculation"]["qualified_units"] == 5
        assert row["calculation"]["gross_amount_minor"] == 25

    def test_tampered_delivered_artifact_refuses_calculation(self, harness):
        run_id, _, result = _run_delivery(harness, _records())
        _register(harness, _commercial())
        delivered = Path(result.outcome.target_path) if hasattr(result.outcome, "target_path") else None
        if delivered is None or not delivered.is_file():
            # Locate the delivered parquet under the storage targets directory.
            delivered = next((harness.storage / "targets").glob("*.parquet"))
        delivered.write_bytes(delivered.read_bytes() + b"tamper")

        with pytest.raises(SettlementServiceError, match="certificate verification failed"):
            harness.service.calculate(
                tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")
        assert harness.ledger.list_settlements(ALPHA) == []

    def test_missing_audit_file_refuses_calculation(self, harness):
        run_id, audit, _ = _run_delivery(harness, _records())
        _register(harness, _commercial())
        audit.unlink()
        with pytest.raises(SettlementServiceError, match="certificate file is missing"):
            harness.service.calculate(
                tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")

    def test_reconciliation_mismatch_is_never_payable(self, harness):
        run_id, _, _ = _run_delivery(harness, _records())
        _register(harness, _commercial())
        harness.tamper_run_footing(run_id)

        row, created = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")
        assert created is True
        assert row["acceptance_status"] == "REJECTED"
        assert row["settlement_status"] == "PENDING_REVIEW"
        assert "RECONCILIATION_MISMATCH" in row["reason_codes"]
        assert row["calculation"]["gross_amount_minor"] == 0

        # Billing fails closed on a non-payable statement.
        result = DryRunBillingProvider(harness.ledger).create_draft_invoice(
            InvoicePayload(tenant_id=ALPHA, settlement_id=row["settlement_id"],
                           currency="USD", amount_minor=0),
            idempotency_key="bill-nonpayable")
        assert result.ok is False
        assert result.error_code == "SETTLEMENT_NOT_BILLABLE"

    def test_rejected_run_is_never_payable(self, harness):
        # Only 2 conforming rows: the MIN_ROW_COUNT batch rule rejects the run.
        run_id, _, result = _run_delivery(harness, _records(delivered=2, held=0, refused=0))
        assert result.status.value == "REJECTED"
        _register(harness, _commercial())
        row, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")
        assert row["acceptance_status"] == "REJECTED"
        assert "BATCH_REJECTED" in row["reason_codes"]
        assert row["calculation"]["gross_amount_minor"] == 0
        assert row["settlement_status"] != "READY_FOR_BILLING"

    def test_partial_acceptance_is_payable_with_reason(self, harness):
        run_id, _, _ = _run_delivery(harness, _records())
        _register(harness, _commercial(expected_units=6))
        row, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")
        assert row["acceptance_status"] == "PARTIALLY_ACCEPTED"
        assert "MANUAL_REVIEW_REQUIRED" in row["reason_codes"]
        assert row["settlement_status"] == "READY_FOR_BILLING"
        assert row["calculation"]["gross_amount_minor"] == 25

    def test_minimum_breach_under_partial_policy_rejects(self, harness):
        run_id, _, _ = _run_delivery(harness, _records(delivered=5))
        _register(harness, _commercial(minimum_accepted_units=10, expected_units=10))
        row, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")
        assert row["acceptance_status"] == "REJECTED"
        assert "MINIMUM_QUANTITY_NOT_MET" in row["reason_codes"]
        assert row["calculation"]["gross_amount_minor"] == 0

    def test_review_on_shortfall_is_not_payable(self, harness):
        run_id, _, _ = _run_delivery(harness, _records())
        _register(harness, _commercial(policy="REVIEW_ON_SHORTFALL", expected_units=6))
        row, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")
        assert row["acceptance_status"] == "PENDING_REVIEW"
        assert row["settlement_status"] == "PENDING_REVIEW"
        assert row["calculation"]["gross_amount_minor"] == 0

    def test_contract_version_is_exact_and_immutable(self, harness):
        run_id, _, _ = _run_delivery(harness, _records())
        v1 = _register(harness, _commercial(version=1, unit_price_minor=5))["contract_digest"]
        v2 = _register(harness, _commercial(version=2, unit_price_minor=7))["contract_digest"]
        assert v1 != v2

        # A conflicting payload for an existing version is rejected.
        with pytest.raises((CommercialContractRegistryError, Exception)):
            _register(harness, _commercial(version=1, unit_price_minor=9))

        # Calculating against v1 after v2 exists still binds v1 exactly.
        row, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test",
            contract_version=1, actor="test")
        assert row["contract_version"] == 1
        assert row["contract_digest"] == v1
        assert row["calculation"]["gross_amount_minor"] == 25

        # Latest resolves to v2; a nonexistent version fails closed.
        row2, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")
        assert row2["contract_version"] == 2
        assert row2["calculation"]["gross_amount_minor"] == 35
        with pytest.raises(SettlementServiceError, match="not found"):
            harness.service.calculate(
                tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test",
                contract_version=99, actor="test")

    def test_contract_version_digest_immutability_in_db(self, harness):
        contract = _commercial(version=1)
        _register(harness, contract)
        record = harness.ledger.get_contract_version("ctr_test", 1, ALPHA)
        assert record["digest"] == contract.contract_digest
        # Re-registering the identical version is idempotent.
        assert harness.ledger.record_contract_version(contract, ALPHA, "test2") is None

    def test_idempotency_key_replays_the_original(self, harness):
        run_id, _, _ = _run_delivery(harness, _records())
        _register(harness, _commercial())
        key = f"{ALPHA}:{run_id}:ctr_test"

        first, created1 = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test",
            actor="test", idempotency_key=key)
        second, created2 = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test",
            actor="test-2", idempotency_key=key)

        assert created1 is True and created2 is False
        assert first["settlement_id"] == second["settlement_id"]
        assert first["created_at"] == second["created_at"]
        assert len(harness.ledger.list_settlements(ALPHA)) == 1

    def test_recalculation_is_idempotent_without_a_key(self, harness):
        run_id, _, _ = _run_delivery(harness, _records())
        _register(harness, _commercial())
        first, created1 = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")
        second, created2 = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="other")
        assert created1 is True and created2 is False
        assert first["settlement_id"] == second["settlement_id"]
        assert len(harness.ledger.list_settlements(ALPHA)) == 1

    def test_cross_tenant_calculation_and_reads_are_isolated(self, harness):
        run_id, _, _ = _run_delivery(harness, _records(), tenant=ALPHA)
        _register(harness, _commercial(), tenant=ALPHA)

        with pytest.raises(SettlementServiceError, match="not found"):
            harness.service.calculate(
                tenant_id=BETA, run_id=run_id, contract_id="ctr_test", actor="beta")

        row, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="alpha")
        assert harness.service.get_settlement(row["settlement_id"], BETA) is None
        assert harness.service.list_settlements(BETA) == []
        assert harness.service.list_settlements(ALPHA)[0]["settlement_id"] == row["settlement_id"]

    def test_state_survives_a_process_restart(self, harness):
        run_id, _, _ = _run_delivery(harness, _records())
        _register(harness, _commercial())
        key = "restart-key"
        row, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test",
            actor="test", idempotency_key=key)

        restarted = harness.fresh_service()
        stored = restarted.get_settlement(row["settlement_id"], ALPHA)
        assert stored is not None and stored["calculation"]["gross_amount_minor"] == 25
        replay, created = restarted.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test",
            actor="restarted", idempotency_key=key)
        assert created is False and replay["settlement_id"] == row["settlement_id"]
        report = restarted.verify_settlement(row["settlement_id"], ALPHA)
        assert report["status"] == "AUTHENTIC_AND_UNMODIFIED"


# ---------------------------------------------------------------- disputes


class TestDisputeLifecycle:
    def _settled(self, harness):
        run_id, _, _ = _run_delivery(harness, _records())
        _register(harness, _commercial())
        row, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")
        assert row["settlement_status"] == "READY_FOR_BILLING"
        return row

    def test_dispute_blocks_billing_until_resolved(self, harness):
        row = self._settled(harness)
        dispute = harness.service.open_dispute(
            settlement_id=row["settlement_id"], tenant_id=ALPHA,
            disputing_party="buyer-x", reason="quantity questioned",
            evidence_references=["audit:delivered"], actor="buyer-x")

        stored = harness.ledger.get_settlement(row["settlement_id"], ALPHA)
        assert stored["settlement_status"] == "DISPUTED"
        assert stored["calculation"]["gross_amount_minor"] == 25  # evidence preserved

        blocked = DryRunBillingProvider(harness.ledger).create_draft_invoice(
            InvoicePayload(tenant_id=ALPHA, settlement_id=row["settlement_id"],
                           currency="USD", amount_minor=25),
            idempotency_key="bill-blocked")
        assert blocked.ok is False
        assert blocked.error_code in ("SETTLEMENT_NOT_BILLABLE", "DISPUTE_OPEN")

        result = harness.service.resolve_dispute(
            dispute_id=dispute.dispute_id, tenant_id=ALPHA,
            resolution=DisputeResolution.ACCEPT_AS_IS,
            resolution_actor="reviewer", resolution_reason="evidence confirmed")
        assert result["settlement_id"] == row["settlement_id"]
        assert harness.ledger.get_settlement(row["settlement_id"], ALPHA)[
            "settlement_status"] == "READY_FOR_BILLING"

        allowed = DryRunBillingProvider(harness.ledger).create_draft_invoice(
            InvoicePayload(tenant_id=ALPHA, settlement_id=row["settlement_id"],
                           currency="USD", amount_minor=25),
            idempotency_key="bill-after-resolution")
        assert allowed.ok is True

        events = [e["event_type"] for e in
                  harness.ledger.get_events(row["settlement_id"], ALPHA)]
        assert "DISPUTE_OPENED" in events and "DISPUTE_RESOLVED" in events

    def test_dispute_resolution_events_are_append_only(self, harness):
        row = self._settled(harness)
        dispute = harness.service.open_dispute(
            settlement_id=row["settlement_id"], tenant_id=ALPHA,
            disputing_party="buyer-x", reason="r", actor="buyer-x")
        harness.service.resolve_dispute(
            dispute_id=dispute.dispute_id, tenant_id=ALPHA,
            resolution=DisputeResolution.CORRECT_CALCULATION,
            resolution_actor="reviewer", resolution_reason="corrected",
            adjustment_settlement_id="stl_manual_adjustment")
        stored = harness.ledger.get_dispute(dispute.dispute_id, ALPHA)
        assert stored["status"] == "RESOLVED"
        assert stored["adjustment_settlement_id"] == "stl_manual_adjustment"
        # Resolving twice is refused; the original record is preserved.
        with pytest.raises(SettlementServiceError, match="already"):
            harness.service.resolve_dispute(
                dispute_id=dispute.dispute_id, tenant_id=ALPHA,
                resolution=DisputeResolution.ACCEPT_AS_IS,
                resolution_actor="reviewer-2", resolution_reason="again")

    def test_reject_settlement_cancels_and_never_bills(self, harness):
        row = self._settled(harness)
        dispute = harness.service.open_dispute(
            settlement_id=row["settlement_id"], tenant_id=ALPHA,
            disputing_party="buyer-x", reason="wrong units", actor="buyer-x")
        harness.service.resolve_dispute(
            dispute_id=dispute.dispute_id, tenant_id=ALPHA,
            resolution=DisputeResolution.REJECT_SETTLEMENT,
            resolution_actor="reviewer", resolution_reason="confirmed error")
        stored = harness.ledger.get_settlement(row["settlement_id"], ALPHA)
        assert stored["settlement_status"] == "CANCELLED"
        blocked = DryRunBillingProvider(harness.ledger).create_draft_invoice(
            InvoicePayload(tenant_id=ALPHA, settlement_id=row["settlement_id"],
                           currency="USD", amount_minor=25),
            idempotency_key="bill-cancelled")
        assert blocked.ok is False
        with pytest.raises(SettlementServiceError, match="cannot be opened"):
            harness.service.open_dispute(
                settlement_id=row["settlement_id"], tenant_id=ALPHA,
                disputing_party="buyer-x", reason="again", actor="buyer-x")

    def test_recalculation_while_disputed_marks_disputed(self, harness):
        row = self._settled(harness)
        harness.service.open_dispute(
            settlement_id=row["settlement_id"], tenant_id=ALPHA,
            disputing_party="buyer-x", reason="open", actor="buyer-x")
        rerun, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=row["run_id"], contract_id="ctr_test",
            actor="test", idempotency_key="recalc-during-dispute")
        assert rerun["settlement_status"] == "DISPUTED"

    def test_supersede_preserves_original_history(self, harness):
        row = self._settled(harness)
        replacement = {"settlement_id": "stl_correction_1"}
        updated = harness.service.supersede_settlement(
            settlement_id=row["settlement_id"], tenant_id=ALPHA,
            replacement=replacement, actor="reviewer")
        assert updated["settlement_status"] == "SUPERSEDED"
        assert updated["calculation"]["gross_amount_minor"] == 25  # unchanged
        events = [e["event_type"] for e in
                  harness.ledger.get_events(row["settlement_id"], ALPHA)]
        assert "SUPERSEDED" in events


# ------------------------------------------------------------------ receipts


class TestSettlementReceipts:
    def _receipt_fixture(self, harness):
        run_id, audit, _ = _run_delivery(harness, _records())
        _register(harness, _commercial())
        row, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")
        cert_data = json.loads(audit.read_text())
        statement = SettlementStatement(**{**row, "calculation": row["calculation"]})
        receipt = build_settlement_receipt(statement, cert_data)
        return run_id, audit, cert_data, statement, receipt

    def test_receipt_tamper_on_any_settlement_field_fails(self, harness):
        _, _, cert_data, _, receipt = self._receipt_fixture(harness)
        for mutate in (
            lambda r: r["settlement_evidence"]["calculation"].__setitem__(
                "gross_amount_minor", r["settlement_evidence"]["calculation"]["gross_amount_minor"] + 1),
            lambda r: r["settlement_evidence"]["calculation"].__setitem__(
                "qualified_units", 999),
            lambda r: r["settlement_evidence"].__setitem__("contract_digest", "0" * 64),
            lambda r: r["contract"].__setitem__("contract_version", 99),
            lambda r: r.__setitem__("run_id", "dm_run_forged"),
        ):
            tampered = json.loads(json.dumps(receipt))
            mutate(tampered)
            report = verify_settlement_receipt(tampered, delivery_certificate=cert_data)
            assert report["status"] == "TAMPERED_OR_INVALID_SIGNATURE", report

    def test_receipt_binds_the_delivery_certificate(self, harness):
        _, _, cert_data, _, receipt = self._receipt_fixture(harness)
        ok = verify_settlement_receipt(receipt, delivery_certificate=cert_data)
        assert ok["status"] == "AUTHENTIC_AND_UNMODIFIED"
        assert ok["artifacts_status"] == "DELIVERY_CERTIFICATE_VERIFIED"

        other = json.loads(json.dumps(cert_data))
        other["reconciliation"]["rows_delivered"] += 1
        mismatch = verify_settlement_receipt(receipt, delivery_certificate=other)
        assert mismatch["status"] == "DELIVERY_CERTIFICATE_DIGEST_MISMATCH"

    def test_receipt_evidence_must_match_expected_statement(self, harness):
        _, _, cert_data, statement, receipt = self._receipt_fixture(harness)
        forged = statement.model_copy(update={
            "calculation": statement.calculation.model_copy(
                update={"gross_amount_minor": statement.calculation.gross_amount_minor + 1})})
        report = verify_settlement_receipt(
            receipt, delivery_certificate=cert_data, expected_statement=forged)
        assert report["status"] == "SETTLEMENT_EVIDENCE_MISMATCH"

    def test_receipts_are_ed25519_when_provisioned(self, harness, monkeypatch):
        private_pem, public_pem = generate_keypair_pem()
        monkeypatch.setattr(settings, "ed25519_private_key", private_pem)
        monkeypatch.setattr(settings, "ed25519_active_key_id", "test-ed25519-v1")
        monkeypatch.setattr(settings, "ed25519_trusted_keys", {"test-ed25519-v1": public_pem})

        # The delivery certificate is signed with Ed25519 too.
        run_id, audit, _ = _run_delivery(harness, _records())
        cert_data = json.loads(audit.read_text())
        assert cert_data["digital_signature"]["algorithm"] == "Ed25519"

        _register(harness, _commercial())
        row, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")
        statement = SettlementStatement(**{**row, "calculation": row["calculation"]})
        receipt = build_settlement_receipt(statement, cert_data)
        assert receipt["digital_signature"]["algorithm"] == "Ed25519"
        assert receipt["digital_signature"]["third_party_verifiable"] is True

        report = verify_settlement_receipt(receipt, delivery_certificate=cert_data)
        assert report["status"] == "AUTHENTIC_AND_UNMODIFIED"
        assert report["third_party_verifiable"] is True

        tampered = json.loads(json.dumps(receipt))
        tampered["settlement_evidence"]["settlement_status"] = "CANCELLED"
        assert verify_settlement_receipt(
            tampered, delivery_certificate=cert_data)["status"] == "TAMPERED_OR_INVALID_SIGNATURE"


# ------------------------------------------------------------------- billing


class TestBillingAdapters:
    def _billable(self, harness):
        run_id, _, _ = _run_delivery(harness, _records())
        _register(harness, _commercial())
        row, _ = harness.service.calculate(
            tenant_id=ALPHA, run_id=run_id, contract_id="ctr_test", actor="test")
        return row

    def _payload(self, row, amount: int = 25) -> InvoicePayload:
        return InvoicePayload(
            tenant_id=ALPHA, settlement_id=row["settlement_id"],
            contract_id="ctr_test", contract_version=1, run_id=row["run_id"],
            certificate_digest=row["certificate_digest"], currency="USD",
            amount_minor=amount, description="test")


    def test_dry_run_provider_is_idempotent(self, harness):
        row = self._billable(harness)
        provider = DryRunBillingProvider(harness.ledger)
        first = provider.create_draft_invoice(self._payload(row), idempotency_key="dry-1")
        second = provider.create_draft_invoice(self._payload(row), idempotency_key="dry-1")
        assert first.ok is True and second.ok is True
        assert first.provider_invoice_id == second.provider_invoice_id
        assert second.request_meta.get("deduplicated") is True
        instructions = harness.ledger.list_billing_instructions(ALPHA)
        assert len(instructions) == 1
        assert instructions[0]["amount_minor"] == 25

    def test_stripe_is_disabled_by_default(self, harness):
        provider = StripeBillingProvider(harness.ledger, secret_key="")
        assert provider.is_configured() is False
        with pytest.raises(RuntimeError, match="not configured"):
            provider.create_draft_invoice(self._payload(
                {"settlement_id": "stl_x", "run_id": "r", "certificate_digest": "d"}),
                idempotency_key="x")

    def test_stripe_rejects_non_test_key_in_test_mode(self, harness):
        provider = StripeBillingProvider(
            harness.ledger, secret_key="sk_live_123", customer_id="cus_1", test_mode=True)
        with pytest.raises(RuntimeError, match="test mode"):
            provider.create_draft_invoice(self._payload({"settlement_id": "stl_x", "run_id": "r",
                                          "certificate_digest": "d"}),
                                          idempotency_key="x")

    def test_stripe_draft_invoice_via_fake_transport(self, harness):
        row = self._billable(harness)
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.url.path.endswith("/invoiceitems"):
                return httpx.Response(200, json={"id": "ii_fake_1"})
            if request.url.path.endswith("/invoices"):
                form = dict(parse_qsl(request.content.decode()))
                assert form["auto_advance"] == "false"
                assert form["metadata[settlement_id]"] == row["settlement_id"]
                return httpx.Response(200, json={"id": "in_fake_1", "status": "draft"})
            raise AssertionError(f"unexpected path {request.url.path}")

        client = httpx.Client(
            base_url="https://api.stripe.com/v1",
            transport=httpx.MockTransport(handler))
        provider = StripeBillingProvider(
            harness.ledger, secret_key="sk_test_123", customer_id="cus_1",
            test_mode=True, transport=client)

        result = provider.create_draft_invoice(self._payload(row), idempotency_key="stripe-1")
        assert result.ok is True
        assert result.status == "PROVIDER_INVOICE_CREATED"
        assert result.provider_invoice_id == "in_fake_1"

        # Draft-only: an invoice item and a draft invoice, never finalized.
        assert len(calls) == 2
        item_call, invoice_call = calls
        assert item_call.headers["Idempotency-Key"] == "stripe-1-item"
        assert invoice_call.headers["Idempotency-Key"] == "stripe-1-invoice"
        item_form = dict(parse_qsl(item_call.content.decode()))
        assert item_form["amount"] == "25"
        assert item_form["currency"] == "usd"

        instruction = harness.ledger.find_billing_instruction(
            ALPHA, "stripe", "in_fake_1")
        assert instruction is not None
        assert instruction["status"] == "PROVIDER_INVOICE_CREATED"
        assert "sk_test_123" not in json.dumps(instruction["request_meta_json"])

        # A retry with the same key replays the recorded instruction without
        # any further network call.
        replay = provider.create_draft_invoice(self._payload(row), idempotency_key="stripe-1")
        assert replay.ok is True
        assert replay.request_meta.get("deduplicated") is True
        assert len(calls) == 2

    def test_stripe_timeout_is_unresolved_and_never_retried_blindly(self, harness):
        row = self._billable(harness)

        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated timeout", request=request)

        client = httpx.Client(base_url="https://api.stripe.com/v1",
                              transport=httpx.MockTransport(timeout_handler))
        provider = StripeBillingProvider(
            harness.ledger, secret_key="sk_test_123", customer_id="cus_1",
            test_mode=True, transport=client)

        first = provider.create_draft_invoice(self._payload(row), idempotency_key="stripe-timeout")
        assert first.ok is False
        assert first.ambiguous is True
        assert first.status == "UNRESOLVED"
        assert first.error_code == "PROVIDER_TIMEOUT"

        instruction = harness.ledger.find_billing_by_idempotency(ALPHA, "stripe-timeout")
        assert instruction is not None and instruction["status"] == "UNRESOLVED"

        # A retry does not create a second attempt: reconciliation is required.
        retry = provider.create_draft_invoice(self._payload(row), idempotency_key="stripe-timeout")
        assert retry.ok is False and retry.ambiguous is True
        assert retry.error_code == "RECONCILIATION_REQUIRED"
        assert len(harness.ledger.list_billing_instructions(ALPHA)) == 1


# ------------------------------------------------------------------ webhooks


class TestWebhooks:
    def _sign(self, body: bytes, secret: str, timestamp: float) -> str:
        import hashlib
        import hmac as hmac_mod
        signed = str(int(timestamp)).encode() + b"." + body
        digest = hmac_mod.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return f"t={int(timestamp)},v1={digest}"

    def test_signature_verification_and_dedup(self, harness):
        provider = StripeBillingProvider(
            harness.ledger, secret_key="sk_test_123", webhook_secret="whsec_test")
        body = json.dumps({"id": "evt_1", "type": "invoice.finalized"}).encode()
        header = self._sign(body, "whsec_test", now_ts := __import__("time").time())

        event = provider.verify_webhook(body=body, signature_header=header)
        assert event["id"] == "evt_1" and not event.get("_deduplicated")
        replay = provider.verify_webhook(body=body, signature_header=header)
        assert replay["_deduplicated"] is True
        assert now_ts  # silence linter about the walrus

    def test_tampered_or_stale_webhooks_fail_closed(self, harness):
        from datashell.billing.webhooks import WebhookVerificationError
        provider = StripeBillingProvider(
            harness.ledger, secret_key="sk_test_123", webhook_secret="whsec_test")
        body = json.dumps({"id": "evt_2"}).encode()
        good = self._sign(body, "whsec_test", __import__("time").time())

        with pytest.raises(WebhookVerificationError):
            provider.verify_webhook(body=body + b"x", signature_header=good)
        with pytest.raises(WebhookVerificationError):
            provider.verify_webhook(body=body, signature_header="garbage")
        stale = self._sign(body, "whsec_test", __import__("time").time() - 10_000)
        with pytest.raises(WebhookVerificationError):
            provider.verify_webhook(body=body, signature_header=stale)
        with pytest.raises(WebhookVerificationError):
            StripeBillingProvider(harness.ledger, secret_key="sk_test_123").verify_webhook(
                body=body, signature_header=good)


# ----------------------------------------------------------------------- API


API_ALPHA_KEY = f"stl_it_alpha_{uuid.uuid4().hex[:8]}"
API_AUDITOR_KEY = f"stl_it_auditor_{uuid.uuid4().hex[:8]}"
API_PRODUCER_KEY = f"stl_it_producer_{uuid.uuid4().hex[:8]}"
API_BETA_KEY = f"stl_it_beta_{uuid.uuid4().hex[:8]}"


def _api_headers(key: str) -> dict:
    return {"X-API-Key": key}


@pytest.fixture()
def api(harness, monkeypatch, tmp_path):
    register_api_key(APIKeyMetadata(key=API_ALPHA_KEY, tenant_id=ALPHA,
                                    role=Role.DATA_ENGINEER, description="alpha engineer"))
    register_api_key(APIKeyMetadata(key=API_AUDITOR_KEY, tenant_id=ALPHA,
                                    role=Role.AUDITOR, description="alpha auditor"))
    register_api_key(APIKeyMetadata(key=API_PRODUCER_KEY, tenant_id=ALPHA,
                                    role=Role.PRODUCER, description="alpha producer"))
    register_api_key(APIKeyMetadata(key=API_BETA_KEY, tenant_id=BETA,
                                    role=Role.DATA_ENGINEER, description="beta engineer"))
    monkeypatch.setattr(settlements_router, "_settlement_service", harness.service)
    return TestClient(app)


def _contract_body(contract_id: str = "ctr_api", version: int = 1,
                   unit_price_minor: int = 5) -> dict:
    payload = json.loads(
        (REPO_ROOT / "examples" / "commercial_delivery_contract.json").read_text())
    payload["contract_id"] = contract_id
    payload["contract_version"] = version
    payload["pricing"]["unit_price_minor"] = unit_price_minor
    payload["acceptance"]["minimum_accepted_units"] = 1
    payload["acceptance"]["expected_units"] = 5
    return payload


class TestSettlementAPI:
    def _prepared(self, harness):
        run_id, _, _ = _run_delivery(harness, _records())
        return run_id

    def test_settlement_endpoints_require_authentication(self, api):
        assert api.get("/api/v1/settlements").status_code == 401
        assert api.post("/api/v1/settlements/calculate", json={
            "run_id": "r", "contract_id": "c"}).status_code == 401

    def test_settlement_endpoints_require_capability(self, api, harness):
        run_id = self._prepared(harness)
        # PRODUCER has no settlement capabilities.
        assert api.get("/api/v1/settlements",
                       headers=_api_headers(API_PRODUCER_KEY)).status_code == 403
        assert api.post("/api/v1/settlements/calculate", headers=_api_headers(API_PRODUCER_KEY),
                        json={"run_id": run_id, "contract_id": "ctr_api"}).status_code == 403
        # AUDITOR can read but not write.
        assert api.get("/api/v1/settlements",
                       headers=_api_headers(API_AUDITOR_KEY)).status_code == 200
        assert api.post("/api/v1/settlements/calculate", headers=_api_headers(API_AUDITOR_KEY),
                        json={"run_id": run_id, "contract_id": "ctr_api"}).status_code == 403

    def test_full_api_flow_with_idempotent_retry(self, api, harness):
        run_id = self._prepared(harness)
        created = api.post("/api/v1/commercial-contracts",
                           headers=_api_headers(API_ALPHA_KEY),
                           json=_contract_body())
        assert created.status_code == 201
        digest = created.json()["contract_digest"]

        first = api.post("/api/v1/settlements/calculate",
                         headers=_api_headers(API_ALPHA_KEY),
                         json={"run_id": run_id, "contract_id": "ctr_api",
                               "idempotency_key": "api-1"})
        assert first.status_code == 200
        body = first.json()
        assert body["created"] is True
        assert body["gross_amount_minor"] == 25
        assert body["contract_digest"] == digest

        replay = api.post("/api/v1/settlements/calculate",
                          headers=_api_headers(API_ALPHA_KEY),
                          json={"run_id": run_id, "contract_id": "ctr_api",
                                "idempotency_key": "api-1"})
        assert replay.json()["created"] is False
        assert replay.json()["settlement_id"] == body["settlement_id"]

        listing = api.get("/api/v1/settlements", headers=_api_headers(API_ALPHA_KEY))
        assert listing.status_code == 200
        assert listing.json()["count"] == 1 and listing.json()["total"] == 1

        sid = body["settlement_id"]
        detail = api.get(f"/api/v1/settlements/{sid}", headers=_api_headers(API_ALPHA_KEY))
        assert detail.status_code == 200 and detail.json()["settlement_id"] == sid
        explanation = api.get(f"/api/v1/settlements/{sid}/explanation",
                              headers=_api_headers(API_ALPHA_KEY))
        assert explanation.status_code == 200
        verification = api.post(f"/api/v1/settlements/{sid}/verify",
                                headers=_api_headers(API_ALPHA_KEY))
        assert verification.status_code == 200
        assert verification.json()["status"] == "AUTHENTIC_AND_UNMODIFIED"

    def test_settlement_preview_aggregates_portfolio(self, api, harness):
        """GET /api/v1/settlement/preview is a bounded read-only dashboard roll-up."""
        run_id = self._prepared(harness)
        created = api.post("/api/v1/commercial-contracts",
                           headers=_api_headers(API_ALPHA_KEY),
                           json=_contract_body())
        assert created.status_code == 201
        calculated = api.post("/api/v1/settlements/calculate",
                              headers=_api_headers(API_ALPHA_KEY),
                              json={"run_id": run_id, "contract_id": "ctr_api",
                                    "idempotency_key": "preview-1"})
        assert calculated.status_code == 200

        payload = api.get("/api/v1/settlement/preview",
                          headers=_api_headers(API_ALPHA_KEY)).json()
        assert payload["tenant_id"] == ALPHA
        assert payload["total"] == 1 and payload["count"] == 1
        currency_totals = payload["totals_by_currency"]["USD"]
        assert currency_totals["settlement_count"] == 1
        assert currency_totals["gross_amount_minor"] == 25
        assert payload["status_counts"]["READY_FOR_BILLING"] == 1
        assert len(payload["settlements"]) == 1
        first = payload["settlements"][0]
        assert first["settlement_id"] == calculated.json()["settlement_id"]
        assert first["settlement_status"] == "READY_FOR_BILLING"
        assert first["acceptance_status"] == "ACCEPTED"
        assert {"settlement_id", "contract_id", "run_id", "currency",
                "gross_amount_minor", "settlement_status",
                "acceptance_status"} == set(first)

    def test_settlement_preview_is_tenant_scoped_and_read_only(self, api, harness):
        """Beta cannot see alpha's portfolio; preview mutates nothing."""
        run_id = self._prepared(harness)
        api.post("/api/v1/commercial-contracts",
                 headers=_api_headers(API_ALPHA_KEY), json=_contract_body())
        api.post("/api/v1/settlements/calculate",
                 headers=_api_headers(API_ALPHA_KEY),
                 json={"run_id": run_id, "contract_id": "ctr_api",
                       "idempotency_key": "scope-1"})

        beta = api.get("/api/v1/settlement/preview", headers=_api_headers(API_BETA_KEY)).json()
        assert beta["tenant_id"] == BETA
        assert beta["total"] == 0 and beta["totals_by_currency"] == {}
        assert beta["settlements"] == []

        before = api.get("/api/v1/settlements", headers=_api_headers(API_ALPHA_KEY)).json()["total"]
        scoped = api.get("/api/v1/settlement/preview", headers=_api_headers(API_ALPHA_KEY)).json()
        assert scoped["total"] == before

    def test_api_cross_tenant_isolation(self, api, harness):
        run_id = self._prepared(harness)
        api.post("/api/v1/commercial-contracts", headers=_api_headers(API_ALPHA_KEY),
                 json=_contract_body())
        sid = api.post("/api/v1/settlements/calculate", headers=_api_headers(API_ALPHA_KEY),
                       json={"run_id": run_id, "contract_id": "ctr_api"}).json()["settlement_id"]

        assert api.get(f"/api/v1/settlements/{sid}",
                       headers=_api_headers(API_BETA_KEY)).status_code == 404
        beta_list = api.get("/api/v1/settlements", headers=_api_headers(API_BETA_KEY)).json()
        assert beta_list["count"] == 0
        # Beta cannot even see the contract.
        assert api.get("/api/v1/commercial-contracts/ctr_api",
                       headers=_api_headers(API_BETA_KEY)).status_code == 404

    def test_api_malformed_requests_fail_safely(self, api, harness):
        # Unknown run: a clean 422 refusal, not a payable outcome.
        resp = api.post("/api/v1/settlements/calculate", headers=_api_headers(API_ALPHA_KEY),
                        json={"run_id": "dm_run_missing", "contract_id": "ctr_api"})
        assert resp.status_code == 422

        # Path-like / malformed identifiers are rejected at the schema boundary.
        for bad in ("../escape", "bad id", "", "x" * 200):
            resp = api.post("/api/v1/settlements/calculate",
                            headers=_api_headers(API_ALPHA_KEY),
                            json={"run_id": "dm_run_x", "contract_id": bad})
            assert resp.status_code == 422, bad

        # Unknown settlement / malformed path identifiers.
        assert api.get("/api/v1/settlements/stl_unknown",
                       headers=_api_headers(API_ALPHA_KEY)).status_code == 404
        assert api.get("/api/v1/settlements/stl_bad*id",
                       headers=_api_headers(API_ALPHA_KEY)).status_code == 400
        assert api.get("/api/v1/settlements/bad..id",
                       headers=_api_headers(API_ALPHA_KEY)).status_code == 404
        assert api.post("/api/v1/commercial-contracts",
                        headers=_api_headers(API_ALPHA_KEY),
                        json={"contract_id": "x"}).status_code == 422

    def test_api_dispute_blocks_billing_preview_and_approval(self, api, harness):
        run_id = self._prepared(harness)
        api.post("/api/v1/commercial-contracts", headers=_api_headers(API_ALPHA_KEY),
                 json=_contract_body())
        sid = api.post("/api/v1/settlements/calculate", headers=_api_headers(API_ALPHA_KEY),
                       json={"run_id": run_id, "contract_id": "ctr_api"}).json()["settlement_id"]

        # Approval requires BILLING_MANAGE; the auditor does not have it.
        assert api.post(f"/api/v1/settlements/{sid}/approvals",
                        headers=_api_headers(API_AUDITOR_KEY),
                        json={"reason": "lgtm"}).status_code == 403
        approved = api.post(f"/api/v1/settlements/{sid}/approvals",
                            headers=_api_headers(API_ALPHA_KEY),
                            json={"reason": "reviewed evidence"}).json()
        assert approved["approved"] is True

        opened = api.post(f"/api/v1/settlements/{sid}/disputes",
                          headers=_api_headers(API_ALPHA_KEY),
                          json={"disputing_party": "buyer-x", "reason": "quantity dispute",
                                "evidence_references": ["audit:delivered"]})
        assert opened.status_code == 201
        assert opened.json()["status"] == "OPEN"

        blocked = api.post(f"/api/v1/settlements/{sid}/billing-preview",
                           headers={**_api_headers(API_ALPHA_KEY),
                                    "Idempotency-Key": "api-bill-1"})
        assert blocked.status_code == 409
        assert api.post(f"/api/v1/settlements/{sid}/approvals",
                        headers=_api_headers(API_ALPHA_KEY),
                        json={"reason": "again"}).status_code == 409

    def test_api_billing_preview_requires_key_and_dedups(self, api, harness):
        run_id = self._prepared(harness)
        api.post("/api/v1/commercial-contracts", headers=_api_headers(API_ALPHA_KEY),
                 json=_contract_body())
        sid = api.post("/api/v1/settlements/calculate", headers=_api_headers(API_ALPHA_KEY),
                       json={"run_id": run_id, "contract_id": "ctr_api"}).json()["settlement_id"]

        # The state-changing external call requires an idempotency key.
        assert api.post(f"/api/v1/settlements/{sid}/billing-preview",
                        headers=_api_headers(API_ALPHA_KEY)).status_code == 400

        headers = {**_api_headers(API_ALPHA_KEY), "Idempotency-Key": "api-bill-2"}
        first = api.post(f"/api/v1/settlements/{sid}/billing-preview", headers=headers).json()
        assert first["ok"] is True
        assert first["status"] == "PROVIDER_INVOICE_CREATED"
        second = api.post(f"/api/v1/settlements/{sid}/billing-preview", headers=headers).json()
        assert second["request_meta"].get("deduplicated") is True
        assert second["billing_instruction_id"] == first["billing_instruction_id"]
        assert len(harness.ledger.list_billing_instructions(ALPHA)) == 1

    def test_api_billing_preview_refuses_non_payable(self, api, harness):
        run_id, _, _ = _run_delivery(harness, _records(delivered=2, held=0, refused=0))
        api.post("/api/v1/commercial-contracts", headers=_api_headers(API_ALPHA_KEY),
                 json=_contract_body())
        sid = api.post("/api/v1/settlements/calculate", headers=_api_headers(API_ALPHA_KEY),
                       json={"run_id": run_id, "contract_id": "ctr_api"}).json()["settlement_id"]
        resp = api.post(f"/api/v1/settlements/{sid}/billing-preview",
                        headers={**_api_headers(API_ALPHA_KEY), "Idempotency-Key": "k"})
        assert resp.status_code == 409
