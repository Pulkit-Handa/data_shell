"""``datashell doctor`` must report the capabilities this environment has.

The doctor command is the operator-facing diagnosis for "is this capability
actually available here?". A probe that looks in the wrong place turns that
answer into a lie: the tracing extra was installed and the doctor still
reported it missing, because the probe imported the distribution name instead
of the module the gateway loads.

These tests pin the two properties that make the report trustworthy:

* every probe points at an importable module path, so an installed extra is
  never reported as missing;
* the tracing probe points at the very module the tracing bridge imports, so
  the report cannot drift away from the runtime behaviour.
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from datashell.cli.main import EXTRA_MODULE_PROBES, command_doctor

REPO_ROOT = Path(__file__).resolve().parents[1]


def _distribution_installed(distribution: str) -> bool:
    try:
        importlib.metadata.distribution(distribution)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def _doctor_report() -> dict[str, Any]:
    """Run the doctor command and return its machine-readable report."""
    import argparse

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        command_doctor(argparse.Namespace(json=True))
    return json.loads(buffer.getvalue())


def _tracing_bridge_exporter_module() -> str:
    """The exporter module the tracing bridge imports at runtime."""
    source = (REPO_ROOT / "datashell" / "core" / "observability.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("trace_exporter"):
            return node.module.rsplit(".", 1)[0]
    raise AssertionError("the tracing bridge no longer imports an OTLP trace exporter")


@pytest.mark.parametrize(
    ("distribution", "module_path"),
    [
        (distribution, module_path)
        for probes in EXTRA_MODULE_PROBES.values()
        for distribution, module_path in probes
    ],
)
def test_an_installed_extra_is_never_reported_as_missing(
    distribution: str, module_path: str
) -> None:
    """If the distribution is installed, its import path must be importable.

    This is the property the tracing probe violated: the extra was installed
    and the doctor still said "missing".
    """
    if not _distribution_installed(distribution):
        pytest.skip(f"{distribution} is not installed in this environment")
    assert importlib.util.find_spec(module_path) is not None, (
        f"the doctor probes {module_path!r} for {distribution!r}, but that module "
        f"is not importable although the distribution is installed"
    )


def test_the_tracing_probe_points_at_the_module_the_bridge_loads() -> None:
    """Doctor must probe the exporter module the gateway actually imports."""
    probes = dict(EXTRA_MODULE_PROBES["otel"])
    assert "opentelemetry.exporter.otlp.proto.grpc" in probes.values()
    assert _tracing_bridge_exporter_module() in probes.values()


def test_the_report_matches_what_is_importable() -> None:
    """Every reported capability must agree with the interpreter's own answer."""
    report = _doctor_report()
    for extra, probes in EXTRA_MODULE_PROBES.items():
        entry = report["extras"][extra]
        for _distribution, module_path in probes:
            expected = importlib.util.find_spec(module_path) is not None
            reported = next(
                value
                for label, value in entry["modules"].items()
                if label == _distribution
            )
            assert reported["available"] is expected, (
                f"doctor reports {extra}/{_distribution} as "
                f"{'available' if reported['available'] else 'missing'}, but "
                f"{module_path!r} is {'importable' if expected else 'not importable'}"
            )


def test_the_tracing_extra_is_reported_when_it_is_installed() -> None:
    """With the extra installed, the doctor must say the tracing half is there."""
    if not _distribution_installed("opentelemetry-sdk"):
        pytest.skip("the otel extra is not installed in this environment")
    report = _doctor_report()
    entry = report["extras"]["otel"]
    assert entry["available"] is True, entry
    for label, value in entry["modules"].items():
        assert value["available"] is True, f"{label}: {value}"
