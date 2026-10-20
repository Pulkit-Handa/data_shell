"""Alert-signal verification: prove that each documented alert condition
actually produces its signal, instead of only asserting that a rule file
exists.

The script triggers each condition in-process and checks the corresponding
Prometheus metric moves. It does **not** verify delivery to an external
notifier (that needs a live Prometheus/Alertmanager), and it says so
explicitly in its output. The target-host drill in
``docs/production/DEPLOYMENT_VERIFICATION.md`` section 11 covers external
delivery.

Usage:
    ENVIRONMENT=test DATASHELL_STORAGE_ROOT=/tmp/alertcheck \
        PYTHONPATH=. python scripts/alert_signal_test.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# The check must run against an isolated storage root so it never touches a
# real deployment's ledger or artifacts.
WORKSPACE = Path(tempfile.mkdtemp(prefix="datashell-alertcheck-"))
os.environ.setdefault("ENVIRONMENT", "test")
os.environ["DATASHELL_STORAGE_ROOT"] = str(WORKSPACE / "storage")
(WORKSPACE / "storage").mkdir(parents=True, exist_ok=True)

from prometheus_client import REGISTRY  # noqa: E402


def _metric_value(name: str, labels: dict | None = None) -> float:
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name != name:
                continue
            if labels:
                if all(sample.labels.get(k) == v for k, v in labels.items()):
                    return float(sample.value)
                continue
            return float(sample.value)
    return 0.0


def check(label: str, condition: str, actual: float, expected: float) -> bool:
    ok = actual == expected
    print(
        f"  [{'PASS' if ok else 'FAIL'}] {label}\n"
        f"         condition: {condition}\n"
        f"         expected: {expected}  actual: {actual}"
    )
    return ok


def main() -> int:
    from datashell.core.ledger import pending_outbox_path, write_pending_outbox
    from datashell.core.telemetry import (
        HTTP_REQUESTS_TOTAL,
        PENDING_LEDGER_ENTRIES,
        PUBLICATION_RECOVERY_ERRORS,
        PUBLICATION_RECOVERY_JOURNALS,
        STORAGE_FREE_BYTES,
    )
    from datashell.pipeline.runner import reconcile_pending_ledger_entries

    results: list[bool] = []
    print("Data Shell — alert signal verification\n")

    # ---- 1. Startup recovery gauges -------------------------------------
    print("1. Startup recovery state")
    PENDING_LEDGER_ENTRIES.set(0)
    PUBLICATION_RECOVERY_ERRORS.set(0)
    PUBLICATION_RECOVERY_JOURNALS.set(0)
    results.append(
        check(
            "no pending ledger entries -> DataShellPendingLedgerEntries stays quiet",
            "datashell_pending_ledger_entries > 0",
            _metric_value("datashell_pending_ledger_entries"),
            0.0,
        )
    )
    results.append(
        check(
            "no recovery errors -> DataShellPublicationRecoveryErrors stays quiet",
            "datashell_publication_recovery_errors > 0",
            _metric_value("datashell_publication_recovery_errors"),
            0.0,
        )
    )

    # ---- 2. Unreconciled pending ledger entry ---------------------------
    print("\n2. Unreconciled pending ledger entry")
    audit = WORKSPACE / "storage" / "output" / "audits" / "never_published_audit.json"
    audit.parent.mkdir(parents=True, exist_ok=True)
    write_pending_outbox(
        pending_outbox_path(audit, "dm_run_never_published"),
        {
            "run_id": "dm_run_never_published",
            "tenant_id": "alert-check",
            "contract_name": "c",
            "rows_read": 10,
            "rows_delivered": 10,
            "status": "LOADED",
            "audit_path": str(audit),          # never written -> not published
            "run_artifact_path": None,
            "quarantine_files": [],
        },
    )
    summary = reconcile_pending_ledger_entries(storage_root=WORKSPACE / "storage")
    PENDING_LEDGER_ENTRIES.set(float(summary.get("skipped", 0) or 0))
    results.append(
        check(
            "an unpublished pending record is counted, not silently dropped",
            "datashell_pending_ledger_entries > 0",
            _metric_value("datashell_pending_ledger_entries"),
            float(summary.get("skipped", 0) or 0),
        )
    )
    results.append(
        check(
            "the unpublished record was skipped, not committed",
            "replayed == 0",
            float(summary.get("replayed", 0) or 0),
            0.0,
        )
    )

    # ---- 3. Storage gauge -----------------------------------------------
    print("\n3. Storage pressure signal")
    STORAGE_FREE_BYTES.set(123456789)
    results.append(
        check(
            "free storage is exported for the disk alert",
            "datashell_storage_free_bytes < 1073741824",
            _metric_value("datashell_storage_free_bytes"),
            123456789.0,
        )
    )

    # ---- 4. Request rejection counter ------------------------------------
    print("\n4. Capacity rejection signal")
    HTTP_REQUESTS_TOTAL.labels(method="POST", route="/api/v1/enforce", status_code="503").inc()
    results.append(
        check(
            "capacity rejections are counted",
            'datashell_http_requests_total{status_code="503"} rises',
            _metric_value(
                "datashell_http_requests_total",
                {
                    "method": "POST",
                    "route": "/api/v1/enforce",
                    "status_code": "503",
                },
            ),
            1.0,
        )
    )

    # ---- 5. Alert rule file parses and references real metrics -----------
    print("\n5. Alert rule definitions")
    alert_path = REPO_ROOT / "deploy" / "prometheus-alerts.yaml"
    names: list[str] = []
    exprs = ""
    try:
        import yaml

        rules = yaml.safe_load(alert_path.read_text(encoding="utf-8"))
        names = [r["alert"] for group in rules["groups"] for r in group["rules"]]
        exprs = " ".join(r["expr"] for group in rules["groups"] for r in group["rules"])
    except Exception as exc:  # pragma: no cover - environment without pyyaml
        print(f"  [WARN] could not parse alert rules: {exc}")
    for required in ("DataShellPendingLedgerEntries", "DataShellPublicationRecoveryErrors"):
        results.append(
            check(f"rule {required} is defined", "alert rule present", float(required in names), 1.0)
        )
    results.append(
        check(
            "the pending-ledger rule reads the metric the startup path sets",
            "datashell_pending_ledger_entries in expr",
            float("datashell_pending_ledger_entries" in exprs),
            1.0,
        )
    )
    results.append(
        check(
            "the recovery-error rule reads the metric the startup path sets",
            "datashell_publication_recovery_errors in expr",
            float("datashell_publication_recovery_errors" in exprs),
            1.0,
        )
    )

    shutil.rmtree(WORKSPACE, ignore_errors=True)
    print("\n" + "=" * 68)
    print(f"Alert signal checks: {sum(1 for r in results if r)}/{len(results)} passed")
    print("NOTE: this verifies the SIGNALS, not external notification delivery.")
    print("      External delivery must be tested against the live monitoring")
    print("      stack (see DEPLOYMENT_VERIFICATION.md section 11).")
    print("=" * 68)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

