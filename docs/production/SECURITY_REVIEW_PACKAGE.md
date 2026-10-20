# Data Shell — Independent Security Review Package

**Version:** 1.0
**Date:** 2026-10-20
**Branch:** `production_hardening`
**Purpose:** everything an independent, qualified reviewer needs to assess the
security posture of this release.

> **This package is preparation, not a review.** The local coding agent that
> assembled it is not an independent reviewer and does not claim to be. The
> gate "independent security review" remains **OPEN** until a reviewer who is
> independent of the implementation performs the review described in § 9 and
> issues a written finding. Nothing in this document substitutes for that.

---

## 1. System under review

Data Shell is a contract-driven boundary-enforcement gateway. It accepts
tabular partner feeds (CSV, Parquet, JSONL) over an authenticated HTTP API or
the CLI, validates them against a tenant-scoped contract, quarantines
non-conforming records, publishes conforming records, and records a signed
audit certificate plus a durable ledger entry for every terminal run.

**Supported deployment topology (the scope of this review):**

| Dimension | Value |
|---|---|
| Topology | One Linux host, one application replica/process, one worker |
| Tenancy | One customer / one tenant for the first pilot |
| Concurrency | One active processing job; request-level concurrency bounded by `MAX_CONCURRENT_REQUESTS` (default 2) |
| Formats | CSV, Parquet, JSONL/NDJSON |
| Max file size | Provisional 1 GiB (see `docs/production/BENCHMARK_PROCEDURE.md`) |
| Storage | Local POSIX filesystem with `fcntl.flock` and atomic `os.replace` |
| Signing | Ed25519 recommended; HMAC-SHA256 for an agreed internal trust model |
| Billing | Dry-run only; no money movement |
| Authentication | API key + role/capability RBAC; no SSO/SCIM |

Multi-replica operation is **not** supported: the credential registry is a
local SQLite file and the rate limiter is process-local.

---

## 2. Architecture and trust boundaries

```
  [ Producer / partner / CLI ]
            |
            | HTTPS (TLS terminated at a trusted ingress)
            v
  +---------------------------------------------------+
  | Data Shell gateway (single process)                |
  |  +---------------------------------------------+  |
  |  | Middleware: body limit, concurrency cap,      |  |
  |  | preflight auth, correlation id, security hdrs |  |
  |  +---------------------------------------------+  |
  |  | Routes: /api/v1/* (capability + tenant scoped) |  |
  |  +---------------------------------------------+  |
  |  | Pipeline: parse -> structural -> semantic ->    |  |
  |  | footing -> publication gate                     |  |
  |  +---------------------------------------------+  |
  |  | Storage: tenant-scoped dirs under output/       |  |
  |  | Ledger: SQLite (run history, quarantine, usage) |  |
  |  +---------------------------------------------+  |
  +---------------------------------------------------+
            |
            v
  [ Published artifacts + signed certificates + ledger ]
```

**Trust boundaries (each is an attack surface):**

| # | Boundary | Crossing data | Control |
|---|---|---|---|
| B1 | Internet → gateway | Untrusted multipart bodies | `MAX_REQUEST_BODY_BYTES`, `MAX_CONTROL_BODY_BYTES`, `MAX_UPLOAD_SIZE_BYTES`, preflight auth, concurrency cap |
| B2 | API key → principal | Credential material | SHA-256 fingerprint registry, per-tenant + per-capability checks, revocation |
| B3 | Request → filesystem | Paths derived from user input | `FilesystemSandbox` (root jail, reserved-namespace denial, cross-tenant denial, `..` rejection) |
| B4 | Uploaded bytes → parser | Malformed/adversarial data | Format allowlist, structural casting, refusal not crash, size ceilings |
| B5 | Contract → enforcement | Contract semantics | Contract validation, version pinning, digest binding in the certificate |
| B6 | Process → durable storage | Artifacts, journals, outbox | `fcntl.flock` journal, fsync, atomic rename, generation-pointer commit point, rollback on crash |
| B7 | Process → ledger | Forensic state | SQLite, tenant-scoped rows, atomic run/quarantine/usage transaction |
| B8 | Gateway → operator | Secrets, keys | Env/secret-manager injection, placeholder and entropy rejection, no secrets in logs/artifacts |
| B9 | Gateway → external services | Settlement, webhooks, billing | Dry-run billing by default, HMAC-signed webhooks, draft-invoice-only Stripe adapter |

---

## 3. Authentication and authorization matrix

| Route / operation | Auth | Capability | Tenant scope | Notes |
|---|---|---|---|---|
| `GET /health`, `GET /ready` | none | — | — | Liveness/readiness only; no tenant data |
| `GET /metrics` | API key | `metrics:read` | — | Prometheus scrape; must not be public |
| `POST /api/v1/enforce` | API key | `pipeline:enforce` | request tenant | Body limit, contract resolution, sandboxed target |
| `GET /api/v1/contracts` | API key | `contracts:read` | tenant | |
| `POST /api/v1/contracts` | API key | `contracts:write` | tenant | Control-body limit applies |
| `GET /api/v1/ledger/*` | API key | `audit:read` | tenant | Every query filtered by tenant |
| `GET /api/v1/settlements/*` | API key | `settlements:read` | tenant | |
| `POST /api/v1/settlements/*` | API key | `settlements:write` | tenant | Idempotency key required for mutations |
| `POST /api/v1/settlements/*/billing-preview` | API key | `billing:manage` | tenant | `Idempotency-Key` header required |
| `POST /api/v1/admin/keys` | API key | `admin:manage` | — | Issues managed credentials |

**Roles and capabilities** (`datashell/core/security.py`):

| Role | Capabilities |
|---|---|
| `ADMIN` | all, including `admin:manage` |
| `DATA_ENGINEER` | contracts read/write, pipeline enforce, audit read, metrics read, settlements read/write, billing manage |
| `PRODUCER` | contracts read, pipeline enforce |
| `AUDITOR` | contracts read, audit read, metrics read, settlements read |

**Rules the reviewer should test:**

1. Every protected route rejects a missing, malformed, unknown, or revoked key
   with `401`, and a key lacking the capability with `403`.
2. A tenant mismatch returns `403` and discloses nothing about the other
   tenant's objects (no existence side channel).
3. `POST /api/v1/admin/keys` is unreachable without `admin:manage`.
4. Query-parameter credentials are not accepted (header-only), so keys cannot
   leak into access logs.
5. Authentication cannot be disabled in production — `settings.auth_enabled`
   is `false` in production raises `500` rather than serving unauthenticated.

---

## 4. Sensitive-data flow

| Data | Where it lives | Protection |
|---|---|---|
| Partner feed content | Upload staging (temporary), quarantine artifacts, delivered artifacts | `0600` files, `0700` directories, sandboxed roots, tenant-scoped paths |
| API keys | Never stored raw — SHA-256 fingerprint in `security_registry.sqlite3` | Fingerprint only; the raw key is shown once at creation |
| Signing secrets | Environment / secret manager | Never written to artifacts, backups, or logs; placeholder + entropy rejection at startup |
| Quarantined records | `output/quarantine/` | Same as delivered artifacts; retained per `DATASHELL_HISTORY_RETENTION_DAYS` |
| Ledger history | `output/datashell_history.db` | File permissions, tenant-scoped queries, not served publicly |
| Logs | stdout JSON | Structured; request IDs, run IDs, tenant IDs — **no row content, no secrets** |

**Redaction check for the reviewer:** confirm that no log line, error body, or
metric label contains feed row content, an API key, or a signing secret.

---

## 5. Cryptographic evidence model

**What a valid certificate proves:**
- The bound artifacts have not been modified since signing.
- The signing key was in the possession of the signer.
- The reconciliation arithmetic footed at run time (the run recorded
  `rows_read = delivered + held + refused`).

**What it does NOT prove:**
- That the input data was truthful.
- That the computation was semantically correct for the business.
- That the contract was the correct business contract.
- Non-repudiation under a shared-secret (HMAC) key — **any party able to
  verify with an HMAC key can also forge**.

**Bound fields** (`datashell/pipeline/artifacts.py`): source payload digest,
delivered payload digest, quarantine payload digest, contract digest, Merkle
root over those four, run ID, tenant ID, timestamp, contract name/version,
delivered status, delivered file path, run artifact path, quarantine artifact
digests, plus the signature (algorithm, key ID, value) and the
`certificate_digest` recorded in the ledger.

**Publication commit point:** the `.generation.json` pointer next to the
delivery target. It is swapped by exactly one atomic `os.replace` after every
artifact of the new generation is durable, and it is repaired on startup
recovery from the durable journal.

**Independent verification:** `datashell verify <certificate>` verifies the
signature and every artifact digest, and additionally reports whether the
certificate is the currently *committed* generation. With Ed25519, a third
party holding only the public key can verify.

**Key lifecycle:** `DATASHELL_ED25519_KEYRING` retains superseded public keys
so historical certificates stay verifiable; `DATASHELL_HMAC_KEYRING` does the
same for HMAC. Private keys must be backed up separately from artifacts.

**Reviewer tests to run:** substitute every bound field (source digest,
delivered digest, quarantine digest, contract digest, run ID, tenant ID,
timestamp, key ID, signature) and confirm each mutation fails verification;
verify with the wrong key; verify with an unknown key ID; verify a
certificate signed by a retired-but-retained key; confirm the Merkle leaf
order and root are tamper-evident.

---

## 6. Dependency and build information

| Item | Value |
|---|---|
| Runtime dependencies | `polars`, `duckdb`, `pyarrow`, `pydantic`, `fastapi`, `uvicorn`, `python-multipart`, `prometheus-client`, `httpx`, `numpy`, `cryptography` (pinned in `requirements.lock`, hash-verified) |
| Python | `>=3.11` (CI matrix 3.11 / 3.12 / 3.13) |
| Build backend | `setuptools` (hash-pinned in `requirements-build.lock`) |
| Container | `Dockerfile` (non-root UID 10001, read-only root filesystem, healthcheck) |
| SBOM | CycloneDX generated from `requirements.lock` in CI |

**Locally executed scans (re-run these; do not trust the summary alone):**

```bash
pip-audit -r requirements.lock -r requirements-dev.lock -r requirements-build.lock
bandit -q -r datashell web -c .bandit -ll
python -m build --no-isolation --outdir dist
python scripts/verify_distribution_contents.py --dist-dir dist
```

---

## 7. Existing security tests (map to the review)

| Area | Test file | What it proves |
|---|---|---|
| Certificate verification fails closed | `tests/test_artifact_verifier_security.py` | A tampered/invalid signature never dereferences artifact paths; a validly signed certificate cannot make the verifier read outside the storage roots |
| Authz / capabilities | `tests/test_security_audit.py` | Route-level capability enforcement |
| Path traversal / sandbox | `tests/test_boundary_hardening.py` | Output-root traversal, reserved-namespace writes, cross-tenant delivery paths are refused |
| Tenant isolation | `tests/test_tenant_isolation.py` | Cross-tenant access denied for runs, quarantine, usage; admin cross-tenant access allowed |
| Key lifecycle | `tests/test_ed25519_signing.py` | Asymmetric signing/verification, key rotation |
| Secret handling | `tests/test_production_runtime_hardening.py` | Production refuses missing/placeholder/weak secrets; wildcard CORS and proxy trust refused |
| Placeholder rejection | `tests/test_cli_verify_roundtrip.py` | The published `.env.example` values cannot boot production |
| Request limits | `tests/test_large_file_streaming.py`, `tests/test_feed_controls.py` | Body/upload ceilings and streaming bounds |
| Failure injection | `tests/test_fault_injection.py` | Crash points around publication, recovery, concurrency |
| Publication coherence | `tests/test_publication_coherence.py` | Generation pointer is the commit point; recovery repairs a stale pointer; readers observe one generation |
| Ledger durability | `tests/test_ledger_outbox.py` | Pending ledger state, idempotent replay, no duplicate quarantine/usage, no fabricated delivery |

---

## 8. Known limitations the reviewer must weigh

1. **Single replica only.** The credential registry is a local SQLite file and
   the rate limiter is process-local. Two replicas would have divergent key
   revocations and independent rate limits.
2. **No external identity provider.** API keys only; no SSO, SCIM, or
   per-user identity. Revocation is manual via the admin API.
3. **Rate limiting is per process** and keyed by API key (and, for failed
   auth, by client IP). It resets on restart.
4. **The generation pointer is respected by the CLI verifier and by recovery,
   but an external consumer that reads a delivery target directly** without
   resolving the pointer can observe a superseded generation. The pointer
   contract is documented; adoption by external consumers is not enforced by
   the product.
5. **No independent penetration test** has been performed.
6. **Certificates do not prove data truthfulness** (see § 5).
7. **The settlement billing adapter is draft-invoice only**; the live Stripe
   path has never been exercised with real credentials.

---

## 9. What the independent review must cover

A qualified reviewer independent of this implementation should:

1. **Threat-model the deployed system** against § 2 and confirm the trust
   boundaries hold for the intended data sensitivity.
2. **Attempt cross-tenant access** on every route and object type, including
   the ledger, quarantine, certificates, settlement statements, and artifact
   paths.
3. **Attempt path traversal, symlink substitution, and TOCTOU** on the
   delivery, staging, quarantine, and audit paths.
4. **Attempt to bypass authorization**: capability confusion, role escalation,
   admin-endpoint reachability, credential-in-query-string.
5. **Fuzz the ingestion boundary**: malformed CSV/Parquet/JSONL, huge fields,
   deep nesting, schema drift, type coercion.
6. **Exercise the failure paths**: disk full, database unavailable, process
   kill at each publication boundary, partial writes, cleanup errors.
7. **Review the cryptographic evidence** against § 5 and attempt forgery with a
   public-key-only verifier.
8. **Review dependency and container posture**: pinned/hashed dependencies,
   image contents, non-root execution, read-only filesystem, exposed ports,
   CI permissions, secret handling.
9. **Attempt resource exhaustion**: concurrent uploads, slow clients,
   oversized bodies, retry storms.
10. **Report findings with severity, reproduction steps, and evidence.**

---

## 10. Reproduction instructions for the reviewer

```bash
git clone https://github.com/Pulkit-Handa/data_shell.git
cd data_shell
git checkout production_hardening
python3 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements-dev.lock
pip install --no-deps --no-build-isolation -e .
ENVIRONMENT=test python -m pytest -q
```

Individual suites:

```bash
python -m pytest tests/test_security_audit.py tests/test_tenant_isolation.py \
  tests/test_boundary_hardening.py tests/test_artifact_verifier_security.py -q
python -m pytest tests/test_fault_injection.py tests/test_publication_coherence.py \
  tests/test_ledger_outbox.py -q
```

Local stack:

```bash
docker compose up -d
curl -fsS http://127.0.0.1:8000/health
```

To point the gateway at an isolated storage root and secrets:

```bash
export ENVIRONMENT=production
export DATASHELL_API_KEY="<32+ char high-entropy value>"
export DATASHELL_HMAC_SECRET="<32+ char high-entropy value>"
export ALLOWED_ORIGINS=https://console.example.com
export FORWARDED_ALLOW_IPS=127.0.0.1
export DATASHELL_STORAGE_ROOT=/tmp/review
```

---

## 11. Reviewer output template

```
Reviewer:            <name / organisation>
Independence:        <statement>
Date:                <date>
Commit reviewed:     <git rev-parse HEAD>
Deployment reviewed: <image digest / environment>

Findings:
  ID | Severity | Affected component | Reproduction | Impact | Recommendation

Gate decision:       PASS / FAIL / PASS-WITH-EXCEPTIONS
Residual risks accepted: <list, with owner>
Signed:               <name, date>
```

Until a completed report exists, the release gate **"independent security
review"** is **BLOCKED**, and general production release is not approved.
