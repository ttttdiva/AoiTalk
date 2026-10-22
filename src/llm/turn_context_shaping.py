"""Within-turn outgoing context shaping for long autonomous tool loops.

A native tool loop appends every assistant tool call and tool result to its
canonical message list and historically resent that list unchanged on every
round.  Each tool result is bounded individually, but a 30+ round autonomous
task still grows the request without limit.

``WithinTurnContextShaper`` produces the *outgoing copy* for one provider
request.  It never mutates the canonical list (which remains the durable turn
transcript), never removes messages, and keeps every tool-call/result pair.
Only the text of older tool results is reduced, deterministically, with the
same local compressor used for history compaction.  It protects:

* the system prompt and every user/assistant message (only tool results are
  ever shaped, so the original request is always sent verbatim);
* the latest round (trailing tool results) and the most recent N results;
* the latest error result and the latest mutation/edit result.

Compaction is monotonic and cached by call ID: once an older result is
compacted it is sent in the same compacted form in later rounds (stable
prefix, no repeated work).  New compaction runs only when the outgoing size
crosses the trigger and then reduces to a lower target (hysteresis), so the
shaper does not re-compact on every round.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .conversation_context import (
    _history_config_bool,
    _history_config_int,
    _history_config_value,
    compact_tool_result_text,
    tool_names_by_call_id_from_messages,
)

logger = logging.getLogger(__name__)

DEFAULT_WITHIN_TURN_ENABLED = True
DEFAULT_WITHIN_TURN_TRIGGER_CHARS = 120_000
DEFAULT_WITHIN_TURN_BUDGET_RATIO = 0.6
DEFAULT_WITHIN_TURN_TARGET_RATIO = 0.6
DEFAULT_WITHIN_TURN_RESULT_MAX_CHARS = 1_600
DEFAULT_WITHIN_TURN_MIN_RESULT_CHARS = 400
DEFAULT_WITHIN_TURN_PRESERVE_RECENT = 2
# Never shrink below this, even for tiny configured/model windows; the
# shaper must not turn a working request into an unusable one.
_MIN_TRIGGER_CHARS = 16_000

_ERROR_MARKERS = (
    "error:",
    "error_code",
    '"success": false',
    '"success":false',
    "tool not found:",
    "tool execution error:",
    "traceback",
    "exception",
    "fatal",
    "not executed",
)
_MUTATION_NAME_PREFIXES = (
    "edit",
    "write",
    "update",
    "create",
    "apply",
    "append",
    "insert",
    "delete",
    "move",
    "copy",
    "rename",
    "replace",
    "patch",
    "undo",
    "docs_write",
    "docs_update",
    "docs_create",
    "docs_place",
)


def _compacted_header(original_chars: int) -> str:
    return (
        "[AoiTalk within-turn context: this earlier tool result was compacted "
        f"for this request (original {original_chars} chars). The full result "
        "is kept in the turn record; re-run the tool if exact content is "
        "needed.]\n"
    )


def _config_float(config: Any, key: str, default: float, *, low: float, high: float) -> float:
    value = _history_config_value(config, key, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    if parsed != parsed:  # NaN
        parsed = float(default)
    return min(high, max(low, parsed))


def _is_mutation_tool(tool_name: str) -> bool:
    name = str(tool_name or "").strip().casefold()
    if not name:
        return False
    try:
        from .tool_policy import FILESYSTEM_MUTATION_TOOL_NAMES

        if name in FILESYSTEM_MUTATION_TOOL_NAMES:
            return True
    except Exception:  # pragma: no cover - policy import is best-effort here
        pass
    return name.startswith(_MUTATION_NAME_PREFIXES)


def _looks_like_error(content: str) -> bool:
    head = content[:800].casefold()
    stripped = head.lstrip()
    return stripped.startswith("error") or any(marker in head for marker in _ERROR_MARKERS)


@dataclass
class WithinTurnShapingReport:
    rounds: int = 0
    compaction_passes: int = 0
    compacted_results: int = 0
    last_original_chars: int = 0
    last_outgoing_chars: int = 0
    trigger_chars: int = 0
    target_chars: int = 0


@dataclass
class _ToolEntry:
    index: int
    call_id: str
    tool_name: str
    content: str


@dataclass
class WithinTurnContextShaper:
    """Shape outgoing provider context for one native turn."""

    enabled: bool = DEFAULT_WITHIN_TURN_ENABLED
    trigger_chars: int = DEFAULT_WITHIN_TURN_TRIGGER_CHARS
    target_ratio: float = DEFAULT_WITHIN_TURN_TARGET_RATIO
    result_max_chars: int = DEFAULT_WITHIN_TURN_RESULT_MAX_CHARS
    min_result_chars: int = DEFAULT_WITHIN_TURN_MIN_RESULT_CHARS
    preserve_recent: int = DEFAULT_WITHIN_TURN_PRESERVE_RECENT
    label: str = "native"
    report: WithinTurnShapingReport = field(default_factory=WithinTurnShapingReport)
    _cache: dict[tuple[str, int], str] = field(default_factory=dict)

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        message_budget_chars: int | None = None,
        label: str = "native",
    ) -> "WithinTurnContextShaper":
        prefix = "context_compression.within_turn"
        enabled = _history_config_bool(
            config, f"{prefix}.enabled", DEFAULT_WITHIN_TURN_ENABLED
        )
        trigger = _history_config_int(
            config,
            f"{prefix}.trigger_chars",
            DEFAULT_WITHIN_TURN_TRIGGER_CHARS,
            minimum=_MIN_TRIGGER_CHARS,
        )
        ratio = _config_float(
            config,
            f"{prefix}.budget_ratio",
            DEFAULT_WITHIN_TURN_BUDGET_RATIO,
            low=0.1,
            high=1.0,
        )
        try:
            budget_chars = int(message_budget_chars or 0)
        except (TypeError, ValueError):
            budget_chars = 0
        if budget_chars > 0:
            trigger = min(trigger, max(_MIN_TRIGGER_CHARS, int(budget_chars * ratio)))
        result_max = _history_config_int(
            config,
            f"{prefix}.result_max_chars",
            DEFAULT_WITHIN_TURN_RESULT_MAX_CHARS,
            minimum=200,
        )
        min_result = min(
            result_max,
            _history_config_int(
                config,
                f"{prefix}.min_result_chars",
                DEFAULT_WITHIN_TURN_MIN_RESULT_CHARS,
                minimum=120,
            ),
        )
        return cls(
            enabled=enabled,
            trigger_chars=trigger,
            target_ratio=_config_float(
                config,
                f"{prefix}.target_ratio",
                DEFAULT_WITHIN_TURN_TARGET_RATIO,
                low=0.2,
                high=1.0,
            ),
            result_max_chars=result_max,
            min_result_chars=min_result,
            preserve_recent=_history_config_int(
                config,
                "context_compression.protect.recent_tool_results",
                DEFAULT_WITHIN_TURN_PRESERVE_RECENT,
            ),
            label=label,
        )

    # -- public API -----------------------------------------------------

    def shape_chat_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        reserved_chars: int = 0,
    ) -> list[dict[str, Any]]:
        """Return chat-completions messages to send (canonical list untouched)."""

        return self._safe_shape(
            messages,
            is_result=lambda item: str(item.get("role") or "") == "tool"
            and isinstance(item.get("content"), str),
            call_id_of=lambda item: str(item.get("tool_call_id") or ""),
            content_key="content",
            reserved_chars=reserved_chars,
        )

    def shape_responses_input(
        self,
        items: list[dict[str, Any]],
        *,
        reserved_chars: int = 0,
    ) -> list[dict[str, Any]]:
        """Return Responses ``input`` items to send (canonical list untouched)."""

        return self._safe_shape(
            items,
            is_result=lambda item: str(item.get("type") or "") == "function_call_output"
            and isinstance(item.get("output"), str),
            call_id_of=lambda item: str(item.get("call_id") or ""),
            content_key="output",
            reserved_chars=reserved_chars,
        )

    # -- internals ------------------------------------------------------

    @staticmethod
    def payload_chars(value: Any) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return len(str(value))

    def _safe_shape(self, items: list[dict[str, Any]], **options: Any) -> list[dict[str, Any]]:
        try:
            return self._shape(items, **options)
        except Exception:  # noqa: BLE001 - never block a provider request
            logger.warning(
                "within-turn context shaping failed; sending canonical context",
                exc_info=True,
            )
            return items

    def _shape(
        self,
        items: list[dict[str, Any]],
        *,
        is_result: Any,
        call_id_of: Any,
        content_key: str,
        reserved_chars: int,
    ) -> list[dict[str, Any]]:
        self.report.rounds += 1
        if not self.enabled or not items:
            return items
        names = tool_names_by_call_id_from_messages(items)
        entries = [
            _ToolEntry(
                index=index,
                call_id=call_id_of(item),
                tool_name=names.get(call_id_of(item), str(item.get("name") or "tool")),
                content=item[content_key],
            )
            for index, item in enumerate(items)
            if isinstance(item, dict) and is_result(item)
        ]
        if not entries:
            return items

        original_total = max(0, int(reserved_chars)) + sum(
            self.payload_chars(item) for item in items
        )
        total = original_total
        replacements: dict[int, str] = {}

        # Re-apply earlier compactions first so the request prefix stays
        # stable across rounds and no result is compacted twice.
        for entry in entries:
            cached = self._cache.get(self._key(entry))
            if cached is not None and len(cached) < len(entry.content):
                replacements[entry.index] = cached
                total -= len(entry.content) - len(cached)

        trigger = max(1, int(self.trigger_chars))
        target = max(1, int(trigger * self.target_ratio))
        self.report.trigger_chars = trigger
        self.report.target_chars = target
        if total > trigger:
            self.report.compaction_passes += 1
            protected = self._protected_indexes(items, entries, is_result)
            candidates = [entry for entry in entries if entry.index not in protected]
            for tier in (self.result_max_chars, self.min_result_chars):
                for entry in candidates:
                    if total <= target:
                        break
                    current = replacements.get(entry.index, entry.content)
                    header = _compacted_header(len(entry.content))
                    if len(current) <= tier + len(header):
                        continue
                    body = compact_tool_result_text(
                        tool_name=entry.tool_name,
                        content=entry.content,
                        max_chars=tier,
                    )
                    compacted = header + body
                    if len(compacted) >= len(current):
                        continue
                    if entry.index not in replacements:
                        self.report.compacted_results += 1
                    total -= len(current) - len(compacted)
                    replacements[entry.index] = compacted
                    self._cache[self._key(entry)] = compacted
                if total <= target:
                    break
            logger.info(
                "within-turn context shaped: runtime=%s round=%s original_chars=%s "
                "outgoing_chars=%s trigger=%s target=%s compacted_total=%s",
                self.label,
                self.report.rounds,
                original_total,
                total,
                trigger,
                target,
                self.report.compacted_results,
            )
        self.report.last_original_chars = original_total
        self.report.last_outgoing_chars = total
        if not replacements:
            return items
        shaped = list(items)
        for index, text in replacements.items():
            replaced = dict(items[index])
            replaced[content_key] = text
            shaped[index] = replaced
        return shaped

    @staticmethod
    def _key(entry: _ToolEntry) -> tuple[str, int]:
        # Tool result text is immutable once appended; the length guards
        # against an unexpected call-ID reuse with different content.
        return (entry.call_id or f"#{entry.index}", len(entry.content))

    def _protected_indexes(
        self,
        items: Sequence[dict[str, Any]],
        entries: Sequence[_ToolEntry],
        is_result: Any,
    ) -> set[int]:
        protected: set[int] = set()
        # Latest round: the trailing contiguous tool results.
        for index in range(len(items) - 1, -1, -1):
            item = items[index]
            if isinstance(item, dict) and is_result(item):
                protected.add(index)
                continue
            break
        if self.preserve_recent > 0:
            protected.update(entry.index for entry in entries[-self.preserve_recent :])
        for entry in reversed(entries):
            if _looks_like_error(entry.content):
                protected.add(entry.index)
                break
        for entry in reversed(entries):
            if _is_mutation_tool(entry.tool_name) and not _looks_like_error(entry.content):
                protected.add(entry.index)
                break
        return protected


def tools_payload_chars(tools_payload: Iterable[Any] | None) -> int:
    if not tools_payload:
        return 0
    return WithinTurnContextShaper.payload_chars(list(tools_payload))
