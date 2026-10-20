"""Enterprise Contract Registry: Concurrency-safe version management, semver activation, and tenant isolation."""

from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from datashell.contract.models import DataContract
from datashell.core.config import settings


class ContractRegistry:
    """
    Manages immutable, tamper-evident contract version history with inter-process locking,
    semantic version ordering for active contracts, and multi-tenant catalog scoping.
    """

    def __init__(self, storage_dir: Path | None = None):
        self.storage_dir = (storage_dir or Path("./contracts_registry")).resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = self.storage_dir / ".registry.lock"
        self._shared_file = self.storage_dir / ".shared_contracts.json"

    def _validate_identifier(self, identifier: str) -> str:
        """Prevent path traversal and invalid characters in identifiers."""
        if not re.match(r"^[a-zA-Z0-9_\-\.]+$", identifier) or ".." in identifier:
            raise ValueError(f"Invalid identifier or path traversal detected: '{identifier}'")
        return identifier

    @staticmethod
    def _parse_semver(version_str: str) -> tuple[int, ...]:
        """Parses version strings into numerical tuples for deterministic semantic ordering."""
        nums = [int(n) for n in re.findall(r"\d+", version_str)]
        return tuple(nums) if nums else (0,)

    def _shared_contract_names(self) -> set[str]:
        if not self._shared_file.exists():
            return set()
        try:
            with open(self._shared_file, encoding="utf-8") as handle:
                payload = json.load(handle)
            names = payload.get("names", [])
            if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                raise ValueError("invalid shared contract manifest schema")
            return set(names)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Cannot safely read shared contract manifest {self._shared_file}: {exc}") from exc

    def _mark_shared_contract_unlocked(self, name: str) -> None:
        names = self._shared_contract_names()
        names.add(self._validate_identifier(name))
        temp = self._shared_file.with_name(f".tmp_{self._shared_file.name}_{uuid.uuid4().hex}")
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "names": sorted(names)}, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self._shared_file)

    def mark_shared_contract(self, name: str) -> None:
        """Explicitly expose a global contract as a read-only shared default."""
        clean_name = self._validate_identifier(name)
        lock_fd = os.open(str(self._lock_file), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if not (self.storage_dir / clean_name / "latest.json").exists():
                raise ValueError(f"Cannot mark missing global contract '{clean_name}' as shared")
            self._mark_shared_contract_unlocked(clean_name)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _get_target_dir(self, name: str, tenant_id: str | None = None, for_write: bool = False) -> Path:
        """Resolve a tenant-owned path or the internal shared catalog path."""
        clean_name = self._validate_identifier(name)
        if tenant_id is not None:
            # Tenant IDs are opaque identity values, not path fragments. Use the
            # same collision-free namespace encoding as delivery/quarantine roots.
            # Only internal calls with tenant_id=None may write the shared catalog.
            clean_tenant = settings.tenant_storage_key(tenant_id)
            tenant_dir = self.storage_dir / "tenants" / clean_tenant / clean_name
            return tenant_dir
        # Global catalog is reserved for explicitly seeded/shared contracts.
        return self.storage_dir / clean_name

    def save_contract(self, contract: DataContract, tenant_id: str | None = None) -> Path:
        """
        Stores a contract version immutably using atomic staging and inter-process locking.
        Deterministic activation ensures 'latest' points to highest semantic version.
        """
        name = self._validate_identifier(contract.metadata.name)
        ver = self._validate_identifier(contract.metadata.version)
        contract_dir = self._get_target_dir(name, tenant_id=tenant_id, for_write=True)
        contract_dir.mkdir(parents=True, exist_ok=True)

        new_payload = contract.model_dump(by_alias=True)

        # Concurrency safety: acquire flock during version inspection and publication
        lock_fd = os.open(str(self._lock_file), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)

            # 1. Check version immutability only inside this write namespace.
            # Shared fallback contracts are readable defaults, not tenant-owned
            # versions and must not prevent a tenant from defining its own copy.
            existing_matches = sorted(contract_dir.glob(f"{name}__v{ver}__*.json"))
            if existing_matches:
                with open(existing_matches[-1], encoding="utf-8") as f:
                    existing_payload = json.load(f)
                if existing_payload == new_payload:
                    # Idempotent re-save of exact same contract
                    if tenant_id is None:
                        self._mark_shared_contract_unlocked(name)
                    return contract_dir / "latest.json"
                raise ValueError(
                    f"Contract '{name}' version '{ver}' already exists and is immutable. "
                    f"Bump version number (e.g. to '{ver}.1')."
                )

            # 2. Write immutable versioned artifact
            now_iso = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
            unique_suffix = uuid.uuid4().hex[:6]
            filename = f"{name}__v{ver}__{now_iso}__{unique_suffix}.json"
            dest = contract_dir / filename

            tmp_dest = contract_dir / f".tmp_{filename}"
            with open(tmp_dest, "w", encoding="utf-8") as f:
                json.dump(new_payload, f, indent=2)
            os.replace(tmp_dest, dest)

            # 3. Deterministic Latest Activation (Update latest ONLY if new ver >= current latest ver)
            latest_file = contract_dir / "latest.json"
            current_latest = None
            if latest_file.exists():
                with open(latest_file, encoding="utf-8") as f:
                    current_latest = DataContract(**json.load(f))
            should_update_latest = True
            if current_latest is not None:
                curr_semver = self._parse_semver(current_latest.metadata.version)
                new_semver = self._parse_semver(ver)
                if new_semver < curr_semver:
                    should_update_latest = False

            if should_update_latest:
                latest_file = contract_dir / "latest.json"
                tmp_latest = contract_dir / f".tmp_latest_{unique_suffix}.json"
                with open(tmp_latest, "w", encoding="utf-8") as f:
                    json.dump(new_payload, f, indent=2)
                os.replace(tmp_latest, latest_file)

            if tenant_id is None:
                self._mark_shared_contract_unlocked(name)
            return dest

        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def get_latest(self, name: str, tenant_id: str | None = None) -> DataContract | None:
        """Retrieve latest active contract for a data product, respecting tenant boundaries."""
        contract_dir = self._get_target_dir(name, tenant_id=tenant_id, for_write=False)
        latest_file = contract_dir / "latest.json"
        if not latest_file.exists():
            # Fallback to shared catalog if tenant catalog had no contract
            if tenant_id and self._validate_identifier(name) in self._shared_contract_names():
                fallback_file = self.storage_dir / self._validate_identifier(name) / "latest.json"
                if fallback_file.exists():
                    latest_file = fallback_file
                else:
                    return None
            elif tenant_id:
                return None
            else:
                return None
        with open(latest_file, encoding="utf-8") as f:
            data = json.load(f)
        return DataContract(**data)

    def get_version(self, name: str, version: str, tenant_id: str | None = None) -> DataContract | None:
        """Retrieve exact contract version deterministically."""
        clean_name = self._validate_identifier(name)
        clean_ver = self._validate_identifier(version)
        contract_dir = self._get_target_dir(clean_name, tenant_id=tenant_id, for_write=False)
        
        matches = sorted(contract_dir.glob(f"{clean_name}__v{clean_ver}__*.json"))
        if not matches and tenant_id and clean_name in self._shared_contract_names():
            # Only explicitly registered shared defaults are visible cross-tenant.
            shared_dir = self.storage_dir / clean_name
            if shared_dir.exists():
                matches = sorted(shared_dir.glob(f"{clean_name}__v{clean_ver}__*.json"))

        if not matches:
            return None

        with open(matches[-1], encoding="utf-8") as f:
            return DataContract(**json.load(f))

    def get_as_of_timestamp(self, name: str, timestamp_iso: str, tenant_id: str | None = None) -> DataContract | None:
        """
        Point-in-time time travel: Retrieves the contract version active at a specific historical moment.
        """
        clean_name = self._validate_identifier(name)
        contract_dir = self._get_target_dir(clean_name, tenant_id=tenant_id, for_write=False)
        if not contract_dir.exists() and tenant_id and clean_name in self._shared_contract_names():
            contract_dir = self.storage_dir / clean_name
        if not contract_dir.exists():
            return None

        all_versions = sorted(contract_dir.glob(f"{clean_name}__v*__*.json"))
        active_version_file = None

        target_dt = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))

        for v_file in all_versions:
            parts = v_file.stem.split("__")
            if len(parts) >= 3:
                ts_str = parts[2]
                try:
                    v_dt = datetime.strptime(ts_str, "%Y%m%dT%H%M%S_%fZ").replace(tzinfo=UTC)
                    if v_dt <= target_dt:
                        active_version_file = v_file
                except Exception:
                    continue

        if active_version_file:
            with open(active_version_file, encoding="utf-8") as f:
                return DataContract(**json.load(f))
        return None
