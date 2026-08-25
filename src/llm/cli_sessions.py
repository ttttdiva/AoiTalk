"""Durable mapping between AoiTalk branches and native CLI sessions.

The conversation database remains the source of truth for messages.  This
module only stores the provider-owned continuation handle and a short-lived
generation lease in ``ConversationSession.context``.  Keeping the mapping in
the existing JSON metadata avoids a schema migration while still making the
state available after a worker or application restart.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select

from ..memory.database import get_db_session
from ..memory.models import ConversationMessage, ConversationSession

logger = logging.getLogger(__name__)

CLI_NATIVE_SESSIONS_CONTEXT_KEY = "cli_native_sessions"
DEFAULT_LEASE_SECONDS = 45 * 60


class CLISessionBusyError(RuntimeError):
    """A native CLI session is already being used by another generation."""


@dataclass(frozen=True)
class CLINativeSessionScope:
    """All values that must match before a provider session can be resumed."""

    chat_session_id: str
    branch_key: str
    provider: str
    model: str
    project_key: str
    working_directory: str
    fingerprint: str

    @property
    def scope_key(self) -> str:
        payload = {
            "chat_session_id": self.chat_session_id,
            "branch_key": self.branch_key,
            "provider": self.provider,
            "model": self.model,
            "project_key": self.project_key,
            "working_directory": self.working_directory,
            "fingerprint": self.fingerprint,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def as_dict(self) -> dict[str, str]:
        return {
            "chat_session_id": self.chat_session_id,
            "branch_key": self.branch_key,
            "provider": self.provider,
            "model": self.model,
            "project_key": self.project_key,
            "working_directory": self.working_directory,
            "fingerprint": self.fingerprint,
            "scope_key": self.scope_key,
        }


@dataclass(frozen=True)
class CLISessionLease:
    """The local decision made while holding the database row lock."""

    scope: CLINativeSessionScope
    generation_id: str
    action: str  # ``start`` or ``resume``
    native_session_id: Optional[str]
    recreated: bool = False


def mask_native_session_id(value: Any) -> Optional[str]:
    """Return a log/UI-safe representation of a provider session id."""

    text = str(value or "").strip()
    if not text:
        return None
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}…{text[-4:]}"


def fingerprint_settings(value: Any) -> str:
    """Create a stable, non-secret fingerprint for native-session settings."""

    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        serialized = repr(value)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _entries_from_context(context: Any) -> list[dict[str, Any]]:
    if not isinstance(context, dict):
        return []
    raw = context.get(CLI_NATIVE_SESSIONS_CONTEXT_KEY)
    if not isinstance(raw, list):
        return []
    return [copy.deepcopy(item) for item in raw if isinstance(item, dict)]


def _set_entries(context: Any, entries: list[dict[str, Any]]) -> dict[str, Any]:
    next_context = copy.deepcopy(context) if isinstance(context, dict) else {}
    # Keep this bounded. Old scopes are still invalidated by scope mismatch,
    # but stale historical handles must not make the session JSON unbounded.
    entries = sorted(
        entries,
        key=lambda item: str(item.get("last_used_at") or item.get("created_at") or ""),
        reverse=True,
    )[:16]
    next_context[CLI_NATIVE_SESSIONS_CONTEXT_KEY] = entries
    return next_context


def _entry_matches(entry: dict[str, Any], scope: CLINativeSessionScope) -> bool:
    return str(entry.get("scope_key") or "") == scope.scope_key


async def resolve_branch_key(
    chat_session_id: str,
    *,
    current_edit_message_id: Optional[str] = None,
) -> str:
    """Resolve a stable identity for the currently active conversation path."""

    if current_edit_message_id and not chat_session_id:
        return f"edit:{current_edit_message_id}"
    try:
        session_uuid = uuid.UUID(str(chat_session_id))
    except (TypeError, ValueError):
        return f"head:{current_edit_message_id or 'unknown'}"

    async with await get_db_session() as db:
        result = await db.execute(
            select(
                ConversationMessage.id,
                ConversationMessage.branch_index,
            )
            .where(
                ConversationMessage.session_id == session_uuid,
                ConversationMessage.is_active_branch.is_(True),
            )
            .order_by(ConversationMessage.created_at, ConversationMessage.id)
        )
        rows = list(result.all())

    if not rows:
        return f"head:{current_edit_message_id or 'empty'}"
    # A branch created by editing has a non-zero sibling index. Its first
    # divergent message remains stable as later turns are appended.
    for message_id, branch_index in rows:
        if int(branch_index or 0) > 0:
            return f"branch:{message_id}"
    return f"root:{rows[0][0]}"


class CLISessionStore:
    """Row-locked persistence and lease operations for native CLI sessions."""

    def __init__(self, *, lease_seconds: int = DEFAULT_LEASE_SECONDS):
        self.lease_seconds = max(30, int(lease_seconds))

    async def acquire(
        self,
        scope: CLINativeSessionScope,
        *,
        force_new: bool = False,
    ) -> CLISessionLease:
        generation_id = str(uuid.uuid4())
        now = _utc_now()
        expires = now + timedelta(seconds=self.lease_seconds)
        async with await get_db_session() as db:
            result = await db.execute(
                select(ConversationSession)
                .where(ConversationSession.id == uuid.UUID(scope.chat_session_id))
                .with_for_update()
            )
            conversation = result.scalar_one_or_none()
            if conversation is None:
                raise ValueError(f"Conversation session not found: {scope.chat_session_id}")

            context = copy.deepcopy(conversation.context or {})
            entries = _entries_from_context(context)
            entry = next((item for item in entries if _entry_matches(item, scope)), None)
            had_previous_native_session = bool(
                entry and str(entry.get("native_session_id") or "").strip()
            )
            if entry is None:
                entry = {
                    **scope.as_dict(),
                    "native_session_id": None,
                    "state": "starting",
                    "created_at": _iso(now),
                    "last_used_at": _iso(now),
                }
                entries.append(entry)
            else:
                active_generation = str(entry.get("active_generation_id") or "")
                lease_expires = _parse_iso(entry.get("lease_expires_at"))
                if (
                    active_generation
                    and active_generation != generation_id
                    and lease_expires is not None
                    and lease_expires > now
                ):
                    raise CLISessionBusyError(
                        "The AoiTalk conversation is already using its CLI session"
                    )
                if force_new:
                    entry["native_session_id"] = None
                    entry["state"] = "invalidated"
                entry.update(scope.as_dict())
                entry["last_used_at"] = _iso(now)

            native_session_id = (
                None if force_new else str(entry.get("native_session_id") or "").strip() or None
            )
            action = "resume" if native_session_id else "start"
            entry["state"] = "resuming" if action == "resume" else "starting"
            entry["active_generation_id"] = generation_id
            entry["lease_expires_at"] = _iso(expires)
            entry["last_error"] = None
            conversation.context = _set_entries(context, entries)
            await db.commit()

        return CLISessionLease(
            scope=scope,
            generation_id=generation_id,
            action=action,
            native_session_id=native_session_id,
            recreated=bool(force_new and had_previous_native_session),
        )

    async def record_native_session(
        self,
        lease: CLISessionLease,
        native_session_id: Optional[str],
    ) -> bool:
        """Persist a provider-confirmed id; never invent one when absent."""

        normalized = str(native_session_id or "").strip() or None
        async with await get_db_session() as db:
            result = await db.execute(
                select(ConversationSession)
                .where(ConversationSession.id == uuid.UUID(lease.scope.chat_session_id))
                .with_for_update()
            )
            conversation = result.scalar_one_or_none()
            if conversation is None:
                return False
            context = copy.deepcopy(conversation.context or {})
            entries = _entries_from_context(context)
            entry = next((item for item in entries if _entry_matches(item, lease.scope)), None)
            if entry is None:
                return False
            owner = str(entry.get("active_generation_id") or "")
            if owner and owner != lease.generation_id:
                raise CLISessionBusyError("CLI session lease ownership changed")
            entry["native_session_id"] = normalized
            entry["state"] = "active" if normalized else "unsupported"
            entry["last_used_at"] = _iso(_utc_now())
            entry["last_error"] = None if normalized else "provider_did_not_return_session_id"
            conversation.context = _set_entries(context, entries)
            await db.commit()
            return bool(normalized)

    async def invalidate(
        self,
        lease: CLISessionLease,
        *,
        reason: str,
    ) -> bool:
        """Unlink a stale/failed provider session before recreating it."""

        async with await get_db_session() as db:
            result = await db.execute(
                select(ConversationSession)
                .where(ConversationSession.id == uuid.UUID(lease.scope.chat_session_id))
                .with_for_update()
            )
            conversation = result.scalar_one_or_none()
            if conversation is None:
                return False
            context = copy.deepcopy(conversation.context or {})
            entries = _entries_from_context(context)
            entry = next((item for item in entries if _entry_matches(item, lease.scope)), None)
            if entry is None:
                return False
            entry["native_session_id"] = None
            entry["state"] = "invalid"
            entry["last_error"] = str(reason or "resume_failed")[:500]
            entry["active_generation_id"] = None
            entry["lease_expires_at"] = None
            entry["last_used_at"] = _iso(_utc_now())
            conversation.context = _set_entries(context, entries)
            await db.commit()
            return True

    async def release(self, lease: CLISessionLease) -> bool:
        """Release only this generation's lease; do not release another worker."""

        async with await get_db_session() as db:
            result = await db.execute(
                select(ConversationSession)
                .where(ConversationSession.id == uuid.UUID(lease.scope.chat_session_id))
                .with_for_update()
            )
            conversation = result.scalar_one_or_none()
            if conversation is None:
                return False
            context = copy.deepcopy(conversation.context or {})
            entries = _entries_from_context(context)
            entry = next((item for item in entries if _entry_matches(item, lease.scope)), None)
            if entry is None:
                return False
            if str(entry.get("active_generation_id") or "") == lease.generation_id:
                entry["active_generation_id"] = None
                entry["lease_expires_at"] = None
                entry["state"] = "active" if entry.get("native_session_id") else "invalid"
                entry["last_used_at"] = _iso(_utc_now())
                conversation.context = _set_entries(context, entries)
                await db.commit()
            return True
