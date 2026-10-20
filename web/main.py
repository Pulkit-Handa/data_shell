"""Data Shell public demo web UI.

A small FastAPI application that serves a landing page, an interactive
playground (upload a small file, define schema + rules, run enforcement), and
a one-click demo runner. It shells out to the ``datashell`` CLI so what the
browser shows is byte-for-byte the same engine the CLI runs.

This is a *separate* application from the authenticated production API
(``datashell.server.app``) and must defend itself:

Execution topology
------------------
* **Staging** (temporary, deleted after every request outcome): uploaded
  contract and source files are copied in bounded chunks into a private
  per-request directory under ``settings.temp_dir`` (``TMPDIR``).
* **Delivery** (durable, sandbox-authorized): the CLI's ``--target`` is a
  unique file inside the caller's tenant-delivery namespace
  (``settings.get_tenant_delivery_dir("public-demo-<tenant>")``), which the
  production filesystem sandbox authorizes for delivery. System temporary
  directories are *not* delivery targets in production, so staging and
  delivery are deliberately different locations.

The user-supplied ``tenant`` form field is validated against a strict pattern
and then namespaced under ``public-demo-``; it never becomes an unchecked
directory path. Delivery targets and quarantine paths declared inside
user-supplied contracts are replaced/dropped and re-derived server-side.

Resource policy (all settings come from the core configuration)
---------------------------------------------------------------
* ``MAX_UPLOAD_SIZE_BYTES``        — per-source-file ceiling (Render: 50 MiB).
* ``MAX_REQUEST_BODY_BYTES``       — whole-request ceiling (upload + multipart
  overhead), enforced in ASGI *before* multipart parsing by counting streamed
  bytes, so it holds even when ``Content-Length`` is absent, malformed, or a
  lie.
* ``MAX_CONTROL_BODY_BYTES``       — ceiling for the contract document /
  interactive contract JSON.
* ``MAX_CONCURRENT_REQUESTS``      — bounded concurrent upload staging.
* ``MAX_CONCURRENT_RUNS``          — upper bound on simultaneous CLI
  executions (per worker process).
* ``RATE_LIMIT_REQUESTS_PER_MINUTE`` — per-client token-bucket rate limit.
* ``DATASHELL_DEMO_GLOBAL_RATE_LIMIT_REQUESTS_PER_MINUTE`` — a *global*,
  identity-independent process ceiling. Client identity is only trustworthy
  when the deployment can prove visitors cannot forge it (see below), so abuse
  is bounded by this ceiling even when the per-client key is spoofed.

Client identity and proxy trust
-------------------------------
``_client_identity`` returns the socket peer address by default. The
``X-Forwarded-For`` value is consulted **only** when
``DATASHELL_DEMO_TRUST_FORWARDED_HEADERS`` is explicitly enabled, and then the
*rightmost* hop (the one appended by the nearest proxy) is used. Do **not**
enable it — and do not treat the per-client limit as a security control — until
you have verified, from the real deployment, that a visitor cannot inject a
``X-Forwarded-For`` value that the proxy forwards unmodified. Behind an
unverified proxy the per-client limit is a fairness hint only; the global
ceiling and the request/concurrency limits are the actual protection. Both
uvicorn (``--forwarded-allow-ips``) and this flag must agree with that
verification.

The concurrency and rate limits are *process-local*: each uvicorn worker and
each replica enforces its own copy. Render is configured with one worker; the
blueprint's single instance therefore gives a single global limit. Scaling
workers or replicas multiplies the effective limit unless a shared backend is
introduced.

Storage quota and retention
---------------------------
Durable demo artifacts (delivered parquet, its lineage copy, quarantine
records, and audit certificates) are kept for a bounded window and under a
total size cap, so repeated public use cannot fill the instance disk even
though each individual request is bounded. ``_enforce_demo_storage_quota``
sweeps only the demo namespaces (``public-demo-*`` and ``datashell-demo``) plus
audit certificates whose recorded tenant is a demo tenant, oldest first; it
never touches API or other tenants' artifacts.

Demo-specific knobs: ``DATASHELL_DEMO_CLI_TIMEOUT_SECONDS`` (per enforcement
run), ``DATASHELL_DEMO_WALKTHROUGH_TIMEOUT_SECONDS`` (one-click demo),
``DATASHELL_DEMO_CAPACITY_WAIT_SECONDS`` (bounded wait for an execution slot
before returning 503), ``DATASHELL_DEMO_ARTIFACT_RETENTION_MINUTES`` and
``DATASHELL_DEMO_MAX_ARTIFACT_BYTES`` (artifact retention/cleanup policy), and
``DATASHELL_DEMO_GLOBAL_RATE_LIMIT_REQUESTS_PER_MINUTE``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from datashell.core.config import settings
from datashell.core.fileops import InsufficientStorageError, available_bytes, ensure_disk_headroom
from datashell.core.security import TokenBucketRateLimiter
from datashell.server.middleware import RequestBodyLimitMiddleware

logger = logging.getLogger("datashell.demo")

BASE_DIR = Path(__file__).resolve().parent
APP_ROOT = BASE_DIR.parent
TEMPLATES = BASE_DIR / "templates"


@asynccontextmanager
async def _lifespan(application: FastAPI):
    # Fresh bounded execution capacity per worker process. Documented as a
    # process-local limit, not a global one across replicas.
    application.state.cli_slots = asyncio.Semaphore(settings.max_concurrent_runs)
    yield


app = FastAPI(title="Data Shell Demo", version="1.0.2", redoc_url=None, lifespan=_lifespan)
app.state.cli_slots = asyncio.Semaphore(settings.max_concurrent_runs)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


_DEMO_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "no-referrer",
}


@app.middleware("http")
async def _demo_security_headers(request: Request, call_next):
    """Baseline hardening headers for the public demo.

    ``SAMEORIGIN`` (rather than ``DENY``) keeps the first-party playground
    usable when framed by the operator's own console, while still refusing
    third-party framing. ``Cache-Control: no-store`` applies to API JSON
    responses only; static assets and HTML pages keep framework defaults so
    browsers can cache them normally.
    """
    response = await call_next(request)
    for header, value in _DEMO_SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if request.url.path.startswith("/api/") and "cache-control" not in {
        key.lower() for key in response.headers.keys()
    }:
        response.headers["Cache-Control"] = "no-store"
    return response


# ----------------------------------------------------------------- demo policy


def _positive_int_env(name: str, default: int) -> int:
    """Read, validate, and fail fast on a demo timeout/capacity knob."""
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer (got {raw!r})") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be a positive integer (got {value})")
    return value


def _bool_env(name: str, default: bool) -> bool:
    """Read a strict boolean knob; unrecognized values fail fast at boot."""
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"{name} must be a boolean (got {raw!r})")


DEMO_CLI_TIMEOUT_SECONDS = _positive_int_env("DATASHELL_DEMO_CLI_TIMEOUT_SECONDS", 300)
DEMO_WALKTHROUGH_TIMEOUT_SECONDS = _positive_int_env("DATASHELL_DEMO_WALKTHROUGH_TIMEOUT_SECONDS", 300)
DEMO_CAPACITY_WAIT_SECONDS = _positive_int_env("DATASHELL_DEMO_CAPACITY_WAIT_SECONDS", 5)

# Durable-artifact retention/cleanup policy. A public demo accumulates delivered
# parquet, quarantine records and audit certificates for the life of the
# container; without a quota, repeated use fills the disk even though every
# individual request is bounded. Oldest artifacts are evicted first.
DEMO_ARTIFACT_RETENTION_MINUTES = _positive_int_env("DATASHELL_DEMO_ARTIFACT_RETENTION_MINUTES", 120)
DEMO_MAX_ARTIFACT_BYTES = _positive_int_env("DATASHELL_DEMO_MAX_ARTIFACT_BYTES", 512 * 1024 * 1024)

# Identity-independent process ceiling. Client identity may be unforgeable only
# when the deployment has verified its proxy; this ceiling bounds abuse even
# when the per-client key can be spoofed by rotating X-Forwarded-For.
DEMO_GLOBAL_RATE_LIMIT_RPM = _positive_int_env(
    "DATASHELL_DEMO_GLOBAL_RATE_LIMIT_REQUESTS_PER_MINUTE",
    max(settings.rate_limit_requests_per_minute * 10, 600),
)

# Off by default: trust the client-supplied X-Forwarded-For value only after the
# operator has verified the deployment's proxy cannot forward a forged value.
DEMO_TRUST_FORWARDED_HEADERS = _bool_env("DATASHELL_DEMO_TRUST_FORWARDED_HEADERS", False)

# The CLI's structured output is captured to disk and read back with a hard
# cap, so a misbehaving run can never exhaust process memory through stdout.
MAX_CLI_OUTPUT_BYTES = 1024 * 1024

# Staging copies move uploads in bounded chunks; 1 MiB keeps peak per-request
# buffer use trivial while staying efficient for 50 MiB files.
STAGING_CHUNK_BYTES = 1024 * 1024

# Formats the engine's ingestion tier actually accepts (CSV, JSONL/NDJSON,
# Parquet). The staged filename is always "source.<ext>" — the client-supplied
# filename never determines a filesystem path.
ALLOWED_SOURCE_SUFFIXES = frozenset({".csv", ".jsonl", ".ndjson", ".parquet"})

# User tenants must be simple identifiers; anything else is rejected before it
# can influence storage layout (defense in depth on top of the engine's
# tenant_storage_key mapping).
TENANT_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")

# Public demo executions never share a namespace with API tenants.
DEMO_TENANT_NAMESPACE = "public-demo"

# The token bucket is keyed by client address and bounded (idle entries are
# evicted by TokenBucketRateLimiter). Process-local: per worker, per instance.
# This per-client bucket is a *fairness* control, not a security boundary: the
# client key is spoofable unless the deployment has verified its proxy.
demo_rate_limiter = TokenBucketRateLimiter(settings.rate_limit_requests_per_minute)

# Identity-independent process ceiling. Rotating a spoofed client key cannot
# evade this bucket, so it bounds abuse of the unauthenticated demo regardless
# of whether X-Forwarded-For is trustworthy. Process-local, like the above.
demo_global_rate_limiter = TokenBucketRateLimiter(DEMO_GLOBAL_RATE_LIMIT_RPM)


def _client_identity(request: Request) -> str:
    """Best-effort caller identity for per-client rate limiting.

    Returns the socket peer address by default. The client-supplied
    ``X-Forwarded-For`` value is used only when
    ``DATASHELL_DEMO_TRUST_FORWARDED_HEADERS`` is explicitly enabled, and then
    only the *rightmost* hop — the value appended by the nearest proxy — is
    taken, so a client that prepends forged hops cannot choose its own key.
    Even the rightmost hop is trustworthy only if the proxy overwrites or
    appends the header; that is a deployment fact this code cannot assume.
    """
    if DEMO_TRUST_FORWARDED_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _rate_limit_or_raise(request: Request) -> None:
    # Per-client fairness first, then the identity-independent ceiling. Both
    # must pass; a spoofed per-client key still consumes the global budget.
    if not demo_rate_limiter.allow(_client_identity(request)):
        raise HTTPException(
            status_code=429,
            detail="Too many demo requests from your address. Wait a moment and try again.",
            headers={"Retry-After": "5"},
        )
    if not demo_global_rate_limiter.allow("global"):
        logger.warning("demo global rate ceiling reached; rejecting request")
        raise HTTPException(
            status_code=429,
            detail="The demo is receiving too many requests right now. Wait a moment and try again.",
            headers={"Retry-After": "5"},
        )


async def _acquire_cli_slot() -> None:
    """Bounded wait for a CLI execution slot; 503 when capacity stays exhausted."""
    slots: asyncio.Semaphore = app.state.cli_slots
    try:
        await asyncio.wait_for(slots.acquire(), timeout=DEMO_CAPACITY_WAIT_SECONDS)
    except (TimeoutError, asyncio.TimeoutError):
        raise HTTPException(
            status_code=503,
            detail="The demo is at capacity right now. Please retry shortly.",
            headers={"Retry-After": "5"},
        )


def _release_cli_slot() -> None:
    app.state.cli_slots.release()


# ----------------------------------------------------------- request limiting

# Starlette spools multipart bodies before endpoint code runs, so oversized
# requests must be rejected *before* multipart parsing. This middleware (the
# same one the production API uses) rejects oversized declared Content-Length
# up front and counts streamed bytes for chunked/misleading bodies, then caps
# how many requests may buffer/stage concurrently.
class DemoBodyLimitMiddleware(RequestBodyLimitMiddleware):
    """Request-body and staging-concurrency limits for the public demo."""

    def __init__(self, inner_app):
        super().__init__(
            inner_app,
            max_body_bytes=settings.max_request_body_bytes,
            max_concurrent_requests=settings.max_concurrent_requests,
            max_control_body_bytes=settings.max_control_body_bytes,
        )


app.add_middleware(DemoBodyLimitMiddleware)


# ------------------------------------------------------------------ staging


class _StagingError(Exception):
    """A controlled staging failure mapped to a specific HTTP error."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def _stage_upload(upload: UploadFile, dest: Path, *, max_bytes: int, label: str) -> int:
    """Copy an upload to ``dest`` in bounded chunks.

    Never reads the whole part into memory, never lets the client filename
    determine the destination path, enforces the per-file ceiling while
    streaming, and applies private (0600) file permissions.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(dest, flags, 0o600)
    except OSError as exc:
        raise _StagingError(507, "Demo storage is unavailable to stage uploads.") from exc

    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await upload.read(STAGING_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise _StagingError(
                        413,
                        f"The {label} exceeds the configured limit of {max_bytes:,} bytes.",
                    )
                try:
                    out.write(chunk)
                except OSError as exc:
                    raise _StagingError(
                        507, "Demo storage ran out of space while staging the upload."
                    ) from exc
                # Yield so concurrent requests can make progress while staging.
                await asyncio.sleep(0)
    except OSError as exc:
        raise _StagingError(507, "Demo storage failed while staging the upload.") from exc
    return total


def _validated_source_suffix(filename: Optional[str]) -> str:
    """Map the uploaded filename to a safe staged suffix (never the raw name)."""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_SOURCE_SUFFIXES:
        allowed = ", ".join(sorted(ALLOWED_SOURCE_SUFFIXES))
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported source format. The demo accepts files with these extensions: {allowed}.",
        )
    return suffix


def _preflight_disk(source_bytes: int) -> None:
    """Refuse to start when staging or delivery obviously cannot fit."""
    for root in (settings.temp_dir, settings.output_dir):
        free = available_bytes(root)
        if free is not None and source_bytes > free:
            raise _StagingError(
                507,
                "The demo does not have enough free storage for this upload right now. Try a smaller file.",
            )
    try:
        ensure_disk_headroom(
            source_bytes,
            multiplier=settings.disk_headroom_multiplier,
            root=settings.output_dir,
        )
    except InsufficientStorageError as exc:
        raise _StagingError(
            507,
            "The demo does not have enough free storage to enforce this contract safely. Try a smaller file.",
        ) from exc


# ------------------------------------------------------- delivery namespace


def _demo_tenant_id(raw_tenant: str) -> str:
    """Validate the user tenant and namespace it into the demo's own prefix."""
    tenant = (raw_tenant or "").strip()
    if not TENANT_PATTERN.fullmatch(tenant):
        raise HTTPException(
            status_code=422,
            detail="tenant must be 1-63 characters of lowercase letters, digits, '_' or '-'.",
        )
    return f"{DEMO_TENANT_NAMESPACE}-{tenant}"


def _demo_delivery_target(demo_tenant: str) -> Path:
    """An explicitly authorized, collision-free delivery target for one run."""
    try:
        tenant_dir = settings.get_tenant_delivery_dir(demo_tenant)
    except RuntimeError as exc:
        # Symlinked or escaping tenant roots are refused by the engine's
        # symlink-safe storage mapping; surface a controlled error.
        logger.error("demo tenant delivery namespace unavailable: %s", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail="Demo storage is misconfigured; delivery is unavailable right now.",
        ) from exc
    # Unique per run: concurrent or repeated runs from users sharing a tenant
    # value can never overwrite each other's delivered artifacts.
    return tenant_dir / f"demo_{uuid.uuid4().hex}.parquet"


# ------------------------------------------------- demo storage quota
#
# The public demo durably preserves delivered parquet, quarantine records and
# audit certificates. Each request is individually bounded, but without a
# quota repeated use would slowly fill the instance disk. These helpers sweep
# *only* the demo namespaces, oldest artifact first, so API and other-tenant
# artifacts are never touched.

_demo_quota_lock = threading.Lock()


def _is_demo_tenant_key(key: str) -> bool:
    """True for storage keys that belong to the public demo namespaces."""
    return key == "datashell-demo" or key.startswith(f"{DEMO_TENANT_NAMESPACE}-")


def _audit_certificate_is_demo(path: Path) -> bool:
    """Identify a demo audit certificate by the tenant recorded inside it.

    Audit certificates live in one flat directory shared with the API, so the
    filename is not enough. The file is read under a hard size cap and any
    unreadable/oversized/unparseable file is treated as *not* ours (never
    deleted).
    """
    try:
        if path.stat().st_size > 256 * 1024:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    tenant = data.get("tenant_id") if isinstance(data, dict) else None
    if not isinstance(tenant, str) or not tenant:
        return False
    try:
        key = settings.tenant_storage_key(tenant)
    except ValueError:
        return False
    return _is_demo_tenant_key(key)


def _iter_demo_artifact_files() -> List[Path]:
    """Every durable artifact owned by the demo namespaces (files only)."""
    files: List[Path] = []
    for tenants_root in (
        settings.output_dir / "tenants",
        settings.quarantine_dir / "tenants",
    ):
        try:
            if not tenants_root.is_dir():
                continue
            children = list(tenants_root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or not _is_demo_tenant_key(child.name):
                continue
            try:
                files.extend(p for p in child.rglob("*") if p.is_file())
            except OSError:
                continue
    try:
        if settings.audit_dir.is_dir():
            for cert in settings.audit_dir.glob("*_audit_certificate.json"):
                if _audit_certificate_is_demo(cert):
                    files.append(cert)
    except OSError:
        pass
    return files


def _safe_unlink(path: Path) -> int:
    """Delete one artifact, returning its size on success and 0 otherwise."""
    try:
        size = path.stat().st_size
        path.unlink()
        return size
    except OSError:
        return 0


def _enforce_demo_storage_quota() -> None:
    """Evict oldest demo artifacts beyond the retention window or size cap.

    Called at the start of every demo request, before that request has produced
    any artifact, so a run's own fresh outputs are never candidates: they are the
    newest by mtime and the age window keeps them well clear of the deadline.
    The sweep is best-effort and never raises, so a cleanup problem cannot fail an
    otherwise successful request. A non-blocking lock keeps concurrent requests
    from sweeping at the same time.
    """
    if not _demo_quota_lock.acquire(blocking=False):
        return
    try:
        now = time.time()
        retention_seconds = DEMO_ARTIFACT_RETENTION_MINUTES * 60
        entries: List[tuple] = []
        for path in _iter_demo_artifact_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
        entries.sort(key=lambda entry: entry[0])  # oldest first

        evicted_bytes = 0
        evicted_files = 0
        retained_bytes = 0

        # 1) age-based retention
        survivors: List[tuple] = []
        for mtime, size, path in entries:
            if now - mtime > retention_seconds:
                evicted_bytes += _safe_unlink(path)
                evicted_files += 1
            else:
                survivors.append((mtime, size, path))
                retained_bytes += size

        # 2) total-bytes cap, still oldest first
        for mtime, size, path in survivors:
            if retained_bytes <= DEMO_MAX_ARTIFACT_BYTES:
                break
            deleted = _safe_unlink(path)
            if deleted:
                retained_bytes -= deleted
                evicted_bytes += deleted
                evicted_files += 1

        if evicted_files:
            logger.info(
                "demo storage quota: evicted %d artifact(s), %d bytes (retained %d bytes)",
                evicted_files,
                evicted_bytes,
                retained_bytes,
            )
    finally:
        _demo_quota_lock.release()


def _create_staging_dir(prefix: str) -> Path:
    """Private per-request staging directory under the configured temp root."""
    try:
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(settings.temp_dir)))
    except OSError as exc:
        raise _StagingError(
            507, "Demo storage is unavailable to stage uploads right now."
        ) from exc


def _prepare_contract(contract_bytes: bytes, delivery_target: Path) -> bytes:
    """Rewrite the user contract's delivery section server-side.

    Targets declared by the client are replaced with the sandbox-authorized
    tenant path, and ``quarantinePath`` is dropped so quarantine lands in the
    engine's tenant-isolated quarantine directory. Traversal attempts, symlink
    tricks, and cross-tenant paths in the contract therefore never reach the
    filesystem.
    """
    try:
        payload = json.loads(contract_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="The contract is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="The contract must be a JSON object.")
    spec = payload.get("spec")
    if not isinstance(spec, dict):
        raise HTTPException(status_code=422, detail="The contract is missing its 'spec' object.")
    spec["delivery"] = {
        "targets": [
            {
                "type": "PARQUET",
                "connectionRef": "public-demo",
                "path": str(delivery_target),
                "mode": "overwrite",
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------- CLI subprocess


class _CLITimeout(Exception):
    pass


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Terminate the CLI and any children it may have spawned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _run_cli(args: List[str], *, timeout: int, workdir: Path) -> subprocess.CompletedProcess:
    """Run ``python -m datashell.cli.main <args>`` as the common CLI boundary.

    * Argument-list invocation — user input is never interpolated into a shell.
    * A process-group-aware timeout, so a hung run (and its children) is
      terminated instead of accumulating.
    * stdout/stderr are captured to bounded files and read back with a hard
      cap, so subprocess output cannot exhaust memory.
    * The exit status is always part of the result and is checked by callers.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(APP_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    stdout_path = workdir / "cli_stdout.log"
    stderr_path = workdir / "cli_stderr.log"
    try:
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            proc = subprocess.Popen(
                [sys.executable, "-m", "datashell.cli.main", *args],
                cwd=str(APP_ROOT),
                env=env,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate_process_group(proc)
                raise _CLITimeout()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="The datashell CLI is unavailable in this environment.") from exc

    def _bounded(path: Path) -> bytes:
        try:
            with open(path, "rb") as fh:
                return fh.read(MAX_CLI_OUTPUT_BYTES)
        except OSError:
            return b""

    return subprocess.CompletedProcess(
        args, proc.returncode, _bounded(stdout_path), _bounded(stderr_path)
    )


def _parse_cli_json(stdout: bytes) -> Optional[Dict[str, Any]]:
    """Defensively parse the CLI's structured output."""
    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _public_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Strip absolute server paths from the browser-facing payload.

    The frontend renders target/quarantine/certificate locations; artifact
    basenames keep that surface useful without disclosing internal layout.
    """
    public = dict(payload)
    for key in ("target_path", "quarantine_path", "audit_path"):
        value = public.get(key)
        if isinstance(value, str) and value:
            public[key] = Path(value).name
        elif key == "target_path" and value is None:
            public[key] = None
    # Absolute staging/workspace locations are server-internal.
    public.pop("workspace", None)
    return public


def _interpret_enforcement_result(
    result: subprocess.CompletedProcess, *, delivery_target: Path, tenant_dir: Path
) -> Dict[str, Any]:
    """Map CLI exit status and output onto the demo's HTTP contract.

    * exit 2  -> contract policy rejection (HTTP 422, never a fake success and
      never an internal error).
    * other nonzero -> engine failure (HTTP 500, generic detail).
    * exit 0  -> the JSON summary must validate *and* the delivered target and
      certificate must exist inside the tenant namespace before success is
      reported.
    """
    payload = _parse_cli_json(result.stdout)

    if result.returncode == 2:
        detail = "The feed was rejected by the contract policy gate. No rows were delivered."
        if payload:
            held = payload.get("held_quality")
            refused = payload.get("refused_structural")
            if isinstance(held, int) and isinstance(refused, int):
                detail = (
                    f"The feed was rejected by the contract policy gate "
                    f"({refused:,} refused, {held:,} held, 0 delivered)."
                )
        logger.info("demo policy rejection: %s", detail)
        raise HTTPException(status_code=422, detail=detail)

    if result.returncode != 0:
        # Bounded, server-side-only diagnostics; the browser gets a generic
        # message with no paths, traces, or upload contents.
        logger.error(
            "demo CLI run failed (exit %s): %s",
            result.returncode,
            (result.stderr or result.stdout or b"").decode("utf-8", errors="replace")[:500],
        )
        raise HTTPException(
            status_code=500,
            detail="The enforcement engine failed to run this contract. Please retry or use a simpler contract.",
        )

    if payload is None:
        logger.error("demo CLI emitted unparseable output: %s", result.stdout[:200])
        raise HTTPException(
            status_code=500,
            detail="The enforcement engine returned an unreadable result. Please retry.",
        )

    status = payload.get("status")
    run_id = payload.get("run_id")
    target_path = payload.get("target_path")
    audit_path = payload.get("audit_path")
    if status != "LOADED" or not isinstance(run_id, str) or not run_id:
        logger.error("demo CLI exit 0 without a loaded run: status=%r run_id=%r", status, run_id)
        raise HTTPException(
            status_code=500,
            detail="The enforcement engine finished without delivering a result. Please retry.",
        )

    # Confirm the artifacts the CLI reports actually exist and that the
    # delivered target is inside the authorized tenant namespace.
    resolved_target = Path(str(target_path)).resolve() if target_path else None
    if resolved_target is None or not resolved_target.is_file():
        logger.error("demo CLI reported no delivered artifact: %r", target_path)
        raise HTTPException(
            status_code=500,
            detail="The delivered artifact could not be verified. Please retry.",
        )
    if not resolved_target.is_relative_to(tenant_dir):
        logger.error("demo CLI delivered outside the tenant namespace: %r", resolved_target)
        raise HTTPException(
            status_code=500,
            detail="The delivered artifact could not be verified. Please retry.",
        )
    if resolved_target != delivery_target.resolve():
        logger.error("demo CLI delivered to an unexpected path: %r", resolved_target)
        raise HTTPException(
            status_code=500,
            detail="The delivered artifact could not be verified. Please retry.",
        )
    if not isinstance(audit_path, str) or not Path(audit_path).is_file():
        logger.error("demo CLI reported no audit certificate: %r", audit_path)
        raise HTTPException(
            status_code=500,
            detail="The run certificate could not be verified. Please retry.",
        )

    return _public_payload(payload)


# ------------------------------------------------------- enforcement flow


async def _enforce_upload(
    contract_upload: Optional[UploadFile],
    contract_form: Optional[str],
    source: UploadFile,
    raw_tenant: str,
) -> Dict[str, Any]:
    """Stage uploads, run the CLI, verify delivery, and always clean up.

    Staging files are removed on success, policy rejection, HTTP error,
    engine failure, and timeout. Durable delivery artifacts (the delivered
    parquet, its run-lineage copy, quarantine records, and the signed audit
    certificate) persist under the product's normal storage conventions.
    """
    demo_tenant = _demo_tenant_id(raw_tenant)
    suffix = _validated_source_suffix(source.filename)
    delivery_target = _demo_delivery_target(demo_tenant)
    tenant_dir = delivery_target.parent

    staging: Optional[Path] = None
    try:
        # Evict stale demo artifacts before allocating new ones, so the quota
        # bounds long-run disk use and frees space for this request.
        _enforce_demo_storage_quota()
        staging = _create_staging_dir("datashell_web_")
        contract_path = staging / "contract.json"
        source_path = staging / f"source{suffix}"

        if contract_upload is not None:
            await _stage_upload(
                contract_upload, contract_path,
                max_bytes=settings.max_control_body_bytes, label="contract file",
            )
            contract_bytes = contract_path.read_bytes()
        else:
            contract_bytes = (contract_form or "").encode("utf-8")
            if len(contract_bytes) > settings.max_control_body_bytes:
                raise _StagingError(
                    413,
                    f"The contract exceeds the configured limit of "
                    f"{settings.max_control_body_bytes:,} bytes.",
                )
            contract_path.write_bytes(contract_bytes)

        source_bytes = await _stage_upload(
            source, source_path,
            max_bytes=settings.max_upload_size_bytes, label="source file",
        )
        _preflight_disk(source_bytes)

        contract_path.write_bytes(_prepare_contract(contract_bytes, delivery_target))

        await _acquire_cli_slot()
        try:
            result = await asyncio.to_thread(
                _run_cli,
                [
                    "run",
                    "--contract", str(contract_path),
                    "--source", str(source_path),
                    "--target", str(delivery_target),
                    "--tenant", demo_tenant,
                    "--json",
                ],
                timeout=DEMO_CLI_TIMEOUT_SECONDS,
                workdir=staging,
            )
        finally:
            _release_cli_slot()

        return _interpret_enforcement_result(
            result, delivery_target=delivery_target, tenant_dir=tenant_dir
        )
    except _StagingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except _CLITimeout:
        logger.error("demo CLI run timed out after %ss", DEMO_CLI_TIMEOUT_SECONDS)
        raise HTTPException(
            status_code=504,
            detail="The run timed out. Try a smaller file or a simpler contract.",
        )
    finally:
        # Reliable cleanup: uploads never outlive this request, whatever the
        # outcome. Durable artifacts live outside the staging directory and
        # are intentionally preserved.
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------- endpoints


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "service": "datashell-demo"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(TEMPLATES / "index.html", media_type="text/html")


@app.get("/playground")
async def playground() -> FileResponse:
    return FileResponse(TEMPLATES / "playground.html", media_type="text/html")


@app.post("/api/demo/run")
async def run_demo(request: Request) -> JSONResponse:
    """Run the self-contained ``datashell demo --json`` walkthrough."""
    _rate_limit_or_raise(request)
    _enforce_demo_storage_quota()
    await _acquire_cli_slot()
    try:
        staging = _create_staging_dir("datashell_webd_")
        try:
            result = await asyncio.to_thread(
                _run_cli, ["demo", "--json"],
                timeout=DEMO_WALKTHROUGH_TIMEOUT_SECONDS, workdir=staging,
            )
        except _CLITimeout:
            logger.error("demo walkthrough timed out after %ss", DEMO_WALKTHROUGH_TIMEOUT_SECONDS)
            raise HTTPException(status_code=504, detail="The demo walkthrough timed out. Please retry.")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    except _StagingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    finally:
        _release_cli_slot()

    if result.returncode != 0:
        logger.error(
            "demo walkthrough failed (exit %s): %s",
            result.returncode,
            (result.stderr or b"").decode("utf-8", errors="replace")[:500],
        )
        if result.returncode == 2:
            raise HTTPException(status_code=422, detail="The demo walkthrough was refused by policy.")
        raise HTTPException(status_code=500, detail="The demo walkthrough failed. Please retry.")

    payload = _parse_cli_json(result.stdout)
    if payload is None or payload.get("status") not in ("LOADED", "REJECTED"):
        logger.error("demo walkthrough emitted unparseable output: %s", result.stdout[:200])
        raise HTTPException(status_code=500, detail="The demo walkthrough returned an unreadable result.")
    return JSONResponse(_public_payload(payload))


@app.post("/api/enforce")
async def enforce(
    request: Request,
    contract: UploadFile = File(...),
    source: UploadFile = File(...),
    tenant: str = Form(default="default-tenant"),
) -> JSONResponse:
    """Enforce a user-supplied contract against a small uploaded file."""
    _rate_limit_or_raise(request)
    return JSONResponse(await _enforce_upload(contract, None, source, tenant))


@app.post("/api/enforce/interactive")
async def enforce_interactive(
    request: Request,
    source: UploadFile = File(...),
    tenant: str = Form(default="demo-tenant"),
    contract_json: str = Form(...),
) -> JSONResponse:
    """Enforce a contract the playground built from the user's form input."""
    _rate_limit_or_raise(request)
    return JSONResponse(await _enforce_upload(None, contract_json, source, tenant))
