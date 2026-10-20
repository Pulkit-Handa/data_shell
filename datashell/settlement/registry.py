"""File-based registry for versioned commercial delivery contracts.

Follows the immutability and tenant-isolation semantics of the data-contract
registry: each (contract_id, version) is written once under
``contracts_registry/commercial/tenants/<tenant>/<contract_id>/`` with
``latest.json`` tracking the highest version. Re-saving an identical payload
is idempotent; a different payload for the same version is rejected, so terms
already referenced by a settlement can never be rewritten.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from datashell.core.config import settings
from datashell.settlement.ledger import SettlementLedger
from datashell.settlement.models import CommercialContract

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CommercialContractRegistryError(RuntimeError):
    pass


class CommercialContractRegistry:
    """Tenant-isolated, immutable commercial contract version store."""

    def __init__(self, storage_dir: Path | None = None, ledger: SettlementLedger | None = None):
        self.storage_dir = Path(storage_dir or (settings.contracts_dir / "commercial")).resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = self.storage_dir / ".registry.lock"
        self.ledger = ledger or SettlementLedger()

    def _validate_identifier(self, identifier: str) -> str:
        if not _SAFE_ID.match(identifier):
            raise CommercialContractRegistryError(
                f"Invalid identifier or path traversal detected: '{identifier}'")
        return identifier

    def _contract_dir(self, contract_id: str, tenant_id: str) -> Path:
        clean_id = self._validate_identifier(contract_id)
        clean_tenant = settings.tenant_storage_key(tenant_id)
        return self.storage_dir / "tenants" / clean_tenant / clean_id

    def register(self, contract: CommercialContract, *, tenant_id: str, created_by: str) -> dict[str, Any]:
        """Validate and record an immutable contract version with its digest."""
        if not isinstance(contract, CommercialContract):
            raise CommercialContractRegistryError("a CommercialContract instance is required")
        settings.tenant_storage_key(tenant_id)  # validate tenant identity

        contract_dir = self._contract_dir(contract.contract_id, tenant_id)
        payload = contract.canonical_payload()
        digest = contract.compute_digest()

        lock_fd = os.open(str(self._lock_file), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            contract_dir.mkdir(parents=True, exist_ok=True)

            existing_versions = sorted(contract_dir.glob("v*.json"))
            for existing in existing_versions:
                with open(existing, encoding="utf-8") as handle:
                    prior = CommercialContract(**json.load(handle))
                if prior.contract_version == contract.contract_version:
                    if prior.canonical_payload() == payload:
                        # Idempotent re-registration of the identical version.
                        self._ensure_latest(contract_dir, payload)
                        self.ledger.record_contract_version(contract, tenant_id, created_by)
                        return {"status": "registered", "contract_id": contract.contract_id,
                                "contract_version": contract.contract_version,
                                "contract_digest": digest, "created": False}
                    raise CommercialContractRegistryError(
                        f"Commercial contract '{contract.contract_id}' v{contract.contract_version} "
                        "already exists and is immutable; create a new version"
                    )

            now_iso = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            filename = f"v{contract.contract_version}__{now_iso}__{uuid.uuid4().hex[:6]}.json"
            dest = contract_dir / filename
            tmp_dest = contract_dir / f".tmp_{filename}"
            with open(tmp_dest, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp_dest, dest)

            self._ensure_latest(contract_dir, payload)
            # Record in the durable ledger as well so settlements can bind to
            # the exact digest with database-backed uniqueness.
            self.ledger.record_contract_version(contract, tenant_id, created_by)
            return {"status": "registered", "contract_id": contract.contract_id,
                    "contract_version": contract.contract_version,
                    "contract_digest": digest, "created": True,
                    "path": str(dest)}
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    @staticmethod
    def _ensure_latest(contract_dir: Path, payload: dict[str, Any]) -> None:
        latest_file = contract_dir / "latest.json"
        tmp_latest = contract_dir / f".tmp_latest_{uuid.uuid4().hex[:6]}.json"
        with open(tmp_latest, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp_latest, latest_file)

    def get_version(self, contract_id: str, contract_version: int, tenant_id: str) -> CommercialContract | None:
        contract_dir = self._contract_dir(contract_id, tenant_id)
        matches = sorted(contract_dir.glob(f"v{contract_version}__*.json"))
        if not matches:
            return None
        with open(matches[-1], encoding="utf-8") as handle:
            return CommercialContract(**json.load(handle))

    def get_latest(self, contract_id: str, tenant_id: str) -> CommercialContract | None:
        contract_dir = self._contract_dir(contract_id, tenant_id)
        latest_file = contract_dir / "latest.json"
        if not latest_file.exists():
            return None
        with open(latest_file, encoding="utf-8") as handle:
            return CommercialContract(**json.load(handle))

    def list_versions(self, contract_id: str, tenant_id: str) -> list[dict[str, Any]]:
        contract_dir = self._contract_dir(contract_id, tenant_id)
        if not contract_dir.exists():
            return []
        versions = []
        for path in sorted(contract_dir.glob("v*.json")):
            with open(path, encoding="utf-8") as handle:
                contract = CommercialContract(**json.load(handle))
            versions.append({
                "contract_id": contract.contract_id,
                "contract_version": contract.contract_version,
                "contract_digest": contract.compute_digest(),
                "created_at": contract.created_at,
                "path": str(path),
            })
        return versions

    def resolve_version(self, contract_id: str, tenant_id: str,
                        version: int | None) -> CommercialContract | None:
        """Resolve the exact contract version a settlement must bind to.

        ``None`` resolves to the latest version — but every issued settlement
        records the concrete version and digest it used, so later contract
        versions never rewrite historical outcomes.
        """
        if version is None:
            return self.get_latest(contract_id, tenant_id)
        return self.get_version(contract_id, version, tenant_id)
