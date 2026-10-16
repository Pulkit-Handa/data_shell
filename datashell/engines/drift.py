"""Cross-Period Historical Drift & Anomaly Engine.

Enforces rules across periods and runs (e.g. comparing today's feed against last
period's baseline). Detects unexpected volume drops, mean shifts, and metric
anomalies before delivery. Baselines are sourced from the durable boundary
ledger, which keeps a rolling per-tenant history of delivered runs.

Comparison semantics
--------------------
A single relative-percentage rule cannot be applied to every metric: a rate or
proportion whose baseline is ``0`` has an undefined relative change, and a
near-zero denominator makes relative percentages explode into noise. Comparison
is therefore metric-aware, driven by the naming contract declared in
:mod:`datashell.engines.metrics` (``null_rate_*`` / ``avg_*`` / ``sum_*``):

``rate`` (``null_rate_<column>``, proportion in ``[0, 1]``)
    Compared by **absolute delta** in proportion points
    (``|current - baseline|``) against ``max_rate_abs_delta``. Units are
    proportion points, so ``0.10`` means "10 percentage points"; ``1.0`` means
    "the whole range". Absolute deltas stay well defined for zero and near-zero
    baselines and for negative values (which a proportion cannot legitimately
    take, but which are still handled without division by zero).

``mean`` / ``sum`` (and ``unknown`` metric names, for backward compatibility)
    Compared by **relative change** ``|current - baseline| / |baseline| * 100``
    against ``max_metric_drift_pct``. The absolute value of the baseline makes
    the formula well defined for negative baselines (valid for sums and means
    of signed data). When ``|baseline| <= 1e-9`` the relative change is
    undefined by construction, so a documented zero-baseline policy applies:
    a current value that is also ~zero is *unchanged* (0%), while any nonzero
    current value is reported as a ``100%`` change ("emergence from zero") and
    therefore fails a threshold below 100.

Missing, invalid, and non-finite observations are never silently treated as
successful comparisons: a metric with no historical baseline emits an explicit
"baseline established" result, a metric that vanished from the current run
emits a failing result, and non-finite values emit an explicit failed result.
"""

from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Dict, Any, Optional, List
from datashell.core.ledger import BoundaryLedger
from datashell.engines.metrics import RATE, metric_kind


# Values at or below this magnitude are treated as zero for the relative
# comparison policy. 1e-9 is far below any proportion or business aggregate a
# feed produces, so it only ever captures exact/underflowed zeros.
NEAR_ZERO_EPSILON = 1e-9

# Documented zero-baseline semantics for relative metrics: an observation that
# emerges from a zero baseline has an unbounded relative change, which the
# engine reports as a 100% change for thresholding purposes.
ZERO_BASELINE_EMERGENCE_PCT = 100.0


@dataclass
class DriftCheckResult:
    """One metric- or volume-drift comparison.

    ``pct_change`` carries the compared magnitude in the units implied by the
    metric: for regime metrics ("rate") it is the **absolute delta in
    proportion points**; for relative metrics (mean/sum/unknown, and volume) it
    is a **percentage change**. ``threshold_pct`` carries the threshold applied
    to that same quantity. ``details`` always states the units explicitly.
    """

    rule_id: str
    metric: str
    current_value: float
    baseline_value: float
    pct_change: float
    threshold_pct: float
    passed: bool
    details: str


class ContractHistoryTracker(BoundaryLedger):
    """Backward-compatible name for the boundary ledger.

    Historically this tracked only the most recent run aggregate per contract;
    it now persists the full forensic run history, quarantine index, and usage
    records while keeping the original ``record_run``/``get_last_run`` API.
    """


def _finite(value: Any) -> Optional[float]:
    """Coerce a metric observation to a finite float, or None when invalid."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _display(value: Any) -> str:
    return repr(value)[:80]


class HistoricalDriftEngine:
    """Evaluates cross-run drift assertions with strict multi-tenant boundary isolation."""

    def __init__(self, history_tracker: Optional[ContractHistoryTracker] = None):
        self.tracker = history_tracker or ContractHistoryTracker()

    @staticmethod
    def _compare_metric(
        metric_name: str,
        curr_val: float,
        base_val: float,
        *,
        max_metric_drift_pct: float,
        max_rate_abs_delta: float,
    ) -> DriftCheckResult:
        rule_id = f"drift-metric-{metric_name}"
        kind = metric_kind(metric_name)

        if kind == RATE:
            delta = abs(curr_val - base_val)
            passed = delta <= max_rate_abs_delta
            return DriftCheckResult(
                rule_id=rule_id,
                metric=metric_name,
                current_value=curr_val,
                baseline_value=base_val,
                pct_change=round(delta, 6),
                threshold_pct=max_rate_abs_delta,
                passed=passed,
                details=(
                    f"Rate metric '{metric_name}': absolute delta {delta:.6f} proportion points "
                    f"(allowed {max_rate_abs_delta:g}) vs baseline {base_val:.6f}, current {curr_val:.6f}."
                ),
            )

        # Relative comparison for means, sums, and unknown metric families.
        if abs(base_val) <= NEAR_ZERO_EPSILON:
            if abs(curr_val) <= NEAR_ZERO_EPSILON:
                pct = 0.0
                details = (
                    f"Metric '{metric_name}' unchanged at ~zero (baseline {base_val:.2f}, "
                    f"current {curr_val:.2f}); relative change is undefined and treated as 0%."
                )
            else:
                pct = ZERO_BASELINE_EMERGENCE_PCT
                details = (
                    f"Metric '{metric_name}' emerged from a zero baseline (baseline {base_val:.2f}, "
                    f"current {curr_val:.2f}); documented zero-baseline semantics count any nonzero "
                    f"change as {ZERO_BASELINE_EMERGENCE_PCT:.0f}%."
                )
            passed = pct <= max_metric_drift_pct
        else:
            pct = abs((curr_val - base_val) / base_val) * 100.0
            passed = pct <= max_metric_drift_pct
            details = (
                f"Metric '{metric_name}' drifted by {pct:.1f}% (baseline: {base_val:.2f}, "
                f"current: {curr_val:.2f})."
            )

        return DriftCheckResult(
            rule_id=rule_id,
            metric=metric_name,
            current_value=curr_val,
            baseline_value=base_val,
            pct_change=round(pct, 2),
            threshold_pct=max_metric_drift_pct,
            passed=passed,
            details=details,
        )

    def evaluate_drift(
        self,
        contract_name: str,
        current_rows: int,
        current_metrics: Dict[str, float],
        tenant_id: str = "default-tenant",
        max_volume_drop_pct: float = 30.0,
        max_metric_drift_pct: float = 50.0,
        baseline_window: int = 1,
        min_metric_baseline_samples: int = 1,
        max_rate_abs_delta: float = 0.1,
    ) -> List[DriftCheckResult]:
        """Compares current run metrics against a rolling baseline for the tenant.

        ``baseline_window=1`` compares against the previous delivered run (the
        original behaviour); larger windows average the last N runs.

        ``min_metric_baseline_samples`` keeps metric-drift dormant until enough
        delivered history exists. While a feed is still learning, the first run
        establishes the baseline and early runs do not raise metric alarms on
        ordinary variation; volume drift keeps its original semantics.

        ``max_rate_abs_delta`` is the allowed absolute delta (in proportion
        points) for ``rate`` metrics such as null rates.
        """
        baseline = self.tracker.get_baseline(
            contract_name=contract_name, tenant_id=tenant_id, window=baseline_window
        )
        if not baseline:
            # First run for this contract & tenant — establishes baseline
            return [DriftCheckResult(
                rule_id="drift-volume-baseline-established",
                metric="row_volume",

                current_value=float(current_rows),
                baseline_value=float(current_rows),
                pct_change=0.0,
                threshold_pct=max_volume_drop_pct,
                passed=True,
                details="Initial run: established baseline."
            )]

        results: List[DriftCheckResult] = []
        baseline_rows = float(baseline["rows_delivered"])

        # 1. Volume Drift Check (semantics preserved: signed % change vs the
        #    rolling delivered-row baseline, skipping zero-row baselines).
        if baseline_rows > 0:
            vol_change = ((current_rows - baseline_rows) / baseline_rows) * 100.0
            vol_passed = vol_change >= -max_volume_drop_pct
            results.append(DriftCheckResult(
                rule_id="drift-volume-stability",
                metric="row_volume",
                current_value=float(current_rows),
                baseline_value=baseline_rows,
                pct_change=round(vol_change, 2),
                threshold_pct=max_volume_drop_pct,
                passed=vol_passed,
                details=f"Volume changed by {vol_change:.1f}% compared to previous run ({int(baseline_rows):,} rows)."
            ))

        # 2. Metric Drift Checks (rates, means, sums) — only once enough
        #    delivered history exists to make the comparison meaningful.
        baseline_metrics = baseline.get("metrics", {}) if isinstance(baseline.get("metrics"), dict) else {}
        if int(baseline.get("sample_count", 0)) >= max(1, int(min_metric_baseline_samples)):
            for metric_name, raw_curr in current_metrics.items():
                curr_val = _finite(raw_curr)
                base_val = _finite(baseline_metrics[metric_name]) if metric_name in baseline_metrics else None

                if metric_name not in baseline_metrics:
                    # New metric with no tenant history: make the baseline
                    # establishment explicit instead of silently skipping it.
                    results.append(DriftCheckResult(
                        rule_id=f"drift-metric-{metric_name}",
                        metric=metric_name,
                        current_value=curr_val if curr_val is not None else 0.0,
                        baseline_value=0.0,
                        pct_change=0.0,
                        threshold_pct=max_metric_drift_pct,
                        passed=True,
                        details=(
                            f"Metric '{metric_name}' has no historical baseline for this tenant; "
                            f"recording the current observation ({_display(raw_curr)}) as the baseline. "
                            f"No comparison performed."
                        ),
                    ))
                    continue

                if base_val is None:
                    results.append(DriftCheckResult(
                        rule_id=f"drift-metric-{metric_name}",
                        metric=metric_name,
                        current_value=curr_val if curr_val is not None else 0.0,
                        baseline_value=0.0,
                        pct_change=0.0,
                        threshold_pct=max_metric_drift_pct,
                        passed=False,
                        details=(
                            f"Metric '{metric_name}' baseline is missing, invalid, or non-finite "
                            f"({_display(baseline_metrics[metric_name])}); comparison is not possible."
                        ),
                    ))
                    continue

                if curr_val is None:
                    results.append(DriftCheckResult(
                        rule_id=f"drift-metric-{metric_name}",
                        metric=metric_name,
                        current_value=0.0,
                        baseline_value=base_val,
                        pct_change=0.0,
                        threshold_pct=max_metric_drift_pct,
                        passed=False,
                        details=(
                            f"Metric '{metric_name}' current observation is missing, invalid, or "
                            f"non-finite ({_display(raw_curr)}); comparison is not possible."
                        ),
                    ))
                    continue

                results.append(self._compare_metric(
                    metric_name,
                    curr_val,
                    base_val,
                    max_metric_drift_pct=max_metric_drift_pct,
                    max_rate_abs_delta=max_rate_abs_delta,
                ))

            # Metrics that existed in the baseline but vanished from the current
            # run are drift too — never a silent pass.
            for metric_name in baseline_metrics:
                if metric_name in current_metrics:
                    continue
                base_val = _finite(baseline_metrics[metric_name])
                results.append(DriftCheckResult(
                    rule_id=f"drift-metric-{metric_name}",
                    metric=metric_name,
                    current_value=0.0,
                    baseline_value=base_val if base_val is not None else 0.0,
                    pct_change=0.0,
                    threshold_pct=max_metric_drift_pct,
                    passed=False,
                    details=(
                        f"Metric '{metric_name}' is present in the baseline but missing from the "
                        f"current run; the feed's metric surface changed."
                    ),
                ))

        return results

