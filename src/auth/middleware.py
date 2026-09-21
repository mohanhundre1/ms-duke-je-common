"""Reusable authentication middleware for domain agent orchestrators.

Call ``authenticate_inbound(context)`` at the top of ``execute()`` in each
orchestrator agent. It extracts the Authorization header, validates via
``TokenValidator``, and returns the claims dict. Raises ``PermissionError``
on failure (the orchestrator should catch and emit a FAILED status).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Lazy-init: only created on first call to avoid import-time failures
# when AUTH_PROVIDER is not set (e.g. unit tests).
_validator = None

def _get_validator():
    global _validator
    if _validator is None:
        from ms_duke_je_common.auth.token_validator import get_token_validator
        _validator = get_token_validator()
    return _validator

def authenticate_inbound(context) -> dict:
    """Validate the inbound bearer token from the delegation agent.

    Parameters
    ----------
    context : RequestContext
        The a2a-sdk request context (has ``call_context.state['headers']``).

    Returns
    -------
    dict
        Validated claims (sub, azp, scp, etc.). Empty dict if auth is
        disabled (AUTH_PROVIDER=local and no token present).

    Raises
    ------
    PermissionError
        If a token is required but missing or invalid.
    """
    # If auth is disabled entirely (legacy local dev without OBO), skip
    auth_provider = os.environ.get("AUTH_PROVIDER", "local")

    headers = {}
    try:
        headers = context.call_context.state.get("headers", {})
    except Exception:
        pass

    auth = headers.get("authorization", "")

    if not auth.lower().startswith("bearer "):
        # No token provided
        if auth_provider == "entra_id":
            raise PermissionError("Missing Authorization: Bearer header")
        # Local dev without OBO — allow unauthenticated calls
        return {}

    token = auth.split(None, 1)[1]
    validator = _get_validator()
    claims = validator.validate(token)
    logger.info(
        "Authenticated inbound call: user=%s, caller=%s, scope=%s",
        claims.get("sub", "?"),
        claims.get("azp", "?"),
        claims.get("scp", "?"),
    )
    return claims

class A2AAuthMiddleware:
    """ASGI middleware that validates Bearer JWT on POST / before any processing.

    GET endpoints (health, agent-card) are exempt. Only POST requests to
    the A2A JSON-RPC root are guarded.

    In local dev (`AUTH_PROVIDER != entra_id`), missing tokens are
    allowed so Docker Compose setups without Azure AD still work.
    """

    # Only guard A2A JSON-RPC paths; other POST endpoints have route-level auth
    _GUARDED_PATHS = frozenset({"/", "/a2a/v1"})

    def __init__(self, app):
        self.app = app
        self.auth_provider = os.environ.get("AUTH_PROVIDER", "local")

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"] in self._GUARDED_PATHS
        ):
            from starlette.requests import Request
            from starlette.responses import JSONResponse

            request = Request(scope, receive)
            auth = request.headers.get("authorization", "")

            if not auth.lower().startswith("bearer "):
                if self.auth_provider == "entra_id":
                    logger.warning("A2A POST auth rejected: missing bearer token, path=%s", request.url.path)
                    resp = JSONResponse({"error": "Missing bearer token"}, status_code=401)
                    await resp(scope, receive, send)
                    return
                # Local dev — allow through
            else:
                try:
                    validator = _get_validator()
                    claims = validator.validate(auth.split(None, 1)[1])
                    logger.info(
                        "A2A POST auth accepted: path=%s, caller=%s, sub=%s",
                        request.url.path, claims.get("azp", claims.get("appid", "?")), claims.get("sub", "?")
                    )
                except Exception as exc:
                    logger.warning("A2A POST auth rejected: %s, path=%s", exc, request.url.path)
                    resp = JSONResponse({"error": "Unauthorized"}, status_code=401)
                    await resp(scope, receive, send)
                    return

        await self.app(scope, receive, send)

def authenticate_request(headers: dict) -> dict:
    """Validate OBO bearer token. No legacy fallback.

    This is the strict post-cutover version — always requires a valid
    bearer token regardless of AUTH_PROVIDER. Use this once the full
    OBO migration is complete and ``x-internal-auth`` is removed.

    Parameters
    ----------
    headers : dict
        HTTP headers dict (lowercase keys).

    Returns
    -------
    dict
        Validated claims: sub (user), azp (delegation-agent client ID), scp (scope).

    Raises
    ------
    PermissionError
        If the bearer token is missing or invalid.
    """
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise PermissionError("Missing Authorization: Bearer header")
    token = auth.split(None, 1)[1]
    validator = _get_validator()
    claims = validator.validate(token)
    logger.info(
        "Authenticated request (strict): user=%s, caller=%s, scope=%s",
        claims.get("sub", "?"),
        claims.get("azp", "?"),
        claims.get("scp", "?"),
    )
    return claims