"""Automated security penetration and defensibility test suite."""


import polars as pl
import pytest
from fastapi.testclient import TestClient

from datashell.contract.models import BatchRule, BatchRuleCheckType, DataContract
from datashell.contract.registry import ContractRegistry
from datashell.engines.quality import SemanticQualityEngine
from datashell.server.app import app

client = TestClient(app)


def test_path_traversal_prevention_in_registry(tmp_path):
    registry = ContractRegistry(tmp_path)
    
    with pytest.raises(ValueError) as exc:
        registry.get_latest("../../../etc/passwd")
    assert "path traversal" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        registry.get_version("valid_name", "../../bad_version")
    assert "path traversal" in str(exc.value)


def test_api_rejects_path_traversal_and_malicious_contract_names():
    resp = client.get("/api/v1/contracts/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)


def test_api_rejects_unsupported_dangerous_file_extensions():
    from datashell.core.config import settings
    fake_payload = b"print('malicious')"
    files = {"file": ("exploit.sh", fake_payload, "application/x-sh")}
    data = {"contract_name": "vendor-settlements"}
    headers = {"X-API-Key": settings.master_api_key}
    
    resp = client.post("/api/v1/enforce", data=data, files=files, headers=headers)
    assert resp.status_code == 400
    assert "Unsupported file format" in resp.json()["detail"]


def test_duckdb_sandbox_prevents_arbitrary_file_read():
    """Verify that DuckDB external access is strictly disabled."""
    malicious_contract = DataContract(
        metadata={"name": "sandbox-test"},
        spec={
            "sources": [{"path": "dummy", "schema": [{"name": "id", "polars_type": "Utf8"}]}],
            "quality": {
                "behavior": "REJECT_RUN",
                "rules": [],
                "batch_rules": [
                    BatchRule(
                        rule_id="try-read-file",
                        check=BatchRuleCheckType.SQL_ASSERTION,
                        # Attempt to read filesystem from within DuckDB
                        sql="EXISTS (SELECT * FROM read_csv('/etc/passwd'))"
                    )
                ]
            }
        }
    )

    df = pl.DataFrame({"id": ["test1", "test2"]})
    engine = SemanticQualityEngine(malicious_contract)

    # Executing the sandboxed DuckDB query should raise permission/access error
    with pytest.raises(Exception) as exc:
        engine.evaluate(df)
    err_msg = str(exc.value).lower()
    assert "disabled" in err_msg or "external" in err_msg or "permission" in err_msg or "read_csv" in err_msg
