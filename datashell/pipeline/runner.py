"""Enterprise Boundary Pipeline Runner: Coordinates zero-copy Arrow memory transport, streaming, and cryptographic gates."""

from __future__ import annotations
import os
import uuid
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union, List, Dict, Any
import polars as pl
import pyarrow.parquet as pq

from datashell.core.config import settings
from datashell.core.sandbox import FilesystemSandbox
from datashell.core.crypto import CryptoEngine
from datashell.core.commit import publish_transactionally, recover_publication
from datashell.core.fileops import materialize_artifact_copy, ensure_disk_headroom
from datashell.core.events import emit_run_event
from datashell.contract.models import DataContract, QualityBehavior
from datashell.engines.ingestion import StructuralIngestionEngine, IngestionResult
from datashell.engines.quality import SemanticQualityEngine, QualityResult
from datashell.engines.metrics import compute_metrics, compute_metrics_from_parquet
from datashell.engines.drift import HistoricalDriftEngine, ContractHistoryTracker, DriftCheckResult
from datashell.engines.reconciler import Reconciler, ReconciliationLedger
from datashell.engines.delivery import DeliveryEngine, DeliveryOutcome, TerminalStatus, safe_json_dumps
from datashell.pipeline.artifacts import ArtifactGenerator
import pyarrow as pa

from datashell.core.telemetry import (
    RUNS_TOTAL, ROWS_READ_TOTAL, ROWS_DELIVERED_TOTAL,
    ROWS_HELD_TOTAL, ROWS_REFUSED_TOTAL, FOOTING_ERRORS_TOTAL,
    RUN_DURATION_SECONDS, ACTIVE_RUNS, telemetry_logger
)


def _common_transaction_root(paths: List[Path]) -> Path:
    """Find a stable writable root for recoverable multi-directory publication."""
    resolved_paths = [Path(path).resolve() for path in paths]
    if settings.is_production_mode:
        root = settings.output_dir.resolve()
        if any(not path.is_relative_to(root) for path in resolved_paths):
            raise ValueError("Production delivery targets and audit certificates must remain under DATASHELL_OUTPUT_DIR")
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
    audit_artifact_path: Optional[str]
    audit_certificate: Optional[Dict[str, Any]] = None


class DataShellPipeline:
    """
    Enterprise boundary enforcement pipeline.
    Ensures bad data never lands in target storage, and never leaves to regulators.
    Supports in-memory, file, and out-of-core streaming execution.
    """

    def __init__(self, contract: DataContract, tenant_id: str = "default-tenant"):
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("tenant_id must be a non-empty authenticated tenant identifier")
        # Validate and normalize the filesystem representation at the boundary;
        # the tenant value itself remains the identity used for auth/audit/history.
        settings.tenant_storage_key(tenant_id)
        self.contract = contract
        self.tenant_id = tenant_id
        self.ingestion_engine = StructuralIngestionEngine(contract)
        self.quality_engine = SemanticQualityEngine(contract)
        self.delivery_engine = DeliveryEngine(contract, tenant_id=self.tenant_id)
        self.history_tracker = ContractHistoryTracker()
        self.drift_engine = HistoricalDriftEngine(history_tracker=self.history_tracker)

    def _record_run_outcome(
        self,
        *,
        run_id: str,
        contract_name: str,
        ledger: ReconciliationLedger,
        outcome: DeliveryOutcome,
        audit_cert: Optional[Dict[str, Any]],
        audit_artifact_path: Optional[str],
        source_path: Optional[Union[str, Path]],
        duration_seconds: float,
        execution_mode: str,
        metrics: Optional[Dict[str, float]] = None,
    ) -> None:
        """Persist the forensic ledger row, quarantine index, usage meter, and event.

        Recording failures are non-fatal: a delivered artifact must never be
        rolled back because auxiliary bookkeeping failed. The ledger is the
        accumulating asset, so it captures *every* terminal outcome — including
        rejected runs — not just successful deliveries.
        """
        source_name: Optional[str] = None
        source_bytes: Optional[int] = None
        if source_path is not None:
            source = Path(source_path)
            source_name = source.name
            try:
                source_bytes = source.stat().st_size
            except OSError:
                source_bytes = None

        manifest = (audit_cert or {}).get("cryptographic_manifest", {}) or {}
        signature = (audit_cert or {}).get("digital_signature", {}) or {}

        try:
            self.history_tracker.record_run(
                run_id=run_id,
                contract_name=contract_name,
                rows_read=ledger.rows_read,
                rows_delivered=outcome.rows_delivered,
                metrics=metrics or {},
                tenant_id=self.tenant_id,
                contract_version=self.contract.metadata.version,
                status=outcome.status.value,
                rows_refused=ledger.refused_structural,
                rows_held=ledger.held_quality,
                unaccounted_for=ledger.unaccounted_for,
                footing_matched=ledger.footing_matched,
                is_run_rejected=ledger.is_run_rejected,
                rejection_reason=ledger.rejection_reason,
                duration_seconds=duration_seconds,
                execution_mode=execution_mode,
                source_name=source_name,
                source_bytes=source_bytes,
                target_path=outcome.target_path,
                audit_path=str(audit_artifact_path) if audit_artifact_path else None,
                quarantine_path=outcome.quarantine_path,
                merkle_root=manifest.get("merkle_root"),
                signature_key_id=signature.get("key_id"),
                rule_breakdown=ledger.rule_breakdown,
                structural_reasons=ledger.structural_reasons,
            )

            for artifact in outcome.quarantine_files:
                name = Path(artifact).name
                is_held = name.endswith("_held_records.parquet")
                self.history_tracker.record_quarantine(
                    run_id=run_id,
                    tenant_id=self.tenant_id,
                    contract_name=contract_name,
                    kind="held_quality" if is_held else "structural_refused",
                    artifact_path=str(artifact),
                    row_count=ledger.held_quality if is_held else ledger.refused_structural,
                    reasons=ledger.rule_breakdown if is_held else ledger.structural_reasons,
                )

            self.history_tracker.record_usage(
                run_id=run_id,
                tenant_id=self.tenant_id,
                contract_name=contract_name,
                status=outcome.status.value,
                execution_mode=execution_mode,
                rows_read=ledger.rows_read,
                rows_delivered=outcome.rows_delivered,
                bytes_processed=source_bytes or 0,
                billable=outcome.status != TerminalStatus.FAILED,
                units=ledger.rows_read,
            )
        except Exception as history_error:
            telemetry_logger.warning(
                "Run ledger update failed",
                run_id=run_id,
                tenant=self.tenant_id,
                error_type=type(history_error).__name__,
            )

        emit_run_event({
            "event": "run.completed",
            "run_id": run_id,
            "tenant_id": self.tenant_id,
            "contract_name": contract_name,
            "contract_version": self.contract.metadata.version,
            "status": outcome.status.value,
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
        source_path: Union[str, Path],
        target_path_override: Optional[str] = None,
        audit_output_path: Optional[str] = None
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
        staged_to_publish: List[Path] = []
        audit_dest = Path(audit_output_path).resolve() if audit_output_path else (settings.audit_dir / f"{run_id}_audit_certificate.json")

        try:
            # Step 1: Ingestion & Tier 1 Structural Gate
            ingest_res: IngestionResult = self.ingestion_engine.ingest_from_file(source_path)

            # Step 2: Quality & Tier 2 Semantic Gate (Polars columnar + DuckDB batch)
            quality_res: QualityResult = self.quality_engine.evaluate(ingest_res.conforming_df)

            # Step 3: Zero-Deficit Reconciliation ("The arithmetic has to foot")
            structural_reasons: Dict[str, int] = {}
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

            # Step 4: Cross-Period Historical Drift Evaluation
            feed_metrics = compute_metrics(quality_res.admitted_df)
            drift_checks = self.drift_engine.evaluate_drift(
                contract_name=contract_name,
                current_rows=quality_res.admitted_count,
                current_metrics=feed_metrics,
                tenant_id=self.tenant_id,
                max_volume_drop_pct=settings.drift_max_volume_drop_pct,
                max_metric_drift_pct=settings.drift_max_metric_drift_pct,
                baseline_window=settings.drift_baseline_window,
                min_metric_baseline_samples=settings.drift_metric_min_baseline_samples,
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
            )
            for staged_path in (outcome.staged_target_path, outcome.staged_run_artifact_path):
                if staged_path:
                    staged_to_publish.append(Path(staged_path))

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
                if not outcome.staged_target_path or not outcome.staged_run_artifact_path or not outcome.target_path or not outcome.run_artifact_path:
                    raise RuntimeError("Loaded delivery is missing staged publication artifacts")
                staged_audit = audit_dest.with_name(f".staged_{run_id}_{audit_dest.name}")
                staged_to_publish.append(staged_audit)
                ArtifactGenerator.save_audit_file(audit_cert, staged_audit)
                final_target = Path(outcome.target_path)
                final_run_artifact = Path(outcome.run_artifact_path)
                # Recover journals produced by earlier versions, then publish the
                # signed certificate, immutable lineage, and active target together.
                recover_publication(final_target)
                transaction_root = _common_transaction_root([final_target, final_run_artifact, audit_dest])
                publish_transactionally(
                    [
                        (Path(outcome.staged_run_artifact_path), final_run_artifact),
                        (staged_audit, audit_dest),
                        (Path(outcome.staged_target_path), final_target),
                    ],
                    run_id=run_id,
                    transaction_root=transaction_root,
                )
                saved_audit = audit_dest
            else:
                saved_audit = ArtifactGenerator.save_audit_file(audit_cert, audit_dest)

            self._record_run_outcome(
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
            )

            # Step 7: Telemetry & Prometheus Metric Recording
            ROWS_READ_TOTAL.inc(ledger.rows_read)
            ROWS_DELIVERED_TOTAL.inc(outcome.rows_delivered)
            ROWS_REFUSED_TOTAL.inc(ledger.refused_structural)
            for r_id, count in quality_res.rule_violation_counts.items():
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
                audit_certificate=audit_cert
            )

        except Exception as e:
            FOOTING_ERRORS_TOTAL.inc()
            telemetry_logger.error("Enforcement run failed", run_id=run_id, contract=contract_name, error_type=type(e).__name__)
            self._record_failed_run(run_id, contract_name, e)
            raise
        finally:
            for staged_path in staged_to_publish:
                if staged_path.exists():
                    try:
                        staged_path.unlink()
                    except OSError:
                        pass
            ACTIVE_RUNS.dec()

    def run_records(
        self,
        records: List[Dict[str, Any]],
        target_path_override: Optional[str] = None,
        audit_output_path: Optional[str] = None
    ) -> PipelineResult:
        """Run enforcement pipeline directly on in-memory records."""
        if target_path_override and self.contract.spec.delivery.targets:
            self.contract.spec.delivery.targets[0].path = target_path_override

        run_id = f"dm_run_{uuid.uuid4().hex[:12]}"
        contract_name = self.contract.metadata.name
        ACTIVE_RUNS.inc()
        start_time = time.time()
        staged_to_publish: List[Path] = []
        audit_dest = Path(audit_output_path).resolve() if audit_output_path else (settings.audit_dir / f"{run_id}_audit_certificate.json")

        try:
            ingest_res: IngestionResult = self.ingestion_engine.ingest_records(records)
            quality_res: QualityResult = self.quality_engine.evaluate(ingest_res.conforming_df)

            structural_reasons: Dict[str, int] = {}
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

            # Historical drift evaluation (Multi-Tenant Isolated)
            feed_metrics = compute_metrics(quality_res.admitted_df)
            drift_checks = self.drift_engine.evaluate_drift(
                contract_name=contract_name,
                current_rows=quality_res.admitted_count,
                current_metrics=feed_metrics,
                tenant_id=self.tenant_id,
                max_volume_drop_pct=settings.drift_max_volume_drop_pct,
                max_metric_drift_pct=settings.drift_max_metric_drift_pct,
                baseline_window=settings.drift_baseline_window,
                min_metric_baseline_samples=settings.drift_metric_min_baseline_samples,
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
            )
            for staged_path in (outcome.staged_target_path, outcome.staged_run_artifact_path):
                if staged_path:
                    staged_to_publish.append(Path(staged_path))

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
                drift_results=drift_checks,
                delivered_sha256_override=outcome.delivered_sha256,
                run_artifact_path=Path(outcome.run_artifact_path) if outcome.run_artifact_path else None
            )
            if outcome.status == TerminalStatus.LOADED:
                if not outcome.staged_target_path or not outcome.staged_run_artifact_path or not outcome.target_path or not outcome.run_artifact_path:
                    raise RuntimeError("Loaded delivery is missing staged publication artifacts")
                staged_audit = audit_dest.with_name(f".staged_{run_id}_{audit_dest.name}")
                staged_to_publish.append(staged_audit)
                ArtifactGenerator.save_audit_file(audit_cert, staged_audit)
                final_target = Path(outcome.target_path)
                final_run_artifact = Path(outcome.run_artifact_path)
                recover_publication(final_target)
                transaction_root = _common_transaction_root([final_target, final_run_artifact, audit_dest])
                publish_transactionally(
                    [
                        (Path(outcome.staged_run_artifact_path), final_run_artifact),
                        (staged_audit, audit_dest),
                        (Path(outcome.staged_target_path), final_target),
                    ],
                    run_id=run_id,
                    transaction_root=transaction_root,
                )
                saved_audit = audit_dest
            else:
                saved_audit = ArtifactGenerator.save_audit_file(audit_cert, audit_dest)

            self._record_run_outcome(
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
            )

            return PipelineResult(
                run_id=run_id,
                status=outcome.status,
                duration_seconds=round(duration, 4),
                ledger=ledger,
                outcome=outcome,
                audit_artifact_path=str(saved_audit),
                audit_certificate=audit_cert
            )
        except Exception as e:
            FOOTING_ERRORS_TOTAL.inc()
            telemetry_logger.error("Enforcement run failed", run_id=run_id, contract=contract_name, error_type=type(e).__name__)
            self._record_failed_run(run_id, contract_name, e)
            raise
        finally:
            for staged_path in staged_to_publish:
                if staged_path.exists():
                    try:
                        staged_path.unlink()
                    except OSError:
                        pass
            ACTIVE_RUNS.dec()

    def run_streaming(
        self,
        source_path: Union[str, Path],
        chunk_size: Optional[int] = None,
        target_path_override: Optional[str] = None,
        audit_output_path: Optional[str] = None
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
        audit_dest = Path(audit_output_path).resolve() if audit_output_path else (settings.audit_dir / f"{run_id}_audit_certificate.json")
        start_time = time.time()
        ACTIVE_RUNS.inc()
        staged_files: List[Path] = []

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
            rule_breakdowns: Dict[str, int] = {}
            structural_reasons: Dict[str, int] = {}
            accumulated_should_reject = False

            target_writer: Optional[pq.ParquetWriter] = None
            held_writer: Optional[pq.ParquetWriter] = None
            rejections_file = open(staged_rejections, "w", encoding="utf-8")

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

                    # Tier 2: Evaluate row-level quality rules on conforming rows (defer batch rules to whole staged dataset)
                    quality_res: QualityResult = self.quality_engine.evaluate(ingest_chunk.conforming_df, evaluate_batch=False)
                    for r_id, count in quality_res.rule_violation_counts.items():
                        rule_breakdowns[r_id] = rule_breakdowns.get(r_id, 0) + count

                    # CRITICAL FIX: Accumulate whole-run rejection flag across micro-batches!
                    # If quality behavior is REJECT_RUN and ANY row failed, the entire run must be rejected!
                    if quality_res.should_reject_run:
                        accumulated_should_reject = True
                    if self.contract.spec.quality.behavior == QualityBehavior.REJECT_RUN and quality_res.held_count > 0:
                        accumulated_should_reject = True

                    # Incrementally write admitted rows to staged target. Flush
                    # each micro-batch as its own row group so writer memory is
                    # bounded by one batch instead of pyarrow's default ~1M-row
                    # row group, which would balloon on wide datasets.
                    if quality_res.admitted_df.height > 0:
                        arrow_adm = quality_res.admitted_df.to_arrow()
                        if target_writer is None:
                            target_writer = pq.ParquetWriter(staged_target, arrow_adm.schema)
                        target_writer.write_table(arrow_adm, row_group_size=arrow_adm.num_rows)
                        total_admitted += quality_res.admitted_df.height

                    # Incrementally write held rows to staged quarantine
                    if quality_res.held_df.height > 0:
                        arrow_held = quality_res.held_df.to_arrow()
                        if held_writer is None:
                            held_writer = pq.ParquetWriter(staged_held, arrow_held.schema)
                        held_writer.write_table(arrow_held, row_group_size=arrow_held.num_rows)
                        total_held_quality += quality_res.held_df.height

            finally:
                if target_writer:
                    target_writer.close()
                if held_writer:
                    held_writer.close()
                rejections_file.close()

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

            # Cross-Period Historical Drift Evaluation (Multi-Tenant Isolated)
            feed_metrics = compute_metrics_from_parquet(staged_target)
            drift_checks = self.drift_engine.evaluate_drift(
                contract_name=contract_name,
                current_rows=total_admitted,
                current_metrics=feed_metrics,
                tenant_id=self.tenant_id,
                max_volume_drop_pct=settings.drift_max_volume_drop_pct,
                max_metric_drift_pct=settings.drift_max_metric_drift_pct,
                baseline_window=settings.drift_baseline_window,
                min_metric_baseline_samples=settings.drift_metric_min_baseline_samples,
            )
            all_drift_passed = all(d.passed for d in drift_checks)
            if not all_drift_passed and self.contract.spec.quality.behavior != QualityBehavior.WARN:
                accumulated_should_reject = True

            # Publish quarantine files
            created_quarantine_files: List[Path] = []
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
                rejection_reason = "Streaming whole-run contract violation, batch assertion, drift failure, or footing deficit"
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
                    details=f"Delivered {total_admitted:,} conforming rows across streaming micro-batches with recoverable lineage publication.",
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
            if outcome.status == TerminalStatus.LOADED:
                staged_audit = audit_dest.with_name(f".staged_{run_id}_{audit_dest.name}")
                staged_files.append(staged_audit)
                ArtifactGenerator.save_audit_file(audit_cert, staged_audit)
                final_target = Path(outcome.target_path)
                final_run_artifact = Path(outcome.run_artifact_path)
                transaction_root = _common_transaction_root([final_target, final_run_artifact, audit_dest])
                publish_transactionally(
                    [
                        (staged_run_artifact, final_run_artifact),
                        (staged_audit, audit_dest),
                        (staged_target, final_target),
                    ],
                    run_id=run_id,
                    transaction_root=transaction_root,
                )
                saved_audit = audit_dest
            else:
                saved_audit = ArtifactGenerator.save_audit_file(audit_cert, audit_dest)

            self._record_run_outcome(
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
            )

            # Telemetry & Prometheus Metric Recording
            ROWS_READ_TOTAL.inc(ledger.rows_read)
            ROWS_DELIVERED_TOTAL.inc(outcome.rows_delivered)
            ROWS_REFUSED_TOTAL.inc(ledger.refused_structural)
            for r_id, count in rule_breakdowns.items():
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
                audit_certificate=audit_cert
            )

        except Exception as e:
            telemetry_logger.error("Streaming enforcement run failed", run_id=run_id, contract=contract_name, error_type=type(e).__name__)
            self._record_failed_run(run_id, contract_name, e)
            raise
        finally:
            ACTIVE_RUNS.dec()
            # Clean up uncommitted staged files in case of unhandled error
            for sf in staged_files:
                if sf.exists():
                    try:
                        sf.unlink()
                    except OSError:
                        pass

