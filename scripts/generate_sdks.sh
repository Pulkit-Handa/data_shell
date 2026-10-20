#!/usr/bin/env bash
# Regenerate the shipped client SDKs from docs/openapi.yaml.
#
# The generator runs inside the pinned hermetic image (via Docker), so no host
# JDK/npm state can skew the generated code. Output is committed as-is —
# generated lines are honest, reviewable artifacts of the committed schema.
#
#   bash scripts/generate_sdks.sh
#
# Validation (run after generation):
#   - Python client: import smoke against datashell_client (docs/SDK.md)
#   - TypeScript client: tsc --noEmit against the generated tsconfig
set -euo pipefail

version="v7.12.0"
image="openapitools/openapi-generator-cli:${version}"
spec="docs/openapi.yaml"

command -v docker >/dev/null || { echo "ERROR: docker is required to run ${image}" >&2; exit 2; }
[ -f "${spec}" ] || { echo "ERROR: ${spec} not found — run scripts/export_openapi.py first" >&2; exit 2; }

docker pull -q "${image}"

run() { docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "${PWD}:/workspace:z" "${image}" "$@"; }

echo "Generating the Python (urllib3) client..."
rm -rf sdks/python-client
run generate \
  -i "/workspace/${spec}" -g python \
  -o /workspace/sdks/python-client \
  --package-name datashell_client \
  --additional-properties=projectName=datashell-client,library=urllib3
rm -f sdks/python-client/git_push.sh sdks/python-client/.travis.yml

echo "Generating the TypeScript (fetch) client..."
rm -rf sdks/typescript-client
run generate \
  -i "/workspace/${spec}" -g typescript-fetch \
  -o /workspace/sdks/typescript-client \
  --additional-properties=npmName=@datashell/client,npmVersion=2.0.0,supportsES6=true,typescriptThreePlus=true,useSingleParameter=true

echo "Done. Validate per docs/SDK.md before committing."
