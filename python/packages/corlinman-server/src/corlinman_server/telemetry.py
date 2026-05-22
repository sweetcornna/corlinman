"""OpenTelemetry OTLP exporter wiring for the Python agent server (S7.T1).

Contract (mirrors the Rust gateway's ``corlinman_gateway::telemetry``):

* Init is opt-in — activated only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is
  set and non-empty. Without it the functions here are no-ops.
* Init failures are warn-and-continue; ``structlog`` keeps working.
* Service name defaults to ``corlinman-server`` (``OTEL_SERVICE_NAME``
  overrides).
* Once :func:`init_telemetry` runs, ``structlog`` adds ``trace_id`` /
  ``span_id`` to every record via :func:`_bind_trace_ids_processor`.
* :func:`span` is a zero-overhead context manager when telemetry is off —
  it never raises and is safe to use unconditionally throughout the call path.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Generator
from typing import Any

import structlog

__all__ = ["init_telemetry", "shutdown_telemetry", "span"]

_PROVIDER: Any = None  # opentelemetry_sdk.trace.TracerProvider when wired


def _bind_trace_ids_processor(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Add ``trace_id`` / ``span_id`` hex strings to every structlog record.

    Called only when an OTel tracer is installed. Missing context yields
    empty strings rather than raising — matches the grpc interceptor,
    which may see requests with no ``traceparent`` header.
    """
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
    except Exception:  # pragma: no cover — defensive
        return event_dict

    if ctx and ctx.is_valid:
        event_dict.setdefault("trace_id", format(ctx.trace_id, "032x"))
        event_dict.setdefault("span_id", format(ctx.span_id, "016x"))
    return event_dict


def init_telemetry() -> bool:
    """Configure the global OTel tracer + structlog binding once.

    Returns ``True`` when the tracer was installed, ``False`` otherwise
    (endpoint unset, dependency missing, or exporter init failed). The
    server boot treats both as success — telemetry stays optional.
    """
    global _PROVIDER

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )
    except ImportError as err:
        structlog.get_logger(__name__).warning(
            "otel.init.skip",
            reason="missing dependency",
            error=str(err),
        )
        return False

    service_name = os.environ.get("OTEL_SERVICE_NAME", "corlinman-server")
    try:
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": "0.1.0",
            }
        )
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=endpoint.startswith("http://"))
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        set_global_textmap(TraceContextTextMapPropagator())
        _PROVIDER = provider
    except Exception as err:  # pragma: no cover — collector-dependent
        structlog.get_logger(__name__).warning(
            "otel.init.failed", endpoint=endpoint, error=str(err)
        )
        return False

    # Rebind structlog so every record carries trace_id / span_id.
    try:
        current = list(structlog.get_config()["processors"])
        if _bind_trace_ids_processor not in current:
            # Insert before the final renderer (last processor).
            current.insert(max(len(current) - 1, 0), _bind_trace_ids_processor)
            structlog.configure(processors=current)
    except Exception:  # pragma: no cover — structlog not configured yet
        pass

    structlog.get_logger(__name__).info("otel.init.ok", endpoint=endpoint)
    return True


@contextlib.contextmanager
def span(
    name: str,
    *,
    attributes: dict[str, str | int | float | bool] | None = None,
) -> Generator[Any, None, None]:
    """Open an OTel span when telemetry is initialised; be a cheap no-op otherwise.

    Usage::

        with telemetry.span("chat.handler", attributes={"model": req.model}) as s:
            ...
            s.set_attribute("http.status_code", 200)

    The yielded value is either a live :class:`opentelemetry.trace.Span` (when
    a tracer provider is installed) or a :class:`opentelemetry.trace.NonRecordingSpan`
    proxy — callers may call ``.set_attribute`` / ``.record_exception`` on it
    safely in both cases. The context manager itself never raises.

    Exceptions propagate normally; they are recorded on the span via
    ``record_exception`` and the span status is set to ``ERROR`` before
    re-raising.
    """
    if _PROVIDER is None:
        # No tracer installed — yield a guaranteed no-op proxy.
        try:
            from opentelemetry import trace

            yield trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)
        except Exception:  # pragma: no cover — OTel not importable
            yield _NullSpan()
        return

    try:
        from opentelemetry.trace import StatusCode
    except Exception:  # pragma: no cover — should not happen after init
        yield _NullSpan()
        return

    # Use _PROVIDER.get_tracer() directly so tests can swap _PROVIDER freely
    # without fighting OTel's once-only global-provider guard.
    tracer = _PROVIDER.get_tracer(__name__)
    with tracer.start_as_current_span(name) as current_span:
        if attributes:
            for key, value in attributes.items():
                try:
                    current_span.set_attribute(key, value)
                except Exception:  # pragma: no cover — defensive
                    pass
        try:
            yield current_span
        except Exception as exc:
            with contextlib.suppress(Exception):
                current_span.record_exception(exc)
                current_span.set_status(StatusCode.ERROR, str(exc))
            raise


class _NullSpan:
    """Minimal no-op span for the extreme edge case where OTel is not importable."""

    def set_attribute(self, _key: str, _value: object) -> None:
        pass

    def record_exception(self, _exc: BaseException) -> None:
        pass

    def set_status(self, *_args: object) -> None:
        pass


def shutdown_telemetry() -> None:
    """Best-effort flush + shutdown of the tracer provider. No-op when
    :func:`init_telemetry` returned ``False``.
    """
    global _PROVIDER
    if _PROVIDER is None:
        return
    with contextlib.suppress(Exception):  # pragma: no cover
        _PROVIDER.shutdown()
    _PROVIDER = None
