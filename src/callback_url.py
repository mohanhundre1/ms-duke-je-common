"""Validation helpers for orchestrator callback URLs."""

from __future__ import annotations

import os
from urllib.parse import urlparse


DEFAULT_ALLOWED_CALLBACK_HOSTS = (
    "duke-api",
    "duke-delegation-agent",
    "fms-duke-je-api-service",
    "fms-duke-je-delegation-agent",
    "ms-duke-je-api-service",
    "ms-duke-je-delegation-agent",
    "localhost",
    "127.0.0.1",
    "host.docker.internal",
)


def _allowed_hosts() -> set[str]:
    raw = os.getenv("CALLBACK_URL_ALLOWED_HOSTS", "").strip()
    if not raw:
        return set(DEFAULT_ALLOWED_CALLBACK_HOSTS)
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def validate_callback_url(callback_url: str) -> str:
    """Validate callback URL and return a normalized string.

    Security checks:
    - only http/https schemes
    - hostname must be in CALLBACK_URL_ALLOWED_HOSTS allowlist
    - path must target /api/internal/callback/{job_id}
    - credentials/userinfo and URL fragments are rejected
    """
    candidate = (callback_url or "").strip()
    if not candidate:
        raise ValueError("callback_url is empty")

    parsed = urlparse(candidate)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError("callback_url must use http or https")

    if parsed.username or parsed.password:
        raise ValueError("callback_url must not include credentials")

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("callback_url host is missing")

    allowed_hosts = _allowed_hosts()
    if host not in allowed_hosts:
        raise ValueError(f"callback_url host '{host}' is not in allowlist")

    path = parsed.path or ""
    _VALID_CALLBACK_PREFIXES = ("/api/internal/callback/", "/api/v1/internal/callback/")
    if not any(path.startswith(p) for p in _VALID_CALLBACK_PREFIXES):
        raise ValueError("callback_url path must start with /api/internal/callback/ or /api/v1/internal/callback/")

    if parsed.fragment:
        raise ValueError("callback_url must not include URL fragment")

    return candidate
