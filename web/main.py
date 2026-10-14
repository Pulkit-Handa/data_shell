"""Data Shell demo web UI.

A tiny FastAPI app that serves a landing page, an interactive playground
(upload a small file, define schema + rules, run enforcement), and a
one-click demo runner. It shells out to the ``datashell`` CLI so what the
browser shows is byte-for-byte the same engine the CLI runs.

Only the core runtime dependencies are required (FastAPI, Starlette, uvicorn,
python-multipart) — templates are plain static HTML, so no Jinja2 needed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
APP_ROOT = BASE_DIR.parent
TEMPLATES = BASE_DIR / "templates"

app = FastAPI(title="Data Shell Demo", version="1.0.2", redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _run_cli(args: List[str], *, timeout: int) -> subprocess.CompletedProcess:
    """Run ``python -m datashell.cli.main <args>`` and capture its output.

    Runs from the app root with the app root on ``PYTHONPATH`` so the CLI
    resolves whether it is pip-installed (Docker) or run from a checkout.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(APP_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        return subprocess.run(
            ["python", "-m", "datashell.cli.main", *args],
            cwd=str(APP_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="The run timed out. Try a smaller file.")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="The datashell CLI is not available in this environment.")


def _as_json(result: subprocess.CompletedProcess) -> Dict[str, Any]:
    out = result.stdout or result.stderr
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return {"raw": out, "returncode": result.returncode}


def _enforce_upload(contract_bytes: bytes, source: UploadFile, tenant: str, prefix: str) -> Dict[str, Any]:
    tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        cpath = tmpdir / "contract.json"
        ext = Path(source.filename or "source.jsonl").suffix.lower() or ".jsonl"
        spath = tmpdir / f"source{ext}"
        tpath = tmpdir / "out.parquet"
        apath = tmpdir / "audit.json"
        cpath.write_bytes(contract_bytes)
        spath.write_bytes(source.file.read())
        result = _run_cli(
            [
                "run",
                "--contract", str(cpath),
                "--source", str(spath),
                "--target", str(tpath),
                "--audit", str(apath),
                "--tenant", tenant,
                "--json",
            ],
            timeout=900,
        )
        return _as_json(result)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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
async def run_demo() -> JSONResponse:
    """Run the self-contained ``datashell demo --json`` walkthrough."""
    result = _run_cli(["demo", "--json"], timeout=300)
    if result.returncode != 0 and not result.stdout:
        raise HTTPException(status_code=500, detail=(result.stderr or "Demo failed")[:500])
    return JSONResponse(_as_json(result))


@app.post("/api/enforce")
async def enforce(
    contract: UploadFile = File(...),
    source: UploadFile = File(...),
    tenant: str = Form(default="default-tenant"),
) -> JSONResponse:
    """Enforce a user-supplied contract against a small uploaded file."""
    return JSONResponse(_enforce_upload(await contract.read(), source, tenant, "datashell_web_"))


@app.post("/api/enforce/interactive")
async def enforce_interactive(
    source: UploadFile = File(...),
    tenant: str = Form(default="demo-tenant"),
    contract_json: str = Form(...),
) -> JSONResponse:
    """Enforce a contract the playground built from the user's form input."""
    return JSONResponse(
        _enforce_upload(contract_json.encode("utf-8"), source, tenant, "datashell_webi_")
    )
