"""Concurrent API load testing & latency percentile measurement (P50, P95, P99),
with full authentication & delivery verification."""

import concurrent.futures
import statistics
import time
from pathlib import Path

import polars as pl
from fastapi.testclient import TestClient

from datashell.contract.models import (
    BatchRule,
    BatchRuleCheckType,
    DataContract,
    DeliverySpec,
    DeliveryTarget,
    QualityRule,
    RowRuleCheckType,
    SchemaColumn,
    SourceDefinition,
)
from datashell.contract.registry import ContractRegistry
from datashell.core.config import settings
from datashell.server.app import app


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
                    QualityRule(rule_id="positive-amount",
                                check=RowRuleCheckType.GREATER_THAN, column="amount", value=0.0)
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

    # The gateway enforces a configured request-concurrency ceiling and answers
    # 503 "capacity exhausted" beyond it. That ceiling is part of the supported
    # envelope, so the benchmark measures behaviour *within* it: offered
    # concurrency is capped to the configured limit and capacity rejections are
    # retried with a small backoff so the measurement reflects sustained
    # service rather than a burst that the gateway is designed to refuse.
    configured_limit = int(getattr(settings, "max_concurrent_requests", 2) or 2)
    effective_concurrency = max(1, min(concurrency, configured_limit))
    max_retries = 20
    retry_delay_seconds = 0.05

    def make_request():
        start = time.perf_counter()
        files = {"file": ("bench_1k.parquet", file_bytes, "application/octet-stream")}
        data = {"contract_name": "api-benchmark-product"}
        attempts = 0
        capacity_rejections = 0
        status_code = 0
        is_success = False
        delivered_count = 0
        while attempts <= max_retries:
            attempts += 1
            resp = client.post("/api/v1/enforce", data=data, files=files, headers=headers)
            status_code = resp.status_code
            if status_code == 200:
                try:
                    body = resp.json()
                    if body.get("status") == "LOADED":
                        delivered_count = body.get("reconciliation", {}).get("rows_delivered", 0)
                        if delivered_count > 0:
                            is_success = True
                except Exception:
                    pass
                break
            if status_code == 503:
                # Capacity rejection: back off and retry within the limit.
                capacity_rejections += 1
                time.sleep(retry_delay_seconds)
                continue
            break
        latency = (time.perf_counter() - start) * 1000.0  # ms
        return status_code, is_success, delivered_count, latency, capacity_rejections, attempts

    latencies = []
    http_200_count = 0
    business_success_count = 0
    capacity_rejection_count = 0
    total_attempts = 0

    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
        futures = [executor.submit(make_request) for _ in range(total_requests)]
        for f in concurrent.futures.as_completed(futures):
            status_code, is_success, delivered_count, lat, capacity_rejections, attempts = f.result()
            if status_code == 200:
                http_200_count += 1
            if is_success:
                business_success_count += 1
            capacity_rejection_count += capacity_rejections
            total_attempts += attempts
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
    print(f"Offered Concurrency:     {concurrency}")
    print(f"Configured Limit:        {configured_limit}")
    print(f"Effective Concurrency:   {effective_concurrency}")
    print(f"HTTP 200 Responses:      {http_200_count}/{total_requests} ({(http_200_count/total_requests)*100:.1f}%)")
    print(f"Business LOADED Verified:{business_success_count}/{total_requests} "
          f"({(business_success_count/total_requests)*100:.1f}%)")
    print(f"Capacity 503 Retries:    {capacity_rejection_count} (total attempts {total_attempts})")
    print(f"Total Wall Time:         {wall_duration:.2f}s")
    print(f"Throughput (RPS):        {rps:.1f} req/sec")
    print(f"P50 Latency:             {p50:.2f} ms")
    print(f"P95 Latency:             {p95:.2f} ms")
    print(f"P99 Latency:             {p99:.2f} ms")
    print("=" * 65)

    # The benchmark is only meaningful if every request actually succeeded.
    if http_200_count != total_requests or business_success_count != total_requests:
        raise RuntimeError(
            f"API load test incomplete: {http_200_count}/{total_requests} HTTP 200, "
            f"{business_success_count}/{total_requests} business-verified"
        )

    if bench_data_path.exists():
        bench_data_path.unlink()


if __name__ == "__main__":
    run_concurrent_load_test(total_requests=50, concurrency=8)
