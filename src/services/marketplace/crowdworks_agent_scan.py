"""Dedicated WorkSource for the CrowdWorks Agent scan cadence.

This module discovers and refreshes durable AgentWork candidates.  The
dedicated refresh path also prepares the owner/session/exact AgentRevision
binding that the Coordinator projects into its canonical AgentRun creation.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import wraps
from types import SimpleNamespace
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import select

from ...memory.models import (
    Agent,
    AgentRevision,
    AgentRun,
    AgentWorkItem,
    ConversationSession,
)
from ...memory.models.agent_automation import (
    AgentActionPolicy,
    AgentActionPolicyRevision,
)
from ...memory.models.operations import ExternalConnection
from ..agent_action_policy_service import window
from ..agent_work_runtime import ExecutionOutcome, WorkCandidate
from ..agent_dialogue_workspace import (
    bind_agent_dialogue_workspace,
    reset_agent_dialogue_workspace,
)
from ..chat_execution_adapter import (
    bind_chat_agent_execution,
    build_agent_chat_turn_client,
    resolve_chat_agent_execution,
)
from ..conversation_agent_service import ConversationAgentService
from ..turn_context import reset_turn_context, set_turn_context
from .crowdworks_action import CrowdWorksPolicy


logger = logging.getLogger(__name__)

_SCAN_TOOLS = frozenset({"crowdworks_candidates", "crowdworks_list_jobs"})


_ACTION_TYPE = "engagement.submit_application"
_ACTIVE_CONNECTION_AUTH_STATUSES = frozenset({"browser_session", "verified"})


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: Any, *, limit: int = 256) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _same_uuid(left: Any, right: Any) -> bool:
    left_uuid = _uuid(left)
    right_uuid = _uuid(right)
    return left_uuid is not None and right_uuid is not None and left_uuid == right_uuid


def _safe_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_result(value: Any, *, limit: int = 32) -> dict[str, Any]:
    """Keep provider/tool result evidence bounded and structured."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:limit]:
            name = _text(key, limit=96)
            if not name:
                continue
            if isinstance(item, Mapping):
                result[name] = _safe_result(item, limit=limit)
            elif isinstance(item, (list, tuple)):
                result[name] = [
                    _safe_result(entry, limit=limit)
                    if isinstance(entry, Mapping)
                    else _text(entry, limit=512)
                    if isinstance(entry, str)
                    else entry
                    for entry in list(item)[:limit]
                ]
            elif isinstance(item, (str, int, float, bool)) or item is None:
                result[name] = item if not isinstance(item, str) else item[:2000]
        return result
    if isinstance(value, str):
        return {"text": value[:2000]}
    return {"value": value} if value is not None else {}


def _result_success(value: Any) -> bool:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                decoded = json.loads(stripped)
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None
            if decoded is not None and decoded is not value:
                return _result_success(decoded)
        return False
    if isinstance(value, Mapping):
        if value.get("success") is False:
            return False
        if value.get("accepted") is False:
            return False
        if value.get("error_code") or value.get("reason_code"):
            return False
        if value.get("success") is True or value.get("accepted") is True:
            return True
        if _text(value.get("status") or value.get("state")).casefold() in {
            "ok",
            "success",
            "succeeded",
            "completed",
            "scanned",
            "proposed",
        }:
            return True
        # Read tools have meaningful structured result keys even though they
        # do not return a generic ``success`` boolean.
        return any(
            key in value
            for key in ("jobs", "fetched", "created", "profile", "opportunity_id")
        )
    return False


def _tool_name(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    nested_call = getattr(value, "call", None)
    if nested_call is not None:
        nested_name = _tool_name(nested_call)
        if nested_name:
            return nested_name
    return _text(
        getattr(value, "name", None)
        or getattr(value, "tool", None)
        or getattr(value, "tool_name", None)
        or (value.get("name") if isinstance(value, Mapping) else None)
        or (value.get("tool") if isinstance(value, Mapping) else None),
        limit=128,
    )


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("CrowdWorks source clock must return a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _uuid(value: Any) -> UUID | None:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _source_revision(
    *,
    policy_id: Any,
    policy_revision_id: Any,
    policy_content_hash: Any,
    agent_revision_id: Any,
    bucket: int,
) -> str:
    binding = ":".join(
        (
            str(policy_id),
            str(policy_revision_id),
            str(policy_content_hash),
            str(agent_revision_id),
            str(bucket),
        )
    )
    return "agent-scan:" + hashlib.sha256(binding.encode("utf-8")).hexdigest()


class _ResolverSessionProxy:
    """Keep the shared resolver on a SQLAlchemy-safe UUID identity boundary.

    ``resolve_chat_agent_execution`` normalizes run identities to strings for
    comparisons, while SQLAlchemy UUID(as_uuid=True) primary-key lookups need
    ``UUID`` values.  Delegate every operation to the Coordinator session and
    coerce only the Agent primary-key lookup used by that helper.  No session
    or transaction is replaced.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def get(self, model: Any, identity: Any, **kwargs: Any) -> Any:
        if model is Agent:
            identity = _uuid(identity) or identity
        value = self._session.get(model, identity, **kwargs)
        return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class _CurrentAgentScanBinding:
    policy: Any
    revision: Any
    connection: Any
    agent: Any
    agent_revision: Any


class CrowdWorksAgentScanWorkSource:
    """Discover one owner-bound scan Work per connection and UTC hour."""

    source_type = "marketplace_crowdworks_agent_scan"
    intent_key = "marketplace.crowdworks.agent_scan"
    execution_adapter = "crowdworks_agent_scan"
    bucket_seconds = 60 * 60

    def __init__(
        self,
        *,
        policy_service: Any,
        enabled: bool = False,
        agent_scan_policy_id: Any = None,
        clock: Callable[[], datetime] | None = None,
        conversation_agent_service: Any | None = None,
    ) -> None:
        self.policies = policy_service
        self.agent_scan_policy_id = _uuid(agent_scan_policy_id)
        self.enabled = enabled is True and self.agent_scan_policy_id is not None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conversation_agent_service = conversation_agent_service

    def _now(self, value: datetime | None = None) -> datetime:
        return _as_utc(value if value is not None else self._clock())

    @staticmethod
    def _connection_is_active(connection: ExternalConnection | None) -> bool:
        if connection is None:
            return False
        return (
            str(getattr(connection, "provider_key", "")).casefold() == "crowdworks"
            and str(getattr(connection, "auth_status", "")).casefold()
            in _ACTIVE_CONNECTION_AUTH_STATUSES
            and getattr(connection, "owner_user_id", None) is not None
        )

    @staticmethod
    def _revision_is_valid(
        revision: AgentActionPolicyRevision | None, now: datetime
    ) -> bool:
        if (
            revision is None
            or revision.action_type != _ACTION_TYPE
            or _uuid(revision.connection_id) is None
            or len(str(getattr(revision, "content_hash", ""))) != 64
        ):
            return False
        try:
            if not window(revision, now.replace(tzinfo=None)):
                return False
            CrowdWorksPolicy.model_validate(revision.constraints_json)
        except Exception:
            return False
        return True

    @staticmethod
    def _agent_revision_is_valid(
        revision: AgentRevision | None,
        *,
        agent_id: Any,
    ) -> bool:
        return bool(
            revision is not None
            and _uuid(revision.id) is not None
            and _uuid(revision.agent_id) == _uuid(agent_id)
            and type(getattr(revision, "version", None)) is int
            and revision.version > 0
            and len(str(getattr(revision, "content_hash", ""))) == 64
        )

    async def _current_policy_revision(self, session: Any, policy_id: Any) -> Any:
        return await session.scalar(
            select(AgentActionPolicyRevision)
            .where(AgentActionPolicyRevision.policy_id == policy_id)
            .order_by(AgentActionPolicyRevision.version.desc())
            .limit(1)
        )

    async def _current_agent_revision(self, session: Any, agent_id: Any) -> Any:
        return await session.scalar(
            select(AgentRevision)
            .where(AgentRevision.agent_id == agent_id)
            .order_by(AgentRevision.version.desc())
            .limit(1)
        )

    async def selected_policy_binding(
        self,
        session: Any,
        *,
        connection_id: Any = None,
        owner_user_id: Any = None,
        policy_revision_id: Any = None,
        agent_id: Any = None,
        agent_revision_id: Any = None,
        now: datetime | None = None,
    ) -> _CurrentAgentScanBinding | None:
        """Resolve and validate the configured Policy against current DB state."""

        selected_policy_id = self.agent_scan_policy_id
        if not self.enabled or selected_policy_id is None:
            return None
        policy = await session.get(
            AgentActionPolicy,
            selected_policy_id,
            populate_existing=True,
        )
        if (
            policy is None
            or policy.state != "active"
            or _uuid(policy.id) != selected_policy_id
        ):
            return None
        selected_agent_id = _uuid(policy.agent_id)
        if selected_agent_id is None or (
            agent_id is not None and _uuid(agent_id) != selected_agent_id
        ):
            return None
        revision = await self._current_policy_revision(session, selected_policy_id)
        utc_now = self._now(now)
        if (
            not self._revision_is_valid(revision, utc_now)
            or _uuid(revision.policy_id) != selected_policy_id
            or (
                policy_revision_id is not None
                and _uuid(revision.id) != _uuid(policy_revision_id)
            )
        ):
            return None
        selected_connection_id = _uuid(revision.connection_id)
        if selected_connection_id is None or (
            connection_id is not None
            and _uuid(connection_id) != selected_connection_id
        ):
            return None
        connection = await session.get(
            ExternalConnection,
            selected_connection_id,
            populate_existing=True,
        )
        owner_id = _uuid(getattr(connection, "owner_user_id", None))
        if (
            not self._connection_is_active(connection)
            or _uuid(connection.id) != selected_connection_id
            or owner_id is None
            or (owner_user_id is not None and _uuid(owner_user_id) != owner_id)
        ):
            return None
        agent = await session.get(
            Agent,
            selected_agent_id,
            populate_existing=True,
        )
        if (
            agent is None
            or agent.state != "active"
            or _uuid(agent.id) != selected_agent_id
            or _uuid(agent.created_by) != owner_id
        ):
            return None
        current_agent_revision = await self._current_agent_revision(
            session, selected_agent_id
        )
        if (
            not self._agent_revision_is_valid(
                current_agent_revision,
                agent_id=selected_agent_id,
            )
            or (
                agent_revision_id is not None
                and _uuid(current_agent_revision.id) != _uuid(agent_revision_id)
            )
        ):
            return None
        return _CurrentAgentScanBinding(
            policy=policy,
            revision=revision,
            connection=connection,
            agent=agent,
            agent_revision=current_agent_revision,
        )

    def _required_capabilities(self) -> tuple[str, ...]:
        definition = self.policies.registry.require_executable(_ACTION_TYPE)
        return tuple(definition.required_capabilities or ())

    async def discover(
        self,
        session: Any,
        *,
        now: datetime | None = None,
    ) -> list[WorkCandidate]:
        if not self.enabled:
            return []
        utc_now = self._now(now)
        bucket = int(utc_now.timestamp()) // self.bucket_seconds
        binding = await self.selected_policy_binding(session, now=utc_now)
        if binding is None:
            return []
        try:
            required_capabilities = self._required_capabilities()
        except Exception:
            return []

        policy = binding.policy
        revision = binding.revision
        connection = binding.connection
        agent_revision = binding.agent_revision
        connection_id = str(connection.id)
        project_id = str(connection.project_id) if connection.project_id is not None else None
        source_revision = _source_revision(
            policy_id=policy.id,
            policy_revision_id=revision.id,
            policy_content_hash=revision.content_hash,
            agent_revision_id=agent_revision.id,
            bucket=bucket,
        )
        return [
            WorkCandidate(
                source_type=self.source_type,
                source_id=connection_id,
                source_revision=source_revision,
                intent_key=self.intent_key,
                domain="marketplace",
                project_id=project_id,
                assigned_agent_id=str(policy.agent_id),
                agent_revision_id=str(agent_revision.id),
                required_capabilities=required_capabilities,
                execution_adapter=self.execution_adapter,
                priority=10,
                max_attempts=3,
                concurrency_key=f"crowdworks:{connection_id}",
                metadata={
                    "policy_id": str(policy.id),
                    "policy_revision_id": str(revision.id),
                    "policy_content_hash": str(revision.content_hash),
                    "connection_id": connection_id,
                    "owner_user_id": str(connection.owner_user_id),
                    "project_id": project_id,
                },
            )
        ]

    async def refresh(self, session: Any, claim: Any) -> bool:
        """Recheck every pin and owner/project boundary before adapter entry."""

        if not self.enabled:
            return False
        metadata = getattr(claim, "metadata", None)
        if not isinstance(metadata, dict):
            return False
        if (
            getattr(claim, "source_type", None) != self.source_type
            or getattr(claim, "intent_key", None) != self.intent_key
            or getattr(claim, "execution_adapter", None) != self.execution_adapter
        ):
            return False

        policy_id = _uuid(metadata.get("policy_id"))
        revision_id = _uuid(metadata.get("policy_revision_id"))
        connection_id = _uuid(metadata.get("connection_id"))
        agent_id = _uuid(getattr(claim, "assigned_agent_id", None))
        agent_revision_id = _uuid(getattr(claim, "agent_revision_id", None))
        if None in (policy_id, revision_id, connection_id, agent_id, agent_revision_id):
            return False
        if policy_id != self.agent_scan_policy_id:
            return False
        if (
            str(getattr(claim, "source_id", "")) != str(connection_id)
            or getattr(claim, "concurrency_key", None) != f"crowdworks:{connection_id}"
        ):
            return False

        utc_now = self._now()
        binding = await self.selected_policy_binding(
            session,
            connection_id=connection_id,
            owner_user_id=metadata.get("owner_user_id"),
            policy_revision_id=revision_id,
            agent_id=agent_id,
            agent_revision_id=agent_revision_id,
            now=utc_now,
        )
        if binding is None:
            return False
        if (
            str(getattr(claim, "project_id", None) or "")
            != str(binding.connection.project_id or "")
            or str(metadata.get("project_id") or "")
            != str(binding.connection.project_id or "")
            or str(metadata.get("policy_content_hash") or "")
            != str(binding.revision.content_hash)
        ):
            return False

        try:
            required_capabilities = self._required_capabilities()
        except Exception:
            return False
        if tuple(getattr(claim, "required_capabilities", ()) or ()) != tuple(
            required_capabilities
        ):
            return False
        bucket = int(utc_now.timestamp()) // self.bucket_seconds
        expected_revision = _source_revision(
            policy_id=binding.policy.id,
            policy_revision_id=binding.revision.id,
            policy_content_hash=binding.revision.content_hash,
            agent_revision_id=binding.agent_revision.id,
            bucket=bucket,
        )
        if str(getattr(claim, "source_revision", "")) != expected_revision:
            return False

        # Phase1 discovery tests call refresh with a candidate-shaped object.
        # Only a durable Coordinator claim has a Work id and may prepare the
        # canonical ConversationSession/binding in this transaction.
        work_item_id = getattr(claim, "work_item_id", None)
        if work_item_id in (None, ""):
            return True
        from ..conversation_agent_service import ConversationAgentService

        service = self._conversation_agent_service or ConversationAgentService(
            config=getattr(self.policies, "config", None)
        )
        await service.prepare_work_session_binding(
            db_session=session,
            work_item=claim,
            owner_user_id=binding.connection.owner_user_id,
            project_id=binding.connection.project_id,
            agent_id=agent_id,
            agent_revision_id=agent_revision_id,
        )
        commit = getattr(session, "commit", None)
        if callable(commit):
            await commit()
        return True


class CrowdWorksAgentScanExecutionAdapter:
    """Execute one canonical CrowdWorks Agent scan turn.

    The Coordinator owns claiming, Run creation, lease renewal, and outcome
    settlement.  This adapter only consumes the persisted Work/Run evidence,
    reconstructs the immutable Chat execution from that evidence, and invokes
    a single tool-only Agent turn.  The optional seams are intentionally
    request-local and are used by contract tests to provide a fake provider;
    production defaults are the three shared Chat execution functions.
    """

    adapter_key = "crowdworks_agent_scan"
    source_type = "marketplace_crowdworks_agent_scan"
    intent_key = "marketplace.crowdworks.agent_scan"

    def __init__(
        self,
        *,
        config: Any | None = None,
        source_client: Any | None = None,
        client_builder: Any | None = None,
        provider_runner: Any | None = None,
    ) -> None:
        self.config = config
        self.source_client = source_client
        # The resolver and binder are deliberately fixed to the shared Chat
        # execution contract.  Only the provider/client boundary is injectable
        # so fake-provider tests cannot bypass persisted Run binding checks.
        self._client_builder = client_builder or build_agent_chat_turn_client
        self._provider_runner = provider_runner
        from ...tools.crowdworks_agent import CROWDWORKS_AGENT_TOOL_NAMES

        canonical_names = frozenset(CROWDWORKS_AGENT_TOOL_NAMES)
        definitions = tuple(
            item
            for item in self._default_tool_definitions()
            if _tool_name(item) in canonical_names
        )
        names = tuple(_tool_name(item) for item in definitions)
        if not definitions or any(not name for name in names):
            raise ValueError("CrowdWorks Agent tool group is unavailable")
        if len(set(names)) != len(names):
            raise ValueError("CrowdWorks Agent tool group contains duplicate tools")
        self.tool_definitions = definitions
        self.tool_names = frozenset(names)

    @staticmethod
    def _default_tool_definitions() -> tuple[Any, ...]:
        from ...tools.crowdworks_agent import CROWDWORKS_AGENT_TOOL_DEFINITIONS

        return tuple(CROWDWORKS_AGENT_TOOL_DEFINITIONS)

    def required_capabilities(self, claim: Any) -> Sequence[str]:
        return tuple(getattr(claim, "required_capabilities", ()) or ())

    @staticmethod
    def _expand_revision_tool_ids(tool_ids: Any) -> set[str] | None:
        """Expand pinned AgentRevision groups without widening the catalog.

        ``None`` is preserved for legacy pre-P2 revisions.  An explicit empty
        list is an intentional no-tools revision and remains empty.
        """

        if tool_ids is None:
            return None
        if isinstance(tool_ids, str):
            raw = (tool_ids,)
        elif isinstance(tool_ids, Sequence) and not isinstance(
            tool_ids, (bytes, bytearray)
        ):
            raw = tool_ids
        else:
            return set()
        try:
            from ...llm.tool_policy import _AGENT_REVISION_TOOL_GROUPS
        except Exception:
            _AGENT_REVISION_TOOL_GROUPS = {}
        expanded: set[str] = set()
        for item in raw:
            key = str(item or "").strip().casefold()
            if not key:
                continue
            expanded.update(
                str(name).strip()
                for name in _AGENT_REVISION_TOOL_GROUPS.get(key, frozenset({key}))
                if str(name).strip()
            )
        return expanded

    def _effective_tool_names(self, execution: Any) -> frozenset[str]:
        """Return the CW-only intersection for this pinned Chat execution."""

        catalog = frozenset(self.tool_names)
        revision_ids = getattr(execution, "tool_ids", None)
        dialogue_ids = getattr(execution, "dialogue_tool_ids", None)
        expanded_revision = self._expand_revision_tool_ids(revision_ids)
        if expanded_revision is None:
            # Legacy revisions have no P2 allowlist.  A request-local dialogue
            # scope, when present, is still a narrowing restriction.
            expanded_revision = self._expand_revision_tool_ids(dialogue_ids)
        if expanded_revision is None:
            return catalog
        effective = catalog & frozenset(expanded_revision)
        if dialogue_ids is not None and revision_ids is not None:
            dialogue_scope = self._expand_revision_tool_ids(dialogue_ids) or set()
            effective &= frozenset(dialogue_scope)
        return frozenset(effective)

    @staticmethod
    def _scope_execution(execution: Any, tool_names: frozenset[str]) -> Any:
        """Clamp both shared Chat scope fields to the computed CW group."""

        scoped_ids = tuple(sorted(tool_names))
        try:
            return replace(
                execution,
                tool_ids=scoped_ids,
                dialogue_tool_ids=scoped_ids,
            )
        except (TypeError, ValueError):
            # Test-only lightweight execution doubles may not be dataclasses.
            # Keep their binding object but expose the same bounded fields to
            # a provider/client seam that inspects them.
            try:
                setattr(execution, "tool_ids", scoped_ids)
                setattr(execution, "dialogue_tool_ids", scoped_ids)
            except Exception:
                pass
            return execution

    @staticmethod
    async def _session_get(session: Any, model: Any, identity: Any) -> Any:
        getter = getattr(session, "get", None)
        if not callable(getter):
            return None
        try:
            value = getter(model, identity, populate_existing=True)
        except TypeError:
            value = getter(model, identity)
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    async def _session_scalar(session: Any, statement: Any) -> Any:
        scalar = getattr(session, "scalar", None)
        if not callable(scalar):
            return None
        value = scalar(statement)
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _blocked(code: str, message: str | None = None, **metadata: Any) -> ExecutionOutcome:
        return ExecutionOutcome(
            classification="blocked",
            error_code=code,
            error_message=message or code,
            metadata=metadata,
        )

    @staticmethod
    def _provider_failure(exc: BaseException) -> ExecutionOutcome:
        detail = _text(exc, limit=240) or type(exc).__name__
        return ExecutionOutcome(
            classification="transient",
            error_code="crowdworks_provider_failed",
            error_message=detail,
            result={"completion_evidence": "provider_failure"},
        )

    @staticmethod
    def _log_scan_failure(claim: Any, outcome: ExecutionOutcome) -> None:
        if outcome.normalized_classification() in {"transient", "permanent"}:
            logger.warning(
                "CrowdWorks scan failure reason_code=crowdworks_agent_scan_failed "
                "unit_kind=tick work_item_id=%s classification=%s",
                getattr(claim, "work_item_id", None),
                outcome.normalized_classification(),
            )

    async def _load_context(
        self,
        *,
        session: Any,
        claim: Any,
        run_id: str | None,
        run_input: Any,
    ) -> tuple[Any, Any, Any, Any, Any, str, str, dict[str, Any]] | ExecutionOutcome:
        """Read and cross-check the canonical Work, Run, Connection, and binding."""

        work_id = _uuid(getattr(claim, "work_item_id", None))
        rid = _uuid(run_id or _read(run_input, "id"))
        if work_id is None or rid is None:
            return self._blocked(
                "crowdworks_run_identity_invalid",
                "CrowdWorks scan requires a canonical Work and Run identity",
            )
        work = await self._session_get(session, AgentWorkItem, work_id)
        run = await self._session_get(session, AgentRun, rid)
        if work is None or run is None:
            return self._blocked(
                "crowdworks_persisted_run_missing",
                "The canonical CrowdWorks Work or Run was not found",
            )

        # Never let the scheduler payload replace the persisted Run.  A
        # caller supplied mapping is only an input identity assertion.
        if run_input is not None:
            for field in (
                "id",
                "work_item_id",
                "session_id",
                "user_id",
                "agent_id",
                "agent_revision_id",
                "project_id",
                "work_item_attempt",
            ):
                supplied = _read(run_input, field)
                actual = _read(run, field)
                if supplied in (None, ""):
                    continue
                if field == "work_item_attempt":
                    try:
                        if int(supplied) != int(actual):
                            return self._blocked(
                                "crowdworks_input_identity_mismatch",
                                "The supplied AgentRun attempt does not match the persisted Run",
                                field=field,
                            )
                    except (TypeError, ValueError, OverflowError):
                        return self._blocked(
                            "crowdworks_input_identity_mismatch",
                            "The supplied AgentRun attempt is invalid",
                            field=field,
                        )
                    continue
                if field.endswith("_id") or field in {"id", "project_id"}:
                    if supplied is not None and actual is not None and not _same_uuid(supplied, actual):
                        return self._blocked(
                            "crowdworks_input_identity_mismatch",
                            "The supplied AgentRun identity does not match the persisted Run",
                            field=field,
                        )
                elif _text(supplied) != _text(actual):
                    return self._blocked(
                        "crowdworks_input_identity_mismatch",
                        "The supplied AgentRun identity does not match the persisted Run",
                        field=field,
                    )

        if (
            _text(getattr(claim, "source_type", "")) != self.source_type
            or _text(getattr(claim, "intent_key", "")) != self.intent_key
            or _text(getattr(claim, "execution_adapter", "")) != self.adapter_key
            or _text(getattr(work, "source_type", "")) != self.source_type
            or _text(getattr(work, "intent_key", "")) != self.intent_key
            or _text(getattr(work, "execution_adapter", "")) != self.adapter_key
        ):
            return self._blocked(
                "crowdworks_input_identity_mismatch",
                "The Work is not a dedicated CrowdWorks Agent scan",
            )
        for field in ("source_id", "source_revision"):
            if _text(getattr(claim, field, None)) != _text(getattr(work, field, None)):
                return self._blocked(
                    "crowdworks_input_identity_mismatch",
                    "The Work identity does not match the claimed scan",
                    field=field,
                )
        if _text(getattr(claim, "work_item_id", None)) != _text(getattr(work, "id", None)):
            return self._blocked(
                "crowdworks_input_identity_mismatch",
                "The claimed Work id does not match the persisted Work",
            )

        # Every claim pin is an assertion about the durable Work.  Check the
        # full identity before reading policy or resolving a Chat route so a
        # forged claim cannot select a different Agent, revision, project, or
        # concurrency lane.
        claim_agent_id = getattr(claim, "assigned_agent_id", None)
        if claim_agent_id in (None, ""):
            claim_agent_id = getattr(claim, "agent_id", None)
        for field, claim_value in (
            ("assigned_agent_id", claim_agent_id),
            ("agent_revision_id", getattr(claim, "agent_revision_id", None)),
        ):
            if not _same_uuid(
                claim_value, getattr(work, field, None)
            ):
                return self._blocked(
                    "crowdworks_input_identity_mismatch",
                    "The claimed Agent identity does not match the persisted Work",
                    field=field,
                )
        claim_project = getattr(claim, "project_id", None)
        work_project = getattr(work, "project_id", None)
        if not (
            (claim_project is None and work_project is None)
            or _same_uuid(claim_project, work_project)
        ):
            return self._blocked(
                "crowdworks_input_identity_mismatch",
                "The claimed Project does not match the persisted Work",
                field="project_id",
            )
        if _text(getattr(claim, "concurrency_key", None)) != _text(
            getattr(work, "concurrency_key", None)
        ):
            return self._blocked(
                "crowdworks_input_identity_mismatch",
                "The claimed concurrency key does not match the persisted Work",
                field="concurrency_key",
            )
        claim_capabilities = tuple(getattr(claim, "required_capabilities", ()) or ())
        work_capabilities = tuple(
            _read(work, "required_capabilities_json", None)
            or _read(work, "required_capabilities", ())
            or ()
        )
        if claim_capabilities != work_capabilities:
            return self._blocked(
                "crowdworks_input_identity_mismatch",
                "The claimed capability ceiling does not match the persisted Work",
                field="required_capabilities",
            )
        claim_attempt = getattr(claim, "attempt", None)
        work_attempt = getattr(work, "attempt_count", None)
        if claim_attempt is not None and work_attempt is not None:
            try:
                if int(claim_attempt) != int(work_attempt):
                    return self._blocked(
                        "crowdworks_input_identity_mismatch",
                        "The claimed attempt does not match the persisted Work",
                        field="attempt",
                    )
            except (TypeError, ValueError, OverflowError):
                return self._blocked(
                    "crowdworks_input_identity_mismatch",
                    "The claimed attempt is invalid",
                    field="attempt",
                )
        claim_session = getattr(claim, "conversation_session_id", None)
        work_session = getattr(work, "conversation_session_id", None)
        if claim_session not in (None, "") and not _same_uuid(
            claim_session, work_session
        ):
            return self._blocked(
                "crowdworks_input_identity_mismatch",
                "The claimed ConversationSession does not match the persisted Work",
                field="conversation_session_id",
            )
        if not _same_uuid(getattr(work, "active_agent_run_id", None), rid):
            return self._blocked(
                "crowdworks_run_binding_mismatch",
                "The Work is not linked to the supplied active AgentRun",
            )
        if _text(getattr(work, "state", "")).casefold() != "running":
            return self._blocked(
                "crowdworks_work_not_running",
                "The CrowdWorks Work is no longer executable",
            )
        if _text(_read(run, "status")).casefold() != "running":
            return self._blocked(
                "crowdworks_run_not_running",
                "The persisted AgentRun is no longer executable",
            )
        if not _same_uuid(getattr(run, "work_item_id", None), work.id):
            return self._blocked(
                "crowdworks_run_binding_mismatch",
                "The persisted AgentRun is linked to another Work",
            )
        run_attempt = _read(run, "work_item_attempt")
        if claim_attempt is None or run_attempt is None:
            return self._blocked(
                "crowdworks_run_binding_mismatch",
                "The persisted AgentRun is missing its Work attempt pin",
            )
        try:
            if int(run_attempt) != int(claim_attempt):
                return self._blocked(
                    "crowdworks_run_binding_mismatch",
                    "The persisted AgentRun attempt does not match the claim",
                )
        except (TypeError, ValueError, OverflowError):
            return self._blocked(
                "crowdworks_run_binding_mismatch",
                "The persisted AgentRun attempt is invalid",
            )
        if not _same_uuid(getattr(run, "agent_id", None), getattr(work, "assigned_agent_id", None)):
            return self._blocked(
                "crowdworks_agent_identity_mismatch",
                "The Run Agent does not match the Work Agent",
            )
        if not _same_uuid(getattr(run, "agent_revision_id", None), getattr(work, "agent_revision_id", None)):
            return self._blocked(
                "crowdworks_agent_revision_mismatch",
                "The Run AgentRevision does not match the Work AgentRevision",
            )

        metadata = _safe_mapping(getattr(work, "metadata_json", None))
        run_metadata = _safe_mapping(_read(run, "run_metadata") or _read(run, "metadata"))
        owner_id = _uuid(metadata.get("owner_user_id"))
        connection_id = _uuid(metadata.get("connection_id") or getattr(claim, "source_id", None))
        policy_id = _uuid(metadata.get("policy_id"))
        policy_revision_id = _uuid(metadata.get("policy_revision_id"))
        if owner_id is None or connection_id is None or policy_id is None or policy_revision_id is None:
            return self._blocked(
                "crowdworks_binding_evidence_missing",
                "The Work does not contain complete CrowdWorks policy/owner evidence",
            )
        if not _same_uuid(connection_id, getattr(claim, "source_id", None)):
            return self._blocked(
                "crowdworks_input_identity_mismatch",
                "The claimed source is not the persisted CrowdWorks connection",
            )
        connection = await self._session_get(session, ExternalConnection, connection_id)
        policy = await self._session_get(session, AgentActionPolicy, policy_id)
        policy_revision = await self._session_get(
            session, AgentActionPolicyRevision, policy_revision_id
        )
        if connection is None or policy is None or policy_revision is None:
            return self._blocked(
                "crowdworks_policy_evidence_missing",
                "The persisted CrowdWorks policy or connection is unavailable",
            )
        if (
            not CrowdWorksAgentScanWorkSource._connection_is_active(connection)
            or not _same_uuid(connection.id, connection_id)
            or not _same_uuid(connection.owner_user_id, owner_id)
            or not _same_uuid(policy.id, policy_id)
            or _text(policy.state).casefold() != "active"
            or not _same_uuid(policy.agent_id, getattr(work, "assigned_agent_id", None))
            or not _same_uuid(policy_revision.policy_id, policy_id)
            or not _same_uuid(policy_revision.connection_id, connection_id)
            or _text(policy_revision.action_type) != _ACTION_TYPE
        ):
            return self._blocked(
                "crowdworks_policy_connection_mismatch",
                "The CrowdWorks policy and connection evidence changed",
            )
        current_revision = await self._session_scalar(
            session,
            select(AgentActionPolicyRevision)
            .where(AgentActionPolicyRevision.policy_id == policy_id)
            .order_by(AgentActionPolicyRevision.version.desc())
            .limit(1),
        )
        if current_revision is None or not _same_uuid(
            current_revision.id, policy_revision_id
        ):
            return self._blocked(
                "crowdworks_policy_revision_changed",
                "The CrowdWorks policy revision changed before execution",
            )
        persisted_policy_hash = metadata.get("policy_content_hash")
        if _text(persisted_policy_hash) != _text(
            getattr(current_revision, "content_hash", None)
        ):
            return self._blocked(
                "crowdworks_policy_revision_changed",
                "The Work policy content hash does not match the current revision",
            )
        if not CrowdWorksAgentScanWorkSource._revision_is_valid(
            policy_revision, _as_utc(datetime.now(timezone.utc))
        ):
            return self._blocked(
                "crowdworks_policy_revision_invalid",
                "The CrowdWorks policy revision is not currently executable",
            )

        project_id = getattr(connection, "project_id", None)
        if not _same_uuid(getattr(work, "project_id", None), project_id) or not _same_uuid(
            getattr(run, "project_id", None), project_id
        ):
            # ``None`` is the canonical projectless value.  UUID equality is
            # handled separately because _same_uuid intentionally rejects two
            # nulls.
            if not (
                getattr(work, "project_id", None) is None
                and getattr(run, "project_id", None) is None
                and project_id is None
            ):
                return self._blocked(
                    "crowdworks_project_mismatch",
                    "The Work, Run, and Connection Project identities differ",
                )
        if _text(getattr(run, "user_id", None)) != str(owner_id):
            return self._blocked(
                "crowdworks_owner_mismatch",
                "The persisted Run owner does not match the Connection owner",
            )
        session_id = _uuid(
            getattr(work, "conversation_session_id", None)
            or metadata.get("conversation_session_id")
        )
        if session_id is None or not _same_uuid(getattr(run, "session_id", None), session_id):
            return self._blocked(
                "crowdworks_session_mismatch",
                "The Work and persisted Run do not share the canonical session",
            )
        conversation = await self._session_get(session, ConversationSession, session_id)
        if (
            conversation is None
            or getattr(conversation, "deleted_at", None) is not None
            or _text(getattr(conversation, "user_id", None)) != str(owner_id)
            or not (
                getattr(conversation, "project_id", None) is None
                and project_id is None
                or _same_uuid(getattr(conversation, "project_id", None), project_id)
            )
            or not (
                getattr(conversation, "agent_id", None) is None
                or _same_uuid(conversation.agent_id, getattr(work, "assigned_agent_id", None))
            )
        ):
            return self._blocked(
                "crowdworks_session_mismatch",
                "The canonical ConversationSession does not match the Work",
            )

        work_binding = metadata.get("agent_binding")
        run_binding = run_metadata.get("agent_binding")
        if not isinstance(work_binding, Mapping) or not isinstance(run_binding, Mapping):
            return self._blocked(
                "crowdworks_binding_evidence_missing",
                "The Work and Run must both carry immutable binding evidence",
            )
        for name in ("binding_id", "snapshot_hash", "session_id", "agent_id", "agent_revision_id"):
            if _text(work_binding.get(name)) != _text(run_binding.get(name)):
                return self._blocked(
                    "crowdworks_binding_mismatch",
                    "The Work and Run binding evidence differs",
                    field=name,
                )
        if (
            not _same_uuid(work_binding.get("session_id"), session_id)
            or not _same_uuid(work_binding.get("agent_id"), getattr(work, "assigned_agent_id", None))
            or not _same_uuid(work_binding.get("agent_revision_id"), getattr(work, "agent_revision_id", None))
            or _uuid(work_binding.get("binding_id")) is None
            or len(_text(work_binding.get("snapshot_hash"))) != 64
        ):
            return self._blocked(
                "crowdworks_binding_mismatch",
                "The immutable binding evidence is malformed",
            )
        return (
            work,
            run,
            connection,
            policy,
            policy_revision,
            str(session_id),
            str(owner_id),
            {
                "work_metadata": metadata,
                "run_metadata": run_metadata,
                "binding": dict(run_binding),
                "policy_id": str(policy_id),
                "policy_revision_id": str(policy_revision_id),
                "connection_id": str(connection_id),
                "project_id": str(project_id) if project_id is not None else None,
            },
        )

    @staticmethod
    def _call_with_supported_kwargs(function: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            signature = inspect.signature(function)
            params = signature.parameters
            if not any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values()):
                kwargs = {key: value for key, value in kwargs.items() if key in params}
        except (TypeError, ValueError):
            pass
        return function(*args, **kwargs)

    def _install_tools(
        self,
        client: Any,
        *,
        tool_definitions: Sequence[Any],
        tool_names: frozenset[str],
    ) -> None:
        """Install only the pinned CW group on the request-local client."""

        setattr(client, "_agent_tool_ids", sorted(tool_names))
        setattr(client, "_crowdworks_agent_tool_definitions", tuple(tool_definitions))
        # Replace the request-local registry with a bounded view.  Reusing a
        # source client's registry could retain filesystem/browser tools even
        # though the AgentRevision allowlist is narrower.
        try:
            from ...tools.registry import ToolRegistry

            registry = ToolRegistry()
            for definition in tool_definitions:
                registry.register(definition)
            setattr(client, "_tool_registry", registry)
            setter = getattr(client, "set_tool_registry", None)
            if callable(setter):
                setter(registry)
        except Exception:
            logger.debug("CrowdWorks fake client has no tool registry", exc_info=True)

    @staticmethod
    def _bounded_native_tool_names(
        values: Any,
        *,
        allowed_names: frozenset[str],
    ) -> tuple[int, list[str], int]:
        if values is None:
            items: list[Any] = []
        elif isinstance(values, (str, bytes, bytearray)):
            items = [values]
        else:
            try:
                items = list(values)
            except TypeError:
                items = [values]
        names: set[str] = set()
        other = 0
        for item in items:
            name = _tool_name(item)
            if name in allowed_names:
                names.add(name)
            else:
                other += 1
        return len(items), sorted(names), other

    @staticmethod
    def _native_provider_tool_names(
        messages: Any,
        *,
        allowed_names: frozenset[str],
    ) -> tuple[int, list[str], int]:
        if not isinstance(messages, Sequence) or isinstance(
            messages, (str, bytes, bytearray)
        ):
            return 0, [], 0
        names: set[str] = set()
        count = 0
        other = 0
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            calls = message.get("tool_calls")
            if isinstance(calls, Sequence) and not isinstance(
                calls, (str, bytes, bytearray)
            ):
                for call in calls:
                    function = (
                        call.get("function")
                        if isinstance(call, Mapping)
                        else getattr(call, "function", None)
                    )
                    name = _tool_name(function) or _tool_name(call)
                    count += 1
                    if name in allowed_names:
                        names.add(name)
                    else:
                        other += 1
            if _text(message.get("type"), limit=48) == "function_call":
                count += 1
                name = _tool_name(message)
                if name in allowed_names:
                    names.add(name)
                else:
                    other += 1
        return count, sorted(names), other

    @staticmethod
    def _capture_native_request_tool_records(
        client: Any,
        *,
        tool_names: frozenset[str],
        run_id: str,
        run_service: Any,
    ) -> dict[str, Any] | None:
        """Capture request-local native schema, provider-call and receipt counts.

        No prompt, arguments, tool output, or marketplace text crosses this
        diagnostic seam. Receipts accumulate across native runner invocations
        so a later tool-free verifier pass cannot erase an earlier execution.
        """

        allowed_names = frozenset(tool_names)
        pinned_route = getattr(client, "_agent_route", None)
        pinned_route = pinned_route if isinstance(pinned_route, Mapping) else {}
        diagnostic = {
            "allowed_tool_count": len(allowed_names),
            "allowed_tool_names": sorted(allowed_names),
            "pinned_route": {
                key: _text(pinned_route.get(key), limit=160)
                for key in ("provider", "model", "effort")
                if _text(pinned_route.get(key), limit=160)
            },
            "client_route": {
                "provider": _text(getattr(client, "provider_label", None), limit=80),
                "model": _text(getattr(client, "model_name", None), limit=160),
            },
            "client_schema_resolution_count": 0,
            "client_schema_tool_count": 0,
            "client_schema_tool_names": [],
            "client_schema_other_tool_count": 0,
            "capture_hook_installed": False,
            "client_schema_capture_installed": False,
            "native_run_count": 0,
            "tool_free_native_run_count": 0,
            "provider_tool_call_count": 0,
            "provider_tool_call_names": [],
            "provider_other_tool_call_count": 0,
            "native_receipt_count": 0,
            "native_receipt_tool_names": [],
            "native_other_receipt_count": 0,
            "captured_receipt_count": 0,
            "captured_receipt_tool_names": [],
            "captured_other_receipt_count": 0,
            "provider_text_present": False,
            "provider_text_char_count": 0,
            "normalized_tool_result_count": 0,
            "normalization_reason_code": None,
            "native_requests": [],
            "native_requests_truncated": 0,
        }
        try:
            setattr(client, "_crowdworks_request_tool_records", [])
            setattr(client, "_crowdworks_native_tool_diagnostics", diagnostic)
        except Exception:
            return None

        resolver_capture: tuple[Any, Any, Any] | None = None
        original_resolver = getattr(
            client, "_get_effective_tools_for_current_session", None
        )
        if callable(original_resolver):

            @wraps(original_resolver)
            def capture_effective_tools(*args: Any, **kwargs: Any) -> Any:
                value = original_resolver(*args, **kwargs)
                count, names, other = CrowdWorksAgentScanExecutionAdapter._bounded_native_tool_names(
                    value,
                    allowed_names=allowed_names,
                )
                diagnostic["client_schema_resolution_count"] += 1
                diagnostic["client_schema_tool_count"] = count
                diagnostic["client_schema_tool_names"] = names
                diagnostic["client_schema_other_tool_count"] = other
                return value

            try:
                setattr(client, "_get_effective_tools_for_current_session", capture_effective_tools)
                diagnostic["client_schema_capture_installed"] = True
                resolver_capture = (client, original_resolver, capture_effective_tools)
            except Exception:
                pass

        runner = getattr(client, "_turn_runner", None)
        original_run = getattr(runner, "run", None)
        runner_capture: tuple[Any, Any, Any] | None = None
        if callable(original_run):

            @wraps(original_run)
            async def capture_result(*args: Any, **kwargs: Any) -> Any:
                agent = args[0] if args else kwargs.get("agent")
                agent_count, agent_names, agent_other = (
                    CrowdWorksAgentScanExecutionAdapter._bounded_native_tool_names(
                        getattr(agent, "tools", None),
                        allowed_names=allowed_names,
                    )
                )
                tool_free = kwargs.get("tool_free") is True
                request_diagnostic: dict[str, Any] = {
                    "tool_free": tool_free,
                    "agent_tool_count": agent_count,
                    "agent_tool_names": agent_names,
                    "agent_other_tool_count": agent_other,
                    "native_provider_candidate_tool_count": 0,
                    "native_provider_candidate_tool_names": [],
                    "native_provider_candidate_other_tool_count": 0,
                    "native_schema_tool_count": 0,
                    "native_schema_tool_names": [],
                    "native_schema_other_tool_count": 0,
                    "provider_tool_call_count": 0,
                    "provider_tool_call_names": [],
                    "provider_other_tool_call_count": 0,
                    "native_receipt_count": 0,
                    "native_receipt_tool_names": [],
                    "native_other_receipt_count": 0,
                    "result_text_present": False,
                    "result_text_char_count": 0,
                }
                run_kwargs = dict(kwargs)
                original_provider = run_kwargs.get("tools_provider")
                # Only the real tool-bearing generation is constrained.  The
                # first schema contains scan tools alone; subsequent rounds
                # regain the original tools so the turn can finish normally.
                prior_records = getattr(client, "_crowdworks_request_tool_records", ())
                require_scan = (
                    not tool_free
                    and bool(_SCAN_TOOLS.intersection(agent_names))
                    and not any(_tool_name(record) in _SCAN_TOOLS for record in prior_records)
                )
                if require_scan:
                    agent = replace(
                        agent,
                        model_settings=replace(agent.model_settings, tool_choice="required"),
                    )
                    if args:
                        args = (agent, *args[1:])
                    else:
                        run_kwargs["agent"] = agent
                    base_provider = original_provider
                    first_resolution = True

                    def scan_first_provider(provider_agent: Any) -> list[Any]:
                        nonlocal first_resolution
                        definitions = list(base_provider(provider_agent) if callable(base_provider) else provider_agent.tools)
                        if first_resolution:
                            first_resolution = False
                            return [item for item in definitions if _tool_name(item) in _SCAN_TOOLS]
                        return definitions

                    original_provider = scan_first_provider
                    run_kwargs["tools_provider"] = original_provider
                if callable(original_provider) and not tool_free:

                    def capture_tool_provider(provider_agent: Any) -> list[Any]:
                        provided = original_provider(provider_agent)
                        if provided is None:
                            definitions: list[Any] = []
                        elif isinstance(provided, (str, bytes, bytearray)):
                            definitions = [provided]
                        else:
                            definitions = list(provided)
                        (
                            request_diagnostic[
                                "native_provider_candidate_tool_count"
                            ],
                            request_diagnostic[
                                "native_provider_candidate_tool_names"
                            ],
                            request_diagnostic[
                                "native_provider_candidate_other_tool_count"
                            ],
                        ) = CrowdWorksAgentScanExecutionAdapter._bounded_native_tool_names(
                            definitions,
                            allowed_names=allowed_names,
                        )
                        native_schema = [
                            definition
                            for definition in definitions
                            if _tool_name(definition) in allowed_names
                        ]
                        (
                            request_diagnostic["native_schema_tool_count"],
                            request_diagnostic["native_schema_tool_names"],
                            request_diagnostic["native_schema_other_tool_count"],
                        ) = CrowdWorksAgentScanExecutionAdapter._bounded_native_tool_names(
                            native_schema,
                            allowed_names=allowed_names,
                        )
                        return definitions

                    run_kwargs["tools_provider"] = capture_tool_provider
                elif not tool_free:
                    (
                        request_diagnostic[
                            "native_provider_candidate_tool_count"
                        ],
                        request_diagnostic[
                            "native_provider_candidate_tool_names"
                        ],
                        request_diagnostic[
                            "native_provider_candidate_other_tool_count"
                        ],
                    ) = CrowdWorksAgentScanExecutionAdapter._bounded_native_tool_names(
                        getattr(agent, "tools", None),
                        allowed_names=allowed_names,
                    )
                    native_schema = [
                        definition
                        for definition in getattr(agent, "tools", ()) or ()
                        if _tool_name(definition) in allowed_names
                    ]
                    (
                        request_diagnostic["native_schema_tool_count"],
                        request_diagnostic["native_schema_tool_names"],
                        request_diagnostic["native_schema_other_tool_count"],
                    ) = CrowdWorksAgentScanExecutionAdapter._bounded_native_tool_names(
                        native_schema,
                        allowed_names=allowed_names,
                    )

                native_result = original_run(*args, **run_kwargs)
                if inspect.isawaitable(native_result):
                    native_result = await native_result
                messages = getattr(native_result, "messages", None)
                (
                    request_diagnostic["provider_tool_call_count"],
                    request_diagnostic["provider_tool_call_names"],
                    request_diagnostic["provider_other_tool_call_count"],
                ) = CrowdWorksAgentScanExecutionAdapter._native_provider_tool_names(
                    messages,
                    allowed_names=allowed_names,
                )
                records = getattr(native_result, "tool_calls", None)
                if isinstance(records, Sequence) and not isinstance(
                    records, (str, bytes, bytearray)
                ):
                    (
                        request_diagnostic["native_receipt_count"],
                        request_diagnostic["native_receipt_tool_names"],
                        request_diagnostic["native_other_receipt_count"],
                    ) = CrowdWorksAgentScanExecutionAdapter._bounded_native_tool_names(
                        records,
                        allowed_names=allowed_names,
                    )
                    previous_records = getattr(
                        client, "_crowdworks_request_tool_records", None
                    )
                    previous_records = (
                        list(previous_records)
                        if isinstance(previous_records, Sequence)
                        and not isinstance(previous_records, (str, bytes, bytearray))
                        else []
                    )
                    from ...assistant.modes.agent_run_events import (
                        _agent_run_tool_call_id,
                        _agent_run_tool_call_payload,
                    )

                    from ..agent_run_service import canonical_audit_digest

                    for index, record in enumerate(records, start=len(previous_records)):
                        payload = _agent_run_tool_call_payload(record)
                        if payload["tool"] not in allowed_names:
                            continue
                        result_digest = canonical_audit_digest(payload["result"])
                        receipt = await run_service.record_tool_call(
                            run_id,
                            tool_name=payload["tool"],
                            arguments=payload["arguments"],
                            result=payload["result"],
                            success=payload["successful"],
                            tool_call_id=_agent_run_tool_call_id(run_id, index, payload),
                            metadata={
                                "source": "crowdworks_agent_scan",
                                "result_digest": result_digest,
                            },
                        )
                        if (
                            receipt is None
                            or receipt.get("success") is not payload["successful"]
                            or (receipt.get("metadata") or {}).get("result_digest") != result_digest
                        ):
                            raise RuntimeError("CrowdWorks AgentRun tool audit was not persisted consistently")
                    previous_records.extend(records)
                    try:
                        setattr(client, "_crowdworks_request_tool_records", previous_records)
                    except Exception:
                        pass
                    _, captured_names, captured_other = CrowdWorksAgentScanExecutionAdapter._bounded_native_tool_names(
                        previous_records,
                        allowed_names=allowed_names,
                    )
                    diagnostic["captured_receipt_count"] = len(previous_records)
                    diagnostic["captured_receipt_tool_names"] = captured_names
                    diagnostic["captured_other_receipt_count"] = captured_other

                final_output = getattr(native_result, "final_output", None)
                if isinstance(final_output, str):
                    request_diagnostic["result_text_present"] = bool(final_output)
                    request_diagnostic["result_text_char_count"] = min(
                        len(final_output), 1_000_000
                    )
                diagnostic["native_run_count"] += 1
                if tool_free:
                    diagnostic["tool_free_native_run_count"] += 1
                diagnostic["provider_tool_call_count"] += request_diagnostic[
                    "provider_tool_call_count"
                ]
                diagnostic["provider_other_tool_call_count"] += request_diagnostic[
                    "provider_other_tool_call_count"
                ]
                diagnostic["native_receipt_count"] += request_diagnostic[
                    "native_receipt_count"
                ]
                diagnostic["native_other_receipt_count"] += request_diagnostic[
                    "native_other_receipt_count"
                ]
                prior_provider_names = set(diagnostic["provider_tool_call_names"])
                prior_provider_names.update(
                    request_diagnostic["provider_tool_call_names"]
                )
                diagnostic["provider_tool_call_names"] = sorted(prior_provider_names)
                prior_receipt_names = set(
                    diagnostic["native_receipt_tool_names"]
                )
                prior_receipt_names.update(
                    request_diagnostic["native_receipt_tool_names"]
                )
                diagnostic["native_receipt_tool_names"] = sorted(prior_receipt_names)
                if len(diagnostic["native_requests"]) < 8:
                    diagnostic["native_requests"].append(request_diagnostic)
                else:
                    diagnostic["native_requests_truncated"] += 1
                return native_result

            try:
                setattr(runner, "run", capture_result)
                diagnostic["capture_hook_installed"] = True
                runner_capture = (runner, original_run, capture_result)
            except Exception:
                pass

        if runner_capture is None and resolver_capture is None:
            return None
        return {"runner": runner_capture, "resolver": resolver_capture}

    @staticmethod
    def _restore_native_request_tool_record_capture(
        capture: Mapping[str, Any] | None,
    ) -> None:
        if not isinstance(capture, Mapping):
            return
        runner_capture = capture.get("runner")
        if isinstance(runner_capture, tuple) and len(runner_capture) == 3:
            runner, original_run, capture_result = runner_capture
            if getattr(runner, "run", None) is capture_result:
                try:
                    setattr(runner, "run", original_run)
                except Exception:
                    pass
        resolver_capture = capture.get("resolver")
        if isinstance(resolver_capture, tuple) and len(resolver_capture) == 3:
            client, original_resolver, capture_resolver = resolver_capture
            if (
                getattr(client, "_get_effective_tools_for_current_session", None)
                is capture_resolver
            ):
                try:
                    setattr(
                        client,
                        "_get_effective_tools_for_current_session",
                        original_resolver,
                    )
                except Exception:
                    pass

    @staticmethod
    def _native_tool_diagnostics_for_result(
        client: Any | None,
        *,
        allowed_names: frozenset[str],
        provider_text: str,
        normalized_tool_result_count: int,
        normalization_reason_code: str | None,
    ) -> dict[str, Any] | None:
        raw = (
            getattr(client, "_crowdworks_native_tool_diagnostics", None)
            if client is not None
            else None
        )
        if not isinstance(raw, Mapping) or not (
            raw.get("client_schema_capture_installed") is True
            or raw.get("capture_hook_installed") is True
        ):
            return None

        def count(key: str) -> int:
            value = raw.get(key)
            return max(0, min(int(value), 1_000_000)) if type(value) is int else 0

        def safe_names(key: str) -> list[str]:
            value = raw.get(key)
            if not isinstance(value, (list, tuple, set, frozenset)):
                return []
            return sorted(
                {
                    item
                    for item in value
                    if isinstance(item, str) and item in allowed_names
                }
            )

        def safe_route(key: str) -> dict[str, str]:
            value = raw.get(key)
            if not isinstance(value, Mapping):
                return {}
            return {
                field: _text(value.get(field), limit=160)
                for field in ("provider", "model", "effort")
                if _text(value.get(field), limit=160)
            }

        def count_from(value: Mapping[str, Any], key: str) -> int:
            item = value.get(key)
            return max(0, min(item, 1_000_000)) if type(item) is int else 0

        def names_from(value: Mapping[str, Any], key: str) -> list[str]:
            names = value.get(key)
            if not isinstance(names, (list, tuple, set, frozenset)):
                return []
            return sorted(
                {
                    name
                    for name in names
                    if isinstance(name, str) and name in allowed_names
                }
            )

        requests: list[dict[str, Any]] = []
        raw_requests = raw.get("native_requests")
        if isinstance(raw_requests, (list, tuple)):
            for item in list(raw_requests)[:8]:
                if not isinstance(item, Mapping):
                    continue
                request = {
                    "tool_free": item.get("tool_free") is True,
                    "agent_tool_count": count_from(item, "agent_tool_count"),
                    "agent_tool_names": names_from(item, "agent_tool_names"),
                    "agent_other_tool_count": count_from(item, "agent_other_tool_count"),
                    "native_provider_candidate_tool_count": count_from(
                        item, "native_provider_candidate_tool_count"
                    ),
                    "native_provider_candidate_tool_names": names_from(
                        item, "native_provider_candidate_tool_names"
                    ),
                    "native_provider_candidate_other_tool_count": count_from(
                        item, "native_provider_candidate_other_tool_count"
                    ),
                    "native_schema_tool_count": count_from(item, "native_schema_tool_count"),
                    "native_schema_tool_names": names_from(item, "native_schema_tool_names"),
                    "native_schema_other_tool_count": count_from(item, "native_schema_other_tool_count"),
                    "provider_tool_call_count": count_from(item, "provider_tool_call_count"),
                    "provider_tool_call_names": names_from(item, "provider_tool_call_names"),
                    "provider_other_tool_call_count": count_from(item, "provider_other_tool_call_count"),
                    "native_receipt_count": count_from(item, "native_receipt_count"),
                    "native_receipt_tool_names": names_from(item, "native_receipt_tool_names"),
                    "native_other_receipt_count": count_from(item, "native_other_receipt_count"),
                    "result_text_present": item.get("result_text_present") is True,
                    "result_text_char_count": count_from(item, "result_text_char_count"),
                }
                requests.append(request)

        diagnostic = {
            "allowed_tool_count": len(allowed_names),
            "allowed_tool_names": sorted(allowed_names),
            "pinned_route": safe_route("pinned_route"),
            "client_route": safe_route("client_route"),
            "client_schema_capture_installed": raw.get("client_schema_capture_installed") is True,
            "client_schema_resolution_count": count("client_schema_resolution_count"),
            "client_schema_tool_count": count("client_schema_tool_count"),
            "client_schema_tool_names": safe_names("client_schema_tool_names"),
            "client_schema_other_tool_count": count("client_schema_other_tool_count"),
            "capture_hook_installed": raw.get("capture_hook_installed") is True,
            "native_run_count": count("native_run_count"),
            "tool_free_native_run_count": count("tool_free_native_run_count"),
            "provider_tool_call_count": count("provider_tool_call_count"),
            "provider_tool_call_names": safe_names("provider_tool_call_names"),
            "provider_other_tool_call_count": count("provider_other_tool_call_count"),
            "native_receipt_count": count("native_receipt_count"),
            "native_receipt_tool_names": safe_names("native_receipt_tool_names"),
            "native_other_receipt_count": count("native_other_receipt_count"),
            "captured_receipt_count": count("captured_receipt_count"),
            "captured_receipt_tool_names": safe_names("captured_receipt_tool_names"),
            "captured_other_receipt_count": count("captured_other_receipt_count"),
            "provider_text_present": bool(provider_text),
            "provider_text_char_count": min(len(provider_text), 1_000_000),
            "normalized_tool_result_count": max(0, int(normalized_tool_result_count)),
            "normalization_reason_code": _text(normalization_reason_code, limit=96),
            "native_requests": requests,
            "native_requests_truncated": count("native_requests_truncated"),
        }
        return diagnostic

    @staticmethod
    def _provider_terminal(value: Any) -> Mapping[str, Any] | None:
        terminal = value.get("provider_terminal") if isinstance(value, Mapping) else None
        if not isinstance(terminal, Mapping):
            terminal = getattr(value, "provider_terminal", None)
        return terminal if isinstance(terminal, Mapping) else None

    @staticmethod
    def _decode_tool_result(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            return value
        try:
            decoded = json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            return value
        return decoded if isinstance(decoded, (Mapping, list, tuple)) else value

    @classmethod
    def _record_success(cls, record: Any, result: Any) -> bool:
        explicit = _read(record, "success")
        if explicit is None:
            explicit = _read(record, "successful")
        if explicit is not None:
            return bool(explicit)
        executed = _read(record, "executed")
        if executed is False:
            return False
        return _result_success(cls._decode_tool_result(result))

    @classmethod
    def _record_to_tool_result(
        cls,
        record: Any,
        *,
        tool_names: frozenset[str],
    ) -> dict[str, Any] | None:
        name = _tool_name(record)
        if not name:
            return None
        if name not in tool_names:
            return {
                "tool": name,
                "success": False,
                "error_code": "crowdworks_tool_not_allowed",
            }
        raw_result = (
            _read(record, "result")
            or _read(record, "output")
            or _read(record, "model_output")
            or _read(record, "tool_result")
        )
        result = cls._decode_tool_result(raw_result)
        success = cls._record_success(record, raw_result)
        error_code = _read(record, "error_code") or _read(record, "reason_code")
        if not error_code:
            error_code = _read(record, "blocked_reason")
        policy_denial = _read(record, "policy_denial")
        if not error_code and isinstance(policy_denial, Mapping):
            error_code = policy_denial.get("code")
        if not error_code and isinstance(result, Mapping):
            error_code = result.get("error_code") or result.get("reason_code")
        output: dict[str, Any] = {
            "tool": name,
            "arguments": _safe_result(_read(record, "arguments") or {}),
            "result": _safe_result(result),
            "success": success,
        }
        tool_call_id = _text(_read(record, "tool_call_id"), limit=160)
        if tool_call_id:
            output["tool_call_id"] = tool_call_id
        if error_code:
            output["error_code"] = error_code
        return output

    @staticmethod
    def _deduplicate_tool_results(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        for call in calls:
            if call.get("tool_call_id") and call in unique:
                continue
            unique.append(call)
        return unique

    async def _execute_tool_call(
        self,
        call: Any,
        *,
        tool_definitions: Sequence[Any],
        tool_names: frozenset[str],
    ) -> dict[str, Any]:
        name = _tool_name(call)
        if name not in tool_names:
            return {
                "tool": name,
                "success": False,
                "error_code": "crowdworks_tool_not_allowed",
            }
        definition = next(
            (item for item in tool_definitions if _tool_name(item) == name),
            None,
        )
        arguments = (
            call.get("arguments") or call.get("args") or {}
            if isinstance(call, Mapping)
            else {}
        )
        try:
            executor = getattr(definition, "execute_async", None)
            if callable(executor):
                value = executor(**dict(arguments))
            else:
                executor = getattr(definition, "execute", None)
                if callable(executor):
                    value = executor(**dict(arguments))
                else:
                    function = getattr(definition, "function", definition)
                    value = function(**dict(arguments))
            if inspect.isawaitable(value):
                value = await value
            success = _result_success(value)
            return {
                "tool": name,
                "arguments": _safe_result(arguments),
                "result": _safe_result(value),
                "success": success,
                **(
                    {"error_code": _text(value.get("error_code") or value.get("reason_code"))}
                    if isinstance(value, Mapping)
                    and (value.get("error_code") or value.get("reason_code"))
                    else {}
                ),
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {
                "tool": name,
                "arguments": _safe_result(arguments),
                "success": False,
                "error_code": "crowdworks_tool_failed",
                "error_message": _text(exc, limit=240),
            }

    async def _normalize_provider_result(
        self,
        value: Any,
        *,
        client: Any | None = None,
        tool_definitions: Sequence[Any] | None = None,
        tool_names: frozenset[str] | None = None,
    ) -> ExecutionOutcome:
        definitions = tuple(tool_definitions or self.tool_definitions)
        allowed_names = frozenset(tool_names or self.tool_names)
        provider_text = value if isinstance(value, str) else ""
        if isinstance(value, ExecutionOutcome):
            if value.normalized_classification() in {"succeeded", "awaiting_approval"}:
                evidence = value.result if isinstance(value.result, Mapping) else {}
                provider_text = next(
                    (
                        evidence[key]
                        for key in ("response", "text")
                        if isinstance(evidence.get(key), str)
                    ),
                    provider_text,
                )
                calls = evidence.get("tool_calls") or evidence.get("tool_results")
                if not calls:
                    result = {"completion_evidence": "text_without_tool_result"}
                    diagnostics = self._native_tool_diagnostics_for_result(
                        client,
                        allowed_names=allowed_names,
                        provider_text=(
                            evidence.get("response")
                            if isinstance(evidence.get("response"), str)
                            else provider_text
                        ),
                        normalized_tool_result_count=0,
                        normalization_reason_code="crowdworks_scan_result_missing",
                    )
                    if diagnostics is not None:
                        result["native_tool_diagnostics"] = diagnostics
                    return ExecutionOutcome(
                        classification="permanent",
                        error_code="crowdworks_scan_result_missing",
                        error_message="The Agent returned no verified CrowdWorks tool result",
                        result=result,
                    )
                if isinstance(calls, Mapping):
                    calls = (calls,)
                normalized_calls = [
                    self._record_to_tool_result(
                        call,
                        tool_names=allowed_names,
                    )
                    for call in calls
                ]
                if any(item is None for item in normalized_calls):
                    return self._blocked(
                        "crowdworks_scan_result_missing",
                        "The Agent returned no recognizable CrowdWorks tool result",
                    )
                safe_calls = self._deduplicate_tool_results(
                    [item for item in normalized_calls if item is not None]
                )
                failed = [item for item in safe_calls if item.get("success") is not True]
                if failed:
                    diagnostics = self._native_tool_diagnostics_for_result(
                        client,
                        allowed_names=allowed_names,
                        provider_text=provider_text,
                        normalized_tool_result_count=len(safe_calls),
                        normalization_reason_code="tool_failure",
                    )
                    failure_result = {
                        "tool_results": safe_calls,
                        "completion_evidence": "tool_failure",
                    }
                    if diagnostics is not None:
                        failure_result["native_tool_diagnostics"] = diagnostics
                    return ExecutionOutcome(
                        classification="permanent",
                        error_code=_text(
                            failed[0].get("error_code") or "crowdworks_tool_failed"
                        ),
                        error_message="A CrowdWorks Agent tool did not produce a valid result",
                        result=failure_result,
                    )
                if not any(item["tool"] in _SCAN_TOOLS for item in safe_calls):
                    return self._blocked(
                        "crowdworks_scan_result_missing",
                        "The Agent returned no required CrowdWorks scan tool result",
                    )
                payload = dict(evidence)
                payload["tool_results"] = safe_calls
                payload["completion_evidence"] = "verified_crowdworks_tool_result"
                diagnostics = self._native_tool_diagnostics_for_result(
                    client,
                    allowed_names=allowed_names,
                    provider_text=(
                        evidence.get("response")
                        if isinstance(evidence.get("response"), str)
                        else provider_text
                    ),
                    normalized_tool_result_count=len(safe_calls),
                    normalization_reason_code=None,
                )
                if diagnostics is not None:
                    payload["native_tool_diagnostics"] = diagnostics
                proposed = any(
                    _text(
                        item.get("result", {}).get("status")
                        if isinstance(item.get("result"), Mapping)
                        else ""
                    ).casefold()
                    == "proposed"
                    or bool(
                        item.get("result", {}).get("accepted")
                        if isinstance(item.get("result"), Mapping)
                        else False
                    )
                    for item in safe_calls
                )
                try:
                    return replace(
                        value,
                        classification=(
                            "succeeded"
                            if proposed
                            else value.normalized_classification()
                        ),
                        success=True if proposed else value.success,
                        domain_status=(
                            "proposed" if proposed else value.domain_status
                        ),
                        result=payload,
                    )
                except (TypeError, ValueError):
                    return ExecutionOutcome(
                        classification=(
                            "succeeded"
                            if proposed
                            else value.classification
                        ),
                        success=True if proposed else value.success,
                        result_summary=value.result_summary,
                        error_code=value.error_code,
                        error_message=value.error_message,
                        domain_ref=value.domain_ref,
                        domain_status=(
                            "proposed" if proposed else value.domain_status
                        ),
                        result=payload,
                        evidence=value.evidence,
                        usage=value.usage,
                        retry_after_seconds=value.retry_after_seconds,
                        metadata=value.metadata,
                    )
            return value
        provider_object = value
        if not isinstance(value, Mapping):
            # A real provider commonly returns assistant text and keeps the
            # executed tool receipts on ``client``.  Defer the text-only
            # outcome until those receipts have been inspected below.
            value = {}
        status = _text(value.get("status") or value.get("classification")).casefold()
        if status in {"cancelled", "canceled", "cancel", "interrupted"}:
            return ExecutionOutcome(
                classification="cancelled",
                error_code="cancelled",
                error_message="The CrowdWorks Agent scan was cancelled",
                result=_safe_result(value),
            )
        terminal = self._provider_terminal(value)
        if terminal is None and client is not None:
            terminal = self._provider_terminal(
                getattr(client, "_last_provider_terminal_evidence", None)
            )
        provider_failure = (
            getattr(client, "_last_provider_failure", None)
            if client is not None
            else None
        )
        if provider_failure not in (None, "", False, {}):
            failure = self._provider_failure(
                provider_failure
                if isinstance(provider_failure, BaseException)
                else RuntimeError(_text(provider_failure, limit=240))
            )
            return failure
        terminal_state = _text(terminal.get("state") if terminal else "").casefold()
        if terminal_state in {"cancelled", "canceled", "cancel"}:
            return ExecutionOutcome(
                classification="cancelled",
                error_code="cancelled",
                error_message="The CrowdWorks Agent scan was cancelled",
                result={"completion_evidence": terminal_state},
            )
        if terminal_state in {
            "interrupted",
            "incomplete",
            "protocol_error",
            "continuation",
        }:
            return ExecutionOutcome(
                classification="uncertain",
                error_code="crowdworks_provider_interrupted",
                error_message="The provider did not complete the CrowdWorks scan turn",
                result={"completion_evidence": terminal_state, "provider_terminal": _safe_result(terminal)},
            )
        if status in {"failed", "error", "failure", "transient"} or value.get("error_code"):
            return ExecutionOutcome(
                classification="transient" if status != "permanent" else "permanent",
                error_code=_text(value.get("error_code") or "crowdworks_provider_failed"),
                error_message=_text(value.get("error_message") or value.get("error") or "provider failure", limit=240),
                result=_safe_result(value),
            )

        raw_calls: list[Any] = []
        for key in ("tool_calls", "tool_results", "calls", "tools"):
            candidate = value.get(key)
            if isinstance(candidate, Mapping):
                raw_calls.append(candidate)
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
                raw_calls.extend(candidate)
        direct_call = value.get("tool_call")
        if direct_call is not None:
            raw_calls.append(direct_call)
        if value.get("tool") or value.get("tool_name"):
            raw_calls.append(value)
        if provider_object is not value:
            for key in ("tool_calls", "tool_results", "calls", "tools"):
                candidate = getattr(provider_object, key, None)
                if isinstance(candidate, Sequence) and not isinstance(
                    candidate, (str, bytes, bytearray)
                ):
                    raw_calls.extend(candidate)

        normalized_calls: list[dict[str, Any]] = []
        for call in raw_calls:
            name = _tool_name(call)
            if not name:
                continue
            if name not in allowed_names:
                return self._blocked(
                    "crowdworks_tool_not_allowed",
                    "The provider requested a tool outside the CrowdWorks Agent group",
                    tool=name,
                )
            supplied_result = (
                _read(call, "result")
                or _read(call, "output")
                or _read(call, "model_output")
                or _read(call, "tool_result")
            )
            if supplied_result is None and isinstance(call, Mapping):
                normalized_calls.append(
                    await self._execute_tool_call(
                        call,
                        tool_definitions=definitions,
                        tool_names=allowed_names,
                    )
                )
            else:
                normalized = self._record_to_tool_result(
                    call,
                    tool_names=allowed_names,
                )
                if normalized is None:
                    return self._blocked(
                        "crowdworks_scan_result_missing",
                        "The Agent returned no recognizable CrowdWorks tool result",
                    )
                normalized_calls.append(normalized)
        if not normalized_calls:
            # Real provider clients return assistant text while retaining the
            # executed tool receipts on the request-local client.  Read those
            # receipts after generation; the text itself is never success
            # evidence.
            for key in (
                "_crowdworks_request_tool_records",
                "_last_turn_tool_records",
                "_last_tool_calls",
                "_last_audit_tool_calls",
            ):
                records = getattr(client, key, None) if client is not None else None
                if isinstance(records, Sequence) and not isinstance(records, (str, bytes, bytearray)):
                    raw_calls.extend(records)
            for call in raw_calls:
                normalized = self._record_to_tool_result(
                    call,
                    tool_names=allowed_names,
                )
                if normalized is None:
                    continue
                if normalized["tool"] not in allowed_names:
                    return self._blocked(
                        "crowdworks_tool_not_allowed",
                        "The provider executed a tool outside the CrowdWorks Agent group",
                        tool=normalized["tool"],
                    )
                normalized_calls.append(normalized)
        normalized_calls = self._deduplicate_tool_results(normalized_calls)
        if not normalized_calls:
            diagnostics = self._native_tool_diagnostics_for_result(
                client,
                allowed_names=allowed_names,
                provider_text=provider_text,
                normalized_tool_result_count=0,
                normalization_reason_code="crowdworks_scan_result_missing",
            )
            result = {"completion_evidence": "text_without_tool_result"}
            if diagnostics is not None:
                result["native_tool_diagnostics"] = diagnostics
            return ExecutionOutcome(
                classification="permanent",
                error_code="crowdworks_scan_result_missing",
                error_message="The Agent returned text without a verified CrowdWorks tool result",
                result=result,
            )
        failed = [item for item in normalized_calls if item.get("success") is not True]
        if failed:
            diagnostics = self._native_tool_diagnostics_for_result(
                client,
                allowed_names=allowed_names,
                provider_text=provider_text,
                normalized_tool_result_count=len(normalized_calls),
                normalization_reason_code="tool_failure",
            )
            result = {
                "tool_results": normalized_calls,
                "completion_evidence": "tool_failure",
            }
            if diagnostics is not None:
                result["native_tool_diagnostics"] = diagnostics
            return ExecutionOutcome(
                classification="permanent",
                error_code=_text(failed[0].get("error_code") or "crowdworks_tool_failed"),
                error_message="A CrowdWorks Agent tool did not produce a valid result",
                result=result,
            )
        if not any(item["tool"] in _SCAN_TOOLS for item in normalized_calls):
            return self._blocked(
                "crowdworks_scan_result_missing",
                "The Agent returned no required CrowdWorks scan tool result",
            )
        payload: dict[str, Any] = {
            "tool_results": normalized_calls,
            "completion_evidence": "verified_crowdworks_tool_result",
        }
        response_value = value.get("response") or value.get("text") or provider_text
        if response_value:
            payload["response"] = _text(response_value, limit=4000)
        if not provider_text and isinstance(response_value, str):
            provider_text = response_value
        diagnostics = self._native_tool_diagnostics_for_result(
            client,
            allowed_names=allowed_names,
            provider_text=provider_text,
            normalized_tool_result_count=len(normalized_calls),
            normalization_reason_code=None,
        )
        if diagnostics is not None:
            payload["native_tool_diagnostics"] = diagnostics
        proposed = any(
            _text(item.get("result", {}).get("status") if isinstance(item.get("result"), Mapping) else "").casefold()
            == "proposed"
            or bool(
                item.get("result", {}).get("accepted")
                if isinstance(item.get("result"), Mapping)
                else False
            )
            for item in normalized_calls
        )
        return ExecutionOutcome(
            classification="succeeded",
            result_summary=_text(
                value.get("summary")
                or value.get("response")
                or value.get("text")
                or provider_text,
                limit=500,
            )
            or "CrowdWorks Agent scan completed with verified tool evidence",
            result=payload,
            domain_status="proposed" if proposed else "scanned",
        )

    async def _run_provider(
        self,
        client: Any,
        instruction: str,
        *,
        tool_definitions: Sequence[Any],
        tool_names: frozenset[str],
    ) -> Any:
        runner = self._provider_runner
        if runner is not None:
            value = self._call_with_supported_kwargs(
                runner,
                client,
                instruction,
                tool_definitions=tuple(tool_definitions),
                tool_names=tuple(sorted(tool_names)),
            )
        else:
            method = getattr(client, "generate_response_async", None)
            if not callable(method):
                method = getattr(client, "generate_async", None)
            if not callable(method):
                raise TypeError("CrowdWorks Agent provider has no async generation method")
            value = self._call_with_supported_kwargs(
                method,
                instruction,
                stream_callback=None,
            )
        return await value if inspect.isawaitable(value) else value

    async def execute(
        self,
        claim: Any,
        *,
        session: Any,
        run_id: str | None,
        run: Mapping[str, Any] | Any | None,
        coordinator: Any,
        **_: Any,
    ) -> ExecutionOutcome:
        if session is None:
            return self._blocked(
                "crowdworks_runtime_context_missing",
                "CrowdWorks Agent scan requires the Coordinator DB session",
            )
        loaded = await self._load_context(
            session=session,
            claim=claim,
            run_id=run_id,
            run_input=run,
        )
        if isinstance(loaded, ExecutionOutcome):
            return loaded
        work, persisted_run, connection, _policy, policy_revision, session_id, owner_id, evidence = loaded
        config = self.config if self.config is not None else getattr(coordinator, "config", None)
        try:
            execution = await resolve_chat_agent_execution(
                session_id=session_id,
                run=persisted_run,
                config=config,
                db_session=_ResolverSessionProxy(session),
                run_id=str(persisted_run.id),
            )
        except asyncio.CancelledError:
            return ExecutionOutcome(classification="cancelled", error_code="cancelled")
        except Exception as exc:
            return self._blocked(
                "crowdworks_agent_binding_unavailable",
                _text(exc, limit=240) or "The canonical Agent binding could not be resolved",
            )
        if execution is None:
            return self._blocked(
                "crowdworks_agent_binding_unavailable",
                "The canonical Agent binding could not be resolved",
            )
        binding = getattr(execution, "binding", None)
        if (
            binding is None
            or not _same_uuid(getattr(binding, "binding_id", None), evidence["binding"].get("binding_id"))
            or _text(getattr(binding, "snapshot_hash", None)) != _text(evidence["binding"].get("snapshot_hash"))
            or not _same_uuid(getattr(binding, "session_id", None), session_id)
            or not _same_uuid(getattr(binding, "agent_id", None), getattr(work, "assigned_agent_id", None))
            or not _same_uuid(getattr(binding, "agent_revision_id", None), getattr(work, "agent_revision_id", None))
        ):
            return self._blocked(
                "crowdworks_agent_binding_mismatch",
                "The resolved Agent binding does not match persisted Run evidence",
            )

        effective_tool_names = self._effective_tool_names(execution)
        if not effective_tool_names:
            return self._blocked(
                "crowdworks_tool_scope_empty",
                "The pinned AgentRevision does not grant a CrowdWorks tool",
            )
        effective_definitions = tuple(
            definition
            for definition in self.tool_definitions
            if _tool_name(definition) in effective_tool_names
        )
        if not effective_definitions:
            return self._blocked(
                "crowdworks_tool_scope_empty",
                "The pinned AgentRevision does not grant a CrowdWorks tool",
            )
        scoped_execution = self._scope_execution(execution, effective_tool_names)

        source_client = self.source_client or SimpleNamespace(
            config=config,
            current_session_id=session_id,
            session_user_id=owner_id,
            current_project_id=evidence["project_id"],
            current_include_project_context=False,
            external_persistence_enabled=False,
        )
        try:
            workspace_token = bind_agent_dialogue_workspace(None)
            try:
                client = self._call_with_supported_kwargs(
                    self._client_builder,
                    source_client,
                    config,
                    scoped_execution,
                    session_id=session_id,
                    project_id=evidence["project_id"],
                    run_id=str(persisted_run.id),
                )
                if inspect.isawaitable(client):
                    client = await client
                self._install_tools(
                    client,
                    tool_definitions=effective_definitions,
                    tool_names=effective_tool_names,
                )
            finally:
                reset_agent_dialogue_workspace(workspace_token)
        except asyncio.CancelledError:
            return ExecutionOutcome(classification="cancelled", error_code="cancelled")
        except Exception as exc:
            outcome = self._provider_failure(exc)
            self._log_scan_failure(claim, outcome)
            return outcome

        constraints = getattr(policy_revision, "constraints_json", None)
        policy_payload = _safe_result(constraints)
        instruction = (
            "CrowdWorksの対象Policyと接続情報に従って、案件の一覧取得・候補確認・必要な詳細確認を実施してください。"
            "公開案件を調査し、応募提案を作成する場合は専用の提案toolが返すproposed状態までに留めてください。"
            "テキストだけで完了と判断せず、許可されたCrowdWorks専用toolの構造化結果を返してください。"
            "workspace、ファイル、コマンド、ブラウザなどの外部toolは使用しないでください。\n"
            f"policy_id={evidence['policy_id']} policy_revision_id={evidence['policy_revision_id']} "
            f"connection_id={evidence['connection_id']} owner_user_id={owner_id} "
            f"provider_key={getattr(connection, 'provider_key', '')} "
            f"auth_status={getattr(connection, 'auth_status', '')} "
            f"project_id={evidence['project_id']}\n"
            f"policy_constraints={json.dumps(policy_payload, ensure_ascii=False, sort_keys=True)}"
        )
        from ...llm.tool_exposure import (
            bind_suppressed_native_tool_allowlist,
            reset_strict_tool_allowlist,
            reset_suppressed_native_tool_allowlist,
            set_strict_tool_allowlist,
        )
        from ..agent_run_service import reset_current_agent_run_id, set_current_agent_run_id

        turn_token = set_turn_context(
            user_id=owner_id,
            session_id=session_id,
            project_id=evidence["project_id"],
            include_project_context=False,
            suppress_automatic_context=True,
            agent_id=str(getattr(work, "assigned_agent_id", "")),
            agent_revision_id=str(getattr(work, "agent_revision_id", "")),
        )
        run_token = set_current_agent_run_id(str(persisted_run.id))
        allow_token = set_strict_tool_allowlist(effective_tool_names)
        native_tool_scope_token = bind_suppressed_native_tool_allowlist(
            effective_tool_names
        )
        try:
            workspace_token = bind_agent_dialogue_workspace(None)
            try:
                binder_context = bind_chat_agent_execution(
                    scoped_execution,
                    user_id=owner_id,
                    session_id=session_id,
                    project_id=evidence["project_id"],
                    config=config,
                )
                async with binder_context:
                    from ..agent_run_service import AgentRunService

                    native_capture = self._capture_native_request_tool_records(
                        client,
                        tool_names=effective_tool_names,
                        run_id=str(persisted_run.id),
                        run_service=AgentRunService(
                            getattr(coordinator, "_db_manager", None), config=config
                        ),
                    )
                    try:
                        raw = await self._run_provider(
                            client,
                            instruction,
                            tool_definitions=effective_definitions,
                            tool_names=effective_tool_names,
                        )
                    finally:
                        self._restore_native_request_tool_record_capture(
                            native_capture
                        )
                    outcome = await self._normalize_provider_result(
                        raw,
                        client=client,
                        tool_definitions=effective_definitions,
                        tool_names=effective_tool_names,
                    )
                    diagnostic = getattr(client, "_crowdworks_native_tool_diagnostics", {})
                    if (
                        outcome.normalized_classification() in {"succeeded", "awaiting_approval"}
                        and diagnostic.get("capture_hook_installed")
                        and not any(
                            _tool_name(record) in _SCAN_TOOLS
                            and _read(record, "executed") is not False
                            and _read(record, "synthetic") is not True
                            and self._record_success(record, _read(record, "result"))
                            for record in getattr(client, "_crowdworks_request_tool_records", ())
                        )
                    ):
                        outcome = self._blocked(
                            "crowdworks_scan_result_missing",
                            "The Agent returned no persisted CrowdWorks scan receipt",
                        )
            finally:
                reset_agent_dialogue_workspace(workspace_token)
        except asyncio.CancelledError:
            return ExecutionOutcome(
                classification="cancelled",
                error_code="cancelled",
                error_message="The CrowdWorks Agent scan was cancelled",
            )
        except Exception as exc:
            outcome = self._provider_failure(exc)
            self._log_scan_failure(claim, outcome)
            return outcome
        finally:
            reset_suppressed_native_tool_allowlist(native_tool_scope_token)
            reset_strict_tool_allowlist(allow_token)
            reset_current_agent_run_id(run_token)
            reset_turn_context(turn_token)

        self._log_scan_failure(claim, outcome)
        return outcome


__all__ = ["CrowdWorksAgentScanWorkSource", "CrowdWorksAgentScanExecutionAdapter"]
