# Data Shell — Backup and Restore Drill Record

**Version:** 1.0
**Date:** 2026-10-20
**Purpose:** the measured result of an isolated backup/restore rehearsal, and
the procedure for repeating it against a target environment.

---

## 1. What the backup captures

`scripts/backup_restore.py` backs up the **entire storage root**, so every
component needed to restore a consistent system is included:

| Component | Location | Backup method |
|---|---|---|
| Boundary ledger (run history, quarantine index, usage, baselines) | `output/datashell_history.db` | SQLite online backup API (consistent snapshot, no torn copy) |
| Credential registry | `output/security_registry.sqlite3` | file copy |
| Published delivery targets, lineage artifacts | `output/…` | file copy |
| **Generation pointers** (the publication commit point) | `output/…/*.generation.json` | file copy |
| Audit certificates | `output/audits/` | file copy |
| Quarantine artifacts (held rows, structural refusals) | `output/quarantine/` | file copy |
| Pending ledger outbox records | `output/…/*.ledger-pending.json` | file copy |
| Contracts and contract versions | `contracts_registry/` | directory copy |
| Raw inbound inputs | `data/` | directory copy |
| Backup manifest with SHA-256 per file | `backup_manifest.json` | generated |

**Secrets are never part of a backup.** Signing keys live in the secret manager
and must be backed up separately under the operator's own key-management
policy; the drill never copies, prints, or records them.

**Policy note for the owner:** the `data/` directory (retained raw inbound
feeds) is included in the backup. If raw inputs must not leave the site, set
the retention so they are pruned before the backup window, or exclude that
directory explicitly — this is an operator decision, not a code default.

---

## 2. Executed drill (local, isolated)

```bash
PYTHONPATH=. python scripts/backup_restore_drill.py --keep
```

The drill builds a disposable storage root, executes **real** enforcement
runs, then backs up, restores into a **separate** directory (never over the
source), and verifies the restored state.

### Measured result

| Measurement | Value |
|---|---|
| Runs produced | 2 (`dm_run_d68ff65ac8d8`, `dm_run_38de8b3f87ce`) |
| Ledger rows before backup | `run_history: 2`, `quarantine_index: 4`, `usage_records: 2` |
| Ledger rows after restore | `run_history: 2`, `quarantine_index: 4`, `usage_records: 2` |
| Files backed up | 20 |
| Backup duration | 0.00 s (small dataset; dominated by file I/O) |
| Restore duration | 0.00 s |
| Total recovery duration | 0.01 s |
| Restored certificates re-verified | 2 / 2 → `AUTHENTIC_AND_UNMODIFIED` |
| Generation pointers restored | 2 → `dm_run_d68ff65ac8d8`, `dm_run_38de8b3f87ce` |
| Manifest checksum verification | PASS (`verify_restore` returned valid) |
| Drill verdict | **PASS** |

**What this proves:** the backup captures the ledger, the published artifacts,
the audit certificates, and the generation pointers; the restore reproduces
them exactly; the restored certificates verify cryptographically against the
restored artifacts using the operator's own key; and the restored ledger
counts match the source exactly.

**What this does NOT prove:** a production RPO/RTO. The dataset is tiny (20
files) and the storage is local. See § 4.

---

## 3. Recovery point and recovery time

| Concept | Meaning here | Measured value |
|---|---|---|
| **Recovery point** | How much data is lost between the last backup and the failure | Determined by the backup *frequency*, not by the drill. The drill measured the state at backup time only. |
| **Recovery time** | How long a restore takes | 0.01 s for a 20-file dataset. Scales with dataset size and storage throughput. |

**The drill measures recovery *duration*; it does not measure recovery *point*.**
The recovery point is a function of how often the operator runs the backup.
For the pilot, run the backup at least daily and before any deployment; the
resulting worst-case data loss is the interval between backups.

---

## 4. Target-environment drill (required to close the recovery gate)

The gate is **BLOCKED** until this runs against the real deployment.

```bash
# 1. Back up the live storage root.
python scripts/backup_restore.py backup \
  --storage-root /app/storage \
  --backup-dir /backups/drill-$(date +%Y%m%d-%H%M%S)

# 2. Verify the backup's checksums.
python scripts/backup_restore.py verify --restore-root /backups/drill-<ts>

# 3. Restore into an isolated directory (never over the live root).
python scripts/backup_restore.py restore \
  --backup-dir /backups/drill-<ts> --restore-root /mnt/restore-check

# 4. Verify the restored evidence independently.
#    a. Every restored certificate verifies against the restored artifacts.
find /mnt/restore-check -name '*_audit_certificate.json' -exec \
  datashell verify {} \; | grep -c AUTHENTIC_AND_UNMODIFIED

#    b. The restored ledger row counts match the live ledger.
sqlite3 /mnt/restore-check/output/datashell_history.db \
  "SELECT COUNT(*) FROM run_history; SELECT COUNT(*) FROM usage_records;"

#    c. The generation pointers are present and name the committed run.
find /mnt/restore-check -name '*.generation.json' -exec cat {} \;
```

### Record these measured values

| Measurement | How to obtain | Value |
|---|---|---|
| Backup duration | time the `backup` command | ______ |
| Restore duration | time the `restore` command | ______ |
| Data volume backed up | `du -sh /app/storage` | ______ |
| Restore throughput | volume ÷ restore duration | ______ |
| Certificates re-verified | count from step 4a | ______ / ______ |
| Ledger rows restored vs source | step 4b | ______ vs ______ |
| Pointers restored | step 4c | ______ |
| **Worst-case data loss (RPO)** | backup interval | ______ |
| **Worst-case downtime (RTO)** | restore + verify + cutover | ______ |

---

## 5. Failure modes and operational steps

| Failure | Detection | Containment | Recovery | Post-recovery verification |
|---|---|---|---|---|
| Backup command fails (disk full, permissions) | non-zero exit; missing manifest | stop the backup schedule; free space | re-run after fixing space | `verify` passes; manifest present |
| Backup produced but checksums fail | `verify` reports mismatches | treat the backup as unusable; do not delete the previous good one | re-run the backup | `verify` passes |
| Restore fails mid-way | non-zero exit from `restore` | keep the live system running | restore into a *new* directory; never over the live root | ledger counts and certificate verification pass |
| Restored certificates fail to verify | `datashell verify` reports a non-`AUTHENTIC_AND_UNMODIFIED` status | do not promote the restore | check that the **signing key** is the same one that signed the artifacts; if the key was rotated, confirm the old public key is in `DATASHELL_ED25519_KEYRING` | all certificates verify |
| Ledger rows differ after restore | step 4b mismatch | do not promote the restore | re-take the backup; investigate a non-quiesced backup | counts match |
| Live volume lost entirely | host failure | none possible | restore the newest verified backup to a new volume, repoint the deployment, re-run §9 of the deployment runbook | one valid run completes and the ledger shows prior history |

**Quiescing note:** the ledger is captured with SQLite's online backup API, so
it is internally consistent. The *artifacts* are copied file-by-file, so a
backup taken while a publication is in flight may capture the new artifact
without its certificate. The manifest's checksums plus the `verify` step are
what detect this. For a pilot, take backups during a quiet period or accept
that a single run may need re-running after a restore.

---

## 6. What the drill record does not establish

- A production RPO/RTO. The numbers in § 2 are from a 20-file local dataset.
- Backup encryption at rest. The operator must encrypt the backup volume or
  the backup directory; the tooling does not encrypt.
- Backup of the signing keys. They are the operator's responsibility under the
  key-management policy.
- Off-site or off-host backup. The drill backs up to a local directory.
