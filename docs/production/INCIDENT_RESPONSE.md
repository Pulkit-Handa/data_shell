# Data Shell — Incident Response Runbook

**Version:** 1.0
**Date:** 2026-10-20
**Scope:** single replica, single tenant, one active job.

Every incident below states: **detection signal → containment → recovery →
post-recovery verification → escalation**. A named human owner must be assigned
per incident class before the pilot starts; this document deliberately does
**not** name a person, because that is an organisational decision this
repository cannot make.

---

## 0. Operator roles (to be assigned)

| Role | Responsibility | Named owner |
|---|---|---|
| **Service owner** | On-call for the gateway; authorises restart/rollback | ______________ |
| **Data owner** | Decides whether a delivery is correct; owns contract semantics | ______________ |
| **Security owner** | Authorises credential revocation and key rotation | ______________ |
| **Release authority** | Approves promote/rollback of a release | ______________ |

Escalation: if the service owner cannot resolve within the agreed support
window, escalate to the release authority. If a suspected data-integrity or
cross-tenant event is involved, escalate to the security owner **immediately**
and preserve all evidence before making changes.

---

## 1. Publication failure (a run did not publish, or published partially)

**Detection:** the API returns a `5xx` for `/api/v1/enforce`, or the run's
`status` is `FAILED`; a `*.ledger-pending.json` outbox file exists next to an
audit certificate; a commit journal (`*.dm-commit-*.json`) is present in the
delivery directory.

**Containment:** do not delete the journal or the outbox file — they are the
recovery record. Stop submitting new runs until the state is understood.

**Recovery:** restart the service. Startup runs journal recovery (rolls back
an interrupted publication to the last committed generation) and then ledger
reconciliation (replays the pending outbox into the ledger). Verify:

```bash
docker compose logs datashell-api | grep -iE "recover|reconcil"
# Expect: no journal_errors, and skipped=0 (or an explained skip).
cat <delivery-target>.generation.json    # names the committed run
datashell verify <committed-run-audit.json>   # AUTHENTIC_AND_UNMODIFIED
```

**Post-recovery verification:** re-submit the same feed; it must complete with
`LOADED` and foot to zero.

**Escalate to:** the service owner if recovery reports errors; the data owner
if the committed generation is not the expected one.

---

## 2. Unreconciled ledger entries (published but not recorded)

**Detection:** the startup log contains
`Unreconciled pending ledger entries remain after startup recovery`, or
`skipped` is greater than zero.

**Meaning:** a run published artifacts but its ledger write did not complete.
The outbox record is still on disk — durable, not lost.

**Containment:** do not delete `*.ledger-pending.json`. The run is *not*
recorded as committed, so no usage has been charged for it.

**Recovery:** restore database access, then replay:

```bash
python -c "
from datashell.pipeline.runner import reconcile_pending_ledger_entries
print(reconcile_pending_ledger_entries())"
```

A record whose artifacts exist is replayed and committed. A record whose
artifacts were **never published** is deliberately skipped and left for
inspection — the log line
`Pending ledger entry describes artifacts that were never published` names the
run. That run genuinely did not deliver and must not be recorded as if it had.

**Post-recovery verification:** confirm the run appears in the ledger with
`ledger_status = COMMITTED`, and that `usage_records` contains exactly one row
for it.

**Escalate to:** the data owner if a skipped record corresponds to a delivery
the customer was told succeeded.

---

## 3. Database unavailable

**Detection:** `/ready` returns `503`; the log shows
`Cannot safely recover corrupted commit journal` or SQLite `OperationalError`
messages; the boundary-ledger integrity check fails.

**Containment:** the gateway fails closed — it will not accept a delivery it
cannot record. Stop submissions. **Do not delete the database file**; it may
be recoverable and it is forensic evidence.

**Recovery:**

1. If the filesystem is full, free space (see § 4). The ledger is a single
   file; `datashell_history.db-wal` and `-shm` may also be present.
2. If the file is corrupt, restore from the newest verified backup
   (`docs/production/BACKUP_RESTORE_DRILL.md` § 4) into a **new** directory
   and repoint the deployment.
3. Restart the service; startup recovery re-runs.

**Post-recovery verification:** `/ready` returns `200`; the ledger shows the
expected history; one valid run completes and is recorded.

**Escalate to:** the service owner; the release authority if a restore is
needed.

---

## 4. Disk exhaustion

**Detection:** `/ready` returns `503`; `datashell_storage_free_bytes` is below
`MIN_FREE_DISK_BYTES`; writes fail with `ENOSPC`.

**Containment:** stop submissions. The preflight disk-headroom check refuses
to start a run that cannot fit its output, so no partial delivery is produced.

**Recovery, in order of preference:**

1. Prune old history: `datashell ledger prune --days <n>` (respects
   `DATASHELL_HISTORY_RETENTION_DAYS`).
2. Remove quarantine artifacts for runs that are outside the retention
   window. **Keep** the audit certificates — they are the evidence.
3. Extend the volume.

**Post-recovery verification:** `/ready` returns `200`; a new run completes;
`df -h` shows headroom above `MIN_FREE_DISK_BYTES`.

**Escalate to:** the service owner.

---

## 5. Certificate verification failure (tamper suspected)

**Detection:** `datashell verify <cert>` reports `TAMPERED_OR_INVALID_SIGNATURE`
or `TAMPERED_UNDERLYING_ARTIFACTS`; the
`datashell_certificate_verification_failures_total` metric rises.

**This is a potential security incident. Preserve evidence first.**

**Containment (immediately):**

1. Do **not** restart or clean the storage volume — that destroys evidence.
2. Snapshot/copy the delivery directory, the audit directory, and the ledger
   database to a separate location.
3. Note the exact artifact, the expected digest from the certificate, and the
   actual digest.

**Recovery:** this is not a service fault to be "fixed" — determine the cause:

- *The file was modified outside the gateway* → treat as a suspected
  compromise: rotate signing keys, revoke credentials, and review access logs.
- *The file was modified by a legitimate later run* → the verification failure
  is correct behaviour for a superseded artifact; the certificate is stale, not
  the data. Check `<target>.generation.json` for the committed generation.
- *The key was rotated* → confirm the old public key is still in
  `DATASHELL_ED25519_KEYRING`; verification of historical certificates depends
  on it.

**Post-recovery verification:** if a key was rotated, verify that *old*
certificates still verify with the retained public key, and that *new* runs
verify with the new key.

**Escalate to:** the security owner **immediately**, and the data owner.

---

## 6. Credential compromise

**Detection:** unexpected `401`s from a legitimate client, an unexpected
`403`, anomalous request volume, or a credential found in a log, a backup, or
a repository.

**Containment (immediately):**

1. Revoke the credential: `POST /api/v1/admin/keys` with the `revoke`
   operation for that key id. Revocation is effective immediately in the
   process (the registry is local SQLite, read on every request).
2. If the master key (`DATASHELL_API_KEY`) is implicated, rotate it in the
   secret manager and restart the service. **The process-local rate limiter
   and credential cache reset on restart**, so a restart also clears
   process-local abuse state.
3. Preserve the request logs.

**Recovery:** issue a new credential; update the consumer; verify the old one
returns `401` and the new one works.

**Post-recovery verification:** confirm the revoked key returns `401` on a
protected route.

**Escalate to:** the security owner **immediately**.

---

## 7. Cross-tenant access suspected

**Detection:** any request that should have been denied returns data, or a
ledger/quarantine/settlement query returns another tenant's rows.

**This is the most serious incident class.**

**Containment (immediately):**

1. Stop the gateway. A single replica means stopping it stops all access.
2. Preserve the full storage volume, the ledger database, and the logs.
3. Do **not** attempt to repair the database in place.

**Recovery:** treat as a security incident. Rotate all credentials, restore
from a known-good backup, and re-verify tenant isolation with
`tests/test_tenant_isolation.py` as the regression suite. Deploy only after
the isolation tests pass against the restored data.

**Post-recovery verification:** re-run the tenant-isolation suite; verify that
each tenant sees only its own rows via the API.

**Escalate to:** the security owner and the release authority **immediately**,
and notify the affected customer(s) per the incident-communication procedure.

---

## 8. Interrupted startup recovery

**Detection:** the service does not become ready; the log shows
`Publication journal recovery reported errors` or a
`Cannot safely recover corrupted commit journal` error.

**Containment:** leave the service stopped. A corrupted journal means the
publication state is unknown; letting the service start risks publishing over
an unknown generation.

**Recovery:**

1. Inspect the journal: it is a JSON file with a `journal_mac`. If the MAC
   does not validate, the journal is untrustworthy and the recovery is
   refused deliberately.
2. Identify the entries and check whether their final artifacts exist and
   match the certificate for that run.
3. If the journal is corrupt and the artifacts are otherwise consistent,
   remove **only** the journal file (after copying it for evidence) and
   restart — the next publication re-establishes the journal.
4. If the artifacts are inconsistent, restore from backup.

**Post-recovery verification:** the service becomes ready; the committed
generation verifies; one valid run completes.

**Escalate to:** the service owner; the data owner if a delivery is affected.

---

## 9. Failed deployment / upgrade

**Detection:** the deployment runbook's § 3 readiness check fails after
deploying a new image; the startup log shows migration or recovery errors.

**Containment:** do not proceed. Keep the previous image available for
rollback.

**Recovery:** follow `docs/production/DEPLOYMENT_VERIFICATION.md` § 13. The
ledger schema only ever adds columns and indexes, so the previous image is
compatible with the migrated database — redeploy it. If a *data* problem is
the cause, restore the pre-upgrade backup (§ 10 of that runbook).

**Post-recovery verification:** `/ready` returns `200`; the ledger shows the
expected history; one valid run completes and verifies.

**Escalate to:** the release authority (they own the rollback decision).

---

## 10. General evidence-preservation rule

For **any** incident that touches data integrity, credentials, or tenant
isolation:

1. Copy the storage volume, the ledger database, and the logs to a separate
   location **before** restarting or repairing anything.
2. Record the commit SHA, the deployment digest, the time, and the operator.
3. Never delete a journal, an outbox record, a quarantine artifact, or an
   audit certificate to "clean up" an incident.
