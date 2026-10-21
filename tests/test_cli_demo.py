"""End-to-end test of the ``datashell demo`` quickstart walkthrough."""

from __future__ import annotations

import json

import pytest

from datashell.cli import main as cli


def _run(argv):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    return excinfo.value.code


def test_demo_json_reports_refusal_tamper_and_recovery(capsys):
    code = _run(["demo", "--json"])
    assert code == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "LOADED"
    assert payload["rows_read"] == 1000
    assert payload["rows_refused"] == 40       # 20 unparseable amounts + 20 null IDs
    assert payload["rows_held"] == 60          # 35 negative + 25 disallowed currency
    assert payload["rows_delivered"] == 900
    assert payload["footing_matched"] is True
    assert payload["verify_status"] == "AUTHENTIC_AND_UNMODIFIED"
    assert payload["tamper_status"] == "TAMPERED_UNDERLYING_ARTIFACTS"
    assert payload["restored_status"] == "AUTHENTIC_AND_UNMODIFIED"
    assert payload["rule_breakdown"] == {"amount-positive": 35, "currency-settled": 25}


def test_demo_valid_scenario_delivers_every_clean_row(capsys):
    code = _run(["demo", "--json", "--scenario", "valid"])
    assert code == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["scenario"] == "valid"
    assert payload["status"] == "LOADED"
    assert payload["rows_read"] == 900
    assert payload["rows_refused"] == 0
    assert payload["rows_held"] == 0
    assert payload["rows_delivered"] == 900
    assert payload["footing_matched"] is True
    assert payload["verify_status"] == "AUTHENTIC_AND_UNMODIFIED"
    assert payload["tamper_status"] == "TAMPERED_UNDERLYING_ARTIFACTS"
    assert payload["restored_status"] == "AUTHENTIC_AND_UNMODIFIED"


def test_demo_human_output_shows_the_full_narrative(capsys):
    assert _run(["demo"]) == 0
    out = capsys.readouterr().out
    assert "BOUNDARY ENFORCEMENT QUICKSTART DEMO" in out
    assert "AUTHENTIC_AND_UNMODIFIED" in out
    assert "TAMPERED_UNDERLYING_ARTIFACTS" in out
    assert "Bad rows were refused" in out
