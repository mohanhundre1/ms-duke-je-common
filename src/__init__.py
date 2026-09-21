"""Shared platform utilities for the Duke MJE automation services."""

__version__ = "0.5.10"

from .callback_url import validate_callback_url
from .file_classifier import classify_file
from .internal_auth import (
    InternalAuthMiddleware,
    compute_internal_auth_header,
)
from .path_validator import validate_workbook_path
from .agent_descriptor import AgentDescriptor, SkillDescriptor
from ms_duke_je_common.task_state_machine import (  # noqa: E402
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    InvalidTransition,
    emit_working,
    validate_transition,
)

try:
    from .internal_auth import InternalAuthMiddleware, compute_internal_auth_header
except Exception:  # pragma: no cover - optional in some branch snapshots
    InternalAuthMiddleware = None
    compute_internal_auth_header = None

from .otel_metrics import get_otel_metrics
from .resilience import CircuitBreaker, CircuitBreakerOpenError, retry_with_backoff
from .agent_registry import register_self_agent

# Re-export correlation utilities for convenient import
try:
    from ms_duke_je_common.correlation import (  # noqa: F401
        correlation_id_var,
        CorrelationIdMiddleware,
        CorrelationIdFilter,
        configure_correlation_logging,
        configure_tracing,
        get_correlation_id,
        correlation_headers,
        HEADER_NAME as CORRELATION_HEADER,
    )
except ImportError:  # pragma: no cover
    pass


__all__ = [
    "TERMINAL_STATES",
    "VALID_TRANSITIONS",
    "InvalidTransition",
    "emit_working",
    "validate_transition",
    "validate_workbook_path",
    "get_otel_metrics",
    "CircuitBreaker",
    "CircuitBreakerOpenError",
    "retry_with_backoff",
    "register_self_agent",
    "classify_file",
    "validate_callback_url",
    "validate_workbook_path",
    "register_self_agent",
    "AgentDescriptor",
    "SkillDescriptor",
]

if InternalAuthMiddleware is not None:
    __all__.append("InternalAuthMiddleware")
if compute_internal_auth_header is not None:
    __all__.append("compute_internal_auth_header")
