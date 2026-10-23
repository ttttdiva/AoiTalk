"""Owner-invoked personal adapter controller.

This path deliberately does not create an AgentRun/WorkItem or generate text.
It executes one existing, bounded personal adapter call and returns its
structured result directly to the authenticated owner. No model, embedding,
trace, notification, or external-provider request is made here.
"""

from __future__ import annotations

import inspect
import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from ..memory.models.conversations import AgentExecutionBinding, ConversationSession
from .agent_authority import AgentAuthorityResolver, AuthorityDecision
from .agent_model_routing import (
    AgentModelRoute,
    AgentModelRouteError,
    AgentRoutingPins,
    resolve_agent_subagent_route,
)
from .conversation_agent_service import (
    AgentExecutionBindingSnapshot,
    ConversationAgentError,
    ConversationAgentService,
)
from .outbound_privacy_service import (
    OutboundPrivacyGateway,
    PrivacyError,
    provider_classification,
)
from .personal_agent_runtime import (
    CAREER_CONTEXT_OPERATION,
    FINANCE_READ_ENTRY_OPERATION,
    FINANCE_SUMMARY_OPERATION,
    PersonalAgentRuntime,
)
from .personal_agent_tools import (
    PERSONAL_AGENT_PROFILES,
    PersonalAgentTools,
    bind_personal_agent_local_route,
)
from .personal_draft_service import PersonalDraftService
from .personal_finance_dataset import PersonalFinanceDatasetService
from .personal_finance_service import SqlPersonalFinanceRepository
from .personal_ledger_service import (
    LedgerPermissionError,
    PersonalLedgerService,
)
from .turn_context import get_turn_context, reset_turn_context, set_turn_context

PERSONAL_ADAPTER_SUBAGENT_ID = "personal_private_adapter"

_CAPABILITY_BY_OPERATION = {
    "deadline.state": ("personal-firearms-admin", "personal_deadline_read"),
    "deadline.propose": ("personal-firearms-admin", "personal_deadline_draft"),
    "inventory.state": ("personal-ammunition-bookkeeper", "personal_inventory_read"),
    "inventory.propose": ("personal-ammunition-bookkeeper", "personal_inventory_draft"),
    "finance.read_entry": ("personal-finance-reviewer", "personal_finance_read"),
    "finance.summary": ("personal-finance-reviewer", "personal_finance_read"),
    "career.context": ("personal-career-planner", "personal_career_context"),
}
_ALLOWED_ARGUMENTS = {
    "deadline.state": frozenset({"subject_key"}),
    "inventory.state": frozenset({"subject_key"}),
    "deadline.propose": frozenset({"evidence_ref", "proposal", "idempotency_key"}),
    "inventory.propose": frozenset({"evidence_ref", "proposal", "idempotency_key"}),
    "finance.read_entry": frozenset({"row_ref"}),
    "finance.summary": frozenset({"accepted_dataset_ref", "month"}),
    "career.context": frozenset({"accepted_profile_ref"}),
}
_FORBIDDEN_IDENTITY_ARGUMENTS = frozenset(
    {
        "user_id",
        "owner",
        "owner_id",
        "owner_user_id",
        "session_id",
        "profile_id",
        "actor",
        "actor_user_id",
        "actor_agent_id",
        "route",
        "provider",
        "base_url",
        "gateway",
    }
)
_LOCAL_MODEL_PROVIDERS = frozenset(
    {"openai_compatible_local", "ollama", "sglang"}
)


class PersonalAgentControllerError(RuntimeError):
    """Safe bounded controller error for the owner-only HTTP boundary."""

    def __init__(self, code: str, status_code: int = 403) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PersonalAgentExecution:
    """Trusted, exact Agent/revision/binding/operator route for one adapter call."""

    owner_id: str
    session_id: str
    profile_id: str
    required_capability: str
    agent_id: str
    agent_revision_id: str
    binding_id: str
    revision_version: int
    binding_snapshot: AgentExecutionBindingSnapshot
    route: AgentModelRoute
    base_url: str
    authority: AuthorityDecision

    def fingerprint(self) -> tuple[Any, ...]:
        return (
            self.owner_id,
            self.session_id,
            self.profile_id,
            self.required_capability,
            self.agent_id,
            self.agent_revision_id,
            self.binding_id,
            self.revision_version,
            self.binding_snapshot.snapshot_hash,
            self.route.agent_id,
            self.route.agent_revision_id,
            self.route.agent_team_id,
            self.route.execution_profile_id,
            self.route.acting_subagent_id,
            self.route.role,
            self.route.provider,
            self.route.model,
            self.route.backend,
            self.route.route_source,
            self.base_url,
            self.authority.allowed,
            self.authority.requested_capability,
            tuple(sorted(self.authority.capabilities)),
            self.authority.deny_reasons,
        )


class PersonalAgentExecutionResolver:
    """Resolve the current private session, exact Agent binding, route and authority."""

    def __init__(self, db_manager: Any, *, config: Any = None) -> None:
        self.db_manager = db_manager
        self.config = config

    async def _session(self):
        value = self.db_manager.get_session()
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _verify_private_participants(row: Any, owner_id: UUID) -> None:
        canonical_character = str(getattr(row, "character_name", "") or "").strip().casefold()
        for participant in getattr(row, "participants", ()) or ():
            participant_type = str(getattr(participant, "participant_type", ""))
            participant_id = str(getattr(participant, "participant_id", ""))
            if participant_type == "user" and participant_id == str(owner_id):
                continue
            # Character is presentation metadata only. It is accepted solely
            # for the session's one canonical Character; Agent identity and
            # authority are always derived from ConversationSession.agent_id.
            if (
                participant_type == "character"
                and str(getattr(participant, "status", "")) == "joined"
                and participant_id.strip().casefold() == canonical_character
            ):
                continue
            raise PersonalAgentControllerError("personal_session_shared", 403)

    @staticmethod
    def _uuid(value: Any, code: str) -> UUID:
        try:
            return value if isinstance(value, UUID) else UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            raise PersonalAgentControllerError(code, 422) from None

    async def _private_session(self, owner_id: UUID, session_id: UUID) -> dict[str, Any]:
        session = await self._session()
        try:
            result = await session.execute(
                select(ConversationSession)
                .options(selectinload(ConversationSession.participants))
                .where(ConversationSession.id == session_id)
            )
            row = result.scalar_one_or_none()
            if row is None or row.deleted_at is not None:
                raise PersonalAgentControllerError("personal_session_unavailable", 404)
            try:
                row_owner = UUID(str(row.user_id))
            except (TypeError, ValueError):
                raise PersonalAgentControllerError("personal_session_unavailable", 404) from None
            if row_owner != owner_id:
                raise PersonalAgentControllerError("personal_session_unavailable", 404)
            if (
                row.is_active is False
                or bool(row.is_group_chat)
                or row.project_id is not None
                or row.app_id is not None
                or row.app_target_id is not None
                or row.parent_session_id is not None
            ):
                raise PersonalAgentControllerError("personal_session_scope_denied", 403)
            self._verify_private_participants(row, owner_id)
            if row.agent_id is None:
                raise PersonalAgentControllerError("personal_agent_not_selected", 409)
            return {
                "owner_id": str(owner_id),
                "session_id": str(session_id),
                "agent_id": str(row.agent_id),
            }
        finally:
            await session.close()

    async def _latest_binding(self, session_id: UUID, agent_id: UUID) -> AgentExecutionBinding:
        session = await self._session()
        try:
            row = (
                await session.execute(
                    select(AgentExecutionBinding)
                    .where(
                        AgentExecutionBinding.session_id == session_id,
                        AgentExecutionBinding.agent_id == agent_id,
                    )
                    .order_by(desc(AgentExecutionBinding.created_at), desc(AgentExecutionBinding.id))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                raise PersonalAgentControllerError("personal_agent_binding_missing", 409)
            return row
        finally:
            await session.close()

    async def resolve(
        self,
        *,
        owner_id: str,
        session_id: str,
        profile_id: str,
        required_capability: str,
    ) -> PersonalAgentExecution:
        owner = self._uuid(owner_id, "invalid_owner")
        sid = self._uuid(session_id, "invalid_session")
        profile_ids = {profile.id for profile in PERSONAL_AGENT_PROFILES}
        if profile_id not in profile_ids:
            raise PersonalAgentControllerError("personal_profile_unavailable", 422)
        current = await self._private_session(owner, sid)
        agent_id = self._uuid(current["agent_id"], "personal_agent_not_selected")

        agent_service = ConversationAgentService(self.db_manager, config=self.config)
        try:
            agent, revision = await agent_service.validate_agent_selection(
                agent_id=agent_id,
                actor_user_id=owner,
                actor_role=None,
                project_id=None,
            )
        except ConversationAgentError as exc:
            raise PersonalAgentControllerError("personal_agent_unavailable", 403) from exc
        if str(agent.id) != current["agent_id"] or str(agent.state) != "active":
            raise PersonalAgentControllerError("personal_agent_unavailable", 403)

        binding_row = await self._latest_binding(sid, agent_id)
        if str(binding_row.agent_revision_id) != str(revision.id):
            raise PersonalAgentControllerError("personal_agent_binding_stale", 409)
        try:
            binding = await agent_service.resolve_execution_binding(
                session_id=sid,
                agent_id=agent_id,
                agent_revision_id=revision.id,
                binding_id=binding_row.id,
                allow_legacy=False,
            )
        except ConversationAgentError as exc:
            raise PersonalAgentControllerError("personal_agent_binding_unavailable", 409) from exc
        if (
            binding.session_id != str(sid)
            or binding.agent_id != str(agent_id)
            or binding.agent_revision_id != str(revision.id)
            or binding.binding_id != str(binding_row.id)
        ):
            raise PersonalAgentControllerError("personal_agent_binding_changed", 409)

        pins = AgentRoutingPins(
            agent_id=binding.agent_id,
            agent_revision_id=binding.agent_revision_id,
            agent_team_id=binding.agent_team_id,
            execution_profile_id=binding.execution_profile_id,
            allowed_subagent_ids=binding.allowed_subagent_ids,
            route_snapshot=binding.route_snapshot,
        )
        try:
            route = resolve_agent_subagent_route(
                self.config,
                pins,
                PERSONAL_ADAPTER_SUBAGENT_ID,
                role="operator",
            )
        except AgentModelRouteError as exc:
            raise PersonalAgentControllerError("personal_operator_route_unavailable", 403) from exc
        if (
            route.acting_subagent_id != PERSONAL_ADAPTER_SUBAGENT_ID
            or route.role != "operator"
            or route.agent_id != binding.agent_id
            or route.agent_revision_id != binding.agent_revision_id
            or route.backend != "api"
        ):
            raise PersonalAgentControllerError("personal_operator_route_invalid", 403)
        endpoint = _resolve_local_provider_endpoint(self.config, route)

        authority = await AgentAuthorityResolver(self.db_manager, config=self.config).resolve(
            agent_id=agent_id,
            revision_id=revision.id,
            required_capability=required_capability,
            tool_capabilities=(required_capability,),
            harness_capabilities=(required_capability,),
        )
        if (
            not authority.allowed
            or authority.requested_capability != required_capability
            or required_capability not in authority.capabilities
            or authority.agent_id != str(agent_id)
            or authority.agent_revision_id != str(revision.id)
            or authority.project_id is not None
            or authority.space_id is not None
        ):
            raise PersonalAgentControllerError("personal_agent_authority_denied", 403)

        # Re-read session ownership and selection after the DB/authority awaits.
        latest = await self._private_session(owner, sid)
        if latest != current:
            raise PersonalAgentControllerError("personal_session_binding_changed", 409)
        return PersonalAgentExecution(
            owner_id=str(owner),
            session_id=str(sid),
            profile_id=profile_id,
            required_capability=required_capability,
            agent_id=str(agent_id),
            agent_revision_id=str(revision.id),
            binding_id=str(binding_row.id),
            revision_version=int(binding.revision_version),
            binding_snapshot=binding,
            route=route,
            base_url=endpoint,
            authority=authority,
        )


def _config_value(config: Any, key: str) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key)
        except (KeyError, TypeError, AttributeError):
            value = None
        if value not in (None, ""):
            return value
    current = config
    for part in key.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _resolve_local_provider_endpoint(config: Any, route: AgentModelRoute) -> str:
    """Resolve only the endpoint belonging to the exact pinned local provider."""

    provider = str(route.provider or "").strip().lower()
    if provider not in _LOCAL_MODEL_PROVIDERS or route.backend != "api":
        raise PersonalAgentControllerError("personal_route_not_local", 403)
    try:
        if provider == "openai_compatible_local":
            from ..llm.openai_compatible_local_profiles import openai_compatible_local_base_url

            endpoint = openai_compatible_local_base_url(config, model=route.model)
        elif provider == "sglang":
            from ..llm.sglang_url import resolve_sglang_base_url

            endpoint = resolve_sglang_base_url(config)
        else:
            from ..llm.ollama_engine import DEFAULT_OLLAMA_BASE_URL

            nested = _config_value(config, "ollama")
            endpoint = str(
                _config_value(config, "runtime.target_base_url")
                or os.getenv("OLLAMA_BASE_URL")
                or _config_value(config, "ollama_base_url")
                or (nested.get("base_url") if isinstance(nested, Mapping) else None)
                or DEFAULT_OLLAMA_BASE_URL
            ).strip()
    except Exception:
        raise PersonalAgentControllerError("personal_route_endpoint_unknown", 403) from None
    endpoint = str(endpoint or "").strip()
    try:
        parsed = urlsplit(endpoint)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        loopback = hostname in {"localhost", "localhost.localdomain"}
        if hostname and not loopback:
            loopback = ipaddress.ip_address(hostname).is_loopback
        if (
            parsed.scheme not in {"http", "https"}
            or not loopback
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise PersonalAgentControllerError("personal_route_not_loopback", 403) from None
    if provider_classification(provider, base_url=endpoint) != "local":
        raise PersonalAgentControllerError("personal_route_not_loopback", 403)
    return endpoint


def _personal_career_source_resolver(session_factory, owner_id: UUID):
    """Fresh, owner-only source resolver for accepted career context checks."""
    from .search_sources import SearchScope, SearchSourceResolver

    session = session_factory()
    if inspect.isawaitable(session):
        async def make_resolver():
            resolved_session = await session
            return SearchSourceResolver(
                resolved_session,
                SearchScope(
                    user_id=owner_id,
                    project_id=None,
                    project_scope_restricted=True,
                ),
            )

        return make_resolver()
    return SearchSourceResolver(
        session,
        SearchScope(
            user_id=owner_id,
            project_id=None,
            project_scope_restricted=True,
        ),
    )


class PersonalAgentController:
    """Server-owned explicit profile selection and adapter-only invocation."""

    def __init__(
        self,
        *,
        execution_resolver: Any,
        config: Any,
        ledger_tools: PersonalAgentTools,
        domain_runtime: PersonalAgentRuntime,
    ) -> None:
        self.execution_resolver = execution_resolver
        self.config = config
        self.ledger_tools = ledger_tools
        self.domain_runtime = domain_runtime

    @classmethod
    def from_server(cls, server: Any) -> "PersonalAgentController":
        db_manager = getattr(server, "_db_manager", None)
        if db_manager is None:
            raise RuntimeError("personal agent database is unavailable")
        config = getattr(server, "config", None)

        def session_factory():
            return db_manager.get_session()

        ledger = PersonalLedgerService(session_factory)
        drafts = PersonalDraftService(ledger)
        ledger_tools = PersonalAgentTools(ledger, drafts, enabled=True)

        runtime = PersonalAgentRuntime(enabled=True)
        from .personal_agent_runtime import career_context_handler, finance_read_handler
        from .personal_career_service import PersonalCareerService

        finance_repo = SqlPersonalFinanceRepository(session_factory)
        finance_datasets = PersonalFinanceDatasetService(session_factory)

        def no_attachment_resolution(_owner: UUID):
            raise LedgerPermissionError("finance attachment operation is unavailable")

        runtime.bind_domain_handler(
            "finance",
            finance_read_handler(finance_repo, no_attachment_resolution, finance_datasets),
        )
        runtime.bind_domain_handler(
            "career",
            career_context_handler(
                PersonalCareerService.from_session_factory(session_factory),
                resolver_factory=lambda owner_id: _personal_career_source_resolver(
                    session_factory, owner_id
                ),
            ),
        )
        return cls(
            execution_resolver=PersonalAgentExecutionResolver(db_manager, config=config),
            config=config,
            ledger_tools=ledger_tools,
            domain_runtime=runtime,
        )

    @staticmethod
    def list_profiles() -> list[dict[str, Any]]:
        operations: dict[str, list[str]] = {
            "personal-firearms-admin": ["deadline.state", "deadline.propose"],
            "personal-ammunition-bookkeeper": ["inventory.state", "inventory.propose"],
            "personal-finance-reviewer": ["finance.read_entry", "finance.summary"],
            "personal-career-planner": ["career.context"],
        }
        return [
            {
                "id": profile.id,
                "priority": profile.priority,
                "kinds": list(profile.kinds),
                "operations": operations[profile.id],
                "registration_state": profile.registration_state,
                "autostart": False,
                "execution_kind": "adapter_only",
            }
            for profile in PERSONAL_AGENT_PROFILES
        ]

    @staticmethod
    def _validate_arguments(operation: str, arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict) or len(arguments) > 16:
            raise PersonalAgentControllerError("personal_arguments_invalid", 422)
        if _FORBIDDEN_IDENTITY_ARGUMENTS & arguments.keys():
            raise PersonalAgentControllerError("personal_identity_arguments_forbidden", 422)
        if set(arguments) - _ALLOWED_ARGUMENTS[operation]:
            raise PersonalAgentControllerError("personal_operation_arguments_invalid", 422)
        if operation.endswith(".state"):
            subject = arguments.get("subject_key")
            if subject is not None and (
                not isinstance(subject, str)
                or not subject
                or len(subject) > 64
                or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in subject)
            ):
                raise PersonalAgentControllerError("personal_subject_key_invalid", 422)
        if operation.endswith(".propose"):
            proposal = arguments.get("proposal")
            expected_kind = "deadline" if operation.startswith("deadline") else "inventory"
            if not isinstance(proposal, dict) or proposal.get("kind") != expected_kind:
                raise PersonalAgentControllerError("personal_proposal_kind_mismatch", 422)
            for key in ("evidence_ref", "idempotency_key"):
                try:
                    UUID(str(arguments[key]))
                except (KeyError, TypeError, ValueError):
                    raise PersonalAgentControllerError("personal_proposal_reference_invalid", 422) from None
        if operation == "finance.read_entry":
            try:
                UUID(str(arguments["row_ref"]))
            except (KeyError, TypeError, ValueError):
                raise PersonalAgentControllerError("personal_finance_reference_invalid", 422) from None
        if operation == "finance.summary":
            try:
                UUID(str(arguments["accepted_dataset_ref"]))
            except (KeyError, TypeError, ValueError):
                raise PersonalAgentControllerError("personal_finance_reference_invalid", 422) from None
            month = arguments.get("month")
            if not isinstance(month, str) or len(month) != 7 or month[4] != "-" or not month[:4].isdigit() or not month[5:].isdigit():
                raise PersonalAgentControllerError("personal_finance_month_invalid", 422)
        if operation == "career.context":
            try:
                UUID(str(arguments["accepted_profile_ref"]))
            except (KeyError, TypeError, ValueError):
                raise PersonalAgentControllerError("personal_career_reference_invalid", 422) from None
        return dict(arguments)

    @staticmethod
    def _assert_execution_matches(
        initial: PersonalAgentExecution,
        current: PersonalAgentExecution,
    ) -> None:
        if initial.fingerprint() != current.fingerprint():
            raise LedgerPermissionError("personal Agent execution binding changed")
        if (
            not current.authority.allowed
            or current.required_capability not in current.authority.capabilities
            or current.route.acting_subagent_id != PERSONAL_ADAPTER_SUBAGENT_ID
            or current.base_url != initial.base_url
        ):
            raise LedgerPermissionError("current personal Agent authority denied")

    async def invoke(
        self,
        *,
        owner_id: str,
        session_id: str,
        profile_id: str,
        operation: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        expected = _CAPABILITY_BY_OPERATION.get(operation)
        if expected is None:
            raise PersonalAgentControllerError("personal_operation_unavailable", 422)
        expected_profile, capability = expected
        if profile_id != expected_profile:
            raise PersonalAgentControllerError("personal_profile_operation_mismatch", 403)
        normalized_arguments = self._validate_arguments(operation, arguments)

        initial = await self.execution_resolver.resolve(
            owner_id=owner_id,
            session_id=session_id,
            profile_id=profile_id,
            required_capability=capability,
        )
        if (
            initial.owner_id != str(UUID(owner_id))
            or initial.session_id != str(UUID(session_id))
            or initial.profile_id != profile_id
            or initial.required_capability != capability
            or initial.binding_snapshot.agent_revision_id != initial.agent_revision_id
            or initial.binding_snapshot.agent_id != initial.agent_id
            or initial.binding_snapshot.binding_id != initial.binding_id
            or initial.route.acting_subagent_id != PERSONAL_ADAPTER_SUBAGENT_ID
            or initial.route.role != "operator"
            or initial.authority.allowed is not True
            or capability not in initial.authority.capabilities
            or initial.authority.project_id is not None
            or initial.authority.space_id is not None
        ):
            raise PersonalAgentControllerError("personal_agent_execution_invalid", 403)

        gateway = OutboundPrivacyGateway(
            self.config,
            user_id=initial.owner_id,
            session_id=initial.session_id,
            session_context={"privacy_mode": "local_only"},
            project_metadata=None,
        )
        if gateway.mode != "local_only" or not gateway.user_id or not gateway.session_id:
            raise PersonalAgentControllerError("personal_local_only_required", 403)

        turn_token = set_turn_context(
            user_id=initial.owner_id,
            session_id=initial.session_id,
            project_id=None,
            include_project_context=False,
            suppress_automatic_context=True,
            agent_id=initial.agent_id,
            agent_revision_id=initial.agent_revision_id,
            agent_execution_binding=initial.binding_snapshot.to_dict(include_prose=False),
            agent_role="personal_private_adapter",
        )
        turn = get_turn_context()

        async def current_authority_guard():
            if get_turn_context() is not turn:
                raise LedgerPermissionError("personal turn changed")
            if gateway.mode != "local_only" or gateway.user_id != turn.user_id or gateway.session_id != turn.session_id:
                raise LedgerPermissionError("personal local-only policy changed")
            try:
                if gateway.ensure_provider_allowed(initial.route.provider, base_url=initial.base_url) != "local":
                    raise LedgerPermissionError("personal route is no longer local")
            except PrivacyError:
                raise LedgerPermissionError("personal route is no longer local") from None
            current = await self.execution_resolver.resolve(
                owner_id=initial.owner_id,
                session_id=initial.session_id,
                profile_id=initial.profile_id,
                required_capability=initial.required_capability,
            )
            self._assert_execution_matches(initial, current)
            return True

        try:
            async with _async_personal_route(
                profile_id=profile_id,
                gateway=gateway,
                provider=initial.route.provider,
                base_url=initial.base_url,
                allow_external_fallback=False,
                current_authority_guard=current_authority_guard,
            ):
                await current_authority_guard()
                result = await self._dispatch(profile_id, operation, normalized_arguments)
                await current_authority_guard()
                return {
                    "execution_kind": "adapter_only",
                    "generative_response": False,
                    "profile_id": profile_id,
                    "operation": operation,
                    "agent_id": initial.agent_id,
                    "agent_revision_id": initial.agent_revision_id,
                    "binding_id": initial.binding_id,
                    "result": result,
                }
        finally:
            reset_turn_context(turn_token)

    async def _dispatch(self, profile_id: str, operation: str, arguments: dict[str, Any]) -> Any:
        if operation in {"deadline.state", "inventory.state"}:
            ledger = "deadline" if operation.startswith("deadline") else "inventory"
            return await self.ledger_tools.ledger_state(ledger=ledger, **arguments)
        if operation in {"deadline.propose", "inventory.propose"}:
            return await self.ledger_tools.propose(**arguments)
        if operation == "finance.read_entry":
            return await self.domain_runtime.dispatch("finance", FINANCE_READ_ENTRY_OPERATION, **arguments)
        if operation == "finance.summary":
            return await self.domain_runtime.dispatch("finance", FINANCE_SUMMARY_OPERATION, **arguments)
        if operation == "career.context":
            return await self.domain_runtime.dispatch("career", CAREER_CONTEXT_OPERATION, **arguments)
        raise PersonalAgentControllerError("personal_operation_unavailable", 422)


@dataclass
class _AsyncRoute:
    """Small adapter around the existing synchronous binding context manager."""

    context: Any

    async def __aenter__(self):
        return self.context.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        return self.context.__exit__(exc_type, exc, tb)


def _async_personal_route(**fields: Any) -> _AsyncRoute:
    return _AsyncRoute(bind_personal_agent_local_route(**fields))


def build_personal_agent_controller(server: Any) -> PersonalAgentController:
    """Composition-root factory for the signed-cookie owner route."""

    return PersonalAgentController.from_server(server)


__all__ = [
    "PERSONAL_ADAPTER_SUBAGENT_ID",
    "PersonalAgentController",
    "PersonalAgentControllerError",
    "PersonalAgentExecution",
    "PersonalAgentExecutionResolver",
    "build_personal_agent_controller",
]
