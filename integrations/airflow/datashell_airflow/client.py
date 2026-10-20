"""Dependency-free HTTP client for the Data Shell gateway API.

Only the Python standard library is used, so this module imports and unit-tests
without Apache Airflow installed. The Airflow wrappers in
``datashell_airflow.operators`` build on this client.

The HTTP upload path is intended for evidence-sized payloads (a file whose bad
rows you want to attribute). For multi-gigabyte inputs, use
``DataShellLocalEnforceOperator``, which invokes the ``datashell`` CLI and streams
out of core without buffering the whole file in memory.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024 ** 3  # matches the gateway's 20 GiB default


class DataShellApiError(RuntimeError):
    """Raised when the gateway returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"Data Shell API error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def encode_multipart(fields: dict[str, Any], files: dict[str, Any]) -> tuple[str, bytes]:
    """Encode form fields and files as ``multipart/form-data``.

    ``files`` maps a field name to ``(filename, content_bytes, content_type)``.
    """
    boundary = "----datashell" + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += str(value).encode("utf-8") + b"\r\n"
    for name, (filename, content, content_type) in files.items():
        body += f"--{boundary}\r\n".encode()
        body += (
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        ).encode()
        body += f"Content-Type: {content_type}\r\n\r\n".encode()
        body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return boundary, bytes(body)


class DataShellApiClient:
    """Minimal, synchronous client for the Data Shell gateway API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 300.0,
        verify_tls: bool = True,
    ) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("base_url is required")
        if not api_key or not api_key.strip():
            raise ValueError("api_key is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = float(timeout)
        if verify_tls:
            self._ssl_context = ssl.create_default_context()
        else:  # pragma: no cover - explicit opt-out, documented as insecure
            self._ssl_context = ssl._create_unverified_context()

    # ------------------------------------------------------------------ request

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"X-API-Key": self.api_key, **(headers or {})},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._ssl_context
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(exc.read().decode("utf-8"))
                detail = payload.get("detail", "") if isinstance(payload, dict) else str(payload)
            except Exception:  # noqa: BLE001 - fall back to the HTTP reason phrase
                detail = exc.reason or "request failed"
            raise DataShellApiError(exc.code, str(detail)) from exc
        except urllib.error.URLError as exc:
            raise DataShellApiError(0, f"connection failed: {exc.reason}") from exc

        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise DataShellApiError(0, "response was not valid JSON") from exc
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    # ---------------------------------------------------------------- endpoints

    def enforce(
        self,
        contract_name: str,
        file_path: str,
        *,
        streaming: bool = False,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    ) -> dict[str, Any]:
        """Submit a local file for contract enforcement and return the ledger result."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Input file not found: {file_path}")
        size = path.stat().st_size
        if size > max_upload_bytes:
            raise ValueError(
                f"Input file is {size:,} bytes, above the {max_upload_bytes:,}-byte HTTP upload "
                f"limit. Use DataShellLocalEnforceOperator (the CLI path) for multi-gigabyte inputs."
            )
        content_type = {
            ".csv": "text/csv",
            ".jsonl": "application/x-ndjson",
            ".ndjson": "application/x-ndjson",
            ".parquet": "application/octet-stream",
        }.get(path.suffix.lower(), "application/octet-stream")
        boundary, body = encode_multipart(
            {"contract_name": contract_name, "streaming": "true" if streaming else "false"},
            {"file": (path.name, path.read_bytes(), content_type)},
        )
        return self._request_json(
            "POST",
            "/api/v1/enforce",
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def get_certificate(self, run_id: str) -> dict[str, Any]:
        """Fetch and verify the signed certificate for a completed run."""
        return self._request_json("GET", f"/api/v1/runs/{run_id}/certificate")

    def list_runs(
        self,
        *,
        contract_name: str | None = None,
        status: str | None = None,
        tenant_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        query = _query_string({
            "contract_name": contract_name, "status_filter": status,
            "tenant_id": tenant_id, "limit": limit,
        })
        return self._request_json("GET", f"/api/v1/runs{query}")

    def run_summary(self, *, contract_name: str | None = None, tenant_id: str | None = None) -> dict[str, Any]:
        query = _query_string({"contract_name": contract_name, "tenant_id": tenant_id})
        return self._request_json("GET", f"/api/v1/runs/summary{query}")

    def quarantine(
        self,
        *,
        contract_name: str | None = None,
        run_id: str | None = None,
        reason: str | None = None,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        query = _query_string({
            "contract_name": contract_name, "run_id": run_id, "reason": reason,
            "tenant_id": tenant_id, "limit": limit,
        })
        return self._request_json("GET", f"/api/v1/quarantine{query}")

    def usage(
        self,
        *,
        contract_name: str | None = None,
        tenant_id: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        query = _query_string({"contract_name": contract_name, "tenant_id": tenant_id, "since": since})
        return self._request_json("GET", f"/api/v1/usage{query}")


def _query_string(params: dict[str, Any]) -> str:
    import urllib.parse
    filtered = {k: v for k, v in params.items() if v is not None}
    return ("?" + urllib.parse.urlencode(filtered)) if filtered else ""


def _default_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()
