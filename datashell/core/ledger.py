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
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from datashell.core.config import settings


# Columns added to ``run_history`` after its original two-field-per-run form.
# Declared as an ordered list so migration is an idempotent ALTER TABLE loop;
# this keeps databases written by earlier releases readable.
_RUN_HISTORY_MIGRATIONS: List[tuple[str, str]] = [
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
]

# Runs recorded before the ledger captured ``status`` implicitly succeeded.
_DELIVERED_FILTER = "(status IS NULL OR status = 'LOADED')"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {})


def _unjson(value: Any) -> Dict[str, Any]:
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

    def __init__(self, db_path: Optional[Path] = None):
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

    # --------------------------------------------------------------- run writes

    def record_run(
        self,
        run_id: str,
        contract_name: str,
        rows_read: int,
        rows_delivered: int,
        metrics: Dict[str, float],
        tenant_id: str = "default-tenant",
        *,
        contract_version: Optional[str] = None,
        status: Optional[str] = None,
        rows_refused: int = 0,
        rows_held: int = 0,
        unaccounted_for: int = 0,
        footing_matched: bool = True,
        is_run_rejected: bool = False,
        rejection_reason: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        execution_mode: Optional[str] = None,
        source_name: Optional[str] = None,
        source_bytes: Optional[int] = None,
        target_path: Optional[str] = None,
        audit_path: Optional[str] = None,
        quarantine_path: Optional[str] = None,
        merkle_root: Optional[str] = None,
        signature_key_id: Optional[str] = None,
        rule_breakdown: Optional[Dict[str, int]] = None,
        structural_reasons: Optional[Dict[str, int]] = None,
    ) -> None:
        """Persist one forensic ledger row for an enforcement run.

        Idempotent on ``run_id`` so a retried finalization cannot double-count.
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
                    rule_breakdown_json, structural_reasons_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    , ?, ?, ?, ?, ?, ?
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
                ),
            )

    def record_quarantine(
        self,
        run_id: str,
        tenant_id: str,
        contract_name: Optional[str],
        kind: str,
        artifact_path: Optional[str],
        row_count: int,
        reasons: Optional[Dict[str, int]] = None,
    ) -> None:
        """Index one quarantine artifact (held rows or structural refusals)."""
        reason_map = reasons or {}
        dominant = max(reason_map, key=reason_map.get) if reason_map else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO quarantine_index (
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
        contract_name: Optional[str],
        status: Optional[str],
        execution_mode: Optional[str],
        rows_read: int,
        rows_delivered: int,
        bytes_processed: int = 0,
        billable: bool = True,
        unit: str = "row",
        units: Optional[int] = None,
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
    ) -> Optional[Dict[str, Any]]:
        """Most recent *delivered* run for a contract (drift baseline source)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id, tenant_id, timestamp, rows_read, rows_delivered, metrics_json "
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
    ) -> Optional[Dict[str, Any]]:
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
                "SELECT rows_delivered, metrics_json FROM run_history "
                f"WHERE tenant_id = ? AND contract_name = ? AND {_DELIVERED_FILTER} "
                "AND timestamp > ? ORDER BY timestamp DESC LIMIT ?",
                (tenant_id, contract_name, cutoff, window),
            ).fetchall()
        if not rows:
            return None

        delivered = [float(r["rows_delivered"] or 0) for r in rows]
        metric_samples: Dict[str, List[float]] = {}
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

    def get_run(self, run_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
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
        contract_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Newest-first run records for a tenant with optional filters."""
        clauses = ["tenant_id = ?"]
        params: List[Any] = [tenant_id]
        if contract_name:
            clauses.append("contract_name = ?")
            params.append(contract_name)
        if status:
            clauses.append("status = ?")
            params.append(status)
        params.extend([max(1, int(limit)), max(0, int(offset))])
        query = (
            "SELECT * FROM run_history WHERE " + " AND ".join(clauses)
            + " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        )
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._run_dict(row) for row in rows]

    def count_runs(
        self,
        tenant_id: str,
        contract_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        """Total runs matching the same filters accepted by :meth:`list_runs`."""
        clauses = ["tenant_id = ?"]
        params: List[Any] = [tenant_id]
        if contract_name:
            clauses.append("contract_name = ?")
            params.append(contract_name)
        if status:
            clauses.append("status = ?")
            params.append(status)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM run_history WHERE " + " AND ".join(clauses),
                params,
            ).fetchone()
        return int(row["n"]) if row else 0

    def history_summary(
        self, tenant_id: str, contract_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Aggregate delivery health for a tenant (optionally one contract)."""
        clauses = ["tenant_id = ?"]
        params: List[Any] = [tenant_id]
        if contract_name:
            clauses.append("contract_name = ?")
            params.append(contract_name)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total_runs,
                    SUM(CASE WHEN status = 'LOADED' OR status IS NULL THEN 1 ELSE 0 END) AS delivered_runs,
                    SUM(CASE WHEN is_run_rejected = 1 THEN 1 ELSE 0 END) AS rejected_runs,
                    COALESCE(SUM(rows_read), 0) AS rows_read,
                    COALESCE(SUM(rows_delivered), 0) AS rows_delivered,
                    COALESCE(SUM(rows_refused), 0) AS rows_refused,
                    COALESCE(SUM(rows_held), 0) AS rows_held,
                    SUM(CASE WHEN footing_matched = 0 THEN 1 ELSE 0 END) AS footing_mismatches,
                    MAX(timestamp) AS last_run_at
                FROM run_history WHERE {where}
                """,
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
        contract_name: Optional[str] = None,
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        """Chronological (oldest-first) slice of recent runs for trend charts."""
        clauses = ["tenant_id = ?"]
        params: List[Any] = [tenant_id]
        if contract_name:
            clauses.append("contract_name = ?")
            params.append(contract_name)
        params.append(max(1, int(limit)))
        query = (
            "SELECT run_id, timestamp, status, rows_read, rows_delivered, "
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
        contract_name: Optional[str] = None,
        run_id: Optional[str] = None,
        kind: Optional[str] = None,
        reason: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Search the quarantine index by contract, run, kind, or dominant reason."""
        clauses = ["tenant_id = ?"]
        params: List[Any] = [tenant_id]
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
            "SELECT * FROM quarantine_index WHERE " + " AND ".join(clauses)
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
        contract_name: Optional[str] = None,
        since: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Metering totals for a tenant, optionally since an ISO-8601 timestamp."""
        clauses = ["tenant_id = ?"]
        params: List[Any] = [tenant_id]
        if contract_name:
            clauses.append("contract_name = ?")
            params.append(contract_name)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total_runs,
                    COALESCE(SUM(units), 0) AS total_units,
                    COALESCE(SUM(rows_read), 0) AS rows_read,
                    COALESCE(SUM(rows_delivered), 0) AS rows_delivered,
                    COALESCE(SUM(bytes_processed), 0) AS bytes_processed,
                    SUM(CASE WHEN billable = 1 THEN units ELSE 0 END) AS billable_units,
                    SUM(CASE WHEN billable = 1 THEN 1 ELSE 0 END) AS billable_runs
                FROM usage_records WHERE {where}
                """,
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
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            return True
        except sqlite3.Error:
            return False

    def prune(self, retention_days: Optional[int] = None) -> Dict[str, int]:
        """Delete ledger rows older than the retention window.

        A non-positive ``retention_days`` (the default) keeps history forever;
        the configured ``DATASHELL_HISTORY_RETENTION_DAYS`` is used otherwise.
        """
        days = settings.history_retention_days if retention_days is None else int(retention_days)
        if days <= 0:
            return {"run_history": 0, "quarantine_index": 0, "usage_records": 0}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        removed: Dict[str, int] = {}
        with self._connect() as conn:
            for table in ("run_history", "quarantine_index", "usage_records"):
                cursor = conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
                removed[table] = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        return removed

    # ----------------------------------------------------------------- internal

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> Dict[str, Any]:
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
        }
