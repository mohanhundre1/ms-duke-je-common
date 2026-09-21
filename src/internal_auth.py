"""Internal service authentication helpers and middleware."""

from __future__ import annotations

import hmac
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def compute_internal_auth_header(secret: str) -> str:
    """Return the outbound internal auth header value.

    This currently forwards the shared secret directly. The helper keeps
    callers centralized so the scheme can evolve without changing call sites.
    """
    return (secret or "").strip()


class InternalAuthMiddleware(BaseHTTPMiddleware):
    """Validate x-internal-auth for internal-only routes.

    Health probes are always allowed so liveness/readiness checks are not
    blocked by auth rollout.
    """

    def __init__(self, app, *, internal_service_key: str | None = None) -> None:
        super().__init__(app)
        self._expected = (
            (internal_service_key or "").strip()
            or os.getenv("INTERNAL_SERVICE_KEY", "").strip()
        )

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in {"/health", "/healthz", "/ready", "/readyz", "/live", "/livez"}:
            return await call_next(request)
        if path in {"/.well-known/agent-card.json", "/.well-known/agent.json"}:
            return await call_next(request)

        if not self._expected:
            return JSONResponse(
                {"error": "INTERNAL_SERVICE_KEY not configured"},
                status_code=503,
            )

        provided = request.headers.get("x-internal-auth", "")
        if not provided:
            # Also accept INTERNAL_SERVICE_KEY in Authorization: Bearer header
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                provided = auth_header[7:].strip()

        if not provided:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        if hmac.compare_digest(self._expected, provided):
            return await call_next(request)

        return JSONResponse({"error": "unauthorized"}, status_code=401)
