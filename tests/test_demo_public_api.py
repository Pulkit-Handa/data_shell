"""End-to-end and security-boundary tests for the public demo app.

These tests exercise the real FastAPI application and the real CLI subprocess:
a sandbox-only unit test does not establish that the hosted demo works. They
cover the production execution path (staging vs. tenant delivery), upload and
request limits, concurrency/rate limiting, subprocess failure mapping, and
staging cleanup.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from datashell.core.config import settings
from datashell.core.security import TokenBucketRateLimiter

TENANT = "demo-tenant"
DEMO_TENANT = f"public-demo-{TENANT}"


# ------------------------------------------------------------------ harness


class DemoHarness:
    def __init__(self, module, dirs):
        self.module = module
        self.dirs = dirs

    def client(self) -> TestClient:
        return TestClient(self.module.app)

    @property
    def tenant_dir(self) -> Path:
        return settings.get_tenant_delivery_dir(DEMO_TENANT)

    def delivered_parquets(self, tenant: str = TENANT):
        directory = settings.get_tenant_delivery_dir(f"public-demo-{tenant}")
        return sorted(p for p in directory.glob("*.parquet"))

    def target_file(self, body, tenant: str = TENANT) -> Path:
        """The active delivered artifact the demo reported (basename only)."""
        return settings.get_tenant_delivery_dir(f"public-demo-{tenant}") / body["target_path"]

    def staging_dirs(self):
        return sorted(p.name for p in self.dirs["temp_dir"].iterdir() if p.name.startswith("datashell_web"))


@pytest.fixture()
def demo(tmp_path, monkeypatch, request):
    """A freshly imported demo app with isolated, Render-shaped storage.

    ``request.param`` may carry ``environment`` plus ``settings``/``env``
    overrides that must be applied *before* the module is imported, because the
    ASGI middleware and rate limiter read them at construction time.
    """
    options = getattr(request, "param", {}) or {}
    environment = options.get("environment", "development")
    storage = tmp_path / "storage"
    dirs = {
        "storage_root": storage,
        "data_dir": storage / "data",
        "contracts_dir": storage / "contracts_registry",
        "output_dir": storage / "output",
        "audit_dir": storage / "output" / "audits",
        "quarantine_dir": storage / "output" / "quarantine",
        "temp_dir": storage / "tmp",
    }
    for path in dirs.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "environment", environment)
    for name, value in dirs.items():
        monkeypatch.setattr(settings, name, value)
    for name, value in (options.get("settings") or {}).items():
        monkeypatch.setattr(settings, name, value)

    for name, value in dirs.items():
        monkeypatch.setenv(f"DATASHELL_{name.upper()}", str(value))
    monkeypatch.setenv("DATASHELL_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("TMPDIR", str(dirs["temp_dir"]))
    monkeypatch.setenv("ENVIRONMENT", environment)
    if environment == "production":
        monkeypatch.setenv("DATASHELL_API_KEY", secrets.token_hex(32))
        monkeypatch.setenv("DATASHELL_HMAC_SECRET", secrets.token_hex(32))
        monkeypatch.setenv("DATASHELL_KEY_ID", "demo-test-key")
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://demo.example")
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
    for name, value in (options.get("env") or {}).items():
        monkeypatch.setenv(name, str(value))

    module = importlib.reload(importlib.import_module("web.main"))
    return DemoHarness(module, dirs)


def _run_enforce(client, contract, source, *, tenant=TENANT, filename="feed.jsonl", interactive=False):
    if interactive:
        return client.post(
            "/api/enforce/interactive",
            files={"source": (filename, source, "application/x-ndjson")},
            data={"tenant": tenant, "contract_json": json.dumps(contract)},
        )
    return client.post(
        "/api/enforce",
        files={
            "contract": ("contract.json", json.dumps(contract).encode(), "application/json"),
            "source": (filename, source, "application/x-ndjson"),
        },
        data={"tenant": tenant},
    )


def _contract(*, batch_min_rows=1, behavior="DROP_ROW", name=None):
    return {
        "apiVersion": "daas/v1",
        "kind": "DataProduct",
        "metadata": {
            "name": name or f"pytest-demo-{uuid.uuid4().hex[:12]}",
            "owner": "demo",
            "version": "1.0.0",
            "status": "ACTIVE",
        },
        "spec": {
            "sources": [
                {
                    "type": "LOCAL",
                    "connectionRef": "local",
                    "path": "source",
                    "format": "jsonl",
                    "schema": [
                        {"name": "id", "polars_type": "Int64", "nullable": False},
                        {"name": "amount", "polars_type": "Float64", "nullable": False},
                    ],
                }
            ],
            "quality": {
                "behavior": behavior,
                "rules": [
                    {"rule_id": "amount-positive", "check": "GREATER_THAN", "column": "amount", "value": 0}
                ],
                "batch_rules": [{"rule_id": "min-rows", "check": "MIN_ROW_COUNT", "value": batch_min_rows}],
            },
            "delivery": {
                "targets": [
                    {"type": "PARQUET", "connectionRef": "demo", "path": "curated/out.parquet", "mode": "overwrite"}
                ],
                "quarantinePath": "quarantine",
            },
        },
    }


def _source(rows=5, amount=10.0):
    records = [{"id": i, "amount": amount + i} for i in range(rows)]
    return ("\n".join(json.dumps(r) for r in records) + "\n").encode()


def _completed(module, returncode, stdout=b"", stderr=b""):
    return module.subprocess.CompletedProcess(["datashell"], returncode, stdout, stderr)


def _patch_cli(monkeypatch, module, *, returncode=0, stdout=b"", stderr=b"", exc=None, calls=None):
    def fake_run(args, *, timeout, workdir):
        if calls is not None:
            calls.append(list(args))
        if exc is not None:
            raise exc
        return _completed(module, returncode, stdout, stderr)

    monkeypatch.setattr(module, "_run_cli", fake_run)
    return fake_run


# -------------------------------------------------- production execution path


@pytest.mark.parametrize("demo", [{"environment": "production"}], indirect=True)
def test_public_demo_succeeds_under_production_configuration(demo):
    """The Render-shaped production run must deliver into the tenant namespace."""
    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=3), _source(5))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "LOADED"
    assert body["rows_delivered"] == 5
    assert body["footing_matched"] is True
    assert body["tenant_id"] == DEMO_TENANT

    delivered = demo.delivered_parquets()
    assert delivered, "a production demo run must persist its delivered artifact"
    target = demo.target_file(body)
    assert target.is_file()
    assert target.name == body["target_path"]
    # The delivery engine also keeps an immutable lineage copy alongside it.
    assert any(p.name.endswith(f"_{body['target_path']}") for p in delivered)
    # Absolute server paths are never disclosed to the browser.
    assert "/" not in body["target_path"]
    assert "/" not in body["audit_path"]
    assert (demo.dirs["audit_dir"] / body["audit_path"]).is_file()


def test_public_demo_succeeds_in_development(demo):
    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=3), _source(5))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "LOADED"


def test_interactive_playground_contract_succeeds(demo):
    with demo.client() as client:
        response = _run_enforce(
            client, _contract(batch_min_rows=3), _source(5), interactive=True
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "LOADED"


def test_policy_rejection_is_a_controlled_422_not_a_success(demo):
    """CLI exit 2 must never be reported as a successful delivery."""
    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=1000), _source(5))
    assert response.status_code == 422
    assert "rejected" in response.json()["detail"].lower()
    assert not demo.delivered_parquets()


# ------------------------------------------------ staging vs delivery separation


@pytest.mark.parametrize("demo", [{"environment": "production"}], indirect=True)
def test_staging_and_delivery_paths_are_separate(demo):
    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=3), _source(5))
    assert response.status_code == 200, response.text

    delivered = demo.delivered_parquets()[-1]
    assert delivered.parent.is_relative_to(demo.dirs["output_dir"] / "tenants")
    assert not delivered.is_relative_to(demo.dirs["temp_dir"])
    # Staging is gone; durable delivery artifacts persist.
    assert demo.staging_dirs() == []
    assert delivered.is_file()

def test_delivery_target_selection_is_tenant_specific(demo):
    with demo.client() as client:
        _run_enforce(client, _contract(batch_min_rows=1), _source(3), tenant="tenant-a")
        _run_enforce(client, _contract(batch_min_rows=1), _source(3), tenant="tenant-b")

    dir_a = settings.get_tenant_delivery_dir("public-demo-tenant-a")
    dir_b = settings.get_tenant_delivery_dir("public-demo-tenant-b")
    assert dir_a != dir_b
    assert dir_a.is_relative_to(demo.dirs["output_dir"] / "tenants")
    assert list(dir_a.glob("*.parquet")) and list(dir_b.glob("*.parquet"))


def test_cross_tenant_artifacts_stay_isolated(demo):
    with demo.client() as client:
        first = _run_enforce(client, _contract(batch_min_rows=1), _source(3), tenant="tenant-a")
        second = _run_enforce(client, _contract(batch_min_rows=1), _source(3), tenant="tenant-b")
    assert first.status_code == 200 and second.status_code == 200

    dir_a = settings.get_tenant_delivery_dir("public-demo-tenant-a")
    dir_b = settings.get_tenant_delivery_dir("public-demo-tenant-b")
    names_a = {p.name for p in dir_a.glob("*.parquet")}
    names_b = {p.name for p in dir_b.glob("*.parquet")}
    assert first.json()["target_path"] in names_a
    assert second.json()["target_path"] in names_b
    assert first.json()["target_path"] not in names_b
    assert second.json()["target_path"] not in names_a
    assert first.json()["target_path"] != second.json()["target_path"]


# ------------------------------------------------------------- input hygiene


def test_user_contract_paths_cannot_escape_the_tenant_namespace(demo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    contract = _contract(batch_min_rows=1)
    contract["spec"]["delivery"] = {
        "targets": [
            {"type": "PARQUET", "path": str(outside / "evil.parquet"), "mode": "overwrite"}
        ],
        "quarantinePath": "/etc/evil-quarantine",
    }
    contract["spec"]["sources"][0]["path"] = "../../../../etc/passwd"

    with demo.client() as client:
        response = _run_enforce(client, contract, _source(3))

    assert response.status_code == 200, response.text
    assert not (outside / "evil.parquet").exists()
    assert not Path("/etc/evil-quarantine").exists()
    delivered = demo.target_file(response.json())
    assert delivered.is_file()
    assert delivered.parent.is_relative_to(demo.dirs["output_dir"] / "tenants")


def test_symlinked_delivery_target_is_refused(demo, monkeypatch, tmp_path):
    """A pre-planted symlink at the target path cannot redirect the delivery."""
    fixed = "deadbeef" * 4
    monkeypatch.setattr(demo.module.uuid, "uuid4", lambda: type("U", (), {"hex": fixed})())
    outside = tmp_path / "outside.parquet"
    tenant_dir = demo.tenant_dir
    planted = tenant_dir / f"demo_{fixed}.parquet"
    if planted.exists() or planted.is_symlink():
        planted.unlink()
    planted.symlink_to(outside)

    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=1), _source(3))

    assert response.status_code == 500
    assert not outside.exists()
    assert "/" not in response.json()["detail"]


def test_symlinked_tenant_root_is_refused(demo, tmp_path):
    tenants = demo.dirs["output_dir"] / "tenants"
    if tenants.exists():
        for child in tenants.iterdir():
            if child.is_symlink() or child.is_file():
                child.unlink()
        tenants.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    tenants.symlink_to(elsewhere, target_is_directory=True)

    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=1), _source(3))

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "Traceback" not in detail and str(elsewhere) not in detail


def test_invalid_tenant_identifier_is_rejected(demo):
    with demo.client() as client:
        for tenant in ("../escape", "Bad Tenant", "a" * 64, "UPPER", "with spaces"):
            response = _run_enforce(client, _contract(batch_min_rows=1), _source(3), tenant=tenant)
            assert response.status_code == 422, tenant
            assert "tenant must be" in response.json()["detail"]


def test_empty_tenant_falls_back_to_the_default_namespace(demo):
    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=1), _source(3), tenant="")
    assert response.status_code == 200, response.text
    assert response.json()["tenant_id"] == "public-demo-default-tenant"


def test_unsupported_source_format_is_rejected(demo):
    with demo.client() as client:
        response = _run_enforce(client, _contract(), _source(3), filename="payload.exe")
    assert response.status_code == 422
    assert ".jsonl" in response.json()["detail"] or ".csv" in response.json()["detail"]


def test_malformed_contract_json_is_rejected(demo):
    with demo.client() as client:
        response = client.post(
            "/api/enforce",
            files={
                "contract": ("contract.json", b"{not-json", "application/json"),
                "source": ("feed.jsonl", _source(3), "application/x-ndjson"),
            },
            data={"tenant": TENANT},
        )
    assert response.status_code == 422
    assert "JSON" in response.json()["detail"]


# ------------------------------------------------------------------ size limits


def test_oversized_source_is_rejected_and_never_runs_the_cli(demo, monkeypatch):
    calls = []
    _patch_cli(monkeypatch, demo.module, calls=calls)
    monkeypatch.setattr(settings, "max_upload_size_bytes", 64)

    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=1), _source(50))

    assert response.status_code == 413
    assert calls == []
    assert demo.staging_dirs() == []


def test_oversized_contract_is_rejected(demo, monkeypatch):
    monkeypatch.setattr(settings, "max_control_body_bytes", 512)
    with demo.client() as client:
        response = _run_enforce(client, _contract(), _source(3))
    assert response.status_code == 413


def test_oversized_interactive_contract_is_rejected(demo, monkeypatch):
    monkeypatch.setattr(settings, "max_control_body_bytes", 512)
    with demo.client() as client:
        response = _run_enforce(
            client, _contract(), _source(3), interactive=True
        )
    assert response.status_code == 413


def test_total_body_limit_rejects_before_parsing(demo, monkeypatch):
    """Declared Content-Length over the request ceiling -> 413 in ASGI."""
    monkeypatch.setattr(settings, "max_request_body_bytes", 1024)
    monkeypatch.setattr(settings, "max_control_body_bytes", 1024)
    reached = {"inner": False}

    async def inner(scope, receive, send):
        reached["inner"] = True

    async def scenario():
        middleware = demo.module.DemoBodyLimitMiddleware(inner)
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/enforce",
                "headers": [(b"content-length", b"9999")],
            },
            receive,
            send,
        )
        return sent

    sent = asyncio.run(scenario())
    assert sent[0]["status"] == 413
    assert reached["inner"] is False
    assert json.loads(sent[1]["body"])["detail"]


def test_chunked_body_without_content_length_is_counted(demo, monkeypatch):
    """No Content-Length (or a misleading one) must not bypass the limit."""
    monkeypatch.setattr(settings, "max_request_body_bytes", 1024)
    monkeypatch.setattr(settings, "max_control_body_bytes", 1024)

    async def inner(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] != "http.request" or not message.get("more_body"):
                break

    async def scenario():
        middleware = demo.module.DemoBodyLimitMiddleware(inner)
        sent = []
        calls = {"n": 0}

        async def receive():
            calls["n"] += 1
            if calls["n"] == 1:
                return {"type": "http.request", "body": b"x" * 800, "more_body": True}
            return {"type": "http.request", "body": b"y" * 800, "more_body": False}

        async def send(message):
            sent.append(message)

        await middleware({"type": "http", "method": "POST", "path": "/api/enforce", "headers": []}, receive, send)
        return sent

    sent = asyncio.run(scenario())
    assert sent[0]["status"] == 413


def test_malformed_content_length_is_rejected(demo):
    async def scenario():
        middleware = demo.module.DemoBodyLimitMiddleware(lambda scope, receive, send: None)
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/enforce",
                "headers": [(b"content-length", b"not-a-number")],
            },
            receive,
            send,
        )
        return sent

    sent = asyncio.run(scenario())
    assert sent[0]["status"] == 400


def test_staging_uses_bounded_chunked_reads(demo, tmp_path):
    """Uploads are streamed in bounded chunks, never fully buffered."""
    class FakeUpload:
        def __init__(self, data: bytes):
            self.data = data

        async def read(self, size=-1):
            # The staging helper must always ask for a bounded chunk size.
            assert size == demo.module.STAGING_CHUNK_BYTES, size
            chunk, self.data = self.data[:size], self.data[size:]
            return chunk

    async def scenario():
        dest = tmp_path / "staged.bin"
        total = await demo.module._stage_upload(
            FakeUpload(b"a" * (demo.module.STAGING_CHUNK_BYTES + 10)),
            dest,
            max_bytes=10 * demo.module.STAGING_CHUNK_BYTES,
            label="source file",
        )
        with pytest.raises(demo.module._StagingError) as excinfo:
            await demo.module._stage_upload(
                FakeUpload(b"b" * 100),
                tmp_path / "too-big.bin",
                max_bytes=50,
                label="source file",
            )
        return total, dest.read_bytes(), excinfo.value

    total, payload, staging_error = asyncio.run(scenario())
    assert total == demo.module.STAGING_CHUNK_BYTES + 10
    assert payload == b"a" * (demo.module.STAGING_CHUNK_BYTES + 10)
    assert staging_error.status_code == 413


# ----------------------------------------------------- concurrency / rate limit


@pytest.mark.parametrize("demo", [{"settings": {"max_concurrent_runs": 1}}], indirect=True)
def test_capacity_exhaustion_returns_503_and_releases_the_slot(demo, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    real_run = demo.module._run_cli

    def blocking_run(args, *, timeout, workdir):
        entered.set()
        release.wait(10)
        return real_run(args, timeout=timeout, workdir=workdir)

    monkeypatch.setattr(demo.module, "_run_cli", blocking_run)
    monkeypatch.setattr(demo.module, "DEMO_CAPACITY_WAIT_SECONDS", 0.5)

    async def scenario():
        transport = httpx.ASGITransport(app=demo.module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as client:
            first = asyncio.create_task(
                client.post(
                    "/api/enforce",
                    files={
                        "contract": ("contract.json",
                                     json.dumps(_contract(batch_min_rows=1)).encode(),
                                     "application/json"),
                        "source": ("feed.jsonl", _source(3), "application/x-ndjson"),
                    },
                    data={"tenant": TENANT},
                )
            )
            await asyncio.to_thread(entered.wait, 10)
            second = await client.post(
                "/api/enforce",
                files={
                    "contract": ("contract.json", json.dumps(_contract(batch_min_rows=1)).encode(), "application/json"),
                    "source": ("feed.jsonl", _source(3), "application/x-ndjson"),
                },
                data={"tenant": TENANT},
            )
            release.set()
            first_response = await first
            return second, first_response

    second, first_response = asyncio.run(scenario())
    assert second.status_code == 503
    assert second.headers.get("retry-after") == "5"
    assert first_response.status_code == 200, first_response.text


def test_slots_are_released_after_timeout_and_exception(demo, monkeypatch):
    real_run = demo.module._run_cli
    with demo.client() as client:
        # A timeout must not leak the execution slot.
        _patch_cli(monkeypatch, demo.module, exc=demo.module._CLITimeout())
        timed_out = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
        assert timed_out.status_code == 504

        # Nor must an unexpected engine failure.
        _patch_cli(monkeypatch, demo.module, returncode=1, stderr=b"boom")
        failed = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
        assert failed.status_code == 500

    # The real CLI still runs afterwards on the same process (slot was freed).
    monkeypatch.setattr(demo.module, "_run_cli", real_run)
    with demo.client() as client:
        recovered = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
    assert recovered.status_code == 200, recovered.text


def test_rate_limit_exhaustion_returns_429(demo, monkeypatch):
    monkeypatch.setattr(demo.module, "demo_rate_limiter", TokenBucketRateLimiter(rate_per_minute=2))
    with demo.client() as client:
        codes = [
            _run_enforce(client, _contract(batch_min_rows=1), _source(3)).status_code
            for _ in range(3)
        ]
    assert codes[:2] == [200, 200]
    assert codes[2] == 429


# ------------------------------------------------- subprocess failure mapping


def test_cli_nonzero_exit_is_never_reported_as_success(demo, monkeypatch):
    _patch_cli(monkeypatch, demo.module, returncode=1, stderr=b"PathSecurityViolation: /secret/path")
    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
    assert response.status_code == 500
    detail = response.json()["detail"]
    for leak in ("Traceback", "PathSecurityViolation", "/secret", "storage", "datashell"):
        assert leak not in detail


def test_cli_exit_two_maps_to_policy_rejection(demo, monkeypatch):
    payload = json.dumps({
        "run_id": "dm_run_x", "status": "REJECTED",
        "held_quality": 4, "refused_structural": 2,
    }).encode()
    _patch_cli(monkeypatch, demo.module, returncode=2, stdout=payload)
    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
    assert response.status_code == 422
    assert "2 refused" in response.json()["detail"] or "4 held" in response.json()["detail"]


def test_malformed_cli_json_is_a_failure(demo, monkeypatch):
    _patch_cli(monkeypatch, demo.module, returncode=0, stdout=b"<html>not json</html>")
    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
    assert response.status_code == 500
    assert "not json" not in response.json()["detail"]


def test_exit_zero_without_a_loaded_run_is_a_failure(demo, monkeypatch):
    stdout = json.dumps({"run_id": "dm_run_x", "status": "FAILED"}).encode()
    _patch_cli(monkeypatch, demo.module, returncode=0, stdout=stdout)
    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
    assert response.status_code == 500


def test_missing_delivered_artifact_is_a_failure(demo, monkeypatch):
    tenant_dir = demo.tenant_dir
    missing = tenant_dir / "demo_missing.parquet"
    audit = demo.dirs["audit_dir"] / "cert.json"
    audit.write_text("{}")
    stdout = json.dumps(
        {"run_id": "dm_run_x", "status": "LOADED", "target_path": str(missing), "audit_path": str(audit)}
    ).encode()
    _patch_cli(monkeypatch, demo.module, returncode=0, stdout=stdout)
    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
    assert response.status_code == 500


def test_missing_certificate_is_a_failure(demo, monkeypatch):
    tenant_dir = demo.tenant_dir
    target = tenant_dir / "demo_present.parquet"
    target.write_bytes(b"parquet")
    stdout = json.dumps(
        {
            "run_id": "dm_run_x",
            "status": "LOADED",
            "target_path": str(target),
            "audit_path": str(demo.dirs["audit_dir"] / "absent.json"),
        }
    ).encode()
    _patch_cli(monkeypatch, demo.module, returncode=0, stdout=stdout)
    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
    assert response.status_code == 500


def test_delivery_outside_the_tenant_namespace_is_a_failure(demo, monkeypatch, tmp_path):
    outside = tmp_path / "elsewhere.parquet"
    outside.write_bytes(b"parquet")
    audit = demo.dirs["audit_dir"] / "cert.json"
    audit.write_text("{}")
    stdout = json.dumps(
        {"run_id": "dm_run_x", "status": "LOADED", "target_path": str(outside), "audit_path": str(audit)}
    ).encode()
    _patch_cli(monkeypatch, demo.module, returncode=0, stdout=stdout)
    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
    assert response.status_code == 500


def test_process_group_termination_kills_children(demo, tmp_path):
    proc = demo.module.subprocess.Popen(
        [demo.module.sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    demo.module._terminate_process_group(proc)
    assert proc.poll() is not None
    assert proc.returncode != 0


# ---------------------------------------------------------- staging robustness


def test_missing_staging_directory_returns_507(demo, monkeypatch):
    monkeypatch.setattr(settings, "temp_dir", demo.dirs["storage_root"] / "does-not-exist")
    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
    assert response.status_code == 507
    assert "does-not-exist" not in response.json()["detail"]


def test_staging_is_cleaned_up_on_success_rejection_failure_and_timeout(demo, monkeypatch):
    with demo.client() as client:
        success = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
        assert success.status_code == 200
        assert demo.staging_dirs() == []

        rejection = _run_enforce(client, _contract(batch_min_rows=1000), _source(3))
        assert rejection.status_code == 422
        assert demo.staging_dirs() == []

        _patch_cli(monkeypatch, demo.module, returncode=1, stderr=b"nope")
        failure = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
        assert failure.status_code == 500
        assert demo.staging_dirs() == []

        _patch_cli(monkeypatch, demo.module, exc=demo.module._CLITimeout())
        timed_out = _run_enforce(client, _contract(batch_min_rows=1), _source(3))
        assert timed_out.status_code == 504
        assert demo.staging_dirs() == []


def test_staged_uploads_are_never_left_behind_on_oversize(demo, monkeypatch):
    monkeypatch.setattr(settings, "max_control_body_bytes", 128)
    with demo.client() as client:
        response = _run_enforce(client, _contract(), _source(3))
    assert response.status_code == 413
    assert demo.staging_dirs() == []


# ---------------------------------------------------------- demo walkthrough


@pytest.mark.parametrize("demo", [{"environment": "production"}], indirect=True)
def test_demo_walkthrough_works_under_production_configuration(demo):
    with demo.client() as client:
        response = client.post("/api/demo/run")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "LOADED"
    assert body["rows_delivered"] == 900
    assert body["verify_status"] == "AUTHENTIC_AND_UNMODIFIED"
    assert "workspace" not in body


def test_demo_walkthrough_failure_is_a_controlled_500(demo, monkeypatch):
    _patch_cli(monkeypatch, demo.module, returncode=1, stderr=b"internal detail")
    with demo.client() as client:
        response = client.post("/api/demo/run")
    assert response.status_code == 500
    assert "internal detail" not in response.json()["detail"]


def test_health_endpoint(demo):
    with demo.client() as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ------------------------------------------- client identity / proxy trust


def _request(module, *, forwarded_for=None, peer="203.0.113.7"):
    """A minimal ASGI request scope for exercising identity/rate-limit logic."""
    from starlette.requests import Request

    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/enforce",
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": (peer, 40000),
        "headers": headers,
    }
    return Request(scope)


def test_client_identity_ignores_forwarded_header_by_default(demo):
    """With trust disabled, a forged X-Forwarded-For never becomes the key."""
    module = demo.module
    assert module.DEMO_TRUST_FORWARDED_HEADERS is False
    forged = _request(module, forwarded_for="198.51.100.9")
    assert module._client_identity(forged) == "203.0.113.7"


@pytest.mark.parametrize(
    "demo",
    [{"env": {"DATASHELL_DEMO_TRUST_FORWARDED_HEADERS": "true"}}],
    indirect=True,
)
def test_client_identity_uses_rightmost_forwarded_hop(demo):
    """Even when enabled, the nearest proxy hop is used, not a client prefix."""
    module = demo.module
    assert module.DEMO_TRUST_FORWARDED_HEADERS is True
    request = _request(module, forwarded_for="1.2.3.4, 198.51.100.9")
    # Rightmost hop wins, so prepending a forged value cannot choose the key.
    assert module._client_identity(request) == "198.51.100.9"


def test_bool_env_rejects_unrecognized_values(demo, monkeypatch):
    monkeypatch.setenv("DATASHELL_TEST_BOOL", "maybe")
    with pytest.raises(RuntimeError):
        demo.module._bool_env("DATASHELL_TEST_BOOL", False)


@pytest.mark.parametrize(
    "demo",
    [
        {
            "env": {
                # Worst case: the client key is fully spoofable...
                "DATASHELL_DEMO_TRUST_FORWARDED_HEADERS": "true",
                "RATE_LIMIT_REQUESTS_PER_MINUTE": "100000",
                # ...but a small identity-independent ceiling still applies.
                "DATASHELL_DEMO_GLOBAL_RATE_LIMIT_REQUESTS_PER_MINUTE": "3",
            }
        }
    ],
    indirect=True,
)
def test_global_ceiling_bounds_spoofed_client_keys(demo):
    """Rotating a forged client identity cannot bypass the global ceiling."""
    module = demo.module
    for index in range(3):
        request = _request(module, forwarded_for=f"10.0.{index}.{index}")
        module._rate_limit_or_raise(request)  # within the global budget
    with pytest.raises(module.HTTPException) as excinfo:
        module._rate_limit_or_raise(_request(module, forwarded_for="10.9.9.9"))
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers["Retry-After"] == "5"


@pytest.mark.parametrize(
    "demo",
    [{"env": {"DATASHELL_DEMO_GLOBAL_RATE_LIMIT_REQUESTS_PER_MINUTE": "100000"}}],
    indirect=True,
)
def test_per_client_limit_still_applies_with_stable_identity(demo, monkeypatch):
    """The per-client fairness bucket is evaluated before the global ceiling."""
    module = demo.module
    monkeypatch.setattr(module, "demo_rate_limiter", TokenBucketRateLimiter(2))
    module._rate_limit_or_raise(_request(module))
    module._rate_limit_or_raise(_request(module))
    with pytest.raises(module.HTTPException) as excinfo:
        module._rate_limit_or_raise(_request(module))
    assert excinfo.value.status_code == 429


# ------------------------------------------------- demo storage quota policy


def _touch(path: Path, age_seconds: float, payload: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def _audit_cert(path: Path, tenant: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tenant_id": tenant, "run_id": path.stem}), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "demo",
    [{"env": {"DATASHELL_DEMO_ARTIFACT_RETENTION_MINUTES": "30"}}],
    indirect=True,
)
def test_storage_quota_evicts_only_stale_demo_artifacts(demo):
    module, dirs = demo.module, demo.dirs
    old_demo = _touch(dirs["output_dir"] / "tenants" / "public-demo-acme" / "old.parquet", 3600)
    new_demo = _touch(dirs["output_dir"] / "tenants" / "public-demo-acme" / "new.parquet", 10)
    old_quarantine = _touch(
        dirs["quarantine_dir"] / "tenants" / "datashell-demo" / "old_held.parquet", 3600
    )
    # The CLI walkthrough namespace is a demo namespace too.
    fresh_quarantine = _touch(
        dirs["quarantine_dir"] / "tenants" / "datashell-demo" / "fresh_held.parquet", 5
    )
    # A non-demo tenant must never be touched, however old.
    api_tenant = _touch(dirs["output_dir"] / "tenants" / "acme" / "keep.parquet", 100000)
    demo_cert = _audit_cert(dirs["audit_dir"] / "dm_run_demo_audit_certificate.json", "public-demo-acme")
    os.utime(demo_cert, (time.time() - 3600, time.time() - 3600))
    api_cert = _audit_cert(dirs["audit_dir"] / "dm_run_api_audit_certificate.json", "acme")
    os.utime(api_cert, (time.time() - 100000, time.time() - 100000))

    module._enforce_demo_storage_quota()

    assert not old_demo.exists()
    assert not old_quarantine.exists()
    assert not demo_cert.exists()
    assert new_demo.exists()
    assert fresh_quarantine.exists()
    assert api_tenant.exists(), "non-demo tenant artifact was evicted"
    assert api_cert.exists(), "non-demo audit certificate was evicted"


@pytest.mark.parametrize(
    "demo",
    [
        {
            "env": {
                "DATASHELL_DEMO_ARTIFACT_RETENTION_MINUTES": "100000",
                "DATASHELL_DEMO_MAX_ARTIFACT_BYTES": "100",
            }
        }
    ],
    indirect=True,
)
def test_storage_quota_enforces_total_bytes_oldest_first(demo):
    module, dirs = demo.module, demo.dirs
    tenant_dir = dirs["output_dir"] / "tenants" / "public-demo-acme"
    oldest = _touch(tenant_dir / "a.parquet", 3000, b"a" * 60)
    middle = _touch(tenant_dir / "b.parquet", 2000, b"b" * 60)
    newest = _touch(tenant_dir / "c.parquet", 1000, b"c" * 60)
    outside = _touch(dirs["output_dir"] / "tenants" / "other" / "keep.parquet", 100000, b"k" * 60)

    module._enforce_demo_storage_quota()

    # 180 bytes > 100 cap -> the two oldest demo files go, newest survives.
    assert not oldest.exists()
    assert not middle.exists()
    assert newest.exists()
    assert outside.exists()


@pytest.mark.parametrize(
    "demo",
    [{"env": {"DATASHELL_DEMO_ARTIFACT_RETENTION_MINUTES": "30"}}],
    indirect=True,
)
def test_oversized_or_unreadable_audit_certificates_are_never_deleted(demo):
    module, dirs = demo.module, demo.dirs
    big = _touch(dirs["audit_dir"] / "dm_run_big_audit_certificate.json", 3600, b"{" + b"0" * 400000)
    garbage = _touch(dirs["audit_dir"] / "dm_run_bad_audit_certificate.json", 3600, b"not json")

    module._enforce_demo_storage_quota()

    assert big.exists()
    assert garbage.exists()


@pytest.mark.parametrize(
    "demo",
    [{"env": {"DATASHELL_DEMO_ARTIFACT_RETENTION_MINUTES": "30"}}],
    indirect=True,
)
def test_enforce_endpoint_sweeps_stale_demo_artifacts(demo):
    """The quota sweep runs as part of a real enforcement request."""
    dirs = demo.dirs
    stale = _touch(dirs["output_dir"] / "tenants" / "public-demo-acme" / "stale.parquet", 3600)

    with demo.client() as client:
        response = _run_enforce(client, _contract(batch_min_rows=3), _source(5))

    assert response.status_code == 200, response.text
    assert not stale.exists()

