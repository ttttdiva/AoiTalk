"""Dormant finance/career dispatch for the personal agents.

Only the dependency-independent part lives here: profile -> domain resolution,
trusted TurnContext/local-route re-checks, and explicit dependency slots. No
tool is registered and nothing is autostarted. Acceptance and verification are
authenticated human actions and are never reachable from this dispatcher.

The finance store, career profile and attachment-source modules belong to other
lanes. Until their reviewed signatures land, ``bind_domain_handler`` is the only
entry point and unbound domains raise ``PersonalDependencyPending``.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable
from uuid import UUID

from .personal_career_service import PersonalCareerService, model_actor
from .personal_finance_attachment_source import FinanceAttachmentSourceError, resolve_finance_attachment_source
from .personal_finance_contracts import FinanceSource
from .personal_finance_summary import FinanceSummaryTrustError, summarize_finance_snapshot

from .personal_agent_tools import PERSONAL_AGENT_PROFILES, _binding, _check_local
from .personal_ledger_service import LedgerPermissionError
from .turn_context import TurnContext, get_turn_context

DOMAIN_PROFILES = {"finance": "personal-finance-reviewer", "career": "personal-career-planner"}
_KIND_BY_DOMAIN = {"finance": "finance", "career": "career"}

Handler = Callable[..., Awaitable[Any]]
_IDENTITY_FIELDS = frozenset({"user_id", "owner", "owner_id", "owner_user_id", "session_id",
                              "profile_id", "actor", "route"})


class PersonalDependencyPending(RuntimeError):
    """The owning lane's module has not been integrated yet."""


def _check_trusted(domain: str) -> TurnContext:
    """Current turn/binding/profile/local-only policy; raises on any violation."""
    binding = _binding.get()
    turn = get_turn_context()
    if binding is None or binding.turn is not turn or type(turn) is not TurnContext:
        raise LedgerPermissionError("trusted local turn binding required")
    if (domain not in DOMAIN_PROFILES or binding.profile.id != DOMAIN_PROFILES[domain]
            or _KIND_BY_DOMAIN[domain] not in binding.profile.kinds):
        raise LedgerPermissionError("personal profile scope denied")
    _check_local(binding)
    return turn


def _original_guard(domain: str) -> Callable[[], None]:
    """Guard bound to the turn/binding active now; call it right after every await."""
    turn, started = _check_trusted(domain), _binding.get()

    def guard() -> None:
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

    async def dispatch(self, domain: str, operation: str, **payload: Any) -> Any:
        """Owner/session come only from the bound TurnContext, never the payload."""
        if _IDENTITY_FIELDS & payload.keys():
            raise LedgerPermissionError("identity fields are not accepted from callers")
        if domain == "career" and operation not in CAREER_ALLOWED_OPERATIONS:
            raise LedgerPermissionError("career operation not allowed")
        if domain == "finance" and operation not in FINANCE_ALLOWED_OPERATIONS:
            raise LedgerPermissionError("finance operation not allowed")
        turn = self._authorize(domain)
        handler = self._handlers.get(domain)
        if handler is None:
            raise PersonalDependencyPending(f"{domain} dependency not integrated")
        started = _binding.get()
        result = await handler(owner_id=turn.user_id, session_id=turn.session_id,
                               operation=operation, **payload)
        # Policy may change during the await, and the turn/binding must be the very
        # ones the handler started under (a swapped valid binding is another owner/session).
        if self._authorize(domain) is not turn or _binding.get() is not started:
            raise LedgerPermissionError("turn binding changed during dispatch")
        return result


CAREER_CONTEXT_OPERATION = "context"
# Fixed read-only allowlist. write/accept/verify (register_source, set_source_selected,
# create_profile_version, confirm_profile_version, ...) are human actions and not dispatchable.
CAREER_ALLOWED_OPERATIONS = frozenset({CAREER_CONTEXT_OPERATION})


def career_context_handler(service: PersonalCareerService) -> Handler:
    """Real handler only when a service built on an explicit SQL factory is injected.

    Requires accepted_profile_ref only; default bounds only (callers cannot widen them); returns the service contract
    unchanged. Owner comes from the bound TurnContext as a model-kind actor.
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
        context = await service.build_context(
            model_actor(owner_id), accepted_profile_ref=parsed, guard=guard)
        guard()
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

    async def load(owner_id: Any, payload: dict, guard: Callable[[], None]) -> tuple[UUID, dict]:
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
        guard()
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
            guard()
            return summarize_finance_snapshot(
                snapshot, owner_user_id=owner, accepted_dataset_ref=dataset_ref,
                month=payload["month"])
        except FinanceSummaryTrustError:
            raise LedgerPermissionError("accepted dataset not available") from None

    async def handler(*, owner_id, session_id, operation, **payload):
        if operation == FINANCE_SUMMARY_OPERATION:
            if dataset_loader is None:
                raise PersonalDependencyPending("finance summary dataset adapter not integrated")
            return await summary(owner_id, payload)
        if operation not in FINANCE_ALLOWED_OPERATIONS:
            raise LedgerPermissionError("finance operation not allowed")
        guard = _original_guard("finance")
        owner, record = await load(owner_id, payload, guard)
        guard()
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
        guard()
        resolver = resolver_factory(owner)
        if inspect.isawaitable(resolver):
            resolver = await resolver
        guard()
        try:
            evidence = await resolve_finance_attachment_source(
                resolver, owner_user_id=owner, source_ref=descriptor.source_ref,
                expected_sha256=descriptor.source_sha256, guard=guard)
        except FinanceAttachmentSourceError as exc:
            raise LedgerPermissionError(f"attachment unavailable: {exc.code}") from None
        finally:
            guard()
        if evidence.mime_type != descriptor.mime_type or evidence.byte_size != descriptor.byte_size:
            raise LedgerPermissionError("attachment does not match confirmed descriptor")
        return {"source_ref": str(evidence.source_ref), "source_sha256": evidence.source_sha256,
                "mime_type": evidence.mime_type, "byte_size": evidence.byte_size,
                "content": evidence.content}

    return handler


assert {p.id for p in PERSONAL_AGENT_PROFILES} >= set(DOMAIN_PROFILES.values())
