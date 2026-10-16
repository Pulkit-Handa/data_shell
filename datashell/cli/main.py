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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from datashell import __version__
from datashell.core.config import settings
from datashell.core.ledger import BoundaryLedger
from datashell.contract.models import DataContract
from datashell.pipeline.artifacts import ArtifactGenerator
from datashell.pipeline.runner import DataShellPipeline


# --------------------------------------------------------------------------- args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datashell",
        description="Data Shell: Contract-driven boundary enforcement engine. Bad data never lands, and never leaves.",
    )
    parser.add_argument("--version", action="version", version=f"data-shell {__version__}")
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

    def _add_ledger_scope(p, *, with_contract: bool = True) -> None:
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

    return parser


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


# --------------------------------------------------------------------------- helpers


def _load_contract(path: str) -> DataContract:
    with open(path, "r", encoding="utf-8") as f:
        return DataContract(**json.load(f))


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _print_table(headers: List[str], rows: List[List[Any]]) -> None:
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


def command_run(args: argparse.Namespace) -> int:
    with open(args.contract, "r", encoding="utf-8") as f:
        data = json.load(f)
    contract = DataContract(**data)
    pipeline = DataShellPipeline(contract, tenant_id=args.tenant)

    if not args.json:
        print(f"▶ Enforcing boundary on '{args.source}' against contract '{contract.metadata.name}'...")
    source_path = Path(args.source)
    source_size = source_path.stat().st_size if source_path.is_file() else 0
    use_streaming = (
        args.streaming
        if args.streaming is not None
        else source_size >= settings.stream_auto_threshold_bytes
    )
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
            "rule_breakdown": result.ledger.rule_breakdown,
            "structural_reasons": result.ledger.structural_reasons,
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

    if result.status.value == "FAILED":
        return 1
    if result.status.value == "REJECTED":
        if not args.json:
            print("\n[!] FEED REJECTED: Violations exceeded declared contract gate.", file=sys.stderr)
        return 2
    return 0


# --------------------------------------------------------------- commands: verify


def command_verify(args: argparse.Namespace) -> int:
    cert_path = Path(args.certificate)
    if not cert_path.is_file():
        print(f"✗ Certificate not found: {cert_path}", file=sys.stderr)
        return 1
    try:
        with open(cert_path, "r", encoding="utf-8") as f:
            cert_data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"✗ Certificate could not be read: {type(exc).__name__}", file=sys.stderr)
        return 1

    report = ArtifactGenerator.verify_certificate_and_artifacts(cert_data)
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

    return 0 if report.get("status") == "AUTHENTIC_AND_UNMODIFIED" else 3


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
        runs = ledger.list_runs(tenant, contract_name=contract, status=args.status, limit=args.limit, offset=args.offset)
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

_DEMO_CONTRACT: Dict[str, Any] = {
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


def _demo_records() -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for i in range(1, 901):
        records.append({
            "settlement_id": f"SETTLE-{1000 + i}",
            "amount": round(10.0 + (i * 1.5), 2),
            "currency": "USD" if i % 2 == 0 else "EUR",
        })
    for i in range(1, 21):
        records.append({"settlement_id": f"SETTLE-BAD-AMT-{i}", "amount": "NOT_A_FLOAT", "currency": "USD"})
    for i in range(21, 41):
        records.append({"settlement_id": None, "amount": 250.0, "currency": "GBP"})
    for i in range(1, 36):
        records.append({"settlement_id": f"SETTLE-NEG-{i}", "amount": -50.0, "currency": "USD"})
    for i in range(36, 61):
        records.append({"settlement_id": f"SETTLE-CURR-{i}", "amount": 120.0, "currency": "JPY"})
    return records


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
    records = _demo_records()

    if not args.json:
        print("=" * 68)
        print("      DATA SHELL — BOUNDARY ENFORCEMENT QUICKSTART DEMO")
        print("         'Bad data never lands, and never leaves.'")
        print("=" * 68)
        print(f"\n[1] Contract '{contract.metadata.name}' v{contract.metadata.version}")
        print(f"    Row rules:   {[r.rule_id for r in contract.spec.quality.rules]}")
        print(f"    Batch rules: {[b.rule_id for b in contract.spec.quality.batch_rules]}")
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
    tamper_status: Optional[str] = None
    restored_status: Optional[str] = None
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
            "run_id": result.run_id,
            "status": result.status.value,
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


def _demo_contract_payload(workspace: Path, target: Path, quarantine: Path) -> Dict[str, Any]:
    payload = json.loads(json.dumps(_DEMO_CONTRACT))
    payload["spec"]["sources"][0]["path"] = str(workspace / "incoming.jsonl")
    payload["spec"]["delivery"] = {
        "targets": [{"type": "PARQUET", "connectionRef": "local", "path": str(target), "mode": "overwrite"}],
        "quarantinePath": str(quarantine),
    }
    return payload


def result_verify_status(audit_path: Optional[str]) -> str:
    if not audit_path or not Path(audit_path).is_file():
        return "NO_CERTIFICATE"
    with open(audit_path, "r", encoding="utf-8") as f:
        cert = json.load(f)
    return ArtifactGenerator.verify_certificate_and_artifacts(cert).get("status", "UNKNOWN")


# --------------------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-contract":
            with open(args.contract, "r", encoding="utf-8") as f:
                data = json.load(f)
            contract = DataContract(**data)
            print(f"✓ Contract '{contract.metadata.name}' v{contract.metadata.version} is valid.")
            print(f"  Rules: {len(contract.spec.quality.rules)} row checks, {len(contract.spec.quality.batch_rules)} batch checks.")
            sys.exit(0)

        if args.command == "run":
            sys.exit(command_run(args))

        if args.command == "verify":
            sys.exit(command_verify(args))

        if args.command == "ledger":
            sys.exit(command_ledger(args))

        if args.command == "demo":
            sys.exit(command_demo(args))

        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\nFATAL: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
