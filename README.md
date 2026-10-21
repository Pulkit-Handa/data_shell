# Data Shell

**Contract-driven enforcement at the data boundary.**

[![CI](https://github.com/Pulkit-Handa/data_shell/actions/workflows/ci.yml/badge.svg)](https://github.com/Pulkit-Handa/data_shell/actions/workflows/ci.yml)
[![Python 3.11 | 3.12 | 3.13](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](pyproject.toml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Status: controlled pilot](https://img.shields.io/badge/status-controlled%20pilot-yellow.svg)](#project-status-and-limitations)

> **Bad data never lands, and never leaves.**

Data Shell sits between an external data feed and the systems that consume it. It
evaluates the feed against a versioned contract, quarantines rows that fail, and
publishes only after reconciliation proves every input row is accounted for. A
successful delivery is committed atomically and bound to a signed certificate;
every terminal outcome is recorded in a durable ledger.

It is a **write gate, not a validation report**. Validation frameworks tell you
what is wrong with a dataset. Data Shell decides whether the dataset may cross the
publication boundary — and produces portable evidence of what was published.

> [!IMPORTANT]
> **Project status: controlled-pilot candidate (v2.0.0).** The reference topology
> is one process, one worker, one replica, and one trusted storage root. It is not
> approved for general production. See [Project status and limitations](#project-status-and-limitations).
>
> **The package is not yet published on PyPI.** The `datashell` name there belongs
> to an unrelated project. Install from this repository.

---

## Contents

- [Why Data Shell](#why-data-shell)
- [How it works](#how-it-works)
- [Use cases](#use-cases)
- [Quick start](#quick-start)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Architecture and deployment](#architecture-and-deployment)
- [Verification and evidence](#verification-and-evidence)
- [Security](#security)
- [Observability](#observability)
- [Performance and capacity](#performance-and-capacity)
- [Development and testing](#development-and-testing)
- [Project status and limitations](#project-status-and-limitations)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

---

## Why Data Shell

Most data-quality tools answer: *does this dataset violate any expectations?*
Data Shell answers a narrower and more consequential question: *may this feed be
published, and can we prove — later, offline, from the bytes on disk — exactly
what was published?*

| Property | What it means in practice |
| --- | --- |
| **Enforcement at the write boundary** | Rejected rows never reach the delivery target. There is no report-only mode and no partial batch. |
| **Zero-deficit reconciliation** | A run publishes only when every input row is accounted for (`read = admitted + held + refused`, zero unaccounted). On a whole-run rejection, admitted rows are withheld and delivered remains zero. |
| **Atomic publication** | The delivery target, immutable lineage copy, and certificate are staged and committed through a write-ahead journal. Crash recovery rolls back to the last committed generation. |
| **Portable evidence** | Certificates bind the contract, source bytes, delivered bytes, and quarantine artifacts under a Merkle root, signed with Ed25519 or HMAC-SHA256. `datashell verify` checks them without calling the service. |
| **One engine, several surfaces** | The same enforcement pipeline is available as a CLI, a Python library, an authenticated HTTP gateway, and an Apache Airflow provider. |
| **Bounded operational scope** | Data Shell does not replace your orchestrator, storage, catalogue, alerting, or identity provider. It owns one decision and the evidence for it. |

Data Shell is complementary to validation and observability stacks. Keep them for
profiling, discovery, and cataloguing; use Data Shell where a feed must be either
accepted as a whole or refused with evidence.

---

## How it works

Every run — in-memory, file-backed, or streaming — passes through four stages:

| Stage | Responsibility | On failure |
| --- | --- | --- |
| **1. Structural gate** | Strict per-row schema casting and type realization. | Unrealizable rows are refused and quarantined with the exact reason. |
| **2. Semantic gate** | Vectorized row rules (Polars) and batch rules (DuckDB), including uniqueness, control totals, allowed values, and SQL assertions. | Held rows are quarantined and attributed to the first failing `rule_id`. |
| **3. Reconciliation** | Asserts `rows_read = admitted + held + refused` with exactly zero unaccounted rows; admitted rows become delivered only when the run commits. | Any mismatch refuses publication; the delivery target is not replaced. |
| **4. Atomic publish** | Stages the target, immutable lineage copy, and signed certificate; commits them under a write-ahead journal and generation pointer. | Startup recovery restores the last committed generation. |

A successful run publishes three things:

- the **delivery target** at the contract-defined path (the latest delivery);
- an **immutable lineage copy** named `<run_id>_<target_name>`; and
- a **signed audit certificate** named `<run_id>_audit_certificate.json`.

The commit point is the atomic replacement of `<target>.generation.json`. Consumers
that read the delivery target directly should resolve that pointer or verify the
certificate; otherwise they can observe a superseded generation. This limitation is
documented in [the generation contract](docs/production/GENERATION_CONTRACT.md).

Depending on the contract, a run can either withhold failing rows and publish the
survivors (`DROP_ROW`) or reject the entire batch (`REJECT_RUN`). In both cases the
quarantine evidence and ledger record are retained.

---

## Use cases

- **Inbound partner and vendor feeds** — schema drift, malformed values, duplicate
  keys, and broken control totals stop at the landing boundary.
- **Outbound regulatory and partner submissions** — prove that the filed artifact
  is the artifact that passed the contract.
- **Commercial delivery and settlement** — calculate a settlement from a verified
  delivery run, with no path from an unverified or unreconciled run to a payable
  amount.
- **Orchestrated batch pipelines** — gate an Airflow DAG on a local file or a remote
  gateway call, and branch on the terminal boundary verdict.
- **Incident forensics** — answer which rows were held, by which rule, in which run,
  without re-reading the source.

Data Shell is **not** an event-streaming platform, a scheduler, a data catalogue, a
dashboarding product, or a payment processor. See
[Project status and limitations](#project-status-and-limitations).

---

## Quick start

### Prerequisites

- Python **3.11, 3.12, or 3.13**
- Git
- A local checkout of this repository

The core engine needs only `polars`, `pyarrow`, `pydantic`, `cryptography`, and
`prometheus-client`. The gateway, DuckDB batch rules, billing adapter, and
OpenTelemetry support are optional extras.

### 1. Install from source

```bash
git clone https://github.com/Pulkit-Handa/data_shell.git
cd data_shell
python -m venv .venv
source .venv/bin/activate

pip install -e ".[all]"   # engine + gateway + DuckDB + billing adapter
# or: pip install -e .   # engine only
```

Confirm what the environment supports:

```bash
datashell doctor
```

### 2. Run the self-contained demo

```bash
datashell demo
```

The demo generates a 1,000-row vendor feed with 100 seeded defects, enforces the
contract, and then demonstrates tamper detection and recovery. Expected outcome:

```text
1,000 read = 40 refused + 60 held + 900 delivered
Unaccounted for: 0 · Matched
```

It then flips one byte in the delivered Parquet, reports
`TAMPERED_UNDERLYING_ARTIFACTS`, restores the authentic bytes, and returns to
`AUTHENTIC_AND_UNMODIFIED`.

### 3. Enforce a sample feed

```bash
mkdir -p output/quickstart

datashell run \
  --contract examples/vendor_settlements_contract.json \
  --source   examples/samples/vendor_settlements_1k.jsonl \
  --target   ./output/quickstart/settlements.parquet \
  --audit    ./output/quickstart/settlements_audit.json \
  --tenant   quickstart
```

Exit codes are part of the contract:

| Exit code | Meaning |
| --- | --- |
| `0` | Delivered |
| `2` | Rejected by contract policy |
| `1` | Engine or infrastructure failure |

The target and audit paths must share a storage root; the engine refuses a
delivery that would escape its sandbox.

### 4. Verify the evidence

```bash
datashell verify ./output/quickstart/settlements_audit.json
datashell ledger summary --tenant quickstart
datashell ledger quarantine --tenant quickstart --limit 5
```

`verify` recomputes the artifact hashes and Merkle root and checks the signature
envelope. It does not require the gateway, the ledger, or network access.

### 5. Start the gateway (optional)

```bash
uvicorn datashell.server.app:app --host 127.0.0.1 --port 8000
```

The console is served at `http://127.0.0.1:8000`. Production startup requires
explicit secrets and an exact HTTPS origin allowlist; see
[Configuration](#configuration) and the
[operations runbook](docs/production/OPERATIONS_RUNBOOK.md).

---

## Installation

Data Shell is currently installed from a source checkout. There is no supported
PyPI release under this name yet.

### Dependency extras

| Extra | Adds | Use when |
| --- | --- | --- |
| *(none)* | `polars`, `pyarrow`, `pydantic`, `cryptography`, `prometheus-client` | You only need the in-process engine. |
| `server` | FastAPI, Uvicorn, multipart uploads | You need the HTTP gateway or console. |
| `duckdb` | DuckDB | Contracts use batch rules that compile to SQL. |
| `billing` | `httpx` | You use the opt-in Stripe draft-invoice adapter. |
| `airflow` | Apache Airflow | You run the provider inside an Airflow environment. |
| `otel` | OpenTelemetry SDK and OTLP/gRPC exporter | You export traces to a collector. |
| `dev` | pytest, ruff, mypy, pre-commit, and test helpers | You are developing or running the suite. |
| `all` | `server`, `duckdb`, and `billing` | You want the full service surface. |

`airflow` is deliberately excluded from `all`: Apache Airflow constrains its own
Python versions and is deployed as scheduler infrastructure.

```bash
pip install -e ".[server,duckdb]"
pip install -e ".[dev]"
```

---

## Usage

### CLI

```text
datashell run               Enforce a contract against a file
datashell validate-contract Validate a contract before running it
datashell verify            Verify a certificate and its bound artifacts
datashell ledger            Query the durable boundary ledger
datashell demo              Run the refusal/tamper/verify walkthrough
datashell rules             List built-in and plugin rule checks
datashell benchmark         Measure throughput on a synthetic feed
datashell doctor            Report available capabilities
datashell contract          Manage commercial delivery contracts
datashell settlement        Calculate, explain, verify, and draft billing
```

Use `datashell <command> --help` for options. `datashell run --json` returns a
machine-readable reconciliation summary for schedulers.

### Python API

```python
import json
from pathlib import Path

from datashell import DataContract, DataShellPipeline

contract = DataContract(**json.loads(
    Path("examples/vendor_settlements_contract.json").read_text()
))

result = DataShellPipeline(contract, tenant_id="analytics").run_file(
    source_path="examples/samples/vendor_settlements_1k.jsonl",
    target_path_override="./output/python/settlements.parquet",
    audit_output_path="./output/python/settlements_audit.json",
)

print(result.status.value)                 # LOADED or REJECTED
print(result.outcome.rows_delivered)       # rows actually published
print(result.ledger.footing_matched)       # must be True
print(result.audit_artifact_path)          # signed certificate path
```

The public entry points are `DataContract`, `DataShellPipeline`, `PipelineResult`,
`QualityRule`, `BatchRule`, and `ReconciliationLedger`.

### HTTP gateway

The gateway adds API-key authentication, role-based capabilities, upload limits,
rate limiting, tenant isolation, metrics, and the same enforcement engine.

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:8000/api/v1/enforce \
  -H "X-API-Key: $DATASHELL_API_KEY" \
  -F "contract_name=vendor-settlements" \
  -F "file=@examples/samples/vendor_settlements_1k.jsonl"
```

A contract must be registered for the tenant before it can be enforced. The full
route set is in [`docs/openapi.yaml`](docs/openapi.yaml). Interactive API docs are
disabled in production; health probes remain available.

### Apache Airflow

The provider exposes two operators over the same engine:

- `DataShellEnforceOperator` calls the gateway API.
- `DataShellLocalEnforceOperator` invokes the CLI on the worker, for large or
  air-gapped inputs.

Install it from the checkout and see the
[provider README](integrations/airflow/README.md):

```bash
pip install -e integrations/airflow
```

---

## Configuration

Development defaults are intentionally permissive. Production startup refuses
placeholder or weak secrets, wildcard origins, and untrusted proxy settings.

| Variable | Purpose | Default |
| --- | --- | --- |
| `ENVIRONMENT` | Runtime mode | `development` |
| `DATASHELL_API_KEY` | Master API credential; required in production | Required in production |
| `DATASHELL_HMAC_SECRET` | HMAC certificate-signing secret; required in production | Required in production |
| `DATASHELL_ED25519_PRIVATE_KEY` | Enables third-party-verifiable Ed25519 certificates | Unset (HMAC fallback) |
| `ALLOWED_ORIGINS` | Exact HTTPS browser origins; required in production | `*` in development |
| `FORWARDED_ALLOW_IPS` | Trusted proxy IPs/CIDRs; never `*` in production | `127.0.0.1` |
| `DATASHELL_STORAGE_ROOT` | Root for ledger, artifacts, and contracts | `.` |
| `DATASHELL_TMP_DIR` / `TMPDIR` | Durable scratch space for uploads and spills | System temp |
| `MAX_UPLOAD_SIZE_BYTES` | Per-request upload ceiling | 20 GiB |
| `MAX_CONCURRENT_RUNS` | Concurrent pipeline executions | `2` |
| `MAX_CONCURRENT_REQUESTS` | Concurrent admitted HTTP requests | `2` |
| `DUCKDB_MEMORY_LIMIT` | DuckDB operator memory envelope | `1GB` |
| `STREAM_AUTO_THRESHOLD_BYTES` | Size at which streaming starts | 64 MiB |
| `STREAM_CHUNK_ROWS` | Rows per streaming micro-batch | `100000` |
| `STREAM_CHUNK_CELL_BUDGET` | Maximum cells per streaming micro-batch; caps rows at budget ÷ declared columns (`0` disables) | `1000000` |
| `DRIFT_ENABLED` | Compare feeds against historical baselines | `false` |
| `DATASHELL_HISTORY_RETENTION_DAYS` | Ledger retention; `0` keeps forever | `0` |

The authoritative list — including drift thresholds, webhook settings, billing,
demo limits, CLI options, API endpoints, roles, capabilities, and certificate
fields — is
[`docs/production/CONFIGURATION_REFERENCE.md`](docs/production/CONFIGURATION_REFERENCE.md).

For production, also read the
[operations runbook](docs/production/OPERATIONS_RUNBOOK.md) and
[threat model](docs/THREAT_MODEL.md).

---

## Architecture and deployment

Data Shell is composed of:

- the **contract registry** (versioned, tenant-scoped JSON contracts);
- the **enforcement engine** (structural cast, semantic rules, reconciliation,
  atomic delivery);
- the **authenticated gateway** (optional HTTP surface with RBAC and limits);
- the **boundary ledger** (SQLite forensic history and quarantine index); and
- the **artifact store** (delivered data, immutable lineage copies, quarantine,
  certificates, and publication journals).

The reference topology is deliberately constrained:

| Component | Reference choice | Consequence |
| --- | --- | --- |
| Credential registry | Local SQLite file | Key changes do not propagate across replicas. |
| Rate limiter | Process-local token bucket | Limits are per worker process. |
| Execution semaphore | Process-local | Concurrency limits are per process. |
| Ledger | SQLite | Single-writer topology. |
| Replicas / workers | **1 / 1** | Do not scale horizontally without replacing the shared-state pieces. |

### Deployment options

| Target | How |
| --- | --- |
| **Local development** | `pip install -e ".[all]"` and run the CLI or `uvicorn datashell.server.app:app`. |
| **Docker Compose** | `cp .env.example .env`, fill in the required secrets and `ALLOWED_ORIGINS`, then `docker compose up --build`. Compose refuses placeholder values. |
| **Kubernetes** | Apply `k8s-deployment.yaml` (one replica, 120 GiB `ReadWriteOnce` PVC). |
| **Render (demo UI)** | Use `render.yaml` for the landing page and interactive playground. |
| **Render (gateway)** | Use `render-api.yaml`; Render only auto-detects `render.yaml`, so copy it into place in your fork. |

No deployment should be promoted to general production until the target-host,
capacity, backup/restore, alert-delivery, and independent-security-review gates
are closed. The current evidence and blockers are recorded in the
[release checklist](docs/production/RELEASE_CHECKLIST.md) and
[residual risk register](docs/production/RESIDUAL_RISK_REGISTER.md).

---

## Verification and evidence

`datashell verify` is the supported offline check. It recomputes every artifact
digest and the Merkle root, validates the signature envelope against a trusted key,
and reports the committed generation.

A valid certificate **proves**:

- the signed artifacts have not changed since signing;
- the signing key was in the possession of the signer; and
- reconciliation footed correctly at run time.

A valid certificate **does not prove**:

- that the input data was truthful or accurate;
- that the contract expressed the correct business rules;
- that the computation was semantically correct; or
- non-repudiation without a customer-controlled key lifecycle.

Certificates are signed with **Ed25519** when an Ed25519 key is provisioned, making
them verifiable from a public key alone. Without one, they use **HMAC-SHA256**,
which requires the verifier to hold the shared secret. The certificate states which
mode was used; key rotation is supported through keyring variables.

The boundary ledger is the system of record for run history, quarantine, usage
metering, and drift baselines. The ledger is durable but is not itself
cryptographically signed; the certificate is the signed artifact.

---

## Security

- **Authentication and authorization:** API-key authentication with four roles
  (`ADMIN`, `DATA_ENGINEER`, `PRODUCER`, `AUDITOR`) and nine capabilities across
  contracts, enforcement, audit, metrics, settlement, and billing.
- **Tenant isolation:** delivery roots are partitioned by tenant; cross-tenant
  delivery and certificate reads are denied.
- **Upload and path controls:** extension allowlisting, size ceilings, sandboxed
  artifact paths, and free-space preflight.
- **Secrets:** production mode refuses short, low-entropy, or placeholder secrets.
- **Static gates:** CI runs Ruff, strict mypy, Bandit with a zero-findings policy,
  and `pip-audit` against the lockfiles, dev lockfiles, and Python client
  requirements.
- **Container posture:** the production image runs as UID/GID `10001:10001`; CI
  exercises it with dropped capabilities and a read-only root filesystem.

There has been **no independent third-party security review** of this codebase.
That is a blocking release gate, not a footnote. The package prepared for review is
[`docs/production/SECURITY_REVIEW_PACKAGE.md`](docs/production/SECURITY_REVIEW_PACKAGE.md).

To report a vulnerability, follow [`SECURITY.md`](SECURITY.md). Do not open a public
issue for an exploitable defect.

---

## Observability

- **Health:** `GET /health` and `GET /ready` (readiness fails on storage, ledger,
  registry, or disk problems).
- **Metrics:** authenticated `GET /metrics` exposes row counts, footing errors, and
  a run-duration histogram. Tenant, contract, and rule IDs are excluded from
  labels to avoid cardinality blow-up.
- **Webhooks:** when configured, terminal runs emit a signed `run.completed` event
  with bounded retries. Delivery is best-effort and cannot fail a run.
- **Tracing:** opt-in OpenTelemetry spans via `datashell[otel]` and
  `OTEL_EXPORTER_OTLP_ENDPOINT`.
- **Ledger queries:** CLI and HTTP surfaces expose run history, quarantine search,
  usage, and drift trends, always tenant-scoped.

The OpenTelemetry activation path is verified end to end in CI: the
`otel-collector-e2e` job starts a real OpenTelemetry collector, drives a
gateway exchange, and asserts the exported span arrives at the collector over
OTLP/gRPC. Reproduce it locally with
[`scripts/run_otel_collector_e2e.sh`](scripts/run_otel_collector_e2e.sh).

---

## Performance and capacity

Data Shell has two ingest backends and one delivered-artifact codec. The backend is
selected by explicit request, deployment policy, or source size; both backends
produce identical decisions, ledger entries, and certificate claims.

Reference-machine observations (single node, 13 GB RAM):

| Workload | Observation |
| --- | --- |
| In-memory, 1M rows | ~3.3M rows/s, ~0.7–0.8 GB peak RSS |
| Streaming, 5M rows of Parquet | ~1–2 s, ~1 GB peak RSS |
| Streaming, 5M rows of CSV | ~30 s, ~3 GB peak RSS |
| Streaming, 1 GiB Parquet, 100 columns × 32 B strings | ~105 s, ~2.1 GB peak RSS |
| Streaming, 1 GiB CSV, 100 columns × 32 B strings | ~62 s, ~1.1 GB peak RSS |

The CSV cost is intentional: CSV columns arrive as strings, so the strict per-row
caster runs to preserve exact rejection diagnostics. Convert very large CSV feeds to
Parquet where possible. **These are observations, not capacity guarantees.** Memory
grows with rows processed; the streaming path is not constant-memory.

Wide-schema feeds are now measured, not inferred. At the 1 GiB cap, a 100-column
schema with 32-byte strings completed in both formats with matched footing and zero
unaccounted rows. Peak RSS was ~1.1 GiB for CSV and ~2.1 GiB for Parquet, so budget
at least 3 GiB of free RAM per active wide-schema Parquet run. The effective
micro-batch is capped by `STREAM_CHUNK_CELL_BUDGET` (1,000,000 cells by default —
10,000 rows for a 100-column feed), even when `STREAM_CHUNK_ROWS` is larger.
See the [benchmark procedure](docs/production/BENCHMARK_PROCEDURE.md#4-procedure-b--large-file-capacity-the-1-gib-cap)
for the raw measurements and caveats.

Measure your own envelope before sizing a host:

```bash
python benchmarks/streaming_profile.py --rows 5000000 --format parquet
python benchmarks/streaming_profile.py --rows 5000000 --format csv
python benchmarks/volumetric_stress_test.py
python benchmarks/api_latency_load_test.py
datashell benchmark --rows 1000000 --json
```

The defaults include a 20 GiB upload ceiling, a 64 MiB streaming threshold,
DuckDB memory limits, concurrency limits, and a free-space preflight. Do not infer
that a 20 GiB upload fits in memory. See [`docs/SCALING.md`](docs/SCALING.md) for
the five scaling surfaces and worked deployment profiles.

---

## Development and testing

```bash
# Reproducible development environment
pip install --require-hashes -r requirements-dev.lock
pip install -e ".[dev]"

# Tests
ENVIRONMENT=test pytest -q
ENVIRONMENT=test pytest integrations/airflow/tests -q

# End-to-end OpenTelemetry export (needs docker and the otel extra)
pip install --require-hashes -r requirements-otel.lock
scripts/run_otel_collector_e2e.sh

# Static gates
ruff check .
mypy datashell/
bandit -r datashell -q
pip-audit -r requirements.lock -r requirements-dev.lock -r requirements-otel.lock
```

CI runs the suite on Python 3.11, 3.12, and 3.13, verifies the committed OpenAPI
document, builds the wheel and sdist, checks distribution contents, generates a
CycloneDX SBOM, exports gateway spans to a real OpenTelemetry collector, and
builds and exercises the production container with a read-only root filesystem.
See [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

The Airflow provider has its own test suite under
[`integrations/airflow/tests`](integrations/airflow/tests). The main test suite
currently contains 528 tests; the provider suite contains 9.

If you change the OpenAPI surface, regenerate the committed document with
`python scripts/export_openapi.py --out docs/openapi.yaml`. If you change release
metadata, run `python scripts/check_release_metadata.py`.

---

## Project status and limitations

**Release posture:** controlled-pilot candidate at v2.0.0. Not approved for general
production.

| Area | Status |
| --- | --- |
| Enforcement, reconciliation, publication, recovery, certificates | Implemented and covered by the automated suite. |
| Topology | Single process, single worker, single replica. |
| Horizontal scale | Not established. Shared credential store, distributed rate limiting, and shared execution coordination are prerequisites. |
| Independent security review | **Outstanding** — blocking general production. |
| Target-host deployment verification | **Outstanding** — blocking general production. |
| Production capacity qualification | **Outstanding** — benchmark on the pilot host and set `MAX_UPLOAD_SIZE_BYTES` from the result. |
| Production backup/restore (RPO/RTO) | **Outstanding** — the local drill is not a production drill. |
| Alert delivery to a human | **Outstanding** — rule definitions exist; delivery has not been exercised on the target. |
| Live Stripe billing | Not exercised. The adapter is draft-invoice-only; no code path moves money. |
| OpenTelemetry collector | Export verified end to end against a real collector in CI (`otel-collector-e2e`). Forwarding to a production tracing backend is an operator concern. |
| PyPI distribution | Not published under this name; install from source. |
| Wide-schema capacity | Measured for 100-column, 32-byte-string feeds at 1 GiB (CSV and Parquet). Wider schemas, wider cell values, and larger wide files remain unmeasured. |
| Consumer contract | A consumer that reads the delivery target by path without resolving `<target>.generation.json` can observe a superseded generation. |

The full blocker list, owners, and evidence status are maintained in
[`docs/production/RELEASE_CHECKLIST.md`](docs/production/RELEASE_CHECKLIST.md) and
[`docs/production/RESIDUAL_RISK_REGISTER.md`](docs/production/RESIDUAL_RISK_REGISTER.md).

---

## Documentation

| Document | Purpose |
| --- | --- |
| [`docs/FIRST_RUN.md`](docs/FIRST_RUN.md) | Five-minute guided first run with the sample feed. |
| [`TUTORIAL.md`](TUTORIAL.md) | End-to-end tutorial from contract authoring to verification. |
| [`docs/EVALUATION.md`](docs/EVALUATION.md) | Adoption path, integration patterns, pilot checklist, and known limitations. |
| [`docs/production/CONFIGURATION_REFERENCE.md`](docs/production/CONFIGURATION_REFERENCE.md) | Authoritative environment, CLI, API, role, and certificate reference. |
| [`docs/production/OPERATIONS_RUNBOOK.md`](docs/production/OPERATIONS_RUNBOOK.md) | Startup, health checks, backup/restore, monitoring, and rollback. |
| [`docs/production/GENERATION_CONTRACT.md`](docs/production/GENERATION_CONTRACT.md) | The publication commit point and reader contract. |
| [`docs/production/RELEASE_CHECKLIST.md`](docs/production/RELEASE_CHECKLIST.md) | Release gates, evidence, and remaining blockers. |
| [`docs/production/DEPLOYMENT_VERIFICATION.md`](docs/production/DEPLOYMENT_VERIFICATION.md) | Executable verification procedure for a target environment. |
| [`docs/production/INCIDENT_RESPONSE.md`](docs/production/INCIDENT_RESPONSE.md) | Incident runbooks for publication, ledger, disk, tampering, and credential failures. |
| [`docs/SCALING.md`](docs/SCALING.md) | Ingest, batch, codec, concurrency, and disk scaling surfaces. |
| [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) | Assets, trust boundaries, threats, and mitigations. |
| [`docs/SDK.md`](docs/SDK.md) | Generated Python/TypeScript clients and OpenAPI versioning. |
| [`docs/SETTLEMENT.md`](docs/SETTLEMENT.md) | Settlement architecture, schemas, acceptance, and invariants. |
| [`docs/SETTLEMENT_OPERATIONS.md`](docs/SETTLEMENT_OPERATIONS.md) | Settlement CLI/API reference, billing, and disputes. |
| [`examples/README.md`](examples/README.md) | Example contracts, sample data, scripts, and notebook. |
| [`docs/README.md`](docs/README.md) | Full documentation index. |

---

## Contributing

Contributions are welcome. Start with [`CONTRIBUTING.md`](CONTRIBUTING.md) and the
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). In short:

1. Open an issue before a large change, especially one that affects the contract
   schema, certificate format, public API, or single-replica constraint.
2. Add tests for new behavior and run the full suite plus the static gates above.
3. Update the relevant documentation and the committed OpenAPI document.
4. Keep the security posture explicit: do not weaken a gate to make a test pass.

Security issues should follow [`SECURITY.md`](SECURITY.md), not the public issue
tracker.

---

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
