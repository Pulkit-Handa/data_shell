# Sample Data

This directory contains small sample datasets you can use to test Data Shell without generating synthetic data.

## vendor_settlements_1k.jsonl

A 1,000-row sample matching the `examples/vendor_settlements_contract.json` schema with the same seeded defect distribution used by `datashell demo`:

- **Valid rows:** 900 (USD/EUR, positive amounts)
- **Defects (100 total):** 20 unparseable amounts (`amount` as string), 20 null `settlement_id`, 35 negative amounts, 25 disallowed currency (`JPY`)

These defects produce:
- **40 structural refusals** (unparseable amounts + null IDs)
- **60 held (quality)** (negative amounts + disallowed currency)
- **900 delivered**

## Usage

```bash
# Enforce against the sample
datashell run \
  --contract examples/vendor_settlements_contract.json \
  --source examples/samples/vendor_settlements_1k.jsonl \
  --target ./output/curated/settlements.parquet \
  --audit ./output/audit/settlements_certificate.json

# Verify the certificate
datashell verify ./output/audit/settlements_certificate.json
```
