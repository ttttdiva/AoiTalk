"""Typed, bounded outcomes for planning tool preflight failures.

The public mapping shape intentionally keeps the historical ``success`` /
``error`` / ``message`` keys.  The ``PlanningToolOutcome`` type itself is the
authority for runtime control flow; callers must not reconstruct authority
from a JSON-decoded mapping that merely resembles this shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal


PlanningFailureClass = Literal["terminal", "correctable_once", "human_wait"]
PlanningFailureCode = Literal[
    "planning_not_active",
    "structured_actions_required",
    "approved_action_atomicity_unsupported",
    "material_action_preview_invalid",
    "approval_already_pending",
]

_CORRECTION_SCHEMA: dict[str, Any] = {
    "actions_json": {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "required": ["tool", "args"],
            "properties": {
                "tool": {"type": "string"},
                "args": {"type": "object"},
                "dynamic_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
    }
}

_MESSAGES: dict[PlanningFailureCode, str] = {
    "planning_not_active": (
        "計画承認を開始できないため、処理を停止しました。"
    ),
    "structured_actions_required": (
        "承認に必要な操作内容が不足しています。操作内容の組み立てを一度修正します。"
    ),
    "approved_action_atomicity_unsupported": (
        "この操作は承認付きの実行に対応していません。計画の操作内容を見直します。"
    ),
    "material_action_preview_invalid": (
        "計画の変更内容を確認できません。計画の範囲と操作内容を見直します。"
    ),
    "approval_already_pending": (
        "承認待ちです。表示中の計画を承認するか、修正を指示してください。"
    ),
}

_CLASSES: dict[PlanningFailureCode, PlanningFailureClass] = {
    "planning_not_active": "terminal",
    "structured_actions_required": "correctable_once",
    "approved_action_atomicity_unsupported": "correctable_once",
    "material_action_preview_invalid": "correctable_once",
    "approval_already_pending": "human_wait",
}


def correction_schema(*, allowed_tools: tuple[str, ...] = ()) -> dict[str, Any]:
    """Return a bounded, provider-safe correction hint.

    The schema contains no candidate plan, raw arguments, or redaction input.
    ``allowed_tools`` is supplied by the planning runtime only after its
    server-owned allowlist has been resolved.
    """

    schema = {
        "actions_json": {
            "type": _CORRECTION_SCHEMA["actions_json"]["type"],
            "minItems": _CORRECTION_SCHEMA["actions_json"]["minItems"],
            "items": {
                "type": "object",
                "required": ["tool", "args"],
                "properties": {
                    "tool": {"type": "string"},
                    "args": {"type": "object"},
                    "dynamic_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        }
    }
    if allowed_tools:
        schema["allowed_tools"] = list(allowed_tools)
    return schema


class PlanningToolOutcome(dict[str, Any]):
    """A JSON-compatible mapping with typed planning failure metadata."""

    failure_code: PlanningFailureCode
    failure_class: PlanningFailureClass
    revision: int | None

    def __init__(
        self,
        *,
        code: PlanningFailureCode,
        failure_class: PlanningFailureClass,
        message: str,
        correction: Mapping[str, Any] | None = None,
        revision: int | None = None,
    ) -> None:
        self.failure_code = code
        self.failure_class = failure_class
        self.revision = (
            revision
            if isinstance(revision, int)
            and not isinstance(revision, bool)
            and 0 < revision <= 999_999_999
            else None
        )
        payload: dict[str, Any] = {
            "success": False,
            "error": code,
            "error_code": code,
            "message": message,
            "retryable": failure_class == "correctable_once",
            "planning_outcome": {
                "class": failure_class,
                "code": code,
                "retryable": failure_class == "correctable_once",
            },
        }
        if self.revision is not None:
            payload["planning_outcome"]["revision"] = self.revision
        if correction is not None:
            payload["correction"] = dict(correction)
            payload["planning_outcome"]["correction"] = dict(correction)
        super().__init__(payload)

    @property
    def is_terminal(self) -> bool:
        return self.failure_class == "terminal"

    @property
    def is_human_wait(self) -> bool:
        return self.failure_class == "human_wait"

    @property
    def is_correctable_once(self) -> bool:
        return self.failure_class == "correctable_once"


def planning_failure_outcome(
    code: PlanningFailureCode,
    *,
    allowed_tools: tuple[str, ...] = (),
    revision: int | None = None,
) -> PlanningToolOutcome:
    """Build one canonical planning failure without exposing raw inputs."""

    correction = (
        correction_schema(allowed_tools=allowed_tools)
        if _CLASSES[code] == "correctable_once"
        else None
    )
    return PlanningToolOutcome(
        code=code,
        failure_class=_CLASSES[code],
        message=_MESSAGES[code],
        correction=correction,
        revision=revision,
    )


def is_planning_tool_outcome(value: object) -> bool:
    """Only a typed instance is authoritative for runtime classification."""

    return isinstance(value, PlanningToolOutcome)


__all__ = [
    "PlanningFailureClass",
    "PlanningFailureCode",
    "PlanningToolOutcome",
    "correction_schema",
    "is_planning_tool_outcome",
    "planning_failure_outcome",
]
