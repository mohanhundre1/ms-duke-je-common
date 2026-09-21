"""Hydrate process env from the active secret provider at startup.

See ms-duke-je-shared-agent-service/bootstrap_secrets.py for design notes.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_DEFAULT_REFRESH_INTERVAL = 300  # seconds
_REFRESH_JOIN_TIMEOUT_SECONDS = 5.0
_refresh_lock = threading.Lock()
_refresh_thread: threading.Thread | None = None
_refresh_stop_event: threading.Event | None = None
HYDRATED_ENV_VARS: tuple[str, ...] = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "DATABASE_URL",
    # AzureAD config (KV names: AzureAD--TenantId, AzureAD--ClientId, etc.)
    # AUTH_* prefix keeps App-Reg values separate from the MSI identity
    # that DefaultAzureCredential reads from AZURE_CLIENT_ID.
    "JWT_SECRET",
    "AUTH_TENANT_ID",
    "AUTH_CLIENT_ID",
    "AUTH_CLIENT_SECRET",
    "AZURE_VALID_AUDIENCE",
    "AZURE_VALID_ISSUER",
    "AZURE_INSTANCE",
    # Secondary AAD app-registration (dual-token validation). Optional -
    # left empty when only the primary app is used.
    # KV names: AzureADSecondary--ClientId, AzureADSecondary--Audience.
    "AUTH_CLIENT_ID_SECONDARY",
    "AZURE_VALID_AUDIENCE_SECONDARY",
    # Shared internal key for X-internal-auth on config-registry/callback.
    "INTERNAL_SERVICE_KEY",
    "SERVICE_BASE_URL",
)


def hydrate_env_from_secrets(names: tuple[str, ...] = HYDRATED_ENV_VARS) -> None:
    """Resolve each name via the secret provider and set it into os.environ.

    Existing values are NOT overwritten - local `.env` wins. Provider is
    selected by SECRET_PROVIDER (default `env` = no-op for local dev,
    `keyvault` = fetch from AZURE_KEYVAULT_URL via DefaultAzureCredential).
    """
    try:
        from ms_duke_je_common import secrets
    except ImportError:
        logger.warning(
            "ms-duke-je-common not installed; skipping secret hydration. "
            "Install with: pip install -e ../ms-duke-je-common"
        )
        return

    provider = os.environ.get("SECRET_PROVIDER", "env").strip().lower()
    logger.info(
        "Hydrating env from secret provider=%s, AZURE_KEYVAULT_URL=%s",
        provider,
        os.environ.get("AZURE_KEYVAULT_URL", "<not set>"),
    )

    for name in names:
        if os.environ.get(name):
            logger.info("  %s already set in env - skipping", name)
            continue
        try:
            value = secrets.get_or_none(name)
        except secrets.SecretProviderError as exc:
            logger.error("Failed to resolve secret %s: %s", name, exc)
            continue
        if value:
            os.environ[name] = value
            logger.info("  %s <- %s", name, provider)
        else:
            logger.warning("  %s not found in provider=%s", name, provider)


def start_refresh_loop(names: tuple[str, ...] = HYDRATED_ENV_VARS) -> None:
    """Start a background daemon thread that periodically re-fetches secrets.

    Controlled by ``SECRET_REFRESH_INTERVAL_SECONDS`` (default 300 s).
    Set to ``0`` to disable. The loop is a no-op when
    ``SECRET_PROVIDER=env`` (local dev) because the env provider has
    nothing new to return.

    The TTL cache is cleared before each refresh cycle so the provider is
    always hit for fresh values rather than serving the cached copy.
    """
    try:
        from ms_duke_je_common import secrets
    except ImportError:
        logger.warning(
            "ms-duke-je-common not installed; skipping refresh loop setup."
        )
        return

    provider = os.environ.get("SECRET_PROVIDER", "env").strip().lower()
    if provider == "env":
        logger.info("SECRET_PROVIDER=env - periodic secret refresh disabled (no-op for local dev)")
        return

    raw = os.environ.get("SECRET_REFRESH_INTERVAL_SECONDS", "").strip()
    if raw:
        try:
            interval = int(raw)
        except ValueError:
            logger.warning(
                "SECRET_REFRESH_INTERVAL_SECONDS=%r is invalid. Using default=%d",
                raw,
                _DEFAULT_REFRESH_INTERVAL,
            )
            interval = _DEFAULT_REFRESH_INTERVAL
    else:
        interval = _DEFAULT_REFRESH_INTERVAL
    if interval <= 0:
        logger.info("SECRET_REFRESH_INTERVAL_SECONDS=%d - periodic secret refresh disabled", interval)
        return

    global _refresh_thread, _refresh_stop_event
    with _refresh_lock:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            logger.info("Secret refresh loop already running; skipping duplicate start")
            return
        stop_event = threading.Event()
        _refresh_stop_event = stop_event

    logger.info(
        "Starting secret refresh loop: provider=%s interval=%ds names=%s",
        provider,
        interval,
        names,
    )

    def _loop(stop_event: threading.Event) -> None:
        while not stop_event.wait(interval):
            logger.info("Secret refresh: clearing cache and re-fetching from provider=%s", provider)
            try:
                secrets.clear_cache()
            except secrets.SecretProviderError as exc:
                logger.error("Secret refresh: failed to clear cache: %s", exc)
                continue
            for name in names:
                try:
                    value = secrets.get_or_none(name)
                except secrets.SecretProviderError as exc:
                    logger.error("Secret refresh: failed to resolve %s: %s", name, exc)
                    continue
                if value:
                    os.environ[name] = value
                    logger.debug("Secret refresh:   %s updated", name)
                else:
                    logger.warning("Secret refresh:   %s not found in provider=%s", name, provider)
        logger.info("Secret refresh: stop signal received; exiting loop")

    thread = threading.Thread(target=_loop, args=(stop_event,), name="secret-refresh", daemon=True)
    with _refresh_lock:
        _refresh_thread = thread
    thread.start()
    logger.info("Secret refresh daemon thread started (tid=%d)", thread.ident or 0)


def stop_refresh_loop(timeout_seconds: float = _REFRESH_JOIN_TIMEOUT_SECONDS) -> None:
    """Request the background refresh thread to stop and wait briefly."""
    global _refresh_thread, _refresh_stop_event
    with _refresh_lock:
        thread = _refresh_thread
        stop_event = _refresh_stop_event
    if thread is None or stop_event is None:
        return

    stop_event.set()
    if thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=timeout_seconds)

    with _refresh_lock:
        if _refresh_thread is thread:
            _refresh_thread = None
            _refresh_stop_event = None
