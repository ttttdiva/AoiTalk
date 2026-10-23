"""Owner-invoked, read-only finance/career adapters for personal agents.

The server-owned signed-cookie controller binds the current private
ConversationSession, exact selected AgentRevision/AgentExecutionBinding, a
current-authority guard, and a session-bound local-only route before dispatch.
This dispatcher itself remains outside the model-tool registry: personal
profiles are still ``disabled_design_only`` with autostart false. Calls execute
existing bounded adapters only, with no LLM request, AgentRun/WorkItem, model
input, trace, notification, or generative response. Source/profile acceptance,
confirmation, financial writes, and all other human-only operations are not in
the domain allowlist.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import select

from ..memory.models.workspace_files import WorkspaceFileEntry
from .personal_career_service import PersonalCareerService, model_actor
from .personal_finance_attachment_source import FinanceAttachmentSourceError, resolve_finance_attachment_source
from .personal_finance_contracts import FinanceSource
from .personal_finance_summary import (
    FinanceSummaryIntegrityError,
    FinanceSummaryOverflowError,
    FinanceSummaryTrustError,
    summarize_finance_snapshot,
)

from .personal_async_guard import run_personal_guard
from .personal_agent_tools import PERSONAL_AGENT_PROFILES, _binding, _check_local
from .personal_ledger_service import (
    LedgerConflictError,
    LedgerPermissionError,
    LedgerValidationError,
)
from .search_sources import SourceRef, SourceUnavailable
from .turn_context import TurnContext, get_turn_context

DOMAIN_PROFILES = {"finance": "personal-finance-reviewer", "career": "personal-career-planner"}
_KIND_BY_DOMAIN = {"finance": "finance", "career": "career"}

Handler = Callable[..., Awaitable[Any]]
_IDENTITY_FIELDS = frozenset({"user_id", "owner", "owner_id", "owner_user_id", "session_id",
                              "profile_id", "actor", "route"})


class PersonalDependencyPending(RuntimeError):
    """The owning lane's module has not been integrated yet."""


class PersonalFinanceSummaryIntegrityError(LedgerConflictError):
    """Safe conflict raised when an accepted summary has conflicting rows."""

    code = "finance_summary_integrity"


class PersonalFinanceSummaryOverflowError(LedgerValidationError):
    """Safe validation error raised when a summary exceeds its numeric bound."""

    code = "finance_summary_overflow"


def _check_trusted(domain: str) -> TurnContext:
    """Current turn/profile/local-only policy; raises on any violation."""
    binding = _binding.get()
    turn = get_turn_context()
    if binding is None or binding.turn is not turn or type(turn) is not TurnContext:
        raise LedgerPermissionError("trusted local turn binding required")
    if (domain not in DOMAIN_PROFILES or binding.profile.id != DOMAIN_PROFILES[domain]
            or _KIND_BY_DOMAIN[domain] not in binding.profile.kinds):
        raise LedgerPermissionError("personal profile scope denied")
    _check_local(binding)
    return turn


def _original_guard(domain: str) -> Callable[[], Awaitable[None]]:
    """Guard exact turn, current Agent authority, and local route after each await."""
    turn, started = _check_trusted(domain), _binding.get()

    async def guard() -> None:
        result = await run_personal_guard(started.current_authority_guard)
        if result is False:
            raise LedgerPermissionError("current Agent authority denied")
        if _check_trusted(domain) is not turn or _binding.get() is not started:
            raise LedgerPermissionError("turn binding changed during dispatch")

    return guard


class PersonalAgentRuntime:
    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled
        self._handlers: dict[str, Handler] = {}

    def bind_domain_handler(self, domain: str, handler: Handler) -> None:
        if domain not in DOMAIN_PROFILES:
            raise ValueError("unknown personal domain")
        self._handlers[domain] = handler

    def _authorize(self, domain: str) -> TurnContext:
        if self.enabled is not True:
            raise LedgerPermissionError("personal agent capability disabled")
        return _check_trusted(domain)

    async def _authorize_current(self, domain: str) -> TurnContext:
        binding = _binding.get()
        turn = self._authorize(domain)
        result = await run_personal_guard(binding.current_authority_guard)
        if result is False:
            raise LedgerPermissionError("current Agent authority denied")
        if _binding.get() is not binding or self._authorize(domain) is not turn:
            raise LedgerPermissionError("turn binding changed during authority check")
        return turn

    async def dispatch(self, domain: str, operation: str, **payload: Any) -> Any:
        """Owner/session come only from the bound TurnContext, never the payload."""
        if _IDENTITY_FIELDS & payload.keys():
            raise LedgerPermissionError("identity fields are not accepted from callers")
        if domain == "career" and operation not in CAREER_ALLOWED_OPERATIONS:
            raise LedgerPermissionError("career operation not allowed")
        if domain == "finance" and operation not in FINANCE_ALLOWED_OPERATIONS:
            raise LedgerPermissionError("finance operation not allowed")
        turn = await self._authorize_current(domain)
        handler = self._handlers.get(domain)
        if handler is None:
            raise PersonalDependencyPending(f"{domain} dependency not integrated")
        started = _binding.get()
        result = await handler(owner_id=turn.user_id, session_id=turn.session_id,
                               operation=operation, **payload)
        # Policy may change during the await, and the turn/binding must be the very
        # ones the handler started under (a swapped valid binding is another owner/session).
        if await self._authorize_current(domain) is not turn or _binding.get() is not started:
            raise LedgerPermissionError("turn binding changed during dispatch")
        return result


CAREER_CONTEXT_OPERATION = "context"
# Fixed read-only allowlist. write/accept/verify (register_source, set_source_selected,
# create_profile_version, confirm_profile_version, ...) are human actions and not dispatchable.
CAREER_ALLOWED_OPERATIONS = frozenset({CAREER_CONTEXT_OPERATION})


def career_context_handler(
    service: PersonalCareerService,
    resolver_factory: Callable[[UUID], Any] | None = None,
) -> Handler:
    """Real handler only when a service built on an explicit SQL factory is injected.

    Requires accepted_profile_ref only; default bounds only (callers cannot widen them); returns the service contract
    unchanged. Owner comes from the bound TurnContext as a model-kind actor. Every accepted version source is
    rechecked through an owner-only resolver before and after context construction.
    """
    if not isinstance(service, PersonalCareerService):
        raise TypeError("PersonalCareerService required")

    async def handler(*, owner_id, session_id, operation, **payload):
        if operation not in CAREER_ALLOWED_OPERATIONS:
            raise LedgerPermissionError("career operation not allowed")
        if set(payload) != {"accepted_profile_ref"}:
            raise LedgerPermissionError("career context takes only accepted_profile_ref")
        ref = payload["accepted_profile_ref"]
        try:
            if not isinstance(ref, str):
                raise ValueError
            parsed = UUID(ref)
        except ValueError:
            raise LedgerPermissionError("valid accepted_profile_ref required") from None
        # Bound to the turn/binding active at dispatch start; runs right after every DB await,
        # before the stored payload is decrypted.
        guard = _original_guard("career")
        actor = model_actor(owner_id)
        version = await service.get_profile_version(actor, parsed, guard=guard)
        await run_personal_guard(guard)

        async def verify_sources() -> None:
            if not version.confirmed or not version.source_refs:
                return
            if resolver_factory is None:
                raise PersonalDependencyPending("career source resolver not integrated")

            selected = await service.list_selected_sources(actor, limit=64)
            await run_personal_guard(guard)
            selected_by_ref = {source.source_ref: source for source in selected}
            if any(source_ref not in selected_by_ref for source_ref in version.source_refs):
                raise LedgerPermissionError("career source selection changed")

            resolver = resolver_factory(UUID(str(owner_id)))
            if inspect.isawaitable(resolver):
                resolver = await resolver
                await run_personal_guard(guard)
            try:
                for source_ref in version.source_refs:
                    registered = selected_by_ref[source_ref]
                    try:
                        resolved = await resolver.resolve(
                            SourceRef("workspace_file", str(source_ref)), open_file=False
                        )
                    except SourceUnavailable:
                        await run_personal_guard(guard)
                        raise LedgerPermissionError("career source unavailable") from None
                    await run_personal_guard(guard)
                    metadata = resolved.metadata if isinstance(resolved.metadata, dict) else {}
                    if (
                        metadata.get("files_scope") != "user"
                        or metadata.get("files_owner_id") != str(owner_id)
                        or metadata.get("content_hash") != registered.source_sha256
                        or metadata.get("mime_type") != registered.mime_type
                    ):
                        raise LedgerPermissionError("career source changed")

                    result = await resolver.session.execute(
                        select(WorkspaceFileEntry.size_bytes).where(
                            WorkspaceFileEntry.id == source_ref,
                            WorkspaceFileEntry.scope == "user",
                            WorkspaceFileEntry.owner_id == UUID(str(owner_id)),
                            WorkspaceFileEntry.status == "indexable",
                            WorkspaceFileEntry.deleted_at.is_(None),
                        )
                    )
                    await run_personal_guard(guard)
                    byte_size = result.scalar_one_or_none()
                    if byte_size is None or int(byte_size) != registered.byte_size:
                        raise LedgerPermissionError("career source changed")
            finally:
                session = getattr(resolver, "session", None)
                close = getattr(session, "close", None)
                if callable(close):
                    closed = close()
                    if inspect.isawaitable(closed):
                        await closed

        await verify_sources()
        context = await service.build_context(
            actor, accepted_profile_ref=parsed, guard=guard)
        await run_personal_guard(guard)
        await verify_sources()
        return context.to_contract()

    return handler


FINANCE_READ_ENTRY_OPERATION = "read_entry"
FINANCE_RESOLVE_ATTACHMENT_OPERATION = "resolve_attachment"
# "summary" needs an injected owner-checked accepted-dataset loader (PersonalFinanceDatasetService)
# and stays dependency-pending without one. append/accept/confirm/write/update/delete are human
# actions and are refused before any handler runs.
FINANCE_SUMMARY_OPERATION = "summary"
FINANCE_ALLOWED_OPERATIONS = frozenset({
    FINANCE_READ_ENTRY_OPERATION, FINANCE_RESOLVE_ATTACHMENT_OPERATION, FINANCE_SUMMARY_OPERATION,
})


def finance_read_handler(
    repository: Any, resolver_factory: Callable[[UUID], Any], dataset_loader: Any = None
) -> Handler:
    """Read-only finance handler over an explicitly injected repository and resolver factory.

    ``read_entry`` and ``resolve_attachment`` take exactly ``row_ref``. Rows are fetched with the
    trusted owner filter; the attachment descriptor comes only from the persisted, owner-matched
    entry. ``resolver_factory(owner_uuid)`` must return an owner-scoped SearchSourceResolver.
    ``summary`` takes exactly ``accepted_dataset_ref`` and ``month`` and needs ``dataset_loader``
    (an object with ``load_snapshot(owner_user_id, dataset_ref, guard)``).
    """
    if not callable(getattr(repository, "get_by_row_ref", None)):
        raise TypeError("finance repository with get_by_row_ref required")
    if not callable(resolver_factory):
        raise TypeError("resolver_factory required")

    async def load(owner_id: Any, payload: dict, guard: Callable[[], Any]) -> tuple[UUID, dict]:
        if set(payload) != {"row_ref"}:
            raise LedgerPermissionError("finance read takes only row_ref")
        try:
            if not isinstance(payload["row_ref"], str):
                raise ValueError
            row_ref = UUID(payload["row_ref"])
            owner = UUID(str(owner_id))
        except ValueError:
            raise LedgerPermissionError("valid row_ref required") from None
        # The repository runs guard() right after its DB await, before decrypting the payload.
        record = await repository.get_by_row_ref(owner_user_id=owner, row_ref=row_ref, guard=guard)
        await run_personal_guard(guard)
        if record is None or record.get("owner_user_id") != owner or record.get("row_ref") != row_ref:
            raise LedgerPermissionError("finance entry not available")
        return owner, dict(record)

    async def summary(owner_id: Any, payload: dict) -> dict:
        if set(payload) != {"accepted_dataset_ref", "month"}:
            raise LedgerPermissionError("finance summary takes only accepted_dataset_ref and month")
        try:
            if not isinstance(payload["accepted_dataset_ref"], str):
                raise ValueError
            dataset_ref = UUID(payload["accepted_dataset_ref"])
            owner = UUID(str(owner_id))
        except ValueError:
            raise LedgerPermissionError("valid accepted_dataset_ref required") from None
        guard = _original_guard("finance")
        try:
            snapshot = await dataset_loader.load_snapshot(
                owner_user_id=owner, dataset_ref=dataset_ref, guard=guard)
            await run_personal_guard(guard)
            return summarize_finance_snapshot(
                snapshot, owner_user_id=owner, accepted_dataset_ref=dataset_ref,
                month=payload["month"])
        except FinanceSummaryTrustError:
            raise LedgerPermissionError("accepted dataset not available") from None
        except FinanceSummaryIntegrityError:
            # Do not retain or expose row refs or source exception text at the
            # agent/API boundary.  The stable code is sufficient for the owner
            # UI to offer a refresh/review path.
            raise PersonalFinanceSummaryIntegrityError() from None
        except FinanceSummaryOverflowError:
            # The summary contract is bounded; never round or return arithmetic
            # details from an internal exception.
            raise PersonalFinanceSummaryOverflowError() from None

    async def handler(*, owner_id, session_id, operation, **payload):
        if operation == FINANCE_SUMMARY_OPERATION:
            if dataset_loader is None:
                raise PersonalDependencyPending("finance summary dataset adapter not integrated")
            return await summary(owner_id, payload)
        if operation not in FINANCE_ALLOWED_OPERATIONS:
            raise LedgerPermissionError("finance operation not allowed")
        guard = _original_guard("finance")
        owner, record = await load(owner_id, payload, guard)
        await run_personal_guard(guard)
        row = (record.get("payload") or {}).get("row") or {}
        source = (record.get("payload") or {}).get("source")
        if operation == FINANCE_READ_ENTRY_OPERATION:
            return {"row_ref": str(record["row_ref"]), "occurred_on": row.get("occurred_on"),
                    "kind": row.get("kind"), "amount_minor": row.get("amount_minor"),
                    "currency": row.get("currency"), "account_alias": row.get("account_alias"),
                    "category": row.get("category"),
                    "source": None if source is None else {
                        k: (str(v) if k == "source_ref" else v) for k, v in dict(source).items()},
                    "content_digest": record["content_digest"],
                    "confirmed_by_kind": record["confirmed_by_kind"]}
        if source is None:
            raise LedgerPermissionError("finance entry has no attachment")
        descriptor = FinanceSource.from_mapping(source)
        await run_personal_guard(guard)
        resolver = resolver_factory(owner)
        if inspect.isawaitable(resolver):
            resolver = await resolver
            await run_personal_guard(guard)
        try:
            evidence = await resolve_finance_attachment_source(
                resolver, owner_user_id=owner, source_ref=descriptor.source_ref,
                expected_sha256=descriptor.source_sha256, guard=guard)
        except FinanceAttachmentSourceError as exc:
            raise LedgerPermissionError(f"attachment unavailable: {exc.code}") from None
        finally:
            await run_personal_guard(guard)
        if evidence.mime_type != descriptor.mime_type or evidence.byte_size != descriptor.byte_size:
            raise LedgerPermissionError("attachment does not match confirmed descriptor")
        return {"source_ref": str(evidence.source_ref), "source_sha256": evidence.source_sha256,
                "mime_type": evidence.mime_type, "byte_size": evidence.byte_size,
                "content": evidence.content}

    return handler


assert {p.id for p in PERSONAL_AGENT_PROFILES} >= set(DOMAIN_PROFILES.values())
