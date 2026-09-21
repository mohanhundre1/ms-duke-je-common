"""Cross-service tracing and correlation with W3C TraceContext and X-Correlation-Id.

Call ``configure_tracing`` and ``configure_correlation_logging`` once at startup,
then wrap the ASGI application in ``CorrelationIdMiddleware``.
"""

from __future__ import annotations

import contextvars
import logging
import os
import uuid
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = [
    "correlation_id_var",
    "CorrelationIdMiddleware",
    "CorrelationIdFilter",
    "configure_correlation_logging",
    "configure_tracing",
    "get_correlation_id",
    "get_trace_id",
    "correlation_headers",
    "HEADER_NAME",
]

HEADER_NAME = "X-Correlation-Id"
_HEADER_KEY = HEADER_NAME.lower()
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def _get_otel_trace_id() -> str:
    """Return the current OTel trace ID as 32 hexadecimal characters, or empty."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:  # pragma: no cover - tracing dependency is optional
        pass
    return ""


def _get_otel_span_id() -> str:
    """Return the current OTel span ID as 16 hexadecimal characters, or empty."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.span_id, "016x")
    except Exception:  # pragma: no cover
        pass
    return ""


def get_correlation_id() -> str:
    """Return the current correlation ID, or an empty string if unset."""
    return correlation_id_var.get() or ""


def get_trace_id() -> str:
    """Return the current W3C trace ID, falling back to a UUID4 hex string."""
    otel_tid = _get_otel_trace_id()
    return otel_tid if otel_tid else uuid.uuid4().hex


def correlation_headers() -> dict[str, str]:
    """Return the correlation header for calls outside instrumented httpx."""
    return {HEADER_NAME: get_correlation_id()}


_tracing_configured = False


def configure_tracing(service_name: str = "", otlp_endpoint: str = "") -> None:
    """Configure OTel tracing, export, and outbound httpx instrumentation once."""
    global _tracing_configured
    if _tracing_configured:
        return
    _tracing_configured = True

    svc_name = service_name or os.getenv("OTEL_SERVICE_NAME", "duke-agent")
    endpoint = otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        resource = Resource.create({"service.name": svc_name})
        provider = TracerProvider(resource=resource)
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
                )
            except ImportError:
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        elif os.getenv("OTEL_CONSOLE_SPANS", "").lower() in ("1", "true"):
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

        trace.set_tracer_provider(provider)
        _logger.info("OTel TracerProvider configured (service=%s)", svc_name)

        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
            _logger.info("httpx auto-instrumented for W3C TraceContext propagation")
        except ImportError:
            _logger.warning(
                "opentelemetry-instrumentation-httpx not installed; "
                "outbound traceparent propagation disabled"
            )
    except ImportError:
        _logger.warning(
            "opentelemetry-sdk not installed; tracing disabled "
            "(install opentelemetry-sdk for ACS RULE 35 compliance)"
        )


_logger = logging.getLogger(__name__)


class CorrelationIdMiddleware:
    """Bridge an inbound OTel trace ID to the response correlation header."""

    def __init__(self, app: ASGIApp) -> None:
        try:
            from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

            self.app: ASGIApp = OpenTelemetryMiddleware(app)
            self._has_otel = True
        except ImportError:
            self.app = app
            self._has_otel = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        incoming = next(
            (value.decode("latin-1") for key, value in scope.get("headers", [])
             if key.lower() == _HEADER_KEY.encode()),
            "",
        )
        cid = incoming or uuid.uuid4().hex
        token = correlation_id_var.set(cid)
        try:
            async def send_with_cid(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    active_cid = _get_otel_trace_id() or get_correlation_id()
                    response_headers = [
                        (key, value) for key, value in message.get("headers", [])
                        if key.lower() != _HEADER_KEY.encode()
                    ]
                    response_headers.append((_HEADER_KEY.encode(), active_cid.encode()))
                    message["headers"] = response_headers
                await send(message)

            await self.app(scope, receive, send_with_cid)
            otel_tid = _get_otel_trace_id()
            if otel_tid:
                correlation_id_var.set(otel_tid)
        finally:
            correlation_id_var.reset(token)


class CorrelationIdFilter(logging.Filter):
    """Inject correlation_id, otel_trace_id, and otel_span_id into LogRecords."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()  # type: ignore[attr-defined]
        record.otel_trace_id = _get_otel_trace_id() or "-"  # type: ignore[attr-defined]
        record.otel_span_id = _get_otel_span_id() or "-"  # type: ignore[attr-defined]
        return True


_LOG_FORMAT = (
    "%(asctime)s %(name)s %(levelname)s "
    "[cid=%(correlation_id)s tid=%(otel_trace_id)s sid=%(otel_span_id)s] "
    "%(message)s"
)


def configure_correlation_logging(
    level: int = logging.INFO,
    fmt: str = _LOG_FORMAT,
) -> None:
    """Install the filter and configure existing or new root handlers once."""
    root = logging.getLogger()
    if any(isinstance(f, CorrelationIdFilter) for f in root.filters):
        return

    cid_filter = CorrelationIdFilter()
    root.addFilter(cid_filter)

    if root.handlers:
        for handler in root.handlers:
            handler.setFormatter(logging.Formatter(fmt))
            if not any(isinstance(f, CorrelationIdFilter) for f in handler.filters):
                handler.addFilter(cid_filter)
    else:
        logging.basicConfig(level=level, format=fmt, force=True)
        for handler in root.handlers:
            if not any(isinstance(f, CorrelationIdFilter) for f in handler.filters):
                handler.addFilter(cid_filter)
