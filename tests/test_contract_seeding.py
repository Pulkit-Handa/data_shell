"""Regression tests for default contract auto-seeding on startup.

This guards the CI production-container check (`.github/workflows/ci.yml`,
`container-build` job): the bundled ``examples/`` directory ships both data
contracts (``apiVersion``/``kind``/``metadata``/``spec``) *and* settlement
commercial contracts (``*_contract.json`` sharing the same suffix but a
different schema). Auto-seeding must only parse the former and must never
abort production startup because of the latter.
"""

from types import SimpleNamespace

import pytest

import datashell.server.app as server_app
from datashell.contract.registry import ContractRegistry
from datashell.settlement.registry import CommercialContractRegistry


@pytest.fixture
def production_settings(monkeypatch):
    """A settings stub that reports production mode for the seeding path."""
    stub = SimpleNamespace(is_production_mode=True)
    monkeypatch.setattr(server_app, "settings", stub)
    return stub


def test_seed_skips_settlement_contracts_in_production(tmp_path, production_settings):
    """Seeding the real examples dir must not raise in production mode."""
    reg = ContractRegistry(tmp_path / "contracts")

    # Must not raise RuntimeError despite examples/ containing
    # commercial_delivery_contract.json (a settlement contract, not a
    # DataContract). Before the fix this aborted startup with a pydantic
    # ValidationError re-raised as a RuntimeError.
    server_app.seed_default_contracts(reg)


def test_seed_registers_only_data_contracts(tmp_path, production_settings):
    """Only envelope-shaped data contracts are seeded; settlement ones are skipped."""
    reg = ContractRegistry(tmp_path / "contracts")
    server_app.seed_default_contracts(reg)

    # The two bundled data-contract examples are seeded as shared defaults...
    assert reg.get_latest("nyc-tlc-yellow-tripdata") is not None
    assert reg.get_latest("vendor-settlements") is not None
    # ...but the settlement commercial contract is not, even though its
    # filename matches the *_contract.json glob used for discovery. Its
    # contract_id must not appear as a seeded shared data contract.
    assert reg.get_latest("ctr_vendor_settlements_usd") is None


def test_seed_is_idempotent_across_settlement_contract(tmp_path, production_settings):
    """Running seeding twice stays clean (no crash on the settlement file)."""
    reg = ContractRegistry(tmp_path / "contracts")
    server_app.seed_default_contracts(reg)
    server_app.seed_default_contracts(reg)  # second run must also not raise


def test_settlement_contract_is_still_loadable_by_registry(tmp_path):
    """The skipped settlement contract remains loadable via its own registry."""
    import json
    from pathlib import Path

    from datashell.settlement.models import CommercialContract

    example = Path(__file__).resolve().parent.parent / "examples" / "commercial_delivery_contract.json"
    payload = json.loads(example.read_text(encoding="utf-8"))
    contract = CommercialContract(**payload)
    registry = CommercialContractRegistry(tmp_path / "commercial")
    result = registry.register(
        contract, tenant_id="tenant-alpha", created_by="regression-test"
    )
    assert result["contract_id"] == contract.contract_id
    loaded = registry.get_latest(contract.contract_id, "tenant-alpha")
    assert loaded is not None
