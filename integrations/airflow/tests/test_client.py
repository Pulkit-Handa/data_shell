"""Unit tests for the dependency-free Data Shell HTTP client.

These never touch the network: requests are intercepted by monkeypatching
``urllib.request.urlopen``.
"""

from __future__ import annotations

import io
import json
import urllib.error
from urllib.request import Request

import pytest

from datashell_airflow.client import (
    DataShellApiClient,
    DataShellApiError,
    _query_string,
    encode_multipart,
)


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_encode_multipart_includes_fields_and_file() -> None:
    boundary, body = encode_multipart(
        {"contract_name": "vendor-settlements", "streaming": "true"},
        {"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )
    text = body.decode("utf-8")
    assert text.startswith(f"--{boundary}\r\n")
    assert 'name="contract_name"' in text
    assert "vendor-settlements" in text
    assert 'name="file"; filename="data.csv"' in text
    assert "Content-Type: text/csv" in text
    assert "a,b\n1,2\n" in text
    assert text.endswith(f"--{boundary}--\r\n")


def test_query_string_drops_none_values() -> None:
    assert _query_string({"a": 1, "b": None, "c": "x"}) in ("?a=1&c=x", "?c=x&a=1")
    assert _query_string({"a": None}) == ""


def _client() -> DataShellApiClient:
    return DataShellApiClient("https://datashell.example.com", "secret-key")


def test_request_sends_api_key_and_parses_json(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(request: Request, timeout=None, context=None):
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeResponse(json.dumps({"status": "LOADED", "rows_delivered": 5}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = _client().get_certificate("dm_run_abc")

    assert result == {"status": "LOADED", "rows_delivered": 5}
    assert captured["request"].get_header("X-api-key") == "secret-key"
    assert captured["request"].full_url.endswith("/api/v1/runs/dm_run_abc/certificate")


def test_http_error_is_mapped_to_api_error(monkeypatch) -> None:
    def fake_urlopen(request: Request, timeout=None, context=None):
        raise urllib.error.HTTPError(
            url=request.full_url,
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"detail": "role lacks capability"}).encode()),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(DataShellApiError) as exc:
        _client().run_summary()
    assert exc.value.status_code == 403
    assert "lacks capability" in exc.value.detail


def test_connection_error_is_mapped_to_api_error(monkeypatch) -> None:
    def fake_urlopen(request: Request, timeout=None, context=None):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(DataShellApiError) as exc:
        _client().list_runs()
    assert exc.value.status_code == 0
    assert "connection failed" in exc.value.detail


def test_enforce_rejects_oversized_file(tmp_path) -> None:
    big = tmp_path / "big.csv"
    big.write_bytes(b"x" * 2048)
    with pytest.raises(ValueError, match="HTTP upload limit"):
        _client().enforce("c", str(big), max_upload_bytes=1024)


def test_enforce_rejects_missing_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        _client().enforce("c", str(tmp_path / "missing.csv"))


def test_enforce_posts_multipart(monkeypatch, tmp_path) -> None:
    source = tmp_path / "data.csv"
    source.write_bytes(b"a,b\n1,2\n")
    captured: dict = {}

    def fake_urlopen(request: Request, timeout=None, context=None):
        captured["request"] = request
        return _FakeResponse(json.dumps({"status": "LOADED"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = _client().enforce("vendor-settlements", str(source))

    assert result == {"status": "LOADED"}
    content_type = captured["request"].get_header("Content-type")
    assert content_type.startswith("multipart/form-data; boundary=")
    assert captured["request"].method == "POST"


def test_client_requires_credentials() -> None:
    with pytest.raises(ValueError):
        DataShellApiClient("", "key")
    with pytest.raises(ValueError):
        DataShellApiClient("https://x", "")
