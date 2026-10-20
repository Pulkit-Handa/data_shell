"""PUB-001 regression tests: a reader resolving the generation pointer first
always observes one complete generation - never a mixture of old and new
files - even when publication is interrupted at every stage.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("polars")


def _read_pointer(path: Path):
    pointer = path.with_name(path.name + ".generation.json")
    with open(pointer, encoding="utf-8") as handle:
        return json.load(handle)


def _commit_pointer(path: Path, payload: dict):
    from datashell.core.commit import _write_generation_pointer_atomically

    pointer = path.with_name(path.name + ".generation.json")
    _write_generation_pointer_atomically(pointer, payload)


def test_pointer_is_atomic_commit_point_and_readers_never_see_mixed_generation(
    tmp_path: Path,
):
    """PUB-001: the generation pointer is the single reader-visible switch."""
    from datashell.core.commit import (
    publish_transactionally,
    read_generation_pointer,
)

    target = tmp_path / "delivered.parquet"
    lineage = tmp_path / "dm_run_x_lineage_delivered.parquet"
    audit = tmp_path / "dm_run_x_audit_certificate.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    new_pointer = {"run_id": "dm_run_new", "tenant_id": "t1"}
    _commit_pointer(target, {"run_id": "dm_run_old", "tenant_id": "t1"})

    staged_target = target.with_name(".staged_dm_run_new_delivered.parquet")
    staged_lineage = target.with_name(".staged_dm_run_new_lineage_delivered.parquet")
    staged_audit = target.with_name(".staged_dm_run_new_dm_run_x_audit_certificate.json")
    staged_target.write_bytes(b"new-content-v2")
    staged_lineage.write_bytes(b"lineage-v2")
    staged_audit.write_text(json.dumps({"status": "ok-v2"}))

    publish_transactionally(
        [
            (staged_lineage, lineage),
            (staged_audit, audit),
            (staged_target, target),
        ],
        run_id="dm_run_new",
        transaction_root=tmp_path,
        generation_pointer=new_pointer,
    )

    pointer = read_generation_pointer(target)
    assert pointer["run_id"] == "dm_run_new"
    assert target.read_bytes() == b"new-content-v2"
    assert json.loads(audit.read_text())["status"] == "ok-v2"
    assert not staged_target.exists()


def test_crash_before_pointer_swap_leaves_old_generation_visible(tmp_path: Path):
    """PUB-001: crash between artifact renames and pointer swap keeps old generation."""
    from datashell.core.commit import (
        _write_generation_pointer_atomically,
        generation_pointer_path,
        publish_transactionally,
    )

    target = tmp_path / "delivered.parquet"
    lineage = tmp_path / "dm_run_x_lineage_delivered.parquet"
    audit = tmp_path / "dm_run_x_audit_certificate.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    pointer_path = generation_pointer_path(target)
    _write_generation_pointer_atomically(pointer_path, {"run_id": "dm_run_old", "tenant_id": "t1"})
    target.write_bytes(b"old-content-v1")

    staged_target = target.with_name(".staged_dm_run_new_delivered.parquet")
    staged_lineage = target.with_name(".staged_dm_run_new_lineage_delivered.parquet")
    staged_audit = audit.with_name(".staged_dm_run_new_dm_run_x_audit_certificate.json")
    staged_target.write_bytes(b"new-content-v2")
    staged_lineage.write_bytes(b"lineage-v2")
    staged_audit.write_text(json.dumps({"status": "ok-v2"}))

    os.replace(staged_lineage, lineage)
    os.replace(staged_audit, audit)
    os.replace(staged_target, target)

    # Even though renames are visible on disk, the pointer (the commit gate)
    # still names the old generation - readers resolving the pointer first
    # never observe a mixed generation.
    assert _read_pointer(target)["run_id"] == "dm_run_old"

    # A subsequent full publication swaps the pointer atomically; readers then
    # observe the new complete generation.
    new_target = target.with_name(".staged_dm_run_new2_delivered.parquet")
    new_lineage = lineage.with_name(".staged_dm_run_new2_lineage_delivered.parquet")
    new_audit = audit.with_name(".staged_dm_run_new2_dm_run_x_audit_certificate.json")
    new_target.write_bytes(b"new-content-v3")
    new_lineage.write_bytes(b"lineage-v3")
    new_audit.write_text(json.dumps({"status": "ok-v3"}))
    publish_transactionally(
        [
            (new_lineage, lineage),
            (new_audit, audit),
            (new_target, target),
        ],
        run_id="dm_run_new2",
        transaction_root=tmp_path,
        generation_pointer={"run_id": "dm_run_new2", "tenant_id": "t1"},
    )
    assert _read_pointer(target)["run_id"] == "dm_run_new2"
    assert target.read_bytes() == b"new-content-v3"


def test_publication_rolls_back_on_failure_and_preserves_old_output(tmp_path: Path):
    """PUB-001: publication failure does not destroy the previous output."""
    from datashell.core.commit import publish_transactionally

    target = tmp_path / "delivered.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    _commit_pointer(
        target,
        {"run_id": "dm_run_old", "tenant_id": "t1"},
    )
    target.write_bytes(b"old-content-v1")

    staged = target.with_name(".staged_dm_run_new_delivered.parquet")
    staged.write_bytes(b"new-content-v2")

    with pytest.raises((OSError, ValueError)):
        publish_transactionally(
            [(staged, target)],
            run_id="dm_run_new",
            transaction_root=Path("/nonexistent-root-for-rollback-test"),
        )
    assert target.read_bytes() == b"old-content-v1"
    # Validation fails before any rename: staged file is left for the pipeline's
    # cleanup to remove; no pointer was written.
    assert staged.exists()
    assert _read_pointer(target)["run_id"] == "dm_run_old"


def test_publication_of_rejected_run_keeps_pointer_untouched(tmp_path: Path):
    """PUB-001: a REJECTED run leaves no delivery target and no new pointer."""
    from datashell.core.commit import publish_transactionally

    target = tmp_path / "delivered.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    _commit_pointer(
        target,
        {"run_id": "dm_run_old", "tenant_id": "t1"},
    )
    target.write_bytes(b"old-content-v1")

    publish_transactionally([], run_id="dm_run_new", transaction_root=tmp_path)
    assert target.read_bytes() == b"old-content-v1"
    assert _read_pointer(target)["run_id"] == "dm_run_old"


def test_recovery_repairs_stale_generation_pointer(tmp_path: Path, monkeypatch):
    """PUB-001: crash after artifact renames but before the pointer swap leaves
    the pointer stale; startup recovery must rewrite it to the committed
    generation so readers converge on the new files.
    """
    from datashell.core.commit import (
        publish_transactionally,
        recover_publication,
    )

    target = tmp_path / "delivered.parquet"
    lineage = tmp_path / "dm_run_x_lineage_delivered.parquet"
    audit = tmp_path / "dm_run_x_audit_certificate.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    # Old committed generation.
    _commit_pointer(target, {"run_id": "dm_run_old", "tenant_id": "t1"})
    target.write_bytes(b"old-content-v1")

    staged_target = target.with_name(".staged_dm_run_new_delivered.parquet")
    staged_lineage = target.with_name(".staged_dm_run_new_lineage_delivered.parquet")
    staged_audit = audit.with_name(".staged_dm_run_new_dm_run_x_audit_certificate.json")
    staged_target.write_bytes(b"new-content-v2")
    staged_lineage.write_bytes(b"lineage-v2")
    staged_audit.write_text(json.dumps({"status": "ok-v2"}))

    # Fail the pointer swap, i.e. crash after the artifact renames are durable
    # but before the reader-visible commit point is reached.
    def _raise_pointer(pointer: Path, payload: dict) -> None:
        raise OSError("simulated crash before pointer swap")

    monkeypatch.setattr(
        "datashell.core.commit._write_generation_pointer_atomically", _raise_pointer
    )
    publish_transactionally(
        [
            (staged_lineage, lineage),
            (staged_audit, audit),
            (staged_target, target),
        ],
        run_id="dm_run_new",
        transaction_root=tmp_path,
        generation_pointer={"run_id": "dm_run_new", "tenant_id": "t1"},
    )

    # The artifacts are published but the pointer still names the old
    # generation: the crash window between rename and commit point.
    assert target.read_bytes() == b"new-content-v2"
    assert _read_pointer(target)["run_id"] == "dm_run_old"

    # Restart: the pointer write works again. Startup recovery must repair the
    # stale pointer to the committed generation.
    monkeypatch.undo()
    recover_publication(target, transaction_root=tmp_path)
    assert _read_pointer(target)["run_id"] == "dm_run_new"
    assert target.read_bytes() == b"new-content-v2"


def test_resolve_committed_generation_reader_contract(tmp_path: Path):
    """PUB-001: resolve_committed_generation is the reader entry point."""
    from datashell.core.commit import (
        _write_generation_pointer_atomically,
        generation_pointer_path,
        resolve_committed_generation,
    )

    target = tmp_path / "delivered.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)

    # No generation committed yet.
    assert resolve_committed_generation(target) is None

    _write_generation_pointer_atomically(
        generation_pointer_path(target), {"run_id": "dm_run_42", "tenant_id": "t1"}
    )
    assert resolve_committed_generation(target) == "dm_run_42"


def test_api_certificate_endpoint_reports_the_committed_generation(tmp_path: Path):
    """PUB-001: every supported reader applies the same generation contract.

    The HTTP certificate endpoint and the CLI verifier must both answer "is
    this the currently committed generation?", and must agree. A superseded
    certificate is still valid evidence for its own run, so the API must say so
    rather than implying the artifact set is current.
    """
    from fastapi.testclient import TestClient

    from datashell.contract.models import DataContract
    from datashell.core.config import settings
    from datashell.server import app as server_app

    contract = DataContract(
        **json.loads(
            (Path(__file__).resolve().parent.parent / "examples" / "vendor_settlements_contract.json").read_text()
        )
    )
    client = TestClient(server_app.app)
    headers = {"X-API-Key": settings.master_api_key}
    tenant = "enterprise-primary"
    server_app.registry.save_contract(contract, tenant_id=tenant)

    def _feed(name: str, multiplier: float) -> bytes:
        rows = [
            {
                "vendor_id": f"v{i}",
                "amount": float(i + 1) * multiplier,
                "currency": "USD",
                "settlement_id": f"{name}{i:06d}",
            }
            for i in range(200)
        ]
        return ("\n".join(json.dumps(r) for r in rows)).encode("utf-8")

    runs = []
    for index, multiplier in enumerate((1.0, 2.0)):
        resp = client.post(
            "/api/v1/enforce",
            data={"contract_name": "vendor-settlements"},
            files={"file": (f"feed{index}.jsonl", _feed("f", multiplier), "application/octet-stream")},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        runs.append(resp.json()["run_id"])

    first = client.get(f"/api/v1/runs/{runs[0]}/certificate", headers=headers).json()
    second = client.get(f"/api/v1/runs/{runs[1]}/certificate", headers=headers).json()

    # The newest run is the committed generation; the older one is superseded.
    assert second["committed_generation"] == runs[1]
    assert second["is_committed_generation"] is True
    assert first["committed_generation"] == runs[1]
    assert first["is_committed_generation"] is False
    # A superseded certificate is still valid evidence for its own run.
    assert first["verification"]["status"] == "AUTHENTIC_AND_UNMODIFIED"


def test_api_and_cli_agree_on_the_committed_generation(tmp_path: Path):
    """PUB-001: the CLI and the API share one reader contract, not two copies."""
    from datashell.core.commit import committed_generation_status

    certificate = {
        "run_id": "dm_run_cli",
        "cryptographic_manifest": {
            "delivered_file_path": str(tmp_path / "delivered.parquet"),
        },
    }
    from datashell.core.commit import _write_generation_pointer_atomically, generation_pointer_path

    _write_generation_pointer_atomically(
        generation_pointer_path(tmp_path / "delivered.parquet"),
        {"run_id": "dm_run_other", "tenant_id": "t1"},
    )
    shared = committed_generation_status(certificate)

    from datashell.cli.main import _committed_generation_check

    assert _committed_generation_check(certificate) == shared
    assert shared["committed_generation"] == "dm_run_other"
    assert shared["is_committed_generation"] is False


def test_resolve_committed_generation_returns_none_without_a_pointer(tmp_path: Path):
    """PUB-001: a target with no committed generation resolves to None."""
    from datashell.core.commit import resolve_committed_generation

    assert resolve_committed_generation(tmp_path / "never_published.parquet") is None


def test_serial_publication_converges_to_one_generation(tmp_path: Path):
    """PUB-001: repeated publication for the same target ends on one complete
    generation (the fcntl-protected transaction serializes writers)."""
    from datashell.core.commit import publish_transactionally

    target = tmp_path / "delivered.parquet"
    lineage = tmp_path / "dm_run_x_lineage_delivered.parquet"
    audit = tmp_path / "dm_run_x_audit_certificate.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    for run_id, content in [("dm_run_a", b"gen-a"), ("dm_run_b", b"gen-b")]:
        staged_target = target.with_name(f".staged_{run_id}_delivered.parquet")
        staged_lineage = lineage.with_name(f".staged_{run_id}_lineage_delivered.parquet")
        staged_audit = audit.with_name(f".staged_{run_id}_dm_run_x_audit_certificate.json")
        staged_target.write_bytes(content)
        staged_lineage.write_bytes(f"lineage-{run_id}".encode())
        staged_audit.write_text(json.dumps({"status": f"ok-{run_id}"}))
        publish_transactionally(
            [
                (staged_lineage, lineage),
                (staged_audit, audit),
                (staged_target, target),
            ],
            run_id=run_id,
            transaction_root=tmp_path,
            generation_pointer={"run_id": run_id, "tenant_id": "t1"},
        )

    assert _read_pointer(target)["run_id"] == "dm_run_b"
    assert target.read_bytes() == b"gen-b"
    assert json.loads(audit.read_text())["status"] == "ok-dm_run_b"
    assert lineage.read_bytes() == b"lineage-dm_run_b"


def test_cli_verify_reports_committed_generation(tmp_path: Path, monkeypatch):
    """PUB-001: a reader that verifies a certificate learns whether it is the
    currently committed generation (the generation pointer is respected)."""
    import subprocess
    import sys

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("DATASHELL_HMAC_SECRET", "DATASHELL_HMAC_KEYRING")
    }
    env.update({"ENVIRONMENT": "test", "DATASHELL_STORAGE_ROOT": str(tmp_path)})
    repo = Path(__file__).resolve().parent.parent

    def _run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "datashell.cli.main", *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=repo,
            timeout=180,
        )

    target = tmp_path / "curated" / "s.parquet"
    audit1 = tmp_path / "audit1" / "cert.json"
    audit2 = tmp_path / "audit2" / "cert.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    audit1.parent.mkdir(parents=True, exist_ok=True)
    audit2.parent.mkdir(parents=True, exist_ok=True)

    for audit in (audit1, audit2):
        proc = _run(
            "run",
            "--contract", "examples/vendor_settlements_contract.json",
            "--source", "examples/samples/vendor_settlements_1k.jsonl",
            "--target", str(target),
            "--audit", str(audit),
        )
        assert proc.returncode == 0, proc.stderr[-2000:]

    # The first certificate is cryptographically valid but superseded.
    first = _run("verify", str(audit1))
    assert first.returncode == 0, first.stdout[-2000:]
    assert "this certificate: no" in first.stdout, first.stdout

    # The second certificate is the committed generation.
    second = _run("verify", str(audit2))
    assert second.returncode == 0, second.stdout[-2000:]
    assert "this certificate: yes" in second.stdout, second.stdout