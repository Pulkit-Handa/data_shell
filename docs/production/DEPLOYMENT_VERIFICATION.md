# Data Shell — Deployment Verification Runbook

**Version:** 1.0
**Date:** 2026-10-20
**Scope:** single host, single replica, single tenant, one active job.

This runbook is the executable procedure for verifying a Data Shell deployment
in a *target* environment. Every step states the expected result and the
command that produces it, so an operator other than the author can execute it
and produce the evidence a release decision needs.

> **Status of this runbook:** every step below is executable and has been run
> against a local, single-process deployment of the current branch (see
> `docs/production/DEPLOYMENT_VERIFICATION.md` § "Local execution record" for
> the recorded result). Steps marked **[TARGET]** require the target
> environment and therefore remain unexecuted until a deployment is authorised.

---

## Session checklist — at a glance

The detailed steps are § 0–§ 14. This is the order to run them, what each one
proves, and what to keep. Every row is a `[TARGET]` step: none of it has been
executed in the target environment.

| # | Action | Command (§) | Expected | Evidence to retain |
|---|---|---|---|---|
| 1 | Record the artefact under test | `git rev-parse HEAD` (§ 0) | The exact commit SHA | SHA, image digest, `docker images` line |
| 2 | Confirm the volume is local POSIX | `stat -f -c %T <volume>` (§ 0) | Not `nfs`/`cifs` | Command output |
| 3 | Confirm secrets are injected, not baked in | `printenv \| grep -c DATASHELL_` (§ 0) | Non-zero; no value in the image | Env var *names* only — never values |
| 4 | Build and start | `docker compose up -d` (§ 1) | Service starts; recovery summary shows `skipped: 0`, `journal_errors: 0` | Startup log excerpt, recovery summary |
| 5 | Confirm placeholders are refused | `EnterpriseSettings()` with a placeholder (§ 2) | `RuntimeError` mentioning *placeholder* | Command output + exit code |
| 6 | Health and readiness | `curl -fsS .../health`, `.../ready` (§ 3) | Both `200` | Both responses |
| 7 | Metrics endpoint is not public | `curl -o /dev/null -w '%{http_code}' .../metrics` (§ 3) | `401` | Status code |
| 8 | Protected endpoint rejects anonymous | `curl .../api/v1/ledger/runs` (§ 4) | `401` | Status code |
| 9 | Valid run | `POST /api/v1/enforce` (§ 5) | `LOADED`, footing matched, 0 unaccounted | Run JSON, ledger row, certificate |
| 10 | Rejected run | Same, with a violating file (§ 6) | Rejected; **no** delivery target written | Run JSON, absent target |
| 11 | Certificate verification | `datashell verify <cert>` (§ 7) | `AUTHENTIC_AND_UNMODIFIED` | Verify output, exit code |
| 12 | Crash and recover | kill during publish (§ 8) | Exactly one usage row; no duplicate | Recovery summary, ledger rows |
| 13 | Restart and persistence | `docker compose restart` (§ 9) | Ledger continuity; readiness restored | Ledger row count before/after |
| 14 | Migration in place | deploy the new image (§ 10) | Columns added, no destructive change | Migration log |
| 15 | Capacity on this host | `BENCHMARK_PROCEDURE.md` § 7 | Pass criteria met at the target size | Full benchmark output + host spec |
| 16 | Backup / restore drill | `BACKUP_RESTORE_DRILL.md` § 4 | Counts match; certs re-verify; pointers restored | `drill_report.json`, worksheet |
| 17 | Alert delivery | `DEPLOYMENT_VERIFICATION.md` § 11 | Alert **received** by a human | Screenshot/email of the received alert |
| 18 | Rollback rehearsal | `IMAGE_TAG=<prev> docker compose up -d` (§ 13) | Previous image serves; no down-migration | Rollback log, readiness response |
| 19 | Package the evidence | § 14 | All 10 artefacts retained | Release checklist, signed |

### Stop conditions — abort the session if any of these occur

Stop, leave the system in a known state, capture the output, and escalate to
the release authority. Do **not** work around a stop condition, and do not
record the affected step as passed.

| # | Condition | Immediate action |
|---|---|---|
| SC-1 | Startup recovery reports `skipped > 0` or `journal_errors > 0` | Do not proceed past step 4. Reconcile or repair the ledger first (`OPERATIONS_RUNBOOK.md`). |
| SC-2 | The service starts with a placeholder or wildcard secret | Stop the deployment. The service must refuse to boot. |
| SC-3 | `/ready` returns `503` on a healthy volume | Stop. Investigate disk headroom and the ledger integrity check. |
| SC-4 | A protected endpoint answers `200` without a credential | **Security stop.** Stop the deployment, preserve logs, escalate to the security owner. |
| SC-5 | Any cross-tenant data is visible | **Security stop.** Stop, preserve evidence, escalate. Do not attempt remediation in place. |
| SC-6 | Row footing does not match, or unaccounted rows > 0 | Stop. The delivery is untrustworthy; do not publish or promote it. |
| SC-7 | A certificate fails verification, or an artifact does not match its certificate | Stop. Treat the delivery target as suspect. |
| SC-8 | Replay produces more than one usage row for one run | Stop. The ledger is not idempotent; do not run further paid work. |
| SC-9 | The volume is `nfs`/`cifs` (or any filesystem without `flock`/`os.replace` semantics) | Stop. The atomicity guarantees are void on this filesystem. |
| SC-10 | Capacity fails the pass criteria at the configured size | Do not raise `MAX_UPLOAD_SIZE_BYTES` to make it pass. Reduce the limit and record the ceiling. |
| SC-11 | The backup/restore drill fails to re-verify a certificate | Stop. Do not proceed to a release decision; restore capability is not proven. |
| SC-12 | The alert is generated but not received by a human | Stop at step 17. The signal is not a delivery; treat alerting as unproven. |
| SC-13 | The rollback rehearsal does not restore service | Stop. Do not promote the new image. |
| SC-14 | Any step's evidence cannot be captured | Stop. An unrecorded step is an unproven step. |

**Rollback is always available and is not a stop condition:** redeploy the
previous image tag (§ 13). The ledger schema only ever adds columns, so no
down-migration is required.

---

## 0. Preconditions

| Item | Requirement | How to confirm |
|---|---|---|
| Branch / commit | The exact commit under test | `git rev-parse HEAD` |
| Python | 3.11 – 3.14 | `python3 --version` |
| Filesystem | Local POSIX filesystem supporting `fcntl.flock` and atomic `os.replace` | `stat -f -c %T <volume>` must not report `nfs`/`cifs` |
| Storage | Persistent volume sized `MAX_UPLOAD_SIZE_BYTES × DATASHELL_DISK_HEADROOM_MULTIPLIER` | `df -h <volume>` |
| Secrets | `DATASHELL_API_KEY`, `DATASHELL_HMAC_SECRET` (or `DATASHELL_ED25519_PRIVATE_KEY`) injected from the secret manager | `printenv \| grep -c DATASHELL_` |
| Signer | Ed25519 recommended for independent verification; HMAC acceptable for an agreed internal trust model | § 2 |
| Operator | A named human owner for the deployment and the rollback decision | recorded in the release checklist |

**Never** place secrets in the image, the repository, or a committed file.
`.env.example` contains placeholders only and production refuses to boot with
them.

---

## 1. Clean deployment and startup

```bash
# [TARGET] Build the release image (non-root, read-only root filesystem).
docker build --pull --tag data-shell:$(git rev-parse --short HEAD) .

# [TARGET] Start the service.
docker compose up -d

# [TARGET] Observe startup logs: recovery runs BEFORE readiness is reported.
docker compose logs -f datashell-api
```

**Expected:** the service logs a start line, and **before** it reports
readiness it logs the recovery summary. If `skipped` or `journal_errors` is
non-zero in that summary, the deployment has **not** recovered cleanly — stop
and follow `docs/production/OPERATIONS_RUNBOOK.md` § "Unreconciled ledger
entries".

**Failure mode to watch:** a readiness probe that passes while reconciliation
was incomplete. The startup gate runs journal recovery and ledger
reconciliation before the application accepts traffic; a failed reconciliation
is logged at ERROR and counted, never silently dropped.

---

## 2. Production-secret validation

```bash
# [TARGET] Placeholders must be refused.
ENVIRONMENT=production \
DATASHELL_API_KEY=replace_with_secure_generated_api_key__min_32_chars \
DATASHELL_HMAC_SECRET=replace_with_secure_generated_hmac_secret__min_32_chars \
ALLOWED_ORIGINS=https://console.example.com FORWARDED_ALLOW_IPS=127.0.0.1 \
python -c "from datashell.core.config import EnterpriseSettings; EnterpriseSettings()"
```

**Expected:** a `RuntimeError` mentioning *placeholder*. The service must
refuse to start.

```bash
# [TARGET] Wildcard CORS / proxy trust must be refused.
ALLOWED_ORIGINS='*' FORWARDED_ALLOW_IPS='*' ...
```

**Expected:** `RuntimeError` for both. A wildcard `ALLOWED_ORIGINS` or
`FORWARDED_ALLOW_IPS=*` in production is a startup failure, not a warning.

---

## 3. Health and readiness

```bash
curl -fsS http://127.0.0.1:8000/health   # -> {"status":"healthy",...}
curl -fsS http://127.0.0.1:8000/ready     # -> {"status":"ready",...}
```

**Expected:** both `200`. `/ready` returns `503` when the storage volume is
unavailable, free disk is below `MIN_FREE_DISK_BYTES`, the credential registry
is unavailable, or the boundary ledger fails its integrity check.

```bash
# The metrics endpoint must NOT be public.
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/metrics   # -> 401
```

---

## 4. Protected API endpoints

```bash
# Unauthenticated access to a protected route must fail.
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/v1/ledger/runs  # -> 401

# Authenticated access must succeed.
curl -s -H "X-API-Key: $DATASHELL_API_KEY" http://127.0.0.1:8000/api/v1/ledger/runs
```

**Expected:** `401` without a credential, `200` with one. A tenant mismatch
must return `403`, never another tenant's data.

---

## 5. A valid processing run

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/enforce \
  -H "X-API-Key: $DATASHELL_API_KEY" \
  -F "contract_name=vendor-settlements" \
  -F "file=@examples/samples/vendor_settlements_1k.jsonl" | tee /tmp/run.json
```

**Expected:** `"status": "LOADED"`, `rows_read: 1000`, `rows_delivered: 900`,
`unaccounted_for: 0`, and `ledger_status: "COMMITTED"`.

**Invariant to check:** `rows_read == rows_delivered + rows_held + rows_refused`.
Any other value is a release-blocking defect.

---

## 6. A rejected or malformed run

```bash
# A feed that violates the contract gate must not publish a delivery target.
curl -sS -X POST http://127.0.0.1:8000/api/v1/enforce \
  -H "X-API-Key: $DATASHELL_API_KEY" \
  -F "contract_name=vendor-settlements" \
  -F "file=@/path/to/rejected-feed.jsonl" | tee /tmp/rejected.json
```

**Expected:** `"status": "REJECTED"`, `rows_delivered: 0`, and **no** new
delivery target. Verify the previously committed generation is untouched:

```bash
cat <delivery-target>.generation.json      # run_id must be unchanged
```

---

## 7. Artifact and certificate verification

```bash
# Independent verification of the published evidence.
datashell verify /app/storage/output/audits/<run>_audit_certificate.json
```

**Expected:** `AUTHENTIC_AND_UNMODIFIED`, `signature valid: True`, and
`committed generation: <run_id> (this certificate: yes)`.

A certificate that verifies cryptographically but reports `this certificate: no`
is a **superseded generation** — expected after a later run, and a defect if it
appears for the newest run.

**Tamper check (must fail closed):**

```bash
cp <delivery-target> /tmp/tampered.parquet
printf 'x' | dd of=/tmp/tampered.parquet bs=1 seek=0 conv=notrunc 2>/dev/null
datashell verify <certificate>   # -> TAMPERED_UNDERLYING_ARTIFACTS (non-zero exit)
```

---

## 8. Publication and ledger recovery behaviour

```bash
# [TARGET] Simulate a crash between artifact publication and ledger recording:
# make the ledger unwritable, run once, then restore it and restart.
chmod 000 /app/storage/output/datashell_history.db
# ... submit one run ...
chmod 660 /app/storage/output/datashell_history.db
docker compose restart datashell-api
docker compose logs datashell-api | grep -i "Reconciled pending ledger entries"
```

**Expected:** the run reports `ledger_status: "PENDING"`, the outbox record
`*.ledger-pending.json` exists next to the audit certificate, and after restart
the startup log reports `replayed: 1` and the run is `COMMITTED` in the ledger
with **exactly one** usage row.

```bash
# No duplicate usage after replay.
curl -s -H "X-API-Key: $DATASHELL_API_KEY" \
  "http://127.0.0.1:8000/api/v1/ledger/usage?tenant=<tenant>"
```

---

## 9. Restart behaviour and persistence

```bash
docker compose restart datashell-api
curl -fsS http://127.0.0.1:8000/ready
curl -s -H "X-API-Key: $DATASHELL_API_KEY" http://127.0.0.1:8000/api/v1/ledger/runs | head -c 400
```

**Expected:** the service returns to ready, and the run history from before the
restart is still present — the ledger lives on the persistent volume, not in
process memory.

---

## 10. Migration and upgrade behaviour

```bash
# [TARGET] Take a backup BEFORE upgrading.
python scripts/backup_restore.py backup \
  --storage-root /app/storage \
  --backup-dir /backups/pre-upgrade-$(date +%Y%m%d-%H%M%S)

# [TARGET] Deploy the new image and start it.
docker compose up -d
docker compose logs --tail 50 datashell-api | grep -iE "migration|recovery|reconcil"
```

**Expected:** the new version starts, applies the idempotent schema migration
in place (the ledger database is migrated in place; no destructive migration
is performed), and reports a clean recovery.

**Downgrade/rollback path:** the ledger schema only ever *adds* columns and
indexes, so the previous image remains compatible with the migrated database.
To roll back, redeploy the previous image tag — the database needs no
down-migration. If a rollback is required because of a *data* problem, restore
from the pre-upgrade backup (§ 12).

---

## 11. Monitoring and alert delivery

```bash
curl -s -H "X-API-Key: $DATASHELL_API_KEY" http://127.0.0.1:8000/metrics \
  | grep -E "^datashell_(runs_total|rows_delivered_total|http_requests_total)"
```

**Expected:** the counters are present and increase with real traffic.

Confirm the alert rules fire by triggering the condition, not by inspecting the
rule file. `deploy/prometheus-alerts.yaml` ships the rule definitions; the
rules that matter for the pilot are:

| Condition | Signal | Expected operator response |
|---|---|---|
| Ledger reconciliation incomplete | ERROR log `Unreconciled pending ledger entries remain after startup recovery` | reconciliation repair runbook |
| Publication recovery incomplete | ERROR log `Publication journal recovery reported errors` | publication-failure runbook |
| Disk pressure | `datashell_storage_free_bytes` below `MIN_FREE_DISK_BYTES` | free space; `/ready` will fail |
| Sustained request rejections | `datashell_http_requests_total{status_code="503"}` rising | capacity is saturated; the limit is enforced, not broken |
| Certificate verification failures | `datashell_certificate_verification_failures_total` rising | investigate tampering or a key rotation |

---

## 12. Backup and restore verification

```bash
# [TARGET] Full drill — see docs/production/BACKUP_RESTORE_DRILL.md.
python scripts/backup_restore.py backup \
  --storage-root /app/storage --backup-dir /backups/drill-$(date +%s)
python scripts/backup_restore.py verify --restore-root /backups/drill-<ts>

# [TARGET] Restore into an isolated location and validate the restored evidence.
python scripts/backup_restore.py restore \
  --backup-dir /backups/drill-<ts> --restore-root /tmp/restore-check
datashell verify /tmp/restore-check/.../<run>_audit_certificate.json
```

**Expected:** every restored certificate verifies against the restored
artifacts, the restored ledger row count matches the source, and the
generation pointers are present and name the committed run.

---

## 13. Rollback

```bash
# [TARGET] Redeploy the previous image (no schema down-migration required).
docker compose down
IMAGE_TAG=<previous-tag> docker compose up -d
curl -fsS http://127.0.0.1:8000/ready
```

**Rollback triggers (any one):** a failed readiness check after deploy; a
cross-tenant read; an unaccounted-row reconciliation failure; a delivered
artifact that does not match its certificate; unreconciled ledger entries that
cannot be repaired.

**Data rollback** (only if published data itself is wrong): restore the
pre-upgrade backup from § 10. Published artifacts are immutable per run, so a
forward fix (publish a corrected generation) is preferred over a data restore.

---

## 14. Evidence to capture

A deployment is verified when the following are recorded:

1. Commit SHA and image digest.
2. Output and exit code of every command in this runbook.
3. The startup recovery summary.
4. One valid run and one rejected run, with their ledger rows and certificates.
5. The tamper-detection result.
6. The crash/recovery result (pending → reconciled, exactly one usage row).
7. The post-restart ledger continuity check.
8. The backup/restore verification result.
9. The alert test result.
10. The rollback rehearsal result.

Store these with the release checklist. A deployment whose evidence is
incomplete is **not** verified, regardless of how healthy the service looks.
