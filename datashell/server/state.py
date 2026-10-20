"""Shared gateway runtime state.

Singletons the routers import, so process-wide gateway state lives in one
module: the contract registry, the upload staging directory, the run
concurrency semaphore, and shared request utilities. Tests rebind these in
exactly one place instead of chasing assembly order.
"""

from __future__ import annotations

import asyncio
import re

from datashell.contract.registry import ContractRegistry
from datashell.core.config import settings

# Upload formats the boundary will persist for enforcement. Everything else is
# rejected before staging (defense against parsers outside the checked set).
ALLOWED_EXTENSIONS = {".parquet", ".csv", ".jsonl", ".ndjson"}

# Per-run upload ceiling (the request-body bound derives from it in settings).
MAX_UPLOAD_SIZE = settings.max_upload_size_bytes

# Bounded concurrency: at most this many pipelines execute at once. The
# middleware admits more *requests* than runs; surplus uploads stage to disk
# quickly and only enforcement holds the semaphore.
RUN_SEMAPHORE = asyncio.Semaphore(settings.max_concurrent_runs)

# Staging root for gateway uploads before enforcement begins.
WORKSPACE_DATA = settings.data_dir
WORKSPACE_DATA.mkdir(parents=True, exist_ok=True)

# The tenant-scoped, immutable, tamper-evident contract registry.
registry = ContractRegistry(settings.contracts_dir)

# Service-generated run/settlement identifiers ([A-Za-z0-9_-]{1,80}); every
# caller-controlled identifier reaching a filesystem path validates here.
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,80}")


def bounded_limit(limit: int, default: int = 50, maximum: int = 500) -> int:
    """Clamp a caller-supplied pagination limit to a sane bounded range."""
    if limit <= 0:
        return default
    return min(int(limit), maximum)
