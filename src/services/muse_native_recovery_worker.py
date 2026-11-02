"""Lifespan-owned recovery for native Muse and Grok Chat receipt rows.

The existing history endpoints call ``recover_muse_chat_history`` on demand.
This worker adds process-lifetime polling with an age-aware Muse cadence and
resumes journaled turns after server startup, while delegating persistence and
delivery acknowledgement to that same owner-scoped, idempotent recovery path.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Any, Callable

from .agent_link.contracts import Channel
from .agent_link.journal import SQLiteAgentLinkJournal
from .grok_bot_chat_service import (
    GrokBotChatSettings,
    GrokBotReceiptStore,
    RECOVERY_WINDOW_SECONDS as GROK_RECEIPT_RECOVERY_WINDOW_SECONDS,
    resolve_grok_bot_chat_settings,
)
from .muse_chat_service import (
    RECOVERY_REQUEST_TIMEOUT_SECONDS,
    resolve_muse_chat_settings,
)

logger = logging.getLogger(__name__)

_RECOVERY_LIMIT = 1000
_GROK_RECOVERY_WINDOW_SECONDS = GROK_RECEIPT_RECOVERY_WINDOW_SECONDS

# Muse recovery is intentionally chat scoped.  The shortest interval required
# by any live row in a Chat selects its cadence: new work is checked
# frequently, while a long-running remote generation does not cause a busy loop.
# The process tick remains short whenever *any* pending row exists so a
# newly-created Chat is noticed promptly even when another Chat is in an older
# bucket.
_MUSE_PENDING_TICK_SECONDS = 5.0
_NO_PENDING_TICK_SECONDS = 30.0
_MUSE_RECOVERY_AGE_BUCKETS = (
    (120.0, 5.0),
    (1800.0, 15.0),
    (7200.0, 60.0),
)


def _muse_recovery_interval(row_age_seconds: float) -> float:
    """Return the per-Chat recovery interval for a row's age."""

    try:
        age = max(0.0, float(row_age_seconds))
    except (TypeError, ValueError, OverflowError):
        age = 0.0
    for upper_bound, interval in _MUSE_RECOVERY_AGE_BUCKETS:
        if age <= upper_bound:
            return interval
    return 300.0


class NativeChatRecoveryWorker:
    """Resume native agent-link turns and persist replies into owned Chats."""

    def __init__(
        self,
        config: Any,
        *,
        journal_factory: Callable[[], SQLiteAgentLinkJournal] | None = None,
        repository_factory: Callable[[], Any] | None = None,
        settings_resolver: Callable[[Any], tuple[Any, Any]] | None = None,
        grok_settings_resolver: Callable[[Any], tuple[Any, Any]] | None = None,
        grok_store_factory: Callable[[Path], Any] | None = None,
        recovery_callback: Callable[..., Any] | None = None,
        grok_recovery_callback: Callable[..., Any] | None = None,
        wall_clock: Callable[[], float] = time.time,
        recovery_timeout_seconds: float = RECOVERY_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config
        self._journal_factory = journal_factory or SQLiteAgentLinkJournal
        self._repository_factory = repository_factory
        self._settings_resolver = settings_resolver or resolve_muse_chat_settings
        self._grok_settings_resolver = (
            grok_settings_resolver or resolve_grok_bot_chat_settings
        )
        self._grok_store_factory = grok_store_factory or GrokBotReceiptStore
        self._recovery_callback = recovery_callback
        self._grok_recovery_callback = grok_recovery_callback
        self._wall_clock = wall_clock
        try:
            timeout = float(recovery_timeout_seconds)
        except (TypeError, ValueError, OverflowError):
            timeout = RECOVERY_REQUEST_TIMEOUT_SECONDS
        self._recovery_timeout_seconds = max(0.1, timeout)
        self._stop_event: asyncio.Event | None = None
        self._muse_last_attempt_at: dict[tuple[str, str], float] = {}

    def _thread_mappings(
        self, path: Path
    ) -> dict[tuple[str, str], list[str]]:
        try:
            with sqlite3.connect(str(path), timeout=5.0) as connection:
                rows = connection.execute(
                    "SELECT owner_id, chat_id, thread_ref FROM agent_link_threads"
                ).fetchall()
        except sqlite3.Error:
            return {}
        mappings: dict[tuple[str, str], list[str]] = {}
        for owner_id, chat_id, thread_ref in rows:
            if all(isinstance(item, str) and item for item in (owner_id, chat_id, thread_ref)):
                mappings.setdefault((owner_id, thread_ref), []).append(chat_id)
        return mappings

    async def _repository(self) -> Any:
        if self._repository_factory is not None:
            value = self._repository_factory()
        else:
            from ..memory.conversation_repository import ConversationRepository

            value = ConversationRepository()
        if hasattr(value, "__await__"):
            return await value
        return value

    async def _recover(self, repository: Any, owner_id: str, chat_id: str) -> None:
        session = await repository.get_session_by_id(chat_id)
        if session is None or str(getattr(session, "user_id", "")) != owner_id:
            return
        if getattr(session, "deleted_at", None) is not None:
            return
        if self._recovery_callback is not None:
            result = self._recovery_callback(
                self.config,
                session=session,
                requester_id=owner_id,
                repository=repository,
            )
        else:
            from .muse_chat_service import recover_muse_chat_history

            result = recover_muse_chat_history(
                self.config,
                session=session,
                requester_id=owner_id,
                repository=repository,
            )
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, timeout=self._recovery_timeout_seconds)

    async def _recover_grok(
        self, repository: Any, settings: GrokBotChatSettings, chat_id: str, owner_id: str
    ) -> None:
        session = await repository.get_session_by_id(chat_id)
        if session is None or str(getattr(session, "user_id", "")) != owner_id:
            return
        if getattr(session, "deleted_at", None) is not None:
            return
        if self._grok_recovery_callback is not None:
            result = self._grok_recovery_callback(
                self.config,
                session=session,
                requester_id=owner_id,
                repository=repository,
            )
        else:
            from .grok_bot_chat_recovery import recover_grok_bot_chat_history

            result = recover_grok_bot_chat_history(
                self.config,
                session=session,
                requester_id=owner_id,
                repository=repository,
            )
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, timeout=self._recovery_timeout_seconds)

    async def run_once(self) -> bool:
        """Recover due Muse rows and report whether pending rows remain."""

        try:
            muse_settings, _muse_problem = self._settings_resolver(self.config)
        except Exception as error:
            logger.warning(
                "Muse native recovery settings unavailable: exception_type=%s",
                type(error).__name__,
            )
            muse_settings = None
        try:
            grok_settings, _grok_problem = self._grok_settings_resolver(self.config)
        except Exception as error:
            logger.warning(
                "Grok Bot recovery settings unavailable: exception_type=%s",
                type(error).__name__,
            )
            grok_settings = None
        muse_enabled = (
            muse_settings is not None
            and getattr(muse_settings, "native_link", False) is True
        )
        grok_enabled = (
            grok_settings is not None
        )
        if not muse_enabled and not grok_enabled:
            return False

        try:
            now = float(self._wall_clock())
            # Store the shortest interval required by any live row in a Chat.
            # A Chat can contain an old turn and a newly-created turn at the
            # same time; the new row must not inherit the old row's five-minute
            # cadence.
            muse_pending: dict[tuple[str, str], float] = {}
            if muse_enabled:
                journal = self._journal_factory()
                recoverable = journal.list_recoverable(
                    now, channel=Channel.MUSE, limit=_RECOVERY_LIMIT
                )
                undelivered = journal.list_undelivered_replies(
                    now, limit=_RECOVERY_LIMIT
                )
                mappings = self._thread_mappings(Path(journal.path))
                allowed_owners = getattr(
                    muse_settings, "allowed_owner_ids", frozenset()
                )
                for record in (*recoverable, *undelivered):
                    if (
                        record.channel is not Channel.MUSE
                        or record.owner_id not in allowed_owners
                    ):
                        continue
                    for chat_id in mappings.get(
                        (record.owner_id, record.thread_ref), ()
                    ):
                        key = (record.owner_id, chat_id)
                        created_at = float(record.created_at)
                        interval = _muse_recovery_interval(now - created_at)
                        previous = muse_pending.get(key)
                        muse_pending[key] = (
                            interval
                            if previous is None
                            else min(previous, interval)
                        )

            grok_chats: set[tuple[str, str]] = set()
            if grok_enabled and Path(grok_settings.receipt_store_path).exists():
                store = self._grok_store_factory(grok_settings.receipt_store_path)
                bindings = getattr(grok_settings, "owners", {})
                for chat in store.list_recoverable_chats(
                    since=now - _GROK_RECOVERY_WINDOW_SECONDS,
                    limit=_RECOVERY_LIMIT,
                ):
                    owner_id = str(chat.get("owner_id") or "")
                    chat_id = str(chat.get("chat_id") or "")
                    binding = grok_settings.binding_for(owner_id)
                    if (
                        not owner_id
                        or not chat_id
                        or owner_id not in bindings
                        or binding is None
                        or int(chat.get("generation") or 0) != binding.generation
                        or str(chat.get("target_identity") or "")
                        != grok_settings.target_identity
                    ):
                        continue
                    # ConversationRepository addresses sessions by UUID. Old
                    # receipts may instead contain external, non-session IDs.
                    try:
                        uuid.UUID(chat_id)
                    except ValueError:
                        continue
                    grok_chats.add((owner_id, chat_id))

            # Do not let a completed Chat's last attempt suppress a later
            # turn that reuses the same Chat identity.
            self._muse_last_attempt_at = {
                key: attempted_at
                for key, attempted_at in self._muse_last_attempt_at.items()
                if key in muse_pending
            }
            if not muse_pending and not grok_chats:
                return False
            repository = await self._repository()
        except Exception as error:
            logger.warning(
                "Native Chat recovery scan unavailable: exception_type=%s",
                type(error).__name__,
            )
            return True

        # Expired/replied rows disappear from the next scan.  Keep attempt
        # timestamps only for still-live Chats so a later turn starts with the
        # normal startup cadence.
        muse_chats: list[tuple[str, str]] = []
        for key, interval in sorted(muse_pending.items()):
            last_attempt = self._muse_last_attempt_at.get(key)
            if last_attempt is not None and now - last_attempt < interval:
                continue
            self._muse_last_attempt_at[key] = now
            muse_chats.append(key)

        for owner_id, chat_id in muse_chats:
            try:
                await asyncio.wait_for(
                    self._recover(repository, owner_id, chat_id),
                    timeout=self._recovery_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Muse native recovery timed out: owner/chat isolated"
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Muse native recovery skipped: exception_type=%s",
                    type(error).__name__,
                )
        for owner_id, chat_id in sorted(grok_chats):
            try:
                await asyncio.wait_for(
                    self._recover_grok(
                        repository, grok_settings, chat_id, owner_id
                    ),
                    timeout=self._recovery_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Grok Bot recovery timed out: owner/chat isolated"
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Grok Bot recovery skipped: exception_type=%s",
                    type(error).__name__,
                )
        # ``True`` means there is still recoverable work, even when this
        # particular row is waiting for its age bucket.  The outer loop then
        # uses the five-second pending tick and notices new Chats promptly.
        return bool(muse_pending or grok_chats)

    async def start(self) -> None:
        """Scan immediately, then poll every 5s while work is pending."""

        stop_event = asyncio.Event()
        self._stop_event = stop_event
        try:
            while not stop_event.is_set():
                pending = await self.run_once()
                interval = (
                    _MUSE_PENDING_TICK_SECONDS
                    if pending
                    else _NO_PENDING_TICK_SECONDS
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    continue
        finally:
            if self._stop_event is stop_event:
                self._stop_event = None

    async def stop(self) -> None:
        event = self._stop_event
        if event is not None:
            event.set()


__all__ = ["NativeChatRecoveryWorker"]
