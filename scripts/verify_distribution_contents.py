#!/usr/bin/env python3
"""Inspect built distributions against an explicit allowlist.

Checks the wheel and the sdist that ``python -m build --no-isolation`` produces:

* **wheel** — only ``datashell/`` and its own ``.dist-info/`` may be present,
  the packaged license/entry point/``py.typed`` must be present;
* **sdist** — must independently carry the code, license/metadata, the
  reproducible lockfiles, the demo application (``web/``: engine + templates +
  static assets) and the test fixtures (``tests/``, ``examples/``);
* **both** — must never contain VCS data, bytecode/caches, local databases,
  delivered/quarantine/audit state, credentials, temp upload staging, or
  non-fixture Parquet output.

The expected distribution name is read from ``pyproject.toml`` rather than
hardcoded, so a distribution rename cannot silently desynchronise this gate
from the metadata it is supposed to police (the wheel's ``.dist-info`` and
the sdist's ``.egg-info`` directory are both named after the distribution).

Runs in CI (``ci.yml``, ``release.yml``) and locally:

    python scripts/verify_distribution_contents.py --dist-dir dist

Exit code 0 = every rule satisfied, 1 = violations (printed), 2 = no artifacts.
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The import package: it is the same in the wheel and in the sdist, and it is
# deliberately independent of the distribution name (`datashell-engine`).
IMPORT_PACKAGE = "datashell"

# Substrings that may never appear in a shipped artifact. Matched case
# insensitively against every member path.
FORBIDDEN = (
    ".git/",                 # repository metadata
    ".github/",              # CI configuration
    "__pycache__/",          # bytecode caches
    ".pytest_cache/",        # test/tool caches
    ".mypy_cache/",
    ".ruff_cache/",
    ".coverage",
    "contracts_registry/",   # runtime state
    "output/",               # delivered data, audit certificates, DBs
    "/data/",
    ".env",                  # environment / credential material
    ".pem",
    "id_rsa",
    "credentials.json",
    ".db",                   # local databases
    ".sqlite",
    "datashell_web_",        # request staging directories
    "staged_",               # staged delivery artifacts
    "public-demo-",          # demo tenant delivery namespaces
    "node_modules/",
)

# Build/VCS artifacts that are legitimate *only* at a specific location.
EGG_INFO_ALLOWED_AT_ROOT = True

# Fixture Parquet files are permitted (and only there): a future fixture lives
# under examples/fixtures/. Everything else is runtime output.
PARQUET_FIXTURE_PREFIXES = ("examples/fixtures/",)


def _project_identity(root: Path = REPO_ROOT) -> tuple[str, str, str]:
    """Return (distribution name, its normalized form, the version).

    The normalized form is the PEP 503 name with hyphens replaced by
    underscores — the form setuptools uses for the wheel's ``.dist-info``
    directory and the sdist's ``.egg-info`` directory.
    """
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    name = str(project["name"])
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    return name, normalized.replace("-", "_"), str(project["version"])


def _violations(names: list[str], *, allow_egg_info_root: bool, egg_info_prefix: str) -> list[str]:
    bad: list[str] = []
    for name in names:
        lowered = name.lower()
        for needle in FORBIDDEN:
            if needle in lowered:
                bad.append(f"{name}  (matches forbidden '{needle}')")
                break
        else:
            if re.search(r"\.py[co]$", lowered):
                bad.append(f"{name}  (bytecode file)")
            elif ".egg-info" in lowered:
                # Generated metadata is normal directly under an sdist root,
                # and never legitimate inside a wheel.
                top = name.split("/", 1)[0]
                if not (allow_egg_info_root and top.startswith(f"{egg_info_prefix}.egg-info")):
                    bad.append(f"{name}  (unexpected egg-info location)")
            elif lowered.endswith(".parquet") and not name.startswith(PARQUET_FIXTURE_PREFIXES):
                bad.append(f"{name}  (non-fixture Parquet)")
    return bad


def check_wheel(path: Path, dist_prefix: str, version: str) -> tuple[list[str], list[str]]:
    """Return (violations, missing required entries) for a wheel."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    problems = _violations(names, allow_egg_info_root=False, egg_info_prefix=dist_prefix)

    # Strict allowlist: nothing outside the package and its own metadata.
    allowed = re.compile(rf"^({IMPORT_PACKAGE}/|{re.escape(dist_prefix)}-[0-9][^/]*\.dist-info/)")
    for name in names:
        if not allowed.match(name):
            problems.append(f"{name}  (outside the {IMPORT_PACKAGE} package allowlist)")

    required = [
        f"{IMPORT_PACKAGE}/py.typed",
        f"{IMPORT_PACKAGE}/cli/main.py",
        f"{IMPORT_PACKAGE}/server/app.py",
    ]
    missing = [r for r in required if r not in names]
    dist_info = next((n.split("/")[0] for n in names if n.endswith(".dist-info/METADATA")), None)
    if dist_info:
        # The metadata directory is named after the distribution: a rename that
        # is not reflected here means the artifact and pyproject disagree.
        expected_dist_info = f"{dist_prefix}-{version}.dist-info"
        if dist_info != expected_dist_info:
            problems.append(f"{dist_info}  (expected the metadata directory {expected_dist_info})")
        for extra in ("licenses/LICENSE", "entry_points.txt", "WHEEL", "RECORD"):
            if f"{dist_info}/{extra}" not in names:
                missing.append(f"{dist_info}/{extra}")
    else:
        missing.append("*.dist-info/METADATA")
    return problems, missing


def check_sdist(path: Path, dist_prefix: str, version: str) -> tuple[list[str], list[str]]:
    """Return (violations, missing required entries) for an sdist."""
    with tarfile.open(path, "r:gz") as tf:
        names = [m.name for m in tf.getmembers() if m.isfile()]
    problems: list[str] = []
    # The sdist root directory is named after the distribution: a rename that
    # is not reflected here means the artifact and pyproject disagree.
    expected_root = f"{dist_prefix}-{version}"
    roots = {n.split("/", 1)[0] for n in names if "/" in n}
    if roots != {expected_root}:
        problems.append(f"{sorted(roots)}  (expected the sdist root {expected_root})")
    # Normalize away the "<name>-<version>/" root so allow/deny rules are stable.
    stripped = [n.split("/", 1)[1] for n in names if "/" in n]
    problems += _violations(stripped, allow_egg_info_root=EGG_INFO_ALLOWED_AT_ROOT, egg_info_prefix=dist_prefix)

    required = [
        # identity, license and legal files
        "pyproject.toml",
        "MANIFEST.in",
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        # reproducible, hash-pinned dependency sets
        "requirements.lock",
        "requirements-dev.lock",
        "requirements-build.lock",
        "requirements-otel.lock",
        # shipped code + typing marker
        "datashell/__init__.py",
        "datashell/py.typed",
        "datashell/pipeline/runner.py",
        # demo application source: engine, templates and static assets
        "web/main.py",
        "web/templates/index.html",
        "web/static/css/style.css",
        # tests and fixtures (the sdist must be independently testable)
        "tests/test_end_to_end_pipeline.py",
        "tests/test_release_metadata.py",
        "examples/nyc_taxi_contract.json",
        "examples/vendor_settlements_contract.json",
        "examples/samples/vendor_settlements_1k.jsonl",
        # release-engineering scripts invoked by the suite and by CI
        "scripts/check_release_metadata.py",
        "scripts/verify_distribution_contents.py",
        "scripts/verify_provider_distribution_contents.py",
        # the end-to-end OpenTelemetry collector check (script + its config)
        "scripts/run_otel_collector_e2e.sh",
        "scripts/otel-collector-ci.yaml",
        "tests/test_otel_collector_e2e.py",
    ]
    missing = [r for r in required if r not in stripped]
    if not any(n.startswith("tests/") and n.endswith(".py") for n in stripped):
        missing.append("tests/*.py")
    if not any(n.startswith("web/js") or n.startswith("web/static/js/") for n in stripped):
        missing.append("web/static/js/*")
    return problems, missing


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", default="dist", help="directory holding the built artifacts")
    parser.add_argument("--wheel", help="explicit wheel path (overrides --dist-dir glob)")
    parser.add_argument("--sdist", help="explicit sdist path (overrides --dist-dir glob)")
    args = parser.parse_args(argv)

    dist_dir = Path(args.dist_dir)
    wheels = [Path(args.wheel)] if args.wheel else sorted(dist_dir.glob("*.whl"))
    sdists = [Path(args.sdist)] if args.sdist else sorted(dist_dir.glob("*.tar.gz"))

    if not wheels or not sdists:
        print(f"ERROR: expected a wheel and an sdist in {dist_dir} "
              f"(found {len(wheels)} wheel(s), {len(sdists)} sdist(s))", file=sys.stderr)
        return 2

    distribution, dist_prefix, version = _project_identity()
    print(f"== expected identity: {distribution} {version} "
          f"(metadata directory {dist_prefix}-{version}.dist-info)")
    failed = False
    for wheel in wheels:
        problems, missing = check_wheel(wheel, dist_prefix, version)
        print(f"== wheel: {wheel.name}")
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
        print(f"   {len(names)} entries, {len({n.split('/')[0] for n in names})} top-level paths")
        if missing:
            failed = True
            for item in missing:
                print(f"   MISSING: {item}")
        if problems:
            failed = True
            for item in problems:
                print(f"   VIOLATION: {item}")
        if not (problems or missing):
            print("   OK: allowlisted content only, required files present")

    for sdist in sdists:
        problems, missing = check_sdist(sdist, dist_prefix, version)
        print(f"== sdist: {sdist.name}")
        with tarfile.open(sdist, "r:gz") as tf:
            count = sum(1 for m in tf.getmembers() if m.isfile())
        print(f"   {count} files")
        if missing:
            failed = True
            for item in missing:
                print(f"   MISSING: {item}")
        if problems:
            failed = True
            for item in problems:
                print(f"   VIOLATION: {item}")
        if not (problems or missing):
            print("   OK: allowlisted content only, required files present")

    if failed:
        print("\nDistribution contents check FAILED")
        return 1
    print("\nDistribution contents check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
