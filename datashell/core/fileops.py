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
from typing import Optional


class InsufficientStorageError(Exception):
    """Raised when the storage volume cannot hold a run's transient footprint."""


def available_bytes(path: Path) -> Optional[int]:
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
