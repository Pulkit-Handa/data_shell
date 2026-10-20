#!/usr/bin/env python3
"""Verify the canonical release identity and its derivations agree.

Single source of truth for the release identity is ``pyproject.toml``; every
other copy of the version and every dependency claim must match it:

* ``datashell.__version__`` (import package),
* the FastAPI app version served by the public demo (``web/main.py``),
* the newest ``## [x.y.z]`` entry in ``CHANGELOG.md``,
* the release tag when one is supplied (``--tag v1.2.3``),
* every declared runtime dependency pinned **with hashes** in the lockfiles,
* historical changelog claims ("removed the unused `x` runtime dependency")
  never contradict the current dependency list.

The distribution name (``datashell`` — matching the import package so
``pip install datashell`` works directly), and the product name
(``Data Shell``) are deliberately distinct and both preserved.

Exit code 0 = consistent, 1 = inconsistency (details printed).

    python scripts/check_release_metadata.py
    python scripts/check_release_metadata.py --tag v2.0.0
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_VERSION_INIT_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
_FASTAPI_VERSION_RE = re.compile(r'FastAPI\([^)]*?version="([^"]+)"', re.DOTALL)
_CHANGELOG_ENTRY_RE = re.compile(r"^##\s+\[([^\]]+)\]", re.MULTILINE)
_REMOVED_DEP_RE = re.compile(r"Removed the unused `([A-Za-z0-9_.\-]+)` runtime dependency")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked_packages(lock_path: Path) -> dict[str, str]:
    """Map normalized package name -> pin line from a pip-compile lockfile."""
    pins: dict[str, str] = {}
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", line)
        if match:
            pins[_normalize(match.group(1))] = match.group(2)
    return pins


def _pinned_with_hashes(lock_text: str) -> set[str]:
    """Names in a lockfile that carry at least one --hash=sha256: entry."""
    hashed: set[str] = set()
    current: str | None = None
    for line in lock_text.splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)==", line)
        if match:
            current = _normalize(match.group(1))
        elif current and re.match(r"^\s+--hash=sha256:", line):
            hashed.add(current)
    return hashed


def collect_problems(root: Path = REPO_ROOT, tag: str | None = None) -> list[str]:
    problems: list[str] = []

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    version = project["version"]

    # --- canonical identity: distribution / import package / product name ---
    if project["name"] != "datashell":
        problems.append(f"pyproject project.name is {project['name']!r}, expected 'datashell'")
    scripts = project.get("scripts", {})
    if scripts.get("datashell") != "datashell.cli.main:main":
        problems.append(
            f"console script 'datashell' is {scripts.get('datashell')!r}, "
            "expected 'datashell.cli.main:main'")

    # --- version derivations must all match pyproject --------------------
    # In a source tree the import package version is read from the source; in an
    # installed-only deployment (wheel + `web/`, no `datashell/` source) it is
    # read from the installed distribution metadata.
    source_init = root / "datashell" / "__init__.py"
    if source_init.is_file():
        match = _VERSION_INIT_RE.search(source_init.read_text(encoding="utf-8"))
        reported, origin = (match.group(1) if match else None), "datashell/__init__.py"
    else:
        try:
            from importlib.metadata import version as _dist_version

            reported, origin = _dist_version(project["name"]), f"installed distribution {project['name']!r}"
        except Exception:  # pragma: no cover - defensive
            reported, origin = None, f"installed distribution {project['name']!r}"
    if reported != version:
        problems.append(f"import package version from {origin} is {reported!r}, expected {version!r}")

    web_text = (root / "web" / "main.py").read_text(encoding="utf-8")
    web_version = _FASTAPI_VERSION_RE.search(web_text)
    if not web_version or web_version.group(1) != version:
        problems.append(
            f"web/main.py FastAPI version is {web_version.group(1) or 'missing'!r}, "
            f"expected {version!r}")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    entries = _CHANGELOG_ENTRY_RE.findall(changelog)
    released = [e for e in entries if e.lower() != "unreleased"]
    if not released:
        problems.append("CHANGELOG.md has no '## [x.y.z]' release entry")
    elif released[0] != version:
        problems.append(f"newest CHANGELOG entry is {released[0]!r}, expected {version!r}")
    if f"[{version}]" not in changelog:
        problems.append(f"CHANGELOG.md has no entry for version {version!r}")

    # --- tag / package version consistency -------------------------------
    if tag is not None:
        expected = f"v{version}"
        if tag != expected:
            problems.append(f"release tag {tag!r} does not match package version (expected {expected!r})")

    # --- declared runtime dependencies are pinned with hashes ------------
    declared = [_normalize(re.split(r"[<>=!~;\s]", dep, maxsplit=1)[0]) for dep in project.get("dependencies", [])]
    for lock_name in ("requirements.lock", "requirements-dev.lock"):
        lock_path = root / lock_name
        if not lock_path.is_file():
            problems.append(f"{lock_name} is missing")
            continue
        lock_text = lock_path.read_text(encoding="utf-8")
        pins = _locked_packages(lock_path)
        hashed = _pinned_with_hashes(lock_text)
        for dep in declared:
            if dep not in pins:
                problems.append(f"{lock_name} does not pin declared dependency {dep!r}")
            elif dep not in hashed:
                problems.append(f"{lock_name} pins {dep!r} without a sha256 hash")

    # --- changelog dependency claims must not contradict pyproject -------
    for claimed_removed in _REMOVED_DEP_RE.findall(changelog):
        if _normalize(claimed_removed) in declared:
            problems.append(
                f"CHANGELOG claims {claimed_removed!r} was removed but pyproject declares it as a runtime dependency"
            )

    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT), help="repository root (defaults to the script's parent)")
    parser.add_argument("--tag", help="release tag to compare against the package version (e.g. v2.0.0)")
    args = parser.parse_args(argv)

    problems = collect_problems(Path(args.root), tag=args.tag)
    if problems:
        print("Release metadata check FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    version = tomllib.loads((Path(args.root) / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    suffix = f" (tag {args.tag})" if args.tag else ""
    print(f"Release metadata consistent: datashell {version}{suffix} "
          "— package, web app, changelog and lockfiles agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
