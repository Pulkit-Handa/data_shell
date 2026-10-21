"""OpenTelemetry bridge for the gateway (optional ``datashell-engine[otel]`` extra).

Tracing activates only when *both* conditions hold:

1. the operator expressed the intent through the standard
   ``OTEL_EXPORTER_OTLP_ENDPOINT`` environment variable (optionally with
   ``DATASHELL_OTEL_SERVICE_NAME`` for the resource name), and
2. the ``opentelemetry`` packages are importable — the ``datashell-engine[otel]``
   extra installs them.

Every helper degrades to a no-op otherwise, so the tracing decision stays in
configuration: environments without tracing run with zero overhead and zero
behavioral difference.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from typing import Any, Protocol

from datashell.core.telemetry import telemetry_logger

SERVICE_NAME_ENV = "DATASHELL_OTEL_SERVICE_NAME"
ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

# Populated by configure_otel(); a sentinel tuple keeps the module importable
# without the opentelemetry packages installed.
_STATE: dict[str, Any] = {"configured": False, "tracer": None, "_reason": "not requested"}


class _SpanLike(Protocol):
    """The minimal span surface the bridge touches (real or fake in tests)."""

    def update_name(self, name: str) -> None: ...

    def set_attribute(self, key: str, value: Any) -> None: ...


def tracing_enabled() -> bool:
    """True once an instrumented tracer provider is actually installed."""
    return _STATE["tracer"] is not None


def configure_otel() -> bool:
    """Install the tracer provider when requested and possible. Idempotent.

    Returns whether tracing is enabled afterwards; logs rather than raising so
    a misconfigured collector endpoint can never take the gateway down.
    """
    if _STATE["configured"]:
        return tracing_enabled()
    _STATE["configured"] = True

    endpoint = os.getenv(ENDPOINT_ENV, "").strip()
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        _STATE["_reason"] = "otel extra not installed"
        telemetry_logger.warning(
            "OpenTelemetry requested via OTEL_EXPORTER_OTLP_ENDPOINT but the "
            "opentelemetry packages are not importable; tracing stays disabled",
            env=ENDPOINT_ENV,
            hint="install datashell-engine[otel]",
        )
        return False

    service_name = os.getenv(SERVICE_NAME_ENV, "datashell").strip() or "datashell"
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _STATE["tracer"] = trace.get_tracer("datashell")
    telemetry_logger.info("OpenTelemetry tracing enabled", service_name=service_name, endpoint=endpoint)
    return True


@contextlib.contextmanager
def http_server_span(method: str) -> Iterator[_SpanLike | None]:
    """Server span for one HTTP exchange (no-op unless tracing is enabled).

    The span starts with a coarse name (the templated route is only known
    after routing); callers rename it with the resolved route before exiting.
    Exceptions are recorded by the real span context manager.
    """
    tracer = _STATE["tracer"]
    if tracer is None:
        yield None
        return
    attributes: dict[str, object] = {"http.request.method": method.upper()}
    with tracer.start_as_current_span(f"{method.upper()} HTTP", attributes=attributes) as span:
        yield span


__all__ = [
    "ENDPOINT_ENV",
    "SERVICE_NAME_ENV",
    "configure_otel",
    "http_server_span",
    "tracing_enabled",
]
