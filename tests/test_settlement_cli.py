"""CLI tests for commercial contracts and the settlement lifecycle.

Runs the real argparse dispatch in-process against isolated storage. Verifies
machine-readable output, exit codes (including the dedicated refusal code 4),
idempotent replays, and that no command moves money.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from datashell.cli import main as cli
from datashell.contract.models import DataContract
from datashell.core.config import settings
from datashell.pipeline.runner import DataShellPipeline

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_CONTRACT_PATH = REPO_ROOT / "examples" / "vendor_settlements_contract.json"
EXAMPLE_COMMERCIAL = REPO_ROOT / "examples" / "commercial_delivery_contract.json"
ALPHA = "tenant-alpha"


def _run(argv):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    return excinfo.value.code


def _out_json(capsys):
    return json.loads(capsys.readouterr().out)


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    for sub in ("output", "output/audits", "output/quarantine", "contracts_registry", "data"):
        (storage / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "output_dir", storage / "output")
    monkeypatch.setattr(settings, "contracts_dir", storage / "contracts_registry")
    monkeypatch.setattr(settings, "data_dir", storage / "data")
    monkeypatch.setattr(settings, "audit_dir", storage / "output" / "audits")
    monkeypatch.setattr(settings, "quarantine_dir", storage / "output" / "quarantine")
    monkeypatch.setattr(settings, "billing_provider", "dry-run")
    return storage


def _records(delivered: int = 5, held: int = 3, refused: int = 2):
    rows = [{"settlement_id": f"S-{i}", "amount": 100.0 + i, "currency": "USD"}
            for i in range(delivered)]
    rows += [{"settlement_id": f"H-{i}", "amount": -1.0, "currency": "USD"}
             for i in range(held)]
    rows += [{"settlement_id": f"R-{i}", "amount": "bad", "currency": "USD"}
             for i in range(refused)]
    return rows


def _pipeline_run(storage: Path, records, *, tenant: str = ALPHA):
    tag = uuid.uuid4().hex[:8]
    source = storage / f"feed_{tag}.jsonl"
    source.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    target = storage / "output" / f"delivered_{tag}.parquet"
    audit = storage / "output" / "audits" / f"audit_{tag}.json"
    contract = DataContract(**json.loads(DATA_CONTRACT_PATH.read_text()))
    pipeline = DataShellPipeline(contract, tenant_id=tenant, drift_enabled=False)
    result = pipeline.run_file(source_path=str(source),
                               target_path_override=str(target),
                               audit_output_path=str(audit))
    return result.run_id


def _contract_file(tmp_path: Path, contract_id: str = "ctr_cli") -> Path:
    payload = json.loads(EXAMPLE_COMMERCIAL.read_text())
    payload["contract_id"] = contract_id
    payload["acceptance"]["expected_units"] = 5
    payload["acceptance"]["minimum_accepted_units"] = 1
    path = tmp_path / "commercial.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestContractCommands:
    def test_validate_accepts_example_and_rejects_invalid(self, cli_env, tmp_path, capsys):
        code = _run(["contract", "validate", str(EXAMPLE_COMMERCIAL)])
        assert code == 0
        out = capsys.readouterr().out
        assert "is valid" in out and "Digest:" in out

        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"contract_id": "x"}), encoding="utf-8")
        code = _run(["contract", "validate", str(bad)])
        assert code == 1
        assert "invalid" in capsys.readouterr().err

    def test_create_show_versions_roundtrip(self, cli_env, tmp_path, capsys):
        path = _contract_file(tmp_path)
        code = _run(["contract", "create", str(path), "--tenant", ALPHA, "--json"])
        assert code == 0
        created = _out_json(capsys)
        assert created["created"] is True

        # Idempotent re-registration: same digest, no new version.
        code = _run(["contract", "create", str(path), "--tenant", ALPHA, "--json"])
        replay = _out_json(capsys)
        assert replay["created"] is False
        assert replay["contract_digest"] == created["contract_digest"]

        code = _run(["contract", "show", "ctr_cli", "--tenant", ALPHA, "--json"])
        assert code == 0
        shown = _out_json(capsys)
        assert shown["contract_digest"] == created["contract_digest"]

        code = _run(["contract", "versions", "ctr_cli", "--tenant", ALPHA, "--json"])
        assert code == 0
        assert len(_out_json(capsys)["versions"]) == 1

        # A different payload for the same version is refused.
        payload = json.loads(path.read_text())
        payload["pricing"]["unit_price_minor"] = 9
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert _run(["contract", "create", str(path), "--tenant", ALPHA]) == 1
        assert "immutable" in capsys.readouterr().err

        # Cross-tenant lookups are empty.
        assert _run(["contract", "show", "ctr_cli", "--tenant", "other"]) == 1


class TestSettlementCommands:
    def _settle(self, cli_env, tmp_path, capsys, records=None):
        run_id = _pipeline_run(cli_env, records or _records())
        _run(["contract", "create", str(_contract_file(tmp_path)),
              "--tenant", ALPHA, "--json"])
        capsys.readouterr()
        code = _run(["settlement", "calculate", "--run-id", run_id,
                     "--contract-id", "ctr_cli", "--tenant", ALPHA, "--json"])
        assert code == 0
        return run_id, _out_json(capsys)

    def test_full_lifecycle(self, cli_env, tmp_path, capsys):
        run_id, settled = self._settle(cli_env, tmp_path, capsys)
        assert settled["created"] is True
        assert settled["calculation"]["gross_amount_minor"] == 25
        sid = settled["settlement_id"]

        # Idempotent replay via the CLI's default key.
        code = _run(["settlement", "calculate", "--run-id", run_id,
                     "--contract-id", "ctr_cli", "--tenant", ALPHA, "--json"])
        assert code == 0 and _out_json(capsys)["created"] is False

        for argv in (["settlement", "show", sid, "--tenant", ALPHA, "--json"],
                     ["settlement", "list", "--tenant", ALPHA, "--json"],
                     ["settlement", "explain", sid, "--tenant", ALPHA, "--json"]):
            assert _run(argv) == 0
            capsys.readouterr()

        assert _run(["settlement", "verify", sid, "--tenant", ALPHA, "--json"]) == 0
        report = _out_json(capsys)
        assert report["status"] == "AUTHENTIC_AND_UNMODIFIED"
        assert report["third_party_verifiable"] is False  # HMAC never mislabelled

        # Draft invoice preview/export: explicitly not a paid invoice.
        assert _run(["settlement", "invoice-preview", sid, "--tenant", ALPHA, "--json"]) == 0
        preview = _out_json(capsys)
        assert preview["document_kind"] == "DRAFT_INVOICE_PREVIEW"
        assert preview["amount_minor"] == 25
        assert preview["eligible_for_billing"] is True

        export = tmp_path / "draft_invoice.json"
        assert _run(["settlement", "invoice-export", sid, "--output", str(export),
                     "--tenant", ALPHA]) == 0
        exported = json.loads(export.read_text())
        assert exported["amount"] == "0.25" and "not a paid invoice" in exported["status"]
        capsys.readouterr()

        # Dry-run billing: creates a draft instruction, replay deduplicates.
        assert _run(["settlement", "billing-preview", sid, "--tenant", ALPHA, "--json"]) == 0
        first = _out_json(capsys)
        assert first["ok"] is True and first["status"] == "PROVIDER_INVOICE_CREATED"
        assert _run(["settlement", "billing-preview", sid, "--tenant", ALPHA, "--json"]) == 0
        replay = _out_json(capsys)
        assert replay["billing_instruction_id"] == first["billing_instruction_id"]

        # Dispute: the CLI blocks billing and resolves back to billable.
        assert _run(["settlement", "dispute-open", sid, "--party", "buyer-x",
                     "--reason", "quantity", "--tenant", ALPHA, "--json"]) == 0
        dispute = _out_json(capsys)
        assert dispute["status"] == "OPEN"
        billing_after_dispute = _run(["settlement", "billing-preview", sid,
                                      "--tenant", ALPHA, "--idempotency-key", "post-dispute"])
        assert billing_after_dispute == 4  # EXIT_CALCULATION_REFUSED

        assert _run(["settlement", "dispute-resolve", dispute["dispute_id"],
                     "--resolution", "ACCEPT_AS_IS", "--reason", "confirmed",
                     "--actor", "reviewer", "--tenant", ALPHA, "--json"]) == 0
        assert _out_json(capsys)["resolution"] == "ACCEPT_AS_IS"
        assert _run(["settlement", "show", sid, "--tenant", ALPHA, "--json"]) == 0
        assert _out_json(capsys)["settlement_status"] == "READY_FOR_BILLING"

    def test_unknown_run_exits_4(self, cli_env, tmp_path, capsys):
        _run(["contract", "create", str(_contract_file(tmp_path)), "--tenant", ALPHA])
        capsys.readouterr()
        code = _run(["settlement", "calculate", "--run-id", "dm_run_missing",
                     "--contract-id", "ctr_cli", "--tenant", ALPHA])
        assert code == 4
        assert "refused" in capsys.readouterr().err

    def test_billing_preview_on_non_billable_exits_4(self, cli_env, tmp_path, capsys):
        # Two rows -> the MIN_ROW_COUNT batch rule rejects the run.
        run_id, settled = self._settle(
            cli_env, tmp_path, capsys, records=_records(delivered=2, held=0, refused=0))
        assert settled["settlement_status"] != "READY_FOR_BILLING"
        code = _run(["settlement", "billing-preview", settled["settlement_id"],
                     "--tenant", ALPHA])
        assert code == 4
        assert "not eligible" in capsys.readouterr().err

    def test_cross_tenant_calculation_exits_4(self, cli_env, tmp_path, capsys):
        run_id = _pipeline_run(cli_env, _records(), tenant=ALPHA)
        _run(["contract", "create", str(_contract_file(tmp_path)), "--tenant", ALPHA])
        capsys.readouterr()
        code = _run(["settlement", "calculate", "--run-id", run_id,
                     "--contract-id", "ctr_cli", "--tenant", "tenant-beta"])
        assert code == 4


class TestSettlementDemoScript:
    def test_demo_script_passes_with_a_small_run(self, tmp_path):
        import subprocess
        import sys as _sys

        script = REPO_ROOT / "examples" / "settlement_demo.py"
        storage = tmp_path / "demo_storage"
        proc = subprocess.run(
            [_sys.executable, str(script), "--records", "2000",
             "--storage", str(storage)],
            capture_output=True, text=True, timeout=600,
            cwd=str(REPO_ROOT))
        assert proc.returncode == 0, proc.stdout + proc.stderr

        report = json.loads((storage / "settlement_demo_report.json").read_text())
        assert report["passed"] is True
        assert report["splits"] == {"refused": 20, "held": 10, "delivered": 1970}
        assert report["qualified_units"] == 1970
        assert report["gross_amount_minor"] == 1970 * 5
        assert report["gross_amount"] == "98.50"
        assert report["verification"]["status"] == "AUTHENTIC_AND_UNMODIFIED"
        assert set(report["scenarios"]) >= {
            "A_incomplete_feed", "B_invalid_certificate", "C_reconciliation_mismatch",
            "D_contract_version", "E_idempotency", "F_tenant_isolation",
            "G_provider_timeout", "H_dispute"}
