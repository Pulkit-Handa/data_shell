"""Release identity and metadata consistency.

The canonical identity lives in ``pyproject.toml``. Everything that restates
it — ``datashell.__version__``, the FastAPI version served by the demo, the
newest CHANGELOG entry, the hash-pinned lockfiles, and the release tag — must
agree. The shared checks live in ``scripts/check_release_metadata.py`` so that
CI, the release workflow and this suite run the exact same logic.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_release_metadata as metadata  # noqa: E402


def test_release_metadata_is_consistent():
    """Version copies, changelog, locks and dependency claims all agree."""
    assert metadata.collect_problems(REPO_ROOT) == []


def test_release_version_matches_installed_package_metadata():
    """The imported package reports the same version as the distribution."""
    from importlib.metadata import version as dist_version

    import datashell

    assert datashell.__version__ == dist_version("datashell")


def test_tag_check_accepts_the_matching_tag():
    from datashell import __version__

    assert metadata.collect_problems(REPO_ROOT, tag=f"v{__version__}") == []


def test_tag_check_rejects_a_mismatched_tag():
    problems = metadata.collect_problems(REPO_ROOT, tag="v0.0.1")
    assert any("does not match package version" in problem for problem in problems)


@pytest.mark.parametrize("lock", ["requirements.lock", "requirements-dev.lock"])
def test_lockfiles_pin_every_declared_dependency_with_hashes(lock):
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    pins = metadata._locked_packages(REPO_ROOT / lock)
    hashed = metadata._pinned_with_hashes((REPO_ROOT / lock).read_text(encoding="utf-8"))
    for dependency in project["dependencies"]:
        name = metadata._normalize(str(dependency).split(";")[0].split(">=")[0].split("<")[0].split("==")[0].strip())
        assert name in pins, f"{lock} is missing a pin for {name}"
        assert name in hashed, f"{lock} pins {name} without a sha256 hash"


def test_changelog_does_not_claim_a_live_dependency_was_removed():
    """Guards the corrected `cryptography` note: claims must match reality."""
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert metadata._REMOVED_DEP_RE.findall(changelog) == []


def test_otel_lockfile_pins_the_tracing_extra_with_hashes():
    """The end-to-end collector job installs this lockfile: it must be pinned.

    The tracing SDK is an optional extra, so nothing else in the suite would
    notice an unpinned or unhashed tracing dependency.
    """
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = REPO_ROOT / "requirements-otel.lock"
    assert lock.is_file(), "requirements-otel.lock is missing"
    pins = metadata._locked_packages(lock)
    hashed = metadata._pinned_with_hashes(lock.read_text(encoding="utf-8"))
    for dependency in project["optional-dependencies"]["otel"]:
        name = metadata._normalize(str(dependency).split(">=")[0].split("<")[0].strip())
        assert name in pins, f"requirements-otel.lock is missing a pin for {name}"
        assert name in hashed, f"requirements-otel.lock pins {name} without a sha256 hash"


def test_otel_lockfile_respects_the_declared_extra_bounds():
    """A tracing pin outside the extra's declared bounds is a release defect."""
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    pins = metadata._locked_packages(REPO_ROOT / "requirements-otel.lock")
    for dependency in project["optional-dependencies"]["otel"]:
        parsed = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*(.*)$", dependency)
        assert parsed is not None
        name, specifier = parsed.group(1), parsed.group(2).strip()
        pinned = pins[metadata._normalize(name)]
        assert metadata._satisfies(pinned, specifier), (
            f"requirements-otel.lock pins {name}=={pinned}, outside {dependency!r}")
