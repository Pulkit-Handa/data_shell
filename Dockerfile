# syntax=docker/dockerfile:1
#
# Hardened production image for the Data Shell boundary-enforcement API
# (datashell.server.app). The public demo/playground has its own image:
# Dockerfile.web.
#
# Reproducibility: runtime dependencies come exclusively from the hash-pinned
# requirements.lock, and the package itself is built into a wheel in a throwaway
# builder stage with the hash-pinned build backend from requirements-build.lock.
# No `datashell/` source is copied into the runtime stage, so the engine that
# runs is always the installed wheel — never a checkout on PYTHONPATH.

# ---------------------------------------------------------------------------
# builder: build the wheel from the checkout with the pinned build backend
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_RETRIES=10 \
    PIP_DEFAULT_TIMEOUT=60 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY requirements.lock requirements-build.lock ./
RUN python -m pip install --require-hashes -r requirements-build.lock

COPY pyproject.toml README.md ./
COPY datashell ./datashell
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_RETRIES=10 \
    PIP_DEFAULT_TIMEOUT=60 \
    PORT=8000 \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    DATASHELL_KEY_ID=datashell-hmac-v1 \
    ENVIRONMENT=production \
    DATASHELL_STORAGE_ROOT=/app/storage \
    DATASHELL_DATA_DIR=/app/storage/data \
    DATASHELL_CONTRACTS_DIR=/app/storage/contracts_registry \
    DATASHELL_OUTPUT_DIR=/app/storage/output \
    DATASHELL_AUDIT_DIR=/app/storage/output/audits \
    DATASHELL_QUARANTINE_DIR=/app/storage/output/quarantine \
    TMPDIR=/app/storage/tmp

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 datashell \
    && useradd --system --uid 10001 --gid datashell --home-dir /nonexistent --shell /usr/sbin/nologin datashell

WORKDIR /app

# Runtime dependencies: exactly the resolved, hash-verified release set.
COPY requirements.lock ./
RUN python -m pip install --require-hashes -r requirements.lock

# The application wheel, installed without pulling a second, unpinned resolution.
COPY --from=builder /wheels /tmp/wheels
RUN python -m pip install --no-deps /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

COPY examples ./examples

# The demo/playground UI ships next to the API. PYTHONPATH exposes only this
# source tree: the engine is imported from the installed wheel.
COPY web ./web
ENV PYTHONPATH=/app

RUN mkdir -p /app/storage/data /app/storage/contracts_registry /app/storage/output/audits /app/storage/output/quarantine /app/storage/tmp \
    && chown -R datashell:datashell /app/storage \
    && rm -rf /root/.cache /tmp/*

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl --fail --silent http://127.0.0.1:8000/ready || exit 1

# One worker is intentional while rate limiting and credential management are
# process-local. Scale only after deploying shared auth and rate-limit backends.
ENTRYPOINT ["sh", "-c", "umask 077; exec uvicorn datashell.server.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
