"""Pluggable secret resolution.

Selects a provider based on the `SECRET_PROVIDER` environment variable
(`env` for local development, `keyvault` for deployed environments) and
caches results in-process for `SECRET_CACHE_TTL_SECONDS` seconds.

Public API
----------
- :func:get            resolve a secret or raise :class:SecretNotFoundError
- :func:get_or_none    resolve a secret or return `None`
- :func:prefetch       eagerly resolve and cache a list of names (for startup)
- :func:reset          clear the cache and the singleton provider (test helper)
"""

from _future_ import annotations

from .exceptions import SecretNotFoundError, SecretProviderError
from .providers import clear_cache, get, get_or_none, prefetch, reset

_all_ = [
    "SecretNotFoundError",
    "SecretProviderError",
    "clear_cache",
    "get",
    "get_or_none",
    "prefetch",
    "reset",
]