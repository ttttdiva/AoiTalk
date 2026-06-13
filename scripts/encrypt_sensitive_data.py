"""Encrypt legacy plaintext sensitive DB fields.

Dry-run by default. Use --apply to write encrypted values.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Iterable

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
    ProjectFact,
    RecordRow,
    TaskComment,
)
from src.security.field_crypto import (
    encrypt_json_secret_leaves,
    encrypt_text,
    is_encrypted_value,
)


TEXT_FIELDS: tuple[tuple[type[Any], str, str], ...] = (
    (ConversationSession, "_current_summary", "current_summary"),
    (ConversationMessage, "_content", "content"),
    (ConversationArchive, "_summary", "summary"),
    (ConversationHistory, "_content", "content"),
    (ContextMemory, "_content", "content"),
    (ProjectFact, "_content", "content"),
    (TaskComment, "_content", "content"),
    (KnowledgeChunk, "_text", "text"),
    (RecordRow, "_title", "title"),
    (RecordRow, "_search_text", "search_text"),
)

JSON_FIELDS: tuple[tuple[type[Any], str, str], ...] = (
    (RecordRow, "_values", "values"),
)


def _iter_rows(session, model: type[Any]) -> Iterable[Any]:
    return session.query(model).yield_per(500)


def _encrypt_text_fields(session, *, apply: bool) -> int:
    changed = 0
    for model, storage_attr, public_attr in TEXT_FIELDS:
        for row in _iter_rows(session, model):
            raw = getattr(row, storage_attr)
            if not raw or is_encrypted_value(raw):
                continue
            changed += 1
            if apply:
                setattr(row, public_attr, raw)
    return changed


def _encrypt_json_fields(session, *, apply: bool) -> int:
    changed = 0
    for model, storage_attr, public_attr in JSON_FIELDS:
        for row in _iter_rows(session, model):
            raw = getattr(row, storage_attr)
            if raw in (None, {}, []) or is_encrypted_value(raw):
                continue
            changed += 1
            if apply:
                setattr(row, public_attr, raw)
    return changed


def _encrypt_google_tokens(session, *, apply: bool) -> int:
    changed = 0
    for row in _iter_rows(session, GoogleCalendarConnection):
        for field in ("access_token", "refresh_token"):
            raw = getattr(row, field)
            if not raw or is_encrypted_value(raw):
                continue
            changed += 1
            if apply:
                setattr(
                    row,
                    field,
                    encrypt_text(
                        raw,
                        aad=f"google_calendar_connections.{field}:{row.user_id}",
                    ),
                )
    return changed


def _encrypt_app_config(session, *, apply: bool) -> int:
    row = session.get(AppConfigSetting, GLOBAL_CONFIG_KEY)
    if row is None or not isinstance(row.value, dict):
        return 0
    encrypted = encrypt_json_secret_leaves(
        row.value,
        aad_prefix="app_config_settings.value",
    )
    if encrypted == row.value:
        return 0
    if apply:
        row.value = encrypted
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write encrypted values")
    args = parser.parse_args()

    db = get_database_manager()
    with db.get_sync_session() as session:
        text_count = _encrypt_text_fields(session, apply=args.apply)
        json_count = _encrypt_json_fields(session, apply=args.apply)
        token_count = _encrypt_google_tokens(session, apply=args.apply)
        app_config_count = _encrypt_app_config(session, apply=args.apply)
        if args.apply:
            session.commit()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] text_fields={text_count}")
    print(f"[{mode}] json_fields={json_count}")
    print(f"[{mode}] google_tokens={token_count}")
    print(f"[{mode}] app_config_rows={app_config_count}")
    if not args.apply:
        print("Re-run with --apply to encrypt these values.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
