"""Question and answer normalization for durable AgentDialogue inputs.

The transport keeps the answer body in the encrypted ConversationMessage.  A
dialogue input only carries the reference to that message, so the service
validates the structured answer against the question message metadata before
it enters either the Human or root-Agent answer path.
"""

from __future__ import annotations

import json
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


MAX_ANSWER_CHARS = 30_000
MAX_CHOICE_CHARS = 256
MAX_CHOICES = 32
QUESTION_INPUT_TYPES = frozenset(
    {
        "free_text",
        "single_choice",
        "multi_choice",
        "yes_no",
        "choices_with_free_text",
    }
)
_ANSWER_KEYS = frozenset({"choices", "free_text"})


class AgentDialogueAnswerError(ValueError):
    """A bounded question or answer violates the shared dialogue contract."""

    def __init__(self, message: str, *, code: str = "dialogue_answer_invalid") -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AgentDialogueQuestionContract:
    input_type: str
    choices: tuple[str, ...]
    allow_multiple: bool
    allow_free_text: bool

    @property
    def has_choices(self) -> bool:
        return bool(self.choices)


@dataclass(frozen=True, slots=True)
class AgentDialogueAnswer:
    """Validated answer with its stable string representation.

    ``canonical`` is the wire value and input to the Input idempotency digest.
    ``display_text`` is the body stored in the encrypted ordinary
    ConversationMessage; it is never copied into ledger metadata.
    """

    canonical: str
    choices: tuple[str, ...]
    free_text: str | None
    display_text: str


def canonical_answer_digest(answer: AgentDialogueAnswer | str) -> str:
    """Hash only the canonical wire value used for Input idempotency."""

    canonical = answer.canonical if isinstance(answer, AgentDialogueAnswer) else str(answer)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _choice_list(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise AgentDialogueAnswerError(
            f"{field} must be a list of strings",
            code="dialogue_question_choices_invalid",
        )
    if len(value) > MAX_CHOICES:
        raise AgentDialogueAnswerError(
            f"{field} has too many choices",
            code="dialogue_question_choices_too_many",
        )
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise AgentDialogueAnswerError(
                f"{field} must contain strings",
                code="dialogue_question_choice_invalid",
            )
        item = raw.strip()
        if not item or len(item) > MAX_CHOICE_CHARS:
            raise AgentDialogueAnswerError(
                f"{field} contains an empty or oversized choice",
                code="dialogue_question_choice_invalid",
            )
        if item in seen:
            raise AgentDialogueAnswerError(
                f"{field} contains duplicate choices",
                code="dialogue_question_choices_duplicate",
            )
        seen.add(item)
        result.append(item)
    return tuple(result)


def build_agent_dialogue_question_contract(
    *,
    input_type: Any = "free_text",
    choices: Any = None,
    allow_multiple: Any = False,
    allow_free_text: Any = True,
) -> AgentDialogueQuestionContract:
    """Normalize the existing ``ask_user_question`` type and flags."""

    normalized_type = str(input_type or "free_text").strip().casefold()
    if normalized_type not in QUESTION_INPUT_TYPES:
        raise AgentDialogueAnswerError(
            "dialogue question input_type is not allowlisted",
            code="dialogue_question_input_type_invalid",
        )

    normalized_choices = _choice_list(choices, field="question choices")
    multiple = bool(allow_multiple)
    free_text = bool(allow_free_text)

    # This is the same normalization used by the ordinary question tool.
    if normalized_type == "yes_no":
        normalized_choices = ("Yes", "No")
        multiple = False
        free_text = False
    elif normalized_type == "free_text":
        if normalized_choices or multiple or not free_text:
            raise AgentDialogueAnswerError(
                "free_text questions cannot carry choices or multiple selection",
                code="dialogue_question_flags_invalid",
            )
    elif normalized_type == "single_choice":
        if not normalized_choices or multiple:
            raise AgentDialogueAnswerError(
                "single_choice question flags are invalid",
                code="dialogue_question_flags_invalid",
            )
    elif normalized_type == "multi_choice":
        if not normalized_choices or not multiple:
            raise AgentDialogueAnswerError(
                "multi_choice question flags are invalid",
                code="dialogue_question_flags_invalid",
            )
    elif normalized_type == "choices_with_free_text":
        if not normalized_choices or not free_text:
            raise AgentDialogueAnswerError(
                "choices_with_free_text question flags are invalid",
                code="dialogue_question_flags_invalid",
            )

    return AgentDialogueQuestionContract(
        input_type=normalized_type,
        choices=normalized_choices,
        allow_multiple=multiple,
        allow_free_text=free_text,
    )


def question_contract_from_message(message: Any, *, input_kind: Any = None) -> AgentDialogueQuestionContract:
    """Read the authorized question metadata from an encrypted message row."""

    metadata = getattr(message, "message_metadata", None)
    metadata = metadata if isinstance(metadata, Mapping) else {}
    input_type = metadata.get("input_type")
    # ``input_kind`` is the durable routing kind, not the UI question type.
    # Never use agent_question/human_question as an input_type fallback.
    if not input_type:
        input_type = "free_text"
    return build_agent_dialogue_question_contract(
        input_type=input_type,
        choices=metadata.get("choices"),
        allow_multiple=metadata.get("allow_multiple", False),
        allow_free_text=metadata.get("allow_free_text", True),
    )


def parse_agent_dialogue_answer(
    answer: Any,
    contract: AgentDialogueQuestionContract,
) -> AgentDialogueAnswer:
    """Validate and canonicalize one HTTP or root-Agent answer.

    Choice-bearing questions require the one shared JSON-in-string encoding::

        {"choices":["a","b"],"free_text":"optional"}

    A free-text-only question retains the historical plain string form.  No
    answer body is copied into a delivery ledger or metadata field.
    """

    if not isinstance(answer, str):
        raise AgentDialogueAnswerError("dialogue answer must be a string", code="dialogue_answer_invalid")
    raw = answer.strip()
    if not raw:
        raise AgentDialogueAnswerError("dialogue answer is required", code="dialogue_answer_required")
    if len(raw) > MAX_ANSWER_CHARS:
        raise AgentDialogueAnswerError("dialogue answer is too long", code="dialogue_answer_too_long")

    if not contract.has_choices:
        if not contract.allow_free_text:
            raise AgentDialogueAnswerError(
                "free-text answer is not allowed",
                code="dialogue_free_text_not_allowed",
            )
        return AgentDialogueAnswer(
            canonical=raw,
            choices=(),
            free_text=raw,
            display_text=raw,
        )

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AgentDialogueAnswerError(
            "choice answers must use the canonical JSON string encoding",
            code="dialogue_answer_json_required",
        ) from exc
    if not isinstance(payload, Mapping) or set(payload) - _ANSWER_KEYS or "choices" not in payload:
        raise AgentDialogueAnswerError(
            "choice answer object has invalid fields",
            code="dialogue_answer_shape_invalid",
        )

    selected = _choice_list(payload.get("choices"), field="answer choices")
    if len(selected) > (1 if not contract.allow_multiple else MAX_CHOICES):
        raise AgentDialogueAnswerError(
            "multiple choices are not allowed for this question",
            code="dialogue_answer_multiple_not_allowed",
        )
    allowed = set(contract.choices)
    if any(item not in allowed for item in selected):
        raise AgentDialogueAnswerError(
            "answer contains a choice that was not offered",
            code="dialogue_answer_choice_not_allowed",
        )

    free_text_value = payload.get("free_text")
    if free_text_value is not None and not isinstance(free_text_value, str):
        raise AgentDialogueAnswerError(
            "free_text must be a string",
            code="dialogue_answer_free_text_invalid",
        )
    free_text = free_text_value.strip() if isinstance(free_text_value, str) else ""
    if len(free_text) > MAX_ANSWER_CHARS:
        raise AgentDialogueAnswerError(
            "free_text answer is too long",
            code="dialogue_answer_too_long",
        )
    if free_text and not contract.allow_free_text:
        raise AgentDialogueAnswerError(
            "free-text answer is not allowed",
            code="dialogue_free_text_not_allowed",
        )
    if contract.input_type == "yes_no" and (len(selected) != 1 or free_text):
        raise AgentDialogueAnswerError(
            "yes_no answer must select exactly one choice",
            code="dialogue_yes_no_invalid",
        )
    if not selected and not free_text:
        raise AgentDialogueAnswerError(
            "at least one choice or free-text answer is required",
            code="dialogue_answer_required",
        )

    canonical_payload: dict[str, Any] = {"choices": list(selected)}
    if free_text:
        canonical_payload["free_text"] = free_text
    canonical = json.dumps(canonical_payload, ensure_ascii=False, separators=(",", ":"))
    display_parts: list[str] = []
    if selected:
        display_parts.append("選択: " + "、".join(selected))
    if free_text:
        display_parts.append("自由入力: " + free_text)
    return AgentDialogueAnswer(
        canonical=canonical,
        choices=selected,
        free_text=free_text or None,
        display_text="\n".join(display_parts),
    )


__all__ = [
    "AgentDialogueAnswer",
    "AgentDialogueAnswerError",
    "AgentDialogueQuestionContract",
    "MAX_ANSWER_CHARS",
    "MAX_CHOICE_CHARS",
    "MAX_CHOICES",
    "QUESTION_INPUT_TYPES",
    "build_agent_dialogue_question_contract",
    "canonical_answer_digest",
    "parse_agent_dialogue_answer",
    "question_contract_from_message",
]
