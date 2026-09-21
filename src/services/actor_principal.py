"""Typed non-User actor foundation for WS01.

``ActorPrincipal`` is an immutable value object, not a universal principal
table.  It keeps human, Agent, and server-owned service identities distinct at
trusted boundaries and refuses malformed/inactive actors.  Historical tables
that only have ``user_id`` remain human-only; callers must use the explicit
projection helpers below when writing new actor-aware rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping
from uuid import UUID

from sqlalchemy import select

from ..memory.models import Agent, User


ActorKind = Literal["human", "agent", "service"]
ACTOR_KINDS = ("human", "agent", "service")
_SERVICE_KEY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{1,119}$")
_ALLOWED_SERVICE_KEYS = frozenset(
    {
        "aoitalk.system",
        "aoitalk.agent-harness",
        "aoitalk.migrations",
        "aoitalk.media-adapter",
    }
)


class ActorPrincipalError(ValueError):
    """Raised when a typed actor is malformed or cannot be resolved."""


@dataclass(frozen=True, slots=True)
class ActorPrincipal:
    kind: ActorKind
    identifier: str
    display_name: str | None = None

    def __post_init__(self) -> None:
        kind = str(self.kind or "").strip().lower()
        if kind not in {"human", "agent", "service"}:
            raise ActorPrincipalError("unsupported actor kind")
        identifier = str(self.identifier or "").strip()
        if kind in {"human", "agent"}:
            try:
                identifier = str(UUID(identifier))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ActorPrincipalError("human/agent actor identifier must be a UUID") from exc
        else:
            if not _SERVICE_KEY_RE.fullmatch(identifier) or identifier not in _ALLOWED_SERVICE_KEYS:
                raise ActorPrincipalError("unknown server-owned service key")
        if self.display_name is not None and len(str(self.display_name)) > 160:
            raise ActorPrincipalError("actor display name is too long")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "identifier", identifier)

    @classmethod
    def human(cls, user_or_id: Any, *, display_name: str | None = None) -> "ActorPrincipal":
        value = getattr(user_or_id, "id", user_or_id)
        return cls("human", str(value), display_name=display_name or getattr(user_or_id, "display_name", None))

    @classmethod
    def agent(cls, agent_or_id: Any, *, display_name: str | None = None) -> "ActorPrincipal":
        state = getattr(agent_or_id, "state", None)
        if isinstance(agent_or_id, Mapping):
            state = agent_or_id.get("state")
        if state is not None and str(state) != "active":
            raise ActorPrincipalError("agent is inactive")
        value = getattr(agent_or_id, "id", agent_or_id)
        if isinstance(agent_or_id, Mapping):
            value = agent_or_id.get("id") or agent_or_id.get("agent_id")
        mapped_name = agent_or_id.get("display_name") if isinstance(agent_or_id, Mapping) else None
        return cls("agent", str(value), display_name=display_name or mapped_name or getattr(agent_or_id, "display_name", None))

    @classmethod
    def service(cls, service_key: str) -> "ActorPrincipal":
        return cls("service", str(service_key))

    @property
    def id(self) -> str:
        """Stable typed identifier (UUID for human/agent, key for service)."""

        return self.identifier

    @property
    def user_id(self) -> str | None:
        return self.identifier if self.kind == "human" else None

    @property
    def agent_id(self) -> str | None:
        return self.identifier if self.kind == "agent" else None

    @property
    def service_key(self) -> str | None:
        return self.identifier if self.kind == "service" else None

    def to_dict(self) -> dict[str, Any]:
        """Safe audit projection; never includes credentials or ORM objects."""

        return {
            "kind": self.kind,
            "id": self.identifier,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "service_key": self.service_key,
            "display_name": self.display_name,
        }

    @classmethod
    def from_payload(cls, value: Any, *, trusted: bool = False) -> "ActorPrincipal":
        """Parse an explicit trusted-boundary payload without escalation.

        The parser is intentionally strict and does not accept ``user_id`` as
        an Agent identifier or infer a principal from arbitrary request/model
        fields.  Callers should prefer ``from_authenticated_user`` and
        ``from_agent`` after resolving the server-side row.
        """

        if not isinstance(value, Mapping):
            raise ActorPrincipalError("actor principal must be an object")
        kind = str(value.get("kind") or value.get("actor_type") or "").strip().lower()
        if kind == "human":
            identifier = value.get("id") or value.get("user_id")
        elif kind == "agent":
            identifier = value.get("id") or value.get("agent_id")
        elif kind == "service":
            if not trusted and value.get("_trusted") is not True:
                raise ActorPrincipalError("service principals require a trusted server factory")
            identifier = value.get("service_key") or value.get("id")
        else:
            raise ActorPrincipalError("actor kind is required")
        if "is_agent" in value and bool(value.get("is_agent")) != (kind == "agent"):
            raise ActorPrincipalError("inconsistent actor marker")
        return cls(kind, str(identifier or ""), display_name=value.get("display_name"))

    async def resolve(self, session, *, require_active: bool = True) -> Any:
        """Resolve this value object to the canonical server-side row/key."""

        if self.kind == "service":
            return self.identifier
        model = User if self.kind == "human" else Agent
        row = await session.get(model, UUID(self.identifier))
        if row is None:
            raise ActorPrincipalError("actor does not exist")
        if require_active:
            active = bool(getattr(row, "is_active", True)) if self.kind == "human" else getattr(row, "state", "") == "active"
            if not active:
                raise ActorPrincipalError("actor is inactive")
        return row


async def resolve_actor_principal(session, principal: ActorPrincipal, *, require_active: bool = True) -> Any:
    if not isinstance(principal, ActorPrincipal):
        raise ActorPrincipalError("principal must be an ActorPrincipal")
    return await principal.resolve(session, require_active=require_active)


def actor_fields(principal: ActorPrincipal) -> dict[str, Any]:
    """Return explicit durable actor columns with an XOR invariant."""

    if not isinstance(principal, ActorPrincipal):
        raise ActorPrincipalError("principal must be an ActorPrincipal")
    return {
        "actor_user_id": principal.user_id,
        "actor_agent_id": principal.agent_id,
        "actor_service_key": principal.service_key,
    }


def from_authenticated_user(user: Any) -> ActorPrincipal:
    if user is None:
        raise ActorPrincipalError("authenticated human is required")
    if isinstance(user, Mapping):
        actor_type = str(user.get("actor_type") or "").strip().lower()
        authority_source = str(user.get("_authority_source") or "").strip().lower()
        if (
            not user.get("id")
            or bool(user.get("is_agent"))
            or actor_type in {"agent", "service", "system"}
            or (actor_type not in {"", "human"})
            # Authenticated users can arrive through browser sessions,
            # bearer/mobile tokens, or an internal server handoff.  The
            # source marker is attached by the auth boundary; accept only
            # those explicit trusted values when actor_type is omitted.
            or (
                not actor_type
                and authority_source
                not in {"web_session", "bearer", "internal", "mobile", "api_token"}
            )
            or user.get("is_active") is False
        ):
            raise ActorPrincipalError("authenticated principal is not a human")
        return ActorPrincipal.human(user["id"], display_name=user.get("display_name"))
    actor_type = str(
        getattr(user, "actor_type", None)
        or getattr(user, "principal_kind", None)
        or ""
    ).strip().lower()
    if bool(getattr(user, "is_agent", False)) or actor_type not in {"", "human", "user"}:
        raise ActorPrincipalError("authenticated principal is not a human")
    if not bool(getattr(user, "is_active", True)):
        raise ActorPrincipalError("authenticated human is inactive")
    return ActorPrincipal.human(user)


def from_agent(agent: Any) -> ActorPrincipal:
    if agent is None:
        raise ActorPrincipalError("agent is required")
    if isinstance(agent, Mapping):
        if not agent.get("id") or str(agent.get("state") or "") != "active":
            raise ActorPrincipalError("agent is inactive")
    state = getattr(agent, "state", None)
    if state is not None and state != "active":
        raise ActorPrincipalError("agent is inactive")
    return ActorPrincipal.agent(agent)


__all__ = [
    "ActorKind",
    "ACTOR_KINDS",
    "ActorPrincipal",
    "ActorPrincipalError",
    "actor_fields",
    "from_authenticated_user",
    "from_agent",
    "resolve_actor_principal",
]

# Small compatibility aliases for callers that use noun-first naming.
ActorPrincipal.from_mapping = classmethod(lambda cls, value: cls.from_payload(value))  # type: ignore[attr-defined]
ActorPrincipal.from_user = classmethod(lambda cls, value: from_authenticated_user(value))  # type: ignore[attr-defined]
ActorPrincipal.from_service = classmethod(lambda cls, value: cls.service(value))  # type: ignore[attr-defined]
