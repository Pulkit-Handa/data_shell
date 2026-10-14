"""Cross-Period Historical Drift & Anomaly Engine.

Enforces rules across periods and runs (e.g. comparing today's feed against last
period's baseline). Detects unexpected volume drops, mean shifts, and metric
anomalies before delivery. Baselines are sourced from the durable boundary
ledger, which keeps a rolling per-tenant history of delivered runs.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any, Optional, List
from datashell.core.ledger import BoundaryLedger


@dataclass
class DriftCheckResult:
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


class HistoricalDriftEngine:
    """Evaluates cross-run drift assertions with strict multi-tenant boundary isolation."""

    def __init__(self, history_tracker: Optional[ContractHistoryTracker] = None):
        self.tracker = history_tracker or ContractHistoryTracker()

    def evaluate_drift(
        self,
        contract_name: str,
        current_rows: int,
        current_metrics: Dict[str, float],
        tenant_id: str = "default-tenant",
        max_volume_drop_pct: float = 30.0,
        max_metric_drift_pct: float = 50.0,
        baseline_window: int = 1
    ) -> List[DriftCheckResult]:
        """Compares current run metrics against a rolling baseline for the tenant.

        ``baseline_window=1`` compares against the previous delivered run (the
        original behaviour); larger windows average the last N runs.
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

        # 1. Volume Drift Check
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

        # 2. Metric Drift Checks (Means, sums, rates)
        baseline_metrics = baseline.get("metrics", {})
        for metric_name, curr_val in current_metrics.items():
            if metric_name in baseline_metrics:
                base_val = float(baseline_metrics[metric_name])
                if abs(base_val) > 1e-6:
                    pct_diff = abs((curr_val - base_val) / base_val) * 100.0
                    passed = pct_diff <= max_metric_drift_pct
                    results.append(DriftCheckResult(
                        rule_id=f"drift-metric-{metric_name}",
                        metric=metric_name,
                        current_value=curr_val,
                        baseline_value=base_val,
                        pct_change=round(pct_diff, 2),
                        threshold_pct=max_metric_drift_pct,
                        passed=passed,
                        details=f"Metric '{metric_name}' drifted by {pct_diff:.1f}% (baseline: {base_val:.2f}, current: {curr_val:.2f})."
                    ))

        return results
