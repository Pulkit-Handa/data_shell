"""Airflow operators that gate a DAG on Data Shell boundary enforcement.

Two operators are provided:

``DataShellEnforceOperator``
    Calls the gateway API over HTTP. Best when a remote control plane owns the
    contract registry and the input is evidence-sized.

``DataShellLocalEnforceOperator``
    Invokes the ``datashell`` CLI on the worker. Best for multi-gigabyte inputs
    and air-gapped workers, because the CLI streams out of core and never
    buffers the whole file in memory.

Both return the same shape of result (the terminal ledger record), so downstream
tasks can branch on ``status`` without caring which operator ran.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from typing import Any

from airflow.exceptions import AirflowException
from airflow.models import BaseOperator

from datashell_airflow.client import DataShellApiClient
from datashell_airflow.hooks import DEFAULT_CONN_ID, DataShellHook


class DataShellEnforceOperator(BaseOperator):
    """Enforce a Data Shell contract against a file via the gateway API."""

    template_fields: Sequence[str] = ("contract_name", "file_path")

    def __init__(
        self,
        *,
        contract_name: str,
        file_path: str,
        datashell_conn_id: str = DEFAULT_CONN_ID,
        base_url: str | None = None,
        api_key: str | None = None,
        streaming: bool = False,
        verify_tls: bool = True,
        timeout: float = 300.0,
        fail_on_rejection: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.contract_name = contract_name
        self.file_path = file_path
        self.datashell_conn_id = datashell_conn_id
        self.base_url = base_url
        self.api_key = api_key
        self.streaming = streaming
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.fail_on_rejection = fail_on_rejection

    def _client(self) -> DataShellApiClient:
        # An explicit base_url (with an api_key) lets a DAG target a gateway
        # without registering an Airflow connection; otherwise use the hook.
        if self.base_url:
            return DataShellApiClient(
                self.base_url,
                self.api_key or "",
                timeout=self.timeout,
                verify_tls=self.verify_tls,
            )
        return DataShellHook(self.datashell_conn_id, timeout=self.timeout).get_conn()

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        self.log.info(
            "Enforcing contract %s against %s", self.contract_name, self.file_path
        )
        result = self._client().enforce(
            self.contract_name, self.file_path, streaming=self.streaming
        )
        if self.fail_on_rejection and result.get("status") == "REJECTED":
            raise AirflowException(
                f"Data Shell rejected contract '{self.contract_name}': "
                f"{result.get('rows_refused', 0)} refused, "
                f"{result.get('rows_held', 0)} held"
            )
        return result


class DataShellLocalEnforceOperator(BaseOperator):
    """Enforce a Data Shell contract with the local ``datashell`` CLI."""

    template_fields: Sequence[str] = ("contract", "source", "target", "audit")

    def __init__(
        self,
        *,
        contract: str,
        source: str,
        target: str | None = None,
        audit: str | None = None,
        tenant: str = "default-tenant",
        streaming: bool | None = None,
        chunk_size: int | None = None,
        datashell_bin: str = "datashell",
        fail_on_rejection: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.contract = contract
        self.source = source
        self.target = target
        self.audit = audit
        self.tenant = tenant
        self.streaming = streaming
        self.chunk_size = chunk_size
        self.datashell_bin = datashell_bin
        self.fail_on_rejection = fail_on_rejection

    def _command(self) -> list[str]:
        command = [
            self.datashell_bin,
            "run",
            "--contract",
            self.contract,
            "--source",
            self.source,
            "--tenant",
            self.tenant,
            "--json",
        ]
        if self.target:
            command += ["--target", self.target]
        if self.audit:
            command += ["--audit", self.audit]
        if self.streaming is True:
            command.append("--streaming")
        elif self.streaming is False:
            command.append("--no-streaming")
        if self.chunk_size:
            command += ["--chunk-size", str(self.chunk_size)]
        return command

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        binary = shutil.which(self.datashell_bin)
        if binary is None:
            raise AirflowException(
                f"Data Shell CLI '{self.datashell_bin}' was not found on PATH. "
                f"Install the data-shell package in the worker environment."
            )
        command = self._command()
        command[0] = binary
        self.log.info("Running: %s", " ".join(command))
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        # Exit codes: 0 = delivered, 2 = REJECTED by policy, 1/3 = engine error.
        if completed.returncode not in (0, 2):
            raise AirflowException(
                f"datashell run failed with exit code {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        try:
            result = json.loads(completed.stdout)
        except ValueError as exc:
            raise AirflowException(
                f"datashell run produced non-JSON output: {completed.stdout[:500]}"
            ) from exc
        if self.fail_on_rejection and result.get("status") == "REJECTED":
            raise AirflowException(
                f"Data Shell rejected contract '{self.contract}': "
                f"{result.get('rows_refused', 0)} refused, "
                f"{result.get('rows_held', 0)} held"
            )
        return result
