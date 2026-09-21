"""Register local YAML/JSON config files with the central config registry.

Each service calls :func:`register_configs` at startup to sync its
``config/`` directory with the API's ``config_registry`` table. Files
whose SHA-256 hash hasn't changed are skipped (no-op).

Usage (in a service's ``serve.py`` or ``__main__.py``)::

    from ms_duke_je_common.config_register import register_configs

    # Agent service (domain configs)
    await register_configs(
        service_id="ms-duke-je-cog-agent-service",
        config_dir="agent/config",
        category="agent",
    )

    # Shared service (multiple categories - call once per directory)
    await register_configs(
        service_id="ms-duke-je-shared-agent-service",
        config_dir="normalization/config/schemas",
        category="normalization",
    )
    await register_configs(
        service_id="ms-duke-je-shared-agent-service",
        config_dir="validation/config/rules",
        category="validation",
    )
    await register_configs(
        service_id="ms-duke-je-shared-agent-service",
        config_dir="erp_template/config",
        category="erp_template",
    )

Environment variables
---------------------
CONFIG_REGISTRY_URL
    Base URL of the API service (default: ``http://fms-duke-je-api-service:8090``).
CONFIG_REGISTRY_ENABLED
    Set to ``false`` to skip registration entirely (useful for local dev).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {".yaml", ".yml", ".json"}


def _detect_config_type(file_path: Path) -> str:
    """Return config_type based on file extension."""
    ext = file_path.suffix.lower()
    if ext in (".yaml", ".yml"):
        return "yaml"
    if ext == ".json":
        return "json"
    return "yaml"


def _parse_file(file_path: Path) -> dict[str, Any]:
    """Read and parse a YAML or JSON file into a dict."""
    raw = file_path.read_text(encoding="utf-8")
    ext = file_path.suffix.lower()
    if ext == ".json":
        return json.loads(raw)
    # YAML: import lazily so services without pyyaml don't break.
    import yaml

    return yaml.safe_load(raw) or {}


def _hash_file(file_path: Path) -> str:
    """SHA-256 hex digest of the raw file bytes."""
    return hashlib.sha256(file_path.read_bytes()).hexdigest()


def scan_config_dir(config_dir: str | Path, category: str) -> list[dict[str, Any]]:
    """Discover config files under config_dir and return metadata list.

    Each entry contains: config_key, config_type, category, content_hash,
    content (parsed dict), file_path (absolute).
    """
    root = Path(config_dir).resolve()
    if not root.is_dir():
        logger.warning("Config directory not found: %s", root)
        return []

    entries: list[dict[str, Any]] = []
    for fp in sorted(root.rglob("*")):
        if not fp.is_file() or fp.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            continue
        try:
            content = _parse_file(fp)
            content_hash = _hash_file(fp)
        except Exception:
            logger.warning("Failed to parse config file: %s", fp, exc_info=True)
            continue

        # config_key = relative path from config_dir (e.g. "schemas/cog.yaml")
        config_key = str(fp.relative_to(root))

        entries.append({
            "config_key": config_key,
            "config_type": _detect_config_type(fp),
            "category": category,
            "content_hash": content_hash,
            "content": content,
            "file_path": str(fp),
        })

    return entries


async def register_configs(
    service_id: str,
    config_dir: str | Path,
    category: str = "agent",
    api_base_url: str | None = None,
) -> dict[str, str]:
    """Scan config_dir and POST each file to the config registry API.

    Args:
        service_id: Service identifier (e.g. "ms-duke-je-cog-agent-service").
        config_dir: Path to the directory containing config files.
        category: One of "pipeline", "normalization", "validation",
            "erp_template", "agent", "retention", "other".
        api_base_url: Override for the API service URL.

    Returns a dict mapping config_key to result: "created", "unchanged", or "error".
    Skips entirely if CONFIG_REGISTRY_ENABLED=false.
    """
    if os.getenv("CONFIG_REGISTRY_ENABLED", "true").lower() == "false":
        logger.info("Config registry disabled (CONFIG_REGISTRY_ENABLED=false)")
        return {}

    base_url = (
        api_base_url
        or os.getenv("CONFIG_REGISTRY_URL", "http://fms-duke-je-delegation-agent:8092")
    ).rstrip("/")
    endpoint = f"{base_url}/api/v1/internal/config-registry"

    entries = scan_config_dir(config_dir, category)
    if not entries:
        logger.info("No config files found in %s", config_dir)
        return {}

    # Import httpx lazily: not all services have it at import time.
    import httpx
    from ms_duke_je_common.correlation import correlation_headers

    headers: dict[str, str] = {}
    internal_key = os.getenv("INTERNAL_SERVICE_KEY", "").strip()
    if not internal_key:
        logger.error(
            "config-register: INTERNAL_SERVICE_KEY missing; "
            "strict internal-key auth is enabled for endpoint=%s",
            endpoint,
        )
        return {entry["config_key"]: "error:missing_internal_key" for entry in entries}

    headers["x-internal-auth"] = internal_key
    logger.info(
        "config-register: using internal service key for service=%s endpoint=%s",
        service_id,
        endpoint,
    )

    results: dict[str, str] = {}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        follow_redirects=True,
        headers=headers,
    ) as client:
        for entry in entries:
            payload = {
                "service_id": service_id,
                "config_key": entry["config_key"],
                "config_type": entry["config_type"],
                "category": entry["category"],
                "content_hash": entry["content_hash"],
                "content": entry["content"],
            }
            try:
                resp = await client.post(endpoint, json=payload, headers=correlation_headers())
                if resp.status_code in (200, 201):
                    body = resp.json()
                    status = "created" if resp.status_code == 201 else "unchanged"
                    results[entry["config_key"]] = status
                    logger.info(
                        "Config %s/%s -> %s (v%s)",
                        service_id, entry["config_key"], status,
                        body.get("version", "?"),
                    )
                else:
                    results[entry["config_key"]] = f"error:{resp.status_code}"
                    logger.warning(
                        "Config registry error for %s: %s %s",
                        entry["config_key"], resp.status_code, resp.text[:200],
                    )
            except Exception:
                results[entry["config_key"]] = "error:connection"
                logger.warning(
                    "Failed to register config %s/%s",
                    service_id, entry["config_key"], exc_info=True,
                )

    created = sum(1 for v in results.values() if v == "created")
    unchanged = sum(1 for v in results.values() if v == "unchanged")
    errors = sum(1 for v in results.values() if v.startswith("error"))
    logger.info(
        "Config registration complete for %s: %d created, %d unchanged, %d errors",
        service_id, created, unchanged, errors,
    )
    return results
