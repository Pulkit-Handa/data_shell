# Design Record — P0 Fixes and Release Engineering (v1.0.2)

**Status:** **HISTORICAL REFERENCE** — This document records design decisions made during the v1.0.2 workstream. The fixes are implemented and verified. For current verification evidence, see `docs/production/VERIFICATION_REPORT.md`.

Decisions made to close the three P0 defects and the release-engineering gaps, with the
alternatives that were rejected. `CHANGELOG.md` records *what* changed; this file records
*why*.

## P0-1 — The public demo could not succeed in production

**Defect.** In production, `datashell demo --json` delivered to a workspace path that the
sandbox refused as `UNSAFE_PATH`. The engine exited `1` and the demo returned HTTP `200`
with `status: FAILED`; the playwright screenshot only proved the HTTP call succeeded.

**Decision: integrate the demo with the sandbox; do not weaken the sandbox.**
Deliveries are redirected into storage roots the engine already authorizes:

| Context | Delivery | Audit | Quarantine |
|---|---|---|---|
| `web/main.py` (public demo) | `settings.get_tenant_delivery_dir("public-demo-<tenant>")` | `settings.audit_dir` | `settings.get_tenant_quarantine_dir(<ns>)` |
| `datashell demo`, production | `get_tenant_delivery_dir("datashell-demo")` | `settings.audit_dir` | `get_tenant_quarantine_dir("datashell-demo")` |
| `datashell demo`, development | unchanged workspace paths | unchanged | unchanged |

The web app derives `spec.delivery` server-side from the validated tenant namespace (never
from client input) and drops any client-supplied `quarantinePath`, so contract JSON can never
steer a write outside the tenant root. The CLI scopes its pipeline to `tenant_id` matching
the delivery namespace.

**Rejected alternatives.** (a) broad `TMPDIR` / `TEMP_DIR` authorization — reopens arbitrary
writes for every caller; (b) patching `FilesystemSandbox` — changes the security boundary for
all tenants to fix one caller; (c) only checking the exit code — a sandbox refusal would still
never deliver, so the demo would "succeed" while writing nothing.

**Failure reporting.** The demo now always checks the exit status *and* confirms the reported
artifact exists inside the tenant directory: exit `0` + valid JSON + artifact present → `200`;
exit `2` → `422`; exit `1`/malformed JSON/missing or out-of-namespace artifact → `500`;
timeout → `504`. Absolute paths are redacted to basenames. The engine's exit-code semantics
(`0` loaded, `1` failed, `2` rejected) are unchanged.

## P0-2 — Uploads were unbounded

**Decision: reuse the core limit settings instead of inventing demo-local ones.**
Sources are bounded by `max_upload_size_bytes`, control bodies by `max_control_body_bytes`,
the whole request (multipart overhead included) by `max_request_body_bytes` via a middleware
subclass; `max_concurrent_runs` bounds CLI executions (semaphore with a bounded wait) and
`max_concurrent_requests` bounds staging; `rate_limit_requests_per_minute` feeds a
process-local token bucket.

Uploads are staged in bounded 1 MiB chunks under `settings.temp_dir` into a per-request
`datashell_web_*` directory created with `0600` files and deleted in `finally` — never
`source.file.read()`, never a user-controlled filename as a path, extensions allowlisted.
Staging and delivery are separate directories; staging never outlives the request.

**Rejected alternatives.** Trusting `Content-Length` (client-controlled, bypasses); a single
whole-request limit (misses per-file and control-body bounds); `O_TMPFILE`-style anonymous
files (nice, but ties the fix to Linux specifics we do not need once names are generated).
Capacity is rejected with `503` + `Retry-After` after a bounded wait instead of an unbounded
queue; rate limiting returns `429` + `Retry-After`.

## P0-3 — Drift silently skipped zero-baseline metrics

**Decision: compare by metric kind, with explicit zero-baseline semantics.**

| Metric kind | Comparison | Threshold |
|---|---|---|
| Rate (`null_rate_*`) | absolute delta in proportion points: `abs(c−b)` | `DRIFT_MAX_RATE_ABS_DELTA` (default `0.10`) |
| Mean / sum / unknown | relative: `abs((c−b)/b) × 100.0` | `DRIFT_MAX_METRIC_DRIFT_PCT` (default `50.0` percent) |
| Volume | relative drop of delivered rows | `DRIFT_MAX_VOLUME_DROP_PCT` (default `30.0` percent) |

Zero-baseline rules: both near zero (ε = `1e-9`) → `0.0%`, passed; emergence from a
zero baseline → `100.0%` (`ZERO_BASELINE_EMERGENCE_PCT`), failed; vanished baseline/current
metric, non-finite or invalid value → explicit failed result; missing historical metrics →
passed "baseline established" (the minimum-sample gate stays dormant). The old code computed
`abs(c−b)/b` for everything,
which yields `nan`/`inf`/`0` at zero and caused the null-rate, mean and sum checks to be
suppressed — a regression that read as a clean run.

`metric_kind()` in `engines/metrics.py` is the single source of truth, shared by extraction
and comparison. `DriftCheckResult` and the certificate schema are unchanged; tenant isolation
is unchanged.

## Demo hardening follow-ups

A later review raised two issues that the first pass left implicit.

**Client-IP rate limiting was spoofable.** `Dockerfile.web` shipped
`--forwarded-allow-ips="*"`, so uvicorn populated `request.client` from the **leftmost**
`X-Forwarded-For` entry — the value a visitor controls — and `_client_identity()` keyed the
token bucket on it. A visitor could rotate the header per request and bypass the limit
entirely, so the demo's only admission control on the cheap path was void. Two layers were
changed so protection does not depend on an unverified proxy:

- The demo image now defaults `--forwarded-allow-ips` to `${FORWARDED_ALLOW_IPS:-127.0.0.1}`
  (matching the API image) and the app consults `X-Forwarded-For` only when
  `DATASHELL_DEMO_TRUST_FORWARDED_HEADERS=true`, taking the **rightmost** hop so a forged
  prefix cannot choose the key. Both default safe.
- An **identity-independent** process ceiling
  (`DATASHELL_DEMO_GLOBAL_RATE_LIMIT_REQUESTS_PER_MINUTE`) is applied in addition to the
  per-client bucket, so even a fully spoofed identity is bounded. The per-client bucket is
  now documented as a fairness control, not a security boundary, and
  `docs/production/OPERATIONS_RUNBOOK.md`
  gives the exact procedure an operator must run before enabling header trust.

Rejected: keeping `*` and merely documenting it (the limit would still be bypassable);
switching uvicorn to "trust nobody" and deleting the per-client bucket (loses fairness for
the common single-proxy deployment); trusting the rightmost hop unconditionally (only safe
if the proxy appends/overwrites — an assumption we cannot make from here).

**Durable demo artifacts had no quota.** delivered parquet, lineage copies, quarantine
records and audit certificates accumulated for the life of the container; individually
bounded requests still filled the disk. `_enforce_demo_storage_quota` now evicts oldest
first after `DATASHELL_DEMO_ARTIFACT_RETENTION_MINUTES` (default 120) or beyond
`DATASHELL_DEMO_MAX_ARTIFACT_BYTES` (default 512 MiB), scoped strictly to the demo
namespaces (`public-demo-*`, `datashell-demo`) plus audit certificates whose recorded
`tenant_id` is a demo tenant. It never touches API/other tenants' artifacts, never deletes
oversized or unparseable certificates, is serialized by a non-blocking lock, and never
raises — a cleanup problem cannot fail a request. The sweep runs before each run, so freed
space is available to the current request and the current run's fresh artifacts are never
candidates.

## Release engineering

- **Lockfiles resolve on Python 3.11 (lowest supported),** not on the developer's 3.14.
  Resolving on 3.14 pinned `numpy==2.5.4` (requires ≥3.12) into a lock meant for
  `requires-python = ">=3.11"`. Regeneration reuses existing pins and only drops what the
  interpreter cannot use, so the diff was `numpy==2.5.4 → 2.4.6` alone. Verified with
  `pip download --require-hashes --only-binary=:all: --python-version {311,312,313,314}`.
- **Install from the lockfiles with `--require-hashes`** in CI, the release job and both
  images. `requirements-build.lock` (`setuptools`, `wheel`, `build`) pins the PEP 517 build
  backend so `python -m build --no-isolation` / `pip wheel --no-build-isolation` never
  re-resolves an unconstrained backend from the network. The build lock is installed only in
  the throwaway `builder` stage, so it never ships.
- **Images are multi-stage and copy only `web/`** into the runtime stage, not `datashell/`;
  the engine is imported from the installed wheel, which the tests assert explicitly
  (`import datashell` → `site-packages`). This is the only way to satisfy "runs from the
  installed package" while the app source sits at `/app`.
- **SBOM from `requirements.lock`** (`cyclonedx-py requirements requirements.lock --pyproject
  pyproject.toml`) instead of `cyclonedx-py environment`: the latter inventories the *runner*
  (dev and tooling packages) rather than the shipped release set. The new form gives
  `data-shell 1.0.2` as root component plus exactly the 27 runtime packages.
- **Packaging allowlist.** The wheel is limited by `[tool.setuptools.packages.find]`
  (`datashell*`); `MANIFEST.in` explicitly includes code, legal files, lockfiles, `web/`,
  `tests/`, `examples/` and `scripts/`, and prunes `output/`, `data/`,
  `contracts_registry/`, caches and build artifacts. `scripts/verify_distribution_contents.py`
  fails the build otherwise. `requirements-build.lock` was added to `.gitignore`'s `*.lock`
  negations or it would have been silently uncommitted.
- **Identity vs. version.** Distribution `data-shell`, import package `datashell`, product
  "Data Shell" are distinct and all preserved. `scripts/check_release_metadata.py` (run by
  CI, the release job and `tests/test_release_metadata.py`) requires `pyproject.toml`,
  `datashell.__version__`, the FastAPI app version and the newest `CHANGELOG.md` entry to
  agree, the tag to be `v<version>`, every declared runtime dependency to be hash-pinned, and
  the changelog never to claim a live dependency was removed — which caught the false
  "removed `cryptography`" note (the package is the Ed25519 signing backend in
  `datashell/core/keys.py`).

## Demo deployment knobs

`render.yaml` now sets `ALLOWED_ORIGINS` (production validation runs at import time, so the
service will not boot without it) plus `MAX_CONCURRENT_RUNS=1`, a 300 s CLI/walkthrough
timeout, a 5 s capacity wait and 60 requests/minute. All of these bounds are **process-local**:
they constrain one worker and are not global quotas; a shared backend is required before
horizontal scaling.

## Verification

See `docs/production/VERIFICATION_REPORT.md` for the exact commands and
results. The externally gated items — live Render
deployment, backup/restore drill, load and soak testing, independent security review, and a
clean CI run on 3.11/3.12/3.13 — remain open and are listed as such.
