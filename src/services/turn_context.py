"""Task-local identity and project scope for one assistant turn."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnContext:
    user_id: str | None = None
    project_id: str | None = None


_current_turn_context: ContextVar[TurnContext] = ContextVar(
    "assistant_turn_context",
    default=TurnContext(),
)


def set_turn_context(*, user_id: str | None, project_id: str | None) -> Token:
    return _current_turn_context.set(
        TurnContext(
            user_id=str(user_id).strip() if user_id else None,
            project_id=str(project_id).strip() if project_id else None,
        )
    )


def get_turn_context() -> TurnContext:
    return _current_turn_context.get()


def reset_turn_context(token: Token) -> None:
    _current_turn_context.reset(token)
