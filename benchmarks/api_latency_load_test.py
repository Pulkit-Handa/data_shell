"""Concurrent API load testing & latency percentile measurement (P50, P95, P99) with full authentication & delivery verification."""

import time
import json
import statistics
import concurrent.futures
from pathlib import Path
from fastapi.testclient import TestClient
import polars as pl

from datashell.server.app import app
from datashell.core.config import settings
from datashell.contract.models import (
    DataContract, SourceDefinition, SchemaColumn,
    QualityRule, BatchRule, RowRuleCheckType, BatchRuleCheckType, DeliverySpec, DeliveryTarget
)
from datashell.contract.registry import ContractRegistry


def run_concurrent_load_test(total_requests: int = 50, concurrency: int = 8):
    client = TestClient(app)
    
    # 1. Register a dedicated, validated benchmark contract
    bench_contract = DataContract(
        metadata={"name": "api-benchmark-product", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path="bench_input.parquet",
                    format="parquet",
                    schema=[
                        SchemaColumn(name="vendor_id", polars_type="Int64", nullable=False),
                        SchemaColumn(name="amount", polars_type="Float64", nullable=False),
                    ]
                )
            ],
            "quality": {
                "behavior": "DROP_ROW",
                "rules": [
                    QualityRule(rule_id="positive-amount", check=RowRuleCheckType.GREATER_THAN, column="amount", value=0.0)
                ],
                "batch_rules": [
                    BatchRule(rule_id="min-records", check=BatchRuleCheckType.MIN_ROW_COUNT, value=100)
                ]
            },
            "delivery": DeliverySpec(
                targets=[DeliveryTarget(path="./output/bench_delivered.parquet", mode="overwrite")],
                quarantinePath="./output/quarantine"
            )
        }
    )
    reg = ContractRegistry(settings.contracts_dir)
    reg.save_contract(bench_contract)

    # 2. Synthesize valid 1,000-row payload
    bench_data_path = Path("./data/bench_payload_1k.parquet")
    bench_data_path.parent.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame({
        "vendor_id": list(range(1, 1001)),
        "amount": [10.5 * (i % 50 + 1) for i in range(1000)]
    })
    df.write_parquet(bench_data_path)
    file_bytes = bench_data_path.read_bytes()

    print(f"Running Authenticated Concurrent API Load Test: {total_requests} requests @ concurrency={concurrency}...")

    headers = {"X-API-Key": settings.master_api_key}

    def make_request():
        start = time.perf_counter()
        files = {"file": ("bench_1k.parquet", file_bytes, "application/octet-stream")}
        data = {"contract_name": "api-benchmark-product"}
        resp = client.post("/api/v1/enforce", data=data, files=files, headers=headers)
        latency = (time.perf_counter() - start) * 1000.0  # ms
        
        is_success = False
        delivered_count = 0
        if resp.status_code == 200:
            try:
                body = resp.json()
                if body.get("status") == "LOADED":
                    delivered_count = body.get("reconciliation", {}).get("rows_delivered", 0)
                    if delivered_count > 0:
                        is_success = True
            except Exception:
                pass
        return resp.status_code, is_success, delivered_count, latency

    latencies = []
    http_200_count = 0
    business_success_count = 0

    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(make_request) for _ in range(total_requests)]
        for f in concurrent.futures.as_completed(futures):
            status_code, is_success, delivered_count, lat = f.result()
            if status_code == 200:
                http_200_count += 1
            if is_success:
                business_success_count += 1
            latencies.append(lat)
    wall_duration = time.perf_counter() - wall_start

    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[int(len(latencies) * 0.99)]
    rps = total_requests / wall_duration

    print("\n" + "=" * 65)
    print("      AUTHENTICATED API LATENCY & DELIVERY INTEGRITY REPORT")
    print("=" * 65)
    print(f"Total Requests:          {total_requests}")
    print(f"Concurrency:             {concurrency}")
    print(f"HTTP 200 Responses:      {http_200_count}/{total_requests} ({(http_200_count/total_requests)*100:.1f}%)")
    print(f"Business LOADED Verified:{business_success_count}/{total_requests} ({(business_success_count/total_requests)*100:.1f}%)")
    print(f"Total Wall Time:         {wall_duration:.2f}s")
    print(f"Throughput (RPS):        {rps:.1f} req/sec")
    print(f"P50 Latency:             {p50:.2f} ms")
    print(f"P95 Latency:             {p95:.2f} ms")
    print(f"P99 Latency:             {p99:.2f} ms")
    print("=" * 65)

    if bench_data_path.exists():
        bench_data_path.unlink()


if __name__ == "__main__":
    run_concurrent_load_test(total_requests=50, concurrency=8)
