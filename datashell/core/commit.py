"""Crash-recoverable publication of related filesystem artifacts.

A filesystem cannot atomically rename multiple files at once. This module uses a
small write-ahead journal and backups so an interrupted multi-file publication is
rolled back on the next attempt, rather than leaving a mismatched active target
and immutable lineage artifact.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from datashell.core.config import settings

MAX_COMMIT_JOURNAL_BYTES = 1024 * 1024
MAX_TRANSACTION_ENTRIES = 32


def _fsync_file(path: Path) -> None:
    """Flush staged artifact contents before publishing their directory entries."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # Some filesystems/platforms do not support directory fsync.
        pass


def _journal_integrity_tag(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "journal_mac"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if settings.is_production_mode:
        return hmac.new(settings.hmac_secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    # In development this detects accidental corruption across tests in-process;
    # production uses a stable secret-backed MAC for restart-safe authenticity.
    return hashlib.sha256(canonical).hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    payload.pop("journal_mac", None)
    payload["journal_mac"] = _journal_integrity_tag(payload)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    _fsync_dir(path.parent)


GENERATION_POINTER_SUFFIX = ".generation.json"


def generation_pointer_path(final_target: Path) -> Path:
    """Path of the reader-visible commit pointer for one delivery target.

    The pointer is the single atomic commit point of a publication: after the
    staged dataset, lineage artifact, and certificate are durably renamed,
    exactly one ``os.replace`` swaps this small JSON file to name the new
    ``run_id``. Readers that resolve the pointer first always observe one
    complete generation — never a mixture of the old and new file sets.
    """
    return final_target.with_name(final_target.name + GENERATION_POINTER_SUFFIX)


def read_generation_pointer(final_target: Path) -> dict[str, Any] | None:
    """Return the committed generation for a target, or ``None`` if never set."""
    pointer = generation_pointer_path(Path(final_target))
    try:
        with open(pointer, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("run_id"):
        return None
    return payload


def resolve_committed_generation(final_target: Path) -> str | None:
    """Resolve the reader-visible committed generation for a delivery target.

    External consumers (audit jobs, settlement, downstream consumers) MUST call
    this before reading the artifacts at ``final_target``'s directory so they
    observe exactly one committed generation. Returns the committed ``run_id``
    or ``None`` when no generation has been committed for this target.

    The pointer is the single atomic commit point of a publication; it is
    written only after every artifact for the new generation is durable. Readers
    that resolve the pointer first therefore never observe a mixture of old and
    new artifact generations through supported interfaces.
    """
    pointer = read_generation_pointer(final_target)
    if pointer is None:
        return None
    return str(pointer.get("run_id"))


def committed_generation_status(certificate: dict[str, Any]) -> dict[str, Any]:
    """Report whether a certificate describes the currently committed generation.

    This is the shared reader contract for every supported surface (CLI
    verifier, HTTP certificate endpoint). A certificate remains valid evidence
    for **its own** run even after a newer generation is committed - the
    immutable per-run lineage artifact is what the certificate binds - but a
    consumer that wants "the current delivery" must resolve the generation
    pointer rather than assume the certificate is the latest.

    Returns:
        ``committed_generation`` - the committed ``run_id``, or ``None`` when no
        generation pointer exists for the delivery target (for example a run
        whose target was never published, such as a rejected run).
        ``is_committed_generation`` - ``True``/``False`` when the generation can
        be compared, or ``None`` when it cannot (no pointer, or the certificate
        carries no run id).
    """
    manifest = (certificate or {}).get("cryptographic_manifest") or {}
    target = manifest.get("delivered_file_path")
    if not target:
        return {"committed_generation": None, "is_committed_generation": None}
    try:
        committed = resolve_committed_generation(Path(str(target)))
    except (OSError, ValueError):
        return {"committed_generation": None, "is_committed_generation": None}
    if committed is None:
        return {"committed_generation": None, "is_committed_generation": None}
    run_id = manifest.get("run_id") or (certificate or {}).get("run_id")
    return {
        "committed_generation": committed,
        "is_committed_generation": (run_id is None) or (str(run_id) == committed),
    }


def _write_generation_pointer_atomically(pointer: Path, payload: dict[str, Any]) -> None:
    """Atomically replace the generation pointer (the commit point)."""
    temp = pointer.with_name(f".{pointer.name}.{uuid.uuid4().hex}.tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, pointer)
    _fsync_dir(pointer.parent)


def _transaction_paths(target: Path, transaction_root: Path | None = None) -> tuple[Path, Path]:
    root = Path(transaction_root).resolve() if transaction_root is not None else target.parent
    token = hashlib.sha256(str(target.resolve()).encode("utf-8")).hexdigest()[:20]
    return root / f".dm-commit-{token}.json", root / f".dm-commit-{token}.lock"


def _rollback(journal_path: Path, journal: dict[str, Any]) -> None:
    for entry in reversed(journal.get("entries", [])):
        final = Path(entry["final"])
        staged = Path(entry["staged"])
        backup = Path(entry["backup"])
        had_original = bool(entry["had_original"])
        if had_original:
            if backup.exists():
                if final.exists():
                    final.unlink()
                os.replace(backup, final)
            # If the backup is absent, the original was never moved; leave it.
        elif not staged.exists() and final.exists():
            # The staged file has already moved to final, which did not exist
            # before this transaction, so remove the uncommitted publication.
            final.unlink()
        # A process crash may leave staged artifacts that were never published.
        staged.unlink(missing_ok=True)
    for directory in {Path(entry["final"]).parent for entry in journal.get("entries", [])}:
        _fsync_dir(directory)
    _fsync_dir(journal_path.parent)
    journal_path.unlink(missing_ok=True)
    _fsync_dir(journal_path.parent)


def _recover_locked(journal_path: Path) -> None:
    if not journal_path.exists():
        return
    if journal_path.is_symlink() or not journal_path.resolve().is_relative_to(journal_path.parent.resolve()):
        raise RuntimeError(f"Refusing unsafe commit journal path: {journal_path}")
    try:
        if journal_path.stat().st_size > MAX_COMMIT_JOURNAL_BYTES:
            raise RuntimeError(f"Commit journal exceeds the permitted size: {journal_path}")
        with open(journal_path, encoding="utf-8") as handle:
            journal = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot safely recover corrupted commit journal {journal_path}") from exc

    valid_journal_header = (
        isinstance(journal, dict)
        and journal.get("version") == 1
        and journal.get("state") in {"prepared", "committed"}
    )
    if not valid_journal_header:
        raise RuntimeError(f"Cannot safely recover unsupported commit journal {journal_path}")
    entries = journal.get("entries")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_TRANSACTION_ENTRIES:  # noqa: E501
        raise RuntimeError(f"Cannot safely recover malformed commit journal {journal_path}")
    # The journal lives at the declared transaction root. Never trust path
    # values from a corrupted journal to touch files outside that root.
    for entry in entries:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("final", "staged", "backup")):
            raise RuntimeError(f"Cannot safely recover malformed entry in {journal_path}")
        root = journal_path.parent.resolve()
        resolved = {k: Path(entry[k]).resolve() for k in ("final", "staged", "backup")}
        if any(not path.is_relative_to(root) for path in resolved.values()):
            raise RuntimeError(f"Refusing journal paths outside transaction directory: {journal_path}")
        if len({path.parent for path in resolved.values()}) != 1:
            raise RuntimeError(f"Refusing journal entries that cross destination directories: {journal_path}")
        try:
            if os.stat(resolved["final"].parent).st_dev != os.stat(root).st_dev:
                raise RuntimeError(f"Refusing journal entries on a different filesystem: {journal_path}")
        except OSError as exc:
            raise RuntimeError(f"Cannot safely access transaction filesystem for {journal_path}") from exc
        if not isinstance(entry.get("had_original"), bool):
            raise RuntimeError(f"Cannot safely recover malformed entry in {journal_path}")
    supplied_mac = journal.get("journal_mac")
    if supplied_mac is None:
        # Backward compatibility for pre-MAC journals is limited to the old
        # same-directory format. New cross-directory journals must be signed.
        crosses_directory = any(
            Path(entry[k]).resolve().parent != journal_path.parent.resolve()
            for entry in entries for k in ("final", "staged", "backup")
        )
        if crosses_directory:
            raise RuntimeError(f"Commit journal integrity verification failed: {journal_path}")
    else:
        expected_mac = _journal_integrity_tag(journal)
        if not isinstance(supplied_mac, str) or not hmac.compare_digest(supplied_mac, expected_mac):
            raise RuntimeError(f"Commit journal integrity verification failed: {journal_path}")
    if journal.get("state") == "committed":
        for entry in entries:
            Path(entry["backup"]).unlink(missing_ok=True)
            _fsync_dir(Path(entry["final"]).parent)
        # Repair the reader-visible generation pointer from the durable journal.
        # If publication crashed after every artifact rename was durable but
        # before the pointer swap, the pointer would still name the previous
        # generation while the final files already hold the new one. Rewriting
        # the pointer here makes crash recovery converge to a consistent state.
        run_id = journal.get("run_id")
        if isinstance(run_id, str) and run_id:
            pointer_payload = {"run_id": run_id}
            for entry in entries:
                pointer = generation_pointer_path(Path(entry["final"]))
                _write_generation_pointer_atomically(pointer, pointer_payload)
        journal_path.unlink(missing_ok=True)
        _fsync_dir(journal_path.parent)
        return
    _rollback(journal_path, journal)


def recover_publication(target: Path, *, transaction_root: Path | None = None) -> None:
    """Recover an interrupted publication before a new delivery writes this target."""
    target = Path(target).resolve()
    root = Path(transaction_root).resolve() if transaction_root is not None else target.parent
    root.mkdir(parents=True, exist_ok=True)
    journal_path, lock_path = _transaction_paths(target, root)
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            _recover_locked(journal_path)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)



def recover_pending_publications(transaction_root: Path) -> int:
    """Recover all interrupted publication journals beneath a storage root.

    Startup uses this before readiness is reported, so consumers do not observe
    a half-published target while waiting for a future delivery to trigger recovery.
    """
    root = Path(transaction_root).resolve()
    if not root.is_dir():
        return 0
    recovered = 0
    for journal_path in sorted(root.rglob(".dm-commit-*.json")):
        if journal_path.is_symlink() or not journal_path.resolve().is_relative_to(root):
            raise RuntimeError(f"Refusing unsafe commit journal path: {journal_path}")
        try:
            if journal_path.stat().st_size > MAX_COMMIT_JOURNAL_BYTES:
                raise RuntimeError(f"Commit journal exceeds the permitted size: {journal_path}")
            with open(journal_path, encoding="utf-8") as handle:
                journal = json.load(handle)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Cannot safely recover corrupted commit journal {journal_path}") from exc
        if not isinstance(journal, dict):
            raise RuntimeError(f"Cannot safely recover malformed commit journal {journal_path}")
        entries: Any = journal.get("entries")
        if not isinstance(entries, list) or not entries or len(entries) > MAX_TRANSACTION_ENTRIES or not isinstance(entries[-1], dict):  # noqa: E501
            raise RuntimeError(f"Cannot safely recover malformed commit journal {journal_path}")
        target_value = entries[-1].get("final")
        if not isinstance(target_value, str):
            raise RuntimeError(f"Cannot safely identify target for commit journal {journal_path}")
        target = Path(target_value).resolve()
        expected_path, _ = _transaction_paths(target, journal_path.parent)
        if expected_path != journal_path.resolve():
            raise RuntimeError(f"Commit journal name does not match its target: {journal_path}")
        recover_publication(target, transaction_root=journal_path.parent)
        recovered += 1
    return recovered


def publish_transactionally(
    pairs: Sequence[tuple[Path, Path]],
    *,
    run_id: str,
    transaction_root: Path | None = None,
    generation_pointer: dict[str, Any] | None = None,
) -> None:
    """Publish staged files to final paths as one recoverable transaction.

    The first path in each pair is the staged source and the second is its final
    destination. On any exception, prior destinations are restored before the
    exception is re-raised. A crash is recovered on the next call for that target.

    When ``generation_pointer`` is supplied, it names the reader-visible commit
    point: after every artifact rename is durable, the pointer file derived
    from the *last* pair's destination is swapped by exactly one atomic
    ``os.replace``. Readers resolving the pointer first therefore observe one
    complete generation — never a mixture of old and new files. A crash before
    the swap leaves the previous pointer with rolled-back files; a crash after
    it leaves complete new files behind the new pointer.
    """
    if not pairs:
        return
    normalized = [(Path(staged).resolve(), Path(final).resolve()) for staged, final in pairs]
    target = normalized[-1][1]
    legacy_parent = target.parent
    parent = Path(transaction_root).resolve() if transaction_root is not None else legacy_parent
    if transaction_root is None and any(final.parent != legacy_parent for _, final in normalized):
        raise ValueError("Transactional artifacts must share a destination directory unless transaction_root is set")
    if len({str(final) for _, final in normalized}) != len(normalized):
        raise ValueError("Transactional artifact destinations must be unique")
    parent.mkdir(parents=True, exist_ok=True)
    root_device = os.stat(parent).st_dev
    for staged, final in normalized:
        if staged.parent != final.parent:
            raise ValueError("Staged and final artifacts must share a directory for atomic rename")
        if not staged.is_relative_to(parent) or not final.is_relative_to(parent):
            raise ValueError("Transactional artifacts must be located beneath transaction_root")
        if root_device is not None and os.stat(final.parent).st_dev != root_device:
            raise ValueError("All transactional artifacts and the journal must reside on the same filesystem")
        if not staged.is_file():
            raise FileNotFoundError(f"Staged artifact is missing: {staged}")
        _fsync_file(staged)

    journal_path, lock_path = _transaction_paths(target, parent)
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        journal = None
        try:
            _recover_locked(journal_path)
            entries: list[dict[str, Any]] = []
            for index, (staged, final) in enumerate(normalized):
                entries.append({
                    "staged": str(staged),
                    "final": str(final),
                    "backup": str(final.with_name(f".{final.name}.{run_id}.{index}.backup")),
                    "had_original": final.exists(),
                })
            journal = {"version": 1, "run_id": run_id, "state": "prepared", "entries": entries}
            _write_json_atomic(journal_path, journal)

            for entry in entries:
                final, backup = Path(entry["final"]), Path(entry["backup"])
                staged = Path(entry["staged"])
                if entry["had_original"]:
                    os.replace(final, backup)
                    _fsync_dir(final.parent)
                os.replace(staged, final)
                _fsync_dir(final.parent)

            journal["state"] = "committed"
            _write_json_atomic(journal_path, journal)
            for entry in entries:
                Path(entry["backup"]).unlink(missing_ok=True)
                _fsync_dir(Path(entry["final"]).parent)
            if generation_pointer is not None:
                pointer = generation_pointer_path(target)
                payload = {"run_id": run_id, **generation_pointer}
                _write_generation_pointer_atomically(pointer, payload)
            journal_path.unlink(missing_ok=True)
            _fsync_dir(parent)
        except Exception:
            if journal is not None and journal_path.exists():
                # Once the committed marker is durable, publication succeeded.
                # A cleanup failure must not be reported as a failed delivery;
                # recovery can remove the remaining backups on the next run.
                try:
                    with open(journal_path, encoding="utf-8") as handle:
                        current_journal = json.load(handle)
                except (OSError, ValueError):
                    current_journal = {}
                if current_journal.get("state") == "committed":
                    return
                _recover_locked(journal_path)
            raise
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
