"""Durable boundary ledger — the accumulating forensic system of record.

Every enforcement run is persisted here, not only successful deliveries. Keeping
the full history of *what crossed the boundary, when, and why* turns the gateway
into a queryable, tenant-isolated asset rather than a stateless validator. It
backs three capabilities:

* **Historical drift baselines** — rolling per-tenant, per-contract baselines
  derived from prior runs instead of a single most-recent value.
* **Forensics** — one ledger row per run plus a quarantine index of every held
  or structurally refused record, so operators and auditors can reconstruct a
  feed's history.
* **Metering** — per-run usage rows that underpin outcome-as-a-service billing.

All read/write methods are tenant scoped: cross-tenant reads are impossible by
construction, which is what makes the ledger safe to expose as a shared asset.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from datashell.core.config import settings

# Size cap for pending-outbox records (same bound as commit journals: a
# ledger payload is small structured metadata, never bulk data).
MAX_OUTBOX_BYTES = 1024 * 1024

# Suffix for durable outbox records keyed by audit certificate path.
PENDING_OUTBOX_SUFFIX = ".ledger-pending.json"


# Columns added to ``run_history`` after its original two-field-per-run form.
# Declared as an ordered list so migration is an idempotent ALTER TABLE loop;
# this keeps databases written by earlier releases readable.
_RUN_HISTORY_MIGRATIONS: list[tuple[str, str]] = [
    ("contract_version", "TEXT"),
    ("status", "TEXT"),
    ("rows_refused", "INTEGER DEFAULT 0"),
    ("rows_held", "INTEGER DEFAULT 0"),
    ("unaccounted_for", "INTEGER DEFAULT 0"),
    ("footing_matched", "INTEGER DEFAULT 1"),
    ("is_run_rejected", "INTEGER DEFAULT 0"),
    ("rejection_reason", "TEXT"),
    ("duration_seconds", "REAL"),
    ("execution_mode", "TEXT"),
    ("source_name", "TEXT"),
    ("source_bytes", "INTEGER"),
    ("target_path", "TEXT"),
    ("audit_path", "TEXT"),
    ("quarantine_path", "TEXT"),
    ("merkle_root", "TEXT"),
    ("signature_key_id", "TEXT"),
    ("rule_breakdown_json", "TEXT"),
    ("structural_reasons_json", "TEXT"),
    ("certificate_digest", "TEXT"),
    ("ledger_status", "TEXT"),
]

# Runs recorded before the ledger captured ``status`` implicitly succeeded.
_DELIVERED_FILTER = "(status IS NULL OR status = 'LOADED')"


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {})


def _unjson(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class BoundaryLedger:
    """Tenant-isolated SQLite store for run history, quarantine, and usage.

    The database is the same ``datashell_history.db`` that originally tracked
    drift baselines, so existing deployments migrate in place.
    """

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or settings.history_db_path)
        self._init_db_failed = False
        try:
            self._init_db()
        except Exception:
            # Defer corruption errors to check_integrity() so readiness probes
            # can detect and report an unhealthy database at runtime.
            self._init_db_failed = True

    # ---------------------------------------------------------------- internal

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_history (
                    run_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'default-tenant',
                    contract_name TEXT,
                    timestamp TEXT,
                    rows_read INTEGER,
                    rows_delivered INTEGER,
                    metrics_json TEXT
                )
                """
            )
            existing = {row[1] for row in conn.execute("PRAGMA table_info(run_history)")}
            for column, ddl in _RUN_HISTORY_MIGRATIONS:
                if column not in existing:
                    conn.execute(f"ALTER TABLE run_history ADD COLUMN {column} {ddl}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenant_contract "
                "ON run_history(tenant_id, contract_name, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenant_status "
                "ON run_history(tenant_id, status, timestamp)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quarantine_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    contract_name TEXT,
                    timestamp TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    artifact_path TEXT,
                    row_count INTEGER DEFAULT 0,
                    dominant_reason TEXT,
                    reasons_json TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_quarantine_tenant "
                "ON quarantine_index(tenant_id, contract_name, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_quarantine_run "
                "ON quarantine_index(tenant_id, run_id)"
            )
            # A quarantine row is identified by (tenant, run, kind, artifact).
            # The unique constraint is what makes replaying a pending ledger
            # entry idempotent: a replay must not duplicate quarantine records.
            # COALESCE keeps NULL artifact paths comparable (SQLite treats NULLs
            # as distinct in unique indexes).
            self._ensure_unique_quarantine_index(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    contract_name TEXT,
                    timestamp TEXT NOT NULL,
                    status TEXT,
                    execution_mode TEXT,
                    rows_read INTEGER DEFAULT 0,
                    rows_delivered INTEGER DEFAULT 0,
                    bytes_processed INTEGER DEFAULT 0,
                    billable INTEGER DEFAULT 1,
                    unit TEXT DEFAULT 'row',
                    units INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_tenant "
                "ON usage_records(tenant_id, contract_name, timestamp)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_run "
                "ON usage_records(tenant_id, run_id)"
            )
            # Baseline reset watermarks for drift evaluation. Recording a reset
            # never deletes forensic history: comparisons simply ignore runs
            # delivered before the watermark.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS baseline_resets (
                    tenant_id TEXT NOT NULL,
                    contract_name TEXT NOT NULL,
                    reset_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, contract_name)
                )
                """
            )

    # ------------------------------------------------------- pending outbox

    @staticmethod
    def _ensure_unique_quarantine_index(conn: sqlite3.Connection) -> None:
        """Add the unique quarantine identity, collapsing legacy duplicates.

        Databases written before this constraint may contain duplicate
        quarantine rows (one per replay of the same pending ledger entry).
        Adding the unique index over such a table fails, so duplicates are
        collapsed first - keeping the earliest row, which is the original
        observation - and the constraint is then created.
        """
        statement = (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_quarantine_unique "
            "ON quarantine_index(tenant_id, run_id, kind, COALESCE(artifact_path, ''))"
        )
        try:
            conn.execute(statement)
            return
        except sqlite3.IntegrityError:
            pass
        conn.execute(
            """
            DELETE FROM quarantine_index
            WHERE id NOT IN (
                SELECT MIN(id) FROM quarantine_index
                GROUP BY tenant_id, run_id, kind, COALESCE(artifact_path, '')
            )
            """
        )
        conn.execute(statement)

    @staticmethod
    def pending_outbox_path(audit_dest: Path, run_id: str | None = None) -> Path:
        """Durable outbox record for one run's ledger payload (LEDGER-001).

        Written atomically *before* artifacts are published and removed only
        after the ledger row is durably recorded. A crash between publication
        and ledger recording therefore always leaves a replayable record: the
        next startup (or an explicit ``reconcile``) re-applies it idempotently
        via ``INSERT OR REPLACE`` keyed on ``run_id``.

        The record name includes the ``run_id`` when one is supplied, so two
        concurrent runs that were pointed at the *same* audit certificate path
        still own separate pending records and can never overwrite one
        another's payload. The legacy form (no run id) is still produced and
        still discovered, so records written by earlier versions recover.
        """
        dest = Path(audit_dest)
        if run_id:
            return dest.with_name(f"{dest.name}.{run_id}{PENDING_OUTBOX_SUFFIX}")
        return dest.with_name(dest.name + PENDING_OUTBOX_SUFFIX)

    @staticmethod
    def write_pending_outbox(outbox_path: Path, payload: dict[str, Any]) -> Path:
        """Atomically persist the ledger payload ahead of publication."""
        outbox_path = Path(outbox_path)
        outbox_path.parent.mkdir(parents=True, exist_ok=True)
        temp = outbox_path.with_name(f".{outbox_path.name}.{uuid.uuid4().hex}.tmp")
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, outbox_path)
        try:
            dir_fd = os.open(str(outbox_path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return outbox_path
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
        return outbox_path

    @staticmethod
    def clear_pending_outbox(outbox_path: Path) -> None:
        Path(outbox_path).unlink(missing_ok=True)

    @staticmethod
    def iter_pending_outboxes(root: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
        """Yield pending-outbox records beneath a storage root (oldest first)."""
        root = Path(root)
        if not root.is_dir():
            return
        for outbox in sorted(root.rglob(f"*{PENDING_OUTBOX_SUFFIX}")):
            if outbox.is_symlink():
                continue
            try:
                if outbox.stat().st_size > MAX_OUTBOX_BYTES:
                    continue
                with open(outbox, encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("run_id"):
                yield outbox, payload


    # --------------------------------------------------------------- run writes

    def record_run(
        self,
        run_id: str,
        contract_name: str,
        rows_read: int,
        rows_delivered: int,
        metrics: dict[str, float],
        tenant_id: str = "default-tenant",
        *,
        contract_version: str | None = None,
        status: str | None = None,
        rows_refused: int = 0,
        rows_held: int = 0,
        unaccounted_for: int = 0,
        footing_matched: bool = True,
        is_run_rejected: bool = False,
        rejection_reason: str | None = None,
        duration_seconds: float | None = None,
        execution_mode: str | None = None,
        source_name: str | None = None,
        source_bytes: int | None = None,
        target_path: str | None = None,
        audit_path: str | None = None,
        quarantine_path: str | None = None,
        merkle_root: str | None = None,
        signature_key_id: str | None = None,
        rule_breakdown: dict[str, int] | None = None,
        structural_reasons: dict[str, int] | None = None,
        certificate_digest: str | None = None,
        ledger_status: str | None = None,
    ) -> None:
        """Persist one forensic ledger row for an enforcement run.

        Idempotent on ``run_id`` so a retried finalization cannot double-count.

        ``ledger_status`` is ``COMMITTED`` for a fully recorded run and
        ``PENDING`` for a run whose artifacts are published but whose ledger
        row still awaits outbox reconciliation. ``None`` (legacy rows) reads
        as committed.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO run_history (
                    run_id, tenant_id, contract_name, timestamp, rows_read,
                    rows_delivered, metrics_json, contract_version, status,
                    rows_refused, rows_held, unaccounted_for, footing_matched,
                    is_run_rejected, rejection_reason, duration_seconds,
                    execution_mode, source_name, source_bytes, target_path,
                    audit_path, quarantine_path, merkle_root, signature_key_id,
                    rule_breakdown_json, structural_reasons_json,
                    certificate_digest, ledger_status
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    , ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    run_id,
                    tenant_id,
                    contract_name,
                    _utcnow(),
                    int(rows_read),
                    int(rows_delivered),
                    _json(metrics),
                    contract_version,
                    status,
                    int(rows_refused),
                    int(rows_held),
                    int(unaccounted_for),
                    1 if footing_matched else 0,
                    1 if is_run_rejected else 0,
                    rejection_reason,
                    duration_seconds,
                    execution_mode,
                    source_name,
                    source_bytes,
                    target_path,
                    audit_path,
                    quarantine_path,
                    merkle_root,
                    signature_key_id,
                    _json(rule_breakdown or {}),
                    _json(structural_reasons or {}),
                    certificate_digest,
                    ledger_status or "COMMITTED",
                ),
            )

    def apply_run_outcome(
        self,
        *,
        run_id: str,
        tenant_id: str,
        contract_name: str,
        rows_read: int,
        rows_delivered: int,
        metrics: dict[str, Any] | None = None,
        contract_version: str | None = None,
        status: str | None = None,
        rows_refused: int = 0,
        rows_held: int = 0,
        unaccounted_for: int = 0,
        footing_matched: bool = True,
        is_run_rejected: bool = False,
        rejection_reason: str | None = None,
        duration_seconds: float | None = None,
        execution_mode: str | None = None,
        source_name: str | None = None,
        source_bytes: int | None = None,
        target_path: str | None = None,
        audit_path: str | None = None,
        quarantine_path: str | None = None,
        merkle_root: str | None = None,
        signature_key_id: str | None = None,
        certificate_digest: str | None = None,
        rule_breakdown: dict[str, int] | None = None,
        structural_reasons: dict[str, int] | None = None,
        quarantine_rows: list[dict[str, Any]] | None = None,
        billable: bool = True,
        unit: str = "row",
        units: int | None = None,
    ) -> None:
        """Apply a complete run outcome to history, quarantine index, and usage.

        All three writes commit in **one** SQLite transaction: either the run
        row, its quarantine index rows, and its usage meter are all durable, or
        none of them are. A run is therefore never left marked ``COMMITTED``
        with incomplete accounting - the failure mode that would let a
        published delivery lose its usage/settlement basis while still looking
        recorded.

        The write is idempotent: ``run_history`` is keyed by ``run_id`` and
        ``usage_records`` by ``(tenant_id, run_id)``, and quarantine rows are
        keyed by ``(tenant_id, run_id, kind, artifact_path)``, so replaying the
        same outcome replaces rather than duplicates.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO run_history (
                    run_id, tenant_id, contract_name, timestamp, rows_read,
                    rows_delivered, metrics_json, contract_version, status,
                    rows_refused, rows_held, unaccounted_for, footing_matched,
                    is_run_rejected, rejection_reason, duration_seconds,
                    execution_mode, source_name, source_bytes, target_path,
                    audit_path, quarantine_path, merkle_root, signature_key_id,
                    rule_breakdown_json, structural_reasons_json,
                    certificate_digest, ledger_status
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    , ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    run_id,
                    tenant_id,
                    contract_name,
                    _utcnow(),
                    int(rows_read),
                    int(rows_delivered),
                    _json(metrics or {}),
                    contract_version,
                    status,
                    int(rows_refused),
                    int(rows_held),
                    int(unaccounted_for),
                    1 if footing_matched else 0,
                    1 if is_run_rejected else 0,
                    rejection_reason,
                    duration_seconds,
                    execution_mode,
                    source_name,
                    source_bytes,
                    target_path,
                    audit_path,
                    quarantine_path,
                    merkle_root,
                    signature_key_id,
                    _json(rule_breakdown or {}),
                    _json(structural_reasons or {}),
                    certificate_digest,
                    "COMMITTED",
                ),
            )
            for row in quarantine_rows or []:
                reasons: dict[str, Any] = dict(row.get("reasons") or {})
                dominant = max(reasons, key=reasons.__getitem__) if reasons else None
                conn.execute(
                    """
                    INSERT OR REPLACE INTO quarantine_index (
                        run_id, tenant_id, contract_name, timestamp, kind,
                        artifact_path, row_count, dominant_reason, reasons_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        tenant_id,
                        contract_name,
                        _utcnow(),
                        str(row.get("kind") or "unknown"),
                        row.get("artifact_path"),
                        int(row.get("row_count") or 0),
                        dominant,
                        _json(reasons),
                    ),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO usage_records (
                    run_id, tenant_id, contract_name, timestamp, status,
                    execution_mode, rows_read, rows_delivered, bytes_processed,
                    billable, unit, units
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    tenant_id,
                    contract_name,
                    _utcnow(),
                    status,
                    execution_mode,
                    int(rows_read),
                    int(rows_delivered),
                    int(source_bytes or 0),
                    1 if billable else 0,
                    unit,
                    int(units if units is not None else rows_read),
                ),
            )

    def record_quarantine(
        self,
        run_id: str,
        tenant_id: str,
        contract_name: str | None,
        kind: str,
        artifact_path: str | None,
        row_count: int,
        reasons: dict[str, int] | None = None,
    ) -> None:
        """Index one quarantine artifact (held rows or structural refusals).

        Idempotent on the (tenant, run, kind, artifact) identity so replaying a
        pending ledger entry replaces the row instead of duplicating it.
        """
        reason_map: dict[str, Any] = dict(reasons or {})
        dominant = max(reason_map, key=reason_map.__getitem__) if reason_map else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO quarantine_index (
                    run_id, tenant_id, contract_name, timestamp, kind,
                    artifact_path, row_count, dominant_reason, reasons_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    tenant_id,
                    contract_name,
                    _utcnow(),
                    kind,
                    artifact_path,
                    int(row_count),
                    dominant,
                    _json(reason_map),
                ),
            )

    def record_usage(
        self,
        run_id: str,
        tenant_id: str,
        contract_name: str | None,
        status: str | None,
        execution_mode: str | None,
        rows_read: int,
        rows_delivered: int,
        bytes_processed: int = 0,
        billable: bool = True,
        unit: str = "row",
        units: int | None = None,
    ) -> None:
        """Record a metering row for outcome-as-a-service accounting."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO usage_records (
                    run_id, tenant_id, contract_name, timestamp, status,
                    execution_mode, rows_read, rows_delivered, bytes_processed,
                    billable, unit, units
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    tenant_id,
                    contract_name,
                    _utcnow(),
                    status,
                    execution_mode,
                    int(rows_read),
                    int(rows_delivered),
                    int(bytes_processed),
                    1 if billable else 0,
                    unit,
                    int(units if units is not None else rows_read),
                ),
            )

    # ---------------------------------------------------------------- run reads

    def get_last_run(
        self, contract_name: str, tenant_id: str = "default-tenant"
    ) -> dict[str, Any] | None:
        """Most recent *delivered* run for a contract (drift baseline source)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id, tenant_id, timestamp, rows_read, rows_delivered, metrics_json "  # nosec B608
                f"FROM run_history WHERE tenant_id = ? AND contract_name = ? AND {_DELIVERED_FILTER} "
                "ORDER BY timestamp DESC LIMIT 1",
                (tenant_id, contract_name),
            ).fetchone()
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "tenant_id": row["tenant_id"],
            "timestamp": row["timestamp"],
            "rows_read": row["rows_read"],
            "rows_delivered": row["rows_delivered"],
            "metrics": _unjson(row["metrics_json"]),
        }

    def get_baseline(
        self,
        contract_name: str,
        tenant_id: str = "default-tenant",
        window: int = 1,
    ) -> dict[str, Any] | None:
        """Rolling delivered-row and metric baseline over the last ``window`` runs.

        ``window=1`` reproduces the historical "compare against the previous run"
        behaviour; larger windows smooth out single-run noise.

        Runs delivered before a :meth:`reset_baseline` watermark for this
        contract/tenant are excluded, so an operator can intentionally restart
        drift history without deleting forensic ledger rows.
        """
        window = max(1, int(window))
        with self._connect() as conn:
            reset_row = conn.execute(
                "SELECT reset_at FROM baseline_resets WHERE tenant_id = ? AND contract_name = ?",
                (tenant_id, contract_name),
            ).fetchone()
            cutoff = reset_row["reset_at"] if reset_row else ""
            rows = conn.execute(
                "SELECT rows_delivered, metrics_json FROM run_history "  # nosec B608
                f"WHERE tenant_id = ? AND contract_name = ? AND {_DELIVERED_FILTER} "
                "AND timestamp > ? ORDER BY timestamp DESC LIMIT ?",
                (tenant_id, contract_name, cutoff, window),
            ).fetchall()
        if not rows:
            return None

        delivered = [float(r["rows_delivered"] or 0) for r in rows]
        metric_samples: dict[str, list[float]] = {}
        for r in rows:
            for name, value in _unjson(r["metrics_json"]).items():
                try:
                    metric_samples.setdefault(name, []).append(float(value))
                except (TypeError, ValueError):
                    continue
        metrics = {
            name: sum(values) / len(values)
            for name, values in metric_samples.items()
            if values
        }
        return {
            "rows_delivered": sum(delivered) / len(delivered),
            "metrics": metrics,
            "sample_count": len(rows),
        }

    def reset_baseline(self, contract_name: str, tenant_id: str = "default-tenant") -> None:
        """Clear the drift baseline for one contract/tenant combination.

        Records a reset watermark instead of deleting run history: drift
        comparisons only consider runs delivered after this point, while the
        forensic run ledger and usage meter remain complete. The next delivered
        run for the contract/tenant establishes the new baseline.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO baseline_resets (tenant_id, contract_name, reset_at) VALUES (?, ?, ?)",
                (tenant_id, contract_name, _utcnow()),
            )

    def get_run(self, run_id: str, tenant_id: str) -> dict[str, Any] | None:
        """Full ledger record for a run, scoped to its tenant."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM run_history WHERE run_id = ? AND tenant_id = ?",
                (run_id, tenant_id),
            ).fetchone()
        return self._run_dict(row) if row else None

    def list_runs(
        self,
        tenant_id: str,
        contract_name: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Newest-first run records for a tenant with optional filters."""
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if contract_name:
            clauses.append("contract_name = ?")
            params.append(contract_name)
        if status:
            clauses.append("status = ?")
            params.append(status)
        params.extend([max(1, int(limit)), max(0, int(offset))])
        query = (
            "SELECT * FROM run_history WHERE " + " AND ".join(clauses)  # nosec B608
            + " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        )
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._run_dict(row) for row in rows]

    def count_runs(
        self,
        tenant_id: str,
        contract_name: str | None = None,
        status: str | None = None,
    ) -> int:
        """Total runs matching the same filters accepted by :meth:`list_runs`."""
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if contract_name:
            clauses.append("contract_name = ?")
            params.append(contract_name)
        if status:
            clauses.append("status = ?")
            params.append(status)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM run_history WHERE " + " AND ".join(clauses),  # nosec B608
                params,
            ).fetchone()
        return int(row["n"]) if row else 0

    def history_summary(
        self, tenant_id: str, contract_name: str | None = None
    ) -> dict[str, Any]:
        """Aggregate delivery health for a tenant (optionally one contract)."""
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if contract_name:
            clauses.append("contract_name = ?")
            params.append(contract_name)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            row = conn.execute(
                (
                    # nosec B608 — reviewed stats builder; text is enum/validated
                    "\n"
                    "                SELECT\n"
                    "                    COUNT(*) AS total_runs,\n"
                    "                    SUM(CASE WHEN status = 'LOADED' OR status IS NULL"
                    " THEN 1 ELSE 0 END) AS delivered_runs,\n"
                    "                    SUM(CASE WHEN is_run_rejected = 1 THEN 1 ELSE 0 END) AS rejected_runs,\n"
                    "                    COALESCE(SUM(rows_read), 0) AS rows_read,\n"
                    "                    COALESCE(SUM(rows_delivered), 0) AS rows_delivered,\n"
                    "                    COALESCE(SUM(rows_refused), 0) AS rows_refused,\n"
                    "                    COALESCE(SUM(rows_held), 0) AS rows_held,\n"
                    "                    SUM(CASE WHEN footing_matched = 0 THEN 1 ELSE 0 END) AS footing_mismatches,\n"
                    "                    MAX(timestamp) AS last_run_at\n"
                    f"                FROM run_history WHERE {where}\n"  # nosec B608
                ),
                params,
            ).fetchone()
        total = int(row["total_runs"] or 0) if row else 0
        delivered = int(row["delivered_runs"] or 0) if row else 0
        return {
            "total_runs": total,
            "delivered_runs": delivered,
            "rejected_runs": int(row["rejected_runs"] or 0) if row else 0,
            "rows_read": int(row["rows_read"] or 0) if row else 0,
            "rows_delivered": int(row["rows_delivered"] or 0) if row else 0,
            "rows_refused": int(row["rows_refused"] or 0) if row else 0,
            "rows_held": int(row["rows_held"] or 0) if row else 0,
            "footing_mismatches": int(row["footing_mismatches"] or 0) if row else 0,
            "last_run_at": row["last_run_at"] if row else None,
            "delivery_rate": round(delivered / total, 4) if total else 0.0,
        }

    def volume_trend(
        self,
        tenant_id: str,
        contract_name: str | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Chronological (oldest-first) slice of recent runs for trend charts."""
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if contract_name:
            clauses.append("contract_name = ?")
            params.append(contract_name)
        params.append(max(1, int(limit)))
        query = (
            "SELECT run_id, timestamp, status, rows_read, rows_delivered, "  # nosec B608
            "rows_refused, rows_held, footing_matched FROM run_history WHERE "
            + " AND ".join(clauses)
            + " ORDER BY timestamp DESC LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        trend = [
            {
                "run_id": r["run_id"],
                "timestamp": r["timestamp"],
                "status": r["status"],
                "rows_read": r["rows_read"],
                "rows_delivered": r["rows_delivered"],
                "rows_refused": r["rows_refused"],
                "rows_held": r["rows_held"],
                "footing_matched": bool(r["footing_matched"]),
            }
            for r in rows
        ]
        trend.reverse()
        return trend

    # ----------------------------------------------------------- quarantine reads

    def list_quarantine(
        self,
        tenant_id: str,
        contract_name: str | None = None,
        run_id: str | None = None,
        kind: str | None = None,
        reason: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search the quarantine index by contract, run, kind, or dominant reason."""
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if contract_name:
            clauses.append("contract_name = ?")
            params.append(contract_name)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if reason:
            clauses.append("(dominant_reason LIKE ? OR reasons_json LIKE ?)")
            params.extend([f"%{reason}%", f"%{reason}%"])
        params.append(max(1, int(limit)))
        query = (
            "SELECT * FROM quarantine_index WHERE " + " AND ".join(clauses)  # nosec B608
            + " ORDER BY timestamp DESC LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "tenant_id": r["tenant_id"],
                "contract_name": r["contract_name"],
                "timestamp": r["timestamp"],
                "kind": r["kind"],
                "artifact_path": r["artifact_path"],
                "row_count": r["row_count"],
                "dominant_reason": r["dominant_reason"],
                "reasons": _unjson(r["reasons_json"]),
            }
            for r in rows
        ]

    # ---------------------------------------------------------------- usage reads

    def usage_summary(
        self,
        tenant_id: str,
        contract_name: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        """Metering totals for a tenant, optionally since an ISO-8601 timestamp."""
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if contract_name:
            clauses.append("contract_name = ?")
            params.append(contract_name)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            row = conn.execute(
                (
                    # nosec B608 — reviewed usage builder; text is enum/validated
                    "\n"
                    "                SELECT\n"
                    "                    COUNT(*) AS total_runs,\n"
                    "                    COALESCE(SUM(units), 0) AS total_units,\n"
                    "                    COALESCE(SUM(rows_read), 0) AS rows_read,\n"
                    "                    COALESCE(SUM(rows_delivered), 0) AS rows_delivered,\n"
                    "                    COALESCE(SUM(bytes_processed), 0) AS bytes_processed,\n"
                    "                    SUM(CASE WHEN billable = 1 THEN units ELSE 0 END) AS billable_units,\n"
                    "                    SUM(CASE WHEN billable = 1 THEN 1 ELSE 0 END) AS billable_runs\n"
                    f"                FROM usage_records WHERE {where}\n"  # nosec B608
                ),
                params,
            ).fetchone()
        return {
            "tenant_id": tenant_id,
            "contract_name": contract_name,
            "since": since,
            "total_runs": int(row["total_runs"] or 0) if row else 0,
            "total_units": int(row["total_units"] or 0) if row else 0,
            "billable_units": int(row["billable_units"] or 0) if row else 0,
            "billable_runs": int(row["billable_runs"] or 0) if row else 0,
            "rows_read": int(row["rows_read"] or 0) if row else 0,
            "rows_delivered": int(row["rows_delivered"] or 0) if row else 0,
            "bytes_processed": int(row["bytes_processed"] or 0) if row else 0,
            "unit": "row",
        }

    # ---------------------------------------------------------------- retention

    def check_integrity(self) -> bool:
        """Return whether the ledger is queryable and structurally consistent.

        Used by readiness probes so an operator learns about a corrupt or
        locked history database before a scheduled run fails.
        """
        if self._init_db_failed:
            return False
        try:
            with self._connect() as conn:
                row = conn.execute("PRAGMA quick_check").fetchone()
                if not row or row[0] != "ok":
                    return False
                for table in ("run_history", "quarantine_index", "usage_records"):
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # nosec B608
            return True
        except sqlite3.Error:
            return False

    def prune(self, retention_days: int | None = None) -> dict[str, int]:
        """Delete ledger rows older than the retention window.

        A non-positive ``retention_days`` (the default) keeps history forever;
        the configured ``DATASHELL_HISTORY_RETENTION_DAYS`` is used otherwise.
        """
        days = settings.history_retention_days if retention_days is None else int(retention_days)
        if days <= 0:
            return {"run_history": 0, "quarantine_index": 0, "usage_records": 0}
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        removed: dict[str, int] = {}
        with self._connect() as conn:
            for table in ("run_history", "quarantine_index", "usage_records"):
                cursor = conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))  # nosec B608
                removed[table] = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        return removed

    # ----------------------------------------------------------------- internal

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        return {
            "run_id": row["run_id"],
            "tenant_id": row["tenant_id"],
            "contract_name": row["contract_name"],
            "contract_version": row["contract_version"] if "contract_version" in keys else None,
            "timestamp": row["timestamp"],
            "status": row["status"] if "status" in keys else None,
            "rows_read": row["rows_read"],
            "rows_delivered": row["rows_delivered"],
            "rows_refused": row["rows_refused"] if "rows_refused" in keys else 0,
            "rows_held": row["rows_held"] if "rows_held" in keys else 0,
            "unaccounted_for": row["unaccounted_for"] if "unaccounted_for" in keys else 0,
            "footing_matched": bool(row["footing_matched"]) if "footing_matched" in keys else True,
            "is_run_rejected": bool(row["is_run_rejected"]) if "is_run_rejected" in keys else False,
            "rejection_reason": row["rejection_reason"] if "rejection_reason" in keys else None,
            "duration_seconds": row["duration_seconds"] if "duration_seconds" in keys else None,
            "execution_mode": row["execution_mode"] if "execution_mode" in keys else None,
            "source_name": row["source_name"] if "source_name" in keys else None,
            "source_bytes": row["source_bytes"] if "source_bytes" in keys else None,
            "target_path": row["target_path"] if "target_path" in keys else None,
            "audit_path": row["audit_path"] if "audit_path" in keys else None,
            "quarantine_path": row["quarantine_path"] if "quarantine_path" in keys else None,
            "merkle_root": row["merkle_root"] if "merkle_root" in keys else None,
            "signature_key_id": row["signature_key_id"] if "signature_key_id" in keys else None,
            "metrics": _unjson(row["metrics_json"]),
            "rule_breakdown": _unjson(row["rule_breakdown_json"]) if "rule_breakdown_json" in keys else {},
            "structural_reasons": _unjson(row["structural_reasons_json"]) if "structural_reasons_json" in keys else {},
            "certificate_digest": row["certificate_digest"] if "certificate_digest" in keys else None,
            "ledger_status": row["ledger_status"] if "ledger_status" in keys and row["ledger_status"] else "COMMITTED",
        }

    # ------------------------------------------------------- pending outbox

    def list_pending_runs(
        self, tenant_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Runs published but not yet fully ledger-recorded (LEDGER-001).

        A ``PENDING`` row means artifacts reached durable storage while the
        ledger write was interrupted or failed; the outbox record on disk
        carries the payload needed for idempotent replay.
        """
        clauses = ["(ledger_status = 'PENDING')"]
        params: list[Any] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            try:
                rows = conn.execute(
                    "SELECT * FROM run_history WHERE " + " AND ".join(clauses)  # nosec B608
                    + " ORDER BY timestamp DESC LIMIT ?",
                    params,
                ).fetchall()
            except sqlite3.OperationalError:
                # Pre-migration database without ledger_status: no pending rows.
                return []
        return [self._run_dict(row) for row in rows]

    def mark_run_committed(self, run_id: str, tenant_id: str) -> bool:
        """Flip a replayed ``PENDING`` row to ``COMMITTED``. Returns True if found."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE run_history SET ledger_status = 'COMMITTED' "
                "WHERE run_id = ? AND tenant_id = ?",
                (run_id, tenant_id),
            )
        return bool(cursor.rowcount)


# -------------------------------------------------------------------
# Module-level compatibility wrappers so callers can import the durable
# outbox helpers without constructing a BoundaryLedger instance.
def pending_outbox_path(audit_dest: Path, run_id: str | None = None) -> Path:
    return BoundaryLedger.pending_outbox_path(audit_dest, run_id)


def write_pending_outbox(outbox_path: Path, payload: dict[str, Any]) -> Path:
    return BoundaryLedger.write_pending_outbox(outbox_path, payload)


def clear_pending_outbox(outbox_path: Path) -> None:
    BoundaryLedger.clear_pending_outbox(outbox_path)


def iter_pending_outboxes(root: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    return BoundaryLedger.iter_pending_outboxes(root)
