# Data Shell Threat Model

## Scope and assumptions

Data Shell accepts tabular inputs, evaluates them against tenant-owned contracts, quarantines rejected records, publishes accepted datasets, and emits signed audit certificates. The threat model assumes clients may be malicious and input files may be malformed or intentionally resource-intensive. The host, container runtime, secret manager, and storage provider are trusted operational components but can fail.

## Assets

- Input, accepted, held, and rejected records.
- Tenant contracts and contract-version metadata.
- Active delivery targets and immutable run artifacts.
- Audit certificates, signing keys, and commit journals.
- API credentials, tenant bindings, run history, and security events.
- The durable boundary ledger: per-run forensics, quarantine index, drift baselines, and usage metering.
- Outbound run-event webhook payloads and their signatures.
- Availability of the API, CPU, memory, temporary storage, and persistent storage.

## Trust boundaries

1. Internet/client to TLS ingress.
2. Ingress to the API container.
3. API identity to tenant-scoped contract, quarantine, history, and delivery namespaces.
4. Untrusted uploaded bytes to parsers and analytical engines.
5. Staged artifacts to committed delivery artifacts.
6. Container filesystem to persistent storage and backup systems.
7. Application logs/metrics to the monitoring platform.
8. API container to external webhook receivers (egress).

## Threats and controls

| Threat | Controls in repository | Residual requirement |
|---|---|---|
| Missing/weak production credentials | Startup rejects missing, placeholder, ephemeral, short, or low-entropy secrets | Provision and rotate through an external secret manager |
| Unauthorized API use | API-key authentication, role capabilities, durable runtime-managed key fingerprints, revocation, per-IP and per-key limits | Independent security review; use shared identity/rate-limit services before multi-replica operation |
| Upload/parser denial of service | Preflight auth/capability checks, control-body and upload size limits, chunk counting, concurrency caps, DuckDB memory cap | Load, disk-full, and soak tests at expected workload |
| Tenant cross-access | Collision-resistant tenant path encoding, tenant-scoped roots, tenant checks, reserved operational paths | Automated cross-tenant negative tests across every endpoint and deployment-specific review |
| Partial delivery after crash | Signed write-ahead journal, durable staged writes, backups/rollback, startup recovery of target + lineage + certificate | Crash-injection tests on the actual production filesystem and storage driver |
| Artifact substitution / malicious certificate path | Content hashes, signed audit certificates, immutable run artifacts; verifier validates the signature before dereferencing paths and restricts artifact paths to tenant storage roots | Key lifecycle, verifier review, and tampering tests |
| Credential database exposure | SHA-256 fingerprints rather than raw managed API keys, file permissions, persistent volume | Protect the volume and backups; SQLite is for single-replica deployment only |
| Sensitive logs/metric cardinality | Generic client errors, correlation IDs, redacted webhook URLs, bounded-cardinality metrics | Configure access-controlled log retention and verify redaction in staging |
| Storage loss or corruption | Persistent storage, readiness checks, recoverable commit journals | Encrypted backups, restoration drills, defined RPO/RTO |
| Ledger tampering or cross-tenant disclosure | Ledger reads are tenant-scoped by construction; admin-only cross-tenant reads; SQLite file lives under the protected storage root | Protect the volume/backups; move to a shared store before multi-replica scaling |
| Webhook spoofing or replay | HMAC-SHA256 over `timestamp.body`, timestamp header, delivery ID, bounded retries | Receivers must verify the signature and reject stale timestamps |

## Deployment constraints

- One replica and one Uvicorn worker only. The SQLite credential registry and in-memory token-bucket limiter are not distributed coordination services.
- Use a tested local/block-backed filesystem. Do not assume SQLite WAL and rename/fsync semantics on arbitrary network filesystems.
- Terminate TLS at a trusted ingress or reverse proxy; keep the service and metrics endpoint off the public internet.
- Configure upload/concurrency/memory limits based on the deployment's actual memory and temporary-storage budgets.
- Keep signing keys outside the container image and persistent artifacts. Back up certificates and data separately from secrets where policy requires it.

## Out of scope / not yet proven

- A formal independent penetration test or third-party audit.
- Multi-region or multi-replica consistency.
- Guaranteed global rate limits or immediate revocation across replicas.
- Formal verification of the transaction state machine.
- A clean-room CI result and container build; the full suite, Airflow provider tests, and image build must pass on the target platform before release.
- Webhook *delivery* guarantees; emission is best-effort and signed, not exactly-once.
