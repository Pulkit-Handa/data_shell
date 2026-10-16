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
9. Unauthenticated internet clients to the public demo/playground container (no API key), which shells out to the enforcement CLI.

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
| Unauthenticated demo abuse (upload floods, CPU/temp exhaustion) | Demo uploads are staged in bounded chunks and deleted per request; source, contract/control and total request-body limits are enforced before parsing; filenames are never used as paths and only allowlisted extensions are accepted; two process-local token buckets apply — a per-client fairness bucket and an identity-independent global ceiling; concurrent CLI runs are capped with a bounded wait and excess load gets `503`/`429` with `Retry-After` | Process-local limits bound one worker only; add a shared rate limiter and admission control before exposing the demo at scale |
| Rate-limit bypass via forged client identity | The per-client bucket is not a trust boundary: `X-Forwarded-For` is ignored unless `DATASHELL_DEMO_TRUST_FORWARDED_HEADERS=true`, the image defaults `--forwarded-allow-ips` to loopback (not `*`), and an identity-independent global ceiling bounds requests even when the client key is spoofed | Verify the deployment's proxy against the documented procedure before enabling header trust; keep the global ceiling as the real backstop |
| Demo storage exhaustion (durable artifacts accumulate) | Demo delivery, quarantine and audit artifacts are evicted oldest-first after an age window and under a total-size cap, scoped to the demo namespaces only; oversized/unparseable certificates and other tenants' artifacts are never deleted; a cleanup failure never fails a request | Size the cap for the instance volume; add external monitoring/alerting on disk usage |
| Demo masking a failed or rejected enforcement | The demo always checks the engine exit status and verifies the reported artifact exists inside the tenant delivery directory; engine failure (`1`) maps to `500`, policy rejection (`2`) to `422`, timeout to `504` — a non-zero run can never be reported as success | Covered by the demo API tests; re-verify per deployment |
| Demo bypassing the sandbox to reach arbitrary paths | The demo uses the same storage-root sandbox as every other run and delivers only into an isolated `public-demo-<tenant>` namespace under the tenant delivery root; no sandbox exemption or broad temp authorization is added | Deployment-specific review of storage roots |
| Silent drift suppression (undetected data-quality regression) | Metric drift is evaluated per metric kind with explicit zero-baseline handling; missing/invalid/non-finite history and vanished metrics are reported and fail rather than being skipped | Tune thresholds per feed; independent review of drift policy |

## Deployment constraints

- One replica and one Uvicorn worker only. The SQLite credential registry and in-memory token-bucket limiter are not distributed coordination services.
- Use a tested local/block-backed filesystem. Do not assume SQLite WAL and rename/fsync semantics on arbitrary network filesystems.
- Terminate TLS at a trusted ingress or reverse proxy; keep the service and metrics endpoint off the public internet.
- Configure upload/concurrency/memory limits based on the deployment's actual memory and temporary-storage budget. The public demo runs the same code path under fixed, conservative limits and must not be given a sandbox exemption.
- Keep signing keys outside the container image and persistent artifacts. Back up certificates and data separately from secrets where policy requires it.

## Out of scope / not yet proven

- A formal independent penetration test or third-party audit.
- Multi-region or multi-replica consistency.
- Guaranteed global rate limits or immediate revocation across replicas. The demo's rate limiting, staging concurrency and CLI-run slot are process-local bounds, not global quotas. Its per-client bucket is a fairness control, not a security boundary: it is trustworthy only after the deployment's proxy has been verified to prevent `X-Forwarded-For` spoofing (documented in `docs/OPERATIONS.md`); until then the identity-independent global ceiling is the protection.
- Formal verification of the transaction state machine.
- A clean-room CI result and container build; the full suite, Airflow provider tests, and image builds must pass on the target platform before release. The locks, distribution contents, container images and demo production path were exercised in a single authoring environment (see `PRODUCTION_READINESS.md` § "Current validation evidence"); the live Render deployment, load/soak tests and independent review remain open.
- Webhook *delivery* guarantees; emission is best-effort and signed, not exactly-once.
