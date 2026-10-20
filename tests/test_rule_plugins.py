"""Section-7 rule extension-point tests.

The check-type namespace is enforced exactly once and never by accident:
built-ins cannot be shadowed by any plugin (installed package or in-process
registration), unknown names fail with the entry-point group spelled out, and
an installed plugin becomes a first-class check on the same dispatch path as
the reviewed built-ins.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import polars as pl
import pytest

import datashell.cli.main as cli_module
from datashell.contract import rule_registry
from datashell.contract.compiler import (
    BatchRuleCompiler,
    RuleCompiler,
)
from datashell.contract.models import (
    BatchRule,
    QualityRule,
    RowRuleCheckType,
)

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture()
def clean_registry():
    """Isolate plugin + overlay state around every test."""
    rule_registry.reload_rule_plugins()
    rule_registry._overlay_row.clear()
    rule_registry._overlay_batch.clear()
    yield rule_registry
    rule_registry._overlay_row.clear()
    rule_registry._overlay_batch.clear()
    rule_registry.reload_rule_plugins()


@pytest.fixture()
def custom_check(clean_registry) -> None:
    """A realistic plugin shape: a domain check the built-ins do not cover."""
    rule_registry.register_row_check(
        "SANE_MAGNITUDE",
        lambda rule: pl.col(rule.column or "amount").is_not_null()
        & (pl.col(rule.column or "amount").abs() < 1_000_000_000.0))


class FakeEntryPoint:
    """Structural stand-in for importlib.metadata.EntryPoint."""

    def __init__(self, group: str, name: str, value: str, obj: Any) -> None:
        self.group = group
        self.name = name
        self.value = value
        self._obj = obj

    def load(self) -> Any:
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


class FakeRegistry:
    def __init__(self, eps: list[FakeEntryPoint]) -> None:
        self._eps = eps

    def select(self, group: str) -> list[FakeEntryPoint]:
        return [ep for ep in self._eps if ep.group == group]


def _row_rule(check: str, column: str = "amount", value: Any = None) -> QualityRule:
    return QualityRule(rule_id="r1", check=check, column=column, value=value)


def _json_payload(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    stdout = capsys.readouterr().out.strip()
    start = stdout.index("{")
    return json.loads(stdout[start:])


# ----------------- In-process registration -----------------

def test_builtin_names_cannot_be_shadowed() -> None:
    with pytest.raises(ValueError, match="cannot be shadowed"):
        rule_registry.register_row_check("GREATER_THAN", lambda rule: pl.lit(True))
    with pytest.raises(ValueError, match="cannot be shadowed"):
        rule_registry.register_batch_check("MIN_ROW_COUNT", lambda rule, table: "")


def test_registered_row_check_dispatches_like_a_builtin(custom_check) -> None:
    rule = _row_rule("SANE_MAGNITUDE")
    expr = RuleCompiler.compile_row_rule(rule)
    frame = pl.DataFrame({"amount": [10.0, 1e12, None]})
    result = frame.select(expr.alias("ok")).get_column("ok")
    assert result.to_list() == [True, False, False]


def test_registered_batch_check_dispatches_like_a_builtin(clean_registry) -> None:
    clean_registry.register_batch_check(
        "ROW_PARITY",
        lambda rule, table: f"SELECT COUNT(*) % 2 = {int(rule.value or 0)} AS passed FROM {table}")
    rule = BatchRule(rule_id="b1", check="ROW_PARITY", value=0)
    sql = BatchRuleCompiler.compile_batch_sql(rule, table_name="admitted_dataset")
    assert "COUNT(*) % 2 = 0" in sql and "FROM admitted_dataset" in sql


def test_plugin_check_inside_full_contract(custom_check) -> None:
    """The plugin composes with the real contract model + compiler end to end."""
    from datashell.contract.models import DataContract

    payload = {
        "apiVersion": "daas/v1", "kind": "DataProduct",
        "metadata": {"name": "plugin-check-demo", "owner": "risk", "version": "1.0.0",
                     "status": "ACTIVE", "description": "demo of a plugin row check"},
        "spec": {
            "sources": [{"type": "LOCAL", "connectionRef": "c", "path": "x/",
                         "format": "parquet",
                         "schema": [
                             {"name": "settlement_id", "polars_type": "Utf8", "nullable": False},
                             {"name": "amount", "polars_type": "Float64", "nullable": False},
                             {"name": "currency", "polars_type": "Utf8", "nullable": False}]}],
            "quality": {"behavior": "DROP_ROW", "rules": [
                {"rule_id": "id-present", "check": "NOT_NULL", "column": "settlement_id"},
                {"rule_id": "sane", "check": "SANE_MAGNITUDE", "column": "amount"},
                {"rule_id": "ccy", "check": "ALLOWED_VALUES", "column": "currency",
                 "value": ["USD", "EUR", "GBP"]}], "batch_rules": []},
            "transformation": {"map": [], "reduce": []},
            "delivery": {"refreshSchedule": "0 * * * *",
                         "targets": [{"type": "PARQUET", "connectionRef": "c",
                                      "path": "./output/curated/x.parquet",
                                      "mode": "overwrite"}],
                         "quarantinePath": "./output/quarantine"},
        },
    }
    contract = DataContract(**payload)
    sane = next(r for r in contract.spec.quality.rules if r.rule_id == "sane")
    expr = RuleCompiler.compile_row_rule(sane)
    held = pl.DataFrame({"amount": [5.0, 5e12]}).select(expr.alias("ok")).get_column("ok")
    assert held.to_list() == [True, False]


# ----------------- Contract-model validation -----------------

def test_unknown_check_is_rejected_at_contract_validation() -> None:
    with pytest.raises(ValueError, match="unknown check 'TOTALLY_MADE_UP'"):
        _row_rule("TOTALLY_MADE_UP")


def test_unknown_batch_check_is_rejected_at_contract_validation() -> None:
    with pytest.raises(ValueError, match="unknown check 'FANCY_ROLLING'"):
        BatchRule(rule_id="b", check="FANCY_ROLLING")


def test_lowercase_check_names_are_rejected() -> None:
    with pytest.raises(ValueError):
        QualityRule.model_validate({"rule_id": "r", "check": "shady_expr", "column": "c"})


def test_check_names_accept_enum_and_plugin_styles(custom_check) -> None:
    assert _row_rule("NOT_NULL", column="amount")
    assert _row_rule("SANE_MAGNITUDE")


# ----------------- Entry-point plugins -----------------

def test_entry_point_plugin_is_discovered_and_dispatched(clean_registry, monkeypatch) -> None:
    def plugin_batch(rule: BatchRule, table_name: str) -> str:
        return f"SELECT COUNT(*) >= 1 AS passed FROM {table_name}"

    entry_point = FakeEntryPoint(
        group=rule_registry.BATCH_CHECK_GROUP,
        name="COUNT_SOMETHING",
        value="fake_pkg:plugin_batch",
        obj=plugin_batch,
    )
    monkeypatch.setattr(rule_registry, "entry_points", lambda: FakeRegistry([entry_point]))
    clean_registry.reload_rule_plugins()

    catalog = rule_registry.available_batch_checks()
    assert catalog["COUNT_SOMETHING"] == "plugin:fake_pkg:plugin_batch"

    rule = BatchRule(rule_id="b", check="COUNT_SOMETHING")
    assert "FROM admitted_dataset" in BatchRuleCompiler.compile_batch_sql(rule)


def test_broken_entry_point_is_reported_not_fatal(clean_registry, monkeypatch) -> None:
    entry_point = FakeEntryPoint(
        group=rule_registry.ROW_CHECK_GROUP, name="BOOM",
        value="boom:module", obj=RuntimeError("boom plugin exploded"))
    monkeypatch.setattr(rule_registry, "entry_points", lambda: FakeRegistry([entry_point]))
    clean_registry.reload_rule_plugins()

    assert rule_registry.available_row_checks().get("BOOM") is None
    errors = rule_registry.plugin_load_errors()
    assert any("BOOM" in error and "boom plugin exploded" in error for error in errors)


# ----------------- CLI catalog -----------------

def test_rules_cli_lists_builtin_and_in_process_checks(custom_check, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli_module.main(["rules", "--json"])
    assert excinfo.value.code == 0
    payload = _json_payload(capsys)
    names = [row[0] for row in payload["checks"]]
    assert "SANE_MAGNITUDE" in names
    assert "GREATER_THAN" in names
    assert RowRuleCheckType.GREATER_THAN.value == "GREATER_THAN"  # enum parity


def test_rules_cli_reports_plugin_errors(capsys, clean_registry) -> None:
    def cli_error_line() -> str:
        return f"row plugin 'BOOM': exploded at {sys.executable}"

    rule_registry._load_errors.append(cli_error_line())
    with pytest.raises(SystemExit) as excinfo:
        cli_module.main(["rules"])
    assert excinfo.value.code == 0
    assert "BOOM" in capsys.readouterr().err
