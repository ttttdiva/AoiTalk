"""Task-local identity and project scope for one assistant turn."""

from __future__ import annotations

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
        )
    )


def get_turn_context() -> TurnContext:
    return _current_turn_context.get()


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
