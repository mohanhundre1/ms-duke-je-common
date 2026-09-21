"""OpenTelemetry metrics helpers used by resilience utilities."""

from __future__ import annotations

from collections.abc import Mapping

try:
    from opentelemetry import metrics as otel_metrics
except Exception:  # pragma: no cover - optional dependency
    otel_metrics = None


class _NoopCounter:
    def add(self, value: int, attributes: Mapping[str, str] | None = None) -> None:
        return


class OTelMetrics:
    """Small facade over OpenTelemetry counters with safe no-op fallback."""

    def __init__(self, service_name: str) -> None:
        self.service_name = service_name

        if otel_metrics is None:
            self._retry_counter = _NoopCounter()
            self._circuit_open_counter = _NoopCounter()
            self._circuit_state_counter = _NoopCounter()
            return

        meter = otel_metrics.get_meter("ms_duke_je_common", "0.1.0")
        self._retry_counter = meter.create_counter(
            name="duke.retry.total",
            description="Count of retry attempts",
            unit="1",
        )
        self._circuit_open_counter = meter.create_counter(
            name="duke.circuit.open.total",
            description="Count of circuit breaker opens",
            unit="1",
        )
        self._circuit_state_counter = meter.create_counter(
            name="duke.circuit.state_change.total",
            description="Count of circuit breaker state changes",
            unit="1",
        )

    def record_retry(self, *, target: str) -> None:
        self._retry_counter.add(1, {"service": self.service_name, "target": target})

    def record_circuit_open(self, *, name: str) -> None:
        attrs = {"service": self.service_name, "circuit": name}
        self._circuit_open_counter.add(1, attrs)
        self._circuit_state_counter.add(1, {**attrs, "state": "open"})

    def record_circuit_state_change(self, *, name: str, state: str) -> None:
        self._circuit_state_counter.add(
            1,
            {"service": self.service_name, "circuit": name, "state": state},
        )


_METRICS_BY_SERVICE: dict[str, OTelMetrics] = {}


def get_otel_metrics(service_name: str) -> OTelMetrics:
    """Return cached metrics facade for the given service."""
    existing = _METRICS_BY_SERVICE.get(service_name)
    if existing is not None:
        return existing
    created = OTelMetrics(service_name=service_name)
    _METRICS_BY_SERVICE[service_name] = created
    return created
