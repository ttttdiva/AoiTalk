"""ExecutionAdapter bridges from AgentWork to existing MediaOps services.

These adapters are deliberately thin: the common coordinator owns claims,
leases, retries, AgentRun linkage and settlement; MediaOps services own their
existing domain ledgers and provider fences.  The module stays importable while
WS02/WS03 are rolling out by using lazy imports and small duck-typed seams.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any
from uuid import UUID

from .media_work_sources import (
    MAX_EVIDENCE,
    MAX_TEXT,
    _call_compatible,
    _callable,
    _feature_enabled,
    _field,
    _id,
    _iso,
    _safe_evidence,
    _safe_mapping,
    _safe_provider_text,
    _safe_url,
    _text,
)

_OUTCOME_STATUSES = frozenset(
    {
        "succeeded",
        "failed",
        "blocked",
        "awaiting_approval",
        "uncertain",
        "transient",
        "permanent",
        "cancelled",
    }
)


class MediaExecutionAdapterError(RuntimeError):
    """Base error for adapter execution."""


class MediaExecutionDisabled(MediaExecutionAdapterError):
    """Autonomous MediaOps is disabled by the effective feature profile."""

    code = "media_operations_autonomy_disabled"


class MediaExecutionUnavailable(MediaExecutionAdapterError):
    """A required domain/provider boundary is unavailable."""


@dataclass(frozen=True, slots=True)
class MediaExecutionOutcome(Mapping[str, Any]):
    """Closed, bounded result suitable for AgentWork settlement."""

    status: str
    reason_code: str | None = None
    result_summary: str | None = None
    domain_ref: str | None = None
    domain_status: str | None = None
    evidence: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    retryable: bool = False
    awaiting_approval: bool = False

    def __post_init__(self) -> None:
        status = str(self.status or "failed").strip().lower()
        if status not in _OUTCOME_STATUSES:
            status = "failed"
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason_code", _text(self.reason_code, 128))
        object.__setattr__(self, "result_summary", _text(self.result_summary, MAX_TEXT))
        object.__setattr__(self, "domain_ref", _id(self.domain_ref))
        object.__setattr__(self, "domain_status", _text(self.domain_status, 64))
        projected_evidence: list[Mapping[str, Any]] = []
        for item in self.evidence[:MAX_EVIDENCE]:
            if not isinstance(item, Mapping):
                continue
            safe_item = _safe_mapping(item, max_depth=2)
            if not isinstance(safe_item, Mapping):
                continue
            if "url" in safe_item or "source_url" in safe_item:
                safe_item = dict(safe_item)
                url_key = "url" if "url" in safe_item else "source_url"
                safe_item[url_key] = _safe_url(safe_item.get(url_key))
                if safe_item[url_key] is None:
                    safe_item.pop(url_key, None)
            projected_evidence.append(safe_item)
        object.__setattr__(self, "evidence", tuple(projected_evidence))
        if status == "awaiting_approval":
            object.__setattr__(self, "awaiting_approval", True)

    @property
    def outcome(self) -> str:
        return self.status

    @property
    def is_success(self) -> bool:
        return self.status == "succeeded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "outcome": self.status,
            "classification": self.status,
            "reason_code": self.reason_code,
            "error_code": self.reason_code,
            "result_summary": self.result_summary,
            "domain_ref": self.domain_ref,
            "domain_status": self.domain_status,
            "evidence": [dict(item) for item in self.evidence],
            "retryable": bool(self.retryable),
            "awaiting_approval": bool(self.awaiting_approval),
            "metadata": {
                "domain_ref": self.domain_ref,
                "domain_status": self.domain_status,
            },
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())

    @classmethod
    def from_value(cls, value: Any, *, default_ref: Any = None) -> "MediaExecutionOutcome":
        if isinstance(value, cls):
            return value
        payload = value if isinstance(value, Mapping) else {}
        aliases = {
            "ok": "succeeded",
            "success": "succeeded",
            "completed": "succeeded",
            "error": "failed",
            "failure": "failed",
            "timeout": "transient",
            "pending": "awaiting_approval",
            "approval_required": "awaiting_approval",
            "unavailable": "blocked",
            "disabled": "blocked",
        }
        raw_status = str(payload.get("status") or payload.get("outcome") or "failed").strip().lower()
        status = aliases.get(raw_status, raw_status)
        if status not in _OUTCOME_STATUSES:
            status = "failed"
        raw_evidence = payload.get("evidence") or payload.get("evidence_refs") or []
        if isinstance(raw_evidence, Mapping):
            raw_evidence = [raw_evidence]
        if isinstance(raw_evidence, (str, bytes)) or not isinstance(raw_evidence, Sequence):
            raw_evidence = []
        return cls(
            status=status,
            reason_code=payload.get("reason_code") or payload.get("error_code"),
            result_summary=payload.get("result_summary") or payload.get("summary"),
            domain_ref=payload.get("domain_ref") or payload.get("id") or default_ref,
            domain_status=payload.get("domain_status") or payload.get("status"),
            evidence=tuple(item for item in raw_evidence if isinstance(item, Mapping)),
            retryable=bool(payload.get("retryable", status == "transient")),
            awaiting_approval=bool(payload.get("awaiting_approval", status == "awaiting_approval")),
        )


def _actor_is_human(actor: Any) -> bool:
    if actor is None:
        return False
    marker = _field(actor, "actor_type") or _field(actor, "kind") or _field(actor, "principal_kind")
    if bool(_field(actor, "is_agent", False)):
        return False
    if marker is None:
        return str(_field(actor, "role", "")).strip().lower() == "admin"
    return str(marker).strip().lower() in {"human", "user", "admin"}


def _safe_error_code(exc: BaseException, default: str) -> str:
    name = exc.__class__.__name__.lower()
    if "authorization" in name or "permission" in name:
        return "authority_denied"
    if "notfound" in name or "not_found" in name:
        return "domain_not_found"
    if "conflict" in name or "stale" in name:
        return "domain_conflict"
    if "validation" in name or "invalid" in name:
        return "domain_validation_failed"
    return default


def _work_payload(work_item: Any, context: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in (work_item, context):
        if isinstance(value, Mapping):
            result.update(value)
        elif value is not None:
            try:
                result.update(vars(value))
            except TypeError:
                for name in ("work_item_id", "source_type", "source_id", "source_revision", "intent_key", "domain", "space_id", "project_id", "task_id", "persona_id", "app_id", "assigned_agent_id", "agent_revision_id", "required_capabilities", "execution_adapter", "priority", "not_before", "deadline", "attempt", "concurrency_key", "budget_reservation", "causal_depth", "mutation_fingerprint", "metadata"):
                    try:
                        if hasattr(value, name):
                            result[name] = getattr(value, name)
                    except Exception:
                        continue
    nested = result.get("payload") or result.get("metadata")
    if isinstance(nested, Mapping):
        for key, value in nested.items():
            result.setdefault(str(key), value)
    # Lease tokens/owners are coordinator fencing credentials. They may be
    # present on the in-process WorkClaim object but must never reach a domain
    # service, adapter evidence, or persisted result payload.
    for secret_key in ("lease_token", "lease_owner", "lease_expires_at"):
        result.pop(secret_key, None)
    return result


def _run_id(run: Any) -> str | None:
    return _id(_field(run, "id") or _field(run, "run_id") or _field(run, "agent_run_id"))


async def _active_run_id(session: Any, work_item_id: Any) -> str | None:
    """Read the coordinator-linked AgentRun without creating another run."""

    if session is None or not work_item_id:
        return None
    try:
        from ..memory.models import AgentWorkItem

        row = await session.get(AgentWorkItem, UUID(str(work_item_id)))
        return _id(getattr(row, "active_agent_run_id", None)) if row is not None else None
    except Exception:
        return None


def _safe_result(result: Any, *, default_ref: Any = None) -> MediaExecutionOutcome:
    if isinstance(result, MediaExecutionOutcome):
        return result
    if result is None:
        return MediaExecutionOutcome(
            status="failed",
            reason_code="domain_result_missing",
            domain_ref=default_ref,
        )
    if isinstance(result, bool):
        return MediaExecutionOutcome(
            status="succeeded" if result else "failed",
            reason_code=None if result else "domain_operation_failed",
            domain_ref=default_ref,
        )
    if not isinstance(result, Mapping):
        to_dict = getattr(result, "to_safe_dict", None) or getattr(result, "to_dict", None)
        if callable(to_dict):
            try:
                result = to_dict()
            except Exception:
                result = None
    if not isinstance(result, Mapping):
        return MediaExecutionOutcome(
            status="failed",
            reason_code="domain_result_invalid",
            domain_ref=default_ref,
        )
    raw_status = str(result.get("status") or result.get("state") or "succeeded").strip().lower()
    if raw_status in {"unavailable", "blocked", "disabled"}:
        status = "blocked"
    elif raw_status in {"uncertain", "unknown"}:
        status = "uncertain"
    elif raw_status in {"failed", "error"}:
        status = "failed"
    elif raw_status in {"pending", "proposed", "awaiting_approval", "pending_review"}:
        status = "awaiting_approval"
    elif raw_status in {
        "transient",
        "permanent",
        "cancelled",
        "succeeded",
        "submitted",
        "accepted",
        "completed",
        "recorded",
    }:
        if raw_status in {"submitted", "accepted", "completed", "recorded"}:
            status = "succeeded"
        else:
            status = raw_status
    elif raw_status in {"rejected", "stale"}:
        status = "permanent"
    elif raw_status in {"running", "queued"}:
        status = "transient"
    else:
        status = "failed"
    evidence = result.get("evidence") or result.get("evidence_refs") or result.get("source_refs") or []
    if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
        evidence = []
    summary = result.get("result_summary") or result.get("summary")
    if summary is None and status == "succeeded":
        summary = "Media domain operation completed"
    return MediaExecutionOutcome(
        status=status,
        reason_code=result.get("reason_code") or result.get("error_code"),
        result_summary=summary,
        domain_ref=result.get("id") or result.get("run_id") or result.get("plan_id") or result.get("action_id") or default_ref,
        domain_status=result.get("status") or result.get("state"),
        evidence=tuple(item for item in evidence[:MAX_EVIDENCE] if isinstance(item, Mapping)),
        retryable=status == "transient",
        awaiting_approval=status == "awaiting_approval",
    )


class MediaExecutionAdapter:
    """Base common-runtime adapter contract."""

    adapter_name = "media"
    CAPABILITIES: tuple[str, ...] = ()
    # Protocol-compatible key consumed by AgentWorkCoordinator.register_adapter.
    adapter_key = "media"

    def __init__(
        self,
        service: Any | None = None,
        *,
        authority_resolver: Callable[..., Any] | None = None,
        feature_checker: Callable[[], Any] | bool | None = None,
        actor_resolver: Callable[..., Any] | None = None,
        domain_actor_resolver: Callable[..., Any] | None = None,
    ) -> None:
        self.service = service
        self.authority_resolver = authority_resolver
        self.feature_checker = feature_checker
        self.actor_resolver = actor_resolver or domain_actor_resolver

    @property
    def enabled(self) -> bool:
        return _feature_enabled(self.feature_checker)

    def is_enabled(self) -> bool:
        return self.enabled

    def required_capabilities(self, claim: Any = None) -> Sequence[str]:
        del claim
        return tuple(self.CAPABILITIES)

    def required_capability_set(self) -> frozenset[str]:
        return frozenset(self.CAPABILITIES)

    def _disabled(self) -> MediaExecutionOutcome:
        return MediaExecutionOutcome(
            status="blocked",
            reason_code=MediaExecutionDisabled.code,
        )

    async def _check_authority(self, *, item: Mapping[str, Any], session: Any, requested: Sequence[str] | None = None) -> MediaExecutionOutcome | None:
        agent_id = _id(item.get("assigned_agent_id") or item.get("agent_id"))
        if not agent_id:
            return MediaExecutionOutcome(status="blocked", reason_code="agent_identity_missing")
        if self.authority_resolver is None:
            return MediaExecutionOutcome(
                status="blocked",
                reason_code="authority_unavailable",
            )
        resolver = (
            self.authority_resolver
            if callable(self.authority_resolver)
            else _callable(self.authority_resolver, "resolve", "check", "authorize")
        )
        if resolver is None:
            return MediaExecutionOutcome(
                status="blocked",
                reason_code="authority_unavailable",
            )
        try:
            value = await _call_compatible(
                resolver,
                positional=(),
                kwargs={
                    "agent_id": agent_id,
                    "revision_id": _id(item.get("agent_revision_id") or item.get("agent_revision")),
                    "project_id": _id(item.get("project_id")),
                    "space_id": _id(item.get("space_id")),
                    "persona_id": _id(item.get("persona_id")),
                    "requested_capabilities": list(requested or self.CAPABILITIES),
                    "capabilities": list(requested or self.CAPABILITIES),
                    "required_capability": (requested or self.CAPABILITIES or [None])[0],
                    "tool_capabilities": list(requested or self.CAPABILITIES),
                    "harness_capabilities": list(requested or self.CAPABILITIES),
                    "session": session,
                },
            )
        except Exception:
            return MediaExecutionOutcome(status="blocked", reason_code="authority_unavailable")
        allowed = (
            value.get("allowed", value.get("is_allowed", False))
            if isinstance(value, Mapping)
            else value
            if isinstance(value, bool)
            else _field(value, "allowed", _field(value, "is_allowed", False))
        )
        if not allowed:
            reason = value.get("reason") if isinstance(value, Mapping) else _field(value, "reason")
            return MediaExecutionOutcome(status="blocked", reason_code=_text(reason, 128) or "authority_denied")
        return None

    async def _refresh_before_execute(self, item: Mapping[str, Any], *, session: Any, actor: Any) -> MediaExecutionOutcome | None:
        method = _callable(self.service, "refresh_source_state", "refresh_before_execution", "refresh")
        if method is None:
            return None
        try:
            await _call_compatible(method, positional=(item,), kwargs={"session": session, "actor": actor})
        except Exception as exc:
            return MediaExecutionOutcome(status="blocked", reason_code=_safe_error_code(exc, "source_refresh_failed"))
        return None

    async def _runtime_context(
        self,
        work_item: Any,
        context: Any,
        *,
        session: Any | None,
        actor: Any | None,
        coordinator: Any | None,
    ) -> tuple[Any | None, Any | None]:
        """Resolve coordinator-provided session/principal without impersonation."""

        item = _work_payload(work_item, context)
        coordinator_actor = None
        if coordinator is not None:
            coordinator_actor = getattr(coordinator, "execution_actor", None) or getattr(
                coordinator,
                "actor",
                None,
            )
        if actor is None:
            actor = item.get("actor") or item.get("execution_actor")
        if self.actor_resolver is not None:
            provided_actor = actor
            try:
                resolved_actor = await _call_compatible(
                    self.actor_resolver,
                    positional=(work_item,),
                    kwargs={"work_item": work_item, "context": context, "session": session},
                )
            except Exception:
                resolved_actor = None
            if resolved_actor is not None:
                actor = resolved_actor
            elif provided_actor is coordinator_actor:
                # A coordinator may carry one global admin actor for startup.
                # Do not silently use it for another Persona's owner-scoped
                # Media rows when the explicit resolver cannot resolve claim.
                actor = None
        if actor is None and self.actor_resolver is None:
            actor = coordinator_actor
        # Do not open a private DB session here: the coordinator owns its
        # transaction lifecycle.  WS03 may pass an already-scoped
        # ``execution_session``/``session`` when a domain adapter needs one;
        # otherwise we fail closed below rather than leaking a connection.
        if session is None and coordinator is not None:
            session = getattr(coordinator, "execution_session", None)
        return session, actor

    async def execute(self, *args: Any, **kwargs: Any) -> MediaExecutionOutcome:
        del args, kwargs
        return MediaExecutionOutcome(
            status="blocked",
            reason_code=MediaExecutionDisabled.code if not self.enabled else "adapter_not_implemented",
        )

    run = execute


class MediaAutomationExecutionAdapter(MediaExecutionAdapter):
    """Execute a due Automation Program through the canonical AutomationRun state machine."""

    adapter_name = "media.automation"
    CAPABILITIES = ("media", "web_read")
    adapter_key = "media.automation"

    def __init__(self, service: Any | None = None, **kwargs: Any) -> None:
        if service is None:
            try:
                service = import_module(
                    ".media_operations_automation_service", package=__package__
                ).MediaOperationsAutomationService()
            except Exception:
                service = None
        super().__init__(service, **kwargs)

    async def execute(
        self,
        work_item: Any = None,
        run: Any = None,
        *,
        session: Any | None = None,
        actor: Any | None = None,
        context: Any = None,
        run_id: Any = None,
        **kwargs: Any,
    ) -> MediaExecutionOutcome:
        coordinator = kwargs.pop("coordinator", None)
        session, actor = await self._runtime_context(
            work_item, context, session=session, actor=actor, coordinator=coordinator
        )
        del run, run_id
        if not self.enabled:
            return self._disabled()
        item = _work_payload(work_item, context)
        authority = await self._check_authority(item=item, session=session)
        if authority is not None:
            return authority
        if session is None or actor is None:
            return MediaExecutionOutcome(status="blocked", reason_code="domain_actor_missing")
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else item
        program_id = _id(item.get("source_id") or item.get("program_id"))
        trigger_key = _text(payload.get("trigger_key") or item.get("intent_key"), 255)
        if not program_id or not trigger_key:
            return MediaExecutionOutcome(status="blocked", reason_code="automation_program_pin_missing")
        method = _callable(self.service, "trigger_program")
        if method is None:
            return MediaExecutionOutcome(status="blocked", reason_code="automation_service_unavailable")
        try:
            result = await _call_compatible(
                method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "program_id": program_id,
                    "trigger_key": trigger_key,
                    "trigger_kind": "agent_work",
                    "execute": True,
                },
            )
        except Exception as exc:
            return MediaExecutionOutcome(
                status="blocked",
                reason_code=_safe_error_code(exc, "automation_execution_failed"),
                domain_ref=program_id,
            )
        state = str(_field(result, "state", "failed") or "failed").strip().lower()
        domain_ref = _id(_field(result, "id") or _field(result, "run_id")) or program_id
        if state == "generation_running" and domain_ref:
            reconcile = _callable(self.service, "reconcile_run")
            if reconcile is not None:
                try:
                    refreshed = await _call_compatible(
                        reconcile,
                        kwargs={"session": session, "actor": actor, "run_id": domain_ref},
                    )
                except Exception:
                    refreshed = None
                if refreshed is not None:
                    result = refreshed
                    state = str(_field(result, "state", state) or state).strip().lower()
                    domain_ref = _id(_field(result, "id") or _field(result, "run_id")) or domain_ref
        if state == "waiting_review":
            return MediaExecutionOutcome(
                status="awaiting_approval",
                reason_code="automation_waiting_review",
                result_summary="Automation prepared candidates for human review",
                domain_ref=domain_ref,
                domain_status=state,
                awaiting_approval=True,
            )
        if state == "uncertain":
            return MediaExecutionOutcome(
                status="uncertain",
                reason_code=_text(_field(result, "error_code"), 128) or "automation_outcome_uncertain",
                domain_ref=domain_ref,
                domain_status=state,
            )
        if state == "failed":
            return MediaExecutionOutcome(
                status="failed",
                reason_code=_text(_field(result, "error_code"), 128) or "automation_failed",
                domain_ref=domain_ref,
                domain_status=state,
            )
        if state == "complete":
            return MediaExecutionOutcome(
                status="succeeded",
                result_summary="Automation run durably advanced to its domain-owned state",
                domain_ref=domain_ref,
                domain_status=state,
            )
        return MediaExecutionOutcome(
            status="transient",
            reason_code="automation_run_in_progress",
            domain_ref=domain_ref,
            domain_status=state,
            retryable=True,
        )


class MediaResearchExecutionAdapter(MediaExecutionAdapter):
    """Execute a pinned research routine and append findings/candidates."""

    adapter_name = "media.research"
    CAPABILITIES = ("media", "web_read")
    adapter_key = "media.research"

    def __init__(self, service: Any | None = None, *, search_client: Any | None = None, config: Any | None = None, **kwargs: Any) -> None:
        if service is None:
            try:
                service = import_module(
                    ".media_operations_research_service", package=__package__
                ).MediaOperationsResearchService()
            except Exception:
                service = None
        super().__init__(service, **kwargs)
        self.search_client = search_client
        self.config = config

    def _search_request(self, item: Mapping[str, Any]) -> dict[str, Any]:
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else item
        revision = payload.get("routine_revision") if isinstance(payload.get("routine_revision"), Mapping) else payload
        queries = revision.get("search_queries") or revision.get("queries") or []
        if isinstance(queries, str):
            queries = [queries]
        queries = [_text(query, 500) for query in list(queries)[:20] if _text(query, 500)]
        if not queries and _text(revision.get("objective"), 1_000):
            queries = [_text(revision.get("objective"), 1_000)]
        known_engines = {"searxng", "google", "bing", "brave", "duckduckgo", "local"}
        engines = revision.get("engines") or revision.get("source_types") or ["searxng"]
        if isinstance(engines, str):
            engines = [engines]
        engines = [engine for engine in engines if str(engine).strip().casefold() in known_engines] or ["searxng"]
        return {
            "queries": queries[:20],
            "engines": [_text(engine, 64) for engine in list(engines)[:16] if _text(engine, 64)],
            "max_results_per_engine": max(1, min(int(revision.get("max_candidates") or 20), 100)),
            "project_id": _id(item.get("project_id")),
            "domains": [_text(domain, 255) for domain in list(revision.get("domains") or [])[:20] if _text(domain, 255)],
        }

    async def _invoke_search(self, request: Mapping[str, Any], actor: Any) -> list[Any]:
        if self.search_client is None:
            try:
                client_type = import_module(
                    ".deep_research_service", package=__package__
                ).DeepResearchSearchClient
                self.search_client = client_type(
                    self.config,
                    project_id=request.get("project_id"),
                )
            except Exception:
                return []
        method = _callable(self.search_client, "search", "run", "query")
        if method is None:
            return []
        results: list[Any] = []
        actor_kind = str(
            _field(actor, "actor_type")
            or _field(actor, "kind")
            or _field(actor, "principal_kind")
            or ""
        ).strip().lower()
        actor_user_id = None if actor_kind == "agent" else _id(
            _field(actor, "id") or _field(actor, "user_id")
        )
        for query in list(request.get("queries") or [])[:20]:
            value = await _call_compatible(
                method,
                positional=(query,),
                kwargs={
                    "engines": request.get("engines") or ["searxng"],
                    "max_results_per_engine": request.get("max_results_per_engine", 20),
                    "project_id": request.get("project_id"),
                    "actor_user_id": actor_user_id,
                    "user_id": actor_user_id,
                    "include_local_knowledge": False,
                    "domains": request.get("domains") or [],
                },
            )
            if isinstance(value, Mapping):
                values = value.get("sources") or value.get("results") or value.get("items") or []
            else:
                values = value
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                results.extend(list(values)[:MAX_EVIDENCE])
            elif values is not None:
                results.append(values)
        return results[:MAX_EVIDENCE]

    @staticmethod
    def _source_projection(source: Any) -> dict[str, Any] | None:
        title = _safe_provider_text(_field(source, "title"), 500)
        summary = _safe_provider_text(
            _field(source, "snippet")
            or _field(source, "summary")
            or _field(source, "description"),
            8_000,
        )
        evidence = _safe_evidence(
            [
                {
                    "url": _field(source, "url") or _field(source, "source_url"),
                    "title": title,
                    "snippet": summary,
                }
            ]
        )
        if not title and not summary:
            return None
        return {
            "title": title or "Research source",
            "summary": summary or title or "Research source",
            "source_url": evidence[0]["url"] if evidence else None,
            "source_published_at": _iso(
                _field(source, "published_at") or _field(source, "source_published_at")
            ),
            "evidence": evidence,
        }

    async def execute(
        self,
        work_item: Any = None,
        run: Any = None,
        *,
        session: Any | None = None,
        actor: Any | None = None,
        context: Any = None,
        run_id: Any = None,
        **kwargs: Any,
    ) -> MediaExecutionOutcome:
        coordinator = kwargs.pop("coordinator", None)
        session, actor = await self._runtime_context(
            work_item,
            context,
            session=session,
            actor=actor,
            coordinator=coordinator,
        )
        if not self.enabled:
            return self._disabled()
        item = _work_payload(work_item, context)
        authority = await self._check_authority(
            item=item, session=session, requested=self.CAPABILITIES
        )
        if authority is not None:
            return authority
        if session is None or actor is None:
            return MediaExecutionOutcome(status="blocked", reason_code="domain_actor_missing")
        refresh = await self._refresh_before_execute(item, session=session, actor=actor)
        if refresh is not None:
            return refresh
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else item
        routine_id = _id(item.get("source_id") or item.get("research_routine_id"))
        routine_version = payload.get("routine_version") or item.get("routine_version")
        if not routine_id or routine_version in (None, ""):
            return MediaExecutionOutcome(status="blocked", reason_code="research_routine_pin_missing")
        start_method = _callable(self.service, "start_research_run")
        if start_method is None:
            return MediaExecutionOutcome(status="blocked", reason_code="research_service_unavailable")
        # WorkItem intent_key is stable across AgentRun retries; using the
        # attempt UUID here would create duplicate ResearchRuns/searches after
        # lease recovery.  AgentRun linkage remains in the generic runtime.
        run_key = _text(item.get("intent_key"), 255) or "media-research"
        try:
            started = await _call_compatible(
                start_method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "research_routine_id": routine_id,
                    "routine_version": int(routine_version),
                    "focus_note": _text(payload.get("focus_note"), 4_000),
                    "source_refs": [],
                    "omissions": [],
                    # ResearchRun is an immutable occurrence; the existing
                    # service has no post-hoc status mutator.  Start it as a
                    # terminal recorded snapshot so a successful adapter
                    # cannot leave a stale ``running`` row after restart.
                    "status": "recorded",
                    "idempotency_key": run_key,
                },
            )
        except Exception as exc:
            return MediaExecutionOutcome(
                status="blocked",
                reason_code=_safe_error_code(exc, "research_run_start_failed"),
            )
        research_run_id = _id(_field(started, "id") or _field(started, "run_id"))
        if not research_run_id:
            return MediaExecutionOutcome(status="failed", reason_code="research_run_reference_missing")
        try:
            sources = await self._invoke_search(self._search_request(item), actor)
        except Exception:
            # The run intent is already durable; never ask the coordinator to
            # blind-retry an unknown external search result.
            return MediaExecutionOutcome(
                status="uncertain",
                reason_code="research_search_outcome_unknown",
                domain_ref=research_run_id,
            )
        if not sources:
            return MediaExecutionOutcome(
                status="failed",
                reason_code="research_provider_unavailable",
                domain_ref=research_run_id,
            )
        finding_method = _callable(self.service, "append_research_finding")
        candidate_method = _callable(self.service, "create_research_candidate")
        findings = candidates = 0
        failures = 0
        evidence_refs: list[Mapping[str, Any]] = []
        for index, source in enumerate(sources[:MAX_EVIDENCE]):
            projection = self._source_projection(source)
            if projection is None or not projection["evidence"]:
                continue
            evidence_refs.extend(projection["evidence"][:2])
            if finding_method is not None:
                try:
                    await _call_compatible(
                        finding_method,
                        kwargs={
                            "session": session,
                            "actor": actor,
                            "run_id": research_run_id,
                            "kind": "fact",
                            "statement": projection["summary"],
                            "evidence": projection["evidence"],
                            "idempotency_key": f"{run_key}:finding:{index}",
                        },
                    )
                    findings += 1
                except Exception:
                    failures += 1
            if candidate_method is not None:
                try:
                    await _call_compatible(
                        candidate_method,
                        kwargs={
                            "session": session,
                            "actor": actor,
                            "research_run_id": research_run_id,
                            "candidate_key": _hash_ref(projection.get("source_url") or projection["title"]),
                            "title": projection["title"],
                            "summary": projection["summary"],
                            "source_url": projection.get("source_url"),
                            "source_published_at": projection.get("source_published_at"),
                            "evidence": projection["evidence"],
                            "reason": "discovered by assigned MediaOps research routine",
                            "idempotency_key": f"{run_key}:candidate:{index}",
                        },
                    )
                    candidates += 1
                except Exception:
                    failures += 1
        if findings == 0 and candidates == 0:
            return MediaExecutionOutcome(
                status="failed",
                reason_code="research_evidence_persistence_failed",
                domain_ref=research_run_id,
            )
        if failures:
            return MediaExecutionOutcome(
                status="uncertain",
                reason_code="research_evidence_partial",
                domain_ref=research_run_id,
                result_summary=f"Recorded {findings} findings and {candidates} candidates; {failures} evidence writes need review",
                evidence=tuple(evidence_refs[:MAX_EVIDENCE]),
            )
        return MediaExecutionOutcome(
            status="succeeded",
            domain_ref=research_run_id,
            domain_status="recorded",
            result_summary=f"Recorded {findings} findings and {candidates} candidates",
            evidence=tuple(evidence_refs[:MAX_EVIDENCE]),
        )


def _hash_ref(value: Any) -> str:
    import hashlib

    return hashlib.sha256(str(value or "source").strip().lower().encode("utf-8")).hexdigest()[:48]


class MediaGenerationExecutionAdapter(MediaExecutionAdapter):
    """Submit/reconcile through GenerationRunIntent; never resubmit blindly."""

    adapter_name = "media.generation"
    CAPABILITIES = ("media",)
    adapter_key = "media.generation"

    def __init__(self, service: Any | None = None, **kwargs: Any) -> None:
        if service is None:
            try:
                service = import_module(
                    ".media_operations_generation_service", package=__package__
                ).MediaOperationsGenerationService()
            except Exception:
                service = None
        super().__init__(service, **kwargs)

    async def execute(
        self,
        work_item: Any = None,
        run: Any = None,
        *,
        session: Any | None = None,
        actor: Any | None = None,
        context: Any = None,
        run_id: Any = None,
        **kwargs: Any,
    ) -> MediaExecutionOutcome:
        coordinator = kwargs.pop("coordinator", None)
        session, actor = await self._runtime_context(
            work_item,
            context,
            session=session,
            actor=actor,
            coordinator=coordinator,
        )
        del run
        if not self.enabled:
            return self._disabled()
        item = _work_payload(work_item, context)
        authority = await self._check_authority(item=item, session=session)
        if authority is not None:
            return authority
        if session is None or actor is None:
            return MediaExecutionOutcome(status="blocked", reason_code="domain_actor_missing")
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else item
        plan_id = _id(item.get("source_id") or item.get("plan_id"))
        plan_hash = _id(payload.get("plan_hash") or item.get("source_revision"))
        if not plan_id or not plan_hash:
            return MediaExecutionOutcome(status="blocked", reason_code="generation_plan_pin_missing")
        mode = str(payload.get("mode") or "submit").strip().lower()
        acknowledge = payload.get("acknowledge_metered_generation", False)
        method = _callable(
            self.service,
            "reconcile_generation_plan" if mode == "reconcile" else "submit_generation_plan",
        )
        if method is None:
            return MediaExecutionOutcome(status="blocked", reason_code="generation_service_unavailable")
        call_kwargs: dict[str, Any] = {
            "session": session,
            "actor": actor,
            "plan_id": plan_id,
            "expected_plan_hash": plan_hash,
            "acknowledge_metered_generation": bool(acknowledge),
            "idempotency_key": _text(item.get("intent_key"), 255),
        }
        if mode == "reconcile":
            intent_hash = _id(payload.get("intent_request_hash"))
            if not intent_hash:
                return MediaExecutionOutcome(status="blocked", reason_code="generation_intent_pin_missing")
            call_kwargs["expected_intent_request_hash"] = intent_hash
        try:
            result = await _call_compatible(method, kwargs=call_kwargs)
        except Exception as exc:
            # submit_generation_plan commits GenerationRunIntent before the
            # provider call.  Any exception afterward is ambiguous, not a
            # generic retry opportunity.
            name = exc.__class__.__name__.casefold()
            if any(marker in name for marker in ("authorization", "validation", "notfound", "not_found")):
                return MediaExecutionOutcome(
                    status="blocked",
                    reason_code=_safe_error_code(exc, "generation_authority_denied"),
                    domain_ref=plan_id,
                )
            return MediaExecutionOutcome(
                status="uncertain" if mode != "reconcile" else "blocked",
                reason_code="generation_submit_outcome_unknown" if mode != "reconcile" else _safe_error_code(exc, "generation_reconcile_failed"),
                domain_ref=plan_id,
            )
        outcome = _safe_result(result, default_ref=plan_id)
        if outcome.domain_status == "unavailable":
            return MediaExecutionOutcome(
                status="blocked",
                reason_code=outcome.reason_code or "generation_studio_unavailable",
                domain_ref=outcome.domain_ref or plan_id,
                domain_status=outcome.domain_status,
                evidence=outcome.evidence,
            )
        if outcome.domain_status == "uncertain":
            return MediaExecutionOutcome(
                status="uncertain",
                reason_code=outcome.reason_code or "generation_outcome_uncertain",
                domain_ref=outcome.domain_ref or plan_id,
                domain_status=outcome.domain_status,
                evidence=outcome.evidence,
            )
        return outcome


class MediaPublicationExecutionAdapter(MediaExecutionAdapter):
    """Create proposal-only ExternalActions and require human approval."""

    adapter_name = "media.publication"
    CAPABILITIES = ("media",)
    adapter_key = "media.publication"

    def __init__(self, service: Any | None = None, **kwargs: Any) -> None:
        if service is None:
            try:
                service = import_module(
                    ".operations_service", package=__package__
                ).OperationsService()
            except Exception:
                service = None
        super().__init__(service, **kwargs)

    async def execute(
        self,
        work_item: Any = None,
        run: Any = None,
        *,
        session: Any | None = None,
        actor: Any | None = None,
        context: Any = None,
        run_id: Any = None,
        **kwargs: Any,
    ) -> MediaExecutionOutcome:
        coordinator = kwargs.pop("coordinator", None)
        session, actor = await self._runtime_context(
            work_item,
            context,
            session=session,
            actor=actor,
            coordinator=coordinator,
        )
        if not self.enabled:
            return self._disabled()
        item = _work_payload(work_item, context)
        authority = await self._check_authority(item=item, session=session)
        if authority is not None:
            return authority
        if session is None or actor is None:
            return MediaExecutionOutcome(status="blocked", reason_code="domain_actor_missing")
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else item
        action_id = _id(payload.get("action_id") or item.get("action_id"))
        if action_id:
            # Existing action approval/attempt/receipt semantics remain the
            # only path for execution.  The first pass records a proposal;
            # after a human approves it, resume the exact action through the
            # trusted manual-attempt boundary.  Never create a second action
            # or treat an approved row as an Agent approval.
            get_method = _callable(self.service, "get_action")
            if get_method is None:
                return MediaExecutionOutcome(
                    status="awaiting_approval",
                    reason_code="human_approval_required",
                    domain_ref=action_id,
                    awaiting_approval=True,
                )
            try:
                detail = await _call_compatible(
                    get_method,
                    kwargs={
                        "session": session,
                        "actor": actor,
                        "action_id": action_id,
                    },
                )
            except Exception as exc:
                return MediaExecutionOutcome(
                    status="blocked",
                    reason_code=_safe_error_code(exc, "publication_action_read_failed"),
                    domain_ref=action_id,
                )
            action_status = str(_field(detail, "status", "") or "").strip().lower()
            action_version = _field(detail, "version") or _field(detail, "action_version")
            if action_status == "approved":
                try:
                    expected_version = int(action_version) if action_version not in (None, "") else None
                except (TypeError, ValueError):
                    expected_version = None
                attempted = await self.execute_approved(
                    action_id,
                    session=session,
                    actor=actor,
                    expected_version=expected_version,
                )
                attempt_status = str(
                    _field(attempted, "status", "") or ""
                ).strip().lower()
                if attempt_status in {"succeeded", "failed", "uncertain"}:
                    return MediaExecutionOutcome(
                        status=attempt_status,
                        reason_code=_field(attempted, "error_code") or _field(attempted, "reason_code"),
                        result_summary=_field(attempted, "result_summary"),
                        domain_ref=action_id,
                        domain_status=attempt_status,
                    )
                # Manual create_attempt persists an intent and leaves the
                # remote result for the existing receipt/reconcile workflow.
                return MediaExecutionOutcome(
                    status="uncertain",
                    reason_code="external_action_receipt_pending",
                    result_summary="Approved action attempt recorded; receipt/reconciliation is required",
                    domain_ref=action_id,
                    domain_status=attempt_status or "attempting",
                )
            if action_status in {"attempting", "running"}:
                return MediaExecutionOutcome(
                    status="uncertain",
                    reason_code="external_action_attempt_in_progress",
                    domain_ref=action_id,
                    domain_status=action_status,
                )
            if action_status in {"succeeded", "failed", "uncertain"}:
                return MediaExecutionOutcome(
                    status=action_status,
                    domain_ref=action_id,
                    domain_status=action_status,
                )
            return MediaExecutionOutcome(
                status="awaiting_approval",
                reason_code="human_approval_required",
                domain_ref=action_id,
                domain_status=action_status or "proposed",
                awaiting_approval=True,
            )
        method = _callable(self.service, "create_media_action")
        if method is None:
            return MediaExecutionOutcome(status="blocked", reason_code="operations_service_unavailable")
        required = (
            "content_item_id",
            "content_variant_id",
            "content_variant_revision_id",
            "persona_revision_id",
            "connection_id",
            "platform",
        )
        if any(payload.get(name) in (None, "") for name in required):
            return MediaExecutionOutcome(status="blocked", reason_code="publication_binding_missing")
        platform = _text(payload.get("platform"), 32)
        linked_run_id = _id(run_id) or _run_id(run) or await _active_run_id(
            session,
            item.get("work_item_id") or item.get("id"),
        )
        try:
            result = await _call_compatible(
                method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "action_type": "media.publish_content",
                    "operation_key": "media.publish_content",
                    "content_item_id": _id(payload.get("content_item_id")),
                    "content_variant_id": _id(payload.get("content_variant_id") or item.get("source_id")),
                    "content_variant_revision_id": _id(payload.get("content_variant_revision_id")),
                    "persona_revision_id": _id(payload.get("persona_revision_id")),
                    "connection_id": _id(payload.get("connection_id")),
                    "idempotency_key": _text(item.get("intent_key"), 255) or "media-publication",
                    "platform": platform,
                    "payload": _safe_mapping(payload.get("publication_payload") or {}, max_depth=2),
                    "artifact_hashes": list(payload.get("artifact_hashes") or [])[:MAX_EVIDENCE],
                    "project_id": _id(item.get("project_id")),
                    "content_item_hash": _id(payload.get("content_item_hash")),
                    "content_variant_hash": _id(payload.get("content_variant_hash")),
                    "content_variant_revision_hash": _id(payload.get("content_variant_revision_hash") or item.get("source_revision")),
                    "content_variant_revision_version": payload.get("content_variant_revision_version"),
                    "persona_revision_hash": _id(payload.get("persona_revision_hash")),
                    "platform_account_id": _id(payload.get("platform_account_id")),
                    "platform_account_revision_id": _id(payload.get("platform_account_revision_id")),
                    "platform_account_revision_hash": _id(payload.get("platform_account_revision_hash")),
                    "execution_mode": "manual",
                    "adapter_target": {
                        "platform": platform,
                        "status": "manual",
                        "mode": "manual",
                        "provider_calls": False,
                    },
                    "origin_agent_id": _id(item.get("assigned_agent_id") or item.get("agent_id")),
                    "origin_agent_run_id": linked_run_id or _id(item.get("origin_agent_run_id")),
                    "origin_work_item_id": _id(
                        item.get("work_item_id")
                        or item.get("id")
                        or item.get("source_id")
                    ),
                },
            )
        except Exception as exc:
            return MediaExecutionOutcome(status="blocked", reason_code=_safe_error_code(exc, "publication_proposal_failed"))
        outcome = _safe_result(result)
        if outcome.status in {
            "failed",
            "blocked",
            "uncertain",
            "transient",
            "permanent",
            "cancelled",
        }:
            return outcome
        if not outcome.domain_ref:
            return MediaExecutionOutcome(
                status="blocked",
                reason_code="publication_action_reference_missing",
            )
        return MediaExecutionOutcome(
            status="awaiting_approval",
            reason_code="human_approval_required",
            result_summary="Publication proposal recorded; human approval is required",
            domain_ref=outcome.domain_ref,
            domain_status=outcome.domain_status or "proposed",
            evidence=outcome.evidence,
            awaiting_approval=True,
        )

    async def execute_approved(
        self,
        action_id: Any,
        *,
        session: Any | None = None,
        actor: Any | None = None,
        expected_version: int | None = None,
        **kwargs: Any,
    ) -> MediaExecutionOutcome:
        """Invoke OperationsService's human-gated manual attempt only."""

        del kwargs
        if not self.enabled:
            return self._disabled()
        if not _actor_is_human(actor):
            return MediaExecutionOutcome(status="blocked", reason_code="human_executor_required")
        method = _callable(self.service, "create_attempt", "start_attempt")
        if method is None or session is None:
            return MediaExecutionOutcome(status="blocked", reason_code="operations_service_unavailable")
        try:
            result = await _call_compatible(
                method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "action_id": _id(action_id),
                    "expected_version": expected_version,
                    "executor_type": "manual",
                    "execution_mode": "manual",
                },
            )
        except Exception as exc:
            return MediaExecutionOutcome(status="blocked", reason_code=_safe_error_code(exc, "publication_attempt_failed"))
        return _safe_result(result, default_ref=action_id)


class MediaMetricsExecutionAdapter(MediaExecutionAdapter):
    """Ingest server-owned metrics or prepare human-reviewed learning proposals."""

    adapter_name = "media.metrics"
    CAPABILITIES = ("media",)
    adapter_key = "media.metrics"

    def __init__(self, service: Any | None = None, *, provider_adapter: Any | None = None, **kwargs: Any) -> None:
        if service is None:
            try:
                service = import_module(
                    ".media_operations_metrics_service", package=__package__
                ).MediaOperationsMetricsService()
            except Exception:
                service = None
        super().__init__(service, **kwargs)
        self.provider_adapter = provider_adapter

    async def execute(
        self,
        work_item: Any = None,
        run: Any = None,
        *,
        session: Any | None = None,
        actor: Any | None = None,
        context: Any = None,
        run_id: Any = None,
        **kwargs: Any,
    ) -> MediaExecutionOutcome:
        coordinator = kwargs.pop("coordinator", None)
        session, actor = await self._runtime_context(
            work_item,
            context,
            session=session,
            actor=actor,
            coordinator=coordinator,
        )
        del run
        if not self.enabled:
            return self._disabled()
        item = _work_payload(work_item, context)
        payload = item.get("payload") or item.get("metadata") or item
        if not isinstance(payload, Mapping):
            payload = item
        kind = str(payload.get("kind") or "metrics").strip().lower()
        authority = await self._check_authority(
            item=item,
            session=session,
            requested=self.CAPABILITIES,
        )
        if authority is not None:
            return authority
        if session is None or actor is None:
            return MediaExecutionOutcome(status="blocked", reason_code="domain_actor_missing")
        if self.service is None:
            return MediaExecutionOutcome(status="blocked", reason_code="metrics_service_unavailable")
        if kind == "learning":
            return await self._prepare_learning(item, payload, session=session, actor=actor)
        return await self._ingest_metrics(item, payload, session=session, actor=actor)

    async def _prepare_learning(
        self,
        item: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        session: Any,
        actor: Any,
    ) -> MediaExecutionOutcome:
        # ``media.learning`` WorkSources project existing LearningProposal
        # rows for review/settlement.  Never create a second proposal from the
        # same row; the human review service remains the sole mutation path.
        existing_proposal_id = _id(
            payload.get("proposal_id") or item.get("source_id")
            if str(item.get("source_type") or "") == "media.learning"
            else payload.get("proposal_id")
        )
        if existing_proposal_id and str(item.get("source_type") or "") == "media.learning":
            get_method = _callable(self.service, "get_learning_proposal")
            if get_method is None:
                return MediaExecutionOutcome(
                    status="awaiting_approval",
                    reason_code="human_review_required",
                    domain_ref=existing_proposal_id,
                    awaiting_approval=True,
                )
            try:
                current = await _call_compatible(
                    get_method,
                    kwargs={
                        "session": session,
                        "actor": actor,
                        "proposal_id": existing_proposal_id,
                    },
                )
            except Exception as exc:
                return MediaExecutionOutcome(
                    status="blocked",
                    reason_code=_safe_error_code(exc, "learning_proposal_read_failed"),
                    domain_ref=existing_proposal_id,
                )
            proposal_status = str(_field(current, "status", "pending_review") or "pending_review").strip().lower()
            if proposal_status in {"pending_review", "pending", "draft", "proposed"}:
                return MediaExecutionOutcome(
                    status="awaiting_approval",
                    reason_code="human_review_required",
                    domain_ref=existing_proposal_id,
                    domain_status=proposal_status,
                    awaiting_approval=True,
                )
            if proposal_status in {"rejected", "stale", "cancelled"}:
                return MediaExecutionOutcome(
                    status="permanent",
                    reason_code=f"learning_proposal_{proposal_status}",
                    domain_ref=existing_proposal_id,
                    domain_status=proposal_status,
                )
            if proposal_status != "accepted":
                return MediaExecutionOutcome(
                    status="blocked",
                    reason_code="learning_proposal_status_unknown",
                    domain_ref=existing_proposal_id,
                    domain_status=proposal_status,
                )
            return _safe_result(current, default_ref=existing_proposal_id)
        method = _callable(self.service, "create_learning_proposal")
        if method is None:
            return MediaExecutionOutcome(status="blocked", reason_code="learning_service_unavailable")
        subject_ref = _id(payload.get("subject_ref") or item.get("persona_id"))
        if not subject_ref:
            return MediaExecutionOutcome(status="blocked", reason_code="learning_subject_missing")
        evidence = _safe_evidence(payload.get("evidence") or payload.get("evidence_refs"))
        try:
            result = await _call_compatible(
                method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "subject_type": _text(payload.get("subject_type") or "persona", 64),
                    "subject_ref": subject_ref,
                    "title": _text(payload.get("title") or "MediaOps learning proposal", 500),
                    "summary": _text(payload.get("summary") or payload.get("recommendation") or "", 8_000),
                    "recommendation": _text(payload.get("recommendation") or payload.get("summary") or "", 8_000),
                    "evidence_refs": evidence,
                    "evidence": evidence,
                    "human_decision_refs": [],
                    "decision_refs": [],
                    "target_fields": list(payload.get("target_fields") or [])[:MAX_EVIDENCE],
                    "proposed_before": _safe_mapping(payload.get("proposed_before") or {}, max_depth=2),
                    "proposed_after": _safe_mapping(payload.get("proposed_after") or {}, max_depth=2),
                    "window_start": _iso(payload.get("window_start")),
                    "window_end": _iso(payload.get("window_end")),
                    "confidence": payload.get("confidence", 0),
                    "uncertainty": _text(payload.get("uncertainty"), 4_000),
                    "project_id": _id(item.get("project_id")),
                    "idempotency_key": _text(item.get("intent_key"), 255) or "media-learning",
                },
            )
        except Exception as exc:
            return MediaExecutionOutcome(status="blocked", reason_code=_safe_error_code(exc, "learning_proposal_failed"))
        outcome = _safe_result(result)
        if outcome.status in {"failed", "blocked", "uncertain", "transient", "permanent", "cancelled"}:
            return outcome
        return MediaExecutionOutcome(
            status="succeeded",
            result_summary="Learning proposal prepared for human review",
            domain_ref=outcome.domain_ref,
            domain_status=outcome.domain_status or "pending",
            evidence=tuple(evidence[:MAX_EVIDENCE]),
        )

    async def _ingest_metrics(
        self,
        item: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        session: Any,
        actor: Any,
    ) -> MediaExecutionOutcome:
        method = _callable(
            self.service,
            "ingest_provider_metric_snapshot",
            "ingest_metric_snapshot",
        )
        if method is None:
            return MediaExecutionOutcome(status="blocked", reason_code="metrics_ingestion_unavailable")
        adapter = payload.get("adapter") or payload.get("provider_adapter") or self.provider_adapter
        if adapter is None:
            # Manual/imported observations remain available through the
            # existing human APIs.  Autonomous ingestion requires the
            # server-owned adapter identity and cannot self-authorize.
            return MediaExecutionOutcome(status="blocked", reason_code="server_owned_metrics_adapter_required")
        observation = payload.get("observation") or payload.get("metrics")
        if not isinstance(observation, Mapping):
            return MediaExecutionOutcome(status="blocked", reason_code="metrics_observation_missing")
        try:
            result = await _call_compatible(
                method,
                kwargs={
                    "session": session,
                    "actor": actor,
                    "adapter": adapter,
                    "observation": _safe_mapping(observation, max_depth=3),
                    "project_id": _id(item.get("project_id")),
                    "persona_ref": _id(item.get("persona_id") or payload.get("persona_ref")),
                    "platform_account_ref": _id(payload.get("platform_account_ref")),
                    "content_variant_ref": _id(payload.get("content_variant_ref")),
                    "publication_ref": _id(payload.get("publication_ref")),
                    "idempotency_key": _text(item.get("intent_key"), 255) or "media-metrics",
                },
            )
        except Exception as exc:
            return MediaExecutionOutcome(status="blocked", reason_code=_safe_error_code(exc, "metrics_ingestion_failed"))
        return _safe_result(result)


class MediaLearningExecutionAdapter(MediaMetricsExecutionAdapter):
    """Review-only LearningProposal preparation lane."""

    adapter_name = "media.learning"
    adapter_key = "media.learning"


ResearchExecutionAdapter = MediaResearchExecutionAdapter
GenerationExecutionAdapter = MediaGenerationExecutionAdapter
PublicationExecutionAdapter = MediaPublicationExecutionAdapter
MetricsExecutionAdapter = MediaMetricsExecutionAdapter
LearningExecutionAdapter = MediaLearningExecutionAdapter
MediaAutomationAdapter = MediaAutomationExecutionAdapter
MediaResearchAdapter = MediaResearchExecutionAdapter
MediaGenerationAdapter = MediaGenerationExecutionAdapter
MediaPublicationAdapter = MediaPublicationExecutionAdapter
MediaMetricsAdapter = MediaMetricsExecutionAdapter
ExecutionAdapter = MediaExecutionAdapter


def register_media_execution_adapters(
    coordinator: Any,
    *,
    research_service: Any | None = None,
    automation_service: Any | None = None,
    generation_service: Any | None = None,
    operations_service: Any | None = None,
    metrics_service: Any | None = None,
    learning_service: Any | None = None,
    provider_adapter: Any | None = None,
    search_client: Any | None = None,
    authority_resolver: Callable[..., Any] | None = None,
    actor_resolver: Callable[..., Any] | None = None,
    config: Any | None = None,
    feature_checker: Callable[[], Any] | bool | None = None,
) -> list[MediaExecutionAdapter]:
    """Register MediaOps adapters on an already-created coordinator."""

    if not _feature_enabled(feature_checker):
        return []
    adapters: list[MediaExecutionAdapter] = [
        MediaAutomationExecutionAdapter(
            automation_service,
            authority_resolver=authority_resolver,
            actor_resolver=actor_resolver,
            feature_checker=feature_checker,
        ),
        MediaResearchExecutionAdapter(
            research_service,
            search_client=search_client,
            authority_resolver=authority_resolver,
            actor_resolver=actor_resolver,
            feature_checker=feature_checker,
            config=config,
        ),
        MediaGenerationExecutionAdapter(
            generation_service,
            authority_resolver=authority_resolver,
            actor_resolver=actor_resolver,
            feature_checker=feature_checker,
        ),
        MediaPublicationExecutionAdapter(
            operations_service,
            authority_resolver=authority_resolver,
            actor_resolver=actor_resolver,
            feature_checker=feature_checker,
        ),
        MediaMetricsExecutionAdapter(
            metrics_service,
            provider_adapter=provider_adapter,
            authority_resolver=authority_resolver,
            actor_resolver=actor_resolver,
            feature_checker=feature_checker,
        ),
        MediaLearningExecutionAdapter(
            learning_service or metrics_service,
            authority_resolver=authority_resolver,
            actor_resolver=actor_resolver,
            feature_checker=feature_checker,
        ),
    ]
    register = _callable(coordinator, "register_adapter")
    if register is None:
        raise MediaExecutionUnavailable("AgentWorkCoordinator adapter registration API is unavailable")
    for adapter in adapters:
        register(adapter, adapter_key=adapter.adapter_key)
    return adapters


register_media_adapters = register_media_execution_adapters


__all__ = [
    "MediaExecutionAdapterError",
    "MediaExecutionDisabled",
    "MediaExecutionUnavailable",
    "MediaExecutionOutcome",
    "MediaExecutionAdapter",
    "MediaAutomationExecutionAdapter",
    "MediaResearchExecutionAdapter",
    "MediaGenerationExecutionAdapter",
    "MediaPublicationExecutionAdapter",
    "MediaMetricsExecutionAdapter",
    "MediaLearningExecutionAdapter",
    "ResearchExecutionAdapter",
    "GenerationExecutionAdapter",
    "PublicationExecutionAdapter",
    "MetricsExecutionAdapter",
    "LearningExecutionAdapter",
    "MediaResearchAdapter",
    "MediaGenerationAdapter",
    "MediaPublicationAdapter",
    "MediaMetricsAdapter",
    "ExecutionAdapter",
    "register_media_execution_adapters",
    "register_media_adapters",
]
