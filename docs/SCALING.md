# Data Shell — Scaling Guide

**Purpose:** every knob that changes how much data Data Shell can push through
the boundary, expressed through environment variables so a deployment is
re-tuned without code changes.

The engine has exactly **two ingest backends** and **one delivered-artifact
codec**. Which backend serves a run is a policy decision, evaluated in one
place (`choose_execution_backend` in `datashell/pipeline/runner.py`), so the
size at which a feed switches from vectorized in-memory processing to
out-of-core micro-batches is configuration, not caller code.

---

## 1. The five scaling surfaces

| # | Surface | Variable(s) | Effect |
|---|---|---|---|
| 1 | Ingest backend selection | `DATASHELL_INGEST_MODE`, `STREAM_AUTO_THRESHOLD_BYTES` | in-memory vs out-of-core micro-batches |
| 2 | Micro-batch size | `STREAM_CHUNK_ROWS` | streaming granularity (chunk scratch; see the measured envelope below) |
| 3 | SQL evaluation parallelism | `DATASHELL_DUCKDB_THREADS`, `DUCKDB_MEMORY_LIMIT` | batch-rule throughput and spill |
| 4 | Delivered artifact codec | `DATASHELL_DELIVERY_COMPRESSION` | delivered size vs CPU |
| 5 | Concurrent work & disk | `MAX_CONCURRENT_RUNS`, `MAX_CONCURRENT_REQUESTS`, `DATASHELL_DISK_HEADROOM_MULTIPLIER`, `MIN_FREE_DISK_BYTES` | how many feeds at once, and the staging footprint |

---

## 2. Deployment profiles

| Profile | Recommended env | Why |
|---|---|---|
| **Laptop / demo** | defaults | `auto` mode, 64 MB threshold, zstd delivery — nothing to think about |
| **Single-node batch (≤ ~10× RAM)** | defaults + `MAX_CONCURRENT_RUNS=1` | threshold auto-promotes big feeds; one run at a time keeps staging disk calm |
| **Streaming node (≫ RAM)** | `DATASHELL_INGEST_MODE=stream` `STREAM_CHUNK_ROWS=250_000` | pin the backend; size batches to ~1–2 s of CPU each |
| **HTTP gateway** | defaults + tuned `MAX_CONCURRENT_RUNS`/`MAX_CONCURRENT_REQUESTS` | the ceilings are honest capacity limits; see § 6 |
| **Batch-SQL heavy (batch rules on huge feeds)** | `DATASHELL_DUCKDB_THREADS=<cores>` `DUCKDB_MEMORY_LIMIT=<40 % RAM>` | DuckDB multicore evaluation, previews its memory envelope |

---

## 3. Ingest backend selection

`choose_execution_backend(source_size, force_streaming, ingest_mode, auto_threshold_bytes)`
resolves, in precedence order:

1. **Explicit caller decision** — CLI `--streaming` / `--no-streaming`, or the
   API request field. Always wins.
2. **`DATASHELL_INGEST_MODE`** — deployment policy:
   - `auto` (default): promote to streaming when the source size reaches
     `STREAM_AUTO_THRESHOLD_BYTES` (default 64 MiB).
   - `memory`: always the in-memory backend (small, latency-sensitive nodes
     that want to fail fast instead of stream).
   - `stream`: always out-of-core (the profile is the one to study before
     sizing hosts; see the measured envelope below).
3. **Size threshold** — the `auto` fallback.

Both backends produce byte-identical *decisions* (same rules, same ledger
lines, same certificate claims); only the runtime profile differs. Measured
on the reference development node (single machine, 13 GB RAM, Python 3.14):

| | in-memory | out-of-core streaming |
|---|---|---|
| Throughput (measured) | **~3.3 M rows/s** at 1 M rows | ~**4.9 M rows/s** on a 5 M-row Parquet feed; ~158 k rows/s on CSV |
| Peak RSS (measured) | ~0.78 GB at 1 M rows | grows with rows processed, not only with chunk: roughly 0.15–0.2 GB per 1 M rows on Parquet, ~0.6 GB per 1 M rows on CSV (allocator churn included) |
| Whole-run reject semantics | identical | identical (REJECT_RUN parity is enforced by tests) |

**Measured streaming envelope.** Reproduce with
`python benchmarks/streaming_profile.py --rows 5000000 --format parquet` and
`--format csv`. The 5 M-row Parquet feed enforced end-to-end (cast → validate →
reconcile → signed delivery) in ~1 s at 989 MiB peak RSS; the same rows as a
87 MiB CSV took ~32 s at ~3.0 GB peak. The difference is by design: Parquet
chunks arrive typed and stay on the vectorized fast path, while CSV chunk
columns arrive as strings, so Tier-1 falls back to the strict per-row caster to
preserve exact per-row rejection diagnostics. For very large CSV lands, convert
to Parquet first (or measure your own envelope before sizing hosts); the
streaming write path itself is bounded — admission, rules, and staged output
all flush per micro-batch, one Parquet row group each.

---

## 4. Micro-batch size

`STREAM_CHUNK_ROWS` (default `100000`) is both the read batch size and the
write batch size for the streaming backend's incremental Parquet writers.
Larger chunks amortize per-batch dispatch across more rows (throughput ↑,
RSS ↑); smaller chunks lower the memory ceiling for constricted nodes.
250 000 rows/batch on wide schemas ≈ 100–200 MB of columnar scratch — keep
total peak ≤ `DUCKDB_MEMORY_LIMIT` + chunk scratch.

## 5. Batch-rule (SQL) evaluation scale

Batch rules (MIN_ROW_COUNT, ALLOWED_VALUES over columns, aggregate checks)
evaluate in **embedded DuckDB** against an Arrow dataset streamed from the
delivered Parquet — data is *never* fully materialized for evaluation.

- `DATASHELL_DUCKDB_THREADS` (default `4`): worker threads for DuckDB's
  vectorized operators. On dedicated nodes set this to the node's core count.
- `DUCKDB_MEMORY_LIMIT` (default `1GB`): DuckDB's own operator memory
  envelope; must be a positive size (`512MB`, `2GB`, …). Anything above the
  limit spills to `DATASHELL_TMP_DIR` — keep that on the same persistent
  volume as staging (`TMPDIR` on tmpfs is rejected by the deployment probe).
- `enable_external_access=false` is forced: SQL rules can only touch the
  registered dataset.

## 6. Delivery artifact codec

`DATASHELL_DELIVERY_COMPRESSION` (default `zstd`) sets the Parquet codec for
delivered targets and quarantine snapshots on **both** backends
(`snappy | zstd | gzip | lz4 | brotli | uncompressed`).

- `zstd` — delivered size ↓ ~30–50 % vs snappy at modest CPU cost. This was
  already the codec of in-memory 1.0 deliveries; the streaming path is now
  unified on it.
- `snappy` — lowest write CPU when the network is not the bottleneck.
- `uncompressed` — for delivery sinks that re-compress landings themselves.

Changing the codec does **not** change any digest claim: SHA-256 binds are
computed after staging against the exact bytes published, so certificates
stay truthful regardless of codec.

## 7. Concurrent work and staging disk

- `MAX_CONCURRENT_RUNS` (default `2`) — pipeline executions allowed at once;
  over-limit HTTP runs get HTTP 503 (client retries, e.g. after backoff).
- `MAX_CONCURRENT_REQUESTS` (default `2`) — requests whose bodies the gateway
  admits concurrently.
- `DATASHELL_DISK_HEADROOM_MULTIPLIER` (default `2.0`) — a run refuses to
  start unless the storage volume can hold `source × multiplier` (staged
  target + worst-case quarantine). Raise it for contracts that hold nearly
  every row (heavy quarantine); lower it when most rows deliver.
- `MIN_FREE_DISK_BYTES` (default `1 GiB`) — absolute floor regardless of the
  multiplier.
- `MAX_UPLOAD_SIZE_BYTES` (default 20 GiB) — per-request ceiling. The HTTP
  body limit follows automatically.

Measured envelope on the reference host (see
`production/BENCHMARK_PROCEDURE.md`): a 1 GiB CSV enforces with **~753 MB
peak RSS**, correct footing, and 0 unaccounted rows; the gateway sustained
**20.9 req/s at concurrency 2** (P50 78.6 ms / P95 183.9 ms / P99 411 ms). Do
not extrapolate "50 GB in memory" from the default upload ceiling — § 8 tells
you how to measure *your* envelope.

## 8. Worked examples

### a) 50 GiB nightly partition, 32-core node, 128 GB RAM

```bash
DATASHELL_STORAGE_ROOT=/srv/datashell
DATASHELL_TMP_DIR=/srv/datashell/tmp          # same block volume as root
DATASHELL_INGEST_MODE=stream                   # 50 GiB ≫ RAM headroom
STREAM_CHUNK_ROWS=250000
DATASHELL_DUCKDB_THREADS=16                    # rule evaluation only
DUCKDB_MEMORY_LIMIT=16GB                       # ~12 % of RAM
DATASHELL_DELIVERY_COMPRESSION=zstd
MAX_CONCURRENT_RUNS=1                          # nightly batch, one at a time
MAX_UPLOAD_SIZE_BYTES=53687091200              # 50 GiB
```

### b) Constricted K8s processing Deployment (2 vCPU, 4 GiB)

```yaml
env:
  - name: ENVIRONMENT
    value: production
  - name: DATASHELL_INGEST_MODE
    value: stream
  - name: STREAM_CHUNK_ROWS
    value: "50000"
  - name: DATASHELL_DUCKDB_THREADS
    value: "2"
  - name: DUCKDB_MEMORY_LIMIT
    value: "1GB"
  - name: MAX_CONCURRENT_RUNS
    value: "1"
  - name: MAX_UPLOAD_SIZE_BYTES
    value: "1073741824"          # 1 GiB per request
  # readiness-probe the /ready endpoint: it fails closed when storage headroom
  # or recovery state are not healthy.
```

## 9. Measure your own envelope (before you commit to numbers)

```bash
# Large-file sweep (use the largest feed you will accept)
ENVIRONMENT=production \
DATASHELL_API_KEY=... DATASHELL_HMAC_SECRET=... \
ALLOWED_ORIGINS=https://console.example.com FORWARDED_ALLOW_IPS=127.0.0.1 \
DATASHELL_STORAGE_ROOT=/app/storage PYTHONPATH=. \
python benchmarks/large_file_capacity_test.py --size-mb 1024

# Gateway latency at your configured concurrency
python benchmarks/api_latency_load_test.py

# Multi-feed volumetric soak with seeded defects
PYTHONPATH=. python benchmarks/volumetric_stress_test.py
```

Pass criteria and the honest claim boundary are already defined in
`production/BENCHMARK_PROCEDURE.md` § 6–7: correctness criteria (LOADED,
footing, 0 unaccounted) are non-negotiable; throughput/RSS numbers are only
claims once you have measured them on the target host.

## 10. Failure modes to expect when under-scaled

| Symptom | Meaning | Knob to revisit |
|---|---|---|
| HTTP 503 "Processing capacity is currently exhausted" | `MAX_CONCURRENT_RUNS` ceiling reached | raise it, or retry with backoff |
| Run refuses to start with headroom error | staging disk cannot hold `source × multiplier` | `MIN_FREE_DISK_BYTES` / `DATASHELL_DISK_HEADROOM_MULTIPLIER` / free space |
| `REJECT_RUN` for `MIN_ROW_COUNT` on truncated feeds | volume/mount truncation or feed dropout — *not* a scaling bug | investigate the feed; the ledger will show why |
| DuckDB out-of-memory on batch rules | operator memory envelope too small for wide schemas | `DUCKDB_MEMORY_LIMIT` ↑, add `TMPDIR` spill space |
