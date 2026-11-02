"""Create one bounded personal deadline draft from the current chat turn.

The adapter has no request arguments.  Its source, owner, session, Agent
binding, and local route all come from the already authenticated turn.  The
only durable operation is the encrypted evidence/proposed pair owned by
``PersonalDraftService``; no ledger entry, notification, or completion action
is performed here.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from datetime import date
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import exists, func, or_, select

from ..memory.models.conversations import (
    ConversationMessage,
    ConversationParticipant,
    ConversationSession,
)
from .personal_agent_tools import PersonalAgentTools, _binding
from .personal_async_guard import run_personal_guard
from .personal_ledger_service import (
    LedgerConflictError,
    LedgerPermissionError,
    LedgerValidationError,
)
from .turn_context import TurnContext, get_turn_context


class PersonalDeadlineChatDraftError(LedgerValidationError):
    """Stable, API-safe validation result for the bounded chat adapter."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class PersonalDeadlineChatDraftPermissionError(LedgerPermissionError):
    """Stable, API-safe authorization result for the bounded chat adapter."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


_ISO_DATE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])"
)
_DATE_SHAPE_RE = re.compile(
    r"(?<![0-9])(?:[0-9]{4}[-/][0-9]{1,2}[-/][0-9]{1,2}|[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)"
)
_DEADLINE_CUE_RE = re.compile(
    r"(?:\bdeadline\b|\bdue\b|\bexpires?\b|\bexpiration\b|\bexpiry\b|"
    r"\brenew(?:al)?\b|\bvalid\s+(?:until|through)\b|\bby\b|"
    r"期限|締切|締め切り|期日|有効期限|更新期限|提出期限|納期|まで)",
    re.IGNORECASE,
)
_RELATIVE_OR_ARITHMETIC_RE = re.compile(
    r"(?:"
    r"\b(?:today|tomorrow|yesterday|next|last|within|after|before|every|annual(?:ly)?|yearly|monthly|weekly)\b"
    r"|\b(?:in)\s+\d+\s*(?:day|days|week|weeks|month|months|year|years)\b"
    r"|\b\d+\s*(?:day|days|week|weeks|month|months|year|years)\s*"
    r"(?:after|before|later|earlier|from)\b"
    r"|\b(?:day|days|week|weeks|month|months|year|years)\s+later\b"
    r"|[+\u2212-]\s*\d+\s*(?:day|days|week|weeks|month|months|year|years)\b"
    r"|\b\d+\s*(?:day|days|week|weeks|month|months|year|years)\b"
    r"|(?:今日|明日|あした|昨日|来週|来月|次回|以内|後|前|毎年|毎月|日後|週間後|ヶ月後|か月後|から)"
    r")",
    re.IGNORECASE,
)
_CONTRADICTORY_RE = re.compile(
    r"(?:\b(?:not|no|without|unless|cancel(?:led)?|cancell?ation|instead|either|or|"
    r"maybe|perhaps|possibly|probably|(?:may|might|could)\s+be|"
    r"tentative|unconfirmed|not\s+sure)\b"
    r"|取消|キャンセル|延期|保留|不要|未定|未確定|未確認|確認待ち|"
    r"違う|違います|ではなくて|ではなく|じゃなく|ではない|ではありません|"
    r"かも(?:しれない|しれません|知れない|知れません)?|"
    r"可能性(?:が)?(?:ある|あります|ございます))",
    re.IGNORECASE,
)
_PERSONAL_DEADLINE_IDEMPOTENCY_NAMESPACE = UUID("8e9ccfbb-1d8b-4f88-ae28-2f9847b0bb4a")


def _chat_idempotency_key(turn: TurnContext) -> UUID:
    """Derive an opaque retry key without copying a source UUID to SQL."""

    identity = "\x00".join(
        (
            str(turn.user_id).strip(),
            str(turn.session_id).strip(),
            str(turn.message_id).strip(),
        )
    )
    return uuid5(_PERSONAL_DEADLINE_IDEMPOTENCY_NAMESPACE, identity)


def _uuid(value: Any, code: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise PersonalDeadlineChatDraftPermissionError(code) from None


def _parse_deadline_source(source_text: Any) -> tuple[str, int, int]:
    if not isinstance(source_text, str) or not source_text.strip():
        raise PersonalDeadlineChatDraftError("personal_deadline_source_empty")
    if len(source_text) > 16_384:
        raise PersonalDeadlineChatDraftError("personal_deadline_source_too_large")

    matches = list(_ISO_DATE_RE.finditer(source_text))
    malformed = list(_DATE_SHAPE_RE.finditer(source_text))
    if len(matches) == 0:
        if malformed:
            raise PersonalDeadlineChatDraftError("personal_deadline_date_invalid")
        raise PersonalDeadlineChatDraftError("personal_deadline_date_missing")
    if len(matches) != 1:
        raise PersonalDeadlineChatDraftError("personal_deadline_date_multiple")
    match = matches[0]
    value = match.group("date")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise PersonalDeadlineChatDraftError("personal_deadline_date_invalid") from None

    # Reject alternate date spellings anywhere in the same message.  They
    # would otherwise make the one-date evidence ambiguous.
    if any(item.group(0) != value for item in malformed):
        raise PersonalDeadlineChatDraftError("personal_deadline_date_invalid")
    if _RELATIVE_OR_ARITHMETIC_RE.search(source_text):
        raise PersonalDeadlineChatDraftError("personal_deadline_date_arithmetic_or_relative")
    if _CONTRADICTORY_RE.search(source_text):
        raise PersonalDeadlineChatDraftError("personal_deadline_date_contradictory")

    cues = list(_DEADLINE_CUE_RE.finditer(source_text))
    if not cues:
        raise PersonalDeadlineChatDraftError("personal_deadline_cue_missing")
    if not any(abs(cue.start() - match.end()) <= 96 or abs(match.start() - cue.end()) <= 96 for cue in cues):
        raise PersonalDeadlineChatDraftError("personal_deadline_cue_unbound")
    return value, match.start(), match.end()


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class PersonalDeadlineChatDraftAdapter:
    """Current-turn-only adapter for one explicit deadline candidate."""

    def __init__(self, personal_tools: PersonalAgentTools) -> None:
        self.personal_tools = personal_tools

    async def _caller_proof(self, turn: TurnContext) -> Any:
        # Import lazily to keep the chat execution adapter independent from
        # this bounded personal source adapter at module import time.
        try:
            from .chat_execution_adapter import (
                PersonalDeadlineCallerRouteProof,
                get_personal_deadline_caller_route_proof,
            )
        except (ImportError, AttributeError):
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_missing"
            ) from None
        proof = get_personal_deadline_caller_route_proof(turn)
        proof = await _await_if_needed(proof)
        if proof is None:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_missing"
            )
        if type(proof) is not PersonalDeadlineCallerRouteProof:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_invalid"
            )
        return proof

    @staticmethod
    def _validate_proof(turn: TurnContext, local: Any, proof: Any) -> tuple[str, str]:
        try:
            from .chat_execution_adapter import PersonalDeadlineCallerRouteProof
            from .conversation_agent_service import AgentExecutionBindingSnapshot
        except ImportError:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_missing"
            ) from None
        if type(proof) is not PersonalDeadlineCallerRouteProof:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_invalid"
            )
        try:
            from .chat_execution_adapter import get_personal_deadline_caller_route_proof

            if get_personal_deadline_caller_route_proof(turn) is not proof:
                raise PersonalDeadlineChatDraftPermissionError(
                    "personal_deadline_caller_proof_stale"
                )
        except PersonalDeadlineChatDraftPermissionError:
            raise
        except Exception:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_invalid"
            ) from None
        turn_binding = turn.agent_execution_binding
        if type(turn_binding) is not AgentExecutionBindingSnapshot:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_agent_binding_invalid"
            )
        if get_turn_context() is not turn:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_turn_binding_changed"
            )
        try:
            live_binding = proof._binding_ref() if proof._binding_ref is not None else None
            live_client = proof._client_ref() if proof._client_ref is not None else None
        except Exception:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_invalid"
            )
        if live_binding is not turn_binding:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_stale"
            )
        if live_client is None or id(live_client) != proof.client_object_id:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_reused"
            )
        binding = _binding.get()
        if binding is not local or getattr(local, "turn", None) is not turn:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_turn_binding_changed"
            )
        if getattr(live_client, "_agent_execution_binding", None) is not turn_binding:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_reused"
            )
        local_profile = getattr(local, "profile", None)
        if getattr(local_profile, "id", None) != "personal-firearms-admin":
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_profile_denied"
            )
        if (
            proof.owner_user_id != str(turn.user_id).strip()
            or proof.session_id != str(turn.session_id).strip()
            or proof.message_id != str(turn.message_id).strip()
            or proof.agent_id != str(turn.agent_id).strip()
            or proof.agent_revision_id != str(turn.agent_revision_id).strip()
            or proof.agent_role != str(turn.agent_role or "").strip().casefold()
            or proof.binding_id != str(turn_binding.binding_id or "").strip()
            or proof.binding_snapshot_hash != str(turn_binding.snapshot_hash or "").strip()
            or proof.binding_object_id != id(turn_binding)
            or bool(str(turn.context_project_hint_id or "").strip())
        ):
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_stale"
            )
        if getattr(live_client, "_personal_deadline_caller_route_proof", None) is not proof:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_reused"
            )
        route = getattr(live_client, "_agent_route", None)
        if not isinstance(route, Mapping):
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_invalid"
            )
        expected_route = {
            "agent_id": proof.agent_id,
            "agent_revision_id": proof.agent_revision_id,
            "role": proof.agent_role,
            "provider": proof.provider,
            "model": proof.model,
            "effort": proof.route_effort,
            "route_source": proof.route_source,
            "inherits_main": proof.route_inherits_main,
            "backend": proof.route_backend,
        }
        for name, expected in expected_route.items():
            observed = route.get(name)
            if name == "effort":
                if str(observed or "") != str(expected or ""):
                    raise PersonalDeadlineChatDraftPermissionError(
                        "personal_deadline_caller_proof_stale"
                    )
            elif observed != expected:
                raise PersonalDeadlineChatDraftPermissionError(
                    "personal_deadline_caller_proof_stale"
                )
        if tuple(str(item) for item in route.get("notes") or ()) != proof.route_notes:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_stale"
            )
        provider = str(proof.provider or "").strip().casefold()
        base_url = str(proof.base_url or "").strip()
        if not provider or not base_url:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_caller_proof_invalid"
            )
        return provider, base_url

    @staticmethod
    def _validate_local_route(turn: TurnContext, local: Any, provider: str, base_url: str) -> Any:
        try:
            from .outbound_privacy_service import (
                OutboundPrivacyGateway,
                PrivacyError,
                effective_privacy_mode,
            )
        except ImportError:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_local_route_unavailable"
            ) from None
        gateway = getattr(local, "gateway", None)
        if not isinstance(gateway, OutboundPrivacyGateway):
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_local_route_unavailable"
            )
        if gateway.user_id != turn.user_id or gateway.session_id != turn.session_id:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_local_route_mismatch"
            )
        try:
            mode = effective_privacy_mode(
                gateway.config,
                session_context=gateway.session_context,
                project_metadata=gateway.project_metadata,
            )
            classification = gateway.ensure_provider_allowed(provider, base_url=base_url)
        except PrivacyError:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_external_route_denied"
            ) from None
        except Exception:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_local_route_unavailable"
            ) from None
        if gateway.mode != "local_only" or gateway.settings.mode != "local_only" or mode != "local_only" or classification != "local":
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_external_route_denied"
            )
        return gateway

    async def _read_current_message(self, turn: TurnContext, guard: Any) -> str:
        owner = _uuid(turn.user_id, "personal_deadline_owner_invalid")
        session_id = _uuid(turn.session_id, "personal_deadline_session_invalid")
        message_id = _uuid(turn.message_id, "personal_deadline_message_invalid")
        agent_id = _uuid(turn.agent_id, "personal_deadline_agent_invalid")

        ledger = getattr(self.personal_tools, "ledger", None)
        drafts = getattr(self.personal_tools, "drafts", None)
        factory = getattr(ledger, "_session_factory", None)
        if not callable(factory):
            factory = getattr(getattr(drafts, "ledger", None), "_session_factory", None)
        if not callable(factory):
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_source_unavailable"
            )
        session = await _await_if_needed(factory())
        await run_personal_guard(guard)
        try:
            bad_participant = exists(
                select(ConversationParticipant.id).where(
                    ConversationParticipant.session_id == ConversationSession.id,
                    ~or_(
                        (
                            ConversationParticipant.participant_type == "user"
                        )
                        & (ConversationParticipant.participant_id == str(owner)),
                        (
                            ConversationParticipant.participant_type == "character"
                        )
                        & (ConversationParticipant.status == "joined")
                        & (
                            func.lower(func.trim(ConversationParticipant.participant_id))
                            == func.lower(func.trim(ConversationSession.character_name))
                        ),
                    ),
                )
            )
            statement = (
                select(ConversationMessage)
                .join(
                    ConversationSession,
                    ConversationSession.id == ConversationMessage.session_id,
                )
                .where(
                    ConversationMessage.id == message_id,
                    ConversationMessage.session_id == session_id,
                    ConversationMessage.deleted_at.is_(None),
                    ConversationMessage.role == "user",
                    ConversationMessage.sender_type.in_(("user", "human")),
                    ConversationMessage.sender_id == str(owner),
                    ConversationSession.id == session_id,
                    ConversationSession.user_id == str(owner),
                    ConversationSession.deleted_at.is_(None),
                    or_(
                        ConversationSession.is_active.is_(True),
                        ConversationSession.is_active.is_(None),
                    ),
                    or_(
                        ConversationSession.is_group_chat.is_(False),
                        ConversationSession.is_group_chat.is_(None),
                    ),
                    ConversationSession.project_id.is_(None),
                    ConversationSession.app_id.is_(None),
                    ConversationSession.app_target_id.is_(None),
                    ConversationSession.parent_session_id.is_(None),
                    ConversationSession.agent_id == agent_id,
                    ~bad_participant,
                )
                .limit(2)
            )
            result = await session.execute(statement)
            await run_personal_guard(guard)
            rows = result.scalars().all()
            await run_personal_guard(guard)
            if len(rows) != 1:
                raise PersonalDeadlineChatDraftError(
                    "personal_deadline_source_not_found"
                )
            row = rows[0]
            source_text = getattr(row, "content", None)
            await run_personal_guard(guard)
            if not isinstance(source_text, str):
                raise PersonalDeadlineChatDraftError(
                    "personal_deadline_source_invalid"
                )
            return source_text
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                await _await_if_needed(close())

    async def create_current_turn_draft(self) -> dict[str, Any]:
        """Create a candidate from exactly the current persisted user message."""

        if not isinstance(self.personal_tools, PersonalAgentTools):
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_tools_untrusted"
            )
        turn = get_turn_context()
        if type(turn) is not TurnContext:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_turn_required"
            )
        required = (
            turn.user_id,
            turn.session_id,
            turn.message_id,
            turn.agent_id,
            turn.agent_revision_id,
            turn.agent_execution_binding,
        )
        if any(value in (None, "") for value in required) or turn.project_id is not None:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_turn_binding_missing"
            )
        local = _binding.get()
        if local is None or getattr(local, "turn", None) is not turn:
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_local_route_missing"
            )

        actor = await self.personal_tools._authorize_current(
            "private.draft.propose", "deadline"
        )
        pinned_guard = self.personal_tools._pinned_guard(
            "private.draft.propose", "deadline", actor
        )
        pinned_proof = None

        async def guard():
            await run_personal_guard(pinned_guard)
            current_proof = await self._caller_proof(turn)
            if pinned_proof is not None and current_proof is not pinned_proof:
                raise PersonalDeadlineChatDraftPermissionError(
                    "personal_deadline_caller_proof_stale"
                )
            provider, base_url = self._validate_proof(turn, local, current_proof)
            self._validate_local_route(turn, local, provider, base_url)
            return current_proof

        proof = await guard()
        pinned_proof = proof

        source_text = await self._read_current_message(turn, guard)
        due_date, _, _ = _parse_deadline_source(source_text)
        # The proof and local route are checked again after reading and
        # parsing, immediately before the owner transaction can be opened.
        await guard()
        subject_key = f"message_{UUID(str(turn.message_id)).hex}"
        proposal = {
            "kind": "deadline",
            "data": {"subject_key": subject_key, "due_date": due_date},
        }
        drafts = getattr(self.personal_tools, "drafts", None)
        seam = getattr(drafts, "record_agent_evidence_and_propose", None)
        if not callable(seam):
            raise PersonalDeadlineChatDraftPermissionError(
                "personal_deadline_draft_seam_unavailable"
            )
        result = await seam(
            actor=actor,
            proposal=proposal,
            source_text=source_text,
            source_message_id=UUID(str(turn.message_id)),
            source_session_id=UUID(str(turn.session_id)),
            idempotency_key=_chat_idempotency_key(turn),
            source_agent_id=UUID(str(turn.agent_id)),
            guard=guard,
        )
        await guard()
        if not isinstance(result, Mapping) or result.get("status") != "awaiting_user_review" or result.get("effective_ledger_change") is not False:
            raise LedgerConflictError("personal deadline draft result is invalid")
        return dict(result)


__all__ = [
    "PersonalDeadlineChatDraftAdapter",
    "PersonalDeadlineChatDraftError",
    "PersonalDeadlineChatDraftPermissionError",
]
