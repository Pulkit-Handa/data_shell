#!/usr/bin/env bash
#
# Data Shell — 60-second boundary-enforcement walkthrough.
#
# Bad rows are refused, good rows land, and any post-delivery tampering is
# provably detected by the signed audit certificate.
#
# Prerequisite: the CLI is installed (run `pip install -e .` from the repo root,
# or `pip install data-shell`).
#
#   ./examples/quickstart.sh
#
set -euo pipefail

if ! command -v datashell >/dev/null 2>&1; then
  echo "The 'datashell' CLI is not on PATH. Install it first:" >&2
  echo "    pip install -e .          # from the repository root" >&2
  exit 1
fi

echo "Data Shell $(datashell --version | awk '{print $3}')"
echo

# 1. The self-contained demo: seeded defects, refusal, atomic publish, tamper.
datashell demo

echo
echo "--- Durable boundary ledger (forensic system of record) ---"
datashell ledger summary
