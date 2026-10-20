"""Section-8 SDK drift guards.

The committed clients are artifacts of docs/openapi.yaml. These tests make the
regeneration relationship explicit and rereadable: the generator version is
pinned identically in the script and the docs, both clients exist, and the
Python client's function names cover the committed API surface, so a silent
regeneration against the wrong schema fails immediately.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
GENERATOR_VERSION = "v7.12.0"
SDK_SCRIPT = REPO_ROOT / "scripts" / "generate_sdks.sh"
SDK_DOCS = REPO_ROOT / "docs" / "SDK.md"
PYTHON_CLIENT = REPO_ROOT / "sdks" / "python-client" / "datashell_client"
TS_CLIENT = REPO_ROOT / "sdks" / "typescript-client" / "src"

# Path -> the python client's generated method stem for that operation.
EXPECTED_PY_METHODS = {
    "enforce": "enforce_boundary_api_v1_enforce_post",
    "register-contract": "register_contract_api_v1_contracts_post",
    "list-contracts": "list_contracts_api_v1_contracts_get",
    "ledger-runs": "list_ledger_runs_api_v1_runs_get",
    "run-certificate": "get_run_certificate_api_v1_runs_run_id_certificate_get",
    "settlement-preview": "settlement_preview_api_v1_settlement_preview_get",
    "settlements-calculate": "calculate_settlement_api_v1_settlements_calculate_post",
}


def test_generator_pin_is_consistent_across_script_and_docs() -> None:
    for artifact in (SDK_SCRIPT, SDK_DOCS):
        text = artifact.read_text(encoding="utf-8")
        assert GENERATOR_VERSION in text, artifact
    script = SDK_SCRIPT.read_text(encoding="utf-8")
    # The image tag must be assembled from the single declared version pin.
    assert script.count('version="v7.12.0"') == 1
    assert 'openapi-generator-cli:${version}"' in script


def test_both_sdks_are_committed_with_the_real_package_surface() -> None:
    assert (PYTHON_CLIENT / "api_client.py").is_file()
    assert (PYTHON_CLIENT / "py.typed").is_file()
    assert (TS_CLIENT / "apis" / "DefaultApi.ts").is_file()
    readme = (REPO_ROOT / "sdks" / "python-client" / "README.md").read_text(encoding="utf-8")
    assert "datashell" in readme.lower()


def test_python_client_covers_the_committed_api_operations() -> None:
    default_api = (PYTHON_CLIENT / "api" / "default_api.py").read_text(encoding="utf-8")
    missing = [f"{stem} (for {path})" for path, stem in EXPECTED_PY_METHODS.items()
               if f"def {stem}(" not in default_api]
    assert not missing, missing
