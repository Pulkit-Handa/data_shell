"""Durable settlement ledger.

Extends the boundary ledger's persistence model with the settlement domain:

* ``settlements`` — one immutable row per issued statement. Issued rows are
  never UPDATEd; corrections insert a new statement that references the
  superseding one. ``idempotency_key`` is unique per tenant so replayed
  calculation requests return the original statement instead of creating a
  duplicate.
* ``settlement_events`` — append-only lifecycle transitions (created,
  disputed, approved, superseded, cancelled, billing instruction created...).
* ``disputes`` — dispute records and their resolutions.
* ``billing_instructions`` — proposed external commercial actions. Distinct
  from a calculated liability, an invoice, or a confirmed payment; these rows
  record intent and external references only.
* ``contract_versions`` — immutable commercial contract versions with the
  digest each settlement binds to.

Limitations (documented, not claimed away): immutability is enforced at the
application level against ordinary callers. A privileged database
administrator with direct SQLite access can modify rows; no independently
verified tamper-evidence (for example a hash chain anchored externally) is
provided by this layer alone.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from datashell.core.config import settings
from datashell.settlement.models import (
    CommercialContract,
    Dispute,
    DisputeResolution,
    DisputeStatus,
    SettlementStatement,
    SettlementStatus,
)


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {})


class SettlementLedgerError(RuntimeError):
    """Raised on persistence-level settlement failures (fail closed)."""


class SettlementLedger:
    """Tenant-scoped SQLite store for settlement statements and billing actions."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or settings.history_db_path)
        self._init_db()

    # ---------------------------------------------------------------- internal

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settlements (
                    settlement_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    contract_version INTEGER NOT NULL,
                    contract_digest TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    certificate_digest TEXT NOT NULL,
                    calculation_json TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    gross_amount_minor INTEGER NOT NULL,
                    qualified_units INTEGER NOT NULL,
                    pricing_model TEXT NOT NULL,
                    calculation_version TEXT NOT NULL,
                    acceptance_status TEXT NOT NULL,
                    settlement_status TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    explanation TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    idempotency_key TEXT,
                    superseding_settlement_id TEXT,
                    billing_instruction_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_settlement_idempotency "
                "ON settlements(tenant_id, idempotency_key)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_settlement_run_contract "
                "ON settlements(run_id, contract_id, contract_version)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_settlements_tenant_status "
                "ON settlements(tenant_id, settlement_status)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settlement_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    settlement_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_settlement_events_settlement "
                "ON settlement_events(settlement_id, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS disputes (
                    dispute_id TEXT PRIMARY KEY,
                    settlement_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    disputing_party TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_references_json TEXT NOT NULL DEFAULT '[]',
                    supporting_explanation TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    resolution TEXT,
                    resolution_actor TEXT,
                    resolution_reason TEXT,
                    resolution_timestamp TEXT,
                    adjustment_settlement_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_disputes_settlement "
                "ON disputes(settlement_id, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS billing_instructions (
                    billing_instruction_id TEXT PRIMARY KEY,
                    settlement_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provider_invoice_id TEXT,
                    currency TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    last_provider_state TEXT,
                    request_meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_provider_ref "
                "ON billing_instructions(tenant_id, provider, provider_invoice_id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_idempotency "
                "ON billing_instructions(tenant_id, idempotency_key)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contract_versions (
                    contract_id TEXT NOT NULL,
                    contract_version INTEGER NOT NULL,
                    tenant_id TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, contract_id, contract_version)
                )
                """
            )

    # ------------------------------------------------------- contract versions

    def record_contract_version(self, contract: CommercialContract, tenant_id: str, created_by: str) -> None:
        """Persist an immutable commercial contract version with its digest.

        Re-saving an identical version is idempotent; a different payload for
        the same (id, version) raises, so terms referenced by a settlement can
        never be rewritten.
        """
        digest = contract.compute_digest()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT payload_json, digest FROM contract_versions "
                "WHERE tenant_id = ? AND contract_id = ? AND contract_version = ?",
                (tenant_id, contract.contract_id, contract.contract_version),
            ).fetchone()
            if existing:
                if existing["payload_json"] != _json(contract.canonical_payload()) or existing["digest"] != digest:
                    raise SettlementLedgerError(
                        f"Commercial contract '{contract.contract_id}' v{contract.contract_version} "
                        "already exists and is immutable; create a new version"
                    )
                return
            conn.execute(
                "INSERT INTO contract_versions "
                "(contract_id, contract_version, tenant_id, digest, payload_json, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    contract.contract_id, contract.contract_version, tenant_id, digest,
                    _json(contract.canonical_payload()), _utcnow(), created_by,
                ),
            )

    def get_contract_version(self, contract_id: str, contract_version: int, tenant_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM contract_versions "
                "WHERE tenant_id = ? AND contract_id = ? AND contract_version = ?",
                (tenant_id, contract_id, contract_version),
            ).fetchone()
        if not row:
            return None
        return {
            "contract_id": row["contract_id"],
            "contract_version": row["contract_version"],
            "tenant_id": row["tenant_id"],
            "digest": row["digest"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
            "created_by": row["created_by"],
        }

    def list_contract_versions(self, contract_id: str, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT contract_id, contract_version, digest, created_at, created_by "
                "FROM contract_versions WHERE tenant_id = ? AND contract_id = ? "
                "ORDER BY contract_version",
                (tenant_id, contract_id),
            ).fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- settlements

    def has_open_dispute(self, settlement_id: str, tenant_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM disputes WHERE settlement_id = ? AND tenant_id = ? "
                "AND status IN ('OPEN', 'UNDER_REVIEW') LIMIT 1",
                (settlement_id, tenant_id),
            ).fetchone()
        return row is not None

    def find_by_idempotency_key(self, tenant_id: str, idempotency_key: str) -> dict[str, Any] | None:
        """Replay detection: the same key returns the original statement."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM settlements WHERE tenant_id = ? AND idempotency_key = ?",
                (tenant_id, idempotency_key),
            ).fetchone()
        return self._settlement_dict(row) if row else None

    def save_settlement(self, statement: SettlementStatement) -> bool:
        """Insert an immutable settlement statement (idempotent on its ID).

        Returns True when a new row was inserted and False when an identical
        statement already existed (the caller reports ``created=False`` so a
        replay is never misreported as a new financial record).

        The statement ID is deterministic in (run, contract version), so a
        re-calculation of the same logical request produces the same ID. When
        the stored row carries no idempotency key but the incoming statement
        does, the key is recorded on the existing row (the calculation is
        deterministic and identical; no financial evidence changes). When the
        same idempotency key already exists for a *different* statement, the
        unique index raises and the caller must fail closed.
        """
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT settlement_id, idempotency_key, gross_amount_minor, settlement_status "
                "FROM settlements WHERE settlement_id = ?",
                (statement.settlement_id,),
            ).fetchone()
            if existing:
                if existing["idempotency_key"] is None and statement.idempotency_key:
                    conn.execute(
                        "UPDATE settlements SET idempotency_key = ? WHERE settlement_id = ?",
                        (statement.idempotency_key, statement.settlement_id),
                    )
                # A different existing key for the same deterministic statement
                # is a distinct request that maps to the same settlement; the
                # statement itself is unchanged, so this is still idempotent.
                return False
            conn.execute(
                """
                INSERT INTO settlements (
                    settlement_id, tenant_id, contract_id, contract_version, contract_digest,
                    run_id, certificate_digest, calculation_json, currency, gross_amount_minor,
                    qualified_units, pricing_model, calculation_version, acceptance_status,
                    settlement_status, reason_codes_json, explanation, actor, idempotency_key,
                    superseding_settlement_id, billing_instruction_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    statement.settlement_id, statement.tenant_id, statement.contract_id,
                    statement.contract_version, statement.contract_digest, statement.run_id,
                    statement.certificate_digest, _json(statement.calculation.model_dump()),
                    statement.currency, statement.calculation.gross_amount_minor,
                    statement.calculation.qualified_units, statement.calculation.pricing_model.value,
                    statement.calculation.calculation_version, statement.acceptance_status.value,
                    statement.settlement_status.value, _json([r.value for r in statement.reason_codes]),
                    statement.explanation, statement.actor, statement.idempotency_key,
                    statement.superseding_settlement_id, statement.billing_instruction_id,
                    statement.created_at,
                ),
            )
            self._insert_event(conn, statement.settlement_id, statement.tenant_id,
                               "CREATED", None, statement.settlement_status.value, statement.actor,
                               f"settlement calculated: "
                               f"{statement.calculation.gross_amount_minor} minor {statement.currency}")
        return True

    def _insert_event(self, conn: sqlite3.Connection, settlement_id: str, tenant_id: str,
                      event_type: str, from_status: str | None, to_status: str | None,
                      actor: str, reason: str, payload: dict[str, Any] | None = None) -> None:
        conn.execute(
            "INSERT INTO settlement_events "
            "(settlement_id, tenant_id, event_type, from_status, to_status, actor, reason, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (settlement_id, tenant_id, event_type, from_status, to_status, actor, reason,
             _json(payload or {}), _utcnow()),
        )

    def get_settlement(self, settlement_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM settlements WHERE settlement_id = ? AND tenant_id = ?",
                (settlement_id, tenant_id),
            ).fetchone()
        return self._settlement_dict(row) if row else None

    def list_settlements(self, tenant_id: str, status: str | None = None,
                         contract_id: str | None = None, limit: int = 50, offset: int = 0
                         ) -> list[dict[str, Any]]:
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if status:
            clauses.append("settlement_status = ?")
            params.append(status)
        if contract_id:
            clauses.append("contract_id = ?")
            params.append(contract_id)
        params.extend([max(1, int(limit)), max(0, int(offset))])
        query = ("SELECT * FROM settlements WHERE " + " AND ".join(clauses)  # nosec B608
                 + " ORDER BY created_at DESC LIMIT ? OFFSET ?")
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._settlement_dict(r) for r in rows]

    def transition_status(self, settlement_id: str, tenant_id: str, to_status: SettlementStatus,
                          actor: str, reason: str, *, event_type: str = "STATUS_TRANSITION",
                          payload: dict[str, Any] | None = None) -> bool:
        """Record a lifecycle transition as an append-only event.

        Historical rows are not rewritten: the event log carries the
        transition history, and the settlement row's status column reflects the
        current state. Returns False when the settlement does not exist for the
        tenant.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT settlement_status FROM settlements WHERE settlement_id = ? AND tenant_id = ?",
                (settlement_id, tenant_id),
            ).fetchone()
            if not row:
                return False
            from_status = row["settlement_status"]
            conn.execute(
                "UPDATE settlements SET settlement_status = ? WHERE settlement_id = ? AND tenant_id = ?",
                (to_status.value, settlement_id, tenant_id),
            )
            self._insert_event(conn, settlement_id, tenant_id, event_type, from_status,
                               to_status.value, actor, reason, payload)
        return True

    def get_events(self, settlement_id: str, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM settlement_events WHERE settlement_id = ? AND tenant_id = ? "
                "ORDER BY event_id",
                (settlement_id, tenant_id),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ disputes

    def save_dispute(self, dispute: Dispute) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO disputes (
                    dispute_id, settlement_id, tenant_id, disputing_party, reason,
                    evidence_references_json, supporting_explanation, status, resolution,
                    resolution_actor, resolution_reason, resolution_timestamp,
                    adjustment_settlement_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dispute.dispute_id, dispute.settlement_id, dispute.tenant_id,
                    dispute.disputing_party, dispute.reason,
                    _json(dispute.evidence_references), dispute.supporting_explanation,
                    dispute.status.value,
                    dispute.resolution.value if dispute.resolution else None,
                    dispute.resolution_actor, dispute.resolution_reason,
                    dispute.resolution_timestamp, dispute.adjustment_settlement_id,
                    dispute.created_at, dispute.updated_at,
                ),
            )

    def update_dispute_resolution(self, dispute_id: str, tenant_id: str, *,
                                  status: DisputeStatus, resolution: DisputeResolution,
                                  resolution_actor: str, resolution_reason: str,
                                  adjustment_settlement_id: str | None = None) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE disputes SET status = ?, resolution = ?, resolution_actor = ?, "
                "resolution_reason = ?, resolution_timestamp = ?, adjustment_settlement_id = ?, updated_at = ? "
                "WHERE dispute_id = ? AND tenant_id = ?",
                (status.value, resolution.value, resolution_actor, resolution_reason,
                 _utcnow(), adjustment_settlement_id, _utcnow(), dispute_id, tenant_id),
            )
            return cur.rowcount > 0

    def get_dispute(self, dispute_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM disputes WHERE dispute_id = ? AND tenant_id = ?",
                (dispute_id, tenant_id),
            ).fetchone()
        return self._dispute_dict(row) if row else None

    def list_disputes(self, tenant_id: str, settlement_id: str | None = None,
                      status: str | None = None) -> list[dict[str, Any]]:
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if settlement_id:
            clauses.append("settlement_id = ?")
            params.append(settlement_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM disputes WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC",  # nosec B608
                params,
            ).fetchall()
        return [self._dispute_dict(r) for r in rows]

    # -------------------------------------------------------- billing instructions

    def save_billing_instruction(self, *, billing_instruction_id: str, settlement_id: str,
                                 tenant_id: str, provider: str, currency: str,
                                 amount_minor: int, idempotency_key: str,
                                 provider_invoice_id: str | None = None,
                                 status: str = "DRAFT", request_meta: dict[str, Any] | None = None) -> None:
        """Persist a proposed external billing action.

        Uniqueness on (tenant, provider, provider_invoice_id) prevents a
        provider-side duplicate from ever being recorded twice; uniqueness on
        (tenant, idempotency_key) makes retries safe.
        """
        now = _utcnow()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO billing_instructions (
                    billing_instruction_id, settlement_id, tenant_id, provider,
                    provider_invoice_id, currency, amount_minor, status, idempotency_key,
                    retry_count, last_error, request_meta_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?)
                """,
                (billing_instruction_id, settlement_id, tenant_id, provider, provider_invoice_id,
                 currency, amount_minor, status, idempotency_key, _json(request_meta or {}), now, now),
            )

    def find_billing_instruction(self, tenant_id: str, provider: str,
                                 provider_invoice_id: str) -> dict[str, Any] | None:
        """Locate an instruction by its provider reference (dedup/reconciliation)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM billing_instructions WHERE tenant_id = ? AND provider = ? "
                "AND provider_invoice_id = ?",
                (tenant_id, provider, provider_invoice_id),
            ).fetchone()
        return dict(row) if row else None

    def find_billing_by_idempotency(self, tenant_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM billing_instructions WHERE tenant_id = ? AND idempotency_key = ?",
                (tenant_id, idempotency_key),
            ).fetchone()
        return dict(row) if row else None

    def update_billing_instruction(self, billing_instruction_id: str, tenant_id: str, **fields: Any) -> bool:
        """Record external progress; never alters the referenced settlement."""
        allowed = {"status", "provider_invoice_id", "retry_count", "last_error", "last_provider_state"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        sets = ", ".join(f"{k} = ?" for k in updates)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE billing_instructions SET {sets}, updated_at = ? "  # nosec B608
                "WHERE billing_instruction_id = ? AND tenant_id = ?",
                (*updates.values(), _utcnow(), billing_instruction_id, tenant_id),
            )
            return cur.rowcount > 0

    def get_billing_instruction(self, billing_instruction_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM billing_instructions WHERE billing_instruction_id = ? AND tenant_id = ?",
                (billing_instruction_id, tenant_id),
            ).fetchone()
        return dict(row) if row else None

    def list_billing_instructions(self, tenant_id: str, status: str | None = None) -> list[dict[str, Any]]:
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM billing_instructions WHERE " + " AND ".join(clauses)  # nosec B608
                + " ORDER BY created_at DESC",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- dict mapping

    @staticmethod
    def _settlement_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "settlement_id": row["settlement_id"],
            "tenant_id": row["tenant_id"],
            "contract_id": row["contract_id"],
            "contract_version": row["contract_version"],
            "contract_digest": row["contract_digest"],
            "run_id": row["run_id"],
            "certificate_digest": row["certificate_digest"],
            "calculation": json.loads(row["calculation_json"]),
            "currency": row["currency"],
            "gross_amount_minor": row["gross_amount_minor"],
            "qualified_units": row["qualified_units"],
            "pricing_model": row["pricing_model"],
            "calculation_version": row["calculation_version"],
            "acceptance_status": row["acceptance_status"],
            "settlement_status": row["settlement_status"],
            "reason_codes": json.loads(row["reason_codes_json"]),
            "explanation": row["explanation"],
            "actor": row["actor"],
            "idempotency_key": row["idempotency_key"],
            "superseding_settlement_id": row["superseding_settlement_id"],
            "billing_instruction_id": row["billing_instruction_id"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _dispute_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "dispute_id": row["dispute_id"],
            "settlement_id": row["settlement_id"],
            "tenant_id": row["tenant_id"],
            "disputing_party": row["disputing_party"],
            "reason": row["reason"],
            "evidence_references": json.loads(row["evidence_references_json"]),
            "supporting_explanation": row["supporting_explanation"],
            "status": row["status"],
            "resolution": row["resolution"],
            "resolution_actor": row["resolution_actor"],
            "resolution_reason": row["resolution_reason"],
            "resolution_timestamp": row["resolution_timestamp"],
            "adjustment_settlement_id": row["adjustment_settlement_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
