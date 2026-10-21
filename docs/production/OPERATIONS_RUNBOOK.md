# Data Shell — Operations Runbook

**Version:** 1.0  
**Date:** 2026-10-19  
**Scope:** Controlled production pilot (single replica, single worker)

---

## 1. System Overview

Data Shell is a contract-driven boundary enforcement gateway. It accepts tabular data (CSV, JSONL, Parquet), validates it against tenant-scoped contracts, quarantines rejected records, publishes accepted artifacts, and records a signed certificate plus durable run history.

**Maximum credible topology:** One process, one worker, one replica, one trusted storage root.

---

## 2. Startup Procedure

### 2.1 Pre-Startup Checks

1. Verify persistent storage volume is mounted and has sufficient free space:
   ```bash
   df -h /app/storage
   ```
2. Required free space: at least `MAX_UPLOAD_SIZE_BYTES * DATASHELL_DISK_HEADROOM_MULTIPLIER` (default: 20 GiB × 2.0 = 40 GiB).

3. Verify secrets are provisioned:
   ```bash
   echo $DATASHELL_API_KEY | wc -c    # Must be >= 32
   echo $DATASHELL_HMAC_SECRET | wc -c  # Must be >= 32
   ```

4. Verify configuration:
   ```bash
   echo $ALLOWED_ORIGINS       # Must be explicit HTTPS origins
   echo $FORWARDED_ALLOW_IPS   # Must be explicit proxy IPs
   echo $ENVIRONMENT           # Must be "production"
   ```

### 2.2 Start the Service

**Docker Compose:**
```bash
docker-compose up -d
```

**Kubernetes:**
```bash
kubectl apply -f k8s-deployment.yaml
```

**Direct (development only):**
```bash
ENVIRONMENT=production \
DATASHELL_API_KEY="<unique-secret>" \
DATASHELL_HMAC_SECRET="<unique-secret>" \
ALLOWED_ORIGINS="https://console.example.com" \
FORWARDED_ALLOW_IPS="127.0.0.1" \
uvicorn datashell.server.app:app --host 0.0.0.0 --port 8000 --workers 1
```

### 2.3 Verify Startup

1. Wait for readiness:
   ```bash
   curl --fail --silent http://127.0.0.1:8000/ready
   ```

2. Check health:
   ```bash
   curl --fail --silent http://127.0.0.1:8000/health
   ```

3. Verify metrics are protected:
   ```bash
   curl --silent -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/metrics
   # Should return 401
   ```

4. Verify docs are disabled in production:
   ```bash
   curl --silent -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/docs
   # Should return 404
   ```

---

## 3. Health Checks

### 3.1 Liveness Probe

```bash
curl http://127.0.0.1:8000/health
```

**Expected response:** `{"status": "healthy", "service": "data-shell"}`

### 3.2 Readiness Probe

```bash
curl http://127.0.0.1:8000/ready
```

**Expected response:** `{"status": "ready", "service": "data-shell"}`

**Readiness fails (503) when:**
- Persistent storage directories are unavailable
- Free disk space is below `MIN_FREE_DISK_BYTES`
- Credential registry is unavailable
- Boundary ledger is unavailable

### 3.3 Metrics Endpoint

```bash
curl -H "X-API-Key: $DATASHELL_API_KEY" http://127.0.0.1:8000/metrics
```

**Note:** Metrics are authenticated. Do not expose `/metrics` publicly.

---

## 4. Backup Procedure

### 4.1 Create a Backup

```bash
python scripts/backup_restore.py backup \
  --storage-root /app/storage \
  --backup-dir /backups/datashell-$(date +%Y%m%d-%H%M%S)
```

**What gets backed up:**
- SQLite boundary ledger (`datashell_history.db`) — using SQLite's online backup API
- Security registry (`security_registry.sqlite3`)
- All artifacts under the output directory (delivered data, quarantine, audit certificates)
- Publication journals (if any exist)
- Backup manifest with SHA-256 checksums

### 4.2 Verify a Backup

```bash
python scripts/backup_restore.py verify \
  --restore-root /backups/datashell-20261019-120000
```

### 4.3 Restore from Backup

```bash
python scripts/backup_restore.py restore \
  --backup-dir /backups/datashell-20261019-120000 \
  --restore-root /app/storage-restored
```

### 4.4 Backup Schedule

- **RPO (Recovery Point Objective):** 24 hours (daily backup)
- **RTO (Recovery Time Objective):** 1 hour (restore + verification)
- **Retention:** 30 days
- **Encryption:** Backups contain sensitive data; encrypt at rest

---

## 5. Recovery Procedures

### 5.1 Publication Crash Recovery

The system automatically recovers interrupted publications on startup. If a crash occurs during multi-file publication:

1. The write-ahead journal is detected on the next startup
2. If the journal state is "prepared", the transaction is rolled back
3. If the journal state is "committed", backups are cleaned up
4. Recovery happens before the service reports ready

**Manual recovery check:**
```bash
# Check for leftover commit journals
find /app/storage -name ".dm-commit-*.json" -type f
```

### 5.2 Ledger Corruption Recovery

1. Stop the service
2. Restore the ledger from the most recent verified backup
3. Restore the artifact root from the same backup
4. Start the service — startup recovery will handle any remaining journals
5. Verify with:
   ```bash
   python scripts/backup_restore.py verify --restore-root /app/storage
   ```

### 5.3 Certificate Verification Failure

If certificate verification fails for a published artifact:

1. Stop further deliveries (scale to 0 replicas)
2. Preserve the journal and related artifacts
3. Verify the audit certificate, immutable run artifact, active target hash, and contract hash
4. Cross-check the run's ledger record
5. If integrity cannot be established, restore from a verified snapshot

---

## 6. Monitoring and Alerting

### 6.1 Key Metrics

| Metric | Description | Alert Threshold |
|---|---|---|
| `http_requests_total` | HTTP request count by status | 5xx rate > 1% |
| `http_request_duration_seconds` | Request latency | p99 > 5s |
| `runs_total` | Pipeline runs by status | Rejection rate > 50% |
| `rows_read_total` | Total rows processed | N/A (informational) |
| `storage_free_bytes` | Free disk space | < 20 GiB |
| `active_runs` | Currently running pipelines | >= MAX_CONCURRENT_RUNS |

### 6.2 Alerts

Import `deploy/prometheus-alerts.yaml` into Prometheus and configure alert routing.

### 6.3 Log Investigation

All logs include correlation IDs and run IDs. To investigate a failed run:

```bash
# Find the run ID from the correlation ID
grep "correlation_id" /var/log/datashell/*.log | grep "<correlation-id>"

# Look up the run in the ledger
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/api/v1/runs/<run_id>

# Check the quarantine index
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/api/v1/quarantine?run_id=<run_id>
```

### 6.4 Tracing Verification

Tracing is off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set **and** the
`datashell[otel]` extra is installed. `datashell doctor` reports both halves.

```bash
# Which halves are present in this environment?
datashell doctor | grep -A3 otel

# Does a span actually leave the process and reach a collector?
# (needs docker; starts a throwaway collector and tears it down)
scripts/run_otel_collector_e2e.sh
```

The startup log line `OpenTelemetry tracing enabled` confirms activation. If
the endpoint is set but the extra is missing, the gateway logs a warning and
keeps tracing disabled — it never fails a request because of tracing.

---

## 7. Incident Response

### 7.1 Cross-Tenant Access Detected

1. Immediately revoke the affected API key:
   ```bash
   curl -X DELETE -H "X-API-Key: $ADMIN_KEY" http://127.0.0.1:8000/api/v1/admin/keys/<key_id>
   ```
2. Preserve security event logs
3. Investigate the access path
4. Review and patch the vulnerability

### 7.2 Resource Exhaustion

1. Check disk space: `df -h /app/storage`
2. Check memory: `docker stats` or `kubectl top pod`
3. If disk is full, investigate:
   ```bash
   du -sh /app/storage/output/*
   ```
4. Increase volume size or prune old data per retention policy

### 7.3 Service Unhealthy

1. Check pod/container status
2. Review logs for startup errors
3. Verify storage volume is mounted
4. Verify secrets are still valid
5. Check for resource limits (OOMKilled)

---

## 8. Rollback Procedure

### 8.1 Application Rollback

1. Roll back the application image through the deployment platform
2. **Do not** roll back persistent storage independently
3. Verify compatibility between the application version and storage format

### 8.2 Rollback Triggers

Rollback immediately if:
- Certificate verification fails for a published artifact
- Ledger/artifact reconciliation detects a missing or mixed transaction
- Cross-tenant access is observed
- Resource limits fail to contain input or concurrency exhaustion
- Recovery cannot meet the declared RPO/RTO
- A release changes contract, certificate, ledger, or API behavior without compatibility evidence

---

## 9. Retention and Deletion

### 9.1 Data Retention

- **Run history:** Controlled by `DATASHELL_HISTORY_RETENTION_DAYS` (default: 0 = keep forever)
- **Artifacts:** Retained until manually deleted or pruned by retention policy
- **Audit certificates:** Retained with artifacts
- **Backups:** 30 days

### 9.2 Data Deletion

To delete a tenant's data:
1. Stop all runs for the tenant
2. Delete artifacts: `rm -rf /app/storage/output/tenants/<tenant-key>`
3. Delete quarantine: `rm -rf /app/storage/output/quarantine/tenants/<tenant-key>`
4. Prune ledger rows: `datashell ledger prune --days 0 --tenant <tenant>`

---

## 10. Support Escalation

1. **Level 1:** Operator follows this runbook
2. **Level 2:** Engineering investigates with logs and ledger
3. **Level 3:** Security team engages for security incidents

All escalations must include: correlation ID, run ID, tenant ID, and timestamp.
