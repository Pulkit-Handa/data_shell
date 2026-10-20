"""Apache Airflow provider for Data Shell boundary enforcement.

Exposes ``DataShellEnforceOperator`` (gateway API) and
``DataShellLocalEnforceOperator`` (local CLI) plus the ``DataShellHook`` connection
helper.

Importing the operators/hook requires Apache Airflow, so they are loaded lazily
via module ``__getattr__``. The dependency-free ``client`` module can always be
imported, which keeps it unit-testable without Airflow installed.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from datashell_airflow.client import DataShellApiClient, DataShellApiError

__version__ = "2.0.0"

_EXPORTS = {
    "DataShellHook": ("datashell_airflow.hooks", "DataShellHook"),
    "DataShellEnforceOperator": ("datashell_airflow.operators", "DataShellEnforceOperator"),
    "DataShellLocalEnforceOperator": (
        "datashell_airflow.operators",
        "DataShellLocalEnforceOperator",
    ),
}

__all__ = ["DataShellApiClient", "DataShellApiError", *sorted(_EXPORTS)]


def __getattr__(name: str) -> Any:
    """Lazily import Airflow-dependent symbols on first access."""
    if name in _EXPORTS:
        module_name, attribute = _EXPORTS[name]
        value = getattr(import_module(module_name), attribute)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_provider_info() -> dict[str, Any]:
    """Metadata used by Airflow to register this provider."""
    return {
        "package-name": "datashell-airflow",
        "name": "Data Shell",
        "description": "Gate Airflow DAGs on Data Shell boundary enforcement.",
        "versions": [__version__],
        "connection-types": [
            {
                "connection-type": "http",
                "hook-class-name": "datashell_airflow.hooks.DataShellHook",
            }
        ],
    }
