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

The concurrency and rate limits are *process-local*: each uvicorn worker and
each replica enforces its own copy. Render is configured with one worker; the
blueprint's single instance therefore gives a single global limit. Scaling
workers or replicas multiplies the effective limit unless a shared backend is
introduced.

Demo-specific knobs: ``DATASHELL_DEMO_CLI_TIMEOUT_SECONDS`` (per enforcement
run), ``DATASHELL_DEMO_WALKTHROUGH_TIMEOUT_SECONDS`` (one-click demo), and
``DATASHELL_DEMO_CAPACITY_WAIT_SECONDS`` (bounded wait for an execution slot
before returning 503).
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


DEMO_CLI_TIMEOUT_SECONDS = _positive_int_env("DATASHELL_DEMO_CLI_TIMEOUT_SECONDS", 300)
DEMO_WALKTHROUGH_TIMEOUT_SECONDS = _positive_int_env("DATASHELL_DEMO_WALKTHROUGH_TIMEOUT_SECONDS", 300)
DEMO_CAPACITY_WAIT_SECONDS = _positive_int_env("DATASHELL_DEMO_CAPACITY_WAIT_SECONDS", 5)

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
demo_rate_limiter = TokenBucketRateLimiter(settings.rate_limit_requests_per_minute)


def _client_identity(request: Request) -> str:
    """Best-effort caller identity for rate limiting.

    uvicorn's proxy-header support (enabled in Dockerfile.web behind Render's
    router) populates ``request.client`` from X-Forwarded-For.
    """
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _rate_limit_or_raise(request: Request) -> None:
    if not demo_rate_limiter.allow(_client_identity(request)):
        raise HTTPException(
            status_code=429,
            detail="Too many demo requests from your address. Wait a moment and try again.",
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
    return {"status": "ok", "service": "data-shell-demo"}


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
