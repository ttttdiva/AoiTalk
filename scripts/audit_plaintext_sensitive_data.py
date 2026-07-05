"""Audit DB fields that should not remain plaintext after encryption migration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.app_config_store import GLOBAL_CONFIG_KEY
from src.memory.database import get_database_manager
from src.memory.models import (
    AppConfigSetting,
    ContextMemory,
    ConversationArchive,
    ConversationHistory,
    ConversationMessage,
    ConversationSession,
    GoogleCalendarConnection,
    KnowledgeChunk,
    RecordRow,
    TaskComment,
)
from src.security.field_crypto import is_encrypted_value


TEXT_FIELDS: tuple[tuple[type[Any], str, str], ...] = (
    (ConversationSession, "_current_summary", "conversation_sessions.current_summary"),
    (ConversationMessage, "_content", "conversation_messages.content"),
    (ConversationArchive, "_summary", "conversation_archives.summary"),
    (ConversationHistory, "_content", "conversation_history.content"),
    (ContextMemory, "_content", "context_memories.content"),
    (TaskComment, "_content", "task_comments.content"),
    (KnowledgeChunk, "_text", "knowledge_chunks.text"),
    (RecordRow, "_title", "record_rows.title"),
    (RecordRow, "_search_text", "record_rows.search_text"),
)

JSON_FIELDS: tuple[tuple[type[Any], str, str], ...] = (
    (RecordRow, "_values", "record_rows.values"),
)


def _count_plaintext(session, model: type[Any], storage_attr: str) -> int:
    count = 0
    for row in session.query(model).yield_per(500):
        raw = getattr(row, storage_attr)
        if raw and not is_encrypted_value(raw):
            count += 1
    return count


def _has_plaintext_app_config(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("api_key", "token", "secret", "password", "credential")):
                if isinstance(child, str) and child and not is_encrypted_value(child):
                    return True
            if _has_plaintext_app_config(child):
                return True
    if isinstance(value, list):
        return any(_has_plaintext_app_config(item) for item in value)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()

    db = get_database_manager()
    failures = 0
    with db.get_sync_session() as session:
        for model, storage_attr, label in TEXT_FIELDS:
            count = _count_plaintext(session, model, storage_attr)
            print(f"{label}: plaintext={count}")
            failures += count

        for model, storage_attr, label in JSON_FIELDS:
            count = _count_plaintext(session, model, storage_attr)
            print(f"{label}: plaintext={count}")
            failures += count

        token_count = 0
        for row in session.query(GoogleCalendarConnection).yield_per(500):
            for field in ("access_token", "refresh_token"):
                raw = getattr(row, field)
                if raw and not is_encrypted_value(raw):
                    token_count += 1
        print(f"google_calendar_connections.tokens: plaintext={token_count}")
        failures += token_count

        row = session.get(AppConfigSetting, GLOBAL_CONFIG_KEY)
        app_config_plaintext = bool(row and _has_plaintext_app_config(row.value))
        print(f"app_config_settings.value secret leaves plaintext={app_config_plaintext}")
        failures += int(app_config_plaintext)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
