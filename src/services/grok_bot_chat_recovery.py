"""Persist late Grok Bot replies into the ordinary Chat history.

Called only from the authenticated Chat history read routes after their normal
session ACL/project checks.  It runs only for the Chat owner whose Grok Bot
binding is configured, performs a bounded read-only reconcile through
:class:`~src.services.grok_bot_chat_service.GrokBotChatService` (never a send),
and stores each newly observed correlated reply once as an assistant message
whose id is derived from the operation and fragment digests.  When the
original turn is still the active waiting leaf, the recovered row replaces
that waiting display through the repository's conditional branch switch.

Placement follows the ordinary Chat tree (``parent_message_id`` + active
branch).  While the original turn is still the active leaf the reply simply
continues it.  Once a newer turn exists, the reply is stored as an inactive
answer branch of the original user message (a sibling of its pending row), so
it is never parented under, or shown as the answer to, the newer turn; the
existing branch navigation reaches it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from .agent_run_service import sanitize_assistant_display_text
from .grok_bot_chat_service import (
    GrokBotChatError,
    GrokBotChatService,
    GrokBotChatSettings,
    GrokBotChatUnavailable,
    GrokBotReceiptStore,
    GrokBotRecoveredReply,
    RECOVERY_WINDOW,
    resolve_grok_bot_chat_settings,
)

logger = logging.getLogger(__name__)
_DEFERRED_SERVICE_CLOSE_TASKS: set[asyncio.Task[Any]] = set()

# The history request waits at most this long for the read-only reconcile; a
# slower native call finishes in its thread and is persisted on the next read.
RECOVERY_REQUEST_TIMEOUT_SECONDS = 8.0
# Public alias for callers/tests that need to build the same owner-scoped
# recovery cutoff as the durable receipt service.
RECOVERY_WINDOW_SECONDS = RECOVERY_WINDOW.total_seconds()

ServiceFactory = Callable[[GrokBotChatSettings, GrokBotReceiptStore], GrokBotChatService]


def _default_service(settings: GrokBotChatSettings, store: GrokBotReceiptStore) -> GrokBotChatService:
    return GrokBotChatService(settings, store=store)


async def _close_service_after_worker(
    service: Any, worker: asyncio.Task[Any]
) -> None:
    try:
        await asyncio.shield(worker)
    except Exception:
        pass
    close = getattr(service, "close", None)
    if callable(close):
        try:
            await asyncio.to_thread(close)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            pass


def _defer_close_service_after_worker(
    service: Any, worker: asyncio.Task[Any]
) -> None:
    task = asyncio.create_task(_close_service_after_worker(service, worker))
    _DEFERRED_SERVICE_CLOSE_TASKS.add(task)
    task.add_done_callback(_DEFERRED_SERVICE_CLOSE_TASKS.discard)


async def _message_or_none(repository: Any, message_id: str) -> Any:
    try:
        return await repository.get_message_by_id(message_id)
    except ValueError:
        return None


def _late_operation_id(row: Any) -> str | None:
    metadata = getattr(row, "message_metadata", None)
    grok = metadata.get("grok_bot") if isinstance(metadata, dict) else None
    if isinstance(grok, dict) and grok.get("late_reply"):
        return str(grok.get("operation_id") or "") or None
    return None


async def _late_reply_placement(
    repository: Any, chat_id: str, pending_row: Any, operation_id: str
) -> tuple[str | None, bool, str | None]:
    """Return ``(parent_id, replace_waiting, expected_leaf_id)``.

    A recovered fragment is always attached to the original user turn.  When
    no newer turn has been written, the new row is first stored inactive and
    then conditionally switched into the active branch, replacing the waiting
    display.  Later fragments chain below the previous fragment while keeping
    the same race-safe active-leaf check.
    """

    if pending_row is None:
        return None, False, None
    if str(getattr(pending_row, "role", "")) == "user":
        original_parent = getattr(pending_row, "id", None)
    else:
        original_parent = getattr(pending_row, "parent_message_id", None)
    if not original_parent:
        return None, False, None

    later = await repository.get_session_messages(
        chat_id, since=getattr(pending_row, "created_at", None)
    )
    own = [row for row in later if _late_operation_id(row) == operation_id]
    tail = own[-1] if own else pending_row
    active = await repository.get_active_branch_messages(chat_id)
    replace_waiting = bool(active) and str(active[-1].id) == str(tail.id)
    parent_id = str(tail.id) if own else str(original_parent)
    return parent_id, replace_waiting, str(tail.id)


async def _retry_active_reply_switch(
    repository: Any,
    chat_id: str,
    reply_row: Any,
    pending_row: Any,
) -> None:
    """Retry an interrupted conditional switch when the original path is active."""

    switch = getattr(repository, "switch_active_branch", None)
    if not callable(switch):
        return
    active = await repository.get_active_branch_messages(chat_id)
    if not active:
        return
    leaf_id = str(active[-1].id)
    reply_id = str(getattr(reply_row, "id", ""))
    if leaf_id == reply_id:
        return
    pending_id = str(getattr(pending_row, "id", ""))
    reply_parent_id = str(getattr(reply_row, "parent_message_id", "") or "")
    if leaf_id not in {pending_id, reply_parent_id}:
        # A newer user turn owns the active path. The saved reply remains an
        # inactive sibling reachable from its original turn.
        return
    switched = await switch(
        chat_id,
        reply_id,
        expected_active_leaf_id=leaf_id,
    )
    if switched is False:
        active_after = await repository.get_active_branch_messages(chat_id)
        if active_after and str(active_after[-1].id) == leaf_id:
            # Leave the receipt unacknowledged so a later recovery pass retries
            # instead of treating an invisible reply as delivered.
            raise RuntimeError("Grok Bot late reply branch switch did not complete")


async def _persist_reply(repository: Any, chat_id: str, reply: GrokBotRecoveredReply) -> bool:
    """Persist one recovered fragment and replace an active waiting display."""

    message_id = reply.message_id
    existing = await _message_or_none(repository, message_id)
    if existing is not None:
        if _late_operation_id(existing) == reply.operation_id:
            pending_row = await _message_or_none(repository, reply.turn_key)
            if (
                pending_row is not None
                and str(getattr(pending_row, "session_id", "")) == chat_id
            ):
                await _retry_active_reply_switch(
                    repository, chat_id, existing, pending_row
                )
        return False
    pending_row = await _message_or_none(repository, reply.turn_key)
    if pending_row is None or str(getattr(pending_row, "session_id", "")) != chat_id:
        # Without the original Chat row there is no safe parent or branch to
        # replace.  Do not guess the latest active row.
        return False
    content = sanitize_assistant_display_text(reply.text)
    if not content:
        return False
    if (
        str(getattr(pending_row, "role", "")) == "assistant"
        and str(getattr(pending_row, "content", "") or "") == content
    ):
        # The normal foreground path already persisted this exact response.
        return False
    pending_message_id = str(pending_row.id)
    parent = getattr(pending_row, "parent_message_id", None)
    reply_to_message_id = str(parent) if parent else None
    parent_id, replace_waiting, expected_leaf_id = await _late_reply_placement(
        repository, chat_id, pending_row, reply.operation_id
    )
    if parent_id is None:
        return False
    metadata = {
        "client_message_id": f"grok-bot-reply:{reply.operation_id}:{reply.delivery_key[:32]}",
        "grok_bot": {
            "operation_id": reply.operation_id,
            "late_reply": True,
            "pending_message_id": pending_message_id,
            "reply_to_message_id": reply_to_message_id,
            "fragment_count": len(reply.fragments),
            "response_truncated": reply.response_truncated,
            "completion_status": reply.completion_status,
            "remote_terminal": reply.remote_terminal,
            "remote_terminal_supported": reply.remote_terminal_supported,
            "remote_cancel": "unsupported",
            "remote_cancel_supported": False,
        },
    }
    try:
        saved = await repository.add_message(
            session_id=chat_id,
            role="assistant",
            content=content,
            metadata=metadata,
            message_id=message_id,
            # Store inactive first so a newer user turn can never be
            # displaced by a late provider reply.
            explicit_parent_message_id=parent_id,
            is_active_branch=False,
        )
    except Exception:
        # A concurrent history read may have inserted the same deterministic
        # id/client_message_id first; that is a delivered reply, not an error.
        existing = await _message_or_none(repository, message_id)
        if existing is not None:
            if _late_operation_id(existing) == reply.operation_id:
                await _retry_active_reply_switch(
                    repository, chat_id, existing, pending_row
                )
            return False
        raise
    if getattr(saved, "_idempotency_replayed", False):
        await _retry_active_reply_switch(repository, chat_id, saved, pending_row)
        return False
    if replace_waiting and expected_leaf_id is not None:
        switch = getattr(repository, "switch_active_branch", None)
        if callable(switch):
            # The repository re-checks the active leaf while holding the
            # session lock.  If a newer turn raced this call, the reply stays
            # an inactive branch under the original user turn.
            switched = await switch(
                chat_id, message_id, expected_active_leaf_id=expected_leaf_id
            )
            if switched is False:
                active = await repository.get_active_branch_messages(chat_id)
                if active and str(active[-1].id) == expected_leaf_id:
                    raise RuntimeError(
                        "Grok Bot late reply branch switch did not complete"
                    )
    return True


async def recover_grok_bot_chat_history(
    config: Any,
    *,
    session: Any,
    requester_id: str,
    repository: Any,
    service_factory: ServiceFactory | None = None,
    timeout: float | None = None,
) -> int:
    """Reconcile and persist late replies for the Chat owner; returns inserts."""

    if config is None or session is None:
        return 0
    owner_id = str(getattr(session, "user_id", "") or "")
    if not owner_id or str(requester_id or "") != owner_id:
        return 0
    settings, _problem = resolve_grok_bot_chat_settings(config)
    if settings is None or settings.binding_for(owner_id) is None:
        return 0
    if not Path(settings.receipt_store_path).exists():
        return 0
    chat_id = str(getattr(session, "id", "") or "")
    store = GrokBotReceiptStore(settings.receipt_store_path)
    if await asyncio.to_thread(store.get_chat, chat_id) is None:
        return 0
    service = (service_factory or _default_service)(settings, store)
    worker: asyncio.Task[Any] | None = None
    worker = asyncio.create_task(
        asyncio.to_thread(service.recover_history, owner_id, chat_id)
    )
    close_deferred = False
    try:
        try:
            wait_timeout = (
                RECOVERY_REQUEST_TIMEOUT_SECONDS if timeout is None else timeout
            )
            replies = await asyncio.wait_for(asyncio.shield(worker), wait_timeout)
        except asyncio.TimeoutError:
            close_deferred = True
            _defer_close_service_after_worker(service, worker)
            replies = await asyncio.to_thread(service.pending_deliveries, owner_id, chat_id)
        inserted = 0
        for reply in replies:
            try:
                persisted = await _persist_reply(repository, chat_id, reply)
                if persisted:
                    inserted += 1
                already_saved = persisted or (
                    await _message_or_none(repository, reply.message_id) is not None
                )
                pending_row = await _message_or_none(repository, reply.turn_key)
                delivered_in_turn = (
                    pending_row is not None
                    and str(getattr(pending_row, "role", "")) == "assistant"
                    and str(getattr(pending_row, "content", "") or "")
                    == sanitize_assistant_display_text(reply.text)
                )
                if not already_saved and not delivered_in_turn:
                    continue
                acknowledge = getattr(
                    getattr(service, "transport", None), "acknowledge_delivery", None
                )
                if not callable(acknowledge):
                    continue
                acknowledged = await asyncio.to_thread(
                    acknowledge, reply.operation_id
                )
                if acknowledged:
                    await asyncio.to_thread(
                        service.mark_delivered,
                        owner_id,
                        chat_id,
                        reply.operation_id,
                        reply.fragment_shas,
                    )
            except Exception:
                # A history read must remain available even if one recovered
                # fragment cannot be committed.  Do not log provider text or
                # exception details.
                logger.info(
                    "Grok Bot Chat history recovery skipped: reply_persistence_unavailable"
                )
        return inserted
    except (GrokBotChatError, GrokBotChatUnavailable) as error:
        logger.info("Grok Bot Chat history recovery skipped: %s", error.code)
        return 0
    finally:
        if not close_deferred:
            if not worker.done():
                close_deferred = True
                _defer_close_service_after_worker(service, worker)
            else:
                close = getattr(service, "close", None)
                if callable(close):
                    try:
                        await asyncio.to_thread(close)
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception:
                        # Native transport close is best-effort and keeps
                        # transport details out of history responses.
                        pass
