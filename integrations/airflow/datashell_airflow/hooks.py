"""Airflow connection hook for the Data Shell gateway.

The hook turns a standard Airflow ``http`` connection into a configured
:class:`~datashell_airflow.client.DataShellApiClient`:

* ``host`` / ``schema`` / ``port`` — the gateway base URL.
* ``password`` — the tenant API key (kept out of the DAG source).
* ``extra.api_key`` — alternative location for the API key.
* ``extra.verify_tls`` — set ``false`` only for a trusted internal network.
"""

from __future__ import annotations

from typing import Any

from airflow.hooks.base import BaseHook

from datashell_airflow.client import DataShellApiClient

DEFAULT_CONN_ID = "datashell_default"


def build_base_url(conn: Any) -> str:
    """Derive a base URL from an Airflow connection's host/schema/port fields."""
    host = (conn.host or "").strip()
    if not host:
        raise ValueError(
            f"Data Shell connection '{conn.conn_id}' has no host; set it to the gateway URL."
        )
    if host.startswith(("http://", "https://")):
        return host
    schema = (conn.schema or "http").strip() or "http"
    port = f":{conn.port}" if conn.port else ""
    return f"{schema}://{host}{port}"


def extract_api_key(conn: Any) -> str:
    """Read the API key from the connection password or its ``extra`` JSON."""
    api_key = (conn.password or "").strip()
    if api_key:
        return api_key
    extra: dict[str, Any] = conn.extra_dejson or {}
    api_key = str(extra.get("api_key", "")).strip()
    if not api_key:
        raise ValueError(
            f"Data Shell connection '{conn.conn_id}' has no API key; set the password field."
        )
    return api_key


class DataShellHook(BaseHook):
    """Returns a ready-to-use :class:`DataShellApiClient` for a connection ID."""

    conn_name_attr = "datashell_conn_id"
    default_conn_name = DEFAULT_CONN_ID
    conn_type = "http"
    hook_name = "Data Shell"

    def __init__(
        self,
        datashell_conn_id: str = DEFAULT_CONN_ID,
        *,
        timeout: float = 300.0,
    ) -> None:
        super().__init__()
        self.datashell_conn_id = datashell_conn_id
        self.timeout = timeout

    def get_conn(self) -> DataShellApiClient:
        conn = self.get_connection(self.datashell_conn_id)
        extra: dict[str, Any] = conn.extra_dejson or {}
        verify_tls = bool(extra.get("verify_tls", True))
        return DataShellApiClient(
            build_base_url(conn),
            extract_api_key(conn),
            timeout=self.timeout,
            verify_tls=verify_tls,
        )
