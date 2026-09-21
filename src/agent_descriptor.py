"""Neutral agent descriptor (plan A.2.8 / III.1#6).

Transport-free description of an agent's discovery card: agents author an
``AgentDescriptor`` and the transport adapter
(``transport/a2a/agent_card_adapter.py``) renders it to an ``a2a.types.AgentCard``.
This keeps the ``a2a`` SDK out of agent authoring code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

_DEFAULT_JSON_MODES: tuple[str, ...] = ("application/json",)


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    """A single advertised capability, transport-neutral."""

    id: str
    name: str
    description: str
    tags: tuple[str, ...] = ()
    input_modes: tuple[str, ...] = _DEFAULT_JSON_MODES
    output_modes: tuple[str, ...] = _DEFAULT_JSON_MODES


@dataclass(frozen=True, slots=True)
class AgentDescriptor:
    """Transport-neutral agent discovery description authored by agents.

    ``capabilities`` carries extra discovery metadata (e.g. the neutral event-model
    ``SCHEMA_VERSION``, A.3); the transport layer decides how to surface it.
    """

    name: str
    description: str
    path_prefix: str
    skills: tuple[SkillDescriptor, ...] = ()
    version: str = "0.1.0"
    base_url: str | None = None
    organization: str = "EY"
    provider_url: str | None = None
    streaming: bool = True
    push_notifications: bool = False
    input_modes: tuple[str, ...] = _DEFAULT_JSON_MODES
    output_modes: tuple[str, ...] = _DEFAULT_JSON_MODES
    bearer_jwt: bool = True
    capabilities: Mapping[str, Any] = field(default_factory=dict)


__all__ = ["AgentDescriptor", "SkillDescriptor"]
