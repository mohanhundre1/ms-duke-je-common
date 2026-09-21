"""Self-registering helper for agent pods.

Each agent (delegation, qna, orchestrator, ...) calls
``func:register_self_agent`` at startup to upsert its own row in the
shared ``agents`` Postgres table. The delegation agent's bearer loop
then resolves downstream agent URLs from this registry instead of
relying on per-pod env vars.

The helper is intentionally minimal: it accepts an ``asyncpg`` pool
that the caller already manages, performs an idempotent UPSERT, and
logs the outcome. It does not own DB lifecycle.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def register_self_agent(
    *,
    pool: Any,
    agent_id: str,
    a2a_url: str,
    category: str,
    display_name: str = "",
    skills: list[str] | None = None,
) -> bool:
    """Upsert an ``agents`` row for the calling pod.

    Args:
        pool: An ``asyncpg.Pool`` (or a wrapper exposing ``.execute``).
        agent_id: Stable identifier (e.g. ``qna-agent``).
        a2a_url: Public A2A endpoint URL the agent advertises.
        category: One of ``orchestrator`` | ``delegation`` | ``qna`` |
            ``shared`` | ``domain`` (free-form, used by callers to
            filter the registry).
        display_name: Optional human-readable name.
        skills: Optional list of skill identifiers; persisted under
            ``config.skills``.

    Returns:
        ``True`` if the row was upserted, ``False`` if ``a2a_url`` was
        empty (no-op).
    """
    if not a2a_url:
        logger.warning(
            "register_self_agent: no a2a_url for %s — skipping",
            agent_id,
        )
        return False

    config_json = json.dumps({"skills": list(skills or [])})

    await pool.execute(
        """
        INSERT INTO agents (
            id, display_name, category, a2a_url,
            is_active, config, created_at, updated_at
        )
        VALUES ($1, $2, $3, $4, TRUE, $5::jsonb, NOW(), NOW())
        ON CONFLICT (id) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            category     = EXCLUDED.category,
            a2a_url      = EXCLUDED.a2a_url,
            is_active    = TRUE,
            config       = EXCLUDED.config,
            updated_at   = NOW()
        """,
        agent_id,
        display_name,
        category,
        a2a_url,
        config_json,
    )

    logger.info(
        "agents registry: upserted %s (%s) → %s",
        agent_id,
        category,
        a2a_url,
    )
    return True


# ── Agent URL resolution from registry payload ──────────────────────────────


def _extract_a2a_url(agent: dict[str, Any]) -> str | None:
    """Return the ``a2aUrl`` in the latest version if present and non-empty."""
    latest = agent.get("latestVersion") or {}
    return latest.get("a2aUrl") or None


def _agent_has_skill_tag(
    agent: dict[str, Any],
    tag: str,
) -> bool:
    """Check whether any skill on the agent has a tag containing ``tag``."""
    latest = agent.get("latestVersion") or {}
    for skill in latest.get("skills") or []:
        tags_raw = skill.get("tags") or ""

        # tags may be a JSON-encoded list string or a real list
        if isinstance(tags_raw, str):
            if tag.lower() in tags_raw.lower():
                return True
        elif isinstance(tags_raw, list):
            if any(tag.lower() in t.lower() for t in tags_raw):
                return True

    return False


def _agent_has_skill_id(
    agent: dict[str, Any],
    skill_id: str,
) -> bool:
    """Check whether any skill on the agent matches the given ``skillId``."""
    latest = agent.get("latestVersion") or {}
    for skill in latest.get("skills") or []:
        sid = (skill.get("skillId") or "").lower()
        if sid == skill_id.lower():
            return True
    return False


def resolve_agent_url(
    registered_agents: list[dict[str, Any]],
    *,
    category: str | None = None,
    name: str | None = None,
    skill_tag: str | None = None,
    skill_id: str | None = None,
    is_delegation_agent: bool | None = None,
) -> str | None:
    """Extract the A2A URL from a ``registered_agents`` list.

    Resolution order:

    1. Match by ``skill_id`` (exact skillId match, with optional category
       and isDelegationAgent filters).
    2. Match by ``name`` (case-insensitive substring of agent name).
    3. If ``name`` is given but no match, fall back to
       ``category="Domain"`` + skill tag containing ``name``.
    4. Match by ``category`` alone (first active agent in that category).
    5. Match by explicit ``skill_tag`` in the Shared category.

    Args:
        registered_agents: Full list of agent objects from the registry API.
        category: Filter by category (case-insensitive), e.g. ``Domain``,
            ``Shared``.
        name: Filter by agent name (case-insensitive substring match).
            Also used as a skill-tag fallback when the name match fails.
        skill_tag: Explicit skill tag to search for (category defaults to
            ``Shared``).
        skill_id: Match exact ``latestVersion.skills[].skillId``.
        is_delegation_agent: If set, filter agents by the
            ``isDelegationAgent`` flag.

    Returns:
        The ``latestVersion.a2aUrl`` of the first matching active agent,
        or ``None`` if no match is found.
    """
    active = [a for a in registered_agents if a.get("isActive")]

    # Apply isDelegationAgent filter when specified
    if is_delegation_agent is not None:
        active = [
            a
            for a in active
            if a.get("isDelegationAgent") == is_delegation_agent
        ]

    # 1. Match by skill_id (+ optional category filter)
    if skill_id:
        for agent in active:
            if category:
                if (agent.get("category") or "").lower() != category.lower():
                    continue
            if _agent_has_skill_id(agent, skill_id):
                url = _extract_a2a_url(agent)
                if url:
                    return url

    # 2. Try matching by name (+ optional category filter)
    if name:
        for agent in active:
            if category:
                if (agent.get("category") or "").lower() != category.lower():
                    continue
            agent_name = (agent.get("name") or "").lower()
            if name.lower() in agent_name:
                url = _extract_a2a_url(agent)
                if url:
                    return url

    # 3. Fallback: search Shared agents whose skill tags contain ``name``
    tag_to_find = skill_tag or name
    for agent in active:
        if (agent.get("category") or "").lower() != "shared":
            continue
        if _agent_has_skill_tag(agent, tag_to_find):
            url = _extract_a2a_url(agent)
            if url:
                return url

    # 4. Match by category alone (no name or skill_id filter)
    if category and not name and not skill_id:
        for agent in active:
            if (agent.get("category") or "").lower() != category.lower():
                continue
            url = _extract_a2a_url(agent)
            if url:
                return url

    # 5. Explicit skill_tag search (no name given)
    if skill_tag and not name:
        fallback_cat = (category or "shared").lower()
        for agent in active:
            if (agent.get("category") or "").lower() != fallback_cat:
                continue
            if _agent_has_skill_tag(agent, skill_tag):
                url = _extract_a2a_url(agent)
                if url:
                    return url

    return None
