"""Enterprise Boundary Pipeline Runner: Coordinates zero-copy Arrow memory
transport, streaming, and cryptographic gates."""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from datashell.contract.models import DataContract, QualityBehavior
from datashell.core.commit import (
    publish_transactionally,
    recover_pending_publications,
    recover_publication,
)
from datashell.core.config import settings
from datashell.core.crypto import CryptoEngine
from datashell.core.events import emit_run_event
from datashell.core.fileops import (
    ensure_disk_headroom,
    materialize_artifact_copy,
    parquet_compression_codec,
)
from datashell.core.ledger import (
    BoundaryLedger,
    clear_pending_outbox,
    iter_pending_outboxes,
    pending_outbox_path,
    write_pending_outbox,
)
from datashell.core.sandbox import FilesystemSandbox
from datashell.core.telemetry import (
    ACTIVE_RUNS,
    FOOTING_ERRORS_TOTAL,
    ROWS_DELIVERED_TOTAL,
    ROWS_HELD_TOTAL,
    ROWS_READ_TOTAL,
    ROWS_REFUSED_TOTAL,
    RUN_DURATION_SECONDS,
    RUNS_TOTAL,
    telemetry_logger,
)
from datashell.engines.delivery import DeliveryEngine, DeliveryOutcome, TerminalStatus, safe_json_dumps
from datashell.engines.drift import ContractHistoryTracker, DriftCheckResult, HistoricalDriftEngine
from datashell.engines.ingestion import IngestionResult, StructuralIngestionEngine
from datashell.engines.metrics import compute_metrics, compute_metrics_from_parquet
from datashell.engines.quality import QualityResult, SemanticQualityEngine
from datashell.engines.reconciler import Reconciler, ReconciliationLedger
from datashell.pipeline.artifacts import ArtifactGenerator


def _quarantine_rows_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the quarantine index rows carried by an outbox payload."""
    rows: list[dict[str, Any]] = []
    for artifact in payload.get("quarantine_files") or []:
        name = Path(str(artifact)).name
        is_held = name.endswith("_held_records.parquet")
        rmap = payload.get("rule_breakdown") if is_held else payload.get("structural_reasons")
        n = payload.get("rows_held", 0) if is_held else payload.get("rows_refused", 0)
        rows.append(
            {
                "kind": "held_quality" if is_held else "structural_refused",
                "artifact_path": str(artifact),
                "row_count": int(n or 0),
                "reasons": dict(rmap or {}),
            }
        )
    return rows


def _publish_with_pending_outbox(
    *,
    pairs: list[tuple[Path, Path]],
    run_id: str,
    transaction_root: Path,
    generation_pointer: dict[str, Any],
    outbox_path: Path,
    payload: dict[str, Any],
) -> None:
    """Write the pending ledger record, publish, and discard it on failure.

    The pending record must exist *before* publication so that a crash
    between publication and the ledger write stays replayable. If publication
    itself fails, nothing was published, so the record is removed instead:
    leaving it would make every later recovery re-report a delivery that never
    happened. The outbox record is per-run, so a concurrent run's record is
    never disturbed by this cleanup.
    """
    write_pending_outbox(outbox_path, payload)
    try:
        publish_transactionally(
            pairs,
            run_id=run_id,
            transaction_root=transaction_root,
            generation_pointer=generation_pointer,
        )
    except Exception:
        clear_pending_outbox(outbox_path)
        raise


def _replay_payload_without_pipeline(store: BoundaryLedger, payload: dict[str, Any]) -> None:
    """Apply an outbox payload directly when no contract catalog is available.

    The run row, its quarantine index rows, and its usage meter are written in
    one transaction, so a replay can never leave a run marked ``COMMITTED``
    with incomplete accounting.
    """
    store.apply_run_outcome(
        run_id=str(payload.get("run_id")),
        tenant_id=str(payload.get("tenant_id")),
        contract_name=str(payload.get("contract_name")),
        rows_read=int(payload.get("rows_read", 0) or 0),
        rows_delivered=int(payload.get("rows_delivered", 0) or 0),
        metrics=dict(payload.get("metrics") or {}),
        contract_version=payload.get("contract_version"),
        status=payload.get("status"),
        rows_refused=int(payload.get("rows_refused", 0) or 0),
        rows_held=int(payload.get("rows_held", 0) or 0),
        unaccounted_for=int(payload.get("unaccounted_for", 0) or 0),
        footing_matched=bool(payload.get("footing_matched", True)),
        is_run_rejected=bool(payload.get("is_run_rejected", False)),
        rejection_reason=payload.get("rejection_reason"),
        duration_seconds=payload.get("duration_seconds"),
        execution_mode=payload.get("execution_mode"),
        source_name=payload.get("source_name"),
        source_bytes=payload.get("source_bytes"),
        target_path=payload.get("target_path"),
        audit_path=payload.get("audit_path"),
        quarantine_path=payload.get("quarantine_path"),
        merkle_root=payload.get("merkle_root"),
        signature_key_id=payload.get("signature_key_id"),
        certificate_digest=payload.get("certificate_digest"),
        rule_breakdown=dict(payload.get("rule_breakdown") or {}),
        structural_reasons=dict(payload.get("structural_reasons") or {}),
        quarantine_rows=_quarantine_rows_from_payload(payload),
        billable=payload.get("status") != TerminalStatus.FAILED.value,
        units=int(payload.get("rows_read", 0) or 0),
    )


def _published_artifacts_present(payload: dict[str, Any]) -> bool:
    """Return True when the run's published artifacts are actually on disk.

    The pending outbox is written *before* publication. If publication then
    fails, the outbox record survives but the artifacts it describes were
    never published. Replaying such a record would fabricate a delivery that
    never happened, so replay verifies the run's own immutable artifacts
    (the lineage copy and the audit certificate, both named by ``run_id``)
    exist before the run is recorded as committed.

    A run with no artifacts (a rejected run publishes no delivery target) is
    replayed as-is: its outcome is recorded from the payload.
    """
    audit_path = payload.get("audit_path")
    run_artifact = payload.get("run_artifact_path")
    if not audit_path and not run_artifact:
        return True
    for candidate in (audit_path, run_artifact):
        if not candidate:
            continue
        try:
            if not Path(str(candidate)).is_file():
                return False
        except OSError:
            return False
    return True


def reconcile_pending_ledger_entries(
    storage_root: Path | None = None,
    ledger: BoundaryLedger | None = None,
) -> dict[str, int]:
    """Replay durable outbox records into the ledger (LEDGER-001 recovery).

    Scans ``storage_root`` for pending-outbox records left by runs whose
    publication succeeded but whose ledger write did not complete, and
    re-applies each payload idempotently. Successfully replayed outboxes are
    removed; malformed, tenant-invalid, or unpublished records are left for
    the operator and counted as skipped.

    Reconciliation never raises - a database outage during startup must not
    prevent the service from booting - but every skip is reported through the
    returned counts and an ERROR log, so unreconciled state is observable
    rather than silently swallowed.
    """
    from datashell.core.telemetry import telemetry_logger as _telemetry

    root = Path(storage_root) if storage_root is not None else settings.output_dir
    store = ledger if ledger is not None else BoundaryLedger()
    replayed = 0
    skipped = 0
    try:
        outboxes = list(iter_pending_outboxes(root))
    except Exception as exc:
        _telemetry.error(
            "Pending ledger reconciliation could not enumerate outbox records",
            storage_root=str(root),
            error_type=type(exc).__name__,
        )
        return {"replayed": 0, "skipped": 0}
    for outbox_path, payload in outboxes:
        try:
            tenant_id = str(payload.get("tenant_id") or "")
            run_id = str(payload.get("run_id") or "")
            if not tenant_id or not run_id:
                skipped += 1
                _telemetry.error(
                    "Pending ledger entry is malformed and was skipped",
                    outbox=str(outbox_path),
                )
                continue
            try:
                settings.tenant_storage_key(tenant_id)
            except Exception:
                skipped += 1
                _telemetry.error(
                    "Pending ledger entry has an invalid tenant and was skipped",
                    outbox=str(outbox_path),
                    run_id=run_id,
                )
                continue
            try:
                existing = store.get_run(run_id, tenant_id)
            except Exception:
                existing = None
            if existing is not None and existing.get("ledger_status", "COMMITTED") == "COMMITTED":
                clear_pending_outbox(outbox_path)
                continue
            if not _published_artifacts_present(payload):
                # The outbox record outlived a publication that never
                # committed. Recording it would fabricate a delivery, so the
                # record is left for an operator to inspect.
                skipped += 1
                _telemetry.error(
                    "Pending ledger entry describes artifacts that were never "
                    "published; left for operator review",
                    outbox=str(outbox_path),
                    run_id=run_id,
                    tenant=tenant_id,
                )
                continue
            _replay_payload_without_pipeline(store, payload)
            store.mark_run_committed(run_id, tenant_id)
            clear_pending_outbox(outbox_path)
            replayed += 1
            _telemetry.info("Reconciled pending ledger entry", run_id=run_id, tenant=tenant_id)
        except Exception as exc:
            skipped += 1
            _telemetry.error(
                "Pending ledger entry could not be reconciled",
                outbox=str(outbox_path),
                error_type=type(exc).__name__,
            )
            continue
    return {"replayed": replayed, "skipped": skipped}


# Execution backends both runner entry points may select. ``in_memory`` loads
# one vectorized batch; ``out_of_core_streaming`` pushes bounded micro-batches
# through the quality/delivery gates without materializing the full feed.
EXECUTION_IN_MEMORY = "in_memory"
EXECUTION_STREAMING = "out_of_core_streaming"

INGEST_MODES = ("auto", "memory", "stream")


def choose_execution_backend(
    source_size_bytes: int,
    *,
    force_streaming: bool | None = None,
    ingest_mode: str = "auto",
    auto_threshold_bytes: int = 64 * 1024 * 1024,
) -> str:
    """Resolve which ingestion backend serves a run.

    Precedence: an explicit caller decision (``force_streaming`` — the CLI
    ``--streaming/--no-streaming`` flag or an API request field) wins; the
    ``DATASHELL_INGEST_MODE`` environment policy is next; the size threshold
    (``STREAM_AUTO_THRESHOLD_BYTES``) auto-promotes large feeds last. This
    keeps every deployment's capacity policy in configuration instead of
    caller code.
    """
    if force_streaming is True:
        return EXECUTION_STREAMING
    if force_streaming is False:
        return EXECUTION_IN_MEMORY
    if ingest_mode == "stream":
        return EXECUTION_STREAMING
    if ingest_mode == "memory":
        return EXECUTION_IN_MEMORY
    if source_size_bytes >= auto_threshold_bytes:
        return EXECUTION_STREAMING
    return EXECUTION_IN_MEMORY


def recover_pending_publications_and_ledger(
    storage_root: Path | None = None,
    ledger: BoundaryLedger | None = None,
) -> dict[str, int]:
    """Startup recovery gate: publication journals first, then ledger outbox.

    Returns a summary containing the number of journals recovered, the number
    of ledger entries replayed or skipped, and the number of journal recovery
    errors. Journal recovery errors are counted rather than raised so the
    service can still start, but they are reported so an operator sees that
    recovery was incomplete.
    """
    root = Path(storage_root) if storage_root is not None else settings.output_dir
    journal_errors = 0
    try:
        journals = int(recover_pending_publications(root))
    except Exception:
        journals = 0
        journal_errors = 1
    result = reconcile_pending_ledger_entries(storage_root=root, ledger=ledger)
    return {"journals": journals, "journal_errors": journal_errors, **result}


def _common_transaction_root(paths: list[Path]) -> Path:
    """Find a stable writable root for recoverable multi-directory publication."""
    resolved_paths = [Path(path).resolve() for path in paths]
    if settings.is_production_mode:
        root = settings.output_dir.resolve()
        if any(not path.is_relative_to(root) for path in resolved_paths):
            raise ValueError(
                "Production delivery targets and audit certificates "
                "must remain under DATASHELL_OUTPUT_DIR")
    else:
        parents = [path.parent for path in resolved_paths]
        root = Path(os.path.commonpath([str(parent) for parent in parents])).resolve()
        if root == Path(root.anchor):
            raise ValueError("Delivery target and audit certificate must share a storage root")
    if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
        raise OSError("The common transaction root is not writable")
    return root


@dataclass
class PipelineResult:
    run_id: str
    status: TerminalStatus
    duration_seconds: float
    ledger: ReconciliationLedger
    outcome: DeliveryOutcome
    audit_artifact_path: str | None
    audit_certificate: dict[str, Any] | None = None
    ledger_status: str = "COMMITTED"


class DataShellPipeline:
    """
    Enterprise boundary enforcement pipeline.
    Ensures bad data never lands in target storage, and never leaves to regulators.
    Supports in-memory, file, and out-of-core streaming execution.

    Historical drift is opt-in (``DRIFT_ENABLED=false`` by default) because
    cross-run comparisons are only meaningful once a feed has a stable history;
    a small test run recorded first must never become the baseline that rejects
    a subsequent production delivery. Pass ``drift_enabled=False`` (for example
    from the CLI ``--no-drift`` flag) to force drift off for one pipeline
    instance, or ``True`` to force it on.
    """

    def __init__(
        self,
        contract: DataContract,
        tenant_id: str = "default-tenant",
        drift_enabled: bool | None = None,
    ):
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("tenant_id must be a non-empty authenticated tenant identifier")
        # Validate and normalize the filesystem representation at the boundary;
        # the tenant value itself remains the identity used for auth/audit/history.
        settings.tenant_storage_key(tenant_id)
        self.contract = contract
        self.tenant_id = tenant_id
        self._drift_enabled_override = drift_enabled
        self.ingestion_engine = StructuralIngestionEngine(contract)
        self.quality_engine = SemanticQualityEngine(contract)
        self.delivery_engine = DeliveryEngine(contract, tenant_id=self.tenant_id)
        self.history_tracker = ContractHistoryTracker()
        self.drift_engine = HistoricalDriftEngine(history_tracker=self.history_tracker)

    def _evaluate_drift(
        self,
        *,
        contract_name: str,
        current_rows: int,
        current_metrics: dict[str, float],
    ) -> list[DriftCheckResult]:
        """Run historical drift checks when enabled, honoring a baseline reset.

        Returns an empty list when drift is disabled, in which case no baseline
        is consulted and no drift evaluation is recorded. When enabled and
        ``DRIFT_BASELINE_RESET`` is truthy, the contract/tenant baseline is
        cleared first so the current run establishes a fresh comparison point.
        """
        enabled = settings.drift_enabled if self._drift_enabled_override is None else self._drift_enabled_override
        if not enabled:
            return []
        if settings.drift_baseline_reset:
            self.history_tracker.reset_baseline(contract_name=contract_name, tenant_id=self.tenant_id)
        return self.drift_engine.evaluate_drift(
            contract_name=contract_name,
            current_rows=current_rows,
            current_metrics=current_metrics,
            tenant_id=self.tenant_id,
            max_volume_drop_pct=settings.drift_max_volume_drop_pct,
            max_metric_drift_pct=settings.drift_max_metric_drift_pct,
            baseline_window=settings.drift_baseline_window,
            min_metric_baseline_samples=settings.drift_metric_min_baseline_samples,
            max_rate_abs_delta=settings.drift_max_rate_abs_delta,
        )

    @staticmethod
    def _rejection_reason(
        batch_results: list[Any] | None,
        drift_checks: list[DriftCheckResult] | None,
        *,
        held_rows: int = 0,
        behavior: QualityBehavior | None = None,
        footing_ok: bool = True,
    ) -> str:
        """Build an operator-actionable rejection reason naming every failed gate.

        Names the batch rule, its actual and expected values, failing drift
        metrics, and row-policy violations so a rejection is diagnosable from
        the CLI output and the ledger without opening the certificate.
        """
        reasons: list[str] = []
        for batch in batch_results or []:
            if getattr(batch, "passed", True):
                continue
            check = str(getattr(batch, "check", ""))
            unit = " rows" if check in ("MIN_ROW_COUNT", "MAX_ROW_COUNT") else ""
            reasons.append(
                f"{batch.rule_id} batch rule failed "
                f"(actual: {batch.actual_value}{unit}, expected: {batch.expected_value})"
            )
        for drift in drift_checks or []:
            if drift.passed:
                continue
            reasons.append(f"{drift.rule_id} failed ({drift.details})")
        if held_rows > 0:
            behavior_label = behavior.value if isinstance(behavior, QualityBehavior) else "declared"
            reasons.append(
                f"row quality violations exceeded {behavior_label} policy ({held_rows} rows held)"
            )
        if not footing_ok:
            reasons.append("reconciliation footing deficit; delivery refused to write")
        return "; ".join(reasons) if reasons else "Violations exceeded declared contract gate"

    def _ledger_payload(
        self,
        *,
        run_id: str,
        contract_name: str,
        ledger: ReconciliationLedger,
        outcome: DeliveryOutcome,
        audit_cert: dict[str, Any] | None,
        audit_artifact_path: str | None,
        source_path: str | Path | None,
        duration_seconds: float,
        execution_mode: str,
        metrics: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Build the durable ledger payload for one terminal run outcome."""
        source_name: str | None = None
        source_bytes: int | None = None
        if source_path is not None:
            source = Path(source_path)
            source_name = source.name
            try:
                source_bytes = source.stat().st_size
            except OSError:
                source_bytes = None
        manifest = (audit_cert or {}).get("cryptographic_manifest", {}) or {}
        signature = (audit_cert or {}).get("digital_signature", {}) or {}
        certificate_digest = CryptoEngine.canonical_json_hash(
            {
                "manifest": manifest,
                "signature": {
                    "algorithm": signature.get("algorithm"),
                    "key_id": signature.get("key_id"),
                    "signature": signature.get("signature"),
                },
            }
        )
        return {
            "run_id": run_id,
            "contract_name": contract_name,
            "rows_read": ledger.rows_read,
            "rows_delivered": outcome.rows_delivered,
            "metrics": metrics or {},
            "tenant_id": self.tenant_id,
            "contract_version": self.contract.metadata.version,
            "status": outcome.status.value,
            "rows_refused": ledger.refused_structural,
            "rows_held": ledger.held_quality,
            "unaccounted_for": ledger.unaccounted_for,
            "footing_matched": ledger.footing_matched,
            "is_run_rejected": ledger.is_run_rejected,
            "rejection_reason": ledger.rejection_reason,
            "duration_seconds": duration_seconds,
            "execution_mode": execution_mode,
            "source_name": source_name,
            "source_bytes": source_bytes,
            "target_path": outcome.target_path,
            "audit_path": str(audit_artifact_path) if audit_artifact_path else None,
            "run_artifact_path": outcome.run_artifact_path,
            "quarantine_path": outcome.quarantine_path,
            "quarantine_files": list(outcome.quarantine_files),
            "merkle_root": manifest.get("merkle_root"),
            "signature_key_id": signature.get("key_id"),
            "certificate_digest": certificate_digest,
            "rule_breakdown": dict(ledger.rule_breakdown),
            "structural_reasons": dict(ledger.structural_reasons),
        }

    def _apply_ledger_payload(self, payload: dict[str, Any]) -> None:
        """Idempotently apply a ledger payload (normal path or outbox replay).

        The run row, its quarantine index rows, and its usage meter are written
        in a single transaction, so a run is never left marked ``COMMITTED``
        with incomplete accounting.
        """
        self.history_tracker.apply_run_outcome(
            run_id=payload["run_id"],
            contract_name=payload["contract_name"],
            rows_read=payload["rows_read"],
            rows_delivered=payload["rows_delivered"],
            metrics=payload.get("metrics") or {},
            tenant_id=payload["tenant_id"],
            contract_version=payload.get("contract_version"),
            status=payload.get("status"),
            rows_refused=payload.get("rows_refused", 0),
            rows_held=payload.get("rows_held", 0),
            unaccounted_for=payload.get("unaccounted_for", 0),
            footing_matched=payload.get("footing_matched", True),
            is_run_rejected=payload.get("is_run_rejected", False),
            rejection_reason=payload.get("rejection_reason"),
            duration_seconds=payload.get("duration_seconds"),
            execution_mode=payload.get("execution_mode"),
            source_name=payload.get("source_name"),
            source_bytes=payload.get("source_bytes"),
            target_path=payload.get("target_path"),
            audit_path=payload.get("audit_path"),
            quarantine_path=payload.get("quarantine_path"),
            merkle_root=payload.get("merkle_root"),
            signature_key_id=payload.get("signature_key_id"),
            certificate_digest=payload.get("certificate_digest"),
            rule_breakdown=payload.get("rule_breakdown") or {},
            structural_reasons=payload.get("structural_reasons") or {},
            quarantine_rows=_quarantine_rows_from_payload(payload),
            billable=payload.get("status") != TerminalStatus.FAILED.value,
            units=int(payload.get("rows_read", 0) or 0),
        )

    def _record_run_outcome(
        self,
        *,
        run_id: str,
        contract_name: str,
        ledger: ReconciliationLedger,
        outcome: DeliveryOutcome,
        audit_cert: dict[str, Any] | None,
        audit_artifact_path: str | None,
        source_path: str | Path | None,
        duration_seconds: float,
        execution_mode: str,
        metrics: dict[str, float] | None = None,
        outbox_path: Path | None = None,
    ) -> str:
        """Persist the ledger row, quarantine index, usage meter, and event.

        Recording failures are non-fatal to the *artifacts* — a delivered file
        is never rolled back because bookkeeping failed — but they are no
        longer silent: the run stays ``published-but-ledger-pending`` (durable
        outbox record plus a ``PENDING`` row when the DB is reachable) and this
        returns ``PENDING`` instead of ``COMMITTED`` so callers surface it.
        """
        payload = self._ledger_payload(
            run_id=run_id,
            contract_name=contract_name,
            ledger=ledger,
            outcome=outcome,
            audit_cert=audit_cert,
            audit_artifact_path=audit_artifact_path,
            source_path=source_path,
            duration_seconds=duration_seconds,
            execution_mode=execution_mode,
            metrics=metrics,
        )
        manifest = (audit_cert or {}).get("cryptographic_manifest", {}) or {}
        try:
            self._apply_ledger_payload(payload)
            ledger_status = "COMMITTED"
            if outbox_path is not None:
                clear_pending_outbox(outbox_path)
        except Exception as history_error:
            telemetry_logger.warning(
                "Run ledger update failed; run is pending reconciliation",
                run_id=run_id,
                tenant=self.tenant_id,
                error_type=type(history_error).__name__,
            )
            ledger_status = "PENDING"
            # The durable outbox already carries this run; the ledger retry is
            # best-effort because reconciliation replays it from the pending
            # outbox on next startup.
            with contextlib.suppress(Exception):
                self.history_tracker.record_run(
                    run_id=run_id,
                    contract_name=contract_name,
                    rows_read=ledger.rows_read,
                    rows_delivered=outcome.rows_delivered,
                    metrics=metrics or {},
                    tenant_id=self.tenant_id,
                    contract_version=self.contract.metadata.version,
                    status=outcome.status.value,
                    certificate_digest=payload["certificate_digest"],
                    ledger_status="PENDING",
                )
        emit_run_event({
            "event": "run.completed",
            "run_id": run_id,
            "tenant_id": self.tenant_id,
            "contract_name": contract_name,
            "contract_version": self.contract.metadata.version,
            "status": outcome.status.value,
            "ledger_status": ledger_status,
            "execution_mode": execution_mode,
            "duration_seconds": duration_seconds,
            "rows_read": ledger.rows_read,
            "rows_delivered": outcome.rows_delivered,
            "rows_refused": ledger.refused_structural,
            "rows_held": ledger.held_quality,
            "unaccounted_for": ledger.unaccounted_for,
            "footing_matched": ledger.footing_matched,
            "is_run_rejected": ledger.is_run_rejected,
            "rejection_reason": ledger.rejection_reason,
            "merkle_root": manifest.get("merkle_root"),
            "audit_path": str(audit_artifact_path) if audit_artifact_path else None,
            "target_path": outcome.target_path,
            "quarantine_path": outcome.quarantine_path,
        })
        return ledger_status

    def _record_failed_run(self, run_id: str, contract_name: str, error: BaseException) -> None:
        """Best-effort forensic entry for a run that failed before delivery."""
        try:
            if self.history_tracker.get_run(run_id, self.tenant_id):
                return
            self.history_tracker.record_run(
                run_id=run_id,
                contract_name=contract_name,
                rows_read=0,
                rows_delivered=0,
                metrics={},
                tenant_id=self.tenant_id,
                contract_version=self.contract.metadata.version,
                status=TerminalStatus.FAILED.value,
                footing_matched=False,
                rejection_reason=f"{type(error).__name__}: {error}"[:1000],
            )
        except Exception:  # noqa: BLE001 - bookkeeping must never mask the original error
            pass

    def run_file(
        self,
        source_path: str | Path,
        target_path_override: str | None = None,
        audit_output_path: str | None = None
    ) -> PipelineResult:
        """Run enforcement pipeline on an input file."""
        if target_path_override and self.contract.spec.delivery.targets:
            self.contract.spec.delivery.targets[0].path = target_path_override

        # Fail fast before staging anything when the storage volume cannot hold
        # the transient footprint of this run.
        source = Path(source_path)
        ensure_disk_headroom(
            source.stat().st_size if source.is_file() else 0,
            multiplier=settings.disk_headroom_multiplier,
            root=settings.output_dir,
        )

        run_id = f"dm_run_{uuid.uuid4().hex[:12]}"
        contract_name = self.contract.metadata.name
        ACTIVE_RUNS.inc()
        start_time = time.time()
        staged_to_publish: list[Path] = []
        audit_dest = (
            Path(audit_output_path).resolve() if audit_output_path
            else settings.audit_dir / f"{run_id}_audit_certificate.json"
        )

        try:
            # Step 1: Ingestion & Tier 1 Structural Gate
            ingest_res: IngestionResult = self.ingestion_engine.ingest_from_file(source_path)

            # Step 2: Quality & Tier 2 Semantic Gate (Polars columnar + DuckDB batch)
            quality_res: QualityResult = self.quality_engine.evaluate(ingest_res.conforming_df)

            # Step 3: Zero-Deficit Reconciliation ("The arithmetic has to foot")
            structural_reasons: dict[str, int] = {}
            for r in ingest_res.rejections:
                reason = f"{r.failed_column}:{r.expected_type}"
                structural_reasons[reason] = structural_reasons.get(reason, 0) + 1

            ledger = Reconciler.balance(
                run_id=run_id,
                rows_read=ingest_res.rows_read,
                refused_structural=ingest_res.refused_count,
                held_quality=quality_res.held_count,
                rows_to_deliver=quality_res.admitted_count,
                rule_breakdown=quality_res.rule_violation_counts,
                structural_reasons=structural_reasons
            )

            # Step 4: Cross-Period Historical Drift Evaluation (opt-in; see
            # DRIFT_ENABLED). When disabled no baseline is consulted.
            feed_metrics = compute_metrics(quality_res.admitted_df)
            drift_checks = self._evaluate_drift(
                contract_name=contract_name,
                current_rows=quality_res.admitted_count,
                current_metrics=feed_metrics,
            )
            all_drift_passed = all(d.passed for d in drift_checks)

            # Step 5: Delivery Gate
            all_checks_passed = quality_res.all_batch_rules_passed and all_drift_passed
            should_reject = quality_res.should_reject_run or (
                not all_drift_passed and self.contract.spec.quality.behavior != QualityBehavior.WARN
            )
            outcome: DeliveryOutcome = self.delivery_engine.deliver(
                admitted_df=quality_res.admitted_df,
                held_df=quality_res.held_df,
                structural_rejections=ingest_res.rejections,
                ledger=ledger,
                all_batch_rules_passed=all_checks_passed,
                should_reject_run=should_reject,
                defer_publish=True,
                rejection_reason=(
                    self._rejection_reason(
                        quality_res.batch_results,
                        drift_checks,
                        held_rows=(
                            quality_res.held_count
                            if quality_res.behavior == QualityBehavior.REJECT_RUN
                            else 0
                        ),
                        behavior=quality_res.behavior,
                        footing_ok=ledger.footing_matched,
                    )
                    if should_reject
                    else None
                ),
            )
            for staged_candidate in (outcome.staged_target_path, outcome.staged_run_artifact_path):
                if staged_candidate:
                    staged_to_publish.append(Path(staged_candidate))

            duration = time.time() - start_time

            # Step 6: Build the certificate before publishing. The delivered hash
            # comes from the staged bytes; the manifest names the final paths.
            delivered_file_path = Path(outcome.target_path) if outcome.target_path else None
            
            audit_cert = ArtifactGenerator.generate_audit_certificate(
                ledger=ledger,
                batch_results=quality_res.batch_results,
                contract_name=contract_name,
                contract_version=self.contract.metadata.version,
                contract_canonical_hash=self.contract.canonical_sha256(),
                source_file_path=Path(source_path),
                delivered_file_path=delivered_file_path,
                delivered_sha256_override=outcome.delivered_sha256,
                run_artifact_path=Path(outcome.run_artifact_path) if outcome.run_artifact_path else None,
                quarantine_files=[Path(p) for p in outcome.quarantine_files],
                drift_results=[d.__dict__ for d in drift_checks],
                tenant_id=self.tenant_id
            )
            if outcome.status == TerminalStatus.LOADED:
                if (
                    outcome.staged_target_path is None
                    or outcome.staged_run_artifact_path is None
                    or outcome.target_path is None
                    or outcome.run_artifact_path is None
                ):
                    raise RuntimeError("Loaded delivery is missing staged publication artifacts")
                staged_audit = audit_dest.with_name(f".staged_{run_id}_{audit_dest.name}")
                staged_to_publish.append(staged_audit)
                ArtifactGenerator.save_audit_file(audit_cert, staged_audit)
                final_target = Path(str(outcome.target_path))
                final_run_artifact = Path(str(outcome.run_artifact_path))
                # Recover journals produced by earlier versions, then publish the
                # signed certificate, immutable lineage, and active target together.
                # The outbox record lands first so a crash between publication
                # and ledger recording stays replayable (LEDGER-001); the
                # generation pointer is the reader-visible commit point (PUB-001).
                recover_publication(final_target)
                outbox_path = pending_outbox_path(audit_dest, run_id)
                transaction_root = _common_transaction_root([final_target, final_run_artifact, audit_dest])
                _publish_with_pending_outbox(
                    pairs=[
                        (Path(str(outcome.staged_run_artifact_path)), final_run_artifact),
                        (staged_audit, audit_dest),
                        (Path(str(outcome.staged_target_path)), final_target),
                    ],
                    run_id=run_id,
                    transaction_root=transaction_root,
                    generation_pointer={
                        "tenant_id": self.tenant_id,
                        "contract_name": contract_name,
                        "contract_version": self.contract.metadata.version,
                        "audit_path": str(audit_dest),
                        "lineage_path": str(final_run_artifact),
                    },
                    outbox_path=outbox_path,
                    payload=self._ledger_payload(
                        run_id=run_id,
                        contract_name=contract_name,
                        ledger=ledger,
                        outcome=outcome,
                        audit_cert=audit_cert,
                        audit_artifact_path=str(audit_dest),
                        source_path=source_path,
                        duration_seconds=duration,
                        execution_mode="in_memory",
                        metrics=feed_metrics,
                    ),
                )
                saved_audit = audit_dest
            else:
                saved_audit = ArtifactGenerator.save_audit_file(audit_cert, audit_dest)
                outbox_path = None

            ledger_status = self._record_run_outcome(
                run_id=run_id,
                contract_name=contract_name,
                ledger=ledger,
                outcome=outcome,
                audit_cert=audit_cert,
                audit_artifact_path=str(saved_audit),
                source_path=source_path,
                duration_seconds=duration,
                execution_mode="in_memory",
                metrics=feed_metrics,
                outbox_path=outbox_path,
            )

            # Step 7: Telemetry & Prometheus Metric Recording
            ROWS_READ_TOTAL.inc(ledger.rows_read)
            ROWS_DELIVERED_TOTAL.inc(outcome.rows_delivered)
            ROWS_REFUSED_TOTAL.inc(ledger.refused_structural)
            for _rule_id, count in quality_res.rule_violation_counts.items():
                ROWS_HELD_TOTAL.inc(count)
            RUNS_TOTAL.labels(status=outcome.status.value).inc()
            RUN_DURATION_SECONDS.observe(duration)

            telemetry_logger.info(
                "Enforcement run completed",
                run_id=run_id,
                tenant=self.tenant_id,
                contract=contract_name,
                status=outcome.status.value,
                duration=round(duration, 4),
                rows_read=ledger.rows_read,
                delivered=outcome.rows_delivered,
                unaccounted=ledger.unaccounted_for,
                merkle_root=audit_cert["cryptographic_manifest"]["merkle_root"]
            )

            return PipelineResult(
                run_id=run_id,
                status=outcome.status,
                duration_seconds=round(duration, 4),
                ledger=ledger,
                outcome=outcome,
                audit_artifact_path=str(saved_audit),
                audit_certificate=audit_cert,
                ledger_status=ledger_status,
            )

        except Exception as e:
            FOOTING_ERRORS_TOTAL.inc()
            telemetry_logger.error(
                "Enforcement run failed", run_id=run_id,
                contract=contract_name, error_type=type(e).__name__)
            self._record_failed_run(run_id, contract_name, e)
            raise
        finally:
            for staged_path in staged_to_publish:
                if staged_path.exists():
                    with contextlib.suppress(OSError):
                        staged_path.unlink()
            ACTIVE_RUNS.dec()

    def run_records(
        self,
        records: list[dict[str, Any]],
        target_path_override: str | None = None,
        audit_output_path: str | None = None
    ) -> PipelineResult:
        """Run enforcement pipeline directly on in-memory records."""
        if target_path_override and self.contract.spec.delivery.targets:
            self.contract.spec.delivery.targets[0].path = target_path_override

        run_id = f"dm_run_{uuid.uuid4().hex[:12]}"
        contract_name = self.contract.metadata.name
        ACTIVE_RUNS.inc()
        start_time = time.time()
        staged_to_publish: list[Path] = []
        audit_dest = (
            Path(audit_output_path).resolve() if audit_output_path
            else settings.audit_dir / f"{run_id}_audit_certificate.json"
        )

        try:
            ingest_res: IngestionResult = self.ingestion_engine.ingest_records(records)
            quality_res: QualityResult = self.quality_engine.evaluate(ingest_res.conforming_df)

            structural_reasons: dict[str, int] = {}
            for r in ingest_res.rejections:
                reason = f"{r.failed_column}:{r.expected_type}"
                structural_reasons[reason] = structural_reasons.get(reason, 0) + 1

            ledger = Reconciler.balance(
                run_id=run_id,
                rows_read=ingest_res.rows_read,
                refused_structural=ingest_res.refused_count,
                held_quality=quality_res.held_count,
                rows_to_deliver=quality_res.admitted_count,
                rule_breakdown=quality_res.rule_violation_counts,
                structural_reasons=structural_reasons
            )

            # Historical drift evaluation (opt-in; multi-tenant isolated)
            feed_metrics = compute_metrics(quality_res.admitted_df)
            drift_checks = self._evaluate_drift(
                contract_name=contract_name,
                current_rows=quality_res.admitted_count,
                current_metrics=feed_metrics,
            )
            all_drift_passed = all(d.passed for d in drift_checks)

            # Delivery Gate
            all_checks_passed = quality_res.all_batch_rules_passed and all_drift_passed
            should_reject = quality_res.should_reject_run or (
                not all_drift_passed and self.contract.spec.quality.behavior != QualityBehavior.WARN
            )

            outcome: DeliveryOutcome = self.delivery_engine.deliver(
                admitted_df=quality_res.admitted_df,
                held_df=quality_res.held_df,
                structural_rejections=ingest_res.rejections,
                ledger=ledger,
                all_batch_rules_passed=all_checks_passed,
                should_reject_run=should_reject,
                defer_publish=True,
                rejection_reason=(
                    self._rejection_reason(
                        quality_res.batch_results,
                        drift_checks,
                        held_rows=(
                            quality_res.held_count
                            if quality_res.behavior == QualityBehavior.REJECT_RUN
                            else 0
                        ),
                        behavior=quality_res.behavior,
                        footing_ok=ledger.footing_matched,
                    )
                    if should_reject
                    else None
                ),
            )
            for staged_candidate in (outcome.staged_target_path, outcome.staged_run_artifact_path):
                if staged_candidate:
                    staged_to_publish.append(Path(staged_candidate))

            duration = time.time() - start_time
            delivered_file_path = Path(outcome.target_path) if outcome.target_path else None
            
            audit_cert = ArtifactGenerator.generate_audit_certificate(
                ledger=ledger,
                batch_results=quality_res.batch_results,
                contract_name=contract_name,
                contract_version=self.contract.metadata.version,
                contract_canonical_hash=self.contract.canonical_sha256(),
                source_file_path=None,
                source_payload_sha256=CryptoEngine.sha256_json(records),
                delivered_file_path=delivered_file_path,
                quarantine_files=[Path(p) for p in outcome.quarantine_files],
                tenant_id=self.tenant_id,
                drift_results=[d.__dict__ for d in drift_checks],
                delivered_sha256_override=outcome.delivered_sha256,
                run_artifact_path=Path(outcome.run_artifact_path) if outcome.run_artifact_path else None
            )
            if outcome.status == TerminalStatus.LOADED:
                if (
                    outcome.staged_target_path is None
                    or outcome.staged_run_artifact_path is None
                    or outcome.target_path is None
                    or outcome.run_artifact_path is None
                ):
                    raise RuntimeError("Loaded delivery is missing staged publication artifacts")
                staged_audit = audit_dest.with_name(f".staged_{run_id}_{audit_dest.name}")
                staged_to_publish.append(staged_audit)
                ArtifactGenerator.save_audit_file(audit_cert, staged_audit)
                final_target = Path(outcome.target_path)
                final_run_artifact = Path(outcome.run_artifact_path)
                recover_publication(final_target)
                outbox_path = pending_outbox_path(audit_dest, run_id)
                transaction_root = _common_transaction_root([final_target, final_run_artifact, audit_dest])
                _publish_with_pending_outbox(
                    pairs=[
                        (Path(outcome.staged_run_artifact_path), final_run_artifact),
                        (staged_audit, audit_dest),
                        (Path(outcome.staged_target_path), final_target),
                    ],
                    run_id=run_id,
                    transaction_root=transaction_root,
                    generation_pointer={
                        "tenant_id": self.tenant_id,
                        "contract_name": contract_name,
                        "contract_version": self.contract.metadata.version,
                        "audit_path": str(audit_dest),
                        "lineage_path": str(final_run_artifact),
                    },
                    outbox_path=outbox_path,
                    payload=self._ledger_payload(
                        run_id=run_id,
                        contract_name=contract_name,
                        ledger=ledger,
                        outcome=outcome,
                        audit_cert=audit_cert,
                        audit_artifact_path=str(audit_dest),
                        source_path=None,
                        duration_seconds=duration,
                        execution_mode="in_memory_records",
                        metrics=feed_metrics,
                    ),
                )
                saved_audit = audit_dest
            else:
                saved_audit = ArtifactGenerator.save_audit_file(audit_cert, audit_dest)
                outbox_path = None

            ledger_status = self._record_run_outcome(
                run_id=run_id,
                contract_name=contract_name,
                ledger=ledger,
                outcome=outcome,
                audit_cert=audit_cert,
                audit_artifact_path=str(saved_audit),
                source_path=None,
                duration_seconds=duration,
                execution_mode="in_memory_records",
                metrics=feed_metrics,
                outbox_path=outbox_path,
            )

            return PipelineResult(
                run_id=run_id,
                status=outcome.status,
                duration_seconds=round(duration, 4),
                ledger=ledger,
                outcome=outcome,
                audit_artifact_path=str(saved_audit),
                audit_certificate=audit_cert,
                ledger_status=ledger_status,
            )
        except Exception as e:
            FOOTING_ERRORS_TOTAL.inc()
            telemetry_logger.error(
                "Enforcement run failed", run_id=run_id,
                contract=contract_name, error_type=type(e).__name__)
            self._record_failed_run(run_id, contract_name, e)
            raise
        finally:
            for staged_path in staged_to_publish:
                if staged_path.exists():
                    with contextlib.suppress(OSError):
                        staged_path.unlink()
            ACTIVE_RUNS.dec()

    def run_streaming(
        self,
        source_path: str | Path,
        chunk_size: int | None = None,
        target_path_override: str | None = None,
        audit_output_path: str | None = None
    ) -> PipelineResult:
        """
        Unified out-of-core streaming execution:
        Processes massive Parquet, CSV, or JSONL datasets in constant O(1) memory
        batches through Tier 1 structural and Tier 2 row-level quality gates, stages
        atomic parquet writes, executes global batch assertions via out-of-core
        DuckDB streaming, and enforces whole-run rejection and historical drift
        policies with zero memory ballooning.
        """
        chunk_size = int(chunk_size or settings.stream_chunk_rows)
        if chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")

        if target_path_override and self.contract.spec.delivery.targets:
            self.contract.spec.delivery.targets[0].path = target_path_override

        # Fail fast before staging anything when the storage volume cannot hold
        # the transient footprint of this run (target + worst-case quarantine).
        source = Path(source_path)
        ensure_disk_headroom(
            source.stat().st_size if source.is_file() else 0,
            multiplier=settings.disk_headroom_multiplier,
            root=settings.output_dir,
        )

        run_id = f"dm_stream_{uuid.uuid4().hex[:12]}"
        contract_name = self.contract.metadata.name
        audit_dest = (
            Path(audit_output_path).resolve() if audit_output_path
            else settings.audit_dir / f"{run_id}_audit_certificate.json"
        )
        start_time = time.time()
        ACTIVE_RUNS.inc()
        staged_files: list[Path] = []

        try:
            quarantine_path_str = self.contract.spec.delivery.quarantinePath
            if quarantine_path_str:
                quarantine_dir = FilesystemSandbox.sanitize_and_validate_path(
                    quarantine_path_str, tenant_id=self.tenant_id
                )
            else:
                quarantine_dir = settings.get_tenant_quarantine_dir(self.tenant_id)
            quarantine_dir.mkdir(parents=True, exist_ok=True)

            target_path_str = (
                self.contract.spec.delivery.targets[0].path
                if self.contract.spec.delivery.targets
                else "./output/delivered.parquet"
            )
            final_target = FilesystemSandbox.sanitize_and_validate_path(
                target_path_str, is_delivery_target=True, tenant_id=self.tenant_id
            )
            # Legacy journals are stored beside the target; new multi-artifact
            # journals are recovered during the root-scoped publication below.
            recover_publication(final_target)
            staged_target = final_target.with_name(f".staged_{run_id}_{final_target.name}")
            staged_held = quarantine_dir / f".staged_{run_id}_held_records.parquet"
            final_held = quarantine_dir / f"{run_id}_held_records.parquet"
            staged_rejections = quarantine_dir / f".staged_{run_id}_structural_rejections.jsonl"
            final_rejections = quarantine_dir / f"{run_id}_structural_rejections.jsonl"

            staged_files.extend([staged_target, staged_held, staged_rejections])

            total_rows_read = 0
            total_structural_refused = 0
            total_held_quality = 0
            total_admitted = 0
            rule_breakdowns: dict[str, int] = {}
            structural_reasons: dict[str, int] = {}
            accumulated_should_reject = False

            target_writer: pq.ParquetWriter | None = None
            held_writer: pq.ParquetWriter | None = None
            # ExitStack keeps the rejection stream's lifetime identical to the
            # parquet writers below: closed after per-chunk processing, before
            # the whole-run batch gate.
            with contextlib.ExitStack() as rejections_stack:
                rejections_file = rejections_stack.enter_context(
                    open(staged_rejections, "w", encoding="utf-8")
                )

                try:
                    for chunk_df in self.ingestion_engine.iter_batches(source_path, batch_size=chunk_size):
                        chunk_len = chunk_df.height
                        total_rows_read += chunk_len

                        # Tier 1: Ingest chunk via the vectorized structural engine
                        # (falls back to strict row-by-row diagnostics when needed).
                        ingest_chunk: IngestionResult = self.ingestion_engine.ingest_dataframe(chunk_df)
                        total_structural_refused += ingest_chunk.refused_count

                        # Stream structural rejections incrementally to JSONL
                        for r in ingest_chunk.rejections:
                            reason = f"{r.failed_column}:{r.expected_type}"
                            structural_reasons[reason] = structural_reasons.get(reason, 0) + 1
                            rejections_file.write(safe_json_dumps({
                                "row_index": r.row_index,
                                "failed_column": r.failed_column,
                                "expected_type": r.expected_type,
                                "error_message": r.error_message,
                                "raw_record": r.raw_record
                            }) + "\n")

                        if ingest_chunk.conforming_df.height == 0:
                            continue

                        # Tier 2: Evaluate row-level quality rules on conforming
                        # rows (defer batch rules to the whole staged dataset)
                        quality_res = self.quality_engine.evaluate(
                            ingest_chunk.conforming_df, evaluate_batch=False)
                        for r_id, count in quality_res.rule_violation_counts.items():
                            rule_breakdowns[r_id] = rule_breakdowns.get(r_id, 0) + count

                        # CRITICAL FIX: Accumulate whole-run rejection flag across micro-batches!
                        # If quality behavior is REJECT_RUN and ANY row failed, the entire run must be rejected!
                        if quality_res.should_reject_run:
                            accumulated_should_reject = True
                        if (self.contract.spec.quality.behavior == QualityBehavior.REJECT_RUN
                                and quality_res.held_count > 0):
                            accumulated_should_reject = True

                        # Incrementally write admitted rows to staged target. Flush
                        # each micro-batch as its own row group so writer memory is
                        # bounded by one batch instead of pyarrow's default ~1M-row
                        # row group, which would balloon on wide datasets.
                        if quality_res.admitted_df.height > 0:
                            arrow_adm = quality_res.admitted_df.to_arrow()
                            if target_writer is None:
                                target_writer = pq.ParquetWriter(
                                    staged_target, arrow_adm.schema, compression=parquet_compression_codec())
                            target_writer.write_table(arrow_adm, row_group_size=arrow_adm.num_rows)
                            total_admitted += quality_res.admitted_df.height

                        # Incrementally write held rows to staged quarantine
                        if quality_res.held_df.height > 0:
                            arrow_held = quality_res.held_df.to_arrow()
                            if held_writer is None:
                                held_writer = pq.ParquetWriter(
                                    staged_held, arrow_held.schema, compression=parquet_compression_codec())
                            held_writer.write_table(arrow_held, row_group_size=arrow_held.num_rows)
                            total_held_quality += quality_res.held_df.height

                finally:
                    if target_writer:
                        target_writer.close()
                    if held_writer:
                        held_writer.close()

            arrow_schema = pl.DataFrame(schema=self.ingestion_engine._polars_schema).to_arrow().schema

            # Handle empty admitted dataset: if 0 admitted rows, write a typed empty Parquet table
            if total_admitted == 0 and not staged_target.exists():
                empty_arrow = pa.Table.from_batches([], schema=arrow_schema)
                pq.write_table(empty_arrow, staged_target)

            # Balance Footing Across the Whole Streaming Job
            ledger = Reconciler.balance(
                run_id=run_id,
                rows_read=total_rows_read,
                refused_structural=total_structural_refused,
                held_quality=total_held_quality,
                rows_to_deliver=total_admitted,
                rule_breakdown=rule_breakdowns,
                structural_reasons=structural_reasons
            )

            # Global Batch Quality Gate Evaluation: Out-of-core via DuckDB over the staged file
            batch_results, all_batch_passed = self.quality_engine.evaluate_batch_rules(
                staged_target,
                fallback_arrow_schema=arrow_schema
            )
            if not all_batch_passed and self.contract.spec.quality.behavior != QualityBehavior.WARN:
                accumulated_should_reject = True

            # Cross-Period Historical Drift Evaluation (opt-in; multi-tenant isolated)
            feed_metrics = compute_metrics_from_parquet(staged_target)
            drift_checks = self._evaluate_drift(
                contract_name=contract_name,
                current_rows=total_admitted,
                current_metrics=feed_metrics,
            )
            all_drift_passed = all(d.passed for d in drift_checks)
            if not all_drift_passed and self.contract.spec.quality.behavior != QualityBehavior.WARN:
                accumulated_should_reject = True

            # Publish quarantine files
            created_quarantine_files: list[Path] = []
            if total_held_quality > 0 and staged_held.exists():
                os.replace(staged_held, final_held)
                created_quarantine_files.append(final_held)

            if total_structural_refused > 0 and staged_rejections.exists():
                os.replace(staged_rejections, final_rejections)
                created_quarantine_files.append(final_rejections)
            elif staged_rejections.exists():
                staged_rejections.unlink()

            # Final Delivery Decision
            if accumulated_should_reject or not ledger.footing_matched:
                if staged_target.exists():
                    staged_target.unlink()
                rejection_reason = self._rejection_reason(
                    batch_results,
                    drift_checks,
                    held_rows=(
                        total_held_quality
                        if self.contract.spec.quality.behavior == QualityBehavior.REJECT_RUN
                        else 0
                    ),
                    behavior=self.contract.spec.quality.behavior,
                    footing_ok=ledger.footing_matched,
                )
                ledger.mark_rejected(rejection_reason)
                outcome = DeliveryOutcome(
                    status=TerminalStatus.REJECTED,
                    rows_delivered=0,
                    target_path=None,
                    quarantine_path=str(quarantine_dir) if created_quarantine_files else None,
                    quarantine_files=[str(p) for p in created_quarantine_files],
                    ledger=ledger,
                    details="Streaming run rejected by contract policy gate. Zero rows delivered."
                )
            else:
                # Compute SHA-256 on staged file BEFORE atomic replace to prevent concurrent hash races
                delivered_sha256 = CryptoEngine.sha256_file(staged_target)

                # Stage and atomically publish immutable run-specific output artifact: <run_id>_<target_name>
                run_artifact_path = final_target.with_name(f"{run_id}_{final_target.name}")
                staged_run_artifact = final_target.with_name(f".staged_{run_id}_lineage_{final_target.name}")
                staged_files.append(staged_run_artifact)
                materialize_artifact_copy(staged_target, staged_run_artifact)

                # Keep all three artifacts staged until the signed certificate
                # is ready; the final transaction publishes certificate, lineage,
                # and active target together.

                outcome = DeliveryOutcome(
                    status=TerminalStatus.LOADED,
                    rows_delivered=total_admitted,
                    target_path=str(final_target),
                    quarantine_path=str(quarantine_dir) if created_quarantine_files else None,
                    quarantine_files=[str(p) for p in created_quarantine_files],
                    ledger=ledger,
                    details=(
                        f"Delivered {total_admitted:,} conforming rows across streaming "
                        "micro-batches with recoverable lineage publication."
                    ),
                    delivered_sha256=delivered_sha256,
                    run_artifact_path=str(run_artifact_path)
                )

            duration = time.time() - start_time
            audit_cert = ArtifactGenerator.generate_audit_certificate(
                ledger=ledger,
                batch_results=batch_results,
                contract_name=contract_name,
                contract_version=self.contract.metadata.version,
                contract_canonical_hash=self.contract.canonical_sha256(),
                source_file_path=Path(source_path),
                delivered_file_path=Path(outcome.target_path) if outcome.target_path else None,
                delivered_sha256_override=outcome.delivered_sha256,
                run_artifact_path=Path(outcome.run_artifact_path) if outcome.run_artifact_path else None,
                quarantine_files=created_quarantine_files,
                drift_results=[d.__dict__ for d in drift_checks],
                tenant_id=self.tenant_id
            )
            if outcome.status == TerminalStatus.LOADED and outcome.target_path and outcome.run_artifact_path:
                staged_audit = audit_dest.with_name(f".staged_{run_id}_{audit_dest.name}")
                staged_files.append(staged_audit)
                ArtifactGenerator.save_audit_file(audit_cert, staged_audit)
                final_target = Path(outcome.target_path)
                final_run_artifact = Path(outcome.run_artifact_path)
                outbox_path = pending_outbox_path(audit_dest, run_id)
                transaction_root = _common_transaction_root([final_target, final_run_artifact, audit_dest])
                _publish_with_pending_outbox(
                    pairs=[
                        (staged_run_artifact, final_run_artifact),
                        (staged_audit, audit_dest),
                        (staged_target, final_target),
                    ],
                    run_id=run_id,
                    transaction_root=transaction_root,
                    generation_pointer={
                        "tenant_id": self.tenant_id,
                        "contract_name": contract_name,
                        "contract_version": self.contract.metadata.version,
                        "audit_path": str(audit_dest),
                        "lineage_path": str(final_run_artifact),
                    },
                    outbox_path=outbox_path,
                    payload=self._ledger_payload(
                        run_id=run_id,
                        contract_name=contract_name,
                        ledger=ledger,
                        outcome=outcome,
                        audit_cert=audit_cert,
                        audit_artifact_path=str(audit_dest),
                        source_path=source_path,
                        duration_seconds=duration,
                        execution_mode="out_of_core_streaming",
                        metrics=feed_metrics,
                    ),
                )
                saved_audit = audit_dest
            else:
                saved_audit = ArtifactGenerator.save_audit_file(audit_cert, audit_dest)
                outbox_path = None

            ledger_status = self._record_run_outcome(
                run_id=run_id,
                contract_name=contract_name,
                ledger=ledger,
                outcome=outcome,
                audit_cert=audit_cert,
                audit_artifact_path=str(saved_audit),
                source_path=source_path,
                duration_seconds=duration,
                execution_mode="out_of_core_streaming",
                metrics=feed_metrics,
                outbox_path=outbox_path,
            )

            # Telemetry & Prometheus Metric Recording
            ROWS_READ_TOTAL.inc(ledger.rows_read)
            ROWS_DELIVERED_TOTAL.inc(outcome.rows_delivered)
            ROWS_REFUSED_TOTAL.inc(ledger.refused_structural)
            for _rule_id, count in rule_breakdowns.items():
                ROWS_HELD_TOTAL.inc(count)
            RUNS_TOTAL.labels(status=outcome.status.value).inc()
            RUN_DURATION_SECONDS.observe(duration)

            telemetry_logger.info(
                "Streaming enforcement run completed",
                run_id=run_id,
                tenant=self.tenant_id,
                contract=contract_name,
                status=outcome.status.value,
                duration=round(duration, 4),
                delivered=outcome.rows_delivered
            )

            return PipelineResult(
                run_id=run_id,
                status=outcome.status,
                duration_seconds=round(duration, 4),
                ledger=ledger,
                outcome=outcome,
                audit_artifact_path=str(saved_audit),
                audit_certificate=audit_cert,
                ledger_status=ledger_status,
            )

        except Exception as e:
            telemetry_logger.error(
                "Streaming enforcement run failed", run_id=run_id,
                contract=contract_name, error_type=type(e).__name__)
            self._record_failed_run(run_id, contract_name, e)
            raise
        finally:
            ACTIVE_RUNS.dec()
            # Clean up uncommitted staged files in case of unhandled error
            for sf in staged_files:
                if sf.exists():
                    with contextlib.suppress(OSError):
                        sf.unlink()

