#!/usr/bin/env python3
"""Inspect the built Airflow provider distributions against an allowlist.

The provider (`datashell-airflow`) is a second, independently published
distribution. It gets the same treatment as the core package: a wheel and an
sdist that are built from the pinned build backend and checked before they
are uploaded.

Checks:

* **wheel** — only ``datashell_airflow/`` and its own ``.dist-info/`` may be
  present; the packaged license, entry points and metadata must be present;
* **sdist** — must carry the provider package, its metadata, its license and
  its test suite (the sdist must be independently testable);
* **both** — must never contain bytecode/caches, VCS data or local state.

The expected distribution name is read from ``integrations/airflow/pyproject.toml``
rather than hardcoded, so a rename cannot silently desynchronise this gate
from the metadata it polices.

Runs in the publish workflow (``publish-pypi.yml``) and locally:

    python scripts/verify_provider_distribution_contents.py --dist-dir integrations/airflow/dist

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
PROVIDER_ROOT = REPO_ROOT / "integrations" / "airflow"

# The import package of the provider distribution.
IMPORT_PACKAGE = "datashell_airflow"

# Substrings that may never appear in a shipped artifact.
FORBIDDEN = (
    ".git/",
    ".github/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".coverage",
    ".env",
    ".pem",
    "id_rsa",
    ".db",
    ".sqlite",
    "node_modules/",
)


def _project_identity(root: Path = PROVIDER_ROOT) -> tuple[str, str, str]:
    """Return (distribution name, its normalized form, the version)."""
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
                top = name.split("/", 1)[0]
                if not (allow_egg_info_root and top.startswith(f"{egg_info_prefix}.egg-info")):
                    bad.append(f"{name}  (unexpected egg-info location)")
    return bad


def check_wheel(path: Path, dist_prefix: str, version: str) -> tuple[list[str], list[str]]:
    """Return (violations, missing required entries) for the provider wheel."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    problems = _violations(names, allow_egg_info_root=False, egg_info_prefix=dist_prefix)

    # Strict allowlist: nothing outside the package and its own metadata.
    allowed = re.compile(rf"^({IMPORT_PACKAGE}/|{re.escape(dist_prefix)}-[0-9][^/]*\.dist-info/)")
    for name in names:
        if not allowed.match(name):
            problems.append(f"{name}  (outside the {IMPORT_PACKAGE} package allowlist)")

    required = [
        f"{IMPORT_PACKAGE}/__init__.py",
        f"{IMPORT_PACKAGE}/client.py",
        f"{IMPORT_PACKAGE}/hooks.py",
        f"{IMPORT_PACKAGE}/operators.py",
    ]
    missing = [r for r in required if r not in names]
    dist_info = next((n.split("/")[0] for n in names if n.endswith(".dist-info/METADATA")), None)
    if dist_info:
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
    """Return (violations, missing required entries) for the provider sdist."""
    with tarfile.open(path, "r:gz") as tf:
        names = [m.name for m in tf.getmembers() if m.isfile()]
    problems: list[str] = []
    expected_root = f"{dist_prefix}-{version}"
    roots = {n.split("/", 1)[0] for n in names if "/" in n}
    if roots != {expected_root}:
        problems.append(f"{sorted(roots)}  (expected the sdist root {expected_root})")
    stripped = [n.split("/", 1)[1] for n in names if "/" in n]
    problems += _violations(stripped, allow_egg_info_root=True, egg_info_prefix=dist_prefix)

    required = [
        # identity, license and legal files
        "pyproject.toml",
        "MANIFEST.in",
        "README.md",
        "LICENSE",
        # shipped code
        f"{IMPORT_PACKAGE}/__init__.py",
        f"{IMPORT_PACKAGE}/client.py",
        # the sdist must be independently testable
        "tests/test_client.py",
        "tests/test_operators.py",
    ]
    missing = [r for r in required if r not in stripped]
    return problems, missing


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", default="integrations/airflow/dist",
                        help="directory holding the built provider artifacts")
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
        print("\nProvider distribution contents check FAILED")
        return 1
    print("\nProvider distribution contents check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
