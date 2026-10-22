"""Task-local identity and project scope for one assistant turn."""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar, Token, copy_context
from dataclasses import dataclass, replace
from typing import Any, Iterable, Iterator, Mapping


# Provider-local system prompt used only while a trusted AoiTalk Help turn is
# active.  The complete, authenticated Guide snapshot is carried in the
# request prompt; this short static prefix prevents a long-lived provider's
# ordinary character/custom/session prompt from leaking into that turn.
AOITALK_HELP_ISOLATED_SYSTEM_PROMPT = (
    "AoiTalk Helpの回答専用ターンです。"
    "ユーザー入力に含まれるサーバー検証済みAoiTalkガイド本文と、"
    "補助的な画像証拠だけを根拠に日本語で回答してください。"
    "ツール、検索、Docs・Project・会話履歴の参照や製品データの操作は行わず、"
    "ガイドに根拠がない仕様は確認できないと伝えてください。"
)


@dataclass(frozen=True)
class ResourceReference:
    """One server-validated resource explicitly named in the current turn.

    The object is deliberately tiny and immutable.  Callers must only create
    references after resolving the untrusted client mention through the
    resource's authorization boundary; ``set_turn_context`` merely normalizes
    and de-duplicates the already trusted values.
    """

    kind: str
    id: str

    def __post_init__(self) -> None:
        kind = str(self.kind or "").strip().casefold()
        identifier = str(self.id or "").strip()
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "id", identifier)


@dataclass(frozen=True)
class AgentDialogueContinuationTarget:
    """Server-authored immutable target for one Human-answer continuation."""

    dialogue_id: str
    input_id: str
    input_revision: int
    control_revision: int
    source_run_id: str
    source_work_item_id: str
    source_message_id: str
    source_session_id: str
    source_agent_id: str
    source_agent_revision_id: str
    answer_message_id: str
    root_session_id: str
    root_agent_id: str
    root_agent_revision_id: str
    root_run_id: str
    root_work_item_id: str
    target_digest: str

    @classmethod
    def from_mapping(cls, value: Any) -> "AgentDialogueContinuationTarget | None":
        if not isinstance(value, Mapping):
            return None
        if value.get("target_kind") != "human_answer_continuation":
            return None
        required = (
            "dialogue_id",
            "input_id",
            "source_run_id",
            "source_work_item_id",
            "source_message_id",
            "source_session_id",
            "source_agent_id",
            "source_agent_revision_id",
            "answer_message_id",
            "root_session_id",
            "root_agent_id",
            "root_agent_revision_id",
            "root_run_id",
            "root_work_item_id",
            "target_digest",
        )
        if any(not str(value.get(key) or "").strip() for key in required):
            return None
        try:
            input_revision = int(value.get("input_revision"))
            control_revision = int(value.get("control_revision"))
        except (TypeError, ValueError, OverflowError):
            return None
        if input_revision < 1 or control_revision < 0:
            return None
        digest_material = {
            str(key): value[key]
            for key in value
            if str(key) != "target_digest"
        }
        expected_digest = hashlib.sha256(
            json.dumps(
                digest_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if str(value.get("target_digest") or "").casefold() != expected_digest:
            return None
        return cls(
            dialogue_id=str(value["dialogue_id"]).strip(),
            input_id=str(value["input_id"]).strip(),
            input_revision=input_revision,
            control_revision=control_revision,
            source_run_id=str(value["source_run_id"]).strip(),
            source_work_item_id=str(value["source_work_item_id"]).strip(),
            source_message_id=str(value["source_message_id"]).strip(),
            source_session_id=str(value["source_session_id"]).strip(),
            source_agent_id=str(value["source_agent_id"]).strip(),
            source_agent_revision_id=str(value["source_agent_revision_id"]).strip(),
            answer_message_id=str(value["answer_message_id"]).strip(),
            root_session_id=str(value["root_session_id"]).strip(),
            root_agent_id=str(value["root_agent_id"]).strip(),
            root_agent_revision_id=str(value["root_agent_revision_id"]).strip(),
            root_run_id=str(value["root_run_id"]).strip(),
            root_work_item_id=str(value["root_work_item_id"]).strip(),
            target_digest=str(value["target_digest"]).strip().casefold(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_kind": "human_answer_continuation",
            "dialogue_id": self.dialogue_id,
            "input_id": self.input_id,
            "input_revision": self.input_revision,
            "control_revision": self.control_revision,
            "source_run_id": self.source_run_id,
            "source_work_item_id": self.source_work_item_id,
            "source_message_id": self.source_message_id,
            "source_session_id": self.source_session_id,
            "source_agent_id": self.source_agent_id,
            "source_agent_revision_id": self.source_agent_revision_id,
            "answer_message_id": self.answer_message_id,
            "root_session_id": self.root_session_id,
            "root_agent_id": self.root_agent_id,
            "root_agent_revision_id": self.root_agent_revision_id,
            "root_run_id": self.root_run_id,
            "root_work_item_id": self.root_work_item_id,
            "target_digest": self.target_digest,
        }


@dataclass(frozen=True)
class TurnContext:
    user_id: str | None = None
    # ``project_id`` is the UI-selected Project identity, not an instruction
    # to inject Project Context.  ``None`` keeps the historical behaviour for
    # callers that predate the explicit turn flag; Web/REST turns always pass
    # an explicit bool so OFF can be distinguished from a selected Project.
    project_id: str | None = None
    include_project_context: bool | None = None
    session_id: str | None = None
    # ``task_id`` is a specific, already-authorized Task scope.  It is kept
    # separate from ``session_id`` (the AoiTalk conversation id) and from
    # provider-native continuation ids.
    task_id: str | None = None
    message_id: str | None = None
    client_message_id: str | None = None
    tool_call_id: str | None = None
    docs_reference_ids: tuple[str, ...] = ()
    # Explicit @ references resolved and authorized at the request boundary.
    # Raw mention names/labels must never be placed here.
    explicit_references: tuple[ResourceReference, ...] = ()
    # Set only by a trusted server boundary after validating Project
    # attachment paths.  Prompt text/markers never grant workspace
    # stewardship by themselves.
    verified_project_attachment: bool = False

    # Trusted background controllers may provide their complete bounded
    # context explicitly and prohibit the normal chat ContextBuilder layers.
    suppress_automatic_context: bool = False
    # When set, read tools must remain on exactly ``project_id``. This is a
    # server-issued execution constraint, never a prompt/model-controlled flag.
    strict_project_scope: bool = False

    # Cloud Advisor is a parent-owned capability.  These fields are optional
    # request-boundary metadata and are deliberately kept separate from the
    # user/model-visible prompt.  ``cloud_advisor_origin`` is normalized to
    # the service enum when supplied by a trusted caller (including the
    # system-owned workflow controller); malformed values are discarded (fail
    # closed to the normal Main-agent origin).  The assessment is accepted
    # only when it is the immutable semantic assessment object owned by
    # ``cloud_advisor_service``; arbitrary mappings/booleans from a request or
    # model tool argument never become escalation authority.
    cloud_advisor_origin: Any | None = None
    cloud_advisor_assessment: Any | None = None

    # A trusted Operations Agent execution binding.  This is populated only
    # after the server has resolved the canonical ConversationSession Agent
    # and pinned its exact AgentRevision.  Character/Persona state deliberately
    # remains separate from this identity.
    agent_id: str | None = None
    agent_revision_id: str | None = None
    agent_execution_binding: Any | None = None
    agent_role: str | None = None
    agent_execution_config: Any | None = None

    # A durable AgentDialogue turn is still an ordinary Chat turn.  These
    # fields are server-derived references used to keep the worker's input,
    # control revision, and human anchor separate from provider prose.
    agent_dialogue_id: str | None = None
    agent_dialogue_role: str | None = None
    agent_dialogue_input_id: str | None = None
    agent_dialogue_control_revision: int | None = None
    agent_dialogue_human_anchor: str | None = None
    agent_dialogue_continuation_target: AgentDialogueContinuationTarget | None = None
    agent_dialogue_typed_resume_answer: str | None = None
    agent_dialogue_typed_resume_answer_id: str | None = None
    context_project_hint_id: str | None = None


_current_turn_context: ContextVar[TurnContext] = ContextVar(
    "assistant_turn_context",
    default=TurnContext(),
)


def bind_context_to_iterator(iterator: Iterator[Any]) -> Iterator[Any]:
    """Run every ``next``/``close`` of a lazy iterator in this context.

    A synchronous generator does not execute its body until the first
    consumer call.  Public provider ``stream_chat`` APIs are commonly created
    inside a request and consumed after that request has reset its
    ``ContextVar`` tokens (or on another thread).  Capturing only the context
    at generator creation is therefore insufficient; each iterator operation
    must execute under the captured context.
    """

    captured = copy_context()

    def _bound() -> Iterator[Any]:
        try:
            while True:
                try:
                    yield captured.run(next, iterator)
                except StopIteration:
                    return
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                captured.run(close)

    return _bound()


def set_turn_context(
    *,
    user_id: str | None = None,
    project_id: str | None = None,
    include_project_context: bool | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    message_id: str | None = None,
    client_message_id: str | None = None,
    tool_call_id: str | None = None,
    docs_reference_ids: Iterable[str] | None = None,
    explicit_references: Iterable[ResourceReference | Mapping[str, Any] | tuple[str, str]] | None = None,
    verified_project_attachment: bool = False,
    suppress_automatic_context: bool = False,
    strict_project_scope: bool = False,
    cloud_advisor_origin: Any | None = None,
    cloud_advisor_assessment: Any | None = None,
    agent_id: str | None = None,
    agent_revision_id: str | None = None,
    agent_execution_binding: Any | None = None,
    agent_role: str | None = None,
    agent_execution_config: Any | None = None,
    agent_dialogue_id: str | None = None,
    agent_dialogue_role: str | None = None,
    agent_dialogue_input_id: str | None = None,
    agent_dialogue_control_revision: int | None = None,
    agent_dialogue_human_anchor: str | None = None,
    agent_dialogue_continuation_target: AgentDialogueContinuationTarget | None = None,
    agent_dialogue_typed_resume_answer: str | None = None,
    agent_dialogue_typed_resume_answer_id: str | None = None,
    context_project_hint_id: str | None = None,
) -> Token:
    normalized_reference_ids = tuple(
        dict.fromkeys(
            str(value).strip().lower()
            for value in docs_reference_ids or ()
            if str(value).strip()
        )
    )
    normalized_references: list[ResourceReference] = []
    seen_references: set[tuple[str, str]] = set()
    for value in explicit_references or ():
        reference: ResourceReference | None = None
        if isinstance(value, ResourceReference):
            reference = value
        elif isinstance(value, Mapping):
            kind = value.get("kind")
            identifier = value.get("id")
            if kind and identifier:
                reference = ResourceReference(str(kind), str(identifier))
        elif isinstance(value, (tuple, list)) and len(value) >= 2:
            kind, identifier = value[0], value[1]
            if kind and identifier:
                reference = ResourceReference(str(kind), str(identifier))
        if reference is None or not reference.kind or not reference.id:
            continue
        key = (reference.kind, reference.id.casefold())
        if key in seen_references:
            continue
        seen_references.add(key)
        normalized_references.append(reference)

    # ``docs_reference_ids`` is a compatibility field used by the Inbox
    # update guard.  Treat values supplied by the trusted Docs ACL resolver as
    # explicit ``docs`` references too, while still preserving the historical
    # tuple shape and case-folding behaviour.
    for reference_id in normalized_reference_ids:
        key = ("docs", reference_id.casefold())
        if key not in seen_references:
            normalized_references.append(ResourceReference("docs", reference_id))
            seen_references.add(key)
    normalized_docs_reference_ids = tuple(
        dict.fromkeys(
            [
                *normalized_reference_ids,
                *(
                    reference.id.strip().lower()
                    for reference in normalized_references
                    if reference.kind in {"docs", "doc", "node"}
                    and reference.id.strip()
                ),
            ]
        )
    )

    # Keep the turn-context module independent from the Cloud Advisor service
    # at import time (the service itself imports ``get_turn_context``).  The
    # trusted type check therefore happens lazily at the request boundary.
    # Unknown origins/assessment shapes are ignored rather than promoted to
    # automatic-escalation authority.
    normalized_cloud_origin: Any | None = None
    if cloud_advisor_origin is not None:
        try:
            from .cloud_advisor_service import CloudAdvisorTriggerOrigin

            if isinstance(cloud_advisor_origin, CloudAdvisorTriggerOrigin):
                normalized_cloud_origin = cloud_advisor_origin
            else:
                normalized_cloud_origin = CloudAdvisorTriggerOrigin(
                    str(cloud_advisor_origin).strip().casefold()
                )
        except (ImportError, TypeError, ValueError):
            normalized_cloud_origin = None

    normalized_cloud_assessment: Any | None = None
    if cloud_advisor_assessment is not None:
        try:
            from .cloud_advisor_service import CloudAdvisorEscalationAssessment

            if isinstance(
                cloud_advisor_assessment,
                CloudAdvisorEscalationAssessment,
            ):
                normalized_cloud_assessment = cloud_advisor_assessment
        except ImportError:
            normalized_cloud_assessment = None

    return _current_turn_context.set(
        TurnContext(
            user_id=str(user_id).strip() if user_id else None,
            project_id=str(project_id).strip() if project_id else None,
            include_project_context=(
                bool(include_project_context)
                if include_project_context is not None
                else None
            ),
            session_id=str(session_id).strip() if session_id else None,
            task_id=str(task_id).strip() if task_id else None,
            message_id=str(message_id).strip() if message_id else None,
            client_message_id=(
                str(client_message_id).strip() if client_message_id else None
            ),
            tool_call_id=str(tool_call_id).strip() if tool_call_id else None,
            docs_reference_ids=normalized_docs_reference_ids,
            explicit_references=tuple(normalized_references),
            verified_project_attachment=bool(verified_project_attachment),
            suppress_automatic_context=bool(suppress_automatic_context),
            strict_project_scope=bool(strict_project_scope),
            cloud_advisor_origin=normalized_cloud_origin,
            cloud_advisor_assessment=normalized_cloud_assessment,
            agent_id=str(agent_id).strip() if agent_id else None,
            agent_revision_id=(
                str(agent_revision_id).strip() if agent_revision_id else None
            ),
            agent_execution_binding=agent_execution_binding,
            agent_role=str(agent_role).strip() if agent_role else None,
            agent_execution_config=agent_execution_config,
            agent_dialogue_id=(
                str(agent_dialogue_id).strip() if agent_dialogue_id else None
            ),
            agent_dialogue_role=(
                str(agent_dialogue_role).strip().casefold()
                if agent_dialogue_role
                else None
            ),
            agent_dialogue_input_id=(
                str(agent_dialogue_input_id).strip() if agent_dialogue_input_id else None
            ),
            agent_dialogue_control_revision=(
                max(0, int(agent_dialogue_control_revision))
                if agent_dialogue_control_revision is not None
                else None
            ),
            agent_dialogue_human_anchor=(
                str(agent_dialogue_human_anchor).strip()[:131_072]
                if agent_dialogue_human_anchor
                else None
            ),
            agent_dialogue_continuation_target=(
                agent_dialogue_continuation_target
                if isinstance(
                    agent_dialogue_continuation_target,
                    AgentDialogueContinuationTarget,
                )
                else None
            ),
            agent_dialogue_typed_resume_answer=(
                str(agent_dialogue_typed_resume_answer)
                if agent_dialogue_typed_resume_answer is not None
                else None
            ),
            agent_dialogue_typed_resume_answer_id=(
                str(agent_dialogue_typed_resume_answer_id).strip()
                if agent_dialogue_typed_resume_answer_id
                else None
            ),
            context_project_hint_id=(
                str(context_project_hint_id).strip() if context_project_hint_id else None
            ),
        )
    )


def get_turn_context() -> TurnContext:
    return _current_turn_context.get()


def is_agent_dialogue_worker(context: TurnContext | None = None) -> bool:
    """Return whether the current turn is a server-bound dialogue worker.

    The explicit role is preferred.  The immutable binding projection is a
    compatibility fallback for adapters that have not yet copied the fields
    onto ``TurnContext``; arbitrary provider/model text is never inspected.
    """

    current = context or get_turn_context()
    role = str(current.agent_dialogue_role or "").strip().casefold()
    if role:
        return role == "worker"
    binding = current.agent_execution_binding
    if isinstance(binding, Mapping):
        role = str(
            binding.get("agent_dialogue_role")
            or binding.get("dialogue_role")
            or binding.get("role")
            or ""
        ).strip().casefold()
        return role == "worker"
    for name in ("agent_dialogue_role", "dialogue_role"):
        role = str(getattr(binding, name, "") or "").strip().casefold()
        if role:
            return role == "worker"
    return False


def override_turn_context(**changes: Any) -> Token:
    """Temporarily override selected fields in the current turn context.

    The existing immutable context is copied so all unmodified identity,
    scope, and authorization references remain bound to the same turn.  The
    returned token must be passed to :func:`reset_turn_context` by the caller
    after the temporary override is no longer needed.
    """
    return _current_turn_context.set(replace(get_turn_context(), **changes))


def is_project_context_enabled(context: TurnContext | None = None) -> bool:
    """Return whether this turn may use the selected Project as context.

    New request boundaries pass ``include_project_context`` explicitly.  A
    ``None`` value is retained for legacy/background callers and preserves
    their previous project-scoped behaviour when a Project identity exists.
    This helper intentionally does *not* erase ``project_id``: authorization
    and explicit ``get_project_context()`` lookups still need the selected ID
    even when this function returns ``False``.
    """

    current = context or get_turn_context()
    if current.include_project_context is not None:
        return bool(current.include_project_context)
    return bool(current.project_id)


def set_turn_tool_call_id(tool_call_id: str | None) -> Token:
    """Attach one provider call ID while preserving the surrounding turn."""
    normalized = str(tool_call_id or "").strip() or None
    return _current_turn_context.set(
        replace(get_turn_context(), tool_call_id=normalized)
    )


def is_docs_reference_in_turn(node_id: str) -> bool:
    """Return whether the current user message explicitly named this Docs UUID."""
    normalized = str(node_id or "").strip().lower()
    return bool(normalized) and normalized in get_turn_context().docs_reference_ids


def is_explicit_reference_in_turn(
    kind: str | ResourceReference,
    reference_id: str | None = None,
    context: TurnContext | None = None,
) -> bool:
    """Return whether an ACL-validated explicit reference is bound to this turn.

    ``kind``/``reference_id`` is the preferred form.  Passing a
    :class:`ResourceReference` as the first argument is accepted as a small
    convenience for resolver/tool code.  Comparison is case-insensitive for
    both the resource kind and identifier; the immutable stored value remains
    canonical for prompt/audit consumers.
    """

    if isinstance(kind, ResourceReference):
        reference = kind
    else:
        normalized_kind = str(kind or "").strip().casefold()
        normalized_id = str(reference_id or "").strip()
        if not normalized_kind or not normalized_id:
            return False
        reference = ResourceReference(normalized_kind, normalized_id)
    if not reference.kind or not reference.id:
        return False
    current = context or get_turn_context()
    return any(
        item.kind == reference.kind and item.id.casefold() == reference.id.casefold()
        for item in current.explicit_references
    )


def reset_turn_context(token: Token) -> None:
    _current_turn_context.reset(token)


def set_cloud_advisor_turn_context(
    *,
    origin: Any,
    assessment: Any | None = None,
) -> Token:
    """Bind trusted Cloud Advisor metadata to the current turn.

    Parent/request controllers should call this only after deciding the
    trigger origin and (for automatic mode) constructing a
    ``CloudAdvisorEscalationAssessment`` from trusted semantic signals.  The
    helper rejects arbitrary mappings and model-provided booleans, preserving
    the fail-closed default when the service is unavailable or the values are
    malformed.  Reset the returned token with :func:`reset_turn_context`.
    """

    current = get_turn_context()
    # Reuse the exact normalization performed by ``set_turn_context`` without
    # rebuilding unrelated identity/scope fields.  This keeps the helper safe
    # for nested parent scopes and avoids an import cycle at module load time.
    normalized_origin: Any | None = None
    normalized_assessment: Any | None = None
    try:
        from .cloud_advisor_service import (
            CloudAdvisorEscalationAssessment,
            CloudAdvisorTriggerOrigin,
        )

        if isinstance(origin, CloudAdvisorTriggerOrigin):
            normalized_origin = origin
        else:
            normalized_origin = CloudAdvisorTriggerOrigin(
                str(origin).strip().casefold()
            )
        if assessment is None:
            normalized_assessment = CloudAdvisorEscalationAssessment()
        elif isinstance(assessment, CloudAdvisorEscalationAssessment):
            normalized_assessment = assessment
    except (ImportError, TypeError, ValueError):
        # A missing service or malformed caller input cannot grant authority.
        normalized_origin = None
        normalized_assessment = None

    return _current_turn_context.set(
        replace(
            current,
            cloud_advisor_origin=normalized_origin,
            cloud_advisor_assessment=normalized_assessment,
        )
    )
