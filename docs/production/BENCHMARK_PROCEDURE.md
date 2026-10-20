# Data Shell — Benchmark Procedure and Measured Results

**Version:** 1.0
**Date:** 2026-10-20
**Purpose:** a reproducible procedure and the actual measurements it produced,
so the supported capacity envelope is evidence-based rather than asserted.

---

## 1. Supported envelope under test

| Dimension | Value under test |
|---|---|
| Topology | One Linux host, one process, one worker, one tenant |
| Formats | CSV (streaming path), Parquet (in-memory path) |
| Max file size | 1 GiB provisional cap |
| Concurrency | One active processing job; API request concurrency bounded by `MAX_CONCURRENT_REQUESTS` (default 2) |
| Billing | Dry-run only |
| Signing | HMAC in the harness; Ed25519 in the deployment profile |

**The 1 GiB cap is enforced by configuration, not by the code path.** The
measurements below are what justifies it; they are not a proof that a larger
file is safe.

---

## 2. Test environment (recorded, not assumed)

| Item | Value |
|---|---|
| OS / kernel | Linux 7.2.11-200.fc44.x86_64 |
| CPU | 16 vCPU (shared cloud instance) |
| RAM | 13,701 MiB total; ~2,800 MiB available at measurement time |
| Temp storage | 6.7 GiB tmpfs at `/tmp` |
| Python | 3.14.8 (CPython, GCC 16.2.1) |
| Filesystem | tmpfs (RAM-backed) — **not** the production block volume |
| Build | Working tree at branch `production_hardening` |

> **This is a shared development container, not production hardware.** The
> numbers below are a *lower bound* on capability for the in-memory path and a
> *first* data point for the streaming path. They are **not** a production
> capacity qualification. A drill on the target host (§ 7) is required to close
> the capacity gate.

---

## 3. Procedure A — volumetric throughput and peak memory

```bash
ENVIRONMENT=test DATASHELL_STORAGE_ROOT=/tmp/bench PYTHONPATH=. \
  python benchmarks/volumetric_stress_test.py
```

Generates Parquet batches with 4 % seeded defects at 10 k / 100 k / 500 k / 1 M
rows and asserts, for every scale: terminal status `LOADED`, reconciliation
footing matched, the expected delivered row count, and the delivered Parquet
file's actual row count. A run that silently dropped rows fails the harness.

### Measured results

| Scale (rows) | Duration | Throughput | Delivered | Peak RSS | Footing |
|---:|---:|---:|---:|---:|:---|
| 10,000 | 0.37 s | 27,158 r/s | 9,600 | 261.2 MB | MATCHED |
| 100,000 | 0.13 s | 764,691 r/s | 96,000 | 329.3 MB | MATCHED |
| 500,000 | 0.47 s | 1,067,381 r/s | 480,000 | 539.9 MB | MATCHED |
| 1,000,000 | 0.52 s | 1,940,579 r/s | 960,000 | 693.7 MB | MATCHED |

**Reading:** the in-memory path is CPU-bound, not I/O-bound, on this host. Peak
RSS grows with the batch size because the whole batch is materialised — the
in-memory path is not appropriate for large inputs; the streaming path is.

---

## 4. Procedure B — large-file capacity (the 1 GiB cap)

```bash
ENVIRONMENT=test DATASHELL_STORAGE_ROOT=/tmp/bench PYTHONPATH=. \
  python benchmarks/large_file_capacity_test.py --size-mb 1024
```

Writes a CSV with a narrow-but-realistic schema (4 columns) and ~4 % seeded
defects, then runs the **out-of-core streaming path**. The harness fails
(non-zero exit) if the run does not deliver rows or if the reconciliation
footing does not match.

### Measured results

| Input size | Rows | Delivered | Wall time | Throughput | Peak RSS | Storage growth | Footing | Unaccounted |
|---:|---:|---:|---:|---:|---:|---:|:---:|---:|
| 33.5 MiB | 900,000 | 864,000 | 5.95 s | 151,207 r/s | 472.4 MB | 15.2 MB | MATCHED | 0 |
| 1,024.3 MiB | 27,550,000 | 26,448,000 | 173.14 s | 159,119 r/s | **752.8 MB** | 467.8 MB | MATCHED | 0 |

**Reading — this is the key capacity evidence:** a 30× larger input raised peak
RSS by only ~280 MB (472 MB → 753 MB), because the streaming path materialises
one chunk at a time. Memory is bounded by the chunk size, not by the input
size. The 1 GiB cap is therefore defensible on memory grounds for a 4-column
schema.

**Caveats that keep this from being a capacity qualification:**
- A wide schema (many columns, large strings) will use materially more memory
  per chunk than this 4-column probe.
- The measurement ran against tmpfs; the production block volume is slower, so
  wall time will be worse and temp-disk behaviour differs.
- Only one job ran at a time; the pilot envelope is one active job, so that is
  consistent, but concurrent-job contention is unmeasured.

---

## 5. Procedure C — authenticated API latency

```bash
ENVIRONMENT=test DATASHELL_STORAGE_ROOT=/tmp/bench PYTHONPATH=. \
  python benchmarks/api_latency_load_test.py
```

50 authenticated `POST /api/v1/enforce` requests carrying a 1,000-row Parquet
feed, with the response body checked for `status == LOADED` and a non-zero
delivered count. The harness raises if any request does not succeed, so a
partially-successful run cannot be reported as a pass.

### Measured results

| Metric | Value |
|---|---|
| Total requests | 50 |
| Offered concurrency | 8 |
| Configured limit (`MAX_CONCURRENT_REQUESTS`) | 2 |
| Effective concurrency | 2 |
| HTTP 200 responses | 50/50 (100 %) |
| Business-verified `LOADED` | 50/50 (100 %) |
| Capacity 503 retries | 0 (total attempts 50) |
| Wall time | 2.39 s |
| Throughput | 20.9 req/s |
| P50 latency | 78.60 ms |
| P95 latency | 183.91 ms |
| P99 latency | 410.98 ms |

**Important finding recorded by the harness:** before it was corrected, the
benchmark offered concurrency 8 against a configured limit of 2 and measured
**48/50 requests rejected with HTTP 503** ("capacity exhausted"). That is the
configured limit working correctly, not a defect — but it means the configured
ceiling, not the request rate, is the binding constraint at this concurrency.
The harness now caps offered concurrency to the configured limit and retries
capacity rejections, so the reported latency reflects sustained service.

---

## 6. What the measurements do and do not establish

| Claim | Status |
|---|---|
| The in-memory path sustains ~1.9 M rows/s at 1 M rows on this host | **Established** (Procedure A) |
| A 1 GiB CSV completes with bounded memory (~753 MB peak RSS), correct footing, and zero unaccounted rows | **Established on this host** (Procedure B) |
| The gateway sustains 20.9 req/s at concurrency 2 with 100 % business-verified deliveries | **Established on this host** (Procedure C) |
| The production deployment can process 1 GiB within its latency/error budget | **NOT established** — requires § 7 |
| The system sustains N concurrent processing jobs | **NOT established** — the pilot envelope is one active job; not implemented or claimed |
| Wide-schema files stay within the same memory bound | **NOT established** — only a 4-column schema was measured |

---

## 7. Target-environment drill (required to close the capacity gate)

Run on the actual pilot host, against the actual image and storage volume.

**Preconditions:** the host matches the deployment profile; the storage volume
is a persistent block volume (not tmpfs); `TMPDIR` points at that volume.

```bash
# 1. Record the environment alongside the results.
uname -a; nproc; free -m; df -h /app/storage; python3 --version
stat -f -c %T /app/storage          # must not be tmpfs/nfs/cifs

# 2. Large-file sweep at the provisional cap.
ENVIRONMENT=production \
DATASHELL_API_KEY="$DATASHELL_API_KEY" DATASHELL_HMAC_SECRET="$DATASHELL_HMAC_SECRET" \
ALLOWED_ORIGINS=https://console.example.com FORWARDED_ALLOW_IPS=127.0.0.1 \
DATASHELL_STORAGE_ROOT=/app/storage PYTHONPATH=/app \
python benchmarks/large_file_capacity_test.py --size-mb 1024

# 3. API latency at the configured concurrency.
ENVIRONMENT=production ... python benchmarks/api_latency_load_test.py
```

### Objective pass criteria (proposed — requires owner approval)

| Criterion | Threshold | Rationale |
|---|---|---|
| 1 GiB file completes | `LOADED`, footing matched, 0 unaccounted rows | correctness is non-negotiable |
| Peak RSS at 1 GiB | ≤ 1.5 GiB | leaves headroom on a 4 GiB limit |
| Peak storage growth | ≤ 2× the input size | matches `DATASHELL_DISK_HEADROOM_MULTIPLIER` |
| Throughput at 1 GiB | ≥ 50,000 rows/s | ~9 minutes worst case for a 1 GiB feed |
| API P95 at configured concurrency | ≤ 500 ms | below any plausible partner timeout |
| Error rate | 0 failed requests under the configured concurrency | the limit is a ceiling, not a failure mode |
| Footing | every run, no exceptions | the core invariant |

**If any criterion fails, the file-size cap must be lowered to the largest
measured size that passes, and the production configuration must enforce that
value** (`MAX_UPLOAD_SIZE_BYTES`). Do not leave the default 20 GiB in place on
the basis of an unmeasured assumption.

---

## 8. Reproducing the harnesses

```bash
pip install --require-hashes -r requirements-dev.lock
pip install --no-deps --no-build-isolation -e .
export PYTHONPATH=$PWD
python benchmarks/volumetric_stress_test.py
python benchmarks/large_file_capacity_test.py --size-mb 256
python benchmarks/api_latency_load_test.py
```

Each harness cleans up the files it generates and exits non-zero on a failed
assertion, so a green exit code is a meaningful result.
