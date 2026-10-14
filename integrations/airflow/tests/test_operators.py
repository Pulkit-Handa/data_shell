"""Airflow-integration tests.

Skipped automatically when Apache Airflow is not installed, so the provider can
be developed and linted in a lightweight environment.
"""

from __future__ import annotations

import pytest

pytest.importorskip("airflow", reason="apache-airflow is required for operator tests")

from datashell_airflow.hooks import build_base_url, extract_api_key  # noqa: E402
from datashell_airflow.operators import (  # noqa: E402
    DataShellEnforceOperator,
    DataShellLocalEnforceOperator,
)


class _Conn:
    def __init__(self, host="datashell.internal", schema="https", port=443, password="k", extra=None):
        self.conn_id = "datashell_default"
        self.host = host
        self.schema = schema
        self.port = port
        self.password = password
        self.extra_dejson = extra or {}


def test_build_base_url_uses_schema_and_port() -> None:
    assert build_base_url(_Conn()) == "https://datashell.internal:443"


def test_build_base_url_passthrough_full_url() -> None:
    assert build_base_url(_Conn(host="http://localhost:8000")) == "http://localhost:8000"


def test_extract_api_key_prefers_password_then_extra() -> None:
    assert extract_api_key(_Conn(password="pw")) == "pw"
    assert extract_api_key(_Conn(password="", extra={"api_key": "from-extra"})) == "from-extra"
    with pytest.raises(ValueError):
        extract_api_key(_Conn(password="", extra={}))


def test_local_operator_builds_expected_command() -> None:
    operator = DataShellLocalEnforceOperator(
        task_id="t",
        contract="/c.json",
        source="/in.csv",
        target="/out.parquet",
        audit="/audit.json",
        tenant="finance",
        streaming=False,
        chunk_size=5000,
    )
    command = operator._command()
    assert command[0] == "datashell"
    assert "--no-streaming" in command
    assert command[command.index("--chunk-size") + 1] == "5000"
    assert command[command.index("--tenant") + 1] == "finance"
    assert "--json" in command


def test_enforce_operator_requires_contract_name() -> None:
    operator = DataShellEnforceOperator(
        task_id="t",
        contract_name="vendor-settlements",
        file_path="/in.csv",
        base_url="https://datashell.example.com",
        api_key="key",
    )
    # A templated field renders from the task context; the raw value is retained.
    assert operator.contract_name == "vendor-settlements"
