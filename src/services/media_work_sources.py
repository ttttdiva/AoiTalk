"""Common-runtime WorkSource bridges for MediaOps.

MediaOps owns the meaning and lifecycle of Personas, research, generation and
publication rows.  This module only *projects* executable domain state into
the durable AgentWork runtime.  It deliberately does not define another
queue, claim loop or MediaOps source of truth.

The common runtime was introduced after the first MediaOps services and has
evolved during rolling deployments.  The bridges therefore use a small
duck-typed coordinator boundary (``materialize_work_item``/``upsert``) and
lazy imports for optional identity models.  This keeps older SQLite fixtures
and manual MediaOps deployments importable while allowing the production
coordinator to provide the durable idempotency/lease semantics.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import import_module
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID


MAX_CANDIDATES = 100
MAX_TEXT = 8_000
MAX_EVIDENCE = 20
MAX_LIST = 32

_SENSITIVE_TOKENS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "password",
        "prompt",
        "private_key",
        "provider_response",
        "raw_response",
        "refresh_token",
        "response",
        "secret",
        "session",
        "signature",
        "token",
        "transcript",
        "environment",
        "env",
        "path",
    }
)
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_OPAQUE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_SAFE_STATUSES = frozenset(
    {
        "pending",
        "claimed",
        "running",
        "awaiting_approval",
        "blocked",
        "retry_wait",
        "uncertain",
        "succeeded",
        "failed",
        "cancelled",
        "dead_letter",
        "transient",
        "permanent",
    }
)


class MediaWorkSourceError(RuntimeError):
    """Base error for source discovery/materialization failures."""


class MediaWorkSourceDisabled(MediaWorkSourceError):
    """Raised when autonomous MediaOps is not enabled."""

    code = "media_operations_autonomy_disabled"


class MediaWorkSourceUnavailable(MediaWorkSourceError):
    """Raised when an optional domain source cannot be inspected."""

    code = "media_source_unavailable"


@dataclass(frozen=True, slots=True)
class WorkCandidate(Mapping[str, Any]):
    """Safe, coordinator-compatible projection of one executable intent.

    The field names intentionally mirror ``agent_work_runtime.WorkCandidate``
    so a source can be registered directly with the common coordinator.  Media
    details (including the publication approval marker) live under bounded
    ``metadata`` rather than extending the common persistence contract.
    """

    source_type: str
    source_id: str
    source_revision: str
    intent_key: str
    domain: str = "media"
    space_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    persona_id: str | None = None
    app_id: str | None = None
    assigned_agent_id: str | None = None
    agent_revision_id: str | None = None
    required_capabilities: tuple[str, ...] = ()
    execution_adapter: str = "media"
    priority: int = 0
    not_before: datetime | None = None
    deadline: datetime | None = None
    max_attempts: int = 3
    concurrency_key: str | None = None
    budget_reservation: Mapping[str, Any] | None = None
    root_work_item_id: str | None = None
    parent_work_item_id: str | None = None
    causation_id: str | None = None
    causal_depth: int = 0
    mutation_fingerprint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the generic runtime envelope with safe media metadata."""

        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "intent_key": self.intent_key,
            "domain": self.domain,
            "space_id": self.space_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "persona_id": self.persona_id,
            "app_id": self.app_id,
            "assigned_agent_id": self.assigned_agent_id,
            "agent_revision_id": self.agent_revision_id,
            "required_capabilities": list(self.required_capabilities),
            "execution_adapter": self.execution_adapter,
            "priority": int(self.priority),
            "not_before": self.not_before,
            "deadline": self.deadline,
            "max_attempts": int(self.max_attempts),
            "concurrency_key": self.concurrency_key,
            "budget_reservation": _safe_mapping(self.budget_reservation or {}, max_depth=2),
            "root_work_item_id": self.root_work_item_id,
            "parent_work_item_id": self.parent_work_item_id,
            "causation_id": self.causation_id,
            "causal_depth": int(self.causal_depth),
            "mutation_fingerprint": self.mutation_fingerprint,
            "metadata": _safe_mapping(self.metadata or {}, max_depth=3),
        }

    def normalized(self) -> Any:
        """Adapt to the canonical runtime candidate when it is available."""

        try:
            runtime_candidate = import_module(
                ".agent_work_runtime", package=__package__
            ).WorkCandidate
        except Exception:
            return self
        return runtime_candidate(**self.to_dict()).normalized()

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())

    @property
    def payload(self) -> Mapping[str, Any]:
        """Compatibility view; payload is persisted only inside metadata."""

        value = self.metadata.get("payload") if isinstance(self.metadata, Mapping) else None
        return value if isinstance(value, Mapping) else {}

    @property
    def approval_required(self) -> bool:
        return bool(self.metadata.get("approval_required")) if isinstance(self.metadata, Mapping) else False


def _await(value: Any) -> Awaitable[Any] | Any:
    return value


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value in (None, ""):
        return None
    rendered = str(value).strip()
    if not rendered:
        return None
    return rendered[:limit]


def _id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, UUID):
        return str(value)
    rendered = str(value).strip()
    if not rendered or len(rendered) > 256 or not _OPAQUE_REF.fullmatch(rendered):
        return None
    return rendered


def _bounded_int(value: Any, *, default: int = 0) -> int:
    if value in (None, "") or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bounded_text_list(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if isinstance(value, (bytes,)) or not isinstance(value, Sequence):
        return []
    result: list[str] = []
    for item in list(value)[:limit]:
        rendered = _text(item, item_limit)
        if rendered and rendered not in result:
            result.append(rendered)
    return result


def _iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        current = value
    else:
        try:
            current = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def _safe_url(value: Any) -> str | None:
    """Keep public source URLs while dropping query/fragment credentials."""

    rendered = _text(value, 2_000)
    if rendered is None:
        return None
    try:
        parts = urlsplit(rendered)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None:
        return None
    # Query values are not needed for provenance and can contain signed tokens
    # even when the parameter name is innocuous.  Drop the entire query.
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc,
            parts.path[:1_000],
            "",
            "",
        )
    )


def _safe_mapping(value: Any, *, max_depth: int = 3, max_items: int = MAX_LIST) -> Any:
    """Recursively project bounded JSON without secret-shaped keys."""

    if max_depth < 0:
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:max_items]:
            key = str(raw_key)
            lowered = key.lower().replace("-", "_")
            if not _SAFE_KEY.fullmatch(key) or lowered in _SENSITIVE_TOKENS:
                continue
            if any(token in lowered for token in _SENSITIVE_TOKENS):
                continue
            if lowered in {"url", "source_url", "deep_link", "remote_url"}:
                safe_url = _safe_url(raw_value)
                if safe_url is not None:
                    result[key] = safe_url
                continue
            projected = _safe_mapping(raw_value, max_depth=max_depth - 1, max_items=max_items)
            if projected is not None:
                result[key] = projected
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            projected
            for item in list(value)[:max_items]
            if (projected := _safe_mapping(item, max_depth=max_depth - 1, max_items=max_items))
            is not None
        ]
    if isinstance(value, str):
        return _safe_provider_text(value, MAX_TEXT)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _text(value, 512)


def _safe_provider_text(value: Any, limit: int) -> str | None:
    """Bound provider prose and redact credential/path-shaped values."""

    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.casefold()
    if any(
        marker in lowered
        for marker in (
            "bearer ",
            "api_key",
            "apikey",
            "password=",
            "token=",
            "secret=",
            "credential=",
            "authorization:",
            "cookie=",
        )
    ):
        return "[REDACTED]"
    if re.search(r"(?:[A-Za-z]:[\\/]|/(?:home|users|tmp|var|etc|appdata)/)", text, re.IGNORECASE):
        return "[REDACTED]"
    return text[:limit]


def _safe_evidence(values: Any) -> list[dict[str, Any]]:
    """Normalize URL evidence to the MediaOps typed evidence shape."""

    if values is None:
        return []
    if isinstance(values, Mapping):
        values = [values]
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        return []
    result: list[dict[str, Any]] = []
    for raw in list(values)[:MAX_EVIDENCE]:
        if isinstance(raw, str):
            url = _safe_url(raw)
            if url:
                result.append({"type": "url", "url": url, "label": None, "note": None})
            continue
        if not isinstance(raw, Mapping):
            continue
        url = _safe_url(raw.get("url") or raw.get("source_url"))
        if not url:
            continue
        result.append(
            {
                "type": "url",
                "url": url,
                "label": _safe_provider_text(
                    raw.get("label") or raw.get("title"),
                    255,
                ),
                "note": _safe_provider_text(
                    raw.get("note") or raw.get("snippet"),
                    1_000,
                ),
            }
        )
    return result


def _callable(obj: Any, *names: str) -> Callable[..., Any] | None:
    for name in names:
        method = getattr(obj, name, None)
        if callable(method):
            return method
    return None


async def _call_compatible(
    method: Callable[..., Any],
    *,
    positional: Sequence[Any] = (),
    kwargs: Mapping[str, Any] | None = None,
) -> Any:
    """Call evolving adapters without masking provider exceptions.

    Only unsupported keyword arguments are filtered.  A ``TypeError`` raised
    *inside* a called method is allowed to propagate, avoiding accidental
    duplicate provider submissions.
    """

    call_kwargs = dict(kwargs or {})
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        parameters = signature.parameters
        positional_names = [
            parameter.name
            for parameter in parameters.values()
            if parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
        ]
        for name in positional_names[: len(positional)]:
            call_kwargs.pop(name, None)
        if not any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            call_kwargs = {key: value for key, value in call_kwargs.items() if key in parameters}
    return await _maybe_await(method(*tuple(positional), **call_kwargs))


def _feature_enabled(checker: Callable[[], Any] | bool | None) -> bool:
    # A test/embedding override may further restrict the gate, but can never
    # enable autonomous work when the effective profile disables it (notably
    # Enterprise, where these capabilities are hard-denied).
    override = True
    if checker is not None:
        try:
            override = bool(checker() if callable(checker) else checker)
        except Exception:
            override = False
    try:
        from ..features import Features

        global_enabled = bool(
            Features.autonomous_agent_runtime()
            and Features.media_operations_autonomy()
        )
        return global_enabled and override
    except Exception:
        return False if checker is None else override


def _hash_key(*parts: Any) -> str:
    material = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _rows(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        for key in ("items", "rows", "results", "routines", "plans", "proposals", "data"):
            if isinstance(value.get(key), Sequence) and not isinstance(value.get(key), (str, bytes)):
                return list(value[key])
        return [value]
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return [value]
    return list(value)


async def _resolve_optional_agent(
    *,
    session: Any,
    persona_id: str | None,
    project_id: str | None,
    space_id: str | None = None,
    resolver: Callable[..., Any] | None,
    service: Any | None,
) -> dict[str, Any] | None:
    """Resolve an explicit active PersonaOperatorAssignment.

    No owner/user fallback is allowed.  Deployments can provide a dedicated
    resolver; otherwise the identity models are queried lazily when present.
    """

    if not persona_id:
        return None
    if resolver is not None:
        resolver_method = resolver if callable(resolver) else _callable(
            resolver,
            "resolve",
            "resolve_operator",
            "get",
        )
        if resolver_method is None:
            return None
        try:
            try:
                result = await _call_compatible(
                    resolver_method,
                    kwargs={
                        "persona_id": persona_id,
                        "project_id": project_id,
                        "space_id": space_id,
                        "session": session,
                    },
                )
            except TypeError:
                result = await _call_compatible(
                    resolver_method,
                    positional=(persona_id,),
                    kwargs={
                        "project_id": project_id,
                        "space_id": space_id,
                        "session": session,
                    },
                )
        except Exception:
            return None
        if result is None:
            return None
        if isinstance(result, Mapping):
            state = str(result.get("state") or result.get("assignment_state") or "active").strip().lower()
            if state != "active":
                return None
            agent_id = _id(result.get("agent_id") or result.get("id"))
            if agent_id:
                return {
                    "agent_id": agent_id,
                    "assignment_id": _id(result.get("assignment_id")),
                    "agent_revision_id": _id(result.get("agent_revision_id") or result.get("revision_id")),
                    "role": _text(result.get("role"), 32),
                }
        agent_id = _id(_field(result, "agent_id") or _field(result, "id"))
        state = str(_field(result, "state", "active") or "active").strip().lower()
        if state != "active":
            return None
        return {
            "agent_id": agent_id,
            "agent_revision_id": _id(_field(result, "agent_revision_id") or _field(result, "revision_id")),
        } if agent_id else None
    if service is not None:
        method = _callable(service, "resolve_persona_operator", "get_persona_operator")
        if method is not None:
            try:
                try:
                    result = await _call_compatible(
                        method,
                        kwargs={
                            "persona_id": persona_id,
                            "project_id": project_id,
                            "space_id": space_id,
                            "session": session,
                        },
                    )
                except TypeError:
                    result = await _call_compatible(
                        method,
                        positional=(persona_id,),
                        kwargs={
                            "project_id": project_id,
                            "space_id": space_id,
                            "session": session,
                        },
                    )
            except Exception:
                result = None
            if result is not None:
                agent_id = _id(_field(result, "agent_id") or _field(result, "id"))
                if agent_id:
                    return {
                        "agent_id": agent_id,
                        "assignment_id": _id(_field(result, "assignment_id")),
                        "agent_revision_id": _id(_field(result, "agent_revision_id") or _field(result, "revision_id")),
                    }
    if session is None:
        return None
    try:
        models = import_module("..memory.models.agent_identity", package=__package__)
        assignment_model = getattr(models, "PersonaOperatorAssignment")
        agent_model = getattr(models, "Agent")
        revision_model = getattr(models, "AgentRevision")
        from sqlalchemy import select

        statement = (
            select(assignment_model, agent_model, revision_model)
            .join(agent_model, agent_model.id == assignment_model.agent_id)
            .join(revision_model, revision_model.agent_id == agent_model.id)
            .where(
                assignment_model.persona_id == UUID(str(persona_id)),
                assignment_model.state == "active",
                agent_model.state == "active",
            )
            .order_by(assignment_model.is_primary.desc(), revision_model.version.desc(), assignment_model.created_at.asc())
            .limit(1)
        )
        result = await _maybe_await(session.execute(statement))
        row = result.first() if callable(getattr(result, "first", None)) else None
        if row is None:
            return None
        assignment, agent, revision = row
        now = datetime.utcnow()
        for field_name in ("active_from", "active_until"):
            value = getattr(assignment, field_name, None)
            if value is None:
                continue
            if isinstance(value, datetime) and value.tzinfo is not None:
                value = value.replace(tzinfo=None)
            if field_name == "active_from" and value > now:
                return None
            if field_name == "active_until" and value <= now:
                return None
        role = str(getattr(assignment, "role", "operator") or "operator").strip().lower()
        if role not in {"operator", "strategist", "researcher", "creator", "analyst", "publisher"}:
            return None
        return {
            "agent_id": _id(getattr(agent, "id", None)),
            "assignment_id": _id(getattr(assignment, "id", None)),
            "agent_revision_id": _id(getattr(revision, "id", None)),
        }
    except Exception:
        return None


async def _persona_id_for_revision(session: Any, revision_id: str | None) -> str | None:
    """Resolve Persona identity from a pinned PersonaRevision when DTOs omit it."""

    if session is None or not revision_id:
        return None
    try:
        models = import_module("..memory.models.media_operations", package=__package__)
        revision_model = getattr(models, "PersonaRevision")
        from sqlalchemy import select

        result = await _maybe_await(
            session.execute(select(revision_model).where(revision_model.id == UUID(str(revision_id))).limit(1))
        )
        row = result.scalar_one_or_none() if callable(getattr(result, "scalar_one_or_none", None)) else result.scalar()
        return _id(getattr(row, "persona_id", None)) if row is not None else None
    except Exception:
        return None


async def _connection_id_for_account(session: Any, account_id: str | None) -> str | None:
    if session is None or not account_id:
        return None
    try:
        models = import_module("..memory.models.media_operations_setup", package=__package__)
        account_model = getattr(models, "PlatformAccount")
        from sqlalchemy import select

        result = await _maybe_await(
            session.execute(
                select(account_model)
                .where(account_model.id == UUID(str(account_id)))
                .limit(1)
            )
        )
        row = (
            result.scalar_one_or_none()
            if callable(getattr(result, "scalar_one_or_none", None))
            else result.scalar()
        )
        return _id(getattr(row, "connection_id", None)) if row is not None else None
    except Exception:
        return None


async def _persona_id_for_account(session: Any, account_id: str | None) -> str | None:
    if session is None or not account_id:
        return None
    try:
        models = import_module("..memory.models.media_operations_setup", package=__package__)
        account_model = getattr(models, "PlatformAccount")
        from sqlalchemy import select

        result = await _maybe_await(
            session.execute(select(account_model).where(account_model.id == UUID(str(account_id))).limit(1))
        )
        row = result.scalar_one_or_none() if callable(getattr(result, "scalar_one_or_none", None)) else result.scalar()
        return _id(getattr(row, "persona_id", None)) if row is not None else None
    except Exception:
        return None


async def _agent_revision_id(session: Any, agent_id: str | None) -> str | None:
    if session is None or not agent_id:
        return None
    try:
        models = import_module("..memory.models.agent_identity", package=__package__)
        revision_model = getattr(models, "AgentRevision")
        from sqlalchemy import select

        result = await _maybe_await(
            session.execute(
                select(revision_model)
                .where(revision_model.agent_id == UUID(str(agent_id)))
                .order_by(revision_model.version.desc())
                .limit(1)
            )
        )
        row = result.scalar_one_or_none() if callable(getattr(result, "scalar_one_or_none", None)) else result.scalar()
        return _id(getattr(row, "id", None)) if row is not None else None
    except Exception:
        return None


async def _space_id_for_project(session: Any, project_id: str | None) -> str | None:
    if session is None or not project_id:
        return None
    try:
        models = import_module("..memory.models.projects", package=__package__)
        project_model = getattr(models, "Project")
        from sqlalchemy import select

        result = await _maybe_await(
            session.execute(
                select(project_model)
                .where(project_model.id == UUID(str(project_id)))
                .limit(1)
            )
        )
        row = (
            result.scalar_one_or_none()
            if callable(getattr(result, "scalar_one_or_none", None))
            else result.scalar()
        )
        return _id(getattr(row, "space_id", None)) if row is not None else None
    except Exception:
        return None


async def _resolve_authority(
    resolver: Callable[..., Any] | Any | None,
    *,
    agent_id: str | None,
    revision_id: str | None = None,
    persona_id: str | None,
    project_id: str | None,
    space_id: str | None = None,
    capabilities: Sequence[str],
    session: Any,
) -> Mapping[str, Any] | None:
    if resolver is None:
        # Autonomous work must never infer project/space authority from an
        # assignment alone.  Manual MediaOps remains available when this
        # resolver is absent, but source registration fails closed.
        return {"allowed": False, "reason": "authority_unavailable"}
    resolver_method = resolver if callable(resolver) else _callable(resolver, "resolve", "check", "authorize")
    if resolver_method is None:
        return {"allowed": False, "reason": "authority_unavailable"}
    try:
        value = await _call_compatible(
            resolver_method,
            positional=(),
            kwargs={
                "agent_id": agent_id,
                "revision_id": revision_id,
                "persona_id": persona_id,
                "project_id": project_id,
                "space_id": space_id,
                "requested_capabilities": list(capabilities),
                "capabilities": list(capabilities),
                "required_capability": capabilities[0] if capabilities else None,
                "tool_capabilities": list(capabilities),
                "harness_capabilities": list(capabilities),
                "session": session,
            },
        )
    except Exception:
        return {"allowed": False, "reason": "authority_unavailable"}
    if value is None:
        return {"allowed": False, "reason": "authority_unavailable"}
    if isinstance(value, Mapping):
        return value
    if isinstance(value, bool):
        return {"allowed": value}
    allowed = _field(value, "allowed", _field(value, "is_allowed", False))
    return {"allowed": bool(allowed)}


class MediaWorkSource:
    """Base bridge shared by all MediaOps source projections."""

    source_type = "media"
    domain = "media"
    execution_adapter = "media"
    required_capabilities: tuple[str, ...] = ()

    def __init__(
        self,
        service: Any | None = None,
        *,
        authority_resolver: Callable[..., Any] | None = None,
        operator_resolver: Callable[..., Any] | None = None,
        feature_checker: Callable[[], Any] | bool | None = None,
        actor: Any | None = None,
    ) -> None:
        self.service = service
        self.authority_resolver = authority_resolver
        self.operator_resolver = operator_resolver
        self.feature_checker = feature_checker
        self.default_actor = actor

    @property
    def enabled(self) -> bool:
        return _feature_enabled(self.feature_checker)

    def is_enabled(self) -> bool:
        return self.enabled

    def _ensure_enabled(self) -> None:
        if not self.enabled:
            raise MediaWorkSourceDisabled(MediaWorkSourceDisabled.code)

    async def discover_candidates(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return await self.discover(*args, **kwargs)

    async def discover(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        del args, kwargs
        return []

    async def refresh_source_state(
        self,
        source: Any,
        *,
        session: Any | None = None,
        actor: Any | None = None,
    ) -> Mapping[str, Any]:
        """Refresh via a domain method when available; never mutate locally."""

        self._ensure_enabled()
        method = _callable(
            self.service,
            "refresh_source_state",
            "refresh_research_source",
            "refresh_generation_source",
        )
        if method is None:
            return {"refreshed": False, "reason": "domain_refresh_unavailable"}
        value = await _call_compatible(
            method,
            positional=(source,),
            kwargs={"session": session, "actor": actor},
        )
        return _safe_mapping(value if isinstance(value, Mapping) else {"value": value})

    async def refresh(
        self,
        session: Any,
        claim: Any,
    ) -> bool:
        """Coordinator hook: source state must still permit execution."""

        if not self.enabled:
            return False
        try:
            result = await self.refresh_source_state(
                claim,
                session=session,
                actor=_field(claim, "actor"),
            )
        except Exception:
            return False
        if isinstance(result, Mapping) and result.get("allowed") is False:
            return False
        if isinstance(result, Mapping) and result.get("refreshed") is False:
            return False
        return True

    async def materialize(
        self,
        candidates: Sequence[Mapping[str, Any]] | Mapping[str, Any],
        coordinator: Any | None = None,
        *,
        source: Any | None = None,
        session: Any | None = None,
    ) -> list[Any]:
        """Materialize through AgentWorkCoordinator's durable idempotency API."""

        self._ensure_enabled()
        rows = _rows(candidates)[:MAX_CANDIDATES]
        payloads = [self._normalize_candidate(row) for row in rows]
        payloads = [row for row in payloads if row is not None]
        if coordinator is None:
            # Returning projections is useful to heartbeat callers that pass
            # them to a coordinator later; no local queue is retained here.
            return payloads
        method = _callable(
            coordinator,
            "materialize_work_item",
            "ensure_work_item",
            "upsert_work_item",
            "create_work_item",
            "materialize",
        )
        if method is None:
            raise MediaWorkSourceUnavailable("AgentWorkCoordinator materialization API is unavailable")
        materialized: list[Any] = []
        for payload in payloads:
            value = await _call_compatible(
                method,
                positional=(payload,),
                kwargs={
                    "candidate": payload,
                    "work_item": payload,
                    "source": source or self,
                    "session": session,
                },
            )
            materialized.append(value)
        return materialized

    async def discover_and_materialize(
        self,
        session: Any | None = None,
        coordinator: Any | None = None,
        *,
        actor: Any | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        if not self.enabled:
            return []
        candidates = await self.discover(session=session, actor=actor, **kwargs)
        return await self.materialize(
            candidates,
            coordinator,
            source=self,
            session=session,
        )

    def _normalize_candidate(self, value: Mapping[str, Any]) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            to_dict = getattr(value, "to_dict", None)
            value = to_dict() if callable(to_dict) else None
        if not isinstance(value, Mapping):
            return None
        source_type = _text(value.get("source_type") or self.source_type, 128)
        source_id = _id(value.get("source_id"))
        source_revision = _id(value.get("source_revision"))
        intent_key = _text(value.get("intent_key"), 255)
        if not source_type or not source_id or not source_revision or not intent_key:
            return None
        candidate = dict(value)
        metadata = value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {}
        if isinstance(value.get("payload"), Mapping):
            metadata = {**metadata, "payload": dict(value["payload"])}
        if value.get("approval_required"):
            metadata = {**metadata, "approval_required": True}
        candidate.update(
            {
                "source_type": source_type,
                "source_id": source_id,
                "source_revision": source_revision,
                "intent_key": intent_key,
                "domain": self.domain,
                "project_id": _id(value.get("project_id")),
                "space_id": _id(value.get("space_id")),
                "persona_id": _id(value.get("persona_id")),
                "assigned_agent_id": _id(value.get("assigned_agent_id")),
                "required_capabilities": [
                    item
                    for item in (_text(raw, 96) for raw in list(value.get("required_capabilities") or self.required_capabilities)[:MAX_LIST])
                    if item
                ],
                "execution_adapter": _text(value.get("execution_adapter") or self.execution_adapter, 128),
                "priority": max(-100, min(100, _bounded_int(value.get("priority"), default=0))),
                "not_before": value.get("not_before") if isinstance(value.get("not_before"), datetime) else _iso(value.get("not_before")),
                "deadline": value.get("deadline") if isinstance(value.get("deadline"), datetime) else _iso(value.get("deadline")),
                "task_id": _id(value.get("task_id")),
                "app_id": _id(value.get("app_id")),
                "agent_revision_id": _id(value.get("agent_revision_id")),
                "max_attempts": max(1, min(_bounded_int(value.get("max_attempts"), default=3), 100)),
                "budget_reservation": _safe_mapping(value.get("budget_reservation") or {}, max_depth=2),
                "root_work_item_id": _id(value.get("root_work_item_id")),
                "parent_work_item_id": _id(value.get("parent_work_item_id")),
                "causation_id": _text(value.get("causation_id"), 255),
                "causal_depth": max(0, min(_bounded_int(value.get("causal_depth"), default=0), 16)),
                "mutation_fingerprint": _id(value.get("mutation_fingerprint")),
                "metadata": _safe_mapping(metadata, max_depth=3),
                "concurrency_key": _text(value.get("concurrency_key"), 255),
            }
        )
        # ``actor``/service instances and unbounded domain rows must never be
        # copied into a work item.  Keep only the canonical keys above.
        allowed = {
            "source_type",
            "source_id",
            "source_revision",
            "intent_key",
            "domain",
            "project_id",
            "space_id",
            "task_id",
            "persona_id",
            "app_id",
            "assigned_agent_id",
            "agent_revision_id",
            "required_capabilities",
            "execution_adapter",
            "priority",
            "not_before",
            "deadline",
            "max_attempts",
            "concurrency_key",
            "budget_reservation",
            "root_work_item_id",
            "parent_work_item_id",
            "causation_id",
            "causal_depth",
            "mutation_fingerprint",
            "metadata",
        }
        return {key: candidate[key] for key in allowed if key in candidate}


class MediaAutomationWorkSource(MediaWorkSource):
    """Project due AoiTalk Automation Programs into the common AgentWork runtime."""

    source_type = "media.automation.program"
    execution_adapter = "media.automation"
    required_capabilities = ("media", "web_read")

    def __init__(self, service: Any | None = None, **kwargs: Any) -> None:
        if service is None:
            try:
                service = import_module(
                    ".media_operations_automation_service", package=__package__
                ).MediaOperationsAutomationService()
            except Exception:
                service = None
        super().__init__(service, **kwargs)

    async def refresh(self, session: Any, claim: Any) -> bool:
        if not self.enabled:
            return False
        actor = self.default_actor or _field(claim, "actor")
        method = _callable(self.service, "get_program")
        if method is None or actor is None:
            return False
        try:
            detail = await _call_compatible(
                method,
                kwargs={"session": session, "actor": actor, "program_id": _field(claim, "source_id")},
            )
        except Exception:
            return False
        if _field(detail, "enabled", True) is False:
            return False
        revision = _field(detail, "current_revision") or {}
        return _id(_field(revision, "id")) == _id(_field(claim, "source_revision"))

    async def discover(self, session: Any | None = None, actor: Any | None = None, *, project_id: Any = None, as_of: Any = None, now: Any = None, limit: int = MAX_CANDIDATES) -> list[WorkCandidate]:
        if not self.enabled:
            return []
        method = _callable(self.service, "list_due_programs")
        actor = actor or self.default_actor
        if method is None or actor is None:
            return []
        try:
            due = await _call_compatible(
                method,
                kwargs={"session": session, "actor": actor, "project_id": project_id, "as_of": as_of if as_of is not None else now, "limit": min(max(int(limit), 1), MAX_CANDIDATES)},
            )
        except Exception:
            return []
        result: list[WorkCandidate] = []
        for program in _rows(due)[:MAX_CANDIDATES]:
            program_id = _id(_field(program, "id"))
            revision = _field(program, "current_revision") or {}
            revision_id = _id(_field(revision, "id"))
            trigger = _field(revision, "trigger") or {}
            trigger_key = _text(_field(program, "trigger_key"), 255)
            agent_id = _id(_field(trigger, "assigned_agent_id"))
            project_ref = _id(_field(program, "project_id"))
            if not program_id or not revision_id or not trigger_key or not agent_id:
                continue
            agent_revision_id = await _agent_revision_id(session, agent_id)
            if not agent_revision_id:
                continue
            space_ref = await _space_id_for_project(session, project_ref)
            authority = await _resolve_authority(
                self.authority_resolver,
                agent_id=agent_id,
                revision_id=agent_revision_id,
                persona_id=None,
                project_id=project_ref,
                space_id=space_ref,
                capabilities=self.required_capabilities,
                session=session,
            )
            if authority is not None and not bool(authority.get("allowed", authority.get("is_allowed", False))):
                continue
            result.append(WorkCandidate(
                source_type=self.source_type,
                source_id=program_id,
                source_revision=revision_id,
                intent_key="automation:" + _hash_key(program_id, revision_id, trigger_key)[:48],
                project_id=project_ref,
                space_id=space_ref,
                assigned_agent_id=agent_id,
                agent_revision_id=agent_revision_id,
                required_capabilities=self.required_capabilities,
                execution_adapter=self.execution_adapter,
                not_before=_iso(_field(program, "next_due_at")),
                concurrency_key=f"media.automation:{program_id}",
                metadata={"payload": {"trigger_key": trigger_key, "execution_mode": _text(_field(revision, "execution_mode"), 32)}},
            ))
        return result


class MediaResearchWorkSource(MediaWorkSource):
    """Project due ResearchRoutine revisions into durable work intents."""

    source_type = "media.research.routine"
    execution_adapter = "media.research"
    # ``media`` and ``web_read`` are the canonical Agent Team catalog ids.
    # Keep domain-specific operation names in metadata, not authority input.
    required_capabilities = ("media", "web_read")

    def __init__(self, service: Any | None = None, **kwargs: Any) -> None:
        if service is None:
            try:
                service = import_module(
                    ".media_operations_research_service", package=__package__
                ).MediaOperationsResearchService()
            except Exception:
                service = None
        super().__init__(service, **kwargs)

    async def refresh(self, session: Any, claim: Any) -> bool:
        if not self.enabled:
            return False
        actor = self.default_actor or _field(claim, "actor")
        method = _callable(self.service, "get_research_routine")
        if method is None or actor is None:
            return False
        try:
            detail = await _call_compatible(
                method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "routine_id": _field(claim, "source_id"),
                    "research_routine_id": _field(claim, "source_id"),
                },
            )
        except Exception:
            return False
        state = str(_field(detail, "state", "") or "").strip().lower()
        if state and state != "active":
            return False
        if _field(detail, "enabled", True) is False:
            return False
        revision = _field(detail, "current_revision") or _field(detail, "revision") or {}
        current_id = _id(_field(revision, "id") or _field(detail, "research_routine_revision_id"))
        return bool(current_id and current_id == _id(_field(claim, "source_revision")))

    async def discover(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: Any = None,
        as_of: Any = None,
        now: Any = None,
        limit: int = MAX_CANDIDATES,
    ) -> list[WorkCandidate]:
        if not self.enabled:
            return []
        method = _callable(self.service, "list_due_research_routines")
        actor = actor or self.default_actor
        if method is None or actor is None:
            return []
        try:
            value = await _call_compatible(
                method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "project_id": project_id,
                    "as_of": as_of if as_of is not None else now,
                    "limit": min(max(int(limit), 1), MAX_CANDIDATES),
                },
            )
        except Exception:
            return []
        result: list[WorkCandidate] = []
        for routine in _rows(value)[:MAX_CANDIDATES]:
            routine_id = _id(_field(routine, "id") or _field(routine, "research_routine_id"))
            revision = _field(routine, "current_revision") or _field(routine, "revision") or {}
            revision_id = _id(_field(revision, "id") or _field(routine, "research_routine_revision_id"))
            revision_version = _field(revision, "version") or _field(routine, "routine_version")
            if not routine_id or not revision_id or revision_version in (None, ""):
                continue
            persona_id = _id(_field(routine, "persona_id"))
            project_ref = _id(_field(routine, "project_id"))
            space_ref = _id(_field(routine, "space_id")) or await _space_id_for_project(
                session, project_ref
            )
            operator = await _resolve_optional_agent(
                session=session,
                persona_id=persona_id,
                project_id=project_ref,
                space_id=space_ref,
                resolver=self.operator_resolver,
                service=self.service,
            )
            if operator is None or not operator.get("agent_id"):
                continue
            if not operator.get("agent_revision_id"):
                operator["agent_revision_id"] = await _agent_revision_id(
                    session, operator.get("agent_id")
                )
            if not operator.get("agent_revision_id"):
                continue
            authority = await _resolve_authority(
                self.authority_resolver,
                agent_id=operator["agent_id"],
                revision_id=operator.get("agent_revision_id"),
                persona_id=persona_id,
                project_id=project_ref,
                space_id=space_ref,
                capabilities=self.required_capabilities,
                session=session,
            )
            if authority is not None and not bool(authority.get("allowed", authority.get("is_allowed", False))):
                continue
            due_marker = _field(routine, "next_due_at") or _field(routine, "due_at") or as_of or "due"
            intent_key = "research:" + _hash_key(routine_id, revision_id, revision_version, due_marker)[:48]
            candidate = WorkCandidate(
                source_type=self.source_type,
                source_id=routine_id,
                source_revision=revision_id,
                intent_key=intent_key,
                project_id=project_ref,
                space_id=space_ref,
                persona_id=persona_id,
                assigned_agent_id=operator["agent_id"],
                agent_revision_id=operator["agent_revision_id"],
                required_capabilities=self.required_capabilities,
                execution_adapter=self.execution_adapter,
                priority=int(_field(routine, "priority", 0) or 0),
                not_before=_iso(_field(routine, "next_due_at")),
                concurrency_key=f"media.research:{routine_id}",
                metadata={
                    "payload": {
                        "routine_version": int(revision_version),
                        "routine_revision_id": revision_id,
                        "routine_revision_hash": _id(_field(revision, "content_hash")),
                        "search_queries": _bounded_text_list(
                            _field(revision, "search_queries")
                            or _field(revision, "search_queries_json"),
                            limit=20,
                            item_limit=500,
                        ),
                        "source_types": _bounded_text_list(
                            _field(revision, "source_types")
                            or _field(revision, "source_types_json"),
                            limit=16,
                            item_limit=64,
                        ),
                        "domains": _bounded_text_list(
                            _field(revision, "domains")
                            or _field(revision, "domains_json"),
                            limit=20,
                            item_limit=255,
                        ),
                        "objective": _text(_field(revision, "objective"), 1_000),
                        "operator_assignment_id": operator.get("assignment_id"),
                        "review_policy": _text(_field(revision, "review_policy"), 64),
                    }
                },
            )
            result.append(candidate)
        return result


class MediaGenerationWorkSource(MediaWorkSource):
    """Project draft/uncertain GenerationPlans without bypassing intents."""

    source_type = "media.generation.plan"
    execution_adapter = "media.generation"
    required_capabilities = ("media",)

    def __init__(self, service: Any | None = None, **kwargs: Any) -> None:
        if service is None:
            try:
                service = import_module(
                    ".media_operations_generation_service", package=__package__
                ).MediaOperationsGenerationService()
            except Exception:
                service = None
        super().__init__(service, **kwargs)

    async def refresh(self, session: Any, claim: Any) -> bool:
        if not self.enabled:
            return False
        actor = self.default_actor or _field(claim, "actor")
        method = _callable(self.service, "get_generation_plan")
        if method is None or actor is None:
            return False
        try:
            detail = await _call_compatible(
                method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "plan_id": _field(claim, "source_id"),
                },
            )
        except Exception:
            return False
        status = str(_field(detail, "status", "") or "").strip().lower()
        if status not in {"draft", "submitted", "uncertain"}:
            # Completed/unavailable/failed plans are not eligible for a fresh
            # provider submit; uncertain intents are handled by reconciliation.
            return False
        plan_hash = _id(_field(detail, "plan_hash") or _field(detail, "content_hash"))
        return bool(plan_hash and plan_hash == _id(_field(claim, "source_revision")))

    async def discover(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: Any = None,
        now: Any = None,
        limit: int = MAX_CANDIDATES,
    ) -> list[WorkCandidate]:
        del now
        if not self.enabled:
            return []
        method = _callable(self.service, "list_generation_plans")
        actor = actor or self.default_actor
        if method is None or actor is None:
            return []
        try:
            value = await _call_compatible(
                method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "project_id": project_id,
                    "limit": min(max(int(limit), 1), MAX_CANDIDATES),
                    "offset": 0,
                },
            )
        except Exception:
            return []
        result: list[WorkCandidate] = []
        for plan in _rows(value)[:MAX_CANDIDATES]:
            plan_id = _id(_field(plan, "id") or _field(plan, "plan_id"))
            plan_hash = _id(_field(plan, "plan_hash") or _field(plan, "content_hash"))
            if not plan_id or not plan_hash:
                continue
            status = str(_field(plan, "status", "draft") or "draft").strip().lower()
            intent = _field(plan, "intent")
            intent_status = str(_field(intent, "status", "") or "").strip().lower()
            if status not in {"draft", "submitted", "unavailable"}:
                continue
            # A durable intent is authoritative.  Only uncertain intents are
            # eligible for explicit reconciliation; no generic retry submit.
            mode = "submit"
            if intent_status in {"submitted", "pending", "failed", "unavailable"} or status in {"submitted", "unavailable"}:
                if intent_status != "uncertain":
                    continue
                mode = "reconcile"
            persona_id = _id(_field(plan, "persona_id"))
            persona_revision_id = _id(_field(plan, "persona_revision_id"))
            if persona_id is None:
                persona_id = await _persona_id_for_revision(session, persona_revision_id)
            project_ref = _id(_field(plan, "project_id"))
            space_ref = _id(_field(plan, "space_id")) or await _space_id_for_project(
                session, project_ref
            )
            operator = await _resolve_optional_agent(
                session=session,
                persona_id=persona_id,
                project_id=project_ref,
                space_id=space_ref,
                resolver=self.operator_resolver,
                service=self.service,
            )
            if operator is None or not operator.get("agent_id"):
                continue
            if not operator.get("agent_revision_id"):
                operator["agent_revision_id"] = await _agent_revision_id(
                    session, operator.get("agent_id")
                )
            if not operator.get("agent_revision_id"):
                continue
            authority = await _resolve_authority(
                self.authority_resolver,
                agent_id=operator["agent_id"],
                revision_id=operator.get("agent_revision_id"),
                persona_id=persona_id,
                project_id=project_ref,
                space_id=space_ref,
                capabilities=self.required_capabilities,
                session=session,
            )
            if authority is not None and not bool(authority.get("allowed", authority.get("is_allowed", False))):
                continue
            intent_key = "generation:" + _hash_key(plan_id, plan_hash, mode)[:48]
            result.append(
                WorkCandidate(
                    source_type=self.source_type,
                    source_id=plan_id,
                    source_revision=plan_hash,
                    intent_key=intent_key,
                    project_id=project_ref,
                    space_id=space_ref,
                    persona_id=persona_id,
                    assigned_agent_id=operator["agent_id"],
                    agent_revision_id=operator["agent_revision_id"],
                    required_capabilities=self.required_capabilities,
                    execution_adapter=self.execution_adapter,
                    concurrency_key=f"media.generation:{plan_id}",
                    metadata={
                        "payload": {
                            "mode": mode,
                            "plan_hash": plan_hash,
                            "persona_revision_id": persona_revision_id,
                            "intent_id": _id(_field(intent, "id")),
                            "intent_request_hash": _id(_field(intent, "request_hash")),
                            "operator_assignment_id": operator.get("assignment_id"),
                        }
                    },
                )
            )
        return result


class MediaPublicationWorkSource(MediaWorkSource):
    """Find ready content for proposal-only publication work."""

    source_type = "media.publication"
    execution_adapter = "media.publication"
    required_capabilities = ("media",)

    def __init__(self, service: Any | None = None, **kwargs: Any) -> None:
        if service is None:
            try:
                service = import_module(
                    ".media_operations_content_service", package=__package__
                ).MediaOperationsContentService()
            except Exception:
                service = None
        super().__init__(service, **kwargs)

    async def refresh(self, session: Any, claim: Any) -> bool:
        if not self.enabled:
            return False
        actor = self.default_actor or _field(claim, "actor")
        method = _callable(self.service, "get_variant", "get_content_variant")
        if method is None or actor is None:
            return False
        try:
            detail = await _call_compatible(
                method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "variant_id": _field(claim, "source_id"),
                },
            )
        except Exception:
            return False
        detail_status = str(_field(detail, "status", "") or "").strip().lower()
        if detail_status and detail_status not in {"ready", "approved", "draft"}:
            return False
        readiness = _field(detail, "readiness") or {}
        if _field(readiness, "ready", _field(detail, "publication_allowed", False)) is not True:
            return False
        blockers = _field(readiness, "blocking_reasons", _field(readiness, "blockers", []))
        if blockers:
            return False
        revision = _field(detail, "current_revision") or {}
        current_hash = _id(_field(detail, "revision_hash") or _field(revision, "content_hash"))
        return bool(current_hash and current_hash == _id(_field(claim, "source_revision")))

    async def discover(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: Any = None,
        now: Any = None,
        limit: int = MAX_CANDIDATES,
    ) -> list[WorkCandidate]:
        del now
        if not self.enabled:
            return []
        method = _callable(self.service, "list_variants", "list_content_variants")
        actor = actor or self.default_actor
        if method is None or actor is None:
            return []
        try:
            value = await _call_compatible(
                method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "project_id": project_id,
                    "limit": min(max(int(limit), 1), MAX_CANDIDATES),
                    "offset": 0,
                },
            )
        except Exception:
            return []
        result: list[WorkCandidate] = []
        for variant in _rows(value)[:MAX_CANDIDATES]:
            current_revision = _field(variant, "current_revision") or _field(variant, "revision") or {}
            variant_id = _id(_field(variant, "id") or _field(variant, "variant_id"))
            revision_id = _id(
                _field(variant, "current_revision_id")
                or _field(variant, "revision_id")
                or _field(variant, "content_variant_revision_id")
                or _field(current_revision, "id")
            )
            revision_hash = _id(
                _field(variant, "current_revision_hash")
                or _field(variant, "revision_hash")
                or _field(variant, "content_variant_hash")
                or _field(current_revision, "content_hash")
                or _field(current_revision, "revision_hash")
            )
            persona_revision_id = _id(_field(variant, "persona_revision_id"))
            project_ref = _id(_field(variant, "project_id"))
            space_ref = _id(_field(variant, "space_id")) or await _space_id_for_project(
                session, project_ref
            )
            if not variant_id or not revision_id or not revision_hash or not persona_revision_id:
                continue
            status = str(_field(variant, "status", "draft") or "draft").strip().lower()
            if status not in {"ready", "approved", "draft"}:
                continue
            readiness = _field(variant, "readiness")
            readiness_status = str(
                _field(readiness, "status", _field(variant, "readiness_status", None))
                or ""
            ).strip().lower()
            ready_flag = _field(readiness, "ready", None)
            blockers = _field(readiness, "blockers", _field(readiness, "blocking_reasons", []))
            if ready_flag is False or blockers:
                continue
            if not readiness_status and ready_flag is True and not blockers:
                readiness_status = "ready"
            if readiness_status not in {"ready", "approved", "pass", "passed"}:
                continue
            persona_id = _id(_field(variant, "persona_id"))
            if persona_id is None:
                persona_id = await _persona_id_for_revision(session, persona_revision_id)
            operator = await _resolve_optional_agent(
                session=session,
                persona_id=persona_id,
                project_id=project_ref,
                space_id=space_ref,
                resolver=self.operator_resolver,
                service=self.service,
            )
            if operator is None or not operator.get("agent_id"):
                continue
            if not operator.get("agent_revision_id"):
                operator["agent_revision_id"] = await _agent_revision_id(
                    session, operator.get("agent_id")
                )
            if not operator.get("agent_revision_id"):
                continue
            authority = await _resolve_authority(
                self.authority_resolver,
                agent_id=operator["agent_id"],
                revision_id=operator.get("agent_revision_id"),
                persona_id=persona_id,
                project_id=project_ref,
                space_id=space_ref,
                capabilities=self.required_capabilities,
                session=session,
            )
            if authority is not None and not bool(authority.get("allowed", authority.get("is_allowed", False))):
                continue
            platform = _text(_field(variant, "platform"), 32)
            account_id = _id(_field(variant, "platform_account_id") or _field(current_revision, "platform_account_id"))
            connection_id = _id(_field(variant, "connection_id") or _field(current_revision, "connection_id"))
            if connection_id is None:
                connection_id = await _connection_id_for_account(session, account_id)
            if connection_id is None:
                continue
            intent_key = "publication:" + _hash_key(variant_id, revision_id, revision_hash, platform)[:48]
            result.append(
                WorkCandidate(
                    source_type=self.source_type,
                    source_id=variant_id,
                    source_revision=revision_hash,
                    intent_key=intent_key,
                    project_id=project_ref,
                    space_id=space_ref,
                    persona_id=persona_id,
                    assigned_agent_id=operator["agent_id"],
                    agent_revision_id=operator["agent_revision_id"],
                    required_capabilities=self.required_capabilities,
                    execution_adapter=self.execution_adapter,
                    concurrency_key=f"media.publication:{variant_id}",
                    metadata={
                        "approval_required": True,
                        "payload": {
                            "content_variant_revision_id": revision_id,
                            "content_variant_revision_hash": revision_hash,
                            "content_variant_revision_version": _bounded_int(
                                _field(current_revision, "version"), default=0
                            ) or None,
                            "content_variant_id": variant_id,
                            "content_variant_hash": _id(_field(variant, "content_hash") or _field(variant, "create_hash") or _field(current_revision, "content_variant_hash")),
                            "content_item_hash": _id(_field(variant, "content_item_hash") or _field(current_revision, "content_item_hash")),
                            "persona_revision_id": persona_revision_id,
                            "persona_revision_hash": _id(_field(variant, "persona_revision_hash") or _field(current_revision, "persona_revision_hash")),
                            "platform": platform,
                            "content_item_id": _id(_field(variant, "content_item_id")),
                            "platform_account_id": account_id,
                            "platform_account_revision_id": _id(_field(variant, "platform_account_revision_id")),
                            "platform_account_revision_hash": _id(
                                _field(variant, "platform_account_revision_hash")
                                or _field(current_revision, "platform_account_revision_hash")
                            ),
                            "connection_id": connection_id,
                            "publication_payload": _safe_mapping(
                                _field(current_revision, "payload")
                                or _field(current_revision, "payload_json"),
                                max_depth=2,
                            ),
                            "operator_assignment_id": operator.get("assignment_id"),
                        }
                    },
                )
            )
        return result


class MediaMetricsWorkSource(MediaWorkSource):
    """Project due metrics ingestion and pending learning proposals."""

    source_type = "media.metrics"
    execution_adapter = "media.metrics"
    required_capabilities = ("media",)

    def __init__(self, service: Any | None = None, **kwargs: Any) -> None:
        if service is None:
            try:
                service = import_module(
                    ".media_operations_metrics_service", package=__package__
                ).MediaOperationsMetricsService()
            except Exception:
                service = None
        super().__init__(service, **kwargs)

    async def refresh(self, session: Any, claim: Any) -> bool:
        """Recheck optional metric/proposal state before execution.

        Metric snapshots and LearningProposals are immutable/append-only in
        their respective ledgers.  Older deployments do not expose a
        point-read helper; in that case discovery already supplied the durable
        identity and there is no mutable provider state to refresh.
        """

        if not self.enabled:
            return False
        actor = self.default_actor or _field(claim, "actor")
        payload = _field(_field(claim, "metadata") or {}, "payload") or {}
        kind = str(_field(payload, "kind", "metrics") or "metrics").strip().lower()
        names = (
            ("get_metric_ingestion_run", "get_metric_snapshot")
            if kind == "metrics"
            else ("get_learning_proposal",)
        )
        method = _callable(self.service, *names)
        if method is None:
            return True
        if actor is None:
            return False
        try:
            result = await _call_compatible(
                method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "run_id": _field(claim, "source_id"),
                    "snapshot_id": _field(claim, "source_id"),
                    "proposal_id": _field(claim, "source_id"),
                },
            )
        except Exception:
            return False
        status = str(_field(result, "status", "pending") or "pending").strip().lower()
        if kind == "metrics":
            return status in {"pending", "queued", "retry_wait", "failed", "succeeded", "accepted"}
        return status in {"pending", "pending_review", "draft", "proposed"}

    async def discover(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: Any = None,
        limit: int = MAX_CANDIDATES,
        kind: str = "metrics",
        now: Any = None,
    ) -> list[WorkCandidate]:
        del now
        if not self.enabled:
            return []
        actor = actor or self.default_actor
        if actor is None or self.service is None:
            return []
        normalized_kind = str(kind or "metrics").strip().lower()
        names = (
            ("list_due_metric_ingestions", "list_due_metrics", "list_metric_ingestion_runs")
            if normalized_kind in {"metrics", "metric", "ingestion"}
            else ("list_pending_learning_proposals", "list_learning_proposals")
        )
        method = _callable(self.service, *names)
        if method is None:
            return []
        try:
            value = await _call_compatible(
                method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "project_id": project_id,
                    "limit": min(max(int(limit), 1), MAX_CANDIDATES),
                    "offset": 0,
                },
            )
        except Exception:
            return []
        result: list[WorkCandidate] = []
        for row in _rows(value)[:MAX_CANDIDATES]:
            row_id = _id(_field(row, "id") or _field(row, "run_id") or _field(row, "proposal_id"))
            revision = _id(
                _field(row, "snapshot_hash")
                or _field(row, "proposal_hash")
                or _field(row, "content_hash")
                or _field(row, "observation_hash")
                or _field(row, "request_hash")
                or _field(row, "run_hash")
                or _field(row, "version")
                or _field(row, "updated_at")
            )
            if not row_id or not revision:
                continue
            status = str(_field(row, "status", "pending") or "pending").strip().lower()
            if normalized_kind in {"metrics", "metric", "ingestion"} and status not in {"pending", "queued", "failed", "retry_wait"}:
                continue
            if normalized_kind not in {"metrics", "metric", "ingestion"} and status not in {"pending", "pending_review", "draft", "proposed"}:
                continue
            kind_source = "metrics" if normalized_kind in {"metrics", "metric", "ingestion"} else "learning"
            source_type = f"media.{kind_source}"
            project_ref = _id(_field(row, "project_id"))
            space_ref = _id(_field(row, "space_id")) or await _space_id_for_project(
                session, project_ref
            )
            persona_ref = _id(_field(row, "persona_id") or _field(row, "subject_ref"))
            if persona_ref is None:
                persona_ref = await _persona_id_for_account(
                    session,
                    _id(_field(row, "platform_account_id")),
                )
            operator = None
            assigned = _id(_field(row, "assigned_agent_id"))
            if assigned:
                operator = {
                    "agent_id": assigned,
                    "agent_revision_id": _id(_field(row, "agent_revision_id")),
                }
            elif persona_ref:
                operator = await _resolve_optional_agent(
                    session=session,
                    persona_id=persona_ref,
                    project_id=project_ref,
                    space_id=space_ref,
                    resolver=self.operator_resolver,
                    service=self.service,
                )
            if operator is None or not operator.get("agent_id"):
                continue
            if not operator.get("agent_revision_id"):
                operator["agent_revision_id"] = await _agent_revision_id(
                    session, operator.get("agent_id")
                )
            if not operator.get("agent_revision_id"):
                continue
            authority = await _resolve_authority(
                self.authority_resolver,
                agent_id=operator["agent_id"],
                revision_id=operator.get("agent_revision_id"),
                persona_id=persona_ref,
                project_id=project_ref,
                space_id=space_ref,
                capabilities=self.required_capabilities,
                session=session,
            )
            if authority is not None and not bool(authority.get("allowed", authority.get("is_allowed", False))):
                continue
            intent_key = f"{kind_source}:" + _hash_key(row_id, revision)[:48]
            result.append(
                WorkCandidate(
                    source_type=source_type,
                    source_id=row_id,
                    source_revision=revision,
                    intent_key=intent_key,
                    project_id=project_ref,
                    space_id=space_ref,
                    persona_id=persona_ref,
                    assigned_agent_id=operator["agent_id"],
                    agent_revision_id=operator["agent_revision_id"],
                    required_capabilities=self.required_capabilities,
                    execution_adapter="media.learning" if kind_source == "learning" else self.execution_adapter,
                    concurrency_key=f"media.{kind_source}:{row_id}",
                    metadata={
                        "payload": {
                            "kind": kind_source,
                            "proposal_id": row_id if kind_source == "learning" else None,
                            "status": status,
                            "snapshot_hash": _id(_field(row, "snapshot_hash")),
                            "proposal_hash": _id(_field(row, "proposal_hash")),
                            "subject_type": _text(_field(row, "subject_type"), 64),
                            "subject_ref": _id(_field(row, "subject_ref")),
                            "operator_assignment_id": operator.get("assignment_id"),
                        }
                    },
                )
            )
        return result


# Repository-facing aliases used by coordinator registration code and tests.
AutomationWorkSource = MediaAutomationWorkSource
ResearchWorkSource = MediaResearchWorkSource
GenerationWorkSource = MediaGenerationWorkSource
PublicationWorkSource = MediaPublicationWorkSource
class MediaLearningWorkSource(MediaMetricsWorkSource):
    """Review-only LearningProposal preparation source lane."""

    source_type = "media.learning"
    execution_adapter = "media.learning"

    async def discover(self, *args: Any, kind: str = "learning", **kwargs: Any) -> list[WorkCandidate]:
        return await super().discover(*args, kind=kind, **kwargs)


MetricsWorkSource = MediaMetricsWorkSource
LearningWorkSource = MediaLearningWorkSource
MediaAutomationSource = MediaAutomationWorkSource
MediaResearchSource = MediaResearchWorkSource
MediaGenerationSource = MediaGenerationWorkSource
MediaPublicationSource = MediaPublicationWorkSource
MediaMetricsSource = MediaMetricsWorkSource
MediaLearningSource = MediaLearningWorkSource
WorkSource = MediaWorkSource


def register_media_work_sources(
    coordinator: Any,
    *,
    research_service: Any | None = None,
    automation_service: Any | None = None,
    generation_service: Any | None = None,
    publication_service: Any | None = None,
    metrics_service: Any | None = None,
    learning_service: Any | None = None,
    authority_resolver: Callable[..., Any] | None = None,
    operator_resolver: Callable[..., Any] | None = None,
    actor: Any | None = None,
    feature_checker: Callable[[], Any] | bool | None = None,
) -> list[MediaWorkSource]:
    """Register MediaOps sources on a common coordinator when enabled.

    This helper is intentionally explicit: callers choose the DB-backed
    service instances and authority/actor context.  It does not start a
    heartbeat or worker loop and returns an empty list while the effective
    profile gate is disabled.
    """

    if not _feature_enabled(feature_checker):
        return []
    sources: list[MediaWorkSource] = [
        MediaAutomationWorkSource(
            automation_service,
            authority_resolver=authority_resolver,
            operator_resolver=operator_resolver,
            actor=actor,
            feature_checker=feature_checker,
        ),
        MediaResearchWorkSource(
            research_service,
            authority_resolver=authority_resolver,
            operator_resolver=operator_resolver,
            actor=actor,
            feature_checker=feature_checker,
        ),
        MediaGenerationWorkSource(
            generation_service,
            authority_resolver=authority_resolver,
            operator_resolver=operator_resolver,
            actor=actor,
            feature_checker=feature_checker,
        ),
        MediaPublicationWorkSource(
            publication_service,
            authority_resolver=authority_resolver,
            operator_resolver=operator_resolver,
            actor=actor,
            feature_checker=feature_checker,
        ),
        MediaMetricsWorkSource(
            metrics_service,
            authority_resolver=authority_resolver,
            operator_resolver=operator_resolver,
            actor=actor,
            feature_checker=feature_checker,
        ),
        MediaLearningWorkSource(
            learning_service or metrics_service,
            authority_resolver=authority_resolver,
            operator_resolver=operator_resolver,
            actor=actor,
            feature_checker=feature_checker,
        ),
    ]
    register = _callable(coordinator, "register_source")
    if register is None:
        raise MediaWorkSourceUnavailable("AgentWorkCoordinator source registration API is unavailable")
    for source in sources:
        register(source, source_type=source.source_type)
    return sources


register_media_sources = register_media_work_sources


__all__ = [
    "MAX_CANDIDATES",
    "MediaWorkSourceError",
    "MediaWorkSourceDisabled",
    "MediaWorkSourceUnavailable",
    "WorkCandidate",
    "MediaWorkSource",
    "MediaAutomationWorkSource",
    "MediaResearchWorkSource",
    "MediaGenerationWorkSource",
    "MediaPublicationWorkSource",
    "MediaMetricsWorkSource",
    "MediaLearningWorkSource",
    "MediaResearchSource",
    "MediaGenerationSource",
    "MediaPublicationSource",
    "MediaMetricsSource",
    "MediaLearningSource",
    "WorkSource",
    "register_media_work_sources",
    "register_media_sources",
    "ResearchWorkSource",
    "GenerationWorkSource",
    "PublicationWorkSource",
    "MetricsWorkSource",
    "LearningWorkSource",
    "_safe_evidence",
    "_safe_mapping",
]
