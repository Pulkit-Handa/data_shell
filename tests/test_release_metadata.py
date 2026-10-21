"""Release identity and metadata consistency.

The canonical identity lives in ``pyproject.toml``. Everything that restates
it — ``datashell.__version__``, the FastAPI version served by the demo, the
newest CHANGELOG entry, the hash-pinned lockfiles, and the release tag — must
agree. The shared checks live in ``scripts/check_release_metadata.py`` so that
CI, the release workflow and this suite run the exact same logic.

The three names are deliberately distinct and all pinned by this suite:

* the PyPI **distribution** name (``datashell-engine``) — ``datashell`` on
  PyPI belongs to an unrelated project, so the distribution cannot use it;
* the **import package** name (``datashell``) — unchanged by the rename;
* the **console script** name (``datashell``) — unchanged by the rename.
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

    assert datashell.__version__ == dist_version("datashell-engine")


def test_distribution_name_is_publishable_and_distinct_from_the_import_package():
    """The distribution, the import package and the console script are pinned.

    The distribution was renamed because ``datashell`` on PyPI belongs to an
    unrelated project. The import package and the console script must keep
    their names: renaming those would break every operator's `import
    datashell` and `datashell <command>`.
    """
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["name"] == "datashell-engine"
    assert project["name"] != "datashell"
    assert project["scripts"]["datashell"] == "datashell.cli.main:main"

    # The import package itself is unchanged and still shipped by the wheel.
    import datashell

    assert datashell.__name__ == "datashell"
    find_include = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["tool"]["setuptools"]["packages"]["find"]["include"]
    assert any(pattern.startswith("datashell") for pattern in find_include)


def test_distribution_name_is_a_valid_pypi_project_name():
    """A name PyPI would reject must fail the release gate, not the upload."""
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert re.fullmatch(r"[a-z0-9]([a-z0-9._-]*[a-z0-9])?", project["name"])
    assert project["name"] == project["name"].lower()


def test_tag_check_accepts_the_matching_tag():
    from datashell import __version__

    assert metadata.collect_problems(REPO_ROOT, tag=f"v{__version__}") == []


def test_tag_check_rejects_a_mismatched_tag():
    problems = metadata.collect_problems(REPO_ROOT, tag="v0.0.1")
    assert any("does not match package version" in problem for problem in problems)


def test_distribution_contents_gate_follows_the_distribution_name():
    """The wheel/sdist gate must read the name, not carry a copy of it.

    A hardcoded name in the gate is how a rename silently desynchronises the
    artifact from the metadata it is supposed to police.
    """
    import verify_distribution_contents as contents

    distribution, dist_prefix, version = contents._project_identity(REPO_ROOT)
    assert distribution == "datashell-engine"
    assert dist_prefix == "datashell_engine"
    assert f"{dist_prefix}-{version}.dist-info" == "datashell_engine-2.0.1.dist-info"
    # The import package is independent of the distribution name.
    assert contents.IMPORT_PACKAGE == "datashell"


def test_release_gate_rejects_a_distribution_name_that_equals_the_import_package():
    """The gate fails if the distribution is renamed back onto `datashell`."""
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] != metadata.EXPECTED_IMPORT_PACKAGE
    assert metadata.EXPECTED_DISTRIBUTION == "datashell-engine"


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
