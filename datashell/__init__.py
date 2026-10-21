"""Data Shell: Contract-driven boundary enforcement engine.

Bad data never lands, and never leaves.

The top-level package intentionally uses lazy exports so importing small modules
(e.g. configuration or transaction recovery) does not eagerly import the full data
processing stack or create a heavy runtime dependency chain.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__version__ = "2.0.1"

_EXPORTS = {
    "DataContract": ("datashell.contract.models", "DataContract"),
    "QualityRule": ("datashell.contract.models", "QualityRule"),
    "BatchRule": ("datashell.contract.models", "BatchRule"),
    "DataShellPipeline": ("datashell.pipeline.runner", "DataShellPipeline"),
    "PipelineResult": ("datashell.pipeline.runner", "PipelineResult"),
    "ReconciliationLedger": ("datashell.engines.reconciler", "ReconciliationLedger"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Load public symbols only when they are requested."""
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, symbol_name = _EXPORTS[name]
    value = getattr(import_module(module_name), symbol_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
