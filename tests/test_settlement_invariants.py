"""Property and invariant tests for the settlement engine (Section 14C).

Deterministic seeded-random property tests plus direct invariant checks.
Deliberately dependency-free (no hypothesis): every random run uses a fixed
seed so failures are reproducible.

The invariants exercised here are the ones the product must never break:

1.  A settlement binds exactly one immutable contract version.
2.  The same stored inputs always reproduce the same financial result.
3.  Idempotent replays never create a second financial record.
4.  No duplicate financial actions are possible (unique constraints backstop).
5.  Every lifecycle transition is append-only and auditable.
6.  Tenants are isolated on every read and write.
7.  Failed reconciliation is never payable.
8.  An invalid certificate is never payable.
9.  Money is exact integer minor-unit arithmetic (no binary floats).
10. Rounding happens exactly once, half-up, at the final stage.
11. Overflow fails closed instead of wrapping or approximating.
12. Supersession preserves the original financial record.
"""

from __future__ import annotations

import json
import random
import sqlite3
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

import pytest

from datashell.settlement.engine import (
    SettlementEngineError,
    calculate_settlement,
)
from datashell.settlement.ledger import SettlementLedger
from datashell.settlement.models import (
    CommercialContract,
    SettlementStatus,
)
from datashell.settlement.money import (
    MAX_GROSS_AMOUNT_MINOR,
    MoneyValidationError,
    round_half_up,
)
from datashell.settlement.pricing import calculate as calculate_price
from datashell.settlement.pricing import price_tiered


# ------------------------------------------------------------------- builders


def _contract(*, unit_price_minor: int = 5, pricing: dict | None = None,
              currency: str = "USD", expected_units: int = 5,
              minimum_accepted_units: int = 1,
              policy: str = "PARTIAL_WITH_MINIMUM") -> CommercialContract:
    return CommercialContract(
        contract_id="ctr_prop", contract_version=1, buyer_id="b", seller_id="s",
        feed_id="feed", currency=currency,
        pricing=pricing or {"model": "PER_ACCEPTED_UNIT",
                            "unit_price_minor": unit_price_minor},
        acceptance={"policy": policy, "minimum_accepted_units": minimum_accepted_units,
                    "expected_units": expected_units},
        effective_at="2026-01-01T00:00:00Z",
    )


def _run(*, run_id: str = "run_prop", delivered: int = 5, read: int | None = None,
         held: int = 0, refused: int = 0, footing: bool = True,
         unaccounted: int = 0, rejected: bool = False, status: str = "LOADED") -> dict:
    return {
        "run_id": run_id, "status": status,
        "rows_read": read if read is not None else delivered + held + refused,
        "rows_delivered": delivered, "rows_refused": refused, "rows_held": held,
        "unaccounted_for": unaccounted, "footing_matched": footing,
        "is_run_rejected": rejected, "audit_path": "audit.json",
    }


def _statement(*, contract: CommercialContract | None = None, run: dict | None = None,
               tenant: str = "t1", idempotency_key: str | None = None,
               certificate_valid: bool = True):
    contract = contract or _contract()
    run = run or _run(delivered=5)
    return calculate_settlement(
        tenant_id=tenant, contract=contract, ledger_run=run,
        certificate_digest="0" * 64, certificate_valid=certificate_valid,
        actor="test", idempotency_key=idempotency_key)


def _decode_minor_units_via_decimals(amount_minor: int, num: int, den: int) -> int:
    """Independent rounding oracle: exact Decimal arithmetic, ROUND_HALF_UP."""
    scaled = Decimal(amount_minor) * Decimal(num) / Decimal(den)
    return int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ------------------------------------------------------------ money exactness


class TestExactMoneyInvariants:
    def test_per_unit_gross_matches_independent_oracle(self):
        rng = random.Random(20261018)
        for _ in range(300):
            units = rng.randint(0, 250_000)
            price = rng.randint(0, 50_000)
            cap = rng.choice([None, rng.randint(0, 200_000)])
            pricing = {"model": "PER_ACCEPTED_UNIT", "unit_price_minor": price,
                       "max_payable_minor": cap}
            result = calculate_price(
                _contract(pricing=pricing).pricing, units)
            expected = units * price
            if cap is not None:
                expected = min(expected, cap)
            assert result.gross_amount_minor == expected

    def test_tiered_gross_matches_independent_oracle(self):
        rng = random.Random(142857)
        for _ in range(200):
            b1 = rng.randint(1, 500)
            b2 = b1 + rng.randint(1, 500)
            p1, p2, p3 = (rng.randint(0, 1000) for _ in range(3))
            unbounded = rng.random() < 0.5
            bands = [{"min_units": 0, "max_units": b1, "unit_price_minor": p1},
                     {"min_units": b1, "max_units": b2, "unit_price_minor": p2},
                     {"min_units": b2, "max_units": None if unbounded else b2 + 300,
                      "unit_price_minor": p3}]
            units = rng.randint(0, b2 + 250)
            contract = _contract(pricing={"model": "TIERED_PRICING", "tiers": bands})
            result = calculate_price(contract.pricing, units)

            expected = 0
            for band in bands:
                lo, hi, price = band["min_units"], band["max_units"], band["unit_price_minor"]
                count = (min(units, hi if hi is not None else units) - lo
                         if units > lo else 0)
                if band is bands[-1] and hi is None:
                    count = max(0, units - lo)
                expected += max(0, count) * price
            if bands[-1]["max_units"] is not None and units > bands[-1]["max_units"]:
                # Units beyond the last bounded band use the final band rate.
                expected += (units - bands[-1]["max_units"]) * bands[-1]["unit_price_minor"]
                expected -= 0
            assert result.gross_amount_minor == expected, (units, bands, result)
            assert sum(t["amount_minor"] for t in result.tiers_applied) == expected

    def test_proration_rounds_half_up_exactly_once(self):
        rng = random.Random(777)
        for _ in range(200):
            units = rng.randint(0, 100_000)
            price = rng.randint(0, 10_000)
            num = rng.randint(0, 9)
            den = rng.randint(1, 9)
            contract = _contract(
                unit_price_minor=price, expected_units=units + rng.randint(1, 100),
                pricing={"model": "PRORATED_SHORT_DELIVERY",
                         "unit_price_minor": price,
                         "proration_numerator": num,
                         "proration_denominator": den})
            short = units < contract.acceptance.expected_units
            result = calculate_price(
                contract.pricing, units,
                expected_units=contract.acceptance.expected_units,
                short_delivery=short)
            base = units * price
            expected = (_decode_minor_units_via_decimals(base, num, den)
                        if short and num != den else base)
            assert result.gross_amount_minor == expected, (units, price, num, den)

    def test_overflow_fails_closed(self):
        price = 10**12
        units = 10**6
        assert units * price > MAX_GROSS_AMOUNT_MINOR
        with pytest.raises(MoneyValidationError):
            calculate_settlement(
                tenant_id="t1", contract=_contract(unit_price_minor=price),
                ledger_run=_run(delivered=units), certificate_digest="0" * 64,
                certificate_valid=True, actor="test")

    def test_calculations_never_contain_binary_floats(self):
        contract = _contract(pricing={
            "model": "PRORATED_SHORT_DELIVERY", "unit_price_minor": 7,
            "proration_numerator": 1, "proration_denominator": 3})
        statement = _statement(run=_run(delivered=10), contract=contract)
        payload = statement.model_dump(mode="json")

        def assert_no_floats(value):
            if isinstance(value, dict):
                for item in value.values():
                    assert_no_floats(item)
            elif isinstance(value, list):
                for item in value:
                    assert_no_floats(item)
            else:
                assert not isinstance(value, float), f"float leaked into {value!r}"

        assert_no_floats(payload)
        json.dumps(payload)  # strictly JSON-serializable without lossy coercion

    def test_round_half_up_is_away_from_zero_on_halves(self):
        assert round_half_up(Fraction(1, 2)) == 1
        assert round_half_up(Fraction(-1, 2)) == -1
        assert round_half_up(Fraction(5, 2)) == 3
        assert _decode_minor_units_via_decimals(5, 1, 2) == 3  # decimal oracle agrees


# ------------------------------------------------------------ determinism


class TestReproducibilityInvariants:
    def test_same_inputs_produce_identical_statement_core(self):
        contract = _contract(unit_price_minor=13)
        run = _run(delivered=97, run_id="run_same")
        first = _statement(contract=contract, run=run)
        second = _statement(contract=contract, run=run)
        assert first.model_dump(exclude={"created_at"}) == \
            second.model_dump(exclude={"created_at"})

    def test_settlement_id_is_deterministic(self):
        first = _statement(run=_run(delivered=3, run_id="run_idem"))
        second = _statement(run=_run(delivered=3, run_id="run_idem"))
        assert first.settlement_id == second.settlement_id == \
            "stl_run_idem_ctr_prop_v1"

    def test_stored_inputs_reproduce_the_stored_amount(self, tmp_path):
        contract = _contract(unit_price_minor=11, expected_units=50)
        run = _run(delivered=37, run_id="run_repro", held=2, refused=1)
        statement = _statement(contract=contract, run=run)
        ledger = SettlementLedger(tmp_path / "settle.db")
        ledger.save_settlement(statement)

        row = ledger.get_settlement(statement.settlement_id, "t1")
        recomputed = calculate_settlement(
            tenant_id="t1", contract=contract, ledger_run=run,
            certificate_digest=row["certificate_digest"],
            certificate_valid=True, actor="auditor")
        assert recomputed.calculation.model_dump() == row["calculation"]
        assert recomputed.contract_digest == row["contract_digest"]
        assert recomputed.settlement_id == row["settlement_id"]

    def test_contract_digest_binds_terms_not_registration_time(self):
        first = _contract(unit_price_minor=5)
        second = CommercialContract(**{
            **first.model_dump(mode="json"), "created_at": "2030-01-01T00:00:00Z"})
        assert first.contract_digest == second.contract_digest
        changed = _contract(unit_price_minor=6)
        assert changed.contract_digest != first.contract_digest


# ------------------------------------------------------------- fail closed


class TestFailClosedInvariants:
    def test_invalid_certificate_never_calculates(self):
        with pytest.raises(SettlementEngineError, match="certificate verification failed"):
            _statement(certificate_valid=False)

    def test_invalid_certificate_never_persists(self, tmp_path):
        ledger = SettlementLedger(tmp_path / "settle.db")
        with pytest.raises(SettlementEngineError):
            _statement(certificate_valid=False)
        assert ledger.list_settlements("t1") == []

    def test_footing_mismatch_is_never_payable(self):
        rng = random.Random(31415)
        for _ in range(50):
            run = _run(delivered=rng.randint(0, 10_000), held=rng.randint(0, 100),
                       refused=rng.randint(0, 100), footing=False,
                       unaccounted=rng.randint(1, 10))
            statement = _statement(run=run, contract=_contract(expected_units=0,
                                                               minimum_accepted_units=0))
            assert statement.calculation.gross_amount_minor == 0
            assert statement.acceptance_status.value == "REJECTED"
            assert statement.settlement_status != SettlementStatus.READY_FOR_BILLING
            assert "RECONCILIATION_MISMATCH" in [r.value for r in statement.reason_codes]

    def test_rejected_runs_are_never_payable(self):
        run = _run(delivered=100, rejected=True, status="REJECTED")
        statement = _statement(run=run, contract=_contract(expected_units=0,
                                                           minimum_accepted_units=0))
        assert statement.calculation.gross_amount_minor == 0
        assert statement.settlement_status != SettlementStatus.READY_FOR_BILLING

    def test_hold_pricing_always_yields_zero(self):
        contract = _contract(pricing={"model": "HOLD_OR_DISPUTE"})
        statement = _statement(contract=contract, run=_run(delivered=1000))
        assert statement.calculation.gross_amount_minor == 0
        assert statement.settlement_status == SettlementStatus.PENDING_REVIEW
        assert "MANUAL_REVIEW_REQUIRED" in [r.value for r in statement.reason_codes]

    def test_qualified_units_never_include_held_or_refused_rows(self):
        rng = random.Random(2718)
        for _ in range(50):
            delivered = rng.randint(0, 1000)
            run = _run(delivered=delivered, held=rng.randint(0, 500),
                       refused=rng.randint(0, 500))
            statement = _statement(
                run=run,
                contract=_contract(unit_price_minor=1, expected_units=0,
                                   minimum_accepted_units=0))
            if statement.acceptance_status.value == "REJECTED":
                continue
            assert statement.calculation.qualified_units == delivered


# --------------------------------------------------------- ledger invariants


class TestLedgerInvariants:
    def test_idempotent_save_never_creates_a_second_row(self, tmp_path):
        ledger = SettlementLedger(tmp_path / "settle.db")
        statement = _statement(idempotency_key="k-1")
        assert ledger.save_settlement(statement) is True
        assert ledger.save_settlement(statement) is False
        assert len(ledger.list_settlements("t1")) == 1

    def test_duplicate_idempotency_key_for_another_statement_is_rejected(self, tmp_path):
        ledger = SettlementLedger(tmp_path / "settle.db")
        first = _statement(idempotency_key="shared-key")
        ledger.save_settlement(first)
        other = _statement(run=_run(run_id="run_other", delivered=3),
                           idempotency_key="shared-key")
        assert other.settlement_id != first.settlement_id
        with pytest.raises(sqlite3.IntegrityError):
            ledger.save_settlement(other)
        assert len(ledger.list_settlements("t1")) == 1

    def test_transitions_are_append_only_and_leave_money_untouched(self, tmp_path):
        ledger = SettlementLedger(tmp_path / "settle.db")
        statement = _statement()
        ledger.save_settlement(statement)
        before = ledger.get_settlement(statement.settlement_id, "t1")
        financial_fields = ("calculation", "gross_amount_minor", "qualified_units",
                            "contract_digest", "certificate_digest", "created_at",
                            "actor", "idempotency_key")

        ledger.transition_status(statement.settlement_id, "t1",
                                 SettlementStatus.DISPUTED, "buyer", "dispute opened")
        ledger.transition_status(statement.settlement_id, "t1",
                                 SettlementStatus.SUPERSEDED, "reviewer", "corrected")
        after = ledger.get_settlement(statement.settlement_id, "t1")

        for field in financial_fields:
            assert after[field] == before[field], field
        assert after["settlement_status"] == "SUPERSEDED"
        events = ledger.get_events(statement.settlement_id, "t1")
        assert [e["event_type"] for e in events] == \
            ["CREATED", "STATUS_TRANSITION", "STATUS_TRANSITION"]
        assert events[1]["from_status"] == "READY_FOR_BILLING"
        assert events[1]["to_status"] == "DISPUTED"
        assert events[2]["to_status"] == "SUPERSEDED"

    def test_every_read_is_tenant_scoped(self, tmp_path):
        ledger = SettlementLedger(tmp_path / "settle.db")
        statement = _statement(tenant="tenant-a", idempotency_key="k-a")
        ledger.save_settlement(statement)
        assert ledger.get_settlement(statement.settlement_id, "tenant-a") is not None
        assert ledger.get_settlement(statement.settlement_id, "tenant-b") is None
        assert ledger.list_settlements("tenant-b") == []
        assert ledger.find_by_idempotency_key("tenant-b", "k-a") is None
        assert ledger.get_events(statement.settlement_id, "tenant-b") == []
        assert ledger.transition_status(statement.settlement_id, "tenant-b",
                                        SettlementStatus.CANCELLED, "x", "cross") is False

    def test_supersession_records_the_replacement_link(self, tmp_path):
        ledger = SettlementLedger(tmp_path / "settle.db")
        statement = _statement()
        ledger.save_settlement(statement)
        ledger.transition_status(
            statement.settlement_id, "t1", SettlementStatus.SUPERSEDED, "reviewer",
            "superseded", event_type="SUPERSEDED",
            payload={"replacement_settlement_id": "stl_new"})
        events = ledger.get_events(statement.settlement_id, "t1")
        assert json.loads(events[-1]["payload_json"]) == {
            "replacement_settlement_id": "stl_new"}


# -------------------------------------------------------------- tier boundaries


class TestTierBoundaryInvariants:
    CONTRACT = _contract(pricing={
        "model": "TIERED_PRICING",
        "tiers": [
            {"min_units": 0, "max_units": 10, "unit_price_minor": 100},
            {"min_units": 10, "max_units": 20, "unit_price_minor": 50},
            {"min_units": 20, "max_units": None, "unit_price_minor": 10},
        ]})

    @pytest.mark.parametrize("units,expected", [
        (0, 0), (1, 100), (9, 900), (10, 1000),      # boundary stays in band 1
        (11, 1050), (20, 1500), (21, 1510), (100, 2300),
    ])
    def test_half_open_bands_exact(self, units, expected):
        result = price_tiered(self.CONTRACT.pricing, units)
        assert result.gross_amount_minor == expected

    def test_tiers_applied_sum_to_gross(self):
        for units in (0, 5, 10, 15, 20, 21, 1000):
            result = price_tiered(self.CONTRACT.pricing, units)
            assert sum(t["amount_minor"] for t in result.tiers_applied) == \
                result.gross_amount_minor
