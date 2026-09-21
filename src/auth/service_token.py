"""Mint service-to-service tokens for headless (no user context) calls.

Used by :mod:`config_register` at startup when no user token exists.

Production (`AUTH_PROVIDER=entra_id`):
    Acquires an Azure AD **client-credentials** token via MSAL
    ``acquire_token_for_client()``. Uses the same app registration
    (`AUTH_CLIENT_ID` / `AUTH_TENANT_ID`) and credential resolution
    as :class:`AzureOboTokenExchanger` — Workload Identity on K8s,
    `AUTH_CLIENT_SECRET` in local Docker.

Local dev (`AUTH_PROVIDER=local`):
    Mints an HS256 JWT signed with `JWT_SECRET`.

Environment variables
---------------------
AUTH_PROVIDER
    `entra_id` for Azure AD, anything else for local HS256.
AUTH_CLIENT_ID / AUTH_TENANT_ID
    Azure AD app registration (used by MSAL).
AUTH_CLIENT_SECRET
    Client secret fallback when `AZURE_FEDERATED_TOKEN_FILE` is not set.
AZURE_VALID_AUDIENCE
    Target scope / audience for the token.
JWT_SECRET / JWT_ALGORITHM
    HS256 signing for local dev.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

_AUTH_PROVIDER = os.getenv("AUTH_PROVIDER", "local")
_JWT_SECRET = os.getenv("JWT_SECRET", "")
_JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
_AZURE_VALID_AUDIENCE = os.getenv("AZURE_VALID_AUDIENCE", "")

# Lazily initialized MSAL app
_msal_app = None


def _present(name: str) -> str:
    """Return a safe present/missing marker for an env var."""
    return "present" if os.getenv(name) else "missing"


def _get_msal_app():
    """Lazy-init MSAL ConfidentialClientApplication for client-credentials."""
    global _msal_app
    if _msal_app is not None:
        logger.info("service-token: reusing cached MSAL app")
        return _msal_app

    from msal import ConfidentialClientApplication

    federated_token_file = os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
    client_secret = os.environ.get("AUTH_CLIENT_SECRET", "").strip()

    # Prefer explicit client secret when present — it is reliable and does not
    # require a federated credential to be registered in Azure AD.
    # Fall back to workload-identity federated token only when no secret is set
    # (i.e. pure Workload Identity setup without any client secret in KV).
    if client_secret:
        credential = client_secret
        logger.info("service-token: using client_secret credential")
    elif federated_token_file:
        def _read_federated_token() -> str:
            with open(os.environ["AZURE_FEDERATED_TOKEN_FILE"]) as f:
                return f.read()
        credential = {"client_assertion": _read_federated_token}
        logger.info("service-token: using federated_token credential file=%s", federated_token_file)
    else:
        raise RuntimeError(
            "Neither AUTH_CLIENT_SECRET nor AZURE_FEDERATED_TOKEN_FILE is set. "
            "Cannot build MSAL ConfidentialClientApplication."
        )

    _msal_app = ConfidentialClientApplication(
        client_id=os.environ["AUTH_CLIENT_ID"],
        authority=f"https://login.microsoftonline.com/{os.environ['AUTH_TENANT_ID']}",
        client_credential=credential,
    )
    return _msal_app


def _make_azure_token() -> str:
    """Acquire an Azure AD token via client-credentials grant."""
    app = _get_msal_app()
    audience = os.getenv("AZURE_VALID_AUDIENCE", "")
    scope = f"{audience}/.default" if audience else ""
    logger.info(
        "service-token: acquiring client-credentials token audience=%s scope=%s",
        audience or "<missing>",
        scope or "<missing>",
    )
    if not scope:
        raise RuntimeError("AZURE_VALID_AUDIENCE not set — cannot request client-credentials token")

    result = app.acquire_token_for_client(scopes=[scope])
    if "error" in result:
        raise RuntimeError(
            f"Client-credentials token failed: {result.get('error_description', result['error'])}"
        )
    token = result["access_token"]
    logger.info(
        "service-token: acquired Azure AD token token_len=%d expires_in=%s",
        len(token),
        result.get("expires_in", "?"),
    )
    return token


def _make_local_token(service_id: str) -> str:
    """Mint a short-lived HS256 JWT for local dev."""
    import jwt as pyjwt

    logger.info(
        "service-token: minting local JWT service_id=%s jwt_secret=%s audience=%s",
        service_id,
        _present("JWT_SECRET"),
        os.getenv("AZURE_VALID_AUDIENCE", "") or "<missing>",
    )
    if not _JWT_SECRET:
        raise RuntimeError("JWT_SECRET not configured — cannot mint service token")
    payload: dict = {
        "sub": service_id,
        "role": "service",
        "type": "access",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    if _AZURE_VALID_AUDIENCE:
        payload["aud"] = _AZURE_VALID_AUDIENCE
    return pyjwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


def make_service_token(service_id: str = "internal-service") -> str:
    """Return a bearer token for service-to-service auth (no user context).

    Uses Azure AD client-credentials in production, HS256 JWT locally.
    """
    auth_provider = os.getenv("AUTH_PROVIDER", "local")
    logger.info(
        "service-token: make_service_token service_id=%s auth_provider=%s",
        service_id,
        auth_provider,
    )
    if auth_provider == "entra_id":
        return _make_azure_token()
    return _make_local_token(service_id)