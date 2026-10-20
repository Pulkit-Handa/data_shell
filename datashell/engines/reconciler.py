"""The Zero-Deficit Reconciliation Engine.

Enforces the core mathematical axiom of Data Shell:
Rows Read = Refused (Structural) + Held (Semantic) + Admitted Rows

The arithmetic has to foot to exactly 0 unaccounted records.
If it does not, delivery REFUSES TO WRITE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


class ReconciliationFootingError(Exception):
    """Raised when the row balance does not foot to 0."""
    pass


@dataclass
class ReconciliationLedger:
    run_id: str
    timestamp: str
    rows_read: int
    refused_structural: int
    held_quality: int
    rows_to_deliver: int          # Candidate rows admitted for delivery
    actual_delivered: int         # Actual rows written (0 if run was rejected)
    unaccounted_for: int
    footing_matched: bool
    pct_delivered: float
    pct_refused_or_held: float
    is_run_rejected: bool = False
    rejection_reason: str | None = None
    rule_breakdown: dict[str, int] = field(default_factory=dict)
    structural_reasons: dict[str, int] = field(default_factory=dict)

    def mark_rejected(self, reason: str) -> None:
        """Updates ledger when whole run is rejected by contract gate."""
        self.is_run_rejected = True
        self.actual_delivered = 0
        self.rejection_reason = reason
        self.pct_delivered = 0.0

    def summary_table(self) -> str:
        """Render standard ASCII reconciliation ledger."""
        status = "0 · Matched" if self.footing_matched else f"DEFICIT: {self.unaccounted_for} ROWS"
        delivered_str = f"{self.actual_delivered:,d}"
        if self.is_run_rejected:
            delivered_str = f"0 (REJECTED: {self.rows_to_deliver:,} held back)"

        lines = [
            "┌─────────────────────────────────────────────────────────────┐",
            "│           DATA SHELL — RUN RECONCILIATION LEDGER         │",
            "├─────────────────────────────────────────────────────────────┤",
            f"│ Run ID:                {self.run_id:<36} │",
            f"│ Timestamp:             {self.timestamp:<36} │",
            "├─────────────────────────────────────────────────────────────┤",
            f"│ Rows read from source:                    {self.rows_read:>17,d} │",
            f"│ Refused at the door · structural:        -{self.refused_structural:>17,d} │",
            f"│ Held by quality rules:                   -{self.held_quality:>17,d} │",
            f"│ Rows delivered:                           {delivered_str:>17} │",
            "├─────────────────────────────────────────────────────────────┤",
            f"│ Unaccounted for:                          {status:>17} │",
            "├─────────────────────────────────────────────────────────────┤",
            f"│ Delivered: {self.pct_delivered:>6.2f}%     "
            f"|   Refused / Held: {self.pct_refused_or_held:>6.2f}%      │",
            "└─────────────────────────────────────────────────────────────┘",
        ]
        return "\n".join(lines)


class Reconciler:
    """
    Validates that every single record entering the boundary is accounted for.
    Refuses delivery if the balance is unbalanced.
    """

    @staticmethod
    def balance(
        run_id: str,
        rows_read: int,
        refused_structural: int,
        held_quality: int,
        rows_to_deliver: int,
        rule_breakdown: dict[str, int] | None = None,
        structural_reasons: dict[str, int] | None = None,
    ) -> ReconciliationLedger:
        """
        Calculates balance and determines whether the arithmetic foots.
        """
        accounted_total = refused_structural + held_quality + rows_to_deliver
        unaccounted = rows_read - accounted_total
        matched = (unaccounted == 0)

        pct_delivered = (rows_to_deliver / rows_read * 100.0) if rows_read > 0 else 0.0
        pct_held = ((refused_structural + held_quality) / rows_read * 100.0) if rows_read > 0 else 0.0

        ledger = ReconciliationLedger(
            run_id=run_id,
            timestamp=datetime.now(UTC).isoformat(),
            rows_read=rows_read,
            refused_structural=refused_structural,
            held_quality=held_quality,
            rows_to_deliver=rows_to_deliver,
            actual_delivered=rows_to_deliver,
            unaccounted_for=unaccounted,
            footing_matched=matched,
            pct_delivered=pct_delivered,
            pct_refused_or_held=pct_held,
            rule_breakdown=rule_breakdown or {},
            structural_reasons=structural_reasons or {},
        )

        return ledger

    @staticmethod
    def enforce_footing(ledger: ReconciliationLedger) -> None:
        """Strict assertion: raises ReconciliationFootingError if footing fails."""
        if not ledger.footing_matched:
            raise ReconciliationFootingError(
                f"Arithmetic failed to foot! Read: {ledger.rows_read:,}, "
                f"Accounted: {ledger.rows_read - ledger.unaccounted_for:,}, "
                f"Unaccounted: {ledger.unaccounted_for:,}. Delivery refused to write."
            )
