"""Filesystem helpers for bounded-I/O artifact handling.

Large deliveries must persist two byte-identical artifacts: the active delivery
target and its immutable run-lineage snapshot. Duplicating multi-gigabyte files
with a byte copy doubles both disk usage and publication I/O. When the two paths
share a filesystem, a hard link provides the same content-addressed snapshot
without a second copy. Publication always replaces artifacts by rename, so the
lineage inode is never mutated in place and the snapshot remains faithful.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal, cast


class InsufficientStorageError(Exception):
    """Raised when the storage volume cannot hold a run's transient footprint."""


def available_bytes(path: Path) -> int | None:
    """Return free bytes on the filesystem hosting ``path``, or None if unknown."""
    try:
        stat = os.statvfs(Path(path))
    except OSError:
        return None
    return stat.f_bavail * stat.f_frsize


def ensure_disk_headroom(source_bytes: int, *, multiplier: float, root: Path) -> None:
    """Fail fast when the storage volume cannot hold a run's transient footprint.

    A run stages the delivered target, may retain a full-size quarantine, and
    writes out-of-core spills on the same volume, so it needs a multiple of the
    source size free before it starts. Failing here produces an actionable error
    instead of a mid-run ``ENOSPC`` that would strand staged artifacts.
    """
    if source_bytes <= 0:
        return
    free = available_bytes(root)
    if free is None:
        return
    required = int(source_bytes * multiplier)
    if free < required:
        raise InsufficientStorageError(
            f"Insufficient free space to enforce safely: {free:,} bytes available on the storage "
            f"volume but approximately {required:,} bytes are required "
            f"(source {source_bytes:,} bytes x {multiplier:g} headroom). "
            f"Free space or raise capacity, then retry."
        )


def materialize_artifact_copy(source: Path, destination: Path) -> None:
    """Copy ``source`` to ``destination`` cheaply, preferring a hard link.

    Falls back to a byte-for-byte copy when hard links are unsupported (for
    example across filesystems or on filesystems that forbid links).
    """
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise FileNotFoundError(f"Cannot materialize artifact copy; source is missing: {source}")
    if destination.exists() or destination.is_symlink():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copyfile(source, destination)


# Codecs Polars and pyarrow both accept for Parquet writes, plus the explicit
# "no compression" spelling. All writer sites normalize through this helper so
# the delivered-artifact codec is one deployment decision, not three.
PARQUET_CODECS = ("snappy", "zstd", "gzip", "lz4", "brotli", "uncompressed")

# The writer-facing subset: everything the two Parquet authoring layers accept
# as a compression argument (both layers take "no compression" as None).
ParquetCodec = Literal["lz4", "snappy", "gzip", "brotli", "zstd"]


def polars_parquet_compression(configured: str | None = None) -> Literal[
    "lz4", "uncompressed", "snappy", "gzip", "brotli", "zstd"
]:
    """Polars-facing spelling of the delivered-artifact codec.

    Polars interprets ``compression=None`` as "use its own default (zstd)",
    not as "no compression"; the explicit ``"uncompressed"`` string is the only
    way to disable compression in a ``write_parquet`` call.
    """
    codec = parquet_compression_codec(configured)
    return cast(Literal["lz4", "uncompressed", "snappy", "gzip", "brotli", "zstd"], codec or "uncompressed")
    """Normalize ``DATASHELL_DELIVERY_COMPRESSION`` to what Polars/pyarrow accept.

    ``None`` means "no compression" for both ``df.write_parquet`` and
    ``pq.ParquetWriter``. Reads the live settings value when ``configured`` is
    omitted, so tests and reconfiguration take effect without a restart. The
    settings import is deferred because the config module is not always
    importable at interpreter startup order for all entry points.
    """
    value = configured
    if value is None:
        from datashell.core.config import settings

        value = settings.delivery_compression
    value = value.strip().lower()
    if value not in PARQUET_CODECS:
        raise ValueError(
            "DATASHELL_DELIVERY_COMPRESSION must be one of: " + ", ".join(PARQUET_CODECS))
    if value in ("uncompressed", "none"):
        return None
    return cast(ParquetCodec, value)

