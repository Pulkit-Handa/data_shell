"""Security Sandbox: Enforces filesystem root boundaries, namespace isolation, and path traversal prevention."""

from __future__ import annotations

from pathlib import Path

from datashell.core.config import settings


class PathSecurityViolation(Exception):
    """Raised when an unapproved, escaping, or reserved filesystem path is accessed."""
    pass


class FilesystemSandbox:
    """
    Enforces strict filesystem jail for all data delivery, quarantine, and staging paths.
    Prevents attacks where contract paths overwrite registry metadata, audit certificates, or system files.
    """

    @staticmethod
    def get_allowed_roots() -> list[Path]:
        """Returns normalized absolute paths of permitted directories for general operations."""
        import tempfile
        return [
            settings.output_dir.resolve(),
            settings.data_dir.resolve(),
            settings.audit_dir.resolve(),
            settings.quarantine_dir.resolve(),
            Path(tempfile.gettempdir()).resolve(),
        ]

    @staticmethod
    def get_allowed_delivery_roots(tenant_id: str | None = None) -> list[Path]:
        """
        Narrow delivery root: User-defined delivery targets may write
        inside the designated tenant output delivery namespace or standard temp directory for testing.
        All operational namespaces (data, audits, quarantine, contracts, history) remain strictly reserved.
        """
        import tempfile
        tenant_dir = settings.get_tenant_delivery_dir(tenant_id or "default-tenant")
        if settings.is_production_mode:
            return [tenant_dir]
        return [
            tenant_dir,
            settings.output_dir.resolve(),
            Path(tempfile.gettempdir()).resolve(),
        ]


    @staticmethod
    def get_forbidden_roots() -> list[Path]:
        """Returns forbidden directories that arbitrary operations must never touch."""
        return [
            settings.contracts_dir.resolve(),
            Path("/etc").resolve(),
            Path("/var").resolve(),
            Path("/usr").resolve(),
            Path("/bin").resolve(),
            Path("/root").resolve(),
        ]

    @staticmethod
    def get_reserved_operational_roots() -> list[Path]:
        """
        Operational namespaces reserved exclusively for the gateway engine.
        User-defined data delivery targets CANNOT write into these namespaces.
        Includes audits, contracts, raw input data, quarantine, and history databases.
        """
        return [
            settings.audit_dir.resolve(),
            settings.contracts_dir.resolve(),
            settings.data_dir.resolve(),
            settings.quarantine_dir.resolve(),
            settings.history_db_path.resolve(),
            settings.security_db_path.resolve(),
        ]

    @classmethod
    def normalize_relative_path(cls, raw_path: str, base_dir: Path) -> Path:
        """
        Normalizes relative path and avoids double nesting (e.g. ./output/curated -> base_dir/curated
        when base_dir is already .../output or .../output/tenants/<tenant>).
        """
        p = Path(raw_path)
        parts = list(p.parts)
        # Strip leading '.' or './'
        while parts and parts[0] in (".", "./"):
            parts.pop(0)
        # If parts start with 'output' and base_dir is already inside output
        child_of_output = (
            parts and parts[0] == "output"
            and "output" in [b.name for b in [base_dir] + list(base_dir.parents)]
        )
        if child_of_output or (parts and parts[0] == base_dir.name):
            parts.pop(0)
        
        return (base_dir / Path(*parts) if parts else base_dir).resolve()

    @classmethod
    def sanitize_and_validate_path(
        cls,
        target_path: str | Path,
        allowed_roots: list[Path] | None = None,
        create_parent: bool = True,
        is_delivery_target: bool = False,
        tenant_id: str | None = None
    ) -> Path:
        """
        Validates that a path stays strictly within approved storage roots,
        does not escape into system directories, enforces tenant-isolated namespaces,
        and respects operational namespace and engine artifact reservations.
        """
        raw_str = str(target_path).strip()
        if not raw_str:
            raise PathSecurityViolation("Target path cannot be empty")

        # Reject path traversal patterns
        parts = Path(raw_str).parts
        if ".." in parts:
            raise PathSecurityViolation(f"Path traversal sequence '..' detected in: '{target_path}'")

        p = Path(raw_str).expanduser()
        if p.is_symlink():
            raise PathSecurityViolation(f"Access denied: destination '{p}' is a symlink")

        effective_tenant = tenant_id or "default-tenant"
        tenant_delivery_dir = settings.get_tenant_delivery_dir(effective_tenant)

        if not p.is_absolute():
            base_dir = tenant_delivery_dir if is_delivery_target else settings.output_dir
            path = cls.normalize_relative_path(raw_str, base_dir)
        else:
            path = p.resolve()

        if path.is_symlink():
            raise PathSecurityViolation(f"Access denied: destination '{path}' is a symlink")

        # Select roots based on operation type
        if is_delivery_target:
            roots = allowed_roots or cls.get_allowed_delivery_roots(tenant_id)
        else:
            roots = allowed_roots or cls.get_allowed_roots()

        forbidden = cls.get_forbidden_roots()

        # Check against forbidden system roots
        for f_root in forbidden:
            if path == f_root or path.is_relative_to(f_root):
                raise PathSecurityViolation(
                    f"Access denied: destination '{path}' enters protected system directory '{f_root}'"
                )

        # Check against reserved operational namespaces and naming for delivery targets
        if is_delivery_target:
            # Forbid targeting database files or lock files
            if path.name.lower().endswith((".db", ".sqlite", ".sqlite3", ".lock")):
                raise PathSecurityViolation(
                    f"Access denied: delivery target '{path}' attempts to overwrite operational database or lock file"
                )

            # Forbid targeting engine operational artifact naming patterns
            filename = path.name.lower()
            if (
                filename.startswith((".staged_", ".dm-commit-"))
                or filename.endswith((
                    ".backup", "_held_records.parquet", "_structural_rejections.jsonl",
                    "_audit.json", "_audit_certificate.json",
                ))
                or "_lineage_" in filename
                or filename.startswith("dm_run_")
                or filename.startswith("dm_stream_")
            ):
                raise PathSecurityViolation(
                    f"Access denied: delivery target '{path.name}' conflicts with "
                    "reserved engine artifact naming pattern"
                )

            for op_root in cls.get_reserved_operational_roots():
                if path == op_root or path.is_relative_to(op_root):
                    raise PathSecurityViolation(
                        f"Access denied: delivery target '{path}' attempts to write into reserved "
                        f"operational namespace '{op_root}'"
                    )

            # Cross-tenant storage boundary check:
            # Deliveries targeted under the tenants/ tree must belong to the authenticated tenant
            tenants_root = (settings.output_dir / "tenants").resolve()
            if path.is_relative_to(tenants_root) and not (
                path == tenant_delivery_dir or path.is_relative_to(tenant_delivery_dir)
            ):
                raise PathSecurityViolation(
                    f"Cross-tenant storage boundary violation: tenant '{effective_tenant}' "
                    f"is not permitted to deliver data into '{path}'"
                )

            # In production, reject temporary-directory delivery targets
            if settings.is_production_mode:
                import tempfile
                sys_tmp = Path(tempfile.gettempdir()).resolve()
                if path == sys_tmp or path.is_relative_to(sys_tmp):
                    raise PathSecurityViolation(
                        f"Access denied: system temporary directory '{sys_tmp}' is not permitted for "
                        f"production delivery targets. Target must be within tenant delivery namespace."
                    )

        # Check that path is inside at least one approved root
        is_inside_allowed = any(path.is_relative_to(a_root) for a_root in roots)

        if not is_inside_allowed:
            raise PathSecurityViolation(
                f"Access denied: destination '{path}' is outside approved storage roots ({[str(r) for r in roots]})"
            )

        if create_parent:
            path.parent.mkdir(parents=True, exist_ok=True)

        return path

