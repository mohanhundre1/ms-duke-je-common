"""Load YAML/JSON configs from the central config registry.

Resolution order:
    1. In-process cache (TTL-based)
    2. Config registry API (GET /api/v1/internal/config-registry/{service_id}/{config_key})

Usage::

    from ms_duke_je_common.config_loader import ConfigLoader

    loader = ConfigLoader(service_id="ms-duke-je-cog-agent-service")

    rates = await loader.load("rates.yaml")
    prompts = await loader.load("prompts.yaml")

Environment variables
---------------------
CONFIG_REGISTRY_URL
    Base URL of the API service (default: ``http://fms-duke-je-api-service:8090``).
CONFIG_CACHE_TTL
    Cache TTL in seconds (default: 18000 = 5 hours).
CONFIG_REGISTRY_TIMEOUT
    Registry HTTP timeout in seconds (default: 30 seconds).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any


logger = logging.getLogger(__name__)

_DEFAULT_TTL = int(os.getenv("CONFIG_CACHE_TTL", "18000"))
_DEFAULT_REGISTRY_TIMEOUT = int(os.getenv("CONFIG_REGISTRY_TIMEOUT", "30"))


class ConfigLoader:
    """Loads configs from the DB config registry."""

    def __init__(
        self,
        service_id: str,
        api_base_url: str | None = None,
        cache_ttl: int = _DEFAULT_TTL,
    ):
        self.service_id = service_id
        self.api_base_url = (
            api_base_url
            or os.getenv("CONFIG_REGISTRY_URL", "http://fms-duke-je-delegation-agent:8092")
        ).rstrip("/")
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def load(self, config_key: str) -> dict[str, Any]:
        """Load a config: cache -> DB registry."""
        cached = self._cache.get(config_key)
        if cached and (time.time() - cached[0]) < self.cache_ttl:
            return cached[1]

        data = await self._fetch_from_registry(config_key)
        self._cache[config_key] = (time.time(), data)
        logger.debug("Config %s/%s loaded from registry", self.service_id, config_key)
        return data

    async def _fetch_from_registry(self, config_key: str) -> dict[str, Any]:
        """GET the active config content from the registry API."""
        import httpx
        from ms_duke_je_common.correlation import correlation_headers

        url = (
            f"{self.api_base_url}/api/v1/internal/config-registry"
            f"/{self.service_id}/{config_key}"
        )
        headers: dict[str, str] = {}
        auth_source = "none"
        internal_key = os.getenv("INTERNAL_SERVICE_KEY", "").strip()
        if not internal_key:
            raise RuntimeError(
                "INTERNAL_SERVICE_KEY missing; strict "
                "internal-key auth is enabled for config registry"
            )
        headers["x-internal-auth"] = internal_key
        auth_source = "internal_service_key"
        try:
            from .correlation import correlation_headers
            headers.update(correlation_headers())
        except ImportError:
            pass

        try:
            logger.info(
                "config-loader: fetching service=%s config=%s auth_source=%s auth_header=%s internal_key=%s tenant=%s url=%s",
                self.service_id,
                config_key,
                auth_source,
                "present" if "Authorization" in headers else "missing",
                "present" if "x-internal-auth" in headers else "missing",
                "present" if "X-Tenant-Id" in headers else "missing",
                url,
            )
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(float(_DEFAULT_REGISTRY_TIMEOUT)),
                follow_redirects=True,
            ) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                entry = resp.json()
                content = entry.get("content")
                if content is None:
                    raise ValueError(
                        f"Config {self.service_id}/{config_key} has no content in registry"
                    )

                # Registry stores YAML as a string; parse it into a dict.
                if isinstance(content, str):
                    import yaml
                    content = yaml.safe_load(content)
                    if not isinstance(content, dict):
                        raise ValueError(
                            f"Config {self.service_id}/{config_key} parsed to "
                            f"{type(content).__name__}, expected dict"
                        )
                return content
        except Exception as exc:
            logger.error(
                "Failed to fetch config %s/%s from %s: %s",
                self.service_id, config_key, url, exc,
            )
            raise

    def invalidate(self, config_key: str | None = None) -> None:
        """Clear cached config(s)."""
        if config_key:
            self._cache.pop(config_key, None)
        else:
            self._cache.clear()
