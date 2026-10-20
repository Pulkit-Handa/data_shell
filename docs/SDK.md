# Client SDKs

The committed clients under `sdks/` are generated from `docs/openapi.yaml` —
the same document CI keeps in sync with the running gateway. All of them speak
the gateway's one authentication scheme (`X-API-Key` header) and the same
tenant-scoped semantics the HTTP API defines: nothing in the client layer
trusts the caller beyond what the gateway already enforces.

| Directory            | Language   | Generator (pinned)                  | HTTP stack    |
|----------------------|------------|-------------------------------------|---------------|
| `sdks/python-client`  | Python 3.9+ | `openapitools/openapi-generator-cli:v7.12.0`, generator `python` | `urllib3` |
| `sdks/typescript-client` | ECMAScript 6 | same image, generator `typescript-fetch` | browser/node `fetch` |

## Regenerating

```bash
bash scripts/generate_sdks.sh
```

The script requires Docker (the only external prereq) and the committed
`docs/openapi.yaml`. It is deliberately re-runnable: it wipes and rewrites
each client directory, so a rerun after a schema change yields a clean diff
and CI-visible drift.

## Python client

```python
from datashell_client import ApiClient, Configuration
from datashell_client.api.default_api import DefaultApi

configuration = Configuration(host="https://gateway.internal:8443")
configuration.api_key["X-API-Key"] = "dm_live_..."
api = DefaultApi(ApiClient(configuration=configuration))

runs = api.list_ledger_runs_api_v1_runs_get(limit=10)
```

Install locally without touching your site-packages:

```bash
pip install --target .venv_sdk_deps sdks/python-client
PYTHONPATH=.venv_sdk_deps python -c "import datashell_client; print('ok')"
```

## TypeScript client

```ts
import { FederationPaymentConfiguration, DefaultApi } from '@datashell/client';
// generated from the same schema; see sdks/typescript-client/README.md
```

Validate the generated code without a local toolchain:

```bash
docker run --rm -v "$PWD:/workspace:z" -w /workspace/sdks/typescript-client \
  node:20-bookworm-slim sh -c "npm ci && npx tsc -p tsconfig.json --noEmit"
```

## Versioning truth table

The SDKs track the gateway API contract, not the wheel version. After a
change to any endpoint shape:

1. `scripts/export_openapi.py` refreshes `docs/openapi.yaml` (CI verifies sync),
2. `scripts/generate_sdks.sh` regenerates the clients,
3. the SDK diff and the gateway diff land in one commit,
4. TypeScript clients get a semver bump (`npmVersion` in the script), Python
   packages a matching one.

## Why generated (not hand-written) clients

Hand-written clients drift exactly when they get used. Generated code from the
CI-verified schema cannot disagree with the gateway about paths or payloads by
construction, and the generator commits make endpoint changes reviewable as
mechanical text.
