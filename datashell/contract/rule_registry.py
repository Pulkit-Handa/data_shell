"""Extensible rule-check registry (built-in checks stay closed; plugins are opt-in).

Quality rules come in two strengths:

* **Built-in checks** are the reviewed enum members (``GREATER_THAN``, ...)
  compiled by :mod:`datashell.contract.compiler`. They are fixed and cannot be
  shadowed by plugins — a plugin named after a built-in is rejected, so a
  malicious optional dependency can never weaken a reviewed rule.
* **Plugin checks** are discovered through the
  ``importlib.metadata`` entry-point groups ``datashell.rule_checks.row`` (a
  callable ``f(rule: QualityRule) -> pl.Expr``) and
  ``datashell.rule_checks.batch`` (``f(rule: BatchRule, table_name: str) ->
  str``). Installing a package such as ``datashell-geo-rules`` therefore adds
  new check types without touching the engine; uninstalling removes them.

Unknown check names fail at the moment they matter — compilation — with the
name groups spelled out. In-process registration exists for tests and for
embedders that construct rules programmatically.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any

import polars as pl

from datashell.contract.models import (
    BatchRule,
    BatchRuleCheckType,
    QualityRule,
    RowRuleCheckType,
)

ROW_CHECK_GROUP = "datashell.rule_checks.row"
BATCH_CHECK_GROUP = "datashell.rule_checks.batch"

RowRuleCheckFn = Callable[[QualityRule], pl.Expr]
BatchRuleCheckFn = Callable[[BatchRule, str], str]

# In-process registrations (tests, embedders). They take precedence over
# entry-point plugins but can never shadow a built-in check name.
_overlay_row: dict[str, RowRuleCheckFn] = {}
_overlay_batch: dict[str, BatchRuleCheckFn] = {}

# Lazily loaded entry-point plugins, keyed by check name.
_plugins_row: dict[str, tuple[RowRuleCheckFn, str]] = {}
_plugins_batch: dict[str, tuple[BatchRuleCheckFn, str]] = {}
_load_errors: list[str] = []
_loaded = False


def _builtin_names() -> set[str]:
    return {member.value for member in RowRuleCheckType} | {
        member.value for member in BatchRuleCheckType
    }


def _ensure_loaded() -> None:
    """Load plugin entry points once per process."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        available = entry_points()
    except Exception:  # pragma: no cover - importlib.metadata defensive
        return
    _load_errors.clear()
    for group, bucket, kind in ((ROW_CHECK_GROUP, _plugins_row, "row"),
                                (BATCH_CHECK_GROUP, _plugins_batch, "batch")):
        for entry_point in sort_entry_points(available.select(group=group)):
            try:
                callable_check = entry_point.load()
                if not callable(callable_check):
                    raise TypeError("entry point did not resolve to a callable")
            except Exception as exc:  # never take enforcement down over a plugin
                _load_errors.append(
                    f"{kind} plugin '{entry_point.name}' from '{entry_point.value}': {exc}")
                continue
            name = entry_point.name.upper()
            if name in _builtin_names():
                _load_errors.append(
                    f"{kind} plugin '{entry_point.name}' shadows a built-in check "
                    "and was rejected")
                continue
            bucket[name] = (callable_check, f"plugin:{entry_point.value}")


def sort_entry_points(eps: Any) -> list[Any]:
    """Deterministic iteration order for reproducible error reports."""
    return sorted(eps, key=lambda ep: (ep.group, ep.name, ep.value))


def reload_rule_plugins() -> None:
    """Re-scan entry points (installing a plugin package at runtime)."""
    global _loaded
    _plugins_row.clear()
    _plugins_batch.clear()
    _loaded = False
    _ensure_loaded()


def register_row_check(name: str, compile_fn: RowRuleCheckFn) -> None:
    """Register a row-check compiler for this process (tests and embedders)."""
    normalized = _validate_name(name)
    if normalized in _builtin_names():
        raise ValueError(f"check '{name}' is a built-in and cannot be shadowed")
    _overlay_row[normalized] = compile_fn


def register_batch_check(name: str, compile_fn: BatchRuleCheckFn) -> None:
    """Register a batch-check compiler for this process (tests and embedders)."""
    normalized = _validate_name(name)
    if normalized in _builtin_names():
        raise ValueError(f"check '{name}' is a built-in and cannot be shadowed")
    _overlay_batch[normalized] = compile_fn


def _validate_name(name: str) -> str:
    candidate = str(name).upper()
    if not candidate or not all(c.isalnum() or c == "_" for c in candidate):
        raise ValueError(f"invalid check name '{name}' (use UPPER_SNAKE_CASE)")
    return candidate


def resolve_row_check(name: str) -> RowRuleCheckFn | None:
    """Resolve a non-built-in row check; None when unknown (compiler errors)."""
    _ensure_loaded()
    normalized = _validate_name(name)
    if normalized in _overlay_row:
        return _overlay_row[normalized]
    hit = _plugins_row.get(normalized)
    return hit[0] if hit else None


def resolve_batch_check(name: str) -> BatchRuleCheckFn | None:
    """Resolve a non-built-in batch check; None when unknown (compiler errors)."""
    _ensure_loaded()
    normalized = _validate_name(name)
    if normalized in _overlay_batch:
        return _overlay_batch[normalized]
    hit = _plugins_batch.get(normalized)
    return hit[0] if hit else None


def available_row_checks() -> dict[str, str]:
    """Map of every row check name to its provenance."""
    _ensure_loaded()
    checks: dict[str, str] = {member.value: "builtin" for member in RowRuleCheckType}
    checks.update({name: value for name, (_, value) in _plugins_row.items()})
    checks.update({name: "in-process" for name in _overlay_row})
    return dict(sorted(checks.items()))


def available_batch_checks() -> dict[str, str]:
    """Map of every batch check name to its provenance."""
    _ensure_loaded()
    checks: dict[str, str] = {member.value: "builtin" for member in BatchRuleCheckType}
    checks.update({name: value for name, (_, value) in _plugins_batch.items()})
    checks.update({name: "in-process" for name in _overlay_batch})
    return dict(sorted(checks.items()))


def plugin_load_errors() -> list[str]:
    """Entry points that failed to load or attempted to shadow a built-in."""
    return list(_load_errors)


__all__ = [
    "BATCH_CHECK_GROUP",
    "ROW_CHECK_GROUP",
    "available_batch_checks",
    "available_row_checks",
    "plugin_load_errors",
    "reload_rule_plugins",
    "register_batch_check",
    "register_row_check",
    "resolve_batch_check",
    "resolve_row_check",
]
