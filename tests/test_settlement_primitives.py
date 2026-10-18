"""Unit tests for settlement primitives: money, contracts, pricing, acceptance."""

from __future__ import annotations

import json
from fractions import Fraction

import pytest

from datashell.settlement.models import (
    AcceptancePolicy,
    AcceptanceStatus,
    CommercialContract,
    DisputeResolution,
    DisputeStatus,
    PenaltyModel,
    PricingModel,
    QualificationUnit,
    ReasonCode,
    SettlementStatus,
    TierBand,
)
from datashell.settlement.money import (
    MoneyValidationError,
    decimal_to_minor_units,
    format_minor_units,
    parse_unit_price_minor,
    round_half_up,
)
from datashell.settlement.pricing import (
    price_per_accepted_unit,
    price_tiered,
)
from datashell.settlement.acceptance import (
    RunEvidence,
    evaluate_acceptance,
)


def _contract(**overrides) -> CommercialContract:
    base = dict(
        contract_id="ctr_test",
        buyer_id="buyer_a",
        seller_id="seller_b",
        feed_id="feed_x",
        currency="USD",
        pricing={"model": "PER_ACCEPTED_UNIT", "unit_price_minor": 5},
        acceptance={"expected_units": 100, "minimum_accepted_units": 50},
        effective_at="2026-01-01T00:00:00Z",
    )
    base.update(overrides)
    return CommercialContract(**base)


class TestMoney:
    def test_parse_string_decimal(self):
        assert parse_unit_price_minor("0.05", "USD") == 5

    def test_parse_integer_minor_units(self):
        assert parse_unit_price_minor(5, "USD") == 5

    def test_float_rejected(self):
        with pytest.raises(MoneyValidationError):
            parse_unit_price_minor(0.05, "USD")

    def test_negative_rejected(self):
        with pytest.raises(MoneyValidationError):
            parse_unit_price_minor("-1", "USD")

    def test_non_exact_decimal_rejected(self):
        # 0.001 USD cannot be represented in cents
        with pytest.raises(MoneyValidationError):
            parse_unit_price_minor("0.001", "USD")

    def test_jpy_zero_precision(self):
        assert parse_unit_price_minor("5", "JPY") == 5
        with pytest.raises(MoneyValidationError):
            parse_unit_price_minor("0.5", "JPY")

    def test_format(self):
        assert format_minor_units(4925000, "USD") == "49250.00"
        assert format_minor_units(5, "JPY") == "5"
        assert format_minor_units(0, "USD") == "0.00"

    def test_round_half_up(self):
        assert round_half_up(Fraction(1, 2)) == 1
        assert round_half_up(Fraction(3, 2)) == 2
        assert round_half_up(Fraction(5, 2)) == 3
        assert round_half_up(Fraction(-1, 2)) == -1  # halves away from zero
        assert round_half_up(Fraction(4, 3)) == 1

    def test_decimal_to_minor_units_exact(self):
        from decimal import Decimal
        assert decimal_to_minor_units(Decimal("49.25"), "USD") == 4925
        assert decimal_to_minor_units(Decimal("49250.00"), "USD") == 4925000
        assert decimal_to_minor_units(Decimal("5"), "JPY") == 5

    def test_decimal_to_minor_units_rejects_extra_precision(self):
        from decimal import Decimal
        from datashell.settlement.money import MoneyValidationError
        with pytest.raises(MoneyValidationError):
            decimal_to_minor_units(Decimal("49.251"), "USD")
        with pytest.raises(MoneyValidationError):
            decimal_to_minor_units(Decimal("0.5"), "JPY")


class TestContractValidation:
    def test_valid_contract_digest_stable(self):
        c1 = _contract()
        c2 = _contract()
        assert c1.contract_digest == c2.contract_digest

    def test_digest_differs_when_terms_change(self):
        c1 = _contract()
        c2 = _contract(pricing={"model": "PER_ACCEPTED_UNIT", "unit_price_minor": 6})
        assert c1.contract_digest != c2.contract_digest

    def test_invalid_currency_rejected(self):
        with pytest.raises(Exception):
            _contract(currency="US")

    def test_invalid_identifier_rejected(self):
        with pytest.raises(Exception):
            _contract(contract_id="bad id with spaces")

    def test_unique_entities_requires_key_columns(self):
        with pytest.raises(Exception):
            CommercialContract(
                contract_id="c", buyer_id="b", seller_id="s", feed_id="f",
                qualification={"billable_unit": "UNIQUE_ENTITIES"},
                effective_at="2026-01-01T00:00:00Z")

    def test_maximum_below_expected_rejected(self):
        with pytest.raises(Exception):
            _contract(acceptance={"expected_units": 1000, "maximum_units": 100})

    def test_tiered_requires_start_at_zero(self):
        with pytest.raises(Exception):
            CommercialContract(
                contract_id="c", buyer_id="b", seller_id="s", feed_id="f",
                pricing={"model": "TIERED_PRICING",
                         "tiers": [{"min_units": 10, "max_units": 100, "unit_price_minor": 5}]},
                effective_at="2026-01-01T00:00:00Z")

    def test_tiered_non_contiguous_rejected(self):
        with pytest.raises(Exception):
            CommercialContract(
                contract_id="c", buyer_id="b", seller_id="s", feed_id="f",
                pricing={"model": "TIERED_PRICING", "tiers": [
                    {"min_units": 0, "max_units": 100, "unit_price_minor": 5},
                    {"min_units": 200, "max_units": None, "unit_price_minor": 3},
                ]},
                effective_at="2026-01-01T00:00:00Z")

    def test_per_unit_requires_price(self):
        with pytest.raises(Exception):
            CommercialContract(
                contract_id="c", buyer_id="b", seller_id="s", feed_id="f",
                pricing={"model": "PER_ACCEPTED_UNIT"},
                effective_at="2026-01-01T00:00:00Z")

    def test_fixed_fee_requires_amount(self):
        with pytest.raises(Exception):
            CommercialContract(
                contract_id="c", buyer_id="b", seller_id="s", feed_id="f",
                pricing={"model": "FIXED_BATCH_FEE"},
                effective_at="2026-01-01T00:00:00Z")

    def test_zero_value_contract_allowed(self):
        c = _contract(pricing={"model": "FIXED_BATCH_FEE", "fixed_fee_minor": 0})
        assert c.contract_digest


class TestPricing:
    def test_per_accepted_unit_exact(self):
        c = _contract()
        result = price_per_accepted_unit(c.pricing, 985000)
        assert result.gross_amount_minor == 4925000  # 985000 * 5

    def test_zero_units_gives_zero(self):
        c = _contract()
        result = price_per_accepted_unit(c.pricing, 0)
        assert result.gross_amount_minor == 0

    def test_negative_units_rejected(self):
        c = _contract()
        with pytest.raises(MoneyValidationError):
            price_per_accepted_unit(c.pricing, -1)

    def test_tiered_bands(self):
        c = CommercialContract(
            contract_id="c", buyer_id="b", seller_id="s", feed_id="f",
            pricing={"model": "TIERED_PRICING", "tiers": [
                {"min_units": 0, "max_units": 1000, "unit_price_minor": 5},
                {"min_units": 1000, "max_units": 100000, "unit_price_minor": 3},
                {"min_units": 100000, "max_units": None, "unit_price_minor": 2},
            ]},
            effective_at="2026-01-01T00:00:00Z")
        result = price_tiered(c.pricing, 200000)
        # 1000*5 + 99000*3 + 100000*2 = 5000 + 297000 + 200000 = 502000
        assert result.gross_amount_minor == 502000
        assert len(result.tiers_applied) == 3

    def test_tier_boundary_deterministic(self):
        c = CommercialContract(
            contract_id="c", buyer_id="b", seller_id="s", feed_id="f",
            pricing={"model": "TIERED_PRICING", "tiers": [
                {"min_units": 0, "max_units": 10, "unit_price_minor": 5},
                {"min_units": 10, "max_units": None, "unit_price_minor": 3},
            ]},
            effective_at="2026-01-01T00:00:00Z")
        # Exactly at the boundary: 10 units all in band 1
        result = price_tiered(c.pricing, 10)
        assert result.gross_amount_minor == 50
        assert result.tiers_applied[0]["units"] == 10

    def test_prorated_short_delivery(self):
        from datashell.settlement.pricing import price_prorated_short_delivery
        c = CommercialContract(
            contract_id="c", buyer_id="b", seller_id="s", feed_id="f",
            pricing={"model": "PRORATED_SHORT_DELIVERY", "unit_price_minor": 5,
                     "proration_numerator": 1, "proration_denominator": 2},
            effective_at="2026-01-01T00:00:00Z")
        result = price_prorated_short_delivery(c.pricing, 500, 1000, short_delivery=True)
        # base 500*5 = 2500, * 1/2 = 1250
        assert result.gross_amount_minor == 1250
        assert result.proration_ratio == "1/2"

    def test_prorated_no_shortfall_plain_price(self):
        from datashell.settlement.pricing import price_prorated_short_delivery
        c = CommercialContract(
            contract_id="c", buyer_id="b", seller_id="s", feed_id="f",
            pricing={"model": "PRORATED_SHORT_DELIVERY", "unit_price_minor": 5},
            effective_at="2026-01-01T00:00:00Z")
        result = price_prorated_short_delivery(c.pricing, 500, 1000, short_delivery=False)
        assert result.gross_amount_minor == 2500
        assert result.proration_ratio is None

    def test_hold_or_dispute_is_zero(self):
        from datashell.settlement.pricing import price_hold_or_dispute
        result = price_hold_or_dispute()
        assert result.gross_amount_minor == 0

    def test_max_payable_cap(self):
        c = CommercialContract(
            contract_id="c", buyer_id="b", seller_id="s", feed_id="f",
            pricing={"model": "PER_ACCEPTED_UNIT", "unit_price_minor": 5,
                     "max_payable_minor": 100},
            effective_at="2026-01-01T00:00:00Z")
        result = price_per_accepted_unit(c.pricing, 1000)
        assert result.gross_amount_minor == 100


class TestAcceptance:
    def _evidence(self, **kw) -> RunEvidence:
        base = dict(run_id="r1", status="LOADED", rows_read=1000, rows_delivered=985,
                    rows_refused=10, rows_held=5, footing_matched=True)
        base.update(kw)
        return RunEvidence(**base)

    def test_clean_delivery_accepted(self):
        c = _contract()
        verdict = evaluate_acceptance(c, self._evidence())
        assert verdict.status == AcceptanceStatus.ACCEPTED
        assert verdict.billing_eligible

    def test_reconciliation_mismatch_rejected_not_payable(self):
        c = _contract()
        verdict = evaluate_acceptance(c, self._evidence(footing_matched=False))
        assert verdict.status == AcceptanceStatus.REJECTED
        assert not verdict.billing_eligible
        assert ReasonCode.RECONCILIATION_MISMATCH in verdict.reason_codes

    def test_invalid_certificate_rejected(self):
        c = _contract()
        verdict = evaluate_acceptance(c, self._evidence(certificate_valid=False))
        assert verdict.status == AcceptanceStatus.REJECTED
        assert ReasonCode.CERTIFICATE_VERIFICATION_FAILED in verdict.reason_codes

    def test_batch_rejected_status_rejected(self):
        c = _contract()
        verdict = evaluate_acceptance(c, self._evidence(status="REJECTED", is_run_rejected=True))
        assert verdict.status == AcceptanceStatus.REJECTED
        assert ReasonCode.BATCH_REJECTED in verdict.reason_codes

    def test_below_minimum_rejected(self):
        c = _contract(acceptance={"expected_units": 1000, "minimum_accepted_units": 900})
        verdict = evaluate_acceptance(c, self._evidence(rows_delivered=899))
        assert verdict.status == AcceptanceStatus.REJECTED
        assert ReasonCode.MINIMUM_QUANTITY_NOT_MET in verdict.reason_codes

    def test_strict_quantity_shortfall_pending_review(self):
        c = _contract(acceptance={"policy": "STRICT_QUANTITY", "expected_units": 1000})
        verdict = evaluate_acceptance(c, self._evidence())
        assert verdict.status == AcceptanceStatus.PENDING_REVIEW
        assert not verdict.billing_eligible

    def test_partial_with_minimum_shortfall_partially_accepted(self):
        c = _contract(acceptance={"policy": "PARTIAL_WITH_MINIMUM",
                                  "expected_units": 1000, "minimum_accepted_units": 50})
        verdict = evaluate_acceptance(c, self._evidence())
        assert verdict.status == AcceptanceStatus.PARTIALLY_ACCEPTED
        assert verdict.billing_eligible

    def test_open_dispute_marks_disputed(self):
        c = _contract()
        verdict = evaluate_acceptance(c, self._evidence(), dispute_open=True)
        assert verdict.status == AcceptanceStatus.DISPUTED
        assert not verdict.billing_eligible
        assert ReasonCode.DISPUTE_OPEN in verdict.reason_codes

    def test_tolerance_exceeded_flags_review_but_payable(self):
        c = _contract(acceptance={"allowed_failure_tolerance_bps": 1})  # 0.01%
        verdict = evaluate_acceptance(c, self._evidence())
        assert verdict.status == AcceptanceStatus.PARTIALLY_ACCEPTED
        assert ReasonCode.MANUAL_REVIEW_REQUIRED in verdict.reason_codes

    def test_missing_artifacts_rejected(self):
        c = _contract()
        verdict = evaluate_acceptance(c, self._evidence(required_artifacts_present=False))
        assert verdict.status == AcceptanceStatus.REJECTED
        assert ReasonCode.REQUIRED_ARTIFACT_MISSING in verdict.reason_codes

    def test_batch_rule_failure_rejected(self):
        c = _contract()
        verdict = evaluate_acceptance(c, self._evidence(batch_rules_passed=False,
                                                       batch_failures=["not-partial"]))
        assert verdict.status == AcceptanceStatus.REJECTED
        assert ReasonCode.CONTRACT_ACCEPTANCE_FAILED in verdict.reason_codes

    def test_reproducibility_same_inputs_same_verdict(self):
        c = _contract()
        ev = self._evidence()
        assert evaluate_acceptance(c, ev) == evaluate_acceptance(c, ev)
