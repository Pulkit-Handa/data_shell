"""Tests for Enterprise Subsystems: Cryptography, RBAC, Streaming Ingestion, and Historical Drift."""

from pathlib import Path

import polars as pl
from fastapi.testclient import TestClient

from datashell.contract.models import DataContract
from datashell.core.config import settings
from datashell.core.crypto import CryptoEngine
from datashell.core.security import TokenBucketRateLimiter
from datashell.engines.drift import ContractHistoryTracker, HistoricalDriftEngine
from datashell.engines.stream_ingestion import StreamingBoundaryRunner
from datashell.server.app import app

client = TestClient(app)


def test_cryptographic_engine_and_tamper_detection(tmp_path):
    # Create two test files
    f1 = tmp_path / "file1.dat"
    f1.write_bytes(b"hello world payload")
    h1 = CryptoEngine.sha256_file(f1)

    f2 = tmp_path / "file2.dat"
    f2.write_bytes(b"different payload")
    h2 = CryptoEngine.sha256_file(f2)

    assert h1 != h2

    # Compute Merkle Root
    merkle_root = CryptoEngine.compute_merkle_root([h1, h2])
    assert len(merkle_root) == 64

    # HMAC Signature and verification
    payload = {"run_id": "test_1", "merkle_root": merkle_root}
    sig = CryptoEngine.sign_manifest(payload, secret_key="test_secret")
    assert CryptoEngine.verify_signature(payload, sig, secret_key="test_secret") is True

    # Tampered payload fails verification
    tampered_payload = {"run_id": "test_1_tampered", "merkle_root": merkle_root}
    assert CryptoEngine.verify_signature(tampered_payload, sig, secret_key="test_secret") is False


def test_token_bucket_rate_limiter():
    limiter = TokenBucketRateLimiter(rate_per_minute=2)
    # Burst 2 tokens allowed
    assert limiter.allow("client_1") is True
    assert limiter.allow("client_1") is True
    # 3rd token exhausted
    assert limiter.allow("client_1") is False


def test_historical_drift_detection(tmp_path):
    db_path = tmp_path / "test_history.db"
    tracker = ContractHistoryTracker(db_path)
    engine = HistoricalDriftEngine(tracker)

    contract_name = "daily-settlement-feed"

    # Run 1: Establishes baseline of 10,000 rows
    r1 = engine.evaluate_drift(contract_name, current_rows=10_000, current_metrics={})
    assert len(r1) == 1
    assert r1[0].passed is True
    tracker.record_run("run_1", contract_name, 10_000, 10_000, {})

    # Run 2: Stable run of 9,500 rows (5% drop, allowed within 30%)
    r2 = engine.evaluate_drift(contract_name, current_rows=9_500, current_metrics={})
    assert r2[0].passed is True

    # Run 3: Catastrophic drop to 5,000 rows (50% drop, exceeds 30% threshold)
    r3 = engine.evaluate_drift(contract_name, current_rows=5_000, current_metrics={})
    assert r3[0].passed is False
    assert r3[0].pct_change == -50.0


def test_streaming_chunked_runner_footing(tmp_path):
    # Load demo contract
    import json
    contract_file = Path(__file__).parent.parent / "examples" / "nyc_taxi_contract.json"
    with open(contract_file) as fp:
        contract = DataContract(**json.load(fp))

    # Generate synthetic parquet file
    df = pl.DataFrame({
        "VendorID": [1, 2, 1, 2] * 250, # 1,000 rows
        "passenger_count": [1, 0, 2, 1] * 250, # 250 rows have 0 passengers (held)
        "trip_distance": [2.5, 3.0, 0.0, 1.2] * 250,
        "PULocationID": [100, 101, 102, 103] * 250,
        "DOLocationID": [200, 201, 202, 203] * 250,
        "payment_type": [1, 1, 1, 1] * 250,
        "fare_amount": [15.0, 20.0, 10.0, -5.0] * 250,
        "total_amount": [18.0, 24.0, 12.0, -3.0] * 250,
    })
    src_file = tmp_path / "stream_in.parquet"
    df.write_parquet(src_file)

    target_file = tmp_path / "stream_out.parquet"
    quarantine_dir = tmp_path / "stream_quarantine"

    runner = StreamingBoundaryRunner(contract, chunk_size=200)
    ledger, outcome = runner.run_streaming_parquet(src_file, target_file, quarantine_dir)

    assert ledger.rows_read == 1000
    assert ledger.unaccounted_for == 0
    assert ledger.footing_matched is True
    assert outcome.status.value == "LOADED"
    assert target_file.exists()


def test_prometheus_metrics_endpoint():
    resp = client.get("/metrics", headers={"X-API-Key": settings.master_api_key})
    assert resp.status_code == 200
    assert "datashell_runs_total" in resp.text
    assert "datashell_rows_read_total" in resp.text
