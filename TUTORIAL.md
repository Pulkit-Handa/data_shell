# Data Shell Tutorial

This tutorial walks you through using Data Shell from contract authoring to enforcement, verification, and ledger queries. You'll run everything locally in under 10 minutes.

## Prerequisites

- Python 3.11+ installed
- Data Shell installed: `pip install -e .` (from repo root)

## 1. Validate a Contract

Start by validating an existing contract:

```bash
datashell validate-contract -c examples/vendor_settlements_contract.json
```

You should see validation pass with rule counts.

## 2. Try the 60-Second Demo

The fastest way to see the full workflow:

```bash
datashell demo
```

This generates synthetic data, enforces the contract, shows refusals/holds, verifies the certificate, demonstrates tampering detection, and restores authenticity. Add `--json` for machine-readable output.

## 3. Enforce Against a Sample Dataset

Use the provided sample dataset:

```bash
# From repo root
mkdir -p output/curated output/audit
datashell run \
  --contract examples/vendor_settlements_contract.json \
  --source examples/samples/vendor_settlements_1k.jsonl \
  --target output/curated/settlements.parquet \
  --audit output/audit/settlements_certificate.json
```

Expected outcome (matches demo distribution):
- 40 structural refusals, 60 held (quality), 900 delivered
- Exit code 0 (delivered). Exit code 2 means policy rejection.

## 4. Verify the Signed Certificate

Independently verify the certificate and its artifacts:

```bash
datashell verify output/audit/settlements_certificate.json
```

You should see `AUTHENTIC_AND_UNMODIFIED`. The certificate includes hashes of delivered and quarantine artifacts (by path and hash).

## 5. Query the Durable Boundary Ledger

Every run is recorded in the forensic ledger:

```bash
# Summary for default tenant
datashell ledger summary

# List recent runs
datashell ledger runs --limit 5

# Show full details for the most recent run
datashell ledger runs --json | python -m json.tool | head -5  # get run_id
datashell ledger show <run_id>

# Search quarantine index
datashell ledger quarantine --reason amount

# Usage metering
datashell ledger usage

# Volume trend
datashell ledger trend --limit 10
```

## 6. Airflow Integration (Optional)

For orchestrated pipelines, use the Airflow provider:

```bash
# Install provider (in Airflow environment)
pip install -e integrations/airflow

# Use DataShellLocalEnforceOperator for multi-GB inputs on workers
# or DataShellEnforceOperator to call a remote gateway
```

See `integrations/airflow/README.md` for details.

## 7. Next Steps

- Customize `examples/vendor_settlements_contract.json` for your schema
- Configure production settings per `docs/production/OPERATIONS_RUNBOOK.md` and
  `docs/production/CONFIGURATION_REFERENCE.md`
- Review `docs/THREAT_MODEL.md` and `docs/production/RELEASE_CHECKLIST.md` before deploying
- Deploy with Render (`render.yaml` for the demo UI, `render-api.yaml` for the gateway)
