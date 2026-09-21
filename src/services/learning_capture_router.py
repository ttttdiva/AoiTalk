"""Conservative learning capture/router for canonical authenticated user turns."""

from __future__ import annotations

import logging
import inspect
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select

from ..memory.models import AgentRun, ConversationMessage
from .dreaming_memory_service import (
    _DURABLE_USER_SCOPE_RE as _DREAMING_DURABLE_USER_SCOPE_RE,
    _PROJECT_SPECIFIC_RE as _DREAMING_PROJECT_SPECIFIC_RE,
    _TRANSIENT_TURN_SCOPE_RE as _DREAMING_TRANSIENT_TURN_SCOPE_RE,
)
from .scoped_memory_service import ScopedMemoryService
from .skill_learning_service import (
    SkillLearningError,
    SkillLearningService,
    SkillLearningValidationError,
)
from .privacy_masking_projection import is_privacy_masking_source

logger = logging.getLogger(__name__)

# Keep optional direct WebSocket learning capture bounded so a slow or
# unavailable learning backend cannot hold up the canonical chat response.
# The dispatch boundary uses this shared value for its ``wait_for`` timeout.
DIRECT_WS_LEARNING_CAPTURE_TIMEOUT_SECONDS = 5.0

_DURABLE_RE = re.compile(
    r"(?:今後(?:は|も)?|これから(?:は|も)?|以後(?:は|も)?|常に|毎回|"
    r"次(?:回)?から|次回以降|二度と|"
    r"from\s+now\s+on|going\s+forward|always|every\s+time|never\s+again)",
    re.IGNORECASE,
)
_EXPLICIT_REMEMBER_RE = re.compile(
    r"(?:覚えて(?:おいて|おく|いて)|記憶して(?:おいて|おく)|"
    r"\bremember\s+(?:this|that|to\b)|"
    r"\bkeep\s+(?:this|that)\s+in\s+mind\b|"
    r"\bkeep\s+(?:using|doing|following)\b)",
    re.IGNORECASE,
)
_PROJECT_RE = re.compile(
    r"(?:"
    r"この(?:プロジェクト|案件|リポジトリ|レポジトリ)"
    r"(?:\s*[／/]\s*(?:プロジェクト|案件|リポジトリ|レポジトリ))?"
    r"(?:では|で|において|用)?|"
    r"この\s*(?:project|repository|repo)"
    r"(?:\s*/\s*(?:project|repository|repo))?"
    r"(?:では|で|において|用)?|"
    r"プロジェクト(?:では|用)|案件(?:では|用)|"
    r"for\s+this\s+(?:project|repository|repo)|"
    r"in\s+this\s+(?:project|repository|repo)|"
    r"(?:project|repository|repo)[-\s]specific"
    r")",
    re.IGNORECASE,
)
_TEMPORARY_RE = re.compile(
    r"(?:今回は|今回だけ|この一回だけ|一時的|今日だけ|今だけ|"
    r"just\s+this\s+once|this\s+time\s+only|temporar(?:y|ily)|today\s+only)",
    re.IGNORECASE,
)
_LOCAL_SCOPE_RE = re.compile(
    r"(?:"
    r"この(?:タスク|作業|セッション|会話|チャット|ターン)"
    r"(?:では|で|だけ|中|に限って)?|"
    r"\b(?:for|in)\s+this\s+(?:task|session|conversation|chat|turn)\b|"
    r"\bthis\s+(?:task|session|conversation|chat|turn)\b"
    r")",
    re.IGNORECASE,
)
_CORRECTION_RE = re.compile(
    r"(?:訂正|修正して|間違(?:い|って)|違う|ではなく|"
    r"correction|correct\s+this|wrong|instead\s+of)",
    re.IGNORECASE,
)
_SECURITY_AUTHORITY_RE = re.compile(
    r"(?:権限|アクセス権|許可|認可|無断|承認|認証情報|資格情報|"
    r"password|パスワード|API\s*キー|トークン|秘密鍵|"
    r"ツール(?:利用|アクセス)権|セキュリティ|permission|approval|credential|"
    r"api[-_\s]?key|access[-_\s]?token|secret[-_\s]?key|tool[-_\s]?access|security|"
    r"(?<![A-Za-z0-9_])sudo(?![A-Za-z0-9_])\s*を?\s*"
    r"(?:使(?:って|う|用)|実行(?:して|する))|"
    r"\b(?:use|run|execute)\s+(?:commands?\s+)?(?:with\s+)?sudo\b|"
    r"\b(?:run|execute)\s+as\s+root\b|"
    r"\b(?:grant|give)\s+(?:me\s+)?(?:admin(?:istrator)?|root)\s+"
    r"(?:access|privileges?|permissions?)\b|"
    r"\b(?:elevate|escalate)\s+(?:my\s+)?"
    r"(?:access|privileges?|permissions?)\b)",
    re.IGNORECASE,
)
_SKILL_RE = re.compile(r"(?:スキル|skill)", re.IGNORECASE)
_RECEIPT_RE = re.compile(
    r"(?:receipt|usage[-_\s]?receipt|使用レシート|利用レシート)\s*[:#=]?\s*"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})"
)
_SKILL_CREATE_RE = re.compile(
    r"(?:スキルとして(?:保存|登録|提案)|再利用可能なスキル(?:として)?(?:保存|登録|提案)|"
    r"create\s+(?:a\s+)?skill|save\s+(?:this\s+)?as\s+(?:a\s+)?skill)"
    r"\s*[:：]?\s*[`\"'「『]?([0-9A-Za-z][0-9A-Za-z_.-]{0,127})",
    re.IGNORECASE,
)
_CONSTRAINT_RE = re.compile(
    r"(?:"
    r"禁止|厳禁|"
    r"ないで(?:ください|下さい|ほしい|欲しい|ね)?(?:[。.!！]|$)|"
    r"(?:て|で)は(?:いけない|ならない)|"
    r"必ず.*しない|"
    r"never|must\s+not|do\s+not"
    r")",
    re.IGNORECASE,
)
_PREFERENCE_RE = re.compile(
    r"(?:してほしい|するようにして|好み|prefer|preference|would\s+like)",
    re.IGNORECASE,
)
_GUIDANCE_ACTION_RE = re.compile(
    r"(?:"
    r"使(?:って|う)|使用(?:して|する)|利用(?:して|する)|"
    r"実行(?:して|する|しないで|しない)|"
    r"(?:回答|返答|表示|保存|参照|編集|削除|変更|生成)"
    r"(?:して|する|しないで|しない)|"
    r"答え(?:て|る)|返(?:して|す)|触らないで|触らない|"
    r"(?:確認|チェック)(?:して(?:ください|下さい|ほしい|欲しい|ね)?|"
    r"するようにして|しないで(?:ください|下さい)?)|"
    r"(?:^|[,;:]\s*)(?:please\s+)?check\b|"
    r"書き換えないで|書き換えない|避けて|"
    r"してほしい|して欲しい|するようにして|"
    r"(?:に|く)して(?:ください|下さい|ほしい|欲しい|ね)?(?:[。.!！]|$)|"
    r"\buse\b|\bkeep\s+(?:using|doing|following)\b|\bdo\s+not\b|"
    r"\bmust(?:\s+not)?\b|\bnever\b|\bprefer\b|\bavoid\b|\bwould\s+like\b"
    r")",
    re.IGNORECASE,
)


def _uuid(value: Any) -> Optional[uuid.UUID]:
    if value in (None, ""):
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _memory_type(text: str) -> str:
    if _CONSTRAINT_RE.search(text):
        return "constraint"
    if _PREFERENCE_RE.search(text):
        return "preference"
    return "instruction"


def _has_guidance_intent(text: str) -> bool:
    """Require an action/rule signal before treating scope words as guidance."""
    return bool(_GUIDANCE_ACTION_RE.search(text) or _CONSTRAINT_RE.search(text))


def _has_durable_user_intent(text: str) -> bool:
    """Require explicit standing intent, while retaining Dreaming vocabulary."""
    if _EXPLICIT_REMEMBER_RE.search(text):
        return True
    if not _has_guidance_intent(text):
        return False
    return bool(
        _DURABLE_RE.search(text)
        or _DREAMING_DURABLE_USER_SCOPE_RE.search(text)
    )


def _has_project_scope_intent(text: str) -> bool:
    """Keep deterministic Project/repository scope aligned with Dreaming."""
    return bool(_PROJECT_RE.search(text) or _DREAMING_PROJECT_SPECIFIC_RE.search(text))


def _has_transient_turn_intent(text: str) -> bool:
    """Transient scope always defeats automatic active promotion."""
    return bool(_TEMPORARY_RE.search(text) or _DREAMING_TRANSIENT_TURN_SCOPE_RE.search(text))


def _has_local_turn_scope_intent(text: str) -> bool:
    """Task/session/conversation/turn instructions are not standing Memory scope."""
    return bool(_LOCAL_SCOPE_RE.search(text))


def _strip_receipt_marker(text: str) -> str:
    return _RECEIPT_RE.sub("", text).strip(" \t\r\n:：-—")


class LearningCaptureRouter:
    """Route only trusted raw user turns into canonical authorities."""

    def __init__(
        self,
        *,
        memory_service: Optional[ScopedMemoryService] = None,
        skill_service: Optional[SkillLearningService] = None,
    ) -> None:
        self._memory = memory_service or ScopedMemoryService()
        self._skills = skill_service or SkillLearningService()

    async def _project_auto_memory_enabled(
        self,
        *,
        actor_id: str,
        project_id: Optional[str],
    ) -> bool:
        """Read the Project auto-capture consent boundary, fail closed.

        Older embedding/test memory adapters do not expose ``get_settings``;
        those adapters predate the Project consent flag and retain their
        historical behavior.  The production ``ScopedMemoryService`` always
        provides the getter, where a missing/malformed/false value disables
        every Project ContextMemory write while leaving Skill/Q&A/Docs paths
        available to their own authorities.
        """

        if not project_id:
            return True
        getter = getattr(self._memory, "get_settings", None)
        if not callable(getter):
            return True
        try:
            settings = getter(
                actor_id=str(actor_id),
                project_id=str(project_id),
            )
            if inspect.isawaitable(settings):
                settings = await settings
        except Exception:
            logger.warning(
                "Project auto-memory setting lookup failed; disabling capture",
                exc_info=True,
            )
            return False
        return (
            isinstance(settings, Mapping)
            and settings.get("project_auto_enabled") is True
        )

    async def capture_persisted_websocket_turn(
        self,
        *,
        actor_id: str,
        raw_text: str,
        session_id: str,
        message_id: str,
        agent_run_id: str,
        project_id: Optional[str] = None,
        client_message_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Bind a server-created fallback AgentRun to its persisted WS message.

        Direct WebSocket turns create their fallback AgentRun before the mode
        callback owns persistence, so ``trigger_message_id`` is initially NULL.
        Bind it only after the real ConversationMessage exists.  The AgentRun
        row is locked while checking identity, and an already-bound run is
        accepted only when it already names this exact message.

        This method never creates an AgentRun or ConversationMessage and never
        accepts a client-provided identity as authority.
        """
        actor = str(actor_id or "").strip()
        text = str(raw_text or "").strip()
        session_uuid = _uuid(session_id)
        message_uuid = _uuid(message_id)
        run_uuid = _uuid(agent_run_id)
        project_uuid = _uuid(project_id)
        if (
            not actor
            or session_uuid is None
            or message_uuid is None
            or run_uuid is None
            or (project_id and project_uuid is None)
        ):
            raise SkillLearningValidationError(
                "direct learning capture requires canonical "
                "session/message/AgentRun IDs"
            )

        from ..memory.database import get_database_manager

        session = await get_database_manager().get_session()
        bound_now = False
        try:
            run_result = await session.execute(
                select(AgentRun)
                .where(AgentRun.id == run_uuid)
                .with_for_update()
            )
            run = run_result.scalar_one_or_none()
            message = await session.get(ConversationMessage, message_uuid)

            if run is None or message is None:
                raise SkillLearningValidationError(
                    "direct learning capture provenance not found"
                )
            # Masking source turns are deliberately durable chat history, not
            # reusable Memory/Skill evidence.  Return before binding/capturing
            # any learning receipt; the trusted marker is metadata-only.
            if is_privacy_masking_source(message):
                return {
                    "route": "ignored",
                    "persisted": False,
                    "reason": "privacy_masking_source",
                }
            if (
                message.role != "user"
                or str(message.sender_type or "") != "user"
                or str(message.sender_id or "") != actor
                or message.session_id != session_uuid
                or str(message.content or "").strip() != text
            ):
                raise SkillLearningValidationError(
                    "direct user message provenance mismatch"
                )
            if (
                str(run.user_id or "") != actor
                or run.session_id != session_uuid
                or str(getattr(run, "run_type", "") or "") != "chat_turn"
                or (
                    project_uuid is not None
                    and run.project_id != project_uuid
                )
                or (
                    project_uuid is None
                    and run.project_id is not None
                )
            ):
                raise SkillLearningValidationError(
                    "direct AgentRun provenance mismatch"
                )

            if run.trigger_message_id is None:
                run.trigger_message_id = message_uuid
                run.updated_at = datetime.utcnow()
                bound_now = True
                await session.commit()
            elif run.trigger_message_id != message_uuid:
                raise SkillLearningValidationError(
                    "direct AgentRun is already bound to another message"
                )
        except Exception:
            if bound_now:
                # commit() failure may leave ORM state dirty even though the
                # transaction did not become durable.
                try:
                    await session.rollback()
                except Exception:
                    logger.exception(
                        "Failed to rollback direct AgentRun learning binding"
                    )
            else:
                try:
                    await session.rollback()
                except Exception:
                    pass
            raise
        finally:
            await session.close()

        # Local no-auth mode deliberately has no authenticated learning
        # principal.  The server-owned fallback run may still be bound to its
        # real message above, but it must not be promoted into Memory/Skill.
        if actor == "default_user":
            return {
                "route": "ignored",
                "persisted": False,
                "reason": "direct_turn_has_no_authenticated_principal",
            }
        if not text:
            return {
                "route": "ignored",
                "persisted": False,
                "reason": "empty",
            }

        # Reuse the canonical capture validator/routing after the binding
        # transaction is durable. It re-reads message/run identity and applies
        # the existing policy/candidate/proposal semantics unchanged.
        return await self.capture_authenticated_turn(
            actor_id=actor,
            raw_text=text,
            session_id=str(session_uuid),
            message_id=str(message_uuid),
            agent_run_id=str(run_uuid),
            project_id=str(project_uuid) if project_uuid else None,
            client_message_id=client_message_id,
        )

    async def _validate_canonical_turn(
        self,
        *,
        actor_id: str,
        raw_text: str,
        session_id: str,
        message_id: str,
        agent_run_id: str,
        project_id: Optional[str],
    ) -> bool:
        from ..memory.database import get_database_manager

        session_uuid = _uuid(session_id)
        message_uuid = _uuid(message_id)
        run_uuid = _uuid(agent_run_id)
        project_uuid = _uuid(project_id)
        if session_uuid is None or message_uuid is None or run_uuid is None:
            raise SkillLearningValidationError(
                "learning capture requires canonical session/message/AgentRun IDs"
            )
        session = await get_database_manager().get_session()
        masking_source = False
        try:
            message = await session.get(ConversationMessage, message_uuid)
            run = await session.get(AgentRun, run_uuid)
            if message is None or run is None:
                raise SkillLearningValidationError("canonical turn not found")
            if (
                message.role != "user"
                or str(message.sender_type or "") != "user"
                or str(message.sender_id or "") != str(actor_id)
                or message.session_id != session_uuid
                or str(message.content or "").strip() != str(raw_text or "").strip()
            ):
                raise SkillLearningValidationError("raw user message provenance mismatch")
            if (
                str(run.user_id or "") != str(actor_id)
                or run.session_id != session_uuid
                or run.trigger_message_id != message_uuid
                or (project_uuid is not None and run.project_id != project_uuid)
                or (project_uuid is None and run.project_id is not None)
            ):
                raise SkillLearningValidationError("AgentRun provenance mismatch")
            # Return the structural marker observed during the same canonical
            # validation read.  This closes the race/availability gap where a
            # second optional metadata lookup could fail and accidentally let
            # a masking source continue into Memory/Skill capture.
            masking_source = is_privacy_masking_source(message)
        finally:
            await session.close()
        return masking_source

    async def capture_authenticated_turn(
        self,
        *,
        actor_id: str,
        raw_text: str,
        session_id: str,
        message_id: str,
        agent_run_id: str,
        project_id: Optional[str] = None,
        client_message_id: Optional[str] = None,
    ) -> dict[str, Any]:
        actor = str(actor_id or "").strip()
        text = str(raw_text or "").strip()
        if not text:
            return {"route": "ignored", "reason": "empty"}

        # ``default_user`` is the auth-disabled compatibility principal, not a
        # UUID-backed Project member. Do not widen ScopedMemoryService's
        # authenticated Project ACL to make automatic learning work for it.
        # Direct WebSocket capture already follows the same fail-closed rule;
        # REST Project learning therefore explicitly remains non-persistent.
        #
        # Legacy no-auth user/global Memory behavior is left unchanged.
        if actor == "default_user" and project_id:
            return {
                "route": "ignored",
                "persisted": False,
                "reason": "no_auth_project_learning_has_no_durable_principal",
            }

        canonical_masking_source = await self._validate_canonical_turn(
            actor_id=actor,
            raw_text=text,
            session_id=str(session_id),
            message_id=str(message_id),
            agent_run_id=str(agent_run_id),
            project_id=project_id,
        )

        # The base validator returns the marker observed in its authoritative
        # read.  Test/embedding subclasses from older deployments may still
        # return ``None``; only those legacy seams need the compatibility
        # reread below.
        if canonical_masking_source is True:
            return {
                "route": "ignored",
                "persisted": False,
                "reason": "privacy_masking_source",
            }

        # Re-read the canonical row's metadata after provenance validation so
        # a masking source cannot enter Memory/Skill even when an older caller
        # reaches this method directly (without the direct-WebSocket helper).
        if canonical_masking_source is None:
            try:
                from ..memory.database import get_database_manager

                session = await get_database_manager().get_session()
                try:
                    message = await session.get(
                        ConversationMessage,
                        _uuid(message_id),
                    )
                finally:
                    await session.close()
                if message is not None and is_privacy_masking_source(message):
                    return {
                        "route": "ignored",
                        "persisted": False,
                        "reason": "privacy_masking_source",
                    }
            except Exception:
                # Legacy validator overrides may not know about the masking
                # marker.  If their optional compatibility read is
                # unavailable, preserve the historical behavior rather than
                # turning a valid learning turn into a hard failure.
                pass
        turn_context = {
            "user_id": actor,
            "project_id": str(project_id) if project_id else None,
            "session_id": str(session_id),
            "message_id": str(message_id),
            "client_message_id": str(client_message_id) if client_message_id else None,
            "agent_run_id": str(agent_run_id),
        }
        canonical_message_uuid = _uuid(message_id)
        conversation_evidence = {
            "type": "conversation_message",
            "id": str(message_id),
            # ``message_id`` is the explicit canonical field consumed by the
            # Project evidence identity helper.  Keep ``id`` for legacy
            # consumers that only understand the historical shape.
            "message_id": str(message_id),
        }
        if canonical_message_uuid is not None:
            conversation_evidence["evidence_identity"] = (
                f"chat:{canonical_message_uuid}"
            )
        evidence_refs = [
            conversation_evidence,
            {"type": "agent_run", "id": str(agent_run_id)},
        ]

        # Policy/approval/credential/tool-access/security corrections are not Memory
        # and not Skill mutations. The raw turn continues through the canonical chat
        # execution path where existing permission/policy authorities handle it.
        if _SECURITY_AUTHORITY_RE.search(text):
            return {
                "route": "policy_authority",
                "persisted": False,
                "reason": "permission_or_security_authority",
            }

        project_auto_enabled = await self._project_auto_memory_enabled(
            actor_id=actor,
            project_id=project_id,
        )

        receipt_match = _RECEIPT_RE.search(text)
        if _SKILL_RE.search(text) and _CORRECTION_RE.search(text):
            if receipt_match:
                receipt_id = receipt_match.group(1)
                correction_text = _strip_receipt_marker(text)
                proposal = await self._skills.create_correction_from_receipt(
                    actor_id=actor,
                    receipt_id=receipt_id,
                    correction_text=correction_text,
                    provenance={"turn_context": turn_context},
                )
                return {
                    "route": "skill_correction_proposal",
                    "persisted": True,
                    "proposal": proposal,
                }

            # A marker is not required when the immediately preceding actual
            # Skill operation is unambiguous within this authenticated
            # actor/session/project boundary.  The resolver never searches by
            # Skill name alone and never crosses actor/session/Project scope.
            try:
                recent_receipt = (
                    await self._skills.resolve_recent_receipt_for_correction(
                        actor_id=actor,
                        session_id=str(session_id),
                        project_id=str(project_id) if project_id else None,
                    )
                )
            except SkillLearningError:
                recent_receipt = None
            if recent_receipt and recent_receipt.get("id"):
                proposal = await self._skills.create_correction_from_receipt(
                    actor_id=actor,
                    receipt_id=str(recent_receipt["id"]),
                    correction_text=text,
                    provenance={"turn_context": turn_context},
                )
                return {
                    "route": "skill_correction_proposal",
                    "persisted": True,
                    "proposal": proposal,
                }

            if project_id and not project_auto_enabled:
                return {
                    "route": "ignored",
                    "persisted": False,
                    "reason": "project_memory_disabled",
                }

            candidate = await self._capture_memory(
                actor_id=actor,
                text=text,
                project_id=project_id,
                turn_context=turn_context,
                evidence_refs=evidence_refs,
                status="candidate",
                source_type="learning_capture_auto",
            )
            return {
                "route": "candidate",
                "persisted": True,
                "reason": "skill_correction_missing_or_ambiguous_receipt",
                "memory": candidate,
            }

        create_match = _SKILL_CREATE_RE.search(text)
        if create_match:
            skill_name = create_match.group(1)
            target_scope = "project" if _has_project_scope_intent(text) else "global"
            if target_scope == "project" and not project_id:
                return {
                    "route": "candidate_unbound_project",
                    "persisted": False,
                    "reason": "explicit project procedure without project context",
                }
            body = text[create_match.end() :].strip(" \t\r\n:：-—") or text
            proposal = await self._skills.create_proposal(
                actor_id=actor,
                operation="create",
                target_scope=target_scope,
                target_name=skill_name,
                project_id=(str(project_id) if target_scope == "project" else None),
                reason_type="procedure",
                proposed_content={
                    "name": skill_name,
                    "description": f"Learned reusable procedure: {skill_name}",
                    "prompt_template": body,
                    "trigger_mode": "both",
                    "aliases": [],
                    "bound_tools": [],
                    "examples": [],
                    "tags": ["learned-procedure"],
                    "parameters": {},
                },
                evidence=evidence_refs,
                provenance={"turn_context": turn_context},
                idempotency_key=f"learning-procedure:{message_id}",
            )
            return {
                "route": "skill_create_proposal",
                "persisted": True,
                "proposal": proposal,
            }

        project_rule = _has_project_scope_intent(text)
        standing_rule = _has_durable_user_intent(text)
        guidance = _has_guidance_intent(text)
        temporary = _has_transient_turn_intent(text)
        local_turn_scope = _has_local_turn_scope_intent(text)
        correction = bool(_CORRECTION_RE.search(text))
        actionable = standing_rule or guidance or correction

        # The explicit Project scope marker is the only signal that may route
        # this turn into Project ContextMemory.  A disabled Project consent
        # flag suppresses active *and* candidate Project rows, but leaves
        # User-global routing and Skill proposal paths untouched.
        if project_rule and project_id and not project_auto_enabled:
            return {
                "route": "ignored",
                "persisted": False,
                "reason": "project_memory_disabled",
            }

        if local_turn_scope and actionable:
            if project_rule and not project_id:
                return {
                    "route": "candidate_unbound_project",
                    "persisted": False,
                    "reason": "turn-local project guidance without project context",
                }
            memory = await self._capture_memory(
                actor_id=actor,
                text=text,
                project_id=project_id if project_rule else None,
                turn_context=turn_context,
                evidence_refs=evidence_refs,
                status="candidate",
                source_type="learning_capture_auto",
            )
            return {
                "route": "candidate",
                "persisted": True,
                "reason": "turn_local_scope",
                "memory": memory,
            }

        if project_rule and not project_id and actionable:
            return {
                "route": "candidate_unbound_project",
                "persisted": False,
                "reason": "explicit project guidance without project context",
            }

        durable = standing_rule or (project_rule and guidance)
        if durable and not temporary:
            memory = await self._capture_memory(
                actor_id=actor,
                text=text,
                project_id=project_id if project_rule else None,
                turn_context=turn_context,
                evidence_refs=evidence_refs,
                status="active",
                source_type="learning_capture",
            )
            return {
                "route": "active_project_memory" if project_rule else "active_user_memory",
                "persisted": True,
                "memory": memory,
            }

        if temporary or correction:
            memory = await self._capture_memory(
                actor_id=actor,
                text=text,
                project_id=project_id if project_rule else None,
                turn_context=turn_context,
                evidence_refs=evidence_refs,
                status="candidate",
                source_type="learning_capture_auto",
            )
            return {
                "route": "candidate",
                "persisted": True,
                "memory": memory,
            }

        return {"route": "ignored", "persisted": False, "reason": "no durable signal"}

    async def _capture_memory(
        self,
        *,
        actor_id: str,
        text: str,
        project_id: Optional[str],
        turn_context: dict[str, Any],
        evidence_refs: list[dict[str, Any]],
        status: str,
        source_type: str,
    ) -> dict[str, Any]:
        project_scope = bool(project_id)
        return await self._memory.upsert_memory(
            actor_id=str(actor_id),
            content=text,
            scope_type="project" if project_scope else "user",
            scope_id=str(project_id) if project_scope else str(actor_id),
            project_id=str(project_id) if project_scope else None,
            memory_type=_memory_type(text),
            source_type=source_type,
            source_ref=f"conversation_message:{turn_context['message_id']}",
            confidence=1.0 if status == "active" else 0.5,
            importance=9 if status == "active" else 5,
            trust_level="verified" if status == "active" else "inferred",
            evidence_refs=evidence_refs,
            evidence_span={"message_id": turn_context["message_id"]},
            status=status,
            turn_context=turn_context,
            idempotency_key=f"learning-memory:{turn_context['message_id']}:{status}",
            source_session_id=(
                str(turn_context["session_id"]) if project_scope else None
            ),
            source_message_id=(
                str(turn_context["message_id"]) if project_scope else None
            ),
            source_user_input=(str(text) if project_scope else None),
        )


async def capture_direct_websocket_learning_best_effort(
    *,
    actor_id: str,
    raw_text: str,
    session_id: str,
    message_id: str,
    agent_run_id: str,
    project_id: Optional[str] = None,
    client_message_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Best-effort direct-WS capture after canonical user persistence.

    This boundary intentionally catches ordinary capture/binding failures so
    learning cannot turn an otherwise valid chat turn into an assistant error.
    Cancellation is not swallowed because asyncio.CancelledError is not an
    Exception on supported Python versions.
    """
    if not (
        str(actor_id or "").strip()
        and str(session_id or "").strip()
        and str(message_id or "").strip()
        and str(agent_run_id or "").strip()
    ):
        return None
    try:
        return await LearningCaptureRouter().capture_persisted_websocket_turn(
            actor_id=str(actor_id),
            raw_text=str(raw_text or ""),
            session_id=str(session_id),
            message_id=str(message_id),
            agent_run_id=str(agent_run_id),
            project_id=str(project_id) if project_id else None,
            client_message_id=(
                str(client_message_id)
                if client_message_id
                else None
            ),
        )
    except Exception:
        logger.exception(
            "Direct WebSocket learning capture failed "
            "(session=%s run=%s message=%s)",
            session_id,
            agent_run_id,
            message_id,
        )
        return None
