"""ツール往復の合間に出る assistant_text / thinking のストリームイベント発行を共通化する。

各プロバイダーエンジンは同期コードから ``stream_callback``（同期・非同期どちらも来る）
を呼ぶ必要があるため、ここで同期 emitter への正規化と payload 組み立てをまとめる。

イベント契約（native_runtime と共有）:
- ``assistant_text``: ``{"text": str, "round": int}``
  ツール呼び出しを伴うラウンドの通常テキストだけを配信する。最終回答は従来の
  最終出力経路（stream_end など）に任せ、ここでは発行しない。
- ``thinking``: ``{"text": str, "kind": "summary"|"raw", "round": int}``
- ``tool_start`` / ``tool_end``: native_runtime と同じ payload 形式。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from typing import Any, Callable, Optional

from .generation_cancellation import (
    GenerationInterrupted,
    raise_if_generation_interrupted,
)

logger = logging.getLogger(__name__)

SyncStreamEmitter = Callable[[str, dict[str, Any]], None]

ASSISTANT_TEXT_EVENT = "assistant_text"
THINKING_EVENT = "thinking"
TOOL_START_EVENT = "tool_start"
TOOL_END_EVENT = "tool_end"

# OpenAI互換サーバーが reasoning を返す時のフィールド名候補。
REASONING_MESSAGE_FIELDS = (
    "reasoning_content",
    "reasoning",
    "thinking_content",
    "thinking",
)

_THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


# worker thread から本来のループへ投げた awaitable の完了待ち上限（秒）。
DISPATCH_TIMEOUT_SECONDS = 30.0


async def _await_value(value: Any) -> Any:
    return await value


def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class LoopBoundStreamCallback:
    """async 文脈で捕捉したループを保持したまま worker thread へ渡せる stream_callback。

    ``asyncio.to_thread`` / ``run_in_executor`` の内側で emitter を作ると
    実行中ループが見えず、DB書き込みやWS送信を含むコールバックを
    使い捨ての ``asyncio.run`` で回してしまう。捕捉したループを持ち回り、
    ``run_coroutine_threadsafe`` で本来のループへ戻すためのラッパー。
    """

    __slots__ = ("callback", "loop")

    def __init__(self, callback: Any, loop: asyncio.AbstractEventLoop) -> None:
        self.callback = callback
        self.loop = loop

    def __call__(self, event_type: str, data: dict[str, Any]) -> Any:
        return self.callback(event_type, data)


def bind_stream_callback_loop(stream_callback: Any) -> Any:
    """現在の実行ループを stream_callback へ束ねる。async 文脈からのみ有効。

    ループを捕捉できない（同期文脈）場合は元の callback をそのまま返すため、
    既存の同期呼び出し経路の挙動は変わらない。
    """
    if stream_callback is None:
        return None
    if isinstance(stream_callback, LoopBoundStreamCallback):
        return stream_callback
    loop = _running_loop()
    if loop is None:
        return stream_callback
    return LoopBoundStreamCallback(stream_callback, loop)


def _dispatch_awaitable(
    awaitable: Any,
    captured_loop: Optional[asyncio.AbstractEventLoop] = None,
) -> None:
    """同期文脈から返された awaitable を、適切なループ上で完了させる。"""
    running_loop = _running_loop()
    if running_loop is not None:
        # 捕捉ループ上で実行中なら run_coroutine_threadsafe はデッドロックするため、
        # 従来どおり実行中ループへタスク投入する。
        running_loop.create_task(_await_value(awaitable))
        return

    if captured_loop is not None and captured_loop.is_running():
        # worker thread からは本来のループへ投げ、完了まで待つ。
        future = asyncio.run_coroutine_threadsafe(
            _await_value(awaitable),
            captured_loop,
        )
        try:
            future.result(timeout=DISPATCH_TIMEOUT_SECONDS)
        except GenerationInterrupted:
            raise
        except Exception:
            logger.debug(
                "[TurnStreamEvents] dispatch to captured loop failed",
                exc_info=True,
            )
        return

    asyncio.run(_await_value(awaitable))


def make_sync_stream_emitter(
    stream_callback: Any,
    *,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> Optional[SyncStreamEmitter]:
    """stream_callback を同期呼び出し可能な emitter へ変換する。

    ``stream_callback`` が None の場合は None を返し、呼び出し側は従来動作のままとなる。
    ``loop`` 未指定時は ``bind_stream_callback_loop`` が束ねたループ、
    無ければ構築時点の実行ループを捕捉する。
    """
    if stream_callback is None:
        return None

    captured_loop = (
        loop
        or getattr(stream_callback, "loop", None)
        or _running_loop()
    )

    def _emit(event_type: str, data: dict[str, Any]) -> None:
        try:
            raise_if_generation_interrupted()
            result = stream_callback(event_type, data)
            if inspect.isawaitable(result):
                _dispatch_awaitable(result, captured_loop)
            raise_if_generation_interrupted()
        except GenerationInterrupted:
            # This exception is the control-flow signal that lets the response
            # handler discard the old attempt and regenerate.  Do not turn it
            # into a provider fallback response.
            raise
        except Exception:
            logger.debug(
                "[TurnStreamEvents] stream callback failed for %s",
                event_type,
                exc_info=True,
            )

    return _emit


def emit_assistant_text(
    emitter: Optional[SyncStreamEmitter],
    text: Any,
    *,
    round_index: int,
    tool_names: Any = (),
) -> bool:
    """ツール往復ラウンドの通常テキストを配信する。空文字なら何もしない。"""
    if emitter is None:
        return False
    value = strip_leading_think_markup(str(text or "")).strip()
    if not value or looks_like_tool_call_echo(value, tool_names):
        return False
    emitter(ASSISTANT_TEXT_EVENT, {"text": value, "round": int(round_index)})
    return True


def emit_thinking(
    emitter: Optional[SyncStreamEmitter],
    text: Any,
    *,
    round_index: int,
    kind: str = "raw",
) -> bool:
    """thinking（生の思考 or サマリー）を配信する。空文字なら何もしない。"""
    if emitter is None:
        return False
    value = str(text or "").strip()
    if not value:
        return False
    emitter(
        THINKING_EVENT,
        {
            "text": value,
            "kind": kind if kind in {"summary", "raw"} else "raw",
            "round": int(round_index),
        },
    )
    return True


def emit_tool_start(
    emitter: Optional[SyncStreamEmitter],
    *,
    tool: str,
    arguments: dict[str, Any] | None = None,
    operation_id: str = "",
    message: str = "",
) -> bool:
    if emitter is None:
        return False
    payload: dict[str, Any] = {
        "tool": tool,
        "tool_args": dict(arguments or {}),
        "message": message or f"{tool} を実行しています",
    }
    if operation_id:
        payload["operation_id"] = operation_id
    emitter(TOOL_START_EVENT, payload)
    return True


def emit_tool_end(
    emitter: Optional[SyncStreamEmitter],
    *,
    tool: str,
    arguments: dict[str, Any] | None = None,
    output: str = "",
    error: str = "",
    operation_id: str = "",
    message: str = "",
) -> bool:
    if emitter is None:
        return False
    tool_result: dict[str, Any] = {
        "tool": tool,
        "arguments": dict(arguments or {}),
        "output": output,
        "error": error,
    }
    payload: dict[str, Any] = {
        "tool": tool,
        "tool_args": dict(arguments or {}),
        "message": message or f"{tool} の実行が完了しました",
        "tool_result": tool_result,
    }
    if operation_id:
        payload["operation_id"] = operation_id
        tool_result["tool_call_id"] = operation_id
    emitter(TOOL_END_EVENT, payload)
    return True


def message_field_value(message: Any, field: str) -> Any:
    """dict / pydantic / SimpleNamespace いずれのメッセージからも属性を取り出す。"""
    if isinstance(message, dict):
        return message.get(field)
    value = getattr(message, field, None)
    if value is not None:
        return value
    model_dump = getattr(message, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
        except Exception:
            return None
        if isinstance(dumped, dict):
            return dumped.get(field)
    return None


def reasoning_text_from_message(message: Any) -> str:
    """OpenAI互換メッセージの reasoning 系フィールドからテキストを取り出す。"""
    if message is None:
        return ""
    for field in REASONING_MESSAGE_FIELDS:
        value = message_field_value(message, field)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            joined = "\n".join(
                str(item.get("text") or "") if isinstance(item, dict) else str(item or "")
                for item in value
            ).strip()
            if joined:
                return joined
    return ""


def think_block_text(text: Any) -> str:
    """``<think>...</think>`` 形式で埋め込まれた思考テキストを取り出す。"""
    if not isinstance(text, str) or "<think>" not in text.lower():
        return ""
    blocks = [block.strip() for block in _THINK_BLOCK_RE.findall(text)]
    return "\n".join(block for block in blocks if block).strip()


def strip_leading_think_markup(text: Any) -> str:
    """本文先頭の ``<think>...</think>`` を取り除く。

    thinking イベントとして配信済みの思考を本文へ二重表示しないために使う。
    """
    value = str(text or "").strip()
    while value.startswith("</think>"):
        value = value[len("</think>") :].lstrip()
    if value.startswith("<think>") and "</think>" in value:
        value = value.split("</think>", 1)[1].lstrip()
    return value


_TOOL_CALL_ECHO_PREFIX_RE = re.compile(
    r"^\s*(?:【\s*ツール[^】]{0,12}】|\[\s*ツール[^\]]{0,12}\]|<\s*/?\s*tool_call\s*>"
    r"|(?:tool[ _]?call|function[ _]?call|ツール(?:実行|呼び出し))\s*[:：])",
    re.IGNORECASE,
)


_NATIVE_TOOL_CALL_START = "<|tool_call_start|>"
_NATIVE_TOOL_CALL_END = "<|tool_call_end|>"
_NATIVE_TOOL_CALL_BLOCK_RE = re.compile(
    re.escape(_NATIVE_TOOL_CALL_START) + r".*?" + re.escape(_NATIVE_TOOL_CALL_END),
    re.DOTALL,
)


def _is_native_special_token_tool_call(value: str) -> bool:
    """Return whether text is only special-token wrapped tool calls.

    LFM2/LFM2.5 chat templates wrap tool calls in the control tokens
    ``<|tool_call_start|>[fn(arg=...)]<|tool_call_end|>``.  When the provider
    did not parse them (for example a plain stream without ``tools``), the
    control tokens arrive as text.  They are never user-facing prose, so the
    wrapper itself is authoritative regardless of the called name.  A
    trailing unterminated block (output cut by the token limit) is included.
    """

    if not value.startswith(_NATIVE_TOOL_CALL_START):
        return False
    remainder = _NATIVE_TOOL_CALL_BLOCK_RE.sub("", value).strip()
    if not remainder:
        return True
    return (
        remainder.startswith(_NATIVE_TOOL_CALL_START)
        and _NATIVE_TOOL_CALL_END not in remainder
    )


def _call_suffix_state(value: str) -> str:
    """Classify a parenthesized/object argument suffix for stream guards."""

    text = str(value or "").lstrip()
    if not text or text[0] not in "({[":
        return "invalid"
    closing = {"(": ")", "{": "}", "[": "]"}
    stack: list[str] = []
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'", "`"}:
            quote = char
            continue
        if char in closing:
            stack.append(closing[char])
            continue
        if char in ")}]":
            if not stack or char != stack.pop():
                return "invalid"
            if not stack:
                return "complete" if not text[index + 1 :].strip() else "trailing"
    return "incomplete"


def _has_complete_call_suffix(value: str) -> bool:
    """Return whether a parenthesized/object argument suffix is complete.

    Stream chunks frequently end in the middle of a generated call.  The
    complete echo detector must wait for the closing delimiter so a normal
    sentence beginning with a registered tool name is not discarded.
    """

    return _call_suffix_state(value) == "complete"


def _has_incomplete_call_suffix(value: str) -> bool:
    """Return whether an argument opener has no closing delimiter yet."""

    return _call_suffix_state(value) == "incomplete"


def _echo_tool_name_allowed(name: str, names: set[str]) -> bool:
    """Apply registered-name authority, with the legacy empty-set fallback."""

    if name in names:
        return True
    # A few legacy event callers cannot expose a registry.  Keep their
    # marker+``tool_name`` guard for unmistakable underscore-style names, but
    # never treat arbitrary prose or an unknown JSON object as a tool echo.
    return not names and "_" in name


def looks_like_tool_call_echo(text: Any, tool_names: Any = ()) -> bool:
    """Return true when text is only a generated tool-call/log echo.

    This detector is shared by streamed assistant events and provider final
    answer guards.  It deliberately accepts long JSON arguments while
    requiring a call-shaped remainder for long marker-prefixed text.
    """

    value = strip_leading_think_markup(str(text or "")).strip()
    if not value:
        return False
    if _is_native_special_token_tool_call(value):
        return True
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        payload = None
    names = {str(name) for name in tool_names or () if name}
    if isinstance(payload, dict) and isinstance(payload.get("name"), str) and (
        "arguments" in payload or "parameters" in payload
    ):
        return bool(names) and payload["name"] in names
    if isinstance(payload, list) and payload and all(
        isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and ("arguments" in item or "parameters" in item)
        for item in payload
    ):
        return bool(names) and all(item["name"] in names for item in payload)
    prefix_match = _TOOL_CALL_ECHO_PREFIX_RE.match(value)
    if prefix_match:
        remainder = value[prefix_match.end() :].lstrip()
        if not remainder:
            return False
        # Some providers put the normal registered JSON shape after a marker.
        # Reuse the strict JSON path above, without making marker prose an
        # unconditional echo.
        if remainder[:1] in "{[":
            try:
                marked_payload = json.loads(remainder)
            except (TypeError, ValueError):
                marked_payload = None
            if isinstance(marked_payload, dict) and isinstance(
                marked_payload.get("name"), str
            ):
                return bool(names) and marked_payload["name"] in names and (
                    "arguments" in marked_payload
                    or "parameters" in marked_payload
                )
            if isinstance(marked_payload, list) and marked_payload and all(
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and ("arguments" in item or "parameters" in item)
                for item in marked_payload
            ):
                return bool(names) and all(item["name"] in names for item in marked_payload)
            return False
        name_match = re.fullmatch(
            r"`?([A-Za-z_][A-Za-z0-9_.-]*)`?\s*(.*)",
            remainder,
            flags=re.DOTALL,
        )
        if not name_match:
            return False
        name, tail = name_match.groups()
        if not _echo_tool_name_allowed(name, names):
            return False
        tail = tail.lstrip()
        # A marker followed by the exact registered name is the final echo
        # shape seen from local models (for example ``【ツール実行】 media_search``).
        return not tail or _has_complete_call_suffix(tail)
    match = re.match(
        r"^`?([A-Za-z_][A-Za-z0-9_.-]*)`?\s*(.*)$",
        value,
        flags=re.DOTALL,
    )
    if match and _echo_tool_name_allowed(match.group(1), names):
        rest = match.group(2).strip()
        return not rest or _has_complete_call_suffix(rest)
    return False


def thinking_text_from_message(message: Any) -> str:
    """reasoning フィールド優先、無ければ本文中の ``<think>`` ブロックを思考として扱う。"""
    reasoning = reasoning_text_from_message(message)
    if reasoning:
        return reasoning
    content = message_field_value(message, "content")
    return think_block_text(content)
