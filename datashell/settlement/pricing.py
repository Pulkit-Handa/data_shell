"""Deterministic pricing calculators.

Every calculator is a pure function of (contract pricing spec, qualified units,
expected units, delivered units). No clocks, no randomness, no network, no
mutable global state: the same inputs always produce the same output, which is
what makes a settlement reproducible from its stored inputs.

All arithmetic is exact: integer minor units and ``fractions.Fraction`` for
rationals. The single rounding stage is ROUND_HALF_UP applied to the final
gross amount (see :func:`pricing.round_half_up`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional, Tuple

from datashell.settlement.models import (
    PricingModel,
    PricingSpec,
    TierBand,
)
from datashell.settlement.money import MAX_GROSS_AMOUNT_MINOR, MoneyValidationError, round_half_up


@dataclass
class PricingResult:
    gross_amount_minor: int
    tiers_applied: List[dict] = field(default_factory=list)
    proration_ratio: Optional[str] = None
    notes: List[str] = field(default_factory=list)


def _cap(gross: int, pricing: PricingSpec) -> int:
    """Apply the contract's maximum payable amount, if configured."""
    if pricing.max_payable_minor is not None and gross > pricing.max_payable_minor:
        return pricing.max_payable_minor
    return gross


def price_per_accepted_unit(pricing: PricingSpec, qualified_units: int) -> PricingResult:
    """``gross = qualified_units x unit_price`` in integer minor units."""
    if pricing.unit_price_minor is None:
        raise MoneyValidationError("PER_ACCEPTED_UNIT requires unit_price_minor")
    if qualified_units < 0:
        raise MoneyValidationError("qualified_units cannot be negative")
    gross = qualified_units * pricing.unit_price_minor
    if gross > MAX_GROSS_AMOUNT_MINOR:
        raise MoneyValidationError("gross amount exceeds the maximum supported monetary value")
    return PricingResult(gross_amount_minor=_cap(gross, pricing))


def price_fixed_batch_fee(pricing: PricingSpec, qualified_units: int) -> PricingResult:
    """A predefined amount once batch acceptance conditions are met.

    The caller (acceptance evaluator) is responsible for refusing to produce a
    payable result when acceptance conditions fail; the calculator only
    performs the agreed arithmetic.
    """
    if pricing.fixed_fee_minor is None:
        raise MoneyValidationError("FIXED_BATCH_FEE requires fixed_fee_minor")
    return PricingResult(gross_amount_minor=_cap(pricing.fixed_fee_minor, pricing))


def price_tiered(pricing: PricingSpec, qualified_units: int) -> PricingResult:
    """Apply configured price bands with deterministic boundaries.

    Bands are contiguous and half-open [min, max). Each unit is priced at the
    rate of the band it falls in, so the result is the sum over bands of
    ``band_units x band_rate`` — computed in exact integer minor units.
    """
    if not pricing.tiers:
        raise MoneyValidationError("TIERED_PRICING requires tier bands")
    if qualified_units < 0:
        raise MoneyValidationError("qualified_units cannot be negative")
    tiers: List[TierBand] = sorted(pricing.tiers, key=lambda t: t.min_units)
    if tiers[0].min_units != 0:
        raise MoneyValidationError("tiered pricing must start at min_units=0")

    gross = 0
    applied: List[dict] = []
    remaining = qualified_units
    for band in tiers:
        if remaining <= 0:
            break
        band_width = (
            (band.max_units - band.min_units)
            if band.max_units is not None
            else max(0, qualified_units - band.min_units)
        )
        units_in_band = min(remaining, band_width)
        if units_in_band > 0:
            band_amount = units_in_band * band.unit_price_minor
            gross += band_amount
            applied.append({
                "band_min": band.min_units,
                "band_max": band.max_units,
                "units": units_in_band,
                "unit_price_minor": band.unit_price_minor,
                "amount_minor": band_amount,
            })
            remaining -= units_in_band
    if remaining > 0:
        # Qualified units beyond the last band use the final band's rate so the
        # calculation remains total (defined, not silently truncated).
        last = tiers[-1]
        if last.max_units is not None:
            extra = remaining * last.unit_price_minor
            gross += extra
            applied.append({
                "band_min": last.max_units,
                "band_max": None,
                "units": remaining,
                "unit_price_minor": last.unit_price_minor,
                "amount_minor": extra,
            })
    if gross > MAX_GROSS_AMOUNT_MINOR:
        raise MoneyValidationError("gross amount exceeds the maximum supported monetary value")
    return PricingResult(gross_amount_minor=_cap(gross, pricing), tiers_applied=applied)


def price_prorated_short_delivery(
    pricing: PricingSpec,
    qualified_units: int,
    expected_units: Optional[int],
    short_delivery: bool,
) -> PricingResult:
    """Apply the agreed contractual formula when a batch is incomplete.

    With ``short_delivery`` true, the payable amount is the full-delivery
    amount scaled by the contracted rational ratio
    ``proration_numerator / proration_denominator`` — an exact Fraction, rounded
    once at the end. Without a shortfall the plain per-unit price applies.
    """
    if pricing.unit_price_minor is None:
        raise MoneyValidationError("PRORATED_SHORT_DELIVERY requires unit_price_minor")
    base = price_per_accepted_unit(pricing, qualified_units)
    if not short_delivery or not expected_units:
        return base
    ratio = Fraction(pricing.proration_numerator, pricing.proration_denominator)
    exact = Fraction(base.gross_amount_minor) * ratio
    gross = round_half_up(exact)
    return PricingResult(
        gross_amount_minor=_cap(gross, pricing),
        proration_ratio=f"{pricing.proration_numerator}/{pricing.proration_denominator}",
        notes=[f"short delivery proration {pricing.proration_numerator}/{pricing.proration_denominator} applied to {base.gross_amount_minor} minor"],
    )


def price_hold_or_dispute() -> PricingResult:
    """Non-payable outcome when contractual acceptance conditions are not met.

    The gross amount is zero; the acceptance evaluator assigns the
    settlement status and reason codes. A zero-value result here is a
    *deliberate* non-payable outcome, never a silent success.
    """
    return PricingResult(gross_amount_minor=0, notes=["acceptance conditions not met; no payable amount"])


def calculate(
    pricing: PricingSpec,
    qualified_units: int,
    expected_units: Optional[int] = None,
    short_delivery: bool = False,
    hold: bool = False,
) -> PricingResult:
    """Dispatch to the configured pricing model."""
    if hold:
        return price_hold_or_dispute()
    if qualified_units < 0:
        raise MoneyValidationError("qualified_units cannot be negative")
    model = pricing.model
    if model == PricingModel.PER_ACCEPTED_UNIT:
        return price_per_accepted_unit(pricing, qualified_units)
    if model == PricingModel.FIXED_BATCH_FEE:
        return price_fixed_batch_fee(pricing, qualified_units)
    if model == PricingModel.TIERED_PRICING:
        return price_tiered(pricing, qualified_units)
    if model == PricingModel.PRORATED_SHORT_DELIVERY:
        return price_prorated_short_delivery(pricing, qualified_units, expected_units, short_delivery)
    if model == PricingModel.HOLD_OR_DISPUTE:
        return price_hold_or_dispute()
    raise MoneyValidationError(f"unsupported pricing model: {model}")
