"""stream_end イベント本文から、欠落した assistant メッセージを会話へ保存し直す。

既定は dry-run（復元候補・本文プレビュー・スキップ理由の表示のみ）。--apply 指定時だけ保存する。
既に同じ AgentRun の assistant 行がある場合はスキップする（冪等）。
保存は ChatTurnPersistence.save_assistant_message（通常の assistant 保存経路）を使い、
metadata に agent_run_id と冪等キー client_message_id を付与する。
message id は stream 由来の id（未使用のUUIDに限る）、なければ run id から決定的に作る。

例:
  venv\\Scripts\\python scripts/restore_missing_assistant_reply.py --run-id 843f468b-...
  venv\\Scripts\\python scripts/restore_missing_assistant_reply.py --session-id 24661665-... --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TRUNCATION_MARKER = "\n... (truncated)"
RESTORE_NAMESPACE = uuid.UUID("5f0c8a52-3b1e-4c6e-9f7d-41a017a1c0de")


@dataclass(frozen=True)
class ReplyCandidate:
    run_id: str
    session_id: str
    content: str
    message_id: str
    source: str
    trigger_message_id: str = ""
    skip_reason: str = ""

    @property
    def client_message_id(self) -> str:
        return restore_client_message_id(self.run_id)


def restore_client_message_id(run_id: str) -> str:
    return f"restored-assistant:{run_id}"


def deterministic_message_id(run_id: str) -> str:
    return str(uuid.uuid5(RESTORE_NAMESPACE, f"assistant-reply:{run_id}"))


def _valid_uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return ""


def _event_value(event: Any, key: str) -> Any:
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)


def fold_stream_reply(events: Iterable[Any]) -> tuple[str, str, str]:
    """Return ``(content, source, stream_message_id)`` of the last streamed attempt.

    stream_end content is stored truncated to 4000 chars; when the truncation
    marker is present the full body is rebuilt from that attempt's
    stream_token events.  Returns empty content when no complete body exists.
    """

    ordered = sorted(events, key=lambda event: int(_event_value(event, "sequence") or 0))
    tokens: list[str] = []
    end_content = ""
    stream_message_id = ""
    for event in ordered:
        event_type = str(_event_value(event, "event_type") or "")
        payload = _event_value(event, "payload")
        payload = payload if isinstance(payload, dict) else {}
        for key in ("assistant_message_id", "message_id"):
            candidate = _valid_uuid(payload.get(key))
            if candidate:
                stream_message_id = candidate
        if event_type == "stream.stream_start":
            tokens = []
            end_content = ""
        elif event_type == "stream.stream_token":
            content = payload.get("content")
            if isinstance(content, str):
                tokens.append(content)
        elif event_type == "stream.stream_end":
            content = payload.get("content")
            end_content = content if isinstance(content, str) else ""
    joined = "".join(tokens)
    if end_content and not end_content.endswith(TRUNCATION_MARKER):
        return end_content, "stream_end", stream_message_id
    if end_content.endswith(TRUNCATION_MARKER):
        prefix = end_content[: -len(TRUNCATION_MARKER)].rstrip()
        if joined.strip() and joined.strip().startswith(prefix[:200].strip()):
            return joined, "stream_token", stream_message_id
        return "", "truncated", stream_message_id
    return "", "none", stream_message_id


def plan_candidate(
    *,
    run: Any,
    events: Iterable[Any],
    messages: Iterable[Any],
    taken_message_ids: Iterable[str] = (),
    allow_append: bool = False,
) -> ReplyCandidate:
    """Decide whether and how to restore one run's reply.  Pure."""

    messages = list(messages)
    run_id = str(_event_value(run, "id"))
    session_id = str(_event_value(run, "session_id") or "")
    trigger = _valid_uuid(_event_value(run, "trigger_message_id"))
    content, source, stream_message_id = fold_stream_reply(events)
    live = [m for m in messages if _event_value(m, "deleted_at") is None]
    all_ids = {str(_event_value(m, "id")) for m in messages} | {
        str(value) for value in taken_message_ids
    }

    def skip(reason: str) -> ReplyCandidate:
        return ReplyCandidate(run_id, session_id, content, "", source, trigger, reason)

    if not session_id:
        return skip("AgentRunに会話セッションがありません")
    for message in live:
        metadata = _event_value(message, "message_metadata") or {}
        if _event_value(message, "role") != "assistant":
            continue
        if (
            str(metadata.get("agent_run_id") or "") == run_id
            or _event_value(message, "client_message_id") == restore_client_message_id(run_id)
        ):
            return skip(f"assistant行は既に存在します: {_event_value(message, 'id')}")
    if not content.strip():
        return skip(
            "stream_end本文が4000文字で切り詰められており、stream_tokenから復元できません"
            if source == "truncated"
            else "stream_end本文がありません"
        )
    run_result = _event_value(run, "result")
    result_message_id = (
        _valid_uuid(run_result.get("assistant_message_id"))
        if isinstance(run_result, dict)
        else ""
    )
    message_id = next(
        (
            candidate
            for candidate in (stream_message_id, result_message_id)
            if candidate and candidate not in all_ids
        ),
        deterministic_message_id(run_id),
    )
    if message_id in all_ids:
        return skip(f"message id が既に使われています: {message_id}")
    active = [m for m in live if _event_value(m, "is_active_branch") is not False]
    latest = max(
        active,
        key=lambda m: (_event_value(m, "created_at") or datetime.min, str(_event_value(m, "id"))),
        default=None,
    )
    if trigger and latest is not None and str(_event_value(latest, "id")) != trigger and not allow_append:
        return ReplyCandidate(
            run_id,
            session_id,
            content,
            message_id,
            source,
            trigger,
            "起点のuserメッセージ以降に別メッセージがあるため末尾追加になります(--allow-append で許可)",
        )
    return ReplyCandidate(run_id, session_id, content, message_id, source, trigger)


async def save_candidate(candidate: ReplyCandidate, persistence: Any = None) -> Any:
    if persistence is None:
        from src.assistant.chat_turn_persistence import ChatTurnPersistence

        persistence = ChatTurnPersistence()
    return await persistence.save_assistant_message(
        session_id=candidate.session_id,
        content=candidate.content,
        metadata={
            "agent_run_id": candidate.run_id,
            "client_message_id": candidate.client_message_id,
            "restored_from": f"agent_run_events.{candidate.source}",
            "restored_at": datetime.now(timezone.utc).isoformat(),
        },
        sender_type="assistant",
        message_id=candidate.message_id,
    )


def _id_condition(column: Any, value: str):
    """Full UUID equality, or a prefix match for short ids such as ``843f468b``."""

    from sqlalchemy import Text, cast

    text = str(value or "").strip().lower()
    full = _valid_uuid(text)
    if full:
        return column == uuid.UUID(full)
    if len(text) < 8 or any(ch not in "0123456789abcdef-" for ch in text):
        raise ValueError(f"IDは8文字以上のUUID(接頭辞)で指定してください: {value}")
    return cast(column, Text).like(f"{text}%")


async def load_targets(session, *, session_id: Optional[str], run_id: Optional[str]):
    from sqlalchemy import select

    from src.memory.models import AgentRun, AgentRunEvent, ConversationMessage

    statement = select(AgentRun)
    if run_id:
        statement = statement.where(_id_condition(AgentRun.id, run_id))
    if session_id:
        statement = statement.where(_id_condition(AgentRun.session_id, session_id))
    runs = list((await session.execute(statement.order_by(AgentRun.created_at))).scalars().all())
    targets = []
    messages_by_session: dict[Any, list[Any]] = {}
    for run in runs:
        events = list(
            (
                await session.execute(
                    select(AgentRunEvent).where(AgentRunEvent.run_id == run.id)
                )
            ).scalars().all()
        )
        if not any(event.event_type == "stream.stream_end" for event in events):
            continue
        if run.session_id not in messages_by_session:
            messages_by_session[run.session_id] = list(
                (
                    await session.execute(
                        select(ConversationMessage).where(
                            ConversationMessage.session_id == run.session_id
                        )
                    )
                ).scalars().all()
            ) if run.session_id else []
        # conversation_messages.id is a global key: a stream-provided id that
        # already belongs to another session must not be reused.
        _content, _source, stream_id = fold_stream_reply(events)
        result_id = (
            _valid_uuid(run.result.get("assistant_message_id"))
            if isinstance(run.result, dict)
            else ""
        )
        wanted = [
            uuid.UUID(value)
            for value in {stream_id, result_id, deterministic_message_id(str(run.id))}
            if value
        ]
        taken = {
            str(value)
            for value in (
                await session.execute(
                    select(ConversationMessage.id).where(ConversationMessage.id.in_(wanted))
                )
            ).scalars().all()
        }
        targets.append((run, events, messages_by_session[run.session_id], taken))
    return targets


async def run(
    args: argparse.Namespace,
    *,
    session=None,
    persistence: Any = None,
    loader=None,
) -> int:
    owns_session = session is None
    if owns_session:
        from src.memory.database import get_database_manager

        session = await get_database_manager().get_session()
    try:
        targets = await (loader or load_targets)(
            session, session_id=args.session_id, run_id=args.run_id
        )
    finally:
        if owns_session:
            await session.close()
    if not targets:
        print("stream_end を持つ対象AgentRunがありません。")
        return 2
    restored = 0
    for run_row, events, messages, taken in targets:
        candidate = plan_candidate(
            run=run_row,
            events=events,
            messages=messages,
            taken_message_ids=taken,
            allow_append=bool(args.allow_append),
        )
        print(f"- run {candidate.run_id} / session {candidate.session_id}")
        if candidate.skip_reason:
            print(f"  skip: {candidate.skip_reason}")
            continue
        print(f"  本文: {len(candidate.content)}文字 ({candidate.source})")
        print(f"  message id: {candidate.message_id}")
        preview = candidate.content[:400].replace("\n", "\n    ")
        print(f"  プレビュー:\n    {preview}")
        if not args.apply:
            print("  dry-run: 保存していません")
            continue
        saved = await save_candidate(candidate, persistence)
        if saved is None:
            print("  保存されませんでした（保存経路が拒否）")
            return 3
        restored += 1
        print(f"  保存しました: {getattr(saved, 'id', candidate.message_id)}")
    print(f"復元: {restored}件" + ("" if args.apply else " (dry-run)"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session-id")
    parser.add_argument("--run-id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-append", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (args.session_id or args.run_id):
        parser.error("--session-id または --run-id を指定してください。")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
