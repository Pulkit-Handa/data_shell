#!/usr/bin/env bash
#
# End-to-end OpenTelemetry export check against a real collector.
#
# What it does:
#   1. starts a pinned OpenTelemetry collector (OTLP/gRPC receiver, file
#      exporter) in Docker;
#   2. points the gateway at it through the standard
#      OTEL_EXPORTER_OTLP_ENDPOINT and DATASHELL_OTEL_SERVICE_NAME variables;
#   3. runs tests/test_otel_collector_e2e.py, which drives one gateway exchange
#      and asserts the collector received the span;
#   4. always removes the container, and keeps the collector log plus the
#      captured spans as evidence.
#
# This is the local twin of the `otel-collector-e2e` CI job, so a failing
# check can be reproduced on a workstation with the same command CI uses.
#
# Requirements: docker, a Python environment with the `datashell[otel]` extra
# and pytest installed, and the repository checked out.
#
# Usage:
#   scripts/run_otel_collector_e2e.sh
#
# Environment overrides:
#   OTEL_COLLECTOR_IMAGE   collector image (default: pinned contrib release)
#   OTEL_COLLECTOR_PORT    host port for the OTLP/gRPC receiver (default: an
#                          ephemeral free port)
#   OTEL_E2E_EVIDENCE_DIR  where to keep the collector log and captured spans
#   DATASHELL_OTEL_SERVICE_NAME  service.name asserted by the check
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

COLLECTOR_IMAGE="${OTEL_COLLECTOR_IMAGE:-otel/opentelemetry-collector-contrib:0.163.1}"
COLLECTOR_CONFIG="${OTEL_COLLECTOR_CONFIG:-${REPO_ROOT}/scripts/otel-collector-ci.yaml}"
CONTAINER_NAME="${OTEL_COLLECTOR_CONTAINER:-datashell-otel-e2e}"
SERVICE_NAME="${DATASHELL_OTEL_SERVICE_NAME:-datashell-e2e}"
READY_TIMEOUT_SECONDS="${OTEL_COLLECTOR_READY_TIMEOUT:-60}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required to run the end-to-end OpenTelemetry check" >&2
  exit 1
fi
if [ ! -f "$COLLECTOR_CONFIG" ]; then
  echo "collector configuration not found: $COLLECTOR_CONFIG" >&2
  exit 1
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/datashell-otel-e2e.XXXXXX")"
SPANS_DIR="${WORK_DIR}/spans"
EVIDENCE_DIR="${OTEL_E2E_EVIDENCE_DIR:-${WORK_DIR}/evidence}"
SPANS_FILE="${SPANS_DIR}/spans.json"
mkdir -p "$SPANS_DIR" "$EVIDENCE_DIR"

# An ephemeral port keeps parallel runs (and a busy workstation) from
# colliding. An explicit OTEL_COLLECTOR_PORT still wins.
if [ -n "${OTEL_COLLECTOR_PORT:-}" ]; then
  HOST_PORT="$OTEL_COLLECTOR_PORT"
else
  HOST_PORT="$(python -c 'import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()')"
fi

# The collector image runs as a non-root user; the capture directory has to be
# writable by that uid. `:z` (added to the mount below) relabels the directory
# for the container on SELinux hosts and is ignored elsewhere.
chmod 0777 "$SPANS_DIR"
# The collector reads its configuration from the mounted capture directory so
# that no repository file needs a container label.
cp "$COLLECTOR_CONFIG" "${SPANS_DIR}/collector.yaml"

cleanup() {
  status=$?
  set +e
  docker logs "$CONTAINER_NAME" >"${EVIDENCE_DIR}/collector.log" 2>&1
  if [ -f "$SPANS_FILE" ]; then
    cp "$SPANS_FILE" "${EVIDENCE_DIR}/spans.json"
  fi
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1
  if [ "$status" -ne 0 ]; then
    echo "" >&2
    echo "The end-to-end OpenTelemetry check failed. Collector log:" >&2
    tail -n 40 "${EVIDENCE_DIR}/collector.log" >&2
  fi
  echo "Evidence: ${EVIDENCE_DIR}" >&2
  # The evidence directory lives inside the work directory unless the caller
  # asked for a specific one; in that case the work directory can go.
  if [ -n "${OTEL_E2E_EVIDENCE_DIR:-}" ]; then
    rm -rf "$WORK_DIR"
  fi
  exit "$status"
}
trap cleanup EXIT

echo "==> Starting the OpenTelemetry collector (${COLLECTOR_IMAGE})"
docker run --detach --name "$CONTAINER_NAME" \
  --publish "127.0.0.1:${HOST_PORT}:4317" \
  --volume "${SPANS_DIR}:/var/lib/datashell-otel:z" \
  "$COLLECTOR_IMAGE" \
  --config=/var/lib/datashell-otel/collector.yaml >/dev/null

echo "==> Waiting for the collector to accept OTLP/gRPC on 127.0.0.1:${HOST_PORT}"
for attempt in $(seq 1 "$READY_TIMEOUT_SECONDS"); do
  if ! docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q true; then
    echo "the collector container stopped during startup" >&2
    exit 1
  fi
  if python -c "
import socket, sys
try:
    socket.create_connection(('127.0.0.1', ${HOST_PORT}), timeout=1).close()
except OSError:
    sys.exit(1)
"; then
    break
  fi
  if [ "$attempt" -eq "$READY_TIMEOUT_SECONDS" ]; then
    echo "the collector did not become ready within ${READY_TIMEOUT_SECONDS}s" >&2
    exit 1
  fi
  sleep 1
done

echo "==> Running the end-to-end tracing check"
ENVIRONMENT=test \
DATASHELL_OTEL_E2E=1 \
OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:${HOST_PORT}" \
DATASHELL_OTEL_SERVICE_NAME="$SERVICE_NAME" \
DATASHELL_OTEL_SPANS_FILE="$SPANS_FILE" \
OTEL_BSP_SCHEDULE_DELAY=200 \
  python -m pytest tests/test_otel_collector_e2e.py -v -ra

echo "==> End-to-end OpenTelemetry check passed"
