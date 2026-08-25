"""Task-local identity and project scope for one assistant turn."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping


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


_current_turn_context: ContextVar[TurnContext] = ContextVar(
    "assistant_turn_context",
    default=TurnContext(),
)


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
