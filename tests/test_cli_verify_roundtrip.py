"""Cross-process CLI signing-key continuity and HTTP hardening headers.

Regression tests for the production-readiness remediation:
separate CLI invocations sharing one workspace must verify, and both HTTP
surfaces must emit baseline security headers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from datashell.core import config as config_module
from datashell.core.config import EnterpriseSettings


def _run_cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    cli_env = dict(os.environ)
    cli_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "datashell.cli.main", *args],
        capture_output=True,
        text=True,
        env=cli_env,
        cwd=Path(__file__).resolve().parent.parent,
        timeout=180,
    )


def test_run_then_verify_across_processes_without_explicit_secrets(tmp_path: Path):
    """P0: ``run`` + ``verify`` in separate processes must verify cleanly."""
    storage_root = tmp_path / "workspace"
    storage_root.mkdir()
    cli_env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("DATASHELL_HMAC_SECRET", "DATASHELL_HMAC_KEYRING")
    }
    cli_env.update(
        {
            "ENVIRONMENT": "test",
            "DATASHELL_STORAGE_ROOT": str(storage_root),
        }
    )

    target = tmp_path / "curated" / "settlements.parquet"
    audit = tmp_path / "audit" / "cert.json"
    target.parent.mkdir(parents=True)
    audit.parent.mkdir(parents=True)
    run_proc = _run_cli(
        cli_env,
        "run",
        "--contract", "examples/vendor_settlements_contract.json",
        "--source", "examples/samples/vendor_settlements_1k.jsonl",
        "--target", str(target),
        "--audit", str(audit),
    )
    assert run_proc.returncode == 0, run_proc.stderr[-2000:]
    assert audit.is_file()

    verify_proc = _run_cli(cli_env, "verify", str(audit))
    assert verify_proc.returncode == 0, verify_proc.stdout[-2000:] + verify_proc.stderr[-2000:]
    assert "AUTHENTIC_AND_UNMODIFIED" in verify_proc.stdout

    with open(audit, "r", encoding="utf-8") as handle:
        cert = json.load(handle)
    assert cert["digital_signature"]["algorithm"] == "HMAC-SHA256"


def test_dev_secret_persists_per_workspace_and_env_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Same storage root shares the dev HMAC secret; explicit env wins."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    for var in ("DATASHELL_HMAC_SECRET", "DATASHELL_HMAC_KEYRING",
                "DATASHELL_OUTPUT_DIR", "DATASHELL_STORAGE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ENVIRONMENT", "test")

    monkeypatch.setenv("DATASHELL_STORAGE_ROOT", str(root_a))
    first = EnterpriseSettings()
    second = EnterpriseSettings()
    assert first.hmac_secret == second.hmac_secret
    assert first.hmac_secret.startswith("dm_dev_")
    secret_file = root_a / "output" / ".datashell-dev-hmac"
    assert secret_file.is_file()
    assert secret_file.stat().st_mode & 0o777 == 0o600

    monkeypatch.setenv("DATASHELL_STORAGE_ROOT", str(root_b))
    third = EnterpriseSettings()
    assert third.hmac_secret != first.hmac_secret

    monkeypatch.setenv("DATASHELL_HMAC_SECRET", "anexplicitdevsecret0123456789abcdef")
    fourth = EnterpriseSettings()
    assert fourth.hmac_secret == "anexplicitdevsecret0123456789abcdef"


def test_production_rejects_published_example_placeholders(monkeypatch: pytest.MonkeyPatch):
    """P0: the documented .env.example placeholders must never boot prod."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://console.example.com")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
    monkeypatch.setenv(
        "DATASHELL_API_KEY", "replace_with_secure_generated_api_key__min_32_chars"
    )
    monkeypatch.setenv(
        "DATASHELL_HMAC_SECRET", "replace_with_secure_generated_hmac_secret__min_32_chars"
    )
    with pytest.raises(RuntimeError, match="placeholder"):
        EnterpriseSettings()


def test_gateway_security_headers_present():
    """P1: the authenticated gateway emits baseline hardening headers."""
    from datashell.server.app import app

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert response.headers.get("Referrer-Policy") == "no-referrer"
    assert response.headers.get("Cache-Control") == "no-store"


def test_demo_security_headers_present():
    """P1: the public demo emits baseline hardening headers."""
    from web.main import app as demo_app

    client = TestClient(demo_app, raise_server_exceptions=False)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("Referrer-Policy") == "no-referrer"
    # Static assets keep framework caching defaults; API JSON is no-store.
    api_response = client.post("/api/demo/run")
    assert api_response.headers.get("Cache-Control") == "no-store"
