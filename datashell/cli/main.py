"""Command Line Interface for Data Shell.

Bad data never lands, and never leaves.

Subcommands
-----------
``run``                Enforce a contract against a local data file.
``validate-contract``  Validate a contract specification file.
``verify``             Verify a signed audit certificate and its artifacts.
``ledger``             Query the durable boundary ledger (runs, quarantine, usage).
``demo``               Run a self-contained refusal -> tamper -> verify walkthrough.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from datashell import __version__
from datashell.contract.models import DataContract
from datashell.core.config import settings
from datashell.core.ledger import BoundaryLedger
from datashell.core.sandbox import FilesystemSandbox
from datashell.pipeline.artifacts import ArtifactGenerator
from datashell.pipeline.runner import (
    EXECUTION_STREAMING,
    DataShellPipeline,
    PipelineResult,
    choose_execution_backend,
)

# --------------------------------------------------------------------------- args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datashell",
        description="Data Shell: Contract-driven boundary enforcement engine. Bad data never lands, and never leaves.",
    )
    parser.add_argument("--version", action="version", version=f"datashell {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run command
    run_parser = subparsers.add_parser("run", help="Run boundary enforcement on a dataset")
    run_parser.add_argument("--contract", "-c", required=True, help="Path to contract JSON file")
    run_parser.add_argument("--source", "-s", required=True, help="Path to input data file (CSV, JSONL, Parquet)")
    run_parser.add_argument("--target", "-t", default=None, help="Output target path override")
    run_parser.add_argument("--audit", "-a", default=None, help="Audit JSON output path")
    run_parser.add_argument("--tenant", default="default-tenant", help="Tenant identity for ledger/audit scoping")
    run_parser.add_argument("--json", action="store_true", help="Emit a machine-readable JSON summary")
    run_parser.add_argument(
        "--streaming", action=argparse.BooleanOptionalAction, default=None,
        help="Force out-of-core streaming (or use --no-streaming to force in-memory). "
             "Defaults to streaming automatically for large sources.",
    )
    run_parser.add_argument(
        "--chunk-size", type=int, default=None,
        help="Rows per micro-batch when streaming (default: STREAM_CHUNK_ROWS).",
    )
    run_parser.add_argument(
        "--no-drift", action="store_true",
        help="Disable historical drift evaluation for this run, regardless of DRIFT_ENABLED.",
    )

    # validate-contract command
    val_parser = subparsers.add_parser("validate-contract", help="Validate a contract specification file")
    val_parser.add_argument("--contract", "-c", required=True, help="Path to contract JSON file")

    # verify command
    verify_parser = subparsers.add_parser("verify", help="Verify a signed audit certificate and its artifacts")
    verify_parser.add_argument("certificate", help="Path to an audit certificate JSON file")
    verify_parser.add_argument("--json", action="store_true", help="Emit the full verification report as JSON")

    # ledger command (query the durable boundary ledger)
    ledger_parser = subparsers.add_parser("ledger", help="Query the durable boundary ledger")
    ledger_sub = ledger_parser.add_subparsers(dest="ledger_command", required=True)

    def _add_ledger_scope(p: argparse.ArgumentParser, *, with_contract: bool = True) -> None:
        p.add_argument("--tenant", default="default-tenant", help="Tenant identity to query")
        if with_contract:
            p.add_argument("--contract", "-c", default=None, help="Filter by contract name")
        p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    summary_p = ledger_sub.add_parser("summary", help="Aggregate delivery health for a tenant")
    _add_ledger_scope(summary_p)

    runs_p = ledger_sub.add_parser("runs", help="List recent runs (newest first)")
    _add_ledger_scope(runs_p)
    runs_p.add_argument("--status", default=None, help="Filter by terminal status (LOADED/REJECTED/FAILED)")
    runs_p.add_argument("--limit", type=int, default=20, help="Maximum rows to return (default: 20)")
    runs_p.add_argument("--offset", type=int, default=0, help="Rows to skip before returning results")

    show_p = ledger_sub.add_parser("show", help="Show one run's full ledger record")
    show_p.add_argument("run_id", help="Run identifier")
    show_p.add_argument("--tenant", default="default-tenant", help="Tenant identity to query")
    show_p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    quarantine_p = ledger_sub.add_parser("quarantine", help="Search the quarantine index")
    _add_ledger_scope(quarantine_p)
    quarantine_p.add_argument("--run-id", default=None, help="Filter by run identifier")
    quarantine_p.add_argument("--kind", default=None, help="Filter by kind (held_quality/structural_refused)")
    quarantine_p.add_argument("--reason", default=None, help="Substring match on a violation reason")
    quarantine_p.add_argument("--limit", type=int, default=50, help="Maximum rows to return (default: 50)")

    usage_p = ledger_sub.add_parser("usage", help="Metering totals for a tenant")
    _add_ledger_scope(usage_p)
    usage_p.add_argument("--since", default=None, help="Only count usage at/after this ISO-8601 timestamp")

    trend_p = ledger_sub.add_parser("trend", help="Chronological delivery-volume trend")
    _add_ledger_scope(trend_p)
    trend_p.add_argument("--limit", type=int, default=30, help="Number of recent runs to include")

    prune_p = ledger_sub.add_parser("prune", help="Delete ledger rows older than a retention window")
    prune_p.add_argument("--days", type=int, default=None, help="Retention window in days (default: configured value)")
    prune_p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    # demo command
    demo_parser = subparsers.add_parser("demo", help="Run a self-contained refusal/tamper/verify walkthrough")
    demo_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    demo_parser.add_argument(
        "--scenario",
        choices=("full", "valid"),
        default="full",
        help="full: seeded-defect feed (refusals, quarantine, tamper recovery); "
             "valid: clean feed (every row delivered)",
    )

    # rules command (check-type catalog)
    rules_parser = subparsers.add_parser(
        "rules", help="List quality-rule check types: built-ins plus any installed plugins")
    rules_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    # benchmark command (throughput measurement on a synthetic feed)
    benchmark_parser = subparsers.add_parser(
        "benchmark", help="Measure boundary throughput on a deterministic synthetic feed")
    benchmark_parser.add_argument("--rows", type=int, default=500_000,
                                  help="Number of synthetic settlement rows (default: 500000)")
    benchmark_parser.add_argument("--out", default=None,
                                  help="Workspace directory for feed/delivery/artifacts (default: fresh temp dir)")
    benchmark_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    # doctor command (environment capability report)
    doctor_parser = subparsers.add_parser("doctor", help="Report which capabilities this environment has")
    doctor_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    # contract command (commercial contract management)
    contract_parser = subparsers.add_parser("contract", help="Manage commercial delivery contracts")
    contract_sub = contract_parser.add_subparsers(dest="contract_command", required=True)

    contract_validate_p = contract_sub.add_parser("validate", help="Validate a commercial contract JSON file")
    contract_validate_p.add_argument("contract", help="Path to commercial contract JSON file")

    contract_create_p = contract_sub.add_parser("create", help="Register an immutable commercial contract version")
    contract_create_p.add_argument("contract", help="Path to commercial contract JSON file")
    contract_create_p.add_argument("--tenant", default="default-tenant", help="Owning tenant identity")
    contract_create_p.add_argument("--actor", default="cli", help="Actor identity recorded as registrar")
    contract_create_p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    contract_show_p = contract_sub.add_parser("show", help="Show a commercial contract (latest or exact version)")
    contract_show_p.add_argument("contract_id", help="Contract identifier")
    contract_show_p.add_argument("--version", type=int, default=None, help="Exact contract version (default: latest)")
    contract_show_p.add_argument("--tenant", default="default-tenant", help="Owning tenant identity")
    contract_show_p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    contract_versions_p = contract_sub.add_parser("versions", help="List all versions of a commercial contract")
    contract_versions_p.add_argument("contract_id", help="Contract identifier")
    contract_versions_p.add_argument("--tenant", default="default-tenant", help="Owning tenant identity")
    contract_versions_p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    # settlement command
    settlement_parser = subparsers.add_parser("settlement", help="Calculate and inspect delivery settlements")
    settlement_sub = settlement_parser.add_subparsers(dest="settlement_command", required=True)

    stl_calc_p = settlement_sub.add_parser("calculate", help="Calculate the settlement for a completed run")
    stl_calc_p.add_argument("--run-id", required=True, help="Completed delivery run identifier")
    stl_calc_p.add_argument("--contract-id", required=True, help="Commercial contract identifier")
    stl_calc_p.add_argument("--contract-version", type=int, default=None,
                            help="Exact contract version (default: latest)")
    stl_calc_p.add_argument("--tenant", default="default-tenant", help="Tenant identity for all lookups")
    stl_calc_p.add_argument("--actor", default="cli", help="Actor identity recorded on the statement")
    stl_calc_p.add_argument("--idempotency-key", default=None,
                            help="Key making repeated submissions replay the original statement")
    stl_calc_p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    stl_show_p = settlement_sub.add_parser("show", help="Show one settlement statement")
    stl_show_p.add_argument("settlement_id", help="Settlement identifier")
    stl_show_p.add_argument("--tenant", default="default-tenant")
    stl_show_p.add_argument("--json", action="store_true")

    stl_list_p = settlement_sub.add_parser("list", help="List settlements")
    stl_list_p.add_argument("--status", default=None, help="Filter by settlement status")
    stl_list_p.add_argument("--contract-id", default=None, help="Filter by contract id")
    stl_list_p.add_argument("--tenant", default="default-tenant")
    stl_list_p.add_argument("--json", action="store_true")

    stl_explain_p = settlement_sub.add_parser("explain", help="Explain the full calculation behind a settlement")
    stl_explain_p.add_argument("settlement_id", help="Settlement identifier")
    stl_explain_p.add_argument("--tenant", default="default-tenant")
    stl_explain_p.add_argument("--json", action="store_true")

    stl_verify_p = settlement_sub.add_parser("verify", help="Verify a settlement's signed evidence")
    stl_verify_p.add_argument("settlement_id", help="Settlement identifier")
    stl_verify_p.add_argument("--tenant", default="default-tenant")
    stl_verify_p.add_argument("--json", action="store_true")

    stl_invoice_preview_p = settlement_sub.add_parser(
        "invoice-preview", help="Preview a draft invoice (never a paid invoice)")
    stl_invoice_preview_p.add_argument("settlement_id", help="Settlement identifier")
    stl_invoice_preview_p.add_argument("--tenant", default="default-tenant")
    stl_invoice_preview_p.add_argument("--actor", default="cli")
    stl_invoice_preview_p.add_argument("--json", action="store_true")

    stl_invoice_export_p = settlement_sub.add_parser(
        "invoice-export", help="Export a draft invoice document as JSON")
    stl_invoice_export_p.add_argument("settlement_id", help="Settlement identifier")
    stl_invoice_export_p.add_argument("--format", default="json", choices=["json"])
    stl_invoice_export_p.add_argument("--output", required=True, help="Output file path")
    stl_invoice_export_p.add_argument("--tenant", default="default-tenant")
    stl_invoice_export_p.add_argument("--actor", default="cli")

    stl_dispute_p = settlement_sub.add_parser("dispute-open", help="Open a dispute against a settlement")
    stl_dispute_p.add_argument("settlement_id", help="Settlement identifier")
    stl_dispute_p.add_argument("--party", required=True, help="Disputing party identity")
    stl_dispute_p.add_argument("--reason", required=True, help="Dispute reason")
    stl_dispute_p.add_argument("--explanation", default="", help="Supporting explanation")
    stl_dispute_p.add_argument("--evidence", nargs="*", default=None, help="Evidence references")
    stl_dispute_p.add_argument("--tenant", default="default-tenant")
    stl_dispute_p.add_argument("--actor", default="cli")
    stl_dispute_p.add_argument("--json", action="store_true")

    stl_dispute_resolve_p = settlement_sub.add_parser("dispute-resolve", help="Resolve an open dispute")
    stl_dispute_resolve_p.add_argument("dispute_id", help="Dispute identifier")
    stl_dispute_resolve_p.add_argument(
        "--resolution", required=True,
        choices=["ACCEPT_AS_IS", "REJECT_SETTLEMENT", "CORRECT_CALCULATION", "REPLACE_WITH_NEW_SETTLEMENT"])
    stl_dispute_resolve_p.add_argument("--reason", required=True, help="Resolution reason")
    stl_dispute_resolve_p.add_argument("--actor", required=True, help="Resolution actor")
    stl_dispute_resolve_p.add_argument("--tenant", default="default-tenant")
    stl_dispute_resolve_p.add_argument("--json", action="store_true")

    stl_billing_p = settlement_sub.add_parser(
        "billing-preview", help="Create or replay a draft billing instruction (no money movement)")
    stl_billing_p.add_argument("settlement_id", help="Settlement identifier")
    stl_billing_p.add_argument("--provider", default="dry-run", choices=["dry-run", "stripe"])
    stl_billing_p.add_argument("--idempotency-key", default=None)
    stl_billing_p.add_argument("--tenant", default="default-tenant")
    stl_billing_p.add_argument("--json", action="store_true")

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


# --------------------------------------------------------------------------- helpers


def _load_contract(path: str) -> DataContract:
    with open(path, encoding="utf-8") as f:
        return DataContract(**json.load(f))


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _print_table(headers: list[str], rows: list[list[Any]]) -> None:
    if not rows:
        print("(no records)")
        return
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        print(fmt.format(*[str(c) for c in row]))


# ----------------------------------------------------------------- commands: run


def _print_rejection_details(result: PipelineResult) -> None:
    """Explain exactly why a run was rejected: rule, count, expectation.

    Printed to stderr so scripts that parse stdout continue to work, while a
    human sees an actionable diagnosis instead of a single generic sentence.
    """
    reason = result.ledger.rejection_reason or "Violations exceeded declared contract gate."
    print(f"\n[!] FEED REJECTED: {reason}", file=sys.stderr)

    rule_breakdown = result.ledger.rule_breakdown or {}
    held_rules = {rule: count for rule, count in rule_breakdown.items() if count > 0}
    if held_rules:
        print("\n  Rule breakdown:", file=sys.stderr)
        for rule_id, count in sorted(held_rules.items()):
            print(f"    - {rule_id}: {count} rows held", file=sys.stderr)

    structural = result.ledger.structural_reasons or {}
    if structural:
        print("\n  Structural refusals:", file=sys.stderr)
        for failed_column, count in sorted(structural.items()):
            print(f"    - {failed_column}: {count} rows refused", file=sys.stderr)

    certificate = result.audit_certificate or {}
    batch_evaluations = certificate.get("batch_evaluations") or []
    failed_batch = [b for b in batch_evaluations if not b.get("passed", True)]
    display_batch = failed_batch or batch_evaluations
    if display_batch:
        print("\n  Batch rules:", file=sys.stderr)
        for batch in display_batch:
            status = "PASSED" if batch.get("passed", True) else "FAILED"
            print(
                f"    - {batch.get('rule_id')}: {status} "
                f"(actual={batch.get('actual')}, expected={batch.get('expected')})",
                file=sys.stderr,
            )

    failed_drift = [d for d in (certificate.get("drift_evaluations") or []) if not d.get("passed", True)]
    if failed_drift:
        print("\n  Drift checks:", file=sys.stderr)
        for drift in failed_drift:
            print(f"    - {drift.get('rule_id')}: FAILED — {drift.get('details')}", file=sys.stderr)

    print("\n  Run with --json for full details.", file=sys.stderr)
    print("  Or query the audit certificate for the per-rule breakdown.", file=sys.stderr)


def command_run(args: argparse.Namespace) -> int:
    with open(args.contract, encoding="utf-8") as f:
        data = json.load(f)
    contract = DataContract(**data)
    pipeline = DataShellPipeline(
        contract,
        tenant_id=args.tenant,
        drift_enabled=False if getattr(args, "no_drift", False) else None,
    )

    if not args.json:
        print(f"▶ Enforcing boundary on '{args.source}' against contract '{contract.metadata.name}'...")
        if args.no_drift:
            print("  Drift: disabled by --no-drift for this run")
        elif settings.drift_enabled:
            print("  Drift: enabled (DRIFT_ENABLED=true)")
        else:
            print("  Drift: disabled (DRIFT_ENABLED default is false)")
    source_path = Path(args.source)
    source_size = source_path.stat().st_size if source_path.is_file() else 0
    execution_mode = choose_execution_backend(
        source_size,
        force_streaming=args.streaming,
        ingest_mode=settings.ingest_mode,
        auto_threshold_bytes=settings.stream_auto_threshold_bytes,
    )
    use_streaming = execution_mode == EXECUTION_STREAMING
    if use_streaming:
        if not args.json:
            print(f"  Mode: out-of-core streaming (chunk size {args.chunk_size or settings.stream_chunk_rows:,} rows)")
        result = pipeline.run_streaming(
            source_path=args.source,
            chunk_size=args.chunk_size,
            target_path_override=args.target,
            audit_output_path=args.audit,
        )
    else:
        if not args.json:
            print("  Mode: in-memory vectorized")
        result = pipeline.run_file(
            source_path=args.source,
            target_path_override=args.target,
            audit_output_path=args.audit,
        )

    certificate = result.audit_certificate or {}
    if args.json:
        print(json.dumps({
            "run_id": result.run_id,
            "tenant_id": args.tenant,
            "status": result.status.value,
            "execution_mode": "out_of_core_streaming" if use_streaming else "in_memory",
            "duration_seconds": result.duration_seconds,
            "rows_read": result.ledger.rows_read,
            "refused_structural": result.ledger.refused_structural,
            "held_quality": result.ledger.held_quality,
            "rows_delivered": result.outcome.rows_delivered,
            "unaccounted_for": result.ledger.unaccounted_for,
            "footing_matched": result.ledger.footing_matched,
            "is_run_rejected": result.ledger.is_run_rejected,
            "rejection_reason": result.ledger.rejection_reason,
            "rule_breakdown": result.ledger.rule_breakdown,
            "structural_reasons": result.ledger.structural_reasons,
            "batch_evaluations": certificate.get("batch_evaluations", []),
            "drift_evaluations": certificate.get("drift_evaluations", []),
            "target_path": result.outcome.target_path,
            "quarantine_path": result.outcome.quarantine_path,
            "audit_path": result.audit_artifact_path,
        }, indent=2))
    else:
        print("\n" + result.ledger.summary_table())
        print(f"\nTerminal Status: {result.status.value}")
        print(f"Delivered rows:  {result.outcome.rows_delivered:,}")
        if result.outcome.target_path:
            print(f"Target:          {result.outcome.target_path}")
        if result.outcome.quarantine_path:
            print(f"Quarantine:      {result.outcome.quarantine_path}")
        print(f"Audit Proof:     {result.audit_artifact_path}")
        print(f"Execution time:  {result.duration_seconds:.2f}s")

        if result.status.value == "LOADED":
            print(f"\nOK Data was published to: {result.outcome.target_path}")
        elif result.status.value == "REJECTED":
            print("\nX No data was published. All rows were either refused or held.")
        elif result.status.value == "FAILED":
            print("\nX Engine failure. No data was published.")

    if result.status.value == "FAILED":
        return 1
    if result.status.value == "REJECTED":
        if not args.json:
            _print_rejection_details(result)
        return 2
    return 0


# ---------------------------------------------------- commands: validate-contract


def _plural(count: int, singular: str) -> str:
    return f"{count} {singular if count == 1 else singular + 's'}"


def command_validate_contract(args: argparse.Namespace) -> int:
    """Authoring-time contract validation with clear errors, summary, and warnings.

    Beyond model construction (which enforces schema presence, rule-column
    membership, duplicate rule IDs, and target-path sandboxing), this surfaces
    every problem in one place instead of stopping at the first exception, and
    warns about gaps that would silently weaken enforcement at run time.
    """
    contract_path = Path(args.contract)
    if not contract_path.is_file():
        print(f"✗ Contract not found: {contract_path}", file=sys.stderr)
        return 1
    try:
        with open(contract_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"✗ Contract file could not be parsed as JSON: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    try:
        contract = DataContract(**data)
    except ValidationError as exc:
        errors = exc.errors()
        print(
            f"✗ Contract '{contract_path}' is invalid "
            f"({len(errors)} error{'s' if len(errors) != 1 else ''}):",
            file=sys.stderr,
        )
        for err in errors:
            location = ".".join(str(part) for part in err.get("loc", ())) or "(contract)"
            message = str(err.get("msg", "invalid value")).replace("Value error, ", "")
            print(f"  - {location}: {message}", file=sys.stderr)
        return 1

    issues: list[str] = []
    warnings: list[str] = []

    sources = contract.spec.sources
    if not sources or not sources[0].schema_:
        issues.append("At least one source with a non-empty schema is required")
    declared_columns = {column.name for column in (sources[0].schema_ if sources else [])}

    seen_rule_ids = set()
    for rule in contract.spec.quality.rules:
        if rule.rule_id in seen_rule_ids:
            issues.append(f"Duplicate rule_id '{rule.rule_id}'")
        seen_rule_ids.add(rule.rule_id)
        if rule.column and rule.column not in declared_columns:
            issues.append(
                f"Row rule '{rule.rule_id}' references column '{rule.column}' which is not in the declared schema"
            )
    for batch_rule in contract.spec.quality.batch_rules:
        if batch_rule.rule_id in seen_rule_ids:
            issues.append(f"Duplicate rule_id '{batch_rule.rule_id}'")
        seen_rule_ids.add(batch_rule.rule_id)
        referenced_columns = ([batch_rule.column] if batch_rule.column else []) + list(batch_rule.columns or [])
        for column in referenced_columns:
            if column not in declared_columns:
                issues.append(
                    f"Batch rule '{batch_rule.rule_id}' references column '{column}' "
                    "which is not in the declared schema"
                )

    for target in contract.spec.delivery.targets:
        try:
            FilesystemSandbox.sanitize_and_validate_path(target.path, create_parent=False)
        except Exception as exc:
            issues.append(f"Delivery target '{target.path}' is outside the allowed storage roots: {exc}")

    if not contract.spec.quality.rules:
        warnings.append("No row rules defined — Tier 2 semantic validation will admit all structurally valid rows")
    if not contract.spec.quality.batch_rules:
        warnings.append("No batch rules defined — consider adding MIN_ROW_COUNT for feed volume validation")
    if not contract.spec.delivery.targets:
        warnings.append("No delivery targets defined — runs will fall back to ./output/delivered.parquet")

    if issues:
        print(
            f"✗ Contract '{contract.metadata.name}' v{contract.metadata.version} is invalid "
            f"({len(issues)} issue{'s' if len(issues) != 1 else ''}):",
            file=sys.stderr,
        )
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        for warning in warnings:
            print(f"  WARNING: {warning}", file=sys.stderr)
        return 1

    print(
        f"✓ Contract '{contract.metadata.name}' v{contract.metadata.version} is valid. "
        f"{_plural(len(contract.spec.quality.rules), 'row rule')}, "
        f"{_plural(len(contract.spec.quality.batch_rules), 'batch rule')}, "
        f"{_plural(len(contract.spec.delivery.targets), 'delivery target')}."
    )
    for warning in warnings:
        print(f"  WARNING: {warning}")
    return 0


# --------------------------------------------------------------- commands: doctor


def _importable(module_name: str) -> tuple[bool, str | None]:
    """Return ``(importable, version_or_none)`` without executing heavy init twice."""
    import importlib

    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return False, None
    return True, str(getattr(module, "__version__", "unknown"))


# Capability probes for the optional extras: extra name, then the modules that
# prove it is installed, as (distribution name, import path).
#
# The import path must be the one the capability actually loads at runtime,
# not the distribution's name: a distribution name is not an import path
# ("python-multipart" is imported as "multipart"), and the tracing extra
# installs ``opentelemetry-exporter-otlp-proto-grpc``, whose import path is
# ``opentelemetry.exporter.otlp.proto.grpc`` — the same module
# ``datashell.core.observability`` loads. Probing the wrong path reports a
# correctly installed extra as missing, which is exactly the misdiagnosis this
# table must not make.
#
# Tracing intent is configuration: the endpoint env turns it on, the extra
# provides the SDK. Doctor reports both halves separately, so "requested but
# missing extra" is diagnosable without guessing.
EXTRA_MODULE_PROBES: dict[str, tuple[tuple[str, str], ...]] = {
    "server": (("fastapi", "fastapi"),
               ("uvicorn", "uvicorn"),
               ("python-multipart", "multipart")),
    "duckdb": (("duckdb", "duckdb"),),
    "billing": (("httpx", "httpx"),),
    "airflow": (("apache-airflow", "airflow"),),
    "otel": (("opentelemetry-sdk", "opentelemetry.sdk"),
             ("opentelemetry-exporter-otlp-proto-grpc", "opentelemetry.exporter.otlp.proto.grpc")),
}


def command_doctor(args: argparse.Namespace) -> int:
    """Environment capability report: what this install can and cannot do.

    ``datashell`` is a feature-partitioned install: the enforcement core is one
    dependency set, and the gateway/batch-SQL/billing capabilities are optional
    extras. ``doctor`` tells an SRE or a CI script exactly which capabilities
    the current environment has, and prints the extra name that supplies any
    missing one. Core capability failures exit 1; missing optional extras do
    not (they are configuration choices, not breakage).
    """
    core_modules = (
        ("polars", "polars"),
        ("pyarrow", "pyarrow"),
        ("pydantic", "pydantic"),
        ("cryptography", "cryptography"),
        ("prometheus_client", "prometheus_client"),
    )

    core_status: dict[str, Any] = {}
    failures: list[str] = []
    for label, module_name in core_modules:
        ok, version = _importable(module_name)
        core_status[label] = {"available": ok, "version": version}
        if not ok:
            failures.append(f"core dependency '{label}' is not importable")

    extras_status: dict[str, Any] = {}
    for extra_name, modules in EXTRA_MODULE_PROBES.items():
        entries: dict[str, Any] = {}
        available = True
        for label, module_name in modules:
            ok, version = _importable(module_name)
            entries[label] = {"available": ok, "version": version}
            available = available and ok
        extras_status[extra_name] = {"available": available, "modules": entries}

    # Environment sanity: storage root writable; production self-checks.
    environment_status: dict[str, Any] = {}
    try:
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.output_dir / ".datashell_doctor.write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        environment_status["storage_root_writable"] = {
            "available": True,
            "path": str(settings.output_dir),
        }
    except OSError as exc:
        environment_status["storage_root_writable"] = {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        failures.append("storage root is not writable")

    # Production posture: EnterpriseSettings validates secrets/CORS/auth on
    # construction, so reaching this point means construction passed. Re-run it
    # against an isolated copy to surface the actionable message for SREs.
    try:
        production_copy = settings.model_copy(deep=True)
        production_copy.model_post_init(None)
        environment_status["production_security_check"] = {"available": True}
    except RuntimeError as exc:
        environment_status["production_security_check"] = {"available": False, "error": str(exc)}
        if settings.is_production_mode:
            failures.append(f"production security check failed: {exc}")

    environment_status["environment"] = settings.environment
    environment_status["auth_enabled"] = settings.auth_enabled

    healthy = not failures

    if args.json:
        print(json.dumps({
            "version": __version__,
            "python": "{}.{}.{}".format(*sys.version_info[:3]),
            "healthy": healthy,
            "core": core_status,
            "extras": extras_status,
            "environment": environment_status,
            "failures": failures,
        }, indent=2))
        return 0 if healthy else 1

    print(f"datashell doctor — version {__version__} (Python {sys.version_info.major}.{sys.version_info.minor})")
    print()
    print("Core enforcement engine:")
    for label, _import_name in core_modules:
        entry = core_status[label]
        mark = "✓" if entry["available"] else "✗"
        version_note = f" (v{entry['version']})" if entry["available"] else ""
        print(f"  {mark} {label}{version_note}")
    print()
    print("Optional capabilities (enable with pip install \"datashell-engine[<extra>]\"):")
    for extra_name, modules in EXTRA_MODULE_PROBES.items():
        entry = extras_status[extra_name]
        mark = "✓" if entry["available"] else "○"
        detail = ", ".join(
            f"{label}: {'present' if entry['modules'][label]['available'] else 'missing'}"
            for label, _module_name in modules
        )
        print(f"  {mark} [{extra_name}] — {detail}")
    print()
    print("Environment:")
    print(f"  ENVIRONMENT={settings.environment}  auth={'enabled' if settings.auth_enabled else 'disabled'}")
    writable = environment_status["storage_root_writable"]["available"]
    print(f"  storage root {'writable' if writable else 'NOT writable'} ({settings.output_dir})")

    if failures:
        print()
        print("✗ Environment problems detected:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    if not all(v["available"] for v in extras_status.values()):
        print()
        print("○ Optional extras are missing — install the ones your deployment needs, e.g. "
              "pip install \"datashell-engine[server,duckdb]\"")
    else:
        print()
        print("✓ All capabilities available.")
    return 0


# --------------------------------------------------------------- commands: verify


def command_verify(args: argparse.Namespace) -> int:
    cert_path = Path(args.certificate)
    if not cert_path.is_file():
        print(f"✗ Certificate not found: {cert_path}", file=sys.stderr)
        return 1
    try:
        with open(cert_path, encoding="utf-8") as f:
            cert_data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"✗ Certificate could not be read: {type(exc).__name__}", file=sys.stderr)
        return 1

    report = ArtifactGenerator.verify_certificate_and_artifacts(cert_data)
    report.update(_committed_generation_check(cert_data))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        status = report.get("status", "UNKNOWN")
        marker = "✓" if status == "AUTHENTIC_AND_UNMODIFIED" else "✗"
        print(f"{marker} Certificate verification: {status}")
        print(f"    signature valid: {report.get('signature_valid')}")
        print(f"    key id:          {report.get('key_id')} (trusted: {report.get('key_trusted')})")
        print(f"    artifacts:       {report.get('artifacts_status')}")
        for name, detail in (report.get("artifact_details") or {}).items():
            detail_status = detail.get("status") if isinstance(detail, dict) else detail
            print(f"      - {name}: {detail_status}")
        committed = report.get("committed_generation")
        if committed is not None:
            same = report.get("is_committed_generation")
            note = "yes" if same else "no (a newer generation is committed)"
            print(f"    committed generation: {committed} (this certificate: {note})")

    return 0 if report.get("status") == "AUTHENTIC_AND_UNMODIFIED" else 3


def _committed_generation_check(cert_data: dict[str, Any]) -> dict[str, Any]:
    """Report whether the certificate is the currently committed generation.

    Delegates to the shared reader contract in ``datashell.core.commit`` so the
    CLI verifier and the HTTP certificate endpoint cannot drift apart.
    """
    from datashell.core.commit import committed_generation_status

    return committed_generation_status(cert_data)


# --------------------------------------------------------------- commands: ledger


def _ledger_json_or(payload: Any, args: argparse.Namespace) -> bool:
    """Print JSON when requested; return True if handled."""
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
        return True
    return False


def command_ledger(args: argparse.Namespace) -> int:
    ledger = BoundaryLedger()
    tenant = getattr(args, "tenant", "default-tenant")
    contract = getattr(args, "contract", None)

    if args.ledger_command == "summary":
        summary = ledger.history_summary(tenant, contract_name=contract)
        if _ledger_json_or(summary, args):
            return 0
        print(f"Boundary ledger summary — tenant '{tenant}'" + (f" · contract '{contract}'" if contract else ""))
        print(f"  Total runs:         {summary['total_runs']:,}")
        print(f"  Delivered runs:     {summary['delivered_runs']:,}")
        print(f"  Rejected runs:      {summary['rejected_runs']:,}")
        print(f"  Footing mismatches: {summary['footing_mismatches']:,}")
        print(f"  Rows read:          {summary['rows_read']:,}")
        print(f"  Rows delivered:     {summary['rows_delivered']:,}")
        print(f"  Rows refused:       {summary['rows_refused']:,}")
        print(f"  Rows held:          {summary['rows_held']:,}")
        print(f"  Delivery rate:      {summary['delivery_rate'] * 100:.2f}%")
        print(f"  Last run at:        {summary['last_run_at'] or '—'}")
        return 0

    if args.ledger_command == "runs":
        runs = ledger.list_runs(tenant, contract_name=contract, status=args.status,
                                limit=args.limit, offset=args.offset)
        total = ledger.count_runs(tenant, contract_name=contract, status=args.status)
        if _ledger_json_or({"tenant_id": tenant, "total": total, "runs": runs}, args):
            return 0
        print(f"Runs for tenant '{tenant}' ({len(runs)} of {total})")
        _print_table(
            ["RUN ID", "STATUS", "CONTRACT", "READ", "DELIVERED", "HELD", "REFUSED", "MODE", "TIMESTAMP"],
            [
                [
                    r["run_id"], r["status"] or "—", r["contract_name"], r["rows_read"],
                    r["rows_delivered"], r["rows_held"], r["rows_refused"],
                    r["execution_mode"] or "—", r["timestamp"],
                ]
                for r in runs
            ],
        )
        return 0

    if args.ledger_command == "show":
        run = ledger.get_run(args.run_id, tenant)
        if not run:
            print(f"✗ Run '{args.run_id}' not found for tenant '{tenant}'", file=sys.stderr)
            return 1
        if _ledger_json_or(run, args):
            return 0
        for key in (
            "run_id", "tenant_id", "contract_name", "contract_version", "status", "timestamp",
            "rows_read", "rows_delivered", "rows_refused", "rows_held", "unaccounted_for",
            "footing_matched", "is_run_rejected", "rejection_reason", "duration_seconds",
            "execution_mode", "source_name", "source_bytes", "target_path", "quarantine_path",
            "audit_path", "merkle_root", "signature_key_id",
        ):
            print(f"  {key:<18} {run.get(key)}")
        if run.get("rule_breakdown"):
            print("  rule_breakdown:")
            for rule, count in run["rule_breakdown"].items():
                print(f"    - {rule}: {count}")
        return 0

    if args.ledger_command == "quarantine":
        records = ledger.list_quarantine(
            tenant, contract_name=contract, run_id=args.run_id,
            kind=args.kind, reason=args.reason, limit=args.limit,
        )
        if _ledger_json_or({"tenant_id": tenant, "quarantine": records}, args):
            return 0
        print(f"Quarantine index for tenant '{tenant}' ({len(records)} artifacts)")
        _print_table(
            ["RUN ID", "KIND", "ROWS", "DOMINANT REASON", "ARTIFACT", "TIMESTAMP"],
            [
                [r["run_id"], r["kind"], r["row_count"], r["dominant_reason"] or "—",
                 Path(r["artifact_path"]).name if r["artifact_path"] else "—", r["timestamp"]]
                for r in records
            ],
        )
        return 0

    if args.ledger_command == "usage":
        summary = ledger.usage_summary(tenant, contract_name=contract, since=args.since)
        if _ledger_json_or(summary, args):
            return 0
        print(f"Usage for tenant '{tenant}'" + (f" · contract '{contract}'" if contract else ""))
        print(f"  Metered runs:    {summary['total_runs']:,}")
        print(f"  Total units:     {summary['total_units']:,} ({summary['unit']})")
        print(f"  Billable units:  {summary['billable_units']:,} across {summary['billable_runs']:,} runs")
        print(f"  Rows read:       {summary['rows_read']:,}")
        print(f"  Rows delivered:  {summary['rows_delivered']:,}")
        print(f"  Bytes processed: {summary['bytes_processed']:,}")
        return 0

    if args.ledger_command == "trend":
        trend = ledger.volume_trend(tenant, contract_name=contract, limit=args.limit)
        if _ledger_json_or({"tenant_id": tenant, "trend": trend}, args):
            return 0
        print(f"Delivery-volume trend for tenant '{tenant}' ({len(trend)} runs, oldest first)")
        _print_table(
            ["TIMESTAMP", "STATUS", "READ", "DELIVERED", "HELD", "REFUSED", "FOOTED"],
            [
                [r["timestamp"], r["status"] or "—", r["rows_read"], r["rows_delivered"],
                 r["rows_held"], r["rows_refused"], "yes" if r["footing_matched"] else "NO"]
                for r in trend
            ],
        )
        return 0

    if args.ledger_command == "prune":
        removed = ledger.prune(args.days)
        if args.json:
            print(json.dumps(removed, indent=2))
        else:
            if not any(removed.values()):
                print("Nothing pruned (history retention is disabled or no rows are older than the window).")
            else:
                for table, count in removed.items():
                    print(f"  {table}: {count:,} rows removed")
        return 0

    print("Unknown ledger subcommand", file=sys.stderr)
    return 2


# ----------------------------------------------------------------- commands: demo

_DEMO_CONTRACT: dict[str, Any] = {
    "apiVersion": "daas/v1",
    "kind": "DataProduct",
    "metadata": {
        "name": "demo-vendor-settlements",
        "owner": "finance-data",
        "version": "1.0.0",
        "status": "ACTIVE",
        "description": "Demonstration contract: enforces vendor settlements at the boundary",
    },
    "spec": {
        "sources": [
            {
                "type": "LOCAL",
                "connectionRef": "local-landing",
                "path": "incoming/",
                "format": "jsonl",
                "schema": [
                    {"name": "settlement_id", "polars_type": "Utf8", "nullable": False},
                    {"name": "amount", "polars_type": "Float64", "nullable": False},
                    {"name": "currency", "polars_type": "Utf8", "nullable": False},
                ],
            }
        ],
        "quality": {
            "behavior": "DROP_ROW",
            "rules": [
                {"rule_id": "amount-positive", "check": "GREATER_THAN", "column": "amount", "value": 0,
                 "description": "Settlement amount must be strictly greater than 0"},
                {"rule_id": "currency-settled", "check": "ALLOWED_VALUES", "column": "currency",
                 "value": ["USD", "EUR", "GBP"],
                 "description": "Currency must be one of the authorized settlement currencies"},
            ],
            "batch_rules": [
                {"rule_id": "not-partial", "check": "MIN_ROW_COUNT", "value": 3,
                 "description": "Feed batch must contain at least 3 surviving records"},
            ],
        },
    },
}


def _demo_records(scenario: str = "full") -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for i in range(1, 901):
        records.append({
            "settlement_id": f"SETTLE-{1000 + i}",
            "amount": round(10.0 + (i * 1.5), 2),
            "currency": "USD" if i % 2 == 0 else "EUR",
        })
    if scenario == "valid":
        # Clean feed: no seeded defects, every row survives the contract.
        return records
    for i in range(1, 21):
        records.append({"settlement_id": f"SETTLE-BAD-AMT-{i}", "amount": "NOT_A_FLOAT", "currency": "USD"})
    for _ in range(21, 41):
        records.append({"settlement_id": None, "amount": 250.0, "currency": "GBP"})
    for i in range(1, 36):
        records.append({"settlement_id": f"SETTLE-NEG-{i}", "amount": -50.0, "currency": "USD"})
    for i in range(36, 61):
        records.append({"settlement_id": f"SETTLE-CURR-{i}", "amount": 120.0, "currency": "JPY"})
    return records


# --------------------------------------------------------------------------- benchmark

# The benchmark is self-contained (it must run in wheel-only installs without
# the repository's examples directory), so the feed contract ships as a literal.
# tests/test_benchmark_cli.py pins this dict to examples/vendor_settlements_contract.json
# so the two definitions cannot drift.
_BENCHMARK_CONTRACT: dict[str, Any] = {
    "apiVersion": "daas/v1",
    "kind": "DataProduct",
    "metadata": {
        "name": "vendor-settlements",
        "owner": "finance-data",
        "version": "1.0.0",
        "status": "ACTIVE",
        "description": "Enforces settlements incoming from external vendor feeds",
    },
    "spec": {
        "sources": [{
            "type": "LOCAL",
            "connectionRef": "local-landing",
            "path": "incoming/settlements/",
            "format": "jsonl",
            "schema": [
                {"name": "settlement_id", "polars_type": "Utf8", "nullable": False},
                {"name": "amount", "polars_type": "Float64", "nullable": False},
                {"name": "currency", "polars_type": "Utf8", "nullable": False},
            ],
        }],
        "quality": {
            "behavior": "DROP_ROW",
            "rules": [
                {"rule_id": "id-present", "check": "NOT_NULL", "column": "settlement_id",
                 "description": "Settlement ID must not be null"},
                {"rule_id": "amount-positive", "check": "GREATER_THAN", "column": "amount",
                 "value": 0, "description": "Settlement amount must be strictly greater than 0"},
                {"rule_id": "currency-settled", "check": "ALLOWED_VALUES", "column": "currency",
                 "value": ["USD", "EUR", "GBP"],
                 "description": "Currency must be one of authorized settlement currencies"},
            ],
            "batch_rules": [
                {"rule_id": "not-partial", "check": "MIN_ROW_COUNT", "value": 3,
                 "description": "Feed batch must contain at least 3 surviving records"},
            ],
        },
        "transformation": {"map": [], "reduce": []},
        "delivery": {
            "refreshSchedule": "0 * * * *",
            "targets": [{
                "type": "PARQUET",
                "connectionRef": "curated-storage",
                "path": "./output/curated/settlements.parquet",
                "mode": "overwrite",
            }],
            "quarantinePath": "./output/quarantine",
        },
    },
}


# --------------------------------------------------------------------------- rules


def command_rules(args: argparse.Namespace) -> int:
    """List every quality-rule check type: built-ins first, then plugins."""
    from datashell.contract import rule_registry

    rows = [(name, "row", source) for name, source in rule_registry.available_row_checks().items()]
    rows += [(name, "batch", source) for name, source in rule_registry.available_batch_checks().items()]

    errors = rule_registry.plugin_load_errors()
    if args.json:
        print(json.dumps({"checks": rows, "plugin_errors": errors}, indent=2))
        return 0

    width = max((len(name) for name, _, _ in rows), default=4)
    for name, kind, source in sorted(rows, key=lambda r: (r[1] != "row", r[2] != "builtin", r[0])):
        marker = "" if source == "builtin" else f"  ({source})"
        print(f"{name:<{width}}  {kind:<5}{marker}")
    for error in errors:
        print(f"PLUGIN ERROR: {error}", file=sys.stderr)
    return 0


def command_benchmark(args: argparse.Namespace) -> int:
    """Measure boundary throughput on a deterministic synthetic feed.

    Everything is local: the feed is generated in the output workspace, the
    in-memory engine enforces the bundled settlement contract, and the run
    flows through the real delivery gate, ledger record, and audit
    certificate. This is the smallest honest number a mixture of python and
    rust can produce on this machine; it is not a distributed-system claim.
    """
    import resource
    import time

    from pyarrow import Table, parquet

    contract = DataContract(**_BENCHMARK_CONTRACT)
    rows = int(args.rows)
    if rows <= 0:
        print("ERROR: --rows must be positive", file=sys.stderr)
        return 2

    out_dir = Path(args.out or tempfile.mkdtemp(prefix="datashell_benchmark_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    source = out_dir / f"feed_{rows}.parquet"
    target = out_dir / "curated.parquet"
    audit = out_dir / f"benchmark_{rows}_audit_certificate.json"

    table = Table.from_pydict({
        "settlement_id": [f"B{i:09d}" for i in range(rows)],
        "amount": [100.0 + ((i * 37) % 1000) / 4.0 for i in range(rows)],
        "currency": [("USD", "EUR", "GBP")[i % 3] for i in range(rows)],
    })
    parquet.write_table(table, source)

    pipeline = DataShellPipeline(contract, tenant_id="benchmark-tenant", drift_enabled=False)
    if not args.json:
        print(f"▶ Benchmark: {rows:,} settlement rows against '{contract.metadata.name}'")
        print(f"  Feed: {source} ({source.stat().st_size / 1_048_576:.2f} MiB)")

    wall_start = time.perf_counter()
    result = pipeline.run_file(
        source_path=source,
        target_path_override=str(target),
        audit_output_path=str(audit),
    )
    wall = time.perf_counter() - wall_start

    peak_rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    report = {
        "rows": rows,
        "feed_bytes": source.stat().st_size,
        "wall_seconds": round(wall, 3),
        "pipeline_seconds": round(result.duration_seconds or 0.0, 3),
        "rows_per_second": int(rows / wall) if wall > 0 else 0,
        "status": result.status.value,
        "execution_mode": "in_memory",
        "rows_read": result.ledger.rows_read,
        "rows_delivered": result.outcome.rows_delivered,
        "held_quality": result.ledger.held_quality,
        "refused_structural": result.ledger.refused_structural,
        "unaccounted_for": result.ledger.unaccounted_for,
        "footing_matched": result.ledger.footing_matched,
        "peak_rss_mib": round(peak_rss_mib, 1),
        "run_id": result.run_id,
        "audit_certificate": str(result.audit_artifact_path),
        "output_dir": str(out_dir),
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("  Reports the smallest honest single-node number; it is not a distributed claim.")
        print(f"  Wall: {report['wall_seconds']}s (pipeline {report['pipeline_seconds']}s)"
              f" · {report['rows_per_second']:,} rows/s")
        print(f"  Status: {report['status']} · read {report['rows_read']:,}"
              f" · delivered {report['rows_delivered']:,}"
              f" · held {report['held_quality']:,}"
              f" · refused {report['refused_structural']:,}"
              f" · unaccounted {report['unaccounted_for']:,}")
        print(f"  Footing matched: {report['footing_matched']} · peak RSS {report['peak_rss_mib']} MiB")
        print(f"  Run: {report['run_id']} · artifacts in {report['output_dir']}")
    return 0


def command_demo(args: argparse.Namespace) -> int:
    """Self-contained proof: refuse bad rows, publish good ones, detect tampering."""
    workspace = Path(tempfile.mkdtemp(prefix="datashell_demo_"))
    if settings.is_production_mode:
        # The production sandbox authorizes delivery only inside a tenant
        # delivery namespace — system temp directories are staging space, not
        # delivery targets — and audit certificates plus their underlying
        # artifacts (delivered rows, quarantine records) must remain under the
        # output root to be verifiable. The walkthrough therefore delivers into
        # a dedicated, explicitly authorized demo tenant directory, quarantines
        # into that tenant's isolated quarantine directory, and writes a
        # durable certificate. The run is scoped to the same tenant so the
        # boundary checks agree.
        demo_tenant = "datashell-demo"
        target = (
            settings.get_tenant_delivery_dir(demo_tenant)
            / f"curated_settlements_{uuid.uuid4().hex[:12]}.parquet"
        )
        quarantine = settings.get_tenant_quarantine_dir(demo_tenant)
        audit = settings.audit_dir / f"demo_walkthrough_{uuid.uuid4().hex[:12]}_audit_certificate.json"
    else:
        demo_tenant = "default-tenant"
        target = workspace / "curated_settlements.parquet"
        quarantine = workspace / "quarantine"
        audit = workspace / "run_audit.json"

    contract = DataContract(**_demo_contract_payload(workspace, target, quarantine))
    records = _demo_records(args.scenario)

    if not args.json:
        print("=" * 68)
        print("      DATA SHELL — BOUNDARY ENFORCEMENT QUICKSTART DEMO")
        print("         'Bad data never lands, and never leaves.'")
        print("=" * 68)
        print(f"\n[1] Contract '{contract.metadata.name}' v{contract.metadata.version}")
        print(f"    Row rules:   {[r.rule_id for r in contract.spec.quality.rules]}")
        print(f"    Batch rules: {[b.rule_id for b in contract.spec.quality.batch_rules]}")
        if args.scenario == "valid":
            print(f"\n[2] Generated {len(records):,} feed records (clean — no seeded defects)")
        else:
            print(f"\n[2] Generated {len(records):,} feed records (900 valid, 100 seeded defects)")

    pipeline = DataShellPipeline(contract, tenant_id=demo_tenant)
    result = pipeline.run_records(
        records=records,
        target_path_override=str(target),
        audit_output_path=str(audit),
    )

    verify_status = result_verify_status(result.audit_artifact_path)

    if not args.json:
        print("\n[3] Boundary enforcement result:")
        print(result.ledger.summary_table())
        print(f"\n    Terminal status: {result.status.value}")
        print(f"    Delivered rows:  {result.outcome.rows_delivered:,} -> {target}")
        print(f"    Quarantine:      {result.outcome.quarantine_path}")
        print(f"    Audit proof:     {audit}")
        print("\n[3a] Held rows by rule:")
        for rule_id, count in sorted(result.ledger.rule_breakdown.items()):
            print(f"      - {rule_id:<20}: {count:>4} held")
        print("[3b] Structural refusals at ingestion:")
        for reason, count in sorted(result.ledger.structural_reasons.items()):
            print(f"      - {reason:<20}: {count:>4} refused")
        print(f"\n[4] Verify the signed certificate -> {verify_status}")

    # Tamper demonstration: flip one byte in the delivered artifact, prove the
    # certificate detects it, then restore the authentic bytes and prove recovery.
    tamper_status: str | None = None
    restored_status: str | None = None
    if result.outcome.rows_delivered > 0 and target.is_file():
        original = target.read_bytes()
        tampered = bytearray(original)
        tampered[0] ^= 0xFF
        target.write_bytes(bytes(tampered))
        tamper_status = result_verify_status(result.audit_artifact_path)
        target.write_bytes(original)
        restored_status = result_verify_status(result.audit_artifact_path)

        if not args.json:
            print(f"[5] Tamper the delivered parquet (1 byte) and re-verify -> {tamper_status}")
            print(f"[6] Restore the authentic bytes and re-verify           -> {restored_status}")
            print("\n✓ Bad rows were refused, good rows landed, and tampering was detected and resolved.")
    elif not args.json:
        print("[5] No rows delivered; skipping the tamper demonstration.")


    if args.json:
        print(json.dumps({
            "scenario": args.scenario,
            "run_id": result.run_id,
            "status": result.status.value,
            # The walkthrough always runs the in-memory records path; the mode
            # is fixed by construction, not measured.
            "execution_mode": "in_memory",
            "rows_read": result.ledger.rows_read,
            "rows_refused": result.ledger.refused_structural,
            "rows_held": result.ledger.held_quality,
            "rows_delivered": result.outcome.rows_delivered,
            "footing_matched": result.ledger.footing_matched,
            "rule_breakdown": result.ledger.rule_breakdown,
            "structural_reasons": result.ledger.structural_reasons,
            "audit_path": result.audit_artifact_path,
            "verify_status": verify_status,
            "tamper_status": tamper_status,
            "restored_status": restored_status,
            "workspace": str(workspace),
        }, indent=2))

    return 0 if result.status.value != "FAILED" else 1


def _demo_contract_payload(workspace: Path, target: Path, quarantine: Path) -> dict[str, Any]:
    payload = cast(dict[str, Any], json.loads(json.dumps(_DEMO_CONTRACT)))
    payload["spec"]["sources"][0]["path"] = str(workspace / "incoming.jsonl")
    payload["spec"]["delivery"] = {
        "targets": [{"type": "PARQUET", "connectionRef": "local", "path": str(target), "mode": "overwrite"}],
        "quarantinePath": str(quarantine),
    }
    return payload


def result_verify_status(audit_path: str | None) -> str:
    if not audit_path or not Path(audit_path).is_file():
        return "NO_CERTIFICATE"
    with open(audit_path, encoding="utf-8") as f:
        cert = cast(dict[str, Any], json.load(f))
    return str(ArtifactGenerator.verify_certificate_and_artifacts(cert).get("status", "UNKNOWN"))


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-contract":
            sys.exit(command_validate_contract(args))

        if args.command == "run":
            sys.exit(command_run(args))

        if args.command == "verify":
            sys.exit(command_verify(args))

        if args.command == "ledger":
            sys.exit(command_ledger(args))

        if args.command == "demo":
            sys.exit(command_demo(args))

        if args.command == "benchmark":
            sys.exit(command_benchmark(args))

        if args.command == "rules":
            sys.exit(command_rules(args))

        if args.command == "doctor":
            sys.exit(command_doctor(args))

        if args.command == "contract":
            from datashell.cli.settlement_commands import (
                command_contract_create,
                command_contract_show,
                command_contract_validate,
                command_contract_versions,
            )
            handlers = {
                "validate": command_contract_validate,
                "create": command_contract_create,
                "show": command_contract_show,
                "versions": command_contract_versions,
            }
            sys.exit(handlers[args.contract_command](args))

        if args.command == "settlement":
            from datashell.cli.settlement_commands import (
                command_billing_preview,
                command_dispute_open,
                command_dispute_resolve,
                command_invoice_export,
                command_invoice_preview,
                command_settlement_calculate,
                command_settlement_explain,
                command_settlement_list,
                command_settlement_show,
                command_settlement_verify,
            )
            handlers = {
                "calculate": command_settlement_calculate,
                "show": command_settlement_show,
                "list": command_settlement_list,
                "explain": command_settlement_explain,
                "verify": command_settlement_verify,
                "invoice-preview": command_invoice_preview,
                "invoice-export": command_invoice_export,
                "dispute-open": command_dispute_open,
                "dispute-resolve": command_dispute_resolve,
                "billing-preview": command_billing_preview,
            }
            sys.exit(handlers[args.settlement_command](args))

        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\nFATAL: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
