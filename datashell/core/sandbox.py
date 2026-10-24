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

    # SQLite's database file and its sidecars (-wal, -shm, -journal) are engine
    # operational state.  A user-controlled path must never be allowed to
    # create a *directory* at one of these names: doing so permanently breaks
    # the ledger/credential database until an operator intervenes.
    OPERATIONAL_DB_SUFFIXES = (
        ".db", ".sqlite", ".sqlite3", ".lock",
        ".db-wal", ".db-shm", ".db-journal",
        ".sqlite-wal", ".sqlite-shm", ".sqlite-journal",
        ".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal",
        "-wal", "-shm", "-journal",
    )

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
    def get_allowed_quarantine_roots(tenant_id: str | None = None) -> list[Path]:
        """Narrow quarantine root: tenant isolation is mandatory.

        Production accepts only the authenticated tenant's quarantine
        namespace.  Development additionally accepts the system temp directory
        so tests and the local demo can use private scratch workspaces.  The
        shared quarantine root is never itself writable by a tenant.
        """
        import tempfile
        tenant_dir = settings.get_tenant_quarantine_dir(tenant_id or "default-tenant")
        if settings.is_production_mode:
            return [tenant_dir]
        return [
            tenant_dir,
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
    def get_reserved_operational_roots(include_quarantine: bool = True) -> list[Path]:
        """
        Operational namespaces reserved exclusively for the gateway engine.
        User-defined data delivery targets CANNOT write into these namespaces.
        Includes audits, contracts, raw input data, quarantine, and history databases.
        """
        roots = [
            settings.audit_dir.resolve(),
            settings.contracts_dir.resolve(),
            settings.data_dir.resolve(),
            settings.history_db_path.resolve(),
            settings.security_db_path.resolve(),
        ]
        if include_quarantine:
            roots.append(settings.quarantine_dir.resolve())
        return roots

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
    def _reject_operational_db_components(cls, path: Path, subject: str) -> None:
        """Reject any path component that names a database, lock, or SQLite sidecar.

        Both delivery targets and quarantine directories are user-controlled.
        A directory created at ``datashell_history.db-wal`` (or any other
        database sidecar) permanently prevents SQLite from opening the ledger,
        so the check covers every component, not just the final filename.
        """
        for component in path.parts:
            if component.lower().endswith(cls.OPERATIONAL_DB_SUFFIXES):
                raise PathSecurityViolation(
                    f"Access denied: {subject} '{path}' attempts to use operational "
                    f"database or lock file component '{component}'"
                )

    @classmethod
    def sanitize_and_validate_path(
        cls,
        target_path: str | Path,
        allowed_roots: list[Path] | None = None,
        create_parent: bool = True,
        is_delivery_target: bool = False,
        tenant_id: str | None = None,
        is_quarantine_path: bool = False,
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

        # Legacy contracts often declare the shared quarantine root
        # (``./output/quarantine``).  Treat that as a logical root and rebase
        # it into the authenticated tenant's namespace instead of writing to
        # the shared directory.  Explicit ``.../tenants/<other>`` paths are
        # deliberately not rebased: they are rejected below as cross-tenant.
        tenant_quarantine_dir: Path | None = None
        if is_quarantine_path and tenant_id is not None:
            tenant_quarantine_dir = settings.get_tenant_quarantine_dir(effective_tenant)
            shared_quarantine_root = settings.quarantine_dir.resolve()
            if allowed_roots is None:
                if path == shared_quarantine_root:
                    path = tenant_quarantine_dir
                elif path.is_relative_to(shared_quarantine_root) and not path.is_relative_to(
                    shared_quarantine_root / "tenants"
                ):
                    relative = path.relative_to(shared_quarantine_root)
                    path = (tenant_quarantine_dir / relative).resolve()

        # Select roots based on operation type. Quarantine paths use a dedicated
        # tenant-scoped root even when a contract supplies an absolute path.
        if is_delivery_target:
            roots = allowed_roots or cls.get_allowed_delivery_roots(tenant_id)
        elif is_quarantine_path and tenant_id is not None:
            roots = allowed_roots or cls.get_allowed_quarantine_roots(tenant_id)
        else:
            roots = allowed_roots or cls.get_allowed_roots()

        forbidden = cls.get_forbidden_roots()

        # Check against forbidden system roots
        for f_root in forbidden:
            if path == f_root or path.is_relative_to(f_root):
                raise PathSecurityViolation(
                    f"Access denied: destination '{path}' enters protected system directory '{f_root}'"
                )

        # Databases, lock files, and SQLite sidecars are never user-writable.
        # This applies to both delivery targets and quarantine paths.
        if is_delivery_target or is_quarantine_path:
            subject = "delivery target" if is_delivery_target else "quarantine path"
            cls._reject_operational_db_components(path, subject)

        # Check against reserved operational namespaces and naming for delivery targets
        if is_delivery_target:
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

        elif is_quarantine_path:
            # Quarantine is engine state, not a delivery target. It may not
            # overwrite audit, data, contract, or database namespaces, but it
            # is allowed inside the dedicated quarantine root.
            for op_root in cls.get_reserved_operational_roots(include_quarantine=False):
                if path == op_root or path.is_relative_to(op_root):
                    raise PathSecurityViolation(
                        f"Access denied: quarantine path '{path}' attempts to write into reserved "
                        f"operational namespace '{op_root}'"
                    )

            if path.exists() and not path.is_dir():
                raise PathSecurityViolation(
                    f"Access denied: quarantine path '{path}' exists and is not a directory"
                )

            # When the authenticated tenant is known, a quarantine path that
            # lives under the quarantine root must belong to that tenant.  This
            # blocks cross-tenant quarantine reads/writes while still allowing
            # an explicit path in development scratch space.
            if tenant_quarantine_dir is not None:
                quarantine_root = settings.quarantine_dir.resolve()
                if path.is_relative_to(quarantine_root) and not (
                    path == tenant_quarantine_dir or path.is_relative_to(tenant_quarantine_dir)
                ):
                    raise PathSecurityViolation(
                        f"Cross-tenant quarantine boundary violation: tenant '{effective_tenant}' "
                        f"is not permitted to write quarantine data into '{path}'"
                    )

            # Development scratch roots can sit under the same temporary
            # directory as the output tree.  Never let a quarantine path cross
            # into another tenant's delivery namespace.
            tenants_root = (settings.output_dir / "tenants").resolve()
            if path.is_relative_to(tenants_root) and not (
                path == tenant_delivery_dir or path.is_relative_to(tenant_delivery_dir)
            ):
                raise PathSecurityViolation(
                    f"Cross-tenant storage boundary violation: tenant '{effective_tenant}' "
                    f"is not permitted to write quarantine data into '{path}'"
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

