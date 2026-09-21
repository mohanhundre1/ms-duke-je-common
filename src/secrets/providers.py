"""Provider implementations and the public resolver functions.

The active provider is selected once per process (lazily) based on the
``SECRET_PROVIDER`` env var. Use :func:`reset` in tests to drop the singleton.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Iterable, Optional, Protocol

from .cache import TTLCache
from .exceptions import SecretNotFoundError, SecretProviderError

logger = logging.getLogger(__name__)
_DEFAULT_TTL = 300.0

class SecretProvider(Protocol):
    name: str

    def fetch(self, key: str) -> Optional[str]:
        """Return the secret value, or ``None`` if not present in the backend."""


# --------------------------------------------------------------------------- #
# Env provider — used for local development and as a fallback.                #
# --------------------------------------------------------------------------- #
class EnvProvider:
    name = "env"

    def fetch(self, key: str) -> Optional[str]:
        return os.environ.get(key)


# --------------------------------------------------------------------------- #
# Key Vault provider — lazy-imports azure deps so the base install stays light. #
# --------------------------------------------------------------------------- #
class KeyVaultProvider:
    name = "keyvault"

    # Mapping: env var name -> Key Vault secret name (AzureAD--* convention)
    _KV_NAME_OVERRIDES: dict[str, str] = {
        "AUTH_TENANT_ID": "AzureAD--TenantId",
        "AUTH_CLIENT_ID": "AzureAD--ClientId",
        "AUTH_CLIENT_SECRET": "AzureAD--ClientSecret",
        "AZURE_VALID_AUDIENCE": "AzureAD--Audience",
        "AZURE_VALID_ISSUER": "AzureAD--ValidIssuer",
        "AZURE_INSTANCE": "AzureAD--Instance",
        # Secondary AAD app-registration for dual-token validation.
        # Tokens whose ``aud`` matches the primary audience are validated
        # against AUTH_CLIENT_ID; tokens minted by the secondary AAD app
        # are validated against these fallback values.
        "AUTH_CLIENT_ID_SECONDARY": "AzureADSecondary--ClientId",
        "AZURE_VALID_AUDIENCE_SECONDARY": "AzureADSecondary--Audience",
        "AGENT_REGISTRY_BASE_URL": "Duke--ServiceLocation--agent-registry--base-url",
        "SERVICE_BASE_URL": "Duke--ServiceLocation--BaseUrl",
        # App Insights connection string for OpenTelemetry export. The default
        # lower-hyphen transform would yield 'otel--endpoint'; the vault secret
        # name preserves camel case, so map it explicitly.
        "OTEL_ENDPOINT": "Otel--Endpoint",
    }

    def __init__(self, vault_url: str) -> None:
        if not vault_url:
            raise SecretProviderError(
                "AZURE_KEYVAULT_URL must be set when SECRET_PROVIDER=keyvault"
            )
        logger.info(
            "[KV-INIT] vault_url=%s AZURE_CLIENT_ID=%s",
            vault_url,
            os.environ.get("AZURE_CLIENT_ID", "<not set>"),
        )
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError as exc:  # pragma: no cover - import guard
            raise SecretProviderError(
                "azure-identity and azure-keyvault-secrets are required for the "
                "keyvault provider. Install with: pip install ms-duke-je-common[secrets]"
            ) from exc

        self._vault_url = vault_url
        try:
            self._client = SecretClient(
                vault_url=vault_url,
                credential=DefaultAzureCredential(exclude_environment_credential=True),
            )
            logger.info("[KV-INIT] SecretClient created successfully")
        except Exception as exc:
            logger.error("[KV-INIT] SecretClient creation FAILED: %s", exc)
            raise SecretProviderError(f"Failed to create KV SecretClient: {exc}") from exc

    @classmethod
    def to_secret_name(cls, env_var: str) -> str:
        """Convert env var to KV secret name.

        Uses ``_KV_NAME_OVERRIDES`` for AzureAD--* secrets, otherwise falls
        back to lowercase-hyphenated (e.g. ``AZURE_OPENAI_API_KEY`` ->
        ``azure-openai-api-key``).
        """
        if env_var in cls._KV_NAME_OVERRIDES:
            return cls._KV_NAME_OVERRIDES[env_var]
        return env_var.lower().replace("_", "-")

    def fetch(self, key: str) -> Optional[str]:
        try:
            from azure.core.exceptions import ResourceNotFoundError

            missing_secret_errors: tuple[type[BaseException], ...] = (
                ResourceNotFoundError,
                KeyError,
            )
        except ImportError:  # pragma: no cover - exercised only without azure-core installed
            missing_secret_errors = (KeyError,)

        secret_name = self.to_secret_name(key)
        logger.info("[KV-FETCH] key=%s -> secret_name=%s", key, secret_name)
        try:
            value = self._client.get_secret(secret_name).value
            logger.info("[KV-FETCH] key=%s OK (value length=%d)", key, len(value) if value else 0)
            return value
        except missing_secret_errors:
            logger.warning("[KV-FETCH] key=%s NOT FOUND in vault", key)
            return None
        except Exception as exc:
            logger.error("[KV-FETCH] key=%s FAILED: %s", key, exc)
            raise SecretProviderError(
                f"Key Vault lookup failed for {secret_name!r} at {self._vault_url}"
            ) from exc


# --------------------------------------------------------------------------- #
# Singleton plumbing                                                          #
# --------------------------------------------------------------------------- #
_provider_lock = threading.Lock()
_provider: Optional[SecretProvider] = None
_cache: Optional[TTLCache] = None

def _build_provider() -> SecretProvider:
    kind = os.environ.get("SECRET_PROVIDER", "env").strip().lower()
    if kind == "env":
        return EnvProvider()
    if kind == "keyvault":
        return KeyVaultProvider(vault_url=os.environ.get("AZURE_KEYVAULT_URL", ""))
    raise SecretProviderError(
        f"Unknown SECRET_PROVIDER={kind!r}. Expected one of: env, keyvault."
    )

def _build_cache() -> TTLCache:
    raw = os.environ.get("SECRET_CACHE_TTL_SECONDS", "").strip()
    ttl = float(raw) if raw else _DEFAULT_TTL
    return TTLCache(ttl_seconds=ttl)

def _ensure() -> tuple[SecretProvider, TTLCache]:
    global _provider, _cache
    if _provider is None or _cache is None:
        with _provider_lock:
            if _provider is None:
                _provider = _build_provider()
            if _cache is None:
                _cache = _build_cache()
    return _provider, _cache


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #
def get(name: str) -> str:
    """Resolve ``name`` or raise :class:`SecretNotFoundError`."""
    value = get_or_none(name)
    if value is None:
        provider, _ = _ensure()
        raise SecretNotFoundError(name, provider.name)
    return value

def get_or_none(name: str) -> Optional[str]:
    """Resolve ``name`` or return ``None`` if missing."""
    provider, cache = _ensure()
    hit, cached = cache.get(name)
    if hit:
        return cached
    value = provider.fetch(name)
    cache.set(name, value)
    return value


def prefetch(names: Iterable[str]) -> dict[str, Optional[str]]:
    """Eagerly resolve and cache ``names``. Returns the resolved map."""
    return {n: get_or_none(n) for n in names}


def clear_cache() -> None:
    """Invalidate the TTL cache so the next lookup fetches fresh values from the
    provider. The provider singleton is preserved (no credential re-init).
    Intended for periodic refresh loops.
    """
    with _provider_lock:
        if _cache is not None:
            _cache.clear()


def reset() -> None:
    """Drop the cached provider and TTL cache. Intended for tests."""
    global _provider, _cache
    with _provider_lock:
        _provider = None
        _cache = None