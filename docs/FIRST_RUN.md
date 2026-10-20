# Data Shell — First Run Guide

This guide takes you from a clean checkout to a verified delivery in about five
minutes, using the bundled sample dataset. No external services or credentials
are required for local development.

> **Data Shell is a write gate, not a report.** Records that fail the contract
> are *never* published to the delivery target. A run either publishes every
> surviving row atomically, or nothing at all.

---

## Prerequisites

- Python 3.11 or newer
- The repository checked out locally

```bash
pip install -e .        # or: pip install datashell, or datashell[all] for every extra
```

---

## 1. Five-minute quickstart

### Step 1 — Explore the contract and sample data

The sample contract validates vendor settlement feeds: every row must have a
non-null ID, a positive amount, an allowed currency, and the batch must contain
at least three surviving records.

```bash
cat examples/vendor_settlements_contract.json
head -3 examples/samples/vendor_settlements_1k.jsonl
```

The 1,000-row sample contains 900 valid rows and 100 seeded defects
(20 unparseable amounts, 20 null IDs, 35 negative amounts, 25 bad currencies).

### Step 2 — Validate the contract before running it

```bash
datashell validate-contract -c examples/vendor_settlements_contract.json
```

Expected output:

```text
✓ Contract 'vendor-settlements' v1.0.0 is valid. 3 row rules, 1 batch rule, 1 delivery target.
```

If the contract is invalid you get one line per problem (missing schema,
unknown rule column, duplicate rule ID, target outside the sandbox) instead of
a runtime surprise.

### Step 3 — Run enforcement

```bash
datashell run \
  --contract examples/vendor_settlements_contract.json \
  --source   examples/samples/vendor_settlements_1k.jsonl \
  --target   /tmp/settlements.parquet \
  --audit    /tmp/settlements_audit.json
```

Expected terminal output (abridged):

```text
┌─────────────────────────────────────────────────────────────┐
│           DATA SHELL — RUN RECONCILIATION LEDGER            │
├─────────────────────────────────────────────────────────────┤
│ Rows read from source:                                1,000 │
│ Refused at the door · structural:        -               40 │
│ Held by quality rules:                   -               60 │
│ Rows delivered:                                         900 │
├─────────────────────────────────────────────────────────────┤
│ Unaccounted for:                                0 · Matched │
└─────────────────────────────────────────────────────────────┘

Terminal Status: LOADED
Delivered rows:  900

OK Data was published to: /tmp/settlements.parquet
```

Exit codes: `0` delivered, `2` rejected by contract policy, `1` engine failure.

### Step 4 — Verify the audit certificate

```bash
datashell verify /tmp/settlements_audit.json
```

Expected:

```text
✓ Certificate verification: AUTHENTIC_AND_UNMODIFIED
```

Tamper with one byte of the delivered Parquet and run `verify` again — the
certificate reports `TAMPERED_UNDERLYING_ARTIFACTS` and exits with code 3.

### Step 5 — Inspect the boundary ledger

```bash
datashell ledger runs --contract vendor-settlements
datashell ledger summary --contract vendor-settlements
```

The ledger is the durable system of record: every run (delivered **and**
rejected), its row counts, rule breakdown, quarantine artifacts, and usage.

---

## 2. What just happened — the four stages

Every run, whether in-memory, file-backed, or out-of-core streaming, passes
through the same four stages:

| Stage | Name | What it does | Where failures go |
|-------|------|--------------|-------------------|
| 1 | **At the door** (structural) | Casts and validates every record against the declared schema. Unrealizable rows are ejected. | Structural refusal ledger + quarantine |
| 2 | **Inside** (semantic) | Evaluates row rules in Polars and batch rules in DuckDB (min row count, uniqueness, control totals, SQL assertions). | Held rows + quarantine, attributed to a `rule_id` |
| 3 | **Reconciliation** | Asserts `Rows Read = Refused + Held + Delivered` with exactly zero unaccounted rows. | If the arithmetic does not foot, delivery refuses to write |
| 4 | **Atomic publish** | Stages the target, immutable lineage copy, and signed certificate, then commits them together with a write-ahead journal. | Nothing is partially published |

The certificate binds the source digest, delivered digest, quarantine digest,
and contract hash into a Merkle root, then signs it (HMAC-SHA256 by default,
Ed25519 when provisioned).

---

## 3. How to read the reconciliation ledger

```text
│ Rows read from source:                   1,000 │
│ Refused at the door · structural:        -  40 │
│ Held by quality rules:                   -  60 │
│ Rows delivered:                            900 │
│ Unaccounted for:                    0 · Matched │
```

- **Rows read** — every record parsed from the source.
- **Refused (structural)** — rows that could not be realized as the declared
  schema (wrong type, missing required column, unparseable value). They are
  written to a quarantine JSONL with row index and reason.
- **Held (semantic)** — rows that parsed but violated a row rule. They are
  written to a quarantine Parquet and attributed to the first failing
  `rule_id`.
- **Delivered** — admitted rows, published only when the contract gate passes.
- **Unaccounted for** — must be `0 · Matched`. Any other value is a bug; the
  engine refuses to publish.

In a rejection, `rows delivered` is `0 (REJECTED: N held back)`: the admitted
rows existed but the whole-run gate (batch rule, `REJECT_RUN` behavior, or
drift) withheld them. The CLI prints the exact rule and numbers, for example:

```text
[!] FEED REJECTED: not-partial batch rule failed (actual: 2 rows, expected: >= 3)

  Batch rules:
    - not-partial: FAILED (actual=2, expected=>= 3)

  Run with --json for full details.
```

Use `--json` for a machine-readable summary including `batch_evaluations`,
`drift_evaluations`, per-rule counts, and the rejection reason.

---

## 4. Authoring a simple contract

Copy the example and modify it. A minimal contract needs a metadata block, one
source with a schema, quality rules, and a delivery target:

```bash
cp examples/vendor_settlements_contract.json /tmp/my_contract.json
```

```json
{
  "apiVersion": "daas/v1",
  "kind": "DataProduct",
  "metadata": { "name": "my-feed", "version": "1.0.0" },
  "spec": {
    "sources": [{
      "type": "LOCAL",
      "path": "incoming/",
      "format": "jsonl",
      "schema": [
        { "name": "order_id", "polars_type": "Utf8",  "nullable": false },
        { "name": "amount",   "polars_type": "Float64", "nullable": false }
      ]
    }],
    "quality": {
      "behavior": "DROP_ROW",
      "rules": [
        { "rule_id": "id-present",      "check": "NOT_NULL",     "column": "order_id" },
        { "rule_id": "amount-positive", "check": "GREATER_THAN", "column": "amount", "value": 0 }
      ],
      "batch_rules": [
        { "rule_id": "min-volume", "check": "MIN_ROW_COUNT", "value": 100 }
      ]
    },
    "delivery": {
      "targets": [{
        "type": "PARQUET",
        "path": "./output/curated/my_feed.parquet",
        "mode": "overwrite"
      }]
    }
  }
}
```

Rules of thumb:

- `behavior: DROP_ROW` — withhold bad rows, deliver good ones (unless a batch
  rule fails).
- `behavior: REJECT_RUN` — any row or batch violation withholds the whole feed.
- `behavior: WARN` — record violations in the certificate but deliver
  everything; useful during a rollout.
- Always validate after editing: `datashell validate-contract -c <file>`.

---

## 5. How to verify a certificate

```bash
datashell verify /tmp/settlements_audit.json          # human output
datashell verify /tmp/settlements_audit.json --json   # full report
```

`verify` re-hashes the source, delivered artifact, lineage copy, and quarantine
artifacts named in the certificate, then checks the signature.

| Verdict | Meaning |
|---------|---------|
| `AUTHENTIC_AND_UNMODIFIED` | Every artifact matches its recorded hash and the signature is trusted |
| `TAMPERED_UNDERLYING_ARTIFACTS` | At least one on-disk artifact differs from the certificate |
| `MISSING_ARTIFACTS` | An artifact named by the certificate no longer exists |
| `SIGNATURE_INVALID` / `UNTRUSTED_KEY` | The signature does not verify under a trusted key |

For third-party verification, provision an Ed25519 key pair: set
`DATASHELL_ED25519_PRIVATE_KEY`, publish the derived public key, and verifiers
only need the public key — no shared secret.

---

## 6. Common pitfalls

### Drift

Historical drift comparison is **opt-in and disabled by default**
(`DRIFT_ENABLED=false`). This is deliberate: a small test run recorded early
would otherwise become the baseline that rejects a normal production delivery.

- Enable it once a feed has a stable history: `DRIFT_ENABLED=true`.
- Disable it for one run: `datashell run ... --no-drift`.
- Re-baseline after an intentional volume/scope change:
  `DRIFT_ENABLED=true DRIFT_BASELINE_RESET=true datashell run ...` (use once).
- Tune thresholds with `DRIFT_MAX_VOLUME_DROP_PCT`,
  `DRIFT_MAX_METRIC_DRIFT_PCT`, `DRIFT_MAX_RATE_ABS_DELTA`, and
  `DRIFT_BASELINE_WINDOW`.

When a drift check fails, the CLI names each failing metric with its current
value, baseline, and threshold.

### Schema mismatches

`SchemaCompatibilityPolicy.EVOLVING` (default) back-fills missing *nullable*
columns with null; `STRICT` refuses rows with missing declared columns. If a
whole feed is refused, compare the source header/schema against the contract
with `validate-contract` first, then check the quarantine JSONL for
`failed_column` and `expected_type`.

### Sandbox constraints

Delivery targets, quarantine paths, and staging directories must stay inside
approved roots:

- In development: the tenant delivery directory (`output/tenants/<tenant>/`),
  the output directory, and the system temp directory.
- In production: only the tenant delivery directory. System temp, `output/`
  internals, databases, and engine artifact names (`.staged_*`, `*_audit.json`,
  ...) are rejected.

If a run fails with `PathSecurityViolation`, the message names the offending
path and the approved roots. Relative target paths resolve under the tenant
delivery directory; use an explicit tenant (`--tenant`) consistently between
writes and reads.

### Batch rules count admitted rows only

`MIN_ROW_COUNT`, control totals, and uniqueness checks run over the rows that
survived row rules — not the raw input. A feed of 1,000 rows with 60 held and
40 refused has a batch count of 900.

### One replica

The credential registry and rate limiter are process-local. Run a single
worker until those are moved to shared infrastructure.

---

## Where to next

- [README](../README.md) — architecture, HTTP API, deployment.
- [TUTORIAL](../TUTORIAL.md) — a longer narrative walkthrough.
- [docs/production/OPERATIONS_RUNBOOK.md](production/OPERATIONS_RUNBOOK.md) — runbooks, retention, key rotation.
- [docs/THREAT_MODEL.md](THREAT_MODEL.md) — trust boundaries and assets.
- [docs/archive/PRODUCT_VALIDATION.md](archive/PRODUCT_VALIDATION.md) — prior audit and
  benchmark/claim verification notes (historical reference).
