"""Metric-aware drift comparison tests (P0-3 regression coverage).

The original engine guarded every metric comparison with ``if abs(base) > 1e-6``,
so a rate that moved from ``0.0`` to ``1.0`` — a total null-rate shift — was
silently skipped and the feed was delivered. These tests pin the corrected
metric-aware policy: absolute-delta thresholds for rates, relative change for
means/sums, and documented semantics for zero/near-zero/invalid observations.
"""

from __future__ import annotations

import math

import pytest

from datashell.core.ledger import BoundaryLedger
from datashell.engines.drift import HistoricalDriftEngine
from datashell.engines.metrics import MEAN, RATE, SUM, UNKNOWN, metric_kind


CONTRACT = "drift-metric-feed"
TENANT = "default-tenant"


def _engine(tmp_path) -> HistoricalDriftEngine:
    return HistoricalDriftEngine(history_tracker=BoundaryLedger(db_path=tmp_path / "history.db"))


def _record(tracker, run_id: str, rows: int, metrics: dict) -> None:
    tracker.record_run(run_id, CONTRACT, rows, rows, metrics, tenant_id=TENANT)


def _by_metric(results):
    return {r.metric: r for r in results if r.rule_id.startswith("drift-metric-")}


# --------------------------------------------------------------- naming contract


def test_metric_kind_is_driven_by_the_declared_naming_contract():
    assert metric_kind("null_rate_amount") == RATE
    assert metric_kind("avg_amount") == MEAN
    assert metric_kind("sum_amount") == SUM
    assert metric_kind("some_custom_metric") == UNKNOWN


# --------------------------------------------------------- P0 regression: rates


def test_zero_to_one_null_rate_is_drift_regression(tmp_path):
    """Baseline 0 -> current 1 must produce a FAILING metric-drift result.

    This is the exact case the old ``abs(base) > 1e-6`` guard suppressed; the
    test fails against the original implementation because no result was
    emitted at all.
    """
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"null_rate_id": 0.0})

    results = engine.evaluate_drift(CONTRACT, 100, {"null_rate_id": 1.0})
    check = _by_metric(results).get("null_rate_id")

    assert check is not None, "zero-baseline metric comparison must not be suppressed"
    assert check.passed is False
    assert check.baseline_value == pytest.approx(0.0)
    assert check.current_value == pytest.approx(1.0)
    # Rate metrics report the absolute delta in proportion points.
    assert check.pct_change == pytest.approx(1.0)
    assert check.threshold_pct == pytest.approx(0.10)
    assert "proportion points" in check.details


def test_rate_small_positive_move_from_zero_passes(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"null_rate_id": 0.0})

    check = _by_metric(engine.evaluate_drift(CONTRACT, 100, {"null_rate_id": 0.02}))["null_rate_id"]
    assert check.passed is True
    assert check.pct_change == pytest.approx(0.02)


def test_rate_unchanged_at_zero_passes(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"null_rate_id": 0.0})

    check = _by_metric(engine.evaluate_drift(CONTRACT, 100, {"null_rate_id": 0.0}))["null_rate_id"]
    assert check.passed is True
    assert check.pct_change == pytest.approx(0.0)


def test_rate_baseline_nonzero_to_zero_respects_absolute_delta(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"null_rate_a": 0.05, "null_rate_b": 0.5})

    results = _by_metric(engine.evaluate_drift(CONTRACT, 100, {"null_rate_a": 0.0, "null_rate_b": 0.0}))
    # 0.05 -> 0.0 is a 0.05-point move: within the 0.10 absolute threshold.
    assert results["null_rate_a"].passed is True
    # 0.50 -> 0.0 is a 0.50-point move: well beyond it.
    assert results["null_rate_b"].passed is False


def test_rate_threshold_is_configurable(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"null_rate_id": 0.0})

    lenient = _by_metric(
        engine.evaluate_drift(CONTRACT, 100, {"null_rate_id": 0.4}, max_rate_abs_delta=0.5)
    )["null_rate_id"]
    strict = _by_metric(
        engine.evaluate_drift(CONTRACT, 100, {"null_rate_id": 0.6}, max_rate_abs_delta=0.5)
    )["null_rate_id"]
    assert lenient.passed is True and lenient.threshold_pct == pytest.approx(0.5)
    assert strict.passed is False


# --------------------------------------------------- means, sums, relative rule


def test_mean_zero_baseline_emergence_fails_and_is_explained(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"avg_amount": 0.0})

    check = _by_metric(engine.evaluate_drift(CONTRACT, 100, {"avg_amount": 5.0}))["avg_amount"]
    assert check.passed is False
    assert check.pct_change == pytest.approx(100.0)
    assert "zero baseline" in check.details


def test_sum_zero_baseline_emergence_fails(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"sum_amount": 0.0})

    check = _by_metric(engine.evaluate_drift(CONTRACT, 100, {"sum_amount": 500.0}))["sum_amount"]
    assert check.passed is False
    assert check.pct_change == pytest.approx(100.0)


def test_zero_baseline_staying_zero_passes_for_means_and_sums(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"avg_amount": 0.0, "sum_amount": 0.0})

    results = _by_metric(engine.evaluate_drift(CONTRACT, 100, {"avg_amount": 0.0, "sum_amount": 0.0}))
    assert results["avg_amount"].pct_change == pytest.approx(0.0)
    assert results["sum_amount"].pct_change == pytest.approx(0.0)
    assert results["avg_amount"].passed and results["sum_amount"].passed


def test_negative_baseline_is_well_defined(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"avg_amount": -10.0, "sum_amount": -1000.0})

    results = _by_metric(
        engine.evaluate_drift(CONTRACT, 100, {"avg_amount": -10.0, "sum_amount": -500.0})
    )
    # Unchanged negative baseline.
    assert results["avg_amount"].pct_change == pytest.approx(0.0)
    assert results["avg_amount"].passed is True
    # -1000 -> -500 is a 50% change against the absolute baseline: at threshold.
    assert results["sum_amount"].pct_change == pytest.approx(50.0)
    assert results["sum_amount"].passed is True

    worse = _by_metric(engine.evaluate_drift(CONTRACT, 100, {"avg_amount": -50.0}))["avg_amount"]
    assert worse.pct_change == pytest.approx(400.0)
    assert worse.passed is False


def test_near_zero_positive_and_negative_baselines_are_treated_as_zero(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"avg_pos": 1e-9, "avg_neg": -1e-9})

    results = _by_metric(
        engine.evaluate_drift(CONTRACT, 100, {"avg_pos": 0.0, "avg_neg": 0.0})
    )
    assert results["avg_pos"].pct_change == pytest.approx(0.0)
    assert results["avg_neg"].pct_change == pytest.approx(0.0)
    # No division-by-zero explosion for near-zero denominators.
    assert results["avg_pos"].passed and results["avg_neg"].passed

    emerged = _by_metric(engine.evaluate_drift(CONTRACT, 100, {"avg_pos": 5.0}))["avg_pos"]
    assert emerged.pct_change == pytest.approx(100.0)
    assert emerged.passed is False


def test_normal_relative_change(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"avg_amount": 10.0})

    within = _by_metric(engine.evaluate_drift(CONTRACT, 100, {"avg_amount": 12.0}))["avg_amount"]
    beyond = _by_metric(engine.evaluate_drift(CONTRACT, 100, {"avg_amount": 20.0}))["avg_amount"]
    assert within.pct_change == pytest.approx(20.0) and within.passed is True
    assert beyond.pct_change == pytest.approx(100.0) and beyond.passed is False


def test_unknown_metric_families_keep_the_backward_compatible_relative_rule(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"custom_ratio": 10.0})

    check = _by_metric(engine.evaluate_drift(CONTRACT, 100, {"custom_ratio": 20.0}))["custom_ratio"]
    assert check.pct_change == pytest.approx(100.0)
    assert check.passed is False


# ------------------------------------------- missing / invalid / non-finite data


def test_missing_baseline_metric_is_explicit_not_silent(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"avg_amount": 10.0})

    check = _by_metric(engine.evaluate_drift(CONTRACT, 100, {"null_rate_new_col": 0.5}))["null_rate_new_col"]
    assert check.passed is True  # establishing, never a fabricated failure
    assert "no historical baseline" in check.details


def test_metric_missing_from_current_run_is_drift(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"avg_amount": 10.0, "sum_amount": 1000.0})

    results = _by_metric(engine.evaluate_drift(CONTRACT, 100, {"avg_amount": 10.0}))
    assert results["sum_amount"].passed is False
    assert "missing from the current run" in results["sum_amount"].details


def test_non_finite_baseline_or_current_is_an_explicit_failure(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"avg_amount": float("nan"), "sum_amount": 1.0})

    results = _by_metric(
        engine.evaluate_drift(CONTRACT, 100, {"avg_amount": 10.0, "sum_amount": math.inf})
    )
    assert results["avg_amount"].passed is False
    assert "not possible" in results["avg_amount"].details
    assert results["sum_amount"].passed is False
    assert "not possible" in results["sum_amount"].details


# --------------------------------------------- sample-count, windows, volume


def test_insufficient_sample_count_keeps_metric_drift_dormant(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"avg_amount": 10.0})

    results = engine.evaluate_drift(CONTRACT, 100, {"avg_amount": 100.0}, min_metric_baseline_samples=2)
    assert not [r for r in results if r.rule_id.startswith("drift-metric-")]
    assert [r for r in results if r.rule_id == "drift-volume-stability"]


def test_multiple_baseline_windows_average_prior_runs(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"avg_amount": 0.0})
    _record(engine.tracker, "run-2", 100, {"avg_amount": 10.0})
    _record(engine.tracker, "run-3", 100, {"avg_amount": 20.0})

    results = _by_metric(
        engine.evaluate_drift(CONTRACT, 100, {"avg_amount": 10.0}, baseline_window=3)
    )
    # Window average of (20, 10, 0) is 10.0 -> no drift.
    assert results["avg_amount"].baseline_value == pytest.approx(10.0)
    assert results["avg_amount"].passed is True

    shifted = _by_metric(
        engine.evaluate_drift(CONTRACT, 100, {"avg_amount": 20.0}, baseline_window=3)
    )["avg_amount"]
    assert shifted.baseline_value == pytest.approx(10.0)
    assert shifted.pct_change == pytest.approx(100.0)
    assert shifted.passed is False


def test_volume_drift_and_initial_baseline_behaviour_preserved(tmp_path):
    engine = _engine(tmp_path)

    initial = engine.evaluate_drift(CONTRACT, 10_000, {})
    assert len(initial) == 1
    assert initial[0].rule_id == "drift-volume-baseline-established"
    assert initial[0].passed is True

    _record(engine.tracker, "run-1", 10_000, {})
    stable = engine.evaluate_drift(CONTRACT, 9_500, {})
    assert stable[0].rule_id == "drift-volume-stability"
    assert stable[0].passed is True

    dropped = engine.evaluate_drift(CONTRACT, 5_000, {})
    assert dropped[0].passed is False
    assert dropped[0].pct_change == pytest.approx(-50.0)


def test_every_result_exposes_status_magnitude_threshold_and_details(tmp_path):
    engine = _engine(tmp_path)
    _record(engine.tracker, "run-1", 100, {"null_rate_id": 0.0, "avg_amount": 10.0})

    for result in engine.evaluate_drift(CONTRACT, 100, {"null_rate_id": 1.0, "avg_amount": 12.0}):
        assert isinstance(result.passed, bool)
        assert isinstance(result.pct_change, float)
        assert isinstance(result.threshold_pct, float)
        assert isinstance(result.details, str) and result.details
