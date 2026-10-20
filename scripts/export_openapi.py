"""Export the gateway's live OpenAPI schema to ``docs/openapi.yaml``.

The committed document is the reviewable contract of the public API: runs in
CI regenerate it and fail when a code change alters routes, payloads, or
summaries without the matching review artifact. Dry-run it locally before a
release: ``python scripts/export_openapi.py``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from datashell.server.app import app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="docs/openapi.yaml",
        help="destination file for the exported schema (default: docs/openapi.yaml)")
    args = parser.parse_args()

    schema = app.openapi()
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "w", encoding="utf-8") as handle:
        yaml.dump(schema, handle, sort_keys=True, default_flow_style=False, allow_unicode=True)
    print(f"Exported {len(schema['paths'])} paths to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
