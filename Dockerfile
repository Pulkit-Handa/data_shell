# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
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
COPY pyproject.toml README.md ./
COPY datashell ./datashell
COPY examples ./examples
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir . \
    && mkdir -p /app/storage/data /app/storage/contracts_registry /app/storage/output/audits /app/storage/output/quarantine /app/storage/tmp \
    && chown -R datashell:datashell /app/storage \
    && rm -rf /root/.cache /tmp/*

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl --fail --silent http://127.0.0.1:8000/ready || exit 1

# One worker is intentional while rate limiting and credential management are
# process-local. Scale only after deploying shared auth and rate-limit backends.
ENTRYPOINT ["sh", "-c", "umask 077; exec uvicorn datashell.server.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]

# Add web UI to container
COPY web /app/web
ENV PYTHONPATH=/app
