# Evaluating Data Shell

**Audience:** the engineer who will run the evaluation, and the technical
approver who reviews it.

**Product status at 2.0.1:** controlled-pilot candidate. The enforcement
engine, reconciliation, publication, recovery, and certificate stack are
implemented and covered by 552 automated tests (543 in the main suite plus 9
Airflow provider tests). The reference topology is a single replica with process-local
limits. Horizontal scale and shared credential stores are documented as
prerequisites, not features (see [Known limitations](#known-limitations-and-open-items)).
Nothing here is a certification claim.

---

## The adoption path

| Stage | Goal | Artifact |
| --- | --- | --- |
| 1. Understand | What the boundary enforces and where it fits | Landing page, README |
| 2. Verify | Inspect the demo, harnesses, evidence methodology | Interactive demo, `benchmarks/` |
| 3. Evaluate | Run the quickstart on your machine | This document, § Quickstart |
| 4. Integrate | Wire the boundary into one existing job | § Integration patterns |
| 5. Pilot | Run a narrowly scoped pilot with acceptance criteria | § Pilot checklist |
| 6. Expand | Additional workflows only after the pilot passes | § Decision criteria |

---

## Stage 3: Evaluate — quickstart

Prerequisites: Python 3.11–3.13. The core engine needs only `polars`,
`pyarrow`, `pydantic`, `cryptography`, and `prometheus-client`; the gateway,
DuckDB batch rules, billing adapter, and OpenTelemetry are extras.

```bash
pip install "datashell-engine[all]"   # from PyPI (the import package is `datashell`)
datashell doctor                     # reports which capabilities this environment has
```

Or, from a source checkout:

```bash
git clone https://github.com/Pulkit-Handa/data_shell
cd data_shell
pip install -e ".[all]"
datashell doctor
```

### 1. The self-contained walkthrough (~2 seconds)

```bash
datashell demo
```

A clean feed with 100 seeded defects: 40 rows refused at the structural tier
(unparseable amounts, null IDs), 60 held by quality rules (negative amounts,
disallowed currency), 900 delivered. Then the run tampers one byte in the
delivered Parquet, the signed certificate detects it, the authentic bytes are
restored, and verification recovers. Every command below was run to produce
the values quoted here.

### 2. The clean-feed counterpoint

```bash
datashell demo --scenario valid
```

Same contract, clean feed: 900 read, 900 delivered, zero unaccounted.

### 3. Your first real run — the repository's own contract and sample

`examples/samples/vendor_settlements_1k.jsonl` deliberately contains the same
100 defects as the walkthrough, so the first real run demonstrates quarantine
on files you own:

```bash
datashell run \
  --contract examples/vendor_settlements_contract.json \
  --source   examples/samples/vendor_settlements_1k.jsonl \
  --target   ./output/eval/settlements.parquet \
  --audit    ./output/eval/settlements_audit.json \
  --tenant   eval-demo \
  --json
```

Expected: exit `0`, `LOADED`, `1000 read = 40 refused + 60 held + 900
delivered`, `unaccounted_for: 0`, `footing_matched: true`. The held and
refused rows land in `output/quarantine/` under the run id.

> Target and audit paths must share a storage root — the engine refuses to
> deliver otherwise. The layout above keeps both under `./output/`.

### 4. Reproduce a whole-batch refusal

Change one line in the contract copy — `"behavior": "DROP_ROW"` →
`"REJECT_RUN"` — and re-run:

```bash
python - <<'PY'
import json
from pathlib import Path
c = json.loads(Path("examples/vendor_settlements_contract.json").read_text())
c["spec"]["quality"]["behavior"] = "REJECT_RUN"
Path("output/eval/strict_contract.json").write_text(json.dumps(c))
PY

datashell run \
  --contract output/eval/strict_contract.json \
  --source   examples/samples/vendor_settlements_1k.jsonl \
  --target   ./output/eval/strict.parquet \
  --audit    ./output/eval/strict_audit.json \
  --tenant   eval-demo --json
echo "exit: $?"     # -> 2
```

Expected: exit `2`, `REJECTED`, `rows_delivered: 0`, a `rejection_reason`,
**no target file written**, and the quarantine evidence still on disk. The
exit code is the machine-readable verdict: `0` delivered, `2` rejected by
policy, `1` engine failure.

### 5. Inspect the evidence

```bash
datashell verify ./output/eval/settlements_audit.json
# -> AUTHENTIC_AND_UNMODIFIED; signature valid; every artifact hash verified

datashell ledger summary --tenant eval-demo
datashell ledger quarantine --tenant eval-demo --limit 5
```

The certificate binds contract, source bytes, delivered bytes, and every
quarantine file by content hash under a Merkle root. `verify` recomputes all
of it offline — no service call, no trust in the reporter.

### 6. Clean up

```bash
rm -rf output/eval output/quarantine
datashell ledger prune --days 1      # optional: trim ledger rows
```

---

## Stage 4: Integrate — where the boundary sits in your workflow

Data Shell **does not replace** your orchestrator, storage, or alerting. It
sits between an incoming feed and the write you currently perform, and owns
one decision: *does this batch cross the boundary, and with what evidence?*

```
external producer
      │  feed (CSV / JSONL / Parquet)
      ▼
your existing landing / orchestration (storage, Airflow, ...)
      │  ① call the boundary instead of copying straight through
      ▼
┌─────────────────────────── DATA SHELL ───────────────────────────┐
│ strict cast → row + batch rules → footing → atomic publish       │
└───────┬───────────────────────────────────────┬─────────────────┘
        │ exit 0: delivered parquet +           │ exit 2: zero rows
        │ signed certificate + ledger entry     │ delivered, quarantine
        ▼                                       ▼ evidence + rejection reason
   downstream consumer                 your existing alerting / ticketing
```

- **Retries and alerts stay yours.** Data Shell returns an exit code and a
  JSON summary; your scheduler decides what to do with them. The Airflow
  provider (`pip install datashell-airflow`) is a thin operator wrapper over
  the same CLI.
- **The gateway is optional.** If you prefer HTTP, the authenticated gateway
  (`datashell-engine[server]`) exposes the same engine with RBAC, rate limits, and
  a 20 GiB request ceiling; the generated Python/TypeScript clients track its
  OpenAPI contract.
- **What the downstream consumer receives:** the delivered artifact only —
  the boundary never writes partial batches. Quarantine and the certificate
  are evidence, not inputs to your pipeline.
- **Bypassing during evaluation:** point `--source`/`--target` back at the
  legacy path. The CLI is stateless apart from the durable ledger and demo
  quota; nothing about adopting it changes your storage layout.

---

## Stage 5: Pilot checklist

A pilot is one narrowly scoped workflow, not a platform migration.

| Item | Requirement |
| --- | --- |
| Scope | One named feed, one contract version, one tenant. No other pipelines touched. |
| Named operator | One engineer owns the runbook, the quarantine path, and the ledger queries. |
| Acceptance criteria | Measurable before you start: e.g. "zero published rows with footing mismatch over N runs", "quarantine index answers 'which rule held row X' in one query", "verify passes on every delivered artifact". |
| Rollback conditions | Agreed in advance: repeated false holds blocking the feed, unexplained engine exits, evidence that cannot be verified offline. |
| Evaluation window | Two scheduled feed cycles minimum (one normal, one anomalous if you can stage one safely). |
| Decision criteria | Continue only if the acceptance criteria are met **and** the investigation-time baseline moves. Otherwise document what failed and why. |

---

## Prerequisites and operational responsibilities

- **Secrets:** the default certificate signer is HMAC-SHA256 (symmetric — the
  verifier needs the key). Provision an Ed25519 key to make certificates
  third-party verifiable from the public key alone. Startup refuses weak or
  seeded secrets in production mode.
- **Storage layout:** delivery targets and audit certificates must share a
  storage root; artifact paths are sandboxed before use.
- **Limits are process-local:** rate limiting and execution concurrency are
  per worker process, per replica. Size accordingly before running multiple
  replicas; horizontal scale needs shared credential/rate-limit stores first.
- **Telemetry:** OpenTelemetry spans are opt-in (`datashell-engine[otel]` plus
  `OTEL_EXPORTER_OTLP_ENDPOINT`). Without it, the app is silent. The export
  path is proven against a real collector by
  `scripts/run_otel_collector_e2e.sh`.
- **Settlement scope:** the settlement ledger calculates obligations and
  records draft billing instructions. It never moves money.

---

## Known limitations and open items

- Horizontal scale beyond the single-replica reference topology: not
  established.
- OpenTelemetry export is verified end to end against a real collector in CI
  (`otel-collector-e2e`): the gateway's spans must arrive over OTLP/gRPC. What
  is *not* covered is the onward path — exporting to a production tracing
  backend (Tempo, Jaeger, Elastic) and operating it remains an operator
  responsibility.
- Billing adapter: draft billing instructions only; live payment provider
  integration is out of scope.
- The volumetric and streaming benchmarks are reproducible, but they measure
  this checkout's dependency versions — treat them as methodology, not a
  guarantee for your environment. Measure before capacity planning.

---

Next steps: [README](../README.md) ·
[Scaling surfaces](SCALING.md) · [Threat model](THREAT_MODEL.md) ·
[Operations runbook](production/OPERATIONS_RUNBOOK.md) ·
[Release checklist](production/RELEASE_CHECKLIST.md)
