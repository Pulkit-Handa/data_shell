"""Exact monetary arithmetic for settlement calculations.

Financial invariants enforced here:

* All monetary values are held as **integer minor units** (e.g. cents for USD).
  Binary floating-point is never used for money, so no representation error
  can enter a calculation.
* Currency precision is declared per ISO-4217-like convention (USD=2, JPY=0).
  A decimal unit price is accepted only when it converts *exactly* to a whole
  number of minor units at the declared precision; otherwise validation fails
  rather than silently rounding the agreed price.
* Rounding happens exactly once, at the final aggregation stage, using
  ROUND_HALF_UP on a rational number. Intermediate values stay exact (Fraction).
* Negative results are rejected by the pricing layer: a contract can define
  penalties as explicitly modeled positive charges, and a payable settlement
  never carries a negative gross amount.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

# Minor-unit exponent per currency code. Currencies not listed default to 2,
# which is correct for USD, EUR, GBP and most others. Extend when a contract
# names a currency with a different precision.
CURRENCY_PRECISION: dict[str, int] = {
    "USD": 2, "EUR": 2, "GBP": 2, "CAD": 2, "AUD": 2, "CHF": 2, "SEK": 2,
    "NOK": 2, "DKK": 2, "NZD": 2, "SGD": 2, "HKD": 2, "MXN": 2,
    "JPY": 0, "KRW": 0, "VND": 0, "ISK": 0, "CLP": 0, "XAF": 0, "XOF": 0,
    "BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3, "TND": 3, "IQD": 3, "LYD": 3,
    "DEFAULT": 2,
}

# Hard ceiling for a single settlement's gross amount. This is a safety rail
# against a malformed contract producing an astronomically large payable
# figure (for example a unit price accidentally denominated in major units).
# 10^15 minor units at precision 2 is one trillion major units.
MAX_GROSS_AMOUNT_MINOR = 10**15


class MoneyValidationError(ValueError):
    """Raised when a monetary value cannot be represented exactly."""


def currency_precision(code: str) -> int:
    """Return the minor-unit exponent for an ISO currency code."""
    if not isinstance(code, str) or not code.strip():
        raise MoneyValidationError("currency must be a non-empty string")
    key = code.strip().upper()
    return CURRENCY_PRECISION.get(key, CURRENCY_PRECISION["DEFAULT"])


def minor_exponent(code: str) -> int:
    """Number of decimal places the currency uses (0 for JPY, 2 for USD)."""
    return currency_precision(code)


def decimal_to_minor_units(amount: Decimal, currency: str) -> int:
    """Convert a Decimal major-unit amount to integer minor units exactly.

    Raises MoneyValidationError when the value has more fractional digits
    than the currency's precision allows — the caller must never silently
    round an *input* price.
    """
    exp = currency_precision(currency)
    try:
        scaled = amount.scaleb(exp)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise MoneyValidationError(f"amount {amount} is out of range for {currency}") from exc
    if scaled != scaled.to_integral_value():
        raise MoneyValidationError(
            f"amount {amount} cannot be represented exactly in {currency} "
            f"(precision {exp} minor-unit digits)"
        )
    minor = int(scaled)
    if abs(minor) > MAX_GROSS_AMOUNT_MINOR:
        raise MoneyValidationError(f"amount {amount} exceeds the maximum supported monetary value")
    return minor


def parse_unit_price_minor(value: object, currency: str) -> int:
    """Parse a contract unit price into integer minor units.

    Accepts int (interpreted directly as minor units), str/Decimal (major
    units, e.g. "0.05"), and float only when it is an exact integer number
    of minor units. Anything ambiguous fails closed.
    """
    if isinstance(value, bool):
        raise MoneyValidationError("unit price must not be a boolean")
    if isinstance(value, int):
        minor = value
    elif isinstance(value, Decimal):
        minor = decimal_to_minor_units(value, currency)
    elif isinstance(value, str):
        try:
            minor = decimal_to_minor_units(Decimal(value.strip()), currency)
        except InvalidOperation as exc:
            raise MoneyValidationError(f"unit price '{value}' is not a valid decimal") from exc
    elif isinstance(value, float):
        # Floats are rejected outright for prices: a caller passing 0.05 as a
        # float has already lost exactness before we see it.
        raise MoneyValidationError(
            "unit price must be supplied as a string, Decimal, or integer minor "
            "units — never a binary float")
    else:
        raise MoneyValidationError(f"unsupported unit price type: {type(value).__name__}")
    if minor < 0:
        raise MoneyValidationError("unit price cannot be negative")
    if abs(minor) > MAX_GROSS_AMOUNT_MINOR:
        raise MoneyValidationError("unit price exceeds the maximum supported monetary value")
    return minor


def format_minor_units(minor: int, currency: str) -> str:
    """Render integer minor units as a canonical decimal major-unit string."""
    exp = currency_precision(currency)
    sign = "-" if minor < 0 else ""
    magnitude = abs(minor)
    if exp == 0:
        return f"{sign}{magnitude}"
    whole, frac = divmod(magnitude, 10 ** exp)
    return f"{sign}{whole}.{frac:0{exp}d}"


def round_half_up(frac: Fraction) -> int:
    """Round an exact rational to the nearest integer, halves away from zero.

    This is the single rounding stage applied to final calculated amounts.
    """
    if frac < 0:
        return -round_half_up(-frac)
    floor = frac.numerator // frac.denominator
    remainder = frac - floor
    if remainder >= Fraction(1, 2):
        return floor + 1
    return floor


@dataclass(frozen=True)
class Money:
    """An exact amount bound to a currency. Arithmetic stays in minor units."""

    minor: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.minor, int):
            raise MoneyValidationError("minor units must be an integer")
        currency_precision(self.currency)  # validates the code
        if abs(self.minor) > MAX_GROSS_AMOUNT_MINOR:
            raise MoneyValidationError("amount exceeds the maximum supported monetary value")

    @classmethod
    def from_major(cls, amount: object, currency: str) -> Money:
        if isinstance(amount, int) and not isinstance(amount, bool):
            minor = decimal_to_minor_units(Decimal(amount), currency)
        elif isinstance(amount, str | Decimal):
            minor = decimal_to_minor_units(Decimal(str(amount)), currency)
        else:
            raise MoneyValidationError(f"cannot construct money from {type(amount).__name__}")
        return cls(minor=minor, currency=currency)

    @property
    def major_exact(self) -> Fraction:
        """Exact major-unit value as a rational (for explanation output)."""
        return Fraction(self.minor, 10 ** currency_precision(self.currency))

    def as_major_str(self) -> str:
        return format_minor_units(self.minor, self.currency)

    def __add__(self, other: Money) -> Money:
        if other.currency != self.currency:
            raise MoneyValidationError("cannot add amounts in different currencies")
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        if other.currency != self.currency:
            raise MoneyValidationError("cannot subtract amounts in different currencies")
        return Money(self.minor - other.minor, self.currency)
