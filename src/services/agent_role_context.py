"""Canonical, immutable role context for one exact Agent binding."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

if TYPE_CHECKING:
    from .conversation_agent_service import AgentExecutionBindingSnapshot


_ROLE_PREAMBLE = (
    "あなたはAoiTalk Operations Agentです。Character/Personaとして振る舞わず、"
    "このAgentRevisionに記載された職務だけを実行してください。"
)
_MAX_ROLE_FIELD_CHARS = 30_000


@dataclass(frozen=True, slots=True)
class AgentRoleContext:
    """Read-only common role text and its exact Agent/Revision identity."""

    mode: Literal["agent_role"]
    context_schema_version: int
    agent_id: str
    agent_revision_id: str
    role_context_sha256: str
    text: str


def _clean(value: Any) -> str:
    """Keep the established Chat role-prompt scalar normalization."""

    if value in (None, ""):
        return ""
    if isinstance(value, (str, UUID, int)):
        return str(value).strip()
    return ""


def _required_binding_id(binding: Any, field: str) -> str:
    value = _clean(getattr(binding, field, None))
    if not value:
        raise ValueError(f"Agent role context requires {field}")
    return value


def _canonical_role_text(binding: Any) -> str:
    parts = [_ROLE_PREAMBLE]
    for label, field in (
        ("Mission", "mission"),
        ("Responsibility", "responsibility_summary"),
        ("Operational instructions", "operational_instructions"),
    ):
        value = _clean(getattr(binding, field, None))
        if value:
            parts.append(f"{label}:\n{value[:_MAX_ROLE_FIELD_CHARS]}")
    return "\n\n".join(parts)


def build_agent_role_context(
    binding: "AgentExecutionBindingSnapshot",
) -> AgentRoleContext:
    """Build the common role context from one immutable binding snapshot.

    The digest covers only the canonical role text.  Harness instructions,
    provider envelopes, routing data, and Character/Persona state are outside
    this context and must be pinned separately by their owning boundaries.
    """

    agent_id = _required_binding_id(binding, "agent_id")
    agent_revision_id = _required_binding_id(binding, "agent_revision_id")
    text = _canonical_role_text(binding)
    return AgentRoleContext(
        mode="agent_role",
        context_schema_version=1,
        agent_id=agent_id,
        agent_revision_id=agent_revision_id,
        role_context_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
    )


__all__ = ["AgentRoleContext", "build_agent_role_context"]
