"""Regression tests for v6 audit remediation: tenant collisions and multi-file commits."""
from pathlib import Path

import pytest

from datashell.core.commit import publish_transactionally, recover_publication
from datashell.core.config import EnterpriseSettings


def test_tenant_path_encoding_is_collision_free_and_keeps_legacy_ids_readable(tmp_path):
    settings = EnterpriseSettings(output_dir=tmp_path / "output", quarantine_dir=tmp_path / "quarantine")
    path_a = settings.get_tenant_delivery_dir("team/a")
    path_b = settings.get_tenant_delivery_dir("teama")
    assert path_a != path_b
    assert path_b.name == "teama"
    assert path_a.name.startswith("~")
    assert settings.get_tenant_delivery_dir("Teama") != settings.get_tenant_delivery_dir("teama")
    assert settings.get_tenant_quarantine_dir("team/a") != settings.get_tenant_quarantine_dir("teama")


def test_tenant_path_encoding_rejects_blank_and_surrounding_whitespace():
    for tenant_id in ("", " tenant", "tenant "):
        with pytest.raises(ValueError):
            EnterpriseSettings.tenant_storage_key(tenant_id)


def test_transactional_publish_commits_both_artifacts(tmp_path):
    staged_lineage = tmp_path / ".staged-lineage"
    staged_target = tmp_path / ".staged-target"
    final_lineage = tmp_path / "run_lineage.parquet"
    final_target = tmp_path / "active.parquet"
    staged_lineage.write_bytes(b"new dataset")
    staged_target.write_bytes(b"new dataset")

    publish_transactionally(
        [(staged_lineage, final_lineage), (staged_target, final_target)], run_id="test-run"
    )
    assert final_lineage.read_bytes() == b"new dataset"
    assert final_target.read_bytes() == b"new dataset"
    assert not staged_lineage.exists()
    assert not staged_target.exists()
    recover_publication(final_target)
    assert final_target.read_bytes() == final_lineage.read_bytes()


def test_transactional_publish_rolls_back_on_mid_commit_failure(tmp_path, monkeypatch):
    import datashell.core.commit as commit_module

    staged_a, staged_b = tmp_path / ".stage-a", tmp_path / ".stage-b"
    final_a, final_b = tmp_path / "a.dat", tmp_path / "b.dat"
    staged_a.write_bytes(b"new-a")
    staged_b.write_bytes(b"new-b")
    final_a.write_bytes(b"old-a")
    final_b.write_bytes(b"old-b")

    real_replace = commit_module.os.replace
    failed = {"once": False}

    def fail_once(src, dst):
        if Path(src) == staged_b and Path(dst) == final_b and not failed["once"]:
            failed["once"] = True
            raise OSError("simulated disk error during second publication")
        return real_replace(src, dst)

    monkeypatch.setattr(commit_module.os, "replace", fail_once)
    with pytest.raises(OSError, match="simulated disk error"):
        publish_transactionally([(staged_a, final_a), (staged_b, final_b)], run_id="rollback-run")

    assert final_a.read_bytes() == b"old-a"
    assert final_b.read_bytes() == b"old-b"
    assert not list(tmp_path.glob("*.backup"))
    assert not list(tmp_path.glob(".dm-commit-*.json"))


def test_contract_registry_uses_encoded_tenant_namespace(tmp_path):
    import json
    from datashell.contract.models import DataContract
    from datashell.contract.registry import ContractRegistry

    example = Path(__file__).parents[1] / "examples" / "vendor_settlements_contract.json"
    contract = DataContract(**json.loads(example.read_text(encoding="utf-8")))
    registry = ContractRegistry(tmp_path / "contracts")
    registry.save_contract(contract)  # shared seed/default contract
    tenant_path = registry.save_contract(contract, tenant_id="team/a")

    assert tenant_path.is_file()
    assert "tenants" in tenant_path.parts
    assert "~7465616d2f61" in tenant_path.parts
    assert (tmp_path / "contracts" / contract.metadata.name / "latest.json").is_file()


def test_unmarked_global_contract_is_not_visible_to_tenants(tmp_path):
    import json
    from datashell.contract.models import DataContract
    from datashell.contract.registry import ContractRegistry

    example = Path(__file__).parents[1] / "examples" / "vendor_settlements_contract.json"
    contract = DataContract(**json.loads(example.read_text(encoding="utf-8")))
    registry = ContractRegistry(tmp_path / "contracts")
    global_dir = registry.storage_dir / contract.metadata.name
    global_dir.mkdir(parents=True)
    payload = contract.model_dump(by_alias=True)
    (global_dir / "latest.json").write_text(json.dumps(payload), encoding="utf-8")
    versioned = global_dir / f"{contract.metadata.name}__v{contract.metadata.version}__old.json"
    versioned.write_text(json.dumps(payload), encoding="utf-8")

    assert registry.get_latest(contract.metadata.name, tenant_id="tenant-alpha") is None
    assert registry.get_version(contract.metadata.name, contract.metadata.version, tenant_id="tenant-alpha") is None
    registry.mark_shared_contract(contract.metadata.name)
    assert registry.get_latest(contract.metadata.name, tenant_id="tenant-alpha") is not None


def test_production_cors_rejects_wildcard_and_empty_allowlist(monkeypatch):
    import secrets
    monkeypatch.setenv("DATASHELL_API_KEY", secrets.token_hex(32))
    monkeypatch.setenv("DATASHELL_HMAC_SECRET", secrets.token_hex(32))
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        EnterpriseSettings(environment="production", allowed_origins=["*"])
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        EnterpriseSettings(environment="production", allowed_origins=[""])


def test_production_cors_accepts_explicit_allowlist(monkeypatch):
    import secrets
    monkeypatch.setenv("DATASHELL_API_KEY", secrets.token_hex(32))
    monkeypatch.setenv("DATASHELL_HMAC_SECRET", secrets.token_hex(32))
    cfg = EnterpriseSettings(
        environment="production",
        allowed_origins=["https://console.example.com", "https://ops.example.com"],
    )
    assert cfg.allowed_origins == ["https://console.example.com", "https://ops.example.com"]


def test_recovery_rejects_corrupt_journal_paths_outside_transaction_directory(tmp_path):
    import json
    from datashell.core.commit import _transaction_paths

    target = tmp_path / "active.parquet"
    journal_path, _ = _transaction_paths(target)
    outside = tmp_path.parent / "do-not-touch.txt"
    outside.write_text("keep", encoding="utf-8")
    journal_path.write_text(json.dumps({
        "version": 1,
        "run_id": "bad-journal",
        "state": "prepared",
        "entries": [{
            "staged": str(tmp_path / ".staged"),
            "final": str(outside),
            "backup": str(tmp_path / ".backup"),
            "had_original": True,
        }],
    }), encoding="utf-8")

    with pytest.raises(RuntimeError, match="outside transaction directory"):
        recover_publication(target)
    assert outside.read_text(encoding="utf-8") == "keep"


def test_transactional_publish_can_commit_delivery_and_certificate_across_subdirectories(tmp_path):
    output_dir = tmp_path / "output"
    tenant_dir = output_dir / "tenants" / "tenant-alpha"
    audit_dir = output_dir / "audits"
    tenant_dir.mkdir(parents=True)
    audit_dir.mkdir(parents=True)

    staged_target = tenant_dir / ".staged-active.parquet"
    staged_lineage = tenant_dir / ".staged-run-lineage.parquet"
    staged_certificate = audit_dir / ".staged-run-certificate.json"
    final_target = tenant_dir / "active.parquet"
    final_lineage = tenant_dir / "run-lineage.parquet"
    final_certificate = audit_dir / "run-certificate.json"

    staged_target.write_bytes(b"target-v2")
    staged_lineage.write_bytes(b"lineage-v2")
    staged_certificate.write_bytes(b"certificate-v2")

    publish_transactionally(
        [
            (staged_lineage, final_lineage),
            (staged_certificate, final_certificate),
            (staged_target, final_target),
        ],
        run_id="cross-dir-test",
        transaction_root=output_dir,
    )
    recover_publication(final_target, transaction_root=output_dir)

    assert final_target.read_bytes() == b"target-v2"
    assert final_lineage.read_bytes() == b"lineage-v2"
    assert final_certificate.read_bytes() == b"certificate-v2"
    assert not list(output_dir.rglob("*.backup"))


def test_cross_directory_recovery_rejects_modified_journal_mac(tmp_path):
    import json
    from datashell.core.commit import _journal_integrity_tag, _transaction_paths

    root = tmp_path / "output"
    tenant_dir = root / "tenants" / "tenant-alpha"
    audit_dir = root / "audits"
    tenant_dir.mkdir(parents=True)
    audit_dir.mkdir(parents=True)
    target = tenant_dir / "active.parquet"
    protected = audit_dir / "protected-certificate.json"
    protected.write_text("preserve", encoding="utf-8")
    journal_path, _ = _transaction_paths(target, root)
    journal = {
        "version": 1,
        "run_id": "tamper-test",
        "state": "prepared",
        "entries": [{
            "staged": str(tenant_dir / ".staged-active"),
            "final": str(target),
            "backup": str(tenant_dir / ".active.backup"),
            "had_original": False,
        }],
    }
    journal["journal_mac"] = _journal_integrity_tag(journal)
    # A filesystem actor modifies the journal to target another tenant artifact.
    journal["entries"][0]["final"] = str(protected)
    journal["entries"][0]["staged"] = str(audit_dir / ".staged-protected")
    journal["entries"][0]["backup"] = str(audit_dir / ".protected.backup")
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(RuntimeError, match="integrity verification failed"):
        recover_publication(target, transaction_root=root)
    assert protected.read_text(encoding="utf-8") == "preserve"


def test_cross_directory_transaction_rolls_back_all_artifacts_on_partial_publish_failure(tmp_path, monkeypatch):
    import datashell.core.commit as commit_module

    root = tmp_path / "output"
    tenant_dir = root / "tenants" / "tenant-alpha"
    audit_dir = root / "audits"
    tenant_dir.mkdir(parents=True)
    audit_dir.mkdir(parents=True)
    staged_lineage = tenant_dir / ".staged-lineage"
    staged_target = tenant_dir / ".staged-target"
    staged_certificate = audit_dir / ".staged-certificate"
    final_lineage = tenant_dir / "lineage.parquet"
    final_target = tenant_dir / "active.parquet"
    final_certificate = audit_dir / "certificate.json"
    for path, payload in (
        (staged_lineage, b"new-lineage"),
        (staged_target, b"new-target"),
        (staged_certificate, b"new-certificate"),
        (final_lineage, b"old-lineage"),
        (final_target, b"old-target"),
        (final_certificate, b"old-certificate"),
    ):
        path.write_bytes(payload)

    real_replace = commit_module.os.replace
    failed = {"value": False}

    def fail_on_target_once(src, dst):
        if Path(src) == staged_target and Path(dst) == final_target and not failed["value"]:
            failed["value"] = True
            raise OSError("injected target publication failure")
        return real_replace(src, dst)

    monkeypatch.setattr(commit_module.os, "replace", fail_on_target_once)
    with pytest.raises(OSError, match="injected target publication failure"):
        publish_transactionally(
            [(staged_lineage, final_lineage), (staged_certificate, final_certificate), (staged_target, final_target)],
            run_id="cross-dir-rollback",
            transaction_root=root,
        )

    assert final_lineage.read_bytes() == b"old-lineage"
    assert final_target.read_bytes() == b"old-target"
    assert final_certificate.read_bytes() == b"old-certificate"
    assert not list(root.rglob("*.backup"))
    assert not list(root.glob(".dm-commit-*.json"))


def test_startup_recovery_rolls_back_interrupted_cross_directory_transaction(tmp_path):
    import json
    from datashell.core.commit import _journal_integrity_tag, _transaction_paths, recover_pending_publications

    root = tmp_path / "output"
    tenant_dir = root / "tenants" / "tenant-alpha"
    audit_dir = root / "audits"
    tenant_dir.mkdir(parents=True)
    audit_dir.mkdir(parents=True)
    target = tenant_dir / "active.parquet"
    staged_target = tenant_dir / ".staged-active.parquet"
    final_certificate = audit_dir / "certificate.json"
    staged_certificate = audit_dir / ".staged-certificate.json"
    staged_target.write_bytes(b"new-target-not-published")
    staged_certificate.write_bytes(b"new-certificate-not-published")
    final_certificate.write_bytes(b"old-certificate")
    journal_path, _ = _transaction_paths(target, root)
    journal = {
        "version": 1,
        "run_id": "simulated-crash",
        "state": "prepared",
        "entries": [
            {"staged": str(staged_certificate), "final": str(final_certificate), "backup": str(audit_dir / ".certificate.backup"), "had_original": True},
            {"staged": str(staged_target), "final": str(target), "backup": str(tenant_dir / ".active.backup"), "had_original": False},
        ],
    }
    journal["journal_mac"] = _journal_integrity_tag(journal)
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    assert recover_pending_publications(root) == 1
    assert not target.exists()
    assert final_certificate.read_bytes() == b"old-certificate"
    assert not staged_target.exists()
    assert not staged_certificate.exists()
    assert not journal_path.exists()


def test_tenant_storage_rejects_symlinked_namespace_roots(tmp_path):
    settings = EnterpriseSettings(output_dir=tmp_path / "output", quarantine_dir=tmp_path / "quarantine")
    outside = tmp_path / "outside"
    outside.mkdir()
    settings.output_dir.mkdir(parents=True)
    settings.quarantine_dir.mkdir(parents=True)
    (settings.output_dir / "tenants").symlink_to(outside, target_is_directory=True)
    (settings.quarantine_dir / "tenants").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="cannot be a symlink"):
        settings.get_tenant_delivery_dir("tenant-a")
    with pytest.raises(RuntimeError, match="cannot be a symlink"):
        settings.get_tenant_quarantine_dir("tenant-a")
