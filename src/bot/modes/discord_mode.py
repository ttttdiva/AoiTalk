"""
Discord mode for AoiTalk bot
"""

import asyncio
import inspect
import logging
import mimetypes
import os
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Callable, Awaitable, Deque, Tuple, Mapping
import aiohttp
import base64
import io
import time
from urllib.parse import urlsplit
from PIL import Image
import google.generativeai as genai

from ...assistant.base import BaseAssistant
from ...config import Config
from ...llm.generation_policy import GenerationProfile, generation_policy_for_profile
from ...memory.history import HistoryManager
from ...services.outbound_privacy_service import OutboundPrivacyGateway

logger = logging.getLogger(__name__)


def normalize_usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Lazy import keeps Discord's optional assistant import cycle-free."""
    from ...llm.conversation_context import normalize_usage as _normalize

    return _normalize(*args, **kwargs)


def persist_usage_sync(*args: Any, **kwargs: Any) -> bool:
    """Lazy import for usage persistence (Discord is optional at startup)."""
    from ...llm.conversation_context import persist_usage_sync as _persist

    return bool(_persist(*args, **kwargs))


@dataclass(frozen=True)
class _DiscordUsageProxy:
    """Immutable per-request identity passed to TokenUsage persistence."""

    user_id: Optional[str]
    current_session_id: Any = None
    current_project_id: Any = None
    character_name: Optional[str] = None
    session_context: Optional[Mapping[str, Any]] = None
    project_metadata: Optional[Mapping[str, Any]] = None

    def _get_session_user_id(self) -> Optional[str]:
        return self.user_id


@dataclass
class _QueuedTurn:
    """Small, payload-only item retained by a Discord session worker."""

    content: str
    image_urls: List[str]
    user_id: int
    guild_id: int
    channel_id: int
    session_id: Optional[str]
    runtime_session_id: Optional[str]
    message_id: Optional[str] = None
    # Canonical Discord principal used by Scoped Memory.  It is kept separate
    # from the durable/runtime session IDs while remaining the same identity
    # used by conversation history, TurnContext, and memory retrieval.
    actor_id: Optional[str] = None
    reply: Optional[Callable[[str], Awaitable[None]]] = None
    typing: Optional[Callable[[], Any]] = None
    future: Optional[asyncio.Future] = None
    cancelled: bool = False
    reset_epoch: int = 0


@dataclass
class _SessionWorker:
    key: Tuple[int, int]
    pending: Deque[_QueuedTurn]
    task: Optional[asyncio.Task] = None
    stopping: bool = False


class DiscordMode(BaseAssistant):
    """Discord-specific assistant mode"""
    
    def __init__(self, config: Config, character: str = None):
        """Initialize Discord mode
        
        Args:
            config: Configuration manager
            character: Character name to use
        """
        super().__init__(config, mode='discord')
        
        # Override character if specified
        if character:
            self.character_name = character
            self.character_config = config.get_character_config(character)
            
        # Discord-specific state
        self.guild_contexts: Dict[int, Dict[str, Any]] = {}  # Guild ID -> context
        # Contexts are scoped by guild *and* user.  A Discord user can be in
        # multiple guilds and those conversations must never share history.
        self.user_contexts: Dict[Tuple[Optional[int], int], Dict[str, Any]] = {}
        self._session_locks: Dict[Tuple[Optional[int], int], asyncio.Lock] = {}
        # The LLM client historically exposes mutable conversation/session
        # fields.  Serialize the short mutation+generation critical section
        # even when a mode instance is shared by test doubles/integrations.
        self._llm_context_lock = asyncio.Lock()
        self._session_workers: Dict[Tuple[int, int], _SessionWorker] = {}
        self._session_reset_events: Dict[Tuple[int, int], asyncio.Event] = {}
        self._session_reset_epochs: Dict[Tuple[int, int], int] = {}
        self._session_identities: Dict[Tuple[int, int], Tuple[Optional[str], Optional[str]]] = {}
        self._prefill_tasks: set[asyncio.Task] = set()
        # Scoped Memory extraction is durable but must not block a Discord
        # reply.  Keep an explicit task ledger so shutdown can cancel/await
        # processors and replayed message IDs cannot launch duplicate work
        # while the original processor is still active.
        self._scoped_memory_tasks: set[asyncio.Task] = set()
        self._scoped_memory_job_tasks: Dict[str, asyncio.Task] = {}
        self._closed = False
        queue_config = config.get('discord.queue', {}) if config else {}
        if not isinstance(queue_config, dict):
            queue_config = {}
        raw_window = queue_config.get(
            'coalesce_window_ms',
            queue_config.get(
                'coalesce_window',
                config.get(
                    'discord.coalesce_window_ms',
                    config.get('discord.session.coalesce_window_ms', 250)
                    if config else 250,
                ) if config else 250,
            ),
        )
        try:
            self.coalesce_window = max(0.0, min(float(raw_window) / 1000.0, 5.0))
        except (TypeError, ValueError):
            self.coalesce_window = 0.25
        try:
            self.max_image_urls = max(1, int(queue_config.get('max_images', 4)))
        except (TypeError, ValueError):
            self.max_image_urls = 4
        try:
            self.reply_timeout = max(
                0.1,
                float(
                    queue_config.get(
                        'reply_timeout_seconds',
                        config.get('discord.reply_timeout_seconds', 30.0)
                        if config else 30.0,
                    )
                ),
            )
        except (TypeError, ValueError):
            self.reply_timeout = 30.0
        try:
            self.image_timeout = max(
                0.1,
                float(
                    queue_config.get(
                        'image_timeout_seconds',
                        config.get('discord.image_timeout_seconds', 15.0)
                        if config else 15.0,
                    )
                ),
            )
        except (TypeError, ValueError):
            self.image_timeout = 15.0
        try:
            self.max_image_bytes = max(
                1,
                int(
                    queue_config.get(
                        'max_image_bytes',
                        config.get('discord.max_image_bytes', 10 * 1024 * 1024)
                        if config else 10 * 1024 * 1024,
                    )
                ),
            )
        except (TypeError, ValueError):
            self.max_image_bytes = 10 * 1024 * 1024
        self._memory_prefill_attempts: Dict[str, bool] = {}
        # Direct vision calls below bypass the regular LLM client request
        # recorder.  Keep a response-identity ledger so a retry/wrapper cannot
        # create a second TokenUsage row for one provider response.
        self._recorded_usage_responses: List[Any] = []

    def _stage(self, stage: str, *, user_id: Any = None, guild_id: Any = None,
               channel_id: Any = None, session_id: Any = None, **extra: Any) -> None:
        """Emit structured lifecycle logs without message content."""
        fields = {
            "stage": stage,
            "user_id": user_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "session_id": session_id,
        }
        fields.update({key: value for key, value in extra.items() if value is not None})
        logger.info("discord_stage %s", fields)

    @staticmethod
    def _context_key(user_id: Optional[int], guild_id: Optional[int]) -> Optional[Tuple[Optional[int], int]]:
        if user_id is None:
            return None
        return (guild_id, user_id)

    def _get_session_lock(self, user_id: Optional[int], guild_id: Optional[int]) -> asyncio.Lock:
        key = self._context_key(user_id, guild_id)
        if key is None:
            # A DM is rejected by AoiTalkBot, but direct callers still get a
            # stable lock rather than sharing all anonymous requests.
            key = (None, 0)
        lock = self._session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[key] = lock
        return lock

    def _get_llm_context_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_llm_context_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._llm_context_lock = lock
        return lock

    def _build_usage_proxy(
        self,
        user_id: Optional[int],
        guild_id: Optional[int],
        session_id: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
    ) -> _DiscordUsageProxy:
        """Build a race-free identity for one direct Discord vision request."""
        # ``session_id`` is the durable ConversationSession UUID used by the
        # LLM client's persistence path.  Direct usage telemetry intentionally
        # carries the runtime turn UUID instead, so the two identities cannot
        # be confused by concurrent requests.
        resolved_session_id = runtime_session_id or session_id
        if resolved_session_id is None and user_id is not None:
            # Legacy/direct callers may not thread the real DiscordSession
            # object.  Use a UUID5 fallback; runtime bot paths pass the real
            # session UUID explicitly.
            resolved_session_id = self._build_discord_session_id(user_id, guild_id)
        return _DiscordUsageProxy(
            user_id=self._build_memory_user_id(user_id, guild_id),
            current_session_id=resolved_session_id,
            character_name=getattr(self, "character_name", None),
        )

    @staticmethod
    def _build_discord_session_id(
        user_id: Optional[int],
        guild_id: Optional[int],
    ) -> Optional[str]:
        if user_id is None:
            return None
        guild = guild_id if guild_id is not None else "dm"
        # SessionHandler normally supplies the real UUID.  This fallback is
        # only for legacy/direct callers that have not threaded a session yet;
        # UUID5 keeps TokenUsage.session_id valid instead of persisting a
        # human-readable key that conversation_context would discard.
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aoitalk:discord:{user_id}:{guild}"))

    @staticmethod
    def _coerce_usage_context(context: Any) -> Any:
        """Accept a caller-provided proxy/dict without mutating it."""
        if context is None:
            return None
        if (
            hasattr(context, "current_session_id")
            or hasattr(context, "current_project_id")
            or callable(getattr(context, "_get_session_user_id", None))
        ):
            return context

        def value(name: str, *aliases: str) -> Any:
            if isinstance(context, dict):
                for key in (name, *aliases):
                    item = context.get(key)
                    if item is not None:
                        return item
            for key in (name, *aliases):
                item = getattr(context, key, None)
                if item is not None:
                    return item
            return None

        return _DiscordUsageProxy(
            user_id=(
                str(value("user_id")).strip()
                if value("user_id") is not None
                else None
            ),
            current_session_id=value("current_session_id", "session_id"),
            current_project_id=value("current_project_id", "project_id"),
            character_name=value("character_name"),
            session_context=value("session_context"),
            project_metadata=value("project_metadata"),
        )

    @staticmethod
    def _usage_value(value: Any, name: str) -> Any:
        result = getattr(value, name, None)
        if result is None and isinstance(value, dict):
            result = value.get(name)
        return result

    @classmethod
    def _gemini_usage_payload(cls, response: Any) -> Optional[Dict[str, Any]]:
        """Normalize Gemini vision usage_metadata when the API reports it."""
        usage = cls._usage_value(response, "usage_metadata")
        if usage is None:
            return None

        def count(name: str) -> Optional[int]:
            raw = cls._usage_value(usage, name)
            if raw is None:
                return None
            try:
                return max(0, int(raw))
            except (TypeError, ValueError):
                return None

        input_tokens = count("prompt_token_count")
        output_tokens = count("candidates_token_count")
        if input_tokens is None and output_tokens is None:
            return None
        cached_tokens = count("cached_content_token_count") or 0
        payload: Dict[str, Any] = {
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "cached_tokens": cached_tokens,
            "cache_read_tokens": cached_tokens,
            "reasoning_tokens": count("thoughts_token_count") or 0,
            "cache_provider": "gemini",
            "metrics_source": "gemini.usage_metadata",
        }
        resolved_model = cls._usage_value(response, "model_version")
        if resolved_model:
            payload["resolved_model"] = str(resolved_model)
        return payload

    def _mark_usage_recorded(self, response: Any) -> bool:
        """Return True when the exact provider response was already persisted."""
        try:
            if getattr(response, "_aoitalk_usage_recorded", False):
                return True
            object.__setattr__(response, "_aoitalk_usage_recorded", True)
            return False
        except Exception:
            recorded = getattr(self, "_recorded_usage_responses", None)
            if recorded is None:
                recorded = []
                self._recorded_usage_responses = recorded
            if any(item is response for item in recorded):
                return True
            recorded.append(response)
            del recorded[:-8]
            return False

    def _record_direct_vision_usage(
        self,
        response: Any,
        *,
        provider: str,
        requested_model: str,
        latency_ms: int = 0,
        usage_client: Any = None,
        usage_context: Any = None,
    ) -> bool:
        """Persist usage from a direct Discord vision API call exactly once."""
        canonical_provider = str(provider or "").strip().lower()
        if canonical_provider == "gemini":
            payload = self._gemini_usage_payload(response)
        else:
            raw_usage = self._usage_value(response, "usage")
            resolved_model = self._usage_value(response, "model")
            payload = normalize_usage(
                raw_usage,
                provider=canonical_provider,
                resolved_model=(str(resolved_model) if resolved_model else None),
            )

        if not payload or (
            payload.get("input_tokens") is None
            and payload.get("output_tokens") is None
        ):
            logger.info(
                "Discord %s vision response has no token usage; "
                "leaving request unmetered rather than estimating",
                canonical_provider or "unknown",
            )
            return False
        if self._mark_usage_recorded(response):
            return False

        try:
            # Direct vision requests use an immutable per-request proxy.  This
            # avoids relying on the shared llm_client's mutable session context,
            # which could be overwritten by another concurrent Discord user.
            tracking_client = (
                usage_client
                if usage_client is not None
                else (
                    self._coerce_usage_context(usage_context)
                    if usage_context is not None
                    else self.llm_client
                )
            )
            persist_usage_sync(
                tracking_client,
                provider=canonical_provider,
                model=str(requested_model),
                usage=payload,
                request_type="vision",
                latency_ms=max(int(latency_ms or 0), 0),
            )
            return True
        except Exception:  # pragma: no cover - telemetry must not break Discord
            logger.debug("Discord direct vision usage persistence failed", exc_info=True)
            return False

    async def _initialize_mode_specific(self) -> bool:
        """Initialize Discord-specific components"""
        try:
            # Discord modeはTTSやSTTを直接使用しない（必要に応じて初期化）
            logger.info("Discord mode initialized")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Discord mode: {e}")
            return False
    
    async def run(self):
        """Run Discord mode (called when needed)"""
        # Discord modeは event-driven なので特別な実行ループは不要
        self.running = True
        logger.info("Discord mode is running")

    async def enqueue_turn(
        self,
        content: str,
        *,
        image_urls: Optional[List[str]] = None,
        user_id: int,
        guild_id: int,
        channel_id: int,
        message_id: Optional[str] = None,
        actor_id: Optional[str] = None,
        session_id: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
        reply: Optional[Callable[[str], Awaitable[None]]] = None,
        typing: Optional[Callable[[], Any]] = None,
    ) -> str:
        """Queue one Discord message on its guild/user worker.

        Workers serialize generations for one durable session while keeping
        guild/channel/user boundaries explicit.  A short idle window combines
        adjacent messages from the same channel into one logical turn; no
        message from another channel can enter that batch.
        """
        if getattr(self, "_closed", False):
            raise asyncio.CancelledError()
        loop = asyncio.get_running_loop()
        worker_key = (int(guild_id), int(user_id))
        observed_epoch = self._session_reset_epochs.get(worker_key, 0)
        reset_event = self._session_reset_events.get(worker_key)
        if reset_event is not None:
            # /clear holds this barrier while cancelling the old worker and
            # rotating the durable row.  New arrivals wait rather than
            # capturing the old conversation ID in a post-clear queue item.
            await reset_event.wait()
            if self._session_reset_epochs.get(worker_key, 0) != observed_epoch:
                # The caller captured its payload before the reset barrier;
                # do not silently move that old content into the new DB row.
                raise asyncio.CancelledError()
        # A reset can start between the initial epoch snapshot and the event
        # lookup (when no event existed yet).  Re-check unconditionally so a
        # payload captured before that boundary cannot be appended to the new
        # worker after the reset has already completed.
        if self._session_reset_epochs.get(worker_key, 0) != observed_epoch:
            raise asyncio.CancelledError()
        current_identity = self._session_identities.get(worker_key)
        requested_identity = (session_id, runtime_session_id)
        if current_identity is not None and requested_identity != current_identity:
            raise asyncio.CancelledError()
        self._session_identities.setdefault(worker_key, requested_identity)
        worker = self._session_workers.get(worker_key)
        if worker is None or (worker.task is not None and worker.task.done()):
            worker = _SessionWorker(key=worker_key, pending=deque())
            self._session_workers[worker_key] = worker

        future = loop.create_future()
        urls = list(dict.fromkeys(str(url) for url in (image_urls or []) if str(url).strip()))
        if len(urls) > self.max_image_urls:
            logger.warning(
                "Discord image limit exceeded; dropping %d URL(s) guild=%s user=%s channel=%s",
                len(urls) - self.max_image_urls,
                guild_id,
                user_id,
                channel_id,
            )
            urls = urls[: self.max_image_urls]
        item = _QueuedTurn(
            content=str(content or "").strip(),
            image_urls=urls,
            user_id=int(user_id),
            guild_id=int(guild_id),
            channel_id=int(channel_id),
            message_id=(str(message_id).strip() if message_id is not None else None) or None,
            actor_id=(
                self._normalize_scoped_memory_actor_id(actor_id)
                or self._build_memory_user_id(user_id, guild_id)
            ),
            session_id=session_id,
            runtime_session_id=runtime_session_id,
            reply=reply,
            typing=typing,
            future=future,
            reset_epoch=self._session_reset_epochs.get(worker_key, 0),
        )
        worker.pending.append(item)
        if worker.task is None or worker.task.done():
            worker.stopping = False
            worker.task = asyncio.create_task(self._run_session_worker(worker))
        try:
            return await future
        except asyncio.CancelledError:
            # A caller may time out/cancel while the item is still pending.
            # Remove it from the queue and sever the reply closure so a later
            # worker drain cannot send a response for a cancelled request.
            item.cancelled = True
            item.reply = None
            try:
                worker.pending.remove(item)
            except ValueError:
                # The worker already popped the item.  Its cancellation-aware
                # reply check will skip delivery after generation completes.
                pass
            if not future.done():
                future.cancel()
            raise

    @staticmethod
    def _same_coalesce_identity(first: _QueuedTurn, candidate: _QueuedTurn) -> bool:
        """Return whether two turns belong to the same durable/runtime turn.

        Guild/user/channel alone is not enough: ``/clear`` rotates the durable
        ConversationSession while the in-process worker remains alive.  The
        runtime identity also protects callers that create a fresh turn UUID
        before persistence has resolved a new durable row.
        """
        return (
            first.guild_id == candidate.guild_id
            and first.user_id == candidate.user_id
            and first.channel_id == candidate.channel_id
            and first.session_id == candidate.session_id
            and first.runtime_session_id == candidate.runtime_session_id
            and first.actor_id == candidate.actor_id
        )

    @staticmethod
    def _queued_item_cancelled(item: _QueuedTurn) -> bool:
        return bool(
            item.cancelled
            or (item.future is not None and item.future.cancelled())
        )

    async def _run_session_worker(self, worker: _SessionWorker) -> None:
        """Drain one guild/user queue; failures never strand later messages."""
        active_batch: List[_QueuedTurn] = []
        try:
            while worker.pending and not worker.stopping:
                first = worker.pending.popleft()
                if (
                    self._queued_item_cancelled(first)
                    or first.reset_epoch != self._session_reset_epochs.get(worker.key, 0)
                ):
                    first.cancelled = True
                    first.reply = None
                    if first.future is not None and not first.future.done():
                        first.future.cancel()
                    continue
                batch = [first]
                active_batch = batch

                # Start the coalesce timer only after the first item is
                # available.  This guarantees A is already generating before
                # B/C can join the next turn when they arrive concurrently.
                if self.coalesce_window > 0:
                    await asyncio.sleep(self.coalesce_window)
                # The caller can cancel the first item while this coalesce
                # window is sleeping.  Re-check before constructing the
                # logical batch; otherwise a cancelled A could be combined
                # with a later B and still generate/reply for A+B.
                if self._queued_item_cancelled(first):
                    active_batch = []
                    continue
                while worker.pending:
                    candidate = worker.pending[0]
                    if (
                        self._queued_item_cancelled(candidate)
                        or candidate.reset_epoch != self._session_reset_epochs.get(worker.key, 0)
                    ):
                        candidate.cancelled = True
                        candidate.reply = None
                        worker.pending.popleft()
                        if candidate.future is not None and not candidate.future.done():
                            candidate.future.cancel()
                        continue
                    # Scoped Memory idempotency is keyed by the source
                    # Discord message ID.  The job service has no durable
                    # alias table for a coalesced batch, so a replay of the
                    # second message could otherwise create a second job.
                    # Keep turns carrying a source ID independent; legacy
                    # callers without IDs retain the coalescing optimization.
                    if first.message_id or candidate.message_id:
                        break
                    if not self._same_coalesce_identity(first, candidate):
                        break
                    batch.append(worker.pending.popleft())

                # Cancellation may race with the final dequeue.  Exclude any
                # item that was cancelled after it entered the batch while
                # preserving arrival order for the remaining items.
                batch = [
                    item for item in batch
                    if not self._queued_item_cancelled(item)
                    and item.reset_epoch == self._session_reset_epochs.get(worker.key, 0)
                ]
                if not batch:
                    active_batch = []
                    continue
                first = batch[0]
                active_batch = batch

                # A coalesced queue batch represents one logical turn.  Use
                # the first available Discord message ID as its stable
                # idempotency key; callers that do not expose IDs retain the
                # legacy content/session fallback in Scoped Memory.
                turn_message_id = next(
                    (item.message_id for item in batch if item.message_id),
                    None,
                )

                content_parts = [item.content for item in batch if item.content]
                combined_content = "\n".join(content_parts)
                image_urls = list(dict.fromkeys(
                    url for item in batch for url in item.image_urls
                ))
                if len(image_urls) > self.max_image_urls:
                    logger.warning(
                        "Discord batch image limit exceeded; dropping %d URL(s) "
                        "guild=%s user=%s channel=%s",
                        len(image_urls) - self.max_image_urls,
                        first.guild_id,
                        first.user_id,
                        first.channel_id,
                    )
                    image_urls = image_urls[: self.max_image_urls]
                response: str

                async def generate_batch() -> str:
                    if image_urls:
                        return await self.process_text_with_images(
                            combined_content,
                            image_urls,
                            user_id=first.user_id,
                            guild_id=first.guild_id,
                            channel_id=first.channel_id,
                            message_id=turn_message_id,
                            actor_id=first.actor_id,
                            session_id=first.session_id,
                            runtime_session_id=first.runtime_session_id,
                        )
                    return await self.process_text(
                        combined_content,
                        user_id=first.user_id,
                        guild_id=first.guild_id,
                        channel_id=first.channel_id,
                        message_id=turn_message_id,
                        actor_id=first.actor_id,
                        session_id=first.session_id,
                        runtime_session_id=first.runtime_session_id,
                    )

                try:
                    typing_context = None
                    if callable(first.typing):
                        try:
                            typing_context = first.typing()
                        except Exception:
                            logger.debug("Discord typing indicator setup failed", exc_info=True)
                    if typing_context is not None:
                        if hasattr(typing_context, "__aenter__"):
                            async with typing_context:
                                response = await generate_batch()
                        else:
                            with typing_context:
                                response = await generate_batch()
                    else:
                        response = await generate_batch()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # keep draining after one failure
                    self._stage(
                        "generation_failed",
                        user_id=first.user_id,
                        guild_id=first.guild_id,
                        channel_id=first.channel_id,
                        session_id=first.session_id,
                        exception=repr(exc),
                    )
                    logger.error(
                        "Discord generation failed guild=%s user=%s channel=%s: %s",
                        first.guild_id,
                        first.user_id,
                        first.channel_id,
                        exc,
                        exc_info=True,
                    )
                    response = "申し訳ありません。応答の生成に失敗しました。"
                # One logical batch has one Discord reply.  All callers waiting
                # on coalesced items receive the same completion value, but
                # only the first message's callback is invoked.
                reply_item = next(
                    (item for item in batch if item.reply is not None and not item.cancelled),
                    first,
                )
                if reply_item.reply is not None and not reply_item.cancelled:
                    try:
                        result = reply_item.reply(response)
                        if inspect.isawaitable(result):
                            await asyncio.wait_for(
                                result,
                                timeout=getattr(self, "reply_timeout", 30.0),
                            )
                    except Exception as exc:
                        self._stage(
                            "reply_send_failed",
                            user_id=first.user_id,
                            guild_id=first.guild_id,
                            channel_id=first.channel_id,
                            session_id=first.session_id,
                            exception=repr(exc),
                        )
                        logger.error(
                            "Discord reply callback failed guild=%s user=%s channel=%s: %s",
                            first.guild_id,
                            first.user_id,
                            first.channel_id,
                            exc,
                            exc_info=True,
                        )
                for item in batch:
                    if (
                        not item.cancelled
                        and item.future is not None
                        and not item.future.done()
                    ):
                        item.future.set_result(response)
                active_batch = []
        except asyncio.CancelledError:
            for item in [*active_batch, *list(worker.pending)]:
                item.cancelled = True
                item.reply = None
                if item.future is not None and not item.future.done():
                    item.future.cancel()
            raise
        finally:
            # Remove only this worker instance.  A new enqueue can install a
            # replacement worker after cancellation without races.
            current = self._session_workers.get(worker.key)
            if current is worker:
                self._session_workers.pop(worker.key, None)

    async def drain_queues(self) -> None:
        """Wait for currently queued workers (primarily useful in tests)."""
        tasks = [worker.task for worker in self._session_workers.values() if worker.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown_queues(self) -> None:
        """Cancel workers and settle pending futures during bot shutdown."""
        # Release enqueue callers waiting behind a reset barrier before
        # cancelling workers.  They will observe the epoch/identity boundary
        # and cancel instead of hanging forever during bot shutdown.
        for event in tuple(self._session_reset_events.values()):
            event.set()
        workers = list(self._session_workers.values())
        for worker in workers:
            worker.stopping = True
            if worker.task and not worker.task.done():
                worker.task.cancel()
        if workers:
            await asyncio.gather(
                *(worker.task for worker in workers if worker.task),
                return_exceptions=True,
            )
        self._session_workers.clear()

    async def _cancel_session_worker(self, worker_key: Tuple[int, int]) -> None:
        worker = self._session_workers.get(worker_key)
        if worker is None:
            return
        worker.stopping = True
        if worker.task and not worker.task.done():
            worker.task.cancel()
            await asyncio.gather(worker.task, return_exceptions=True)
        current = self._session_workers.get(worker_key)
        if current is worker:
            self._session_workers.pop(worker_key, None)

    def set_session_identity(
        self,
        user_id: int,
        guild_id: int,
        *,
        session_id: Optional[str],
        runtime_session_id: Optional[str],
        force: bool = True,
        epoch: Optional[int] = None,
    ) -> None:
        """Publish the current durable/runtime identity for queue admission."""
        key = (int(guild_id), int(user_id))
        if (
            epoch is not None
            and self._session_reset_epochs.get(key, 0) != epoch
        ):
            # A caller captured its IDs before a reset and resumed after the
            # boundary.  Never let that stale payload overwrite the identity
            # published by /clear (including the fail-safe None/runtime pair).
            logger.debug(
                "Ignoring stale Discord session identity epoch guild=%s user=%s",
                guild_id,
                user_id,
            )
            return
        requested = (
            session_id,
            runtime_session_id,
        )
        current = self._session_identities.get(key)
        if (
            not force
            and current is not None
            and current != requested
        ):
            # A message can finish prefill after /clear has completed.  Do
            # not let its captured old IDs overwrite the newly published
            # identity and sneak past the reset epoch.
            logger.debug(
                "Ignoring stale Discord session identity guild=%s user=%s",
                guild_id,
                user_id,
            )
            return
        self._session_identities[key] = requested

    @asynccontextmanager
    async def session_reset(self, user_id: int, guild_id: int):
        """Pause one Discord session while /clear rotates its DB row.

        New enqueue calls wait on the per-session event.  Existing workers are
        cancelled and their futures/reply closures settled before the context
        and shared LLM locks are acquired, preventing an old turn from
        crossing the reset boundary.
        """
        worker_key = (int(guild_id), int(user_id))
        event = self._session_reset_events.get(worker_key)
        if event is None:
            event = asyncio.Event()
            event.set()
            self._session_reset_events[worker_key] = event
        self._session_reset_epochs[worker_key] = self._session_reset_epochs.get(worker_key, 0) + 1
        event.clear()
        context_lock = self._get_session_lock(user_id, guild_id)
        llm_context_lock = self._get_llm_context_lock()
        try:
            await self._cancel_session_worker(worker_key)
            await context_lock.acquire()
            try:
                await llm_context_lock.acquire()
            except BaseException:
                context_lock.release()
                raise
            try:
                yield
            finally:
                llm_context_lock.release()
                context_lock.release()
        finally:
            current_event = self._session_reset_events.get(worker_key)
            # Always release the captured Event, even if cleanup cleared the
            # registry before this reset context unwinds.  Waiters hold the
            # object reference and must never remain blocked on a detached,
            # cleared event.
            event.set()
            if current_event is event:
                self._session_reset_events.pop(worker_key, None)
    
    async def _cleanup_mode_specific(self):
        """Cleanup Discord-specific resources"""
        self._closed = True
        current_task = asyncio.current_task()
        prefill_tasks = tuple(getattr(self, "_prefill_tasks", set()))
        for task in prefill_tasks:
            if task is not current_task and not task.done():
                task.cancel()
        pending_prefills = [
            task for task in prefill_tasks
            if task is not current_task
        ]
        if pending_prefills:
            await asyncio.gather(*pending_prefills, return_exceptions=True)
        if hasattr(self, "_prefill_tasks"):
            self._prefill_tasks.clear()
        memory_tasks = tuple(getattr(self, "_scoped_memory_tasks", set()))
        for task in memory_tasks:
            if task is not current_task and not task.done():
                task.cancel()
        pending_memory = [task for task in memory_tasks if task is not current_task]
        if pending_memory:
            await asyncio.gather(*pending_memory, return_exceptions=True)
        if hasattr(self, "_scoped_memory_tasks"):
            self._scoped_memory_tasks.clear()
        if hasattr(self, "_scoped_memory_job_tasks"):
            self._scoped_memory_job_tasks.clear()
        await self.shutdown_queues()
        self.running = False
        self.guild_contexts.clear()
        self.user_contexts.clear()
        self._session_locks.clear()
        # Do not strand an enqueue waiter by dropping its Event while it is
        # still cleared.  ``shutdown_queues`` also sets these, but set again
        # here for callers that invoke this cleanup hook directly.
        for event in tuple(self._session_reset_events.values()):
            event.set()
        self._session_reset_events.clear()
        self._session_reset_epochs.clear()
        self._session_identities.clear()
        logger.info("Discord mode cleaned up")

    @staticmethod
    def _normalize_scoped_memory_actor_id(actor_id: Any) -> Optional[str]:
        """Validate a canonical Discord Scoped Memory principal.

        Discord turns intentionally use ``discord:<guild>:<user>`` as the
        principal across conversation history, TurnContext and Scoped Memory.
        The external-principal service resolves this namespace without
        manufacturing a ``users`` row; malformed/foreign principals are
        rejected and the caller falls back to the current Discord key.
        """
        if actor_id is None:
            return None
        value = str(actor_id).strip()
        parts = value.split(":")
        if len(parts) != 3 or parts[0].casefold() != "discord":
            return None
        if not all(part.strip() for part in parts[1:]):
            return None
        return ":".join(part.strip() for part in parts)

    def _schedule_scoped_memory_job(self, job: Optional[Dict[str, Any]]) -> None:
        """Process an enqueued job in the background without delaying reply."""
        if not isinstance(job, dict) or getattr(self, "_closed", False):
            return
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            return
        tasks = getattr(self, "_scoped_memory_tasks", None)
        job_tasks = getattr(self, "_scoped_memory_job_tasks", None)
        if tasks is None:
            tasks = self._scoped_memory_tasks = set()
        if job_tasks is None:
            job_tasks = self._scoped_memory_job_tasks = {}
        existing = job_tasks.get(job_id)
        if existing is not None and not existing.done():
            return
        principal = str(job.get("principal_key") or job.get("user_id") or "").strip()
        task = asyncio.create_task(
            self._process_scoped_memory_job(job_id, principal=principal)
        )
        tasks.add(task)
        job_tasks[job_id] = task

        def _finished(done: asyncio.Task) -> None:
            tasks.discard(done)
            if job_tasks.get(job_id) is done:
                job_tasks.pop(job_id, None)
            if done.cancelled():
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                logger.warning(
                    "Discord Scoped Memory processor failed job=%s: %s",
                    job_id,
                    error,
                )

        task.add_done_callback(_finished)

    async def _process_scoped_memory_job(
        self,
        job_id: str,
        *,
        principal: str,
    ) -> None:
        """Claim/process one job, then opportunistically recover older jobs."""
        try:
            from ...services.scoped_memory_job_service import (
                process_pending_scoped_memory_jobs,
                process_scoped_memory_job,
            )

            client = getattr(self, "llm_client", None)
            await process_scoped_memory_job(job_id, llm_client=client)
            if principal:
                await process_pending_scoped_memory_jobs(
                    llm_client=client,
                    user_id=principal,
                    limit=3,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Durable jobs remain pending/failed for the next turn's retry;
            # extraction failures must never alter a successful Discord reply.
            logger.warning(
                "Discord Scoped Memory processor failed job=%s: %s",
                job_id,
                exc,
                exc_info=True,
            )

    async def _enqueue_scoped_memory_job(
        self,
        user_input: str,
        assistant_response: str,
        *,
        user_id: Optional[int],
        guild_id: Optional[int],
        session_id: Optional[str],
        message_id: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Enqueue one durable Scoped Memory extraction request.

        Discord's mode does not use :class:`ResponseHandler`, so successful
        text and direct-vision turns need this small bridge explicitly.  The
        job service owns the unique ``message_key`` constraint; callers may
        safely replay a Discord delivery and receive the existing job row.
        Failures are deliberately isolated from a successful user response.
        """
        if not assistant_response or not user_id or not session_id:
            return None

        try:
            durable_session_id = str(uuid.UUID(str(session_id)))
        except (TypeError, ValueError, AttributeError):
            # Runtime/session placeholders are not durable conversation rows.
            # Do not hand them to the job service where they could create a
            # dead extraction attempt or attach memory to the wrong scope.
            logger.info(
                "Skipping Discord Scoped Memory job without durable session guild=%s user=%s session=%s",
                guild_id,
                user_id,
                session_id,
            )
            return None

        resolved_actor_id = self._normalize_scoped_memory_actor_id(actor_id)
        if resolved_actor_id is None:
            resolved_actor_id = self._build_memory_user_id(user_id, guild_id)

        resolved_message_id = message_id
        if not resolved_message_id:
            try:
                from ...services.turn_context import get_turn_context

                turn = get_turn_context()
                resolved_message_id = turn.message_id or turn.client_message_id
            except Exception:
                resolved_message_id = None

        try:
            from ...services.scoped_memory_job_service import (
                enqueue_scoped_memory_job,
            )

            job = await enqueue_scoped_memory_job(
                user_id=resolved_actor_id,
                session_id=durable_session_id,
                project_id=None,
                user_input=str(user_input or ""),
                assistant_response=str(assistant_response),
                message_id=(
                    str(resolved_message_id).strip()
                    if resolved_message_id is not None
                    else None
                )
                or None,
                privacy_config=getattr(self.llm_client, "config", None),
                session_context=getattr(self.llm_client, "_privacy_session_context", None),
                project_metadata=getattr(self.llm_client, "_privacy_project_metadata", None),
            )
            self._schedule_scoped_memory_job(job)
            return job
        except Exception as exc:
            logger.warning(
                "Discord Scoped Memory job enqueue failed session=%s message=%s: %s",
                session_id,
                resolved_message_id,
                exc,
            )
            return None

    async def process_text(
        self,
        text: str,
        user_id: int = None,
        guild_id: int = None,
        session_id: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
        channel_id: Optional[int] = None,
        message_id: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> str:
        """Process text message and generate response
        
        Args:
            text: Input text from user
            user_id: Discord user ID (optional)
            guild_id: Discord guild ID (optional)
            
        Returns:
            Generated response text
        """
        # Ordinary LLM requests still use the historical shared client.  Lock
        # only this guild/user session so independent Discord users do not
        # serialize one another or overwrite each other's context.
        context_lock = self._get_session_lock(user_id, guild_id)
        await context_lock.acquire()
        llm_context_lock = self._get_llm_context_lock()
        try:
            await llm_context_lock.acquire()
        except BaseException:
            context_lock.release()
            raise

        turn_context_token = None
        try:
            # セッションコンテキストとメモリ関連情報を設定
            self._set_llm_session_context(
                user_id,
                guild_id,
                session_id=session_id,
                runtime_session_id=runtime_session_id,
            )

            # Tool/direct-provider calls consult this task-local context.  It
            # prevents concurrent Discord requests from borrowing the shared
            # client's last-writer identity even though the existing client
            # session mutation is retained for ordinary chat persistence.
            try:
                from ...services.turn_context import set_turn_context

                turn_context_token = set_turn_context(
                    user_id=self._build_memory_user_id(user_id, guild_id),
                    project_id=None,
                    # TurnContext.session_id is the durable ConversationSession
                    # identity consumed by memory/tools.  Keep the runtime turn
                    # UUID separate in the explicit runtime_session_id argument
                    # and usage proxy; never let it replace the durable scope.
                    session_id=session_id,
                    message_id=message_id,
                )
            except Exception:
                turn_context_token = None

            # コンテキストの取得または作成
            context = self._get_or_create_context(user_id, guild_id)

            # メッセージ履歴に追加
            if 'history_manager' not in context:
                context['history_manager'] = HistoryManager(
                    max_history_length=self.config.get('discord.max_history_length', 20)
                )

            context['history_manager'].add_message('user', text)

            # LLMで応答生成
            self._stage(
                "generation_started",
                user_id=user_id,
                guild_id=guild_id,
                channel_id=channel_id,
                session_id=session_id,
            )
            response = await self._generate_response_with_context(text, context)
            if response is None:
                self._stage(
                    "generation_failed",
                    user_id=user_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    session_id=session_id,
                    exception="provider_returned_no_response",
                )
            self._stage(
                "generation_finished",
                user_id=user_id,
                guild_id=guild_id,
                channel_id=channel_id,
                session_id=session_id,
            )

            if response:
                await self._enqueue_scoped_memory_job(
                    text,
                    response,
                    user_id=user_id,
                    guild_id=guild_id,
                    session_id=session_id,
                    message_id=message_id,
                    actor_id=actor_id,
                )

            # 応答を履歴に追加
            if response:
                context['history_manager'].add_message('assistant', response)

                # Check for background summarization
                if hasattr(self.llm_client, 'check_and_summarize_history'):
                    self.llm_client.check_and_summarize_history(context['history_manager'])

            return response or "申し訳ありません。応答の生成に失敗しました。"
            
        except Exception as e:
            self._stage(
                "generation_failed",
                user_id=user_id,
                guild_id=guild_id,
                channel_id=channel_id,
                session_id=session_id,
                exception=repr(e),
            )
            logger.error(f"Error processing text: {e}", exc_info=True)
            return "エラーが発生しました。もう一度お試しください。"
        finally:
            if turn_context_token is not None:
                try:
                    from ...services.turn_context import reset_turn_context

                    reset_turn_context(turn_context_token)
                except Exception:
                    logger.debug("Failed to reset Discord turn context", exc_info=True)
            llm_context_lock.release()
            context_lock.release()
    
    async def process_text_with_images(
        self,
        text: str,
        image_urls: List[str],
        user_id: int = None,
        guild_id: int = None,
        session_id: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
        channel_id: Optional[int] = None,
        message_id: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> str:
        """Process text message with images and generate response
        
        Args:
            text: Input text from user
            image_urls: List of image URLs
            user_id: Discord user ID (optional)
            guild_id: Discord guild ID (optional)
            
        Returns:
            Generated response text
        """
        context_lock = self._get_session_lock(user_id, guild_id)
        await context_lock.acquire()
        llm_context_lock = self._get_llm_context_lock()
        try:
            await llm_context_lock.acquire()
        except BaseException:
            context_lock.release()
            raise
        turn_context_token = None
        try:
            self._set_llm_session_context(
                user_id,
                guild_id,
                session_id=session_id,
                runtime_session_id=runtime_session_id,
            )
            try:
                from ...services.turn_context import set_turn_context

                turn_context_token = set_turn_context(
                    user_id=self._build_memory_user_id(user_id, guild_id),
                    project_id=None,
                    # Scoped Memory and turn-local tools must observe the
                    # durable ConversationSession UUID.  Runtime identity is
                    # carried separately for usage/queue correlation.
                    session_id=session_id,
                    message_id=message_id,
                )
            except Exception:
                turn_context_token = None

            # Direct vision calls use an immutable request-local proxy for usage
            # attribution and never rely on another user's mutable context.
            usage_client = self._build_usage_proxy(
                user_id,
                guild_id,
                session_id=session_id,
                runtime_session_id=runtime_session_id,
            )
            context = self._get_or_create_context(user_id, guild_id)

            # ダウンロード・生成はこの論理ターン内で一度だけ行う。
            images_data = []
            urls = list(dict.fromkeys(str(url) for url in (image_urls or []) if str(url).strip()))
            if len(urls) > self.max_image_urls:
                logger.warning(
                    "Discord image limit exceeded; dropping %d URL(s) "
                    "guild=%s user=%s channel=%s",
                    len(urls) - self.max_image_urls,
                    guild_id,
                    user_id,
                    channel_id,
                )
            urls = urls[: self.max_image_urls]
            timeout = aiohttp.ClientTimeout(total=self.image_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as http_session:
                for url in urls:
                    url_path = self._safe_image_url_path(url)
                    try:
                        parts = urlsplit(url)
                        if parts.scheme.lower() not in {"http", "https"}:
                            logger.warning(
                                "Discord image download rejected non-http URL path=%s",
                                url_path,
                            )
                            continue
                        async with http_session.get(url) as resp:
                            if resp.status != 200:
                                logger.warning(
                                    "Discord image download failed status=%s path=%s",
                                    resp.status,
                                    url_path,
                                )
                                continue

                            headers = getattr(resp, "headers", {}) or {}
                            content_length = headers.get("Content-Length")
                            try:
                                if content_length is not None and int(content_length) > self.max_image_bytes:
                                    logger.warning(
                                        "Discord image exceeds byte limit bytes=%s limit=%s path=%s",
                                        content_length,
                                        self.max_image_bytes,
                                        url_path,
                                    )
                                    continue
                            except (TypeError, ValueError):
                                # Ignore malformed Content-Length; enforce the
                                # limit again after reading the body.
                                pass

                            image_bytes = await asyncio.wait_for(
                                self._read_bounded_image_response(resp),
                                timeout=self.image_timeout,
                            )
                            if image_bytes is None:
                                logger.warning(
                                    "Discord image exceeds byte limit limit=%s path=%s",
                                    self.max_image_bytes,
                                    url_path,
                                )
                                continue

                            content_type = self._resolve_image_mime_type(
                                image_bytes,
                                headers.get("Content-Type"),
                                url,
                            )
                            if not content_type:
                                logger.warning(
                                    "Discord image content type is not an image path=%s",
                                    url_path,
                                )
                                continue
                            images_data.append({
                                'data': image_bytes,
                                'mime_type': content_type,
                                'url': url,
                            })
                    except Exception as exc:
                        safe_error = str(exc).replace(str(url), url_path)
                        logger.error(
                            "Error downloading Discord image path=%s type=%s: %s",
                            url_path,
                            type(exc).__name__,
                            safe_error,
                        )

            if not text:
                text = "この画像について説明してください。"
            self._stage(
                "generation_started",
                user_id=user_id,
                guild_id=guild_id,
                channel_id=channel_id,
                session_id=session_id,
                image_count=len(images_data),
            )
            response = await self._generate_response_with_images(
                text,
                images_data,
                context,
                usage_client=usage_client,
            )
            if response is None:
                self._stage(
                    "generation_failed",
                    user_id=user_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    session_id=session_id,
                    exception="provider_returned_no_response",
                )
            self._stage(
                "generation_finished",
                user_id=user_id,
                guild_id=guild_id,
                channel_id=channel_id,
                session_id=session_id,
                image_count=len(images_data),
            )

            # Direct vision providers bypass AgentLLMClient's ordinary
            # persistence hook.  Persist the complete multimodal turn exactly
            # once against the durable ConversationSession when available.
            if response and session_id:
                memory_manager = getattr(self.llm_client, 'memory_manager', None)
                add_to_session = getattr(memory_manager, 'add_message_to_session', None)
                if callable(add_to_session):
                    try:
                        async def persist(role: str, content: str) -> None:
                            kwargs = {
                                'session_id': session_id,
                                'role': role,
                                'content': content,
                                'llm_client': self.llm_client,
                            }
                            try:
                                result = add_to_session(**kwargs)
                            except TypeError as exc:
                                if 'llm_client' not in str(exc):
                                    raise
                                kwargs.pop('llm_client', None)
                                result = add_to_session(**kwargs)
                            if hasattr(result, '__await__'):
                                await result

                        await persist('user', f"{text} [画像{len(images_data)}枚]")
                        await persist('assistant', response)
                    except Exception as exc:
                        logger.warning(
                            "Discord multimodal history persistence failed session=%s: %s",
                            session_id,
                            exc,
                            exc_info=True,
                        )

            if response:
                await self._enqueue_scoped_memory_job(
                    f"{text} [画像{len(images_data)}枚]",
                    response,
                    user_id=user_id,
                    guild_id=guild_id,
                    session_id=session_id,
                    message_id=message_id,
                    actor_id=actor_id,
                )

            if response:
                if 'history_manager' not in context:
                    context['history_manager'] = HistoryManager(
                        max_history_length=self.config.get('discord.max_history_length', 20)
                    )
                # Store one user item for the complete multimodal turn.
                context['history_manager'].add_message(
                    'user', f"{text} [画像{len(images_data)}枚]"
                )
                context['history_manager'].add_message('assistant', response)
                if hasattr(self.llm_client, 'check_and_summarize_history'):
                    self.llm_client.check_and_summarize_history(context['history_manager'])
            return response or "申し訳ありません。画像の処理に失敗しました。"
        except Exception as exc:
            self._stage(
                "generation_failed",
                user_id=user_id,
                guild_id=guild_id,
                channel_id=channel_id,
                session_id=session_id,
                exception=repr(exc),
            )
            logger.error("Error processing text with images: %s", exc, exc_info=True)
            return "エラーが発生しました。画像の処理中に問題が発生しました。"
        finally:
            if turn_context_token is not None:
                try:
                    from ...services.turn_context import reset_turn_context

                    reset_turn_context(turn_context_token)
                except Exception:
                    logger.debug("Failed to reset Discord turn context", exc_info=True)
            llm_context_lock.release()
            context_lock.release()

    @staticmethod
    def _safe_image_url_path(url: Any) -> str:
        """Return a query-free URL path for diagnostics.

        Discord attachment URLs are often signed.  Logging the complete URL
        would expose query credentials, so only the path is retained.
        """
        try:
            path = urlsplit(str(url)).path or "/"
        except Exception:
            path = "/"
        return path[:512]

    async def _read_bounded_image_response(self, response: Any) -> Optional[bytes]:
        """Read an HTTP image body without allocating beyond the configured cap."""
        content = getattr(response, "content", None)
        iter_chunked = getattr(content, "iter_chunked", None)
        if callable(iter_chunked):
            chunks: List[bytes] = []
            total = 0
            async for chunk in iter_chunked(min(64 * 1024, self.max_image_bytes + 1)):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.max_image_bytes:
                    return None
                chunks.append(bytes(chunk))
            return b"".join(chunks)

        # Do not fall back to ``response.read()`` here.  Generic adapters may
        # accept a size argument and ignore it, allocating an unbounded body
        # before returning.  aiohttp's production response exposes
        # ``content.iter_chunked`` above; adapters without a streaming API are
        # rejected safely instead of risking an oversized allocation.
        logger.warning(
            "Discord image adapter lacks bounded streaming reads; rejecting response"
        )
        return None

    @staticmethod
    def _resolve_image_mime_type(
        image_bytes: bytes,
        content_type: Optional[str],
        url: str,
    ) -> Optional[str]:
        """Validate/infer an image MIME type without trusting URL queries."""
        header_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if header_type and not header_type.startswith("image/"):
            return None
        if header_type.startswith("image/"):
            return header_type

        path_type = mimetypes.guess_type(urlsplit(str(url)).path)[0]
        if path_type and path_type.startswith("image/"):
            return path_type

        # Some CDN responses omit Content-Type and use extensionless signed
        # paths.  Let Pillow inspect the bounded bytes before rejecting them.
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image_format = str(image.format or "").lower()
            aliases = {"jpg": "jpeg", "jpe": "jpeg"}
            image_format = aliases.get(image_format, image_format)
            return f"image/{image_format}" if image_format else None
        except Exception:
            return None
    
    async def process_voice(
        self,
        text: str,
        user_id: int = None,
        guild_id: int = None,
        session_id: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
    ) -> Optional[str]:
        """Process voice input text and generate response"""
        return await self.process_text(
            text,
            user_id,
            guild_id,
            session_id=session_id,
            runtime_session_id=runtime_session_id,
        )

    async def prefill_context_from_memory(
        self,
        user_id: Optional[int],
        guild_id: Optional[int],
        max_messages: Optional[int] = None,
        session_id: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
    ) -> bool:
        """Load recent conversation history from persistent memory for context.

        Prefill runs before a turn enters the Discord worker, so it must share
        the same per-session lock as generation/reset.  The reset epoch and
        durable/runtime identity are captured before the potentially slow
        repository call and checked again before mutating local history.  A
        result fetched for a pre-``/clear`` session is therefore discarded
        instead of being re-injected into the freshly-cleared context.
        """
        memory_manager = getattr(self.llm_client, 'memory_manager', None)
        if not memory_manager or user_id is None:
            return False

        # Prefill is normally started by the message/voice ingress path, not
        # by the queue worker itself.  Track the task so mode cleanup can
        # cancel an in-flight repository request and wait for its finally
        # block before clearing the context maps.  ``_closed`` is a shutdown
        # generation boundary: an old fetch must never repopulate history
        # after cleanup has reset epochs/maps back to their defaults.
        if getattr(self, "_closed", False):
            return False
        memory_user_id = self._build_memory_user_id(user_id, guild_id)
        worker_key = (
            int(guild_id) if guild_id is not None else None,
            int(user_id),
        )
        reset_epochs = getattr(self, "_session_reset_epochs", {})
        session_identities = getattr(self, "_session_identities", {})
        captured_epoch = reset_epochs.get(worker_key, 0)
        requested_identity = (session_id, runtime_session_id)

        def identity_is_current() -> bool:
            if getattr(self, "_closed", False):
                return False
            current_identity = session_identities.get(worker_key)
            if current_identity is None:
                return True
            # Callers that do not thread IDs retain the legacy behavior; when
            # supplied, both durable and runtime identities must still match
            # the active queue boundary.
            if session_id is None and runtime_session_id is None:
                return True
            return current_identity == requested_identity

        if not identity_is_current():
            return False

        prefill_tasks = getattr(self, "_prefill_tasks", None)
        current_task = asyncio.current_task()
        if prefill_tasks is not None and current_task is not None:
            prefill_tasks.add(current_task)

        context_lock = self._get_session_lock(user_id, guild_id)
        lock_acquired = False
        try:
            await context_lock.acquire()
            lock_acquired = True
        except BaseException:
            # Cancellation while waiting for a generation/reset lock must
            # still unregister this task; cleanup otherwise retains a stale
            # task object and can repeatedly try to cancel it.
            if prefill_tasks is not None and current_task is not None:
                prefill_tasks.discard(current_task)
            raise
        try:
            # /clear may have started while this prefill was waiting for the
            # lock.  Never query or mutate history across that boundary.
            if reset_epochs.get(worker_key, 0) != captured_epoch or not identity_is_current():
                return False
            if self._memory_prefill_attempts.get(memory_user_id):
                return False

            message_limit = max_messages or self.config.get('discord.memory_prefill_message_count', 10)
            message_limit = max(2, min(message_limit, self.config.get('discord.max_history_length', 20)))

            try:
                messages = await memory_manager.get_recent_messages(
                    memory_user_id,
                    self.character_name,
                    count=message_limit
                )
            except Exception as exc:
                logger.error(f"Failed to prefill memory context: {exc}")
                # Do not mark the attempt complete when a reset raced the
                # repository call; the new session may legitimately prefill.
                if reset_epochs.get(worker_key, 0) == captured_epoch and identity_is_current():
                    self._memory_prefill_attempts[memory_user_id] = True
                return False

            # A reset can increment the epoch while the repository call is in
            # flight.  Discard the fetched rows before touching local history.
            if reset_epochs.get(worker_key, 0) != captured_epoch or not identity_is_current():
                return False
            if not messages:
                self._memory_prefill_attempts[memory_user_id] = True
                return False

            context = self._get_or_create_context(user_id, guild_id)
            if 'history_manager' not in context:
                context['history_manager'] = HistoryManager(
                    max_history_length=self.config.get('discord.max_history_length', 20)
                )

            context['history_manager'].clear()

            for msg in messages:
                role = msg.get('role', 'user')
                content = msg.get('content')
                if content:
                    context['history_manager'].add_message(role, content)

            self._memory_prefill_attempts[memory_user_id] = True
            return True
        finally:
            if lock_acquired:
                context_lock.release()
            if prefill_tasks is not None and current_task is not None:
                prefill_tasks.discard(current_task)

    def should_prefill_context_from_memory(self) -> bool:
        """Return whether Discord ingress must perform legacy user prefill.

        Native providers that own durable ``ConversationSession`` loading
        (advertised by ``manages_conversation_session_history``) must receive
        an untouched local history and load the active session themselves.
        The method-name fallback keeps compatibility with older AgentLLMClient
        instances that expose ``_sync_history_with_current_session`` before
        the explicit capability marker was added.  Providers without either
        capability retain the legacy user-scoped prefill path.
        """
        client = getattr(self, "llm_client", None)
        if bool(getattr(client, "manages_conversation_session_history", False)):
            return False
        return not callable(
            getattr(client, "_sync_history_with_current_session", None)
        )

    def _build_memory_user_id(self, user_id: Optional[int], guild_id: Optional[int]) -> str:
        # Keep DMs in a distinct tenant slot so ``discord:<user>`` cannot
        # collide with a guild principal or legacy one-part namespace.
        parts = ['discord', str(guild_id) if guild_id is not None else 'dm']
        if user_id is not None:
            parts.append(str(user_id))
        return ':'.join(parts)

    def _set_llm_session_context(
        self,
        user_id: Optional[int],
        guild_id: Optional[int],
        session_id: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
    ) -> None:
        set_context = getattr(self.llm_client, 'set_session_context', None)
        if not callable(set_context) and not hasattr(self.llm_client, 'current_session_id'):
            return

        memory_user_id = self._build_memory_user_id(user_id, guild_id)
        resolved_session_id = session_id or self._build_discord_session_id(
            user_id,
            guild_id,
        )
        metadata = {
            'platform': 'discord',
            'guild_id': str(guild_id) if guild_id is not None else None,
            'mode': self.mode,
            'session_id': resolved_session_id,
            'runtime_session_id': runtime_session_id,
        }

        try:
            if callable(set_context):
                set_context(
                    user_id=memory_user_id,
                    metadata=metadata
                )
            # LLM clients historically expose ``current_session_id`` as a
            # mutable field rather than accepting it in set_session_context.
            # Keep that compatibility path, but set only the durable
            # ConversationSession UUID.  Runtime turn identity is carried by
            # ``turn_context``/``_DiscordUsageProxy`` separately.
            if resolved_session_id is not None and hasattr(
                self.llm_client,
                'current_session_id',
            ):
                self.llm_client.current_session_id = str(resolved_session_id)
            self.llm_client.generation_policy = generation_policy_for_profile(
                GenerationProfile.CHAT
            )
        except Exception as exc:
            logger.debug(f"Failed to set session context: {exc}")

    async def _call_llm_generate_response(self, text: str) -> Optional[str]:
        """Call LLM generate_response without blocking the event loop"""
        try:
            if hasattr(self.llm_client, 'generate_response_async'):
                return await self.llm_client.generate_response_async(text)

            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: self.llm_client.generate_response(text, stream=False)
            )
        except Exception as exc:
            logger.error(f"Failed to invoke LLM response: {exc}")
            return None
    
    def _get_or_create_context(self, user_id: Optional[int] = None, guild_id: Optional[int] = None) -> Dict[str, Any]:
        """Get or create context for user/guild
        
        Args:
            user_id: Discord user ID
            guild_id: Discord guild ID
            
        Returns:
            Context dictionary
        """
        # ユーザーコンテキストを優先
        if user_id is not None:
            key = self._context_key(user_id, guild_id)
            if key not in self.user_contexts:
                self.user_contexts[key] = {
                    'history_manager': HistoryManager(
                        max_history_length=self.config.get('discord.max_history_length', 20)
                    ),
                    'guild_id': guild_id,
                    'user_id': user_id,
                    'character': self.character_name
                }
            return self.user_contexts[key]
        
        # ギルドコンテキスト
        if guild_id is not None:
            if guild_id not in self.guild_contexts:
                self.guild_contexts[guild_id] = {
                    'history_manager': HistoryManager(
                         max_history_length=self.config.get('discord.max_history_length', 20)
                    ),
                    'character': self.character_name
                }
            return self.guild_contexts[guild_id]
        
        # デフォルトコンテキスト
        return {
            'history_manager': HistoryManager(
                max_history_length=self.config.get('discord.max_history_length', 20)
            ), 
            'character': self.character_name
        }
    
    async def _generate_response_with_context(self, text: str, context: Dict[str, Any]) -> Optional[str]:
        """Generate response with context
        
        Args:
            text: Input text
            context: Context dictionary
            
        Returns:
            Generated response or None
        """
        try:
            # キャラクター設定の確認
            character = context.get('character', self.character_name)
            if character != self.character_name:
                # キャラクターが変更された場合は再初期化
                self.character_name = character
                self.character_config = self.config.get_character_config(character)
                self._init_common_components()
            
            # Function callingを使用するかチェック
            use_tools = self.config.get('use_tools', True)
            
            if use_tools and hasattr(self.llm_client, 'generate_response'):
                # 既存のLLMマネージャーを使用（ツール対応）
                # 会話履歴を設定
                if hasattr(self.llm_client, 'conversation_history'):
                    # 最近の会話履歴を設定
                    self.llm_client.conversation_history = []
                    
                    history_manager = context.get('history_manager')
                    messages = history_manager.get_context(10) if history_manager else []

                    # ``process_text`` records the current user turn in the
                    # display history before invoking the provider.  Gemini's
                    # ``generate_response`` appends ``text`` itself when it
                    # builds the provider request, so forwarding that newest
                    # history item would send the current user message twice.
                    # Keep only the persisted/past transcript here; other
                    # providers that expose ``conversation_history`` receive
                    # the same canonical past-only list and append their turn
                    # according to their own request builder.
                    if (
                        messages
                        and messages[-1].get('role') == 'user'
                        and messages[-1].get('content') == text
                    ):
                        messages = messages[:-1]

                    for msg in messages:
                        if msg['role'] == 'user':
                            self.llm_client.conversation_history.append({
                                'role': 'user',
                                'content': msg['content']
                            })
                        elif msg['role'] == 'assistant':
                            self.llm_client.conversation_history.append({
                                'role': 'assistant', 
                                'content': msg['content']
                            })
                
                # ツール付きで応答生成（非同期呼び出しでイベントループをブロックしない）
                response = await self._call_llm_generate_response(text)
            else:
                # 通常の応答生成
                response = await self._generate_with_interrupt_check(
                    text=text,
                    task_id=f"discord-{context.get('guild_id', 'dm')}"
                )
            
            return response
            
        except Exception as e:
            logger.error(f"Error generating response: {e}", exc_info=True)
            return None
    
    async def _generate_response_with_images(
        self,
        text: str,
        images_data: List[Dict],
        context: Dict[str, Any],
        *,
        usage_client: Any = None,
        usage_context: Any = None,
    ) -> Optional[str]:
        """Generate response with images
        
        Args:
            text: Input text
            images_data: List of image data dictionaries ({'data': bytes, 'mime_type': str})
            context: Context dictionary
            
        Returns:
            Generated response or None
        """
        started = time.monotonic()
        try:
            request_usage_context = self._coerce_usage_context(
                usage_context if usage_context is not None else usage_client
            )

            # キャラクター設定の確認
            character = context.get('character', self.character_name)
            if character != self.character_name:
                # キャラクターが変更された場合は再初期化
                self.character_name = character
                self.character_config = self.config.get_character_config(character)
                self._init_common_components()
            
            vision_model = self.config.get('discord.vision_model', 'gemini-3-flash-preview')
            logger.info(f"Using vision model: {vision_model}")
            
            if 'gemini' in vision_model.lower():
                # Gemini APIを使用
                api_key = self.config.get('gemini_api_key') or os.getenv('GOOGLE_API_KEY') or os.getenv('GEMINI_API_KEY')
                if not api_key:
                    logger.error("API Key for Gemini is not set (checked gemini_api_key, GOOGLE_API_KEY, GEMINI_API_KEY)")
                    return "申し訳ありません。APIキー設定のエラーです。"
                
                genai.configure(api_key=api_key)
                
                # モデル設定
                model = genai.GenerativeModel(
                    model_name=vision_model,
                    system_instruction=self.character_config.get('personality', {}).get('details', 'あなたは親切なAIアシスタントです。')
                )
                
                # コンテンツ構築
                content_parts = []
                
                # テキスト追加
                content_parts.append(text)
                
                # 画像追加
                for img_data in images_data:
                    try:
                        # バイト列からPIL Imageを作成
                        image = Image.open(io.BytesIO(img_data['data']))
                        content_parts.append(image)
                    except Exception as e:
                        logger.error(f"Error processing image for Gemini: {e}")
                
                # 会話履歴を考慮（簡易的）
                history_text = ""
                history_manager = context.get('history_manager')
                if history_manager:
                    # 最新5件を取得
                    for msg in history_manager.get_context(5):
                        role = "ユーザー" if msg['role'] == 'user' else "あなた"
                        content = msg['content']
                        # 画像プレースホルダーを除去
                        content = content.split('[画像')[0].strip()
                        history_text += f"{role}: {content}\n"
                
                if history_text:
                    prompt = f"これまでの会話:\n{history_text}\n\nユーザーの入力: {text}"
                    content_parts[0] = prompt

                privacy_gateway = OutboundPrivacyGateway(
                    self.config,
                    session_id=str(
                        getattr(request_usage_context, "current_session_id", "") or ""
                    ),
                    user_id=str(getattr(request_usage_context, "user_id", "") or ""),
                    session_context=getattr(
                        request_usage_context, "session_context", None
                    ),
                    project_metadata=getattr(
                        request_usage_context, "project_metadata", None
                    ),
                )
                protected = privacy_gateway.protect_sync(
                    {"text": content_parts[0], "media": images_data},
                    provider="gemini",
                    source_kind="discord_vision",
                )
                if isinstance(protected.payload, dict):
                    content_parts[0] = str(
                        protected.payload.get("text") or content_parts[0]
                    )
                
                # 生成実行
                response = await asyncio.to_thread(
                    model.generate_content,
                    content_parts
                )

                # This direct Gemini call is not routed through llm_client's
                # ordinary chat recorder.  Record only provider-reported usage
                # and only after a successful response object is available.
                self._record_direct_vision_usage(
                    response,
                    provider="gemini",
                    requested_model=vision_model,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    usage_client=usage_client,
                    usage_context=usage_context,
                )

                # This direct Gemini request has its own session-scoped
                # gateway.  Restore aliases only on the local user-facing
                # response; never expose the protected placeholders emitted
                # by the provider.
                return privacy_gateway.restore(getattr(response, "text", "") or "") or None
                
            else:
                # OpenAI APIを使用 (GPT-4oなど)
                import openai
                client = openai.OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
                
                # システム指示（Responses APIのinstructionsへ）
                instructions = self.character_config.get('personality', {}).get('details', 'あなたは親切なAIアシスタントです。')

                # 入力メッセージを構築
                input_messages = []

                # 会話履歴を追加（テキストのみ）
                history_manager = context.get('history_manager')
                if history_manager:
                    for msg in history_manager.get_context(10):  # 最近の10件
                        if msg['role'] in ['user', 'assistant']:
                            input_messages.append({
                                "role": msg['role'],
                                "content": msg['content']
                            })

                # 画像データをResponses API形式に変換
                openai_images = []
                for img_data in images_data:
                    base64_image = base64.b64encode(img_data['data']).decode('utf-8')
                    openai_images.append({
                        'type': 'input_image',
                        'image_url': f"data:{img_data['mime_type']};base64,{base64_image}"
                    })

                # 現在のメッセージを追加（画像付き）
                user_message = {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": text}
                    ]
                }
                user_message["content"].extend(openai_images)
                input_messages.append(user_message)

                privacy_gateway = OutboundPrivacyGateway(
                    self.config,
                    session_id=str(
                        getattr(request_usage_context, "current_session_id", "") or ""
                    ),
                    user_id=str(getattr(request_usage_context, "user_id", "") or ""),
                    session_context=getattr(
                        request_usage_context, "session_context", None
                    ),
                    project_metadata=getattr(
                        request_usage_context, "project_metadata", None
                    ),
                )
                protected = privacy_gateway.protect_sync(
                    {"instructions": instructions, "input": input_messages},
                    provider="openai",
                    source_kind="discord_vision",
                )
                if isinstance(protected.payload, dict):
                    instructions = str(protected.payload.get("instructions") or instructions)
                    input_messages = protected.payload.get("input") or input_messages

                # GPT-4oで応答生成
                # The OpenAI SDK call is synchronous.  Run it off the event
                # loop so a slow network request cannot block every Discord
                # session worker.
                response = await asyncio.to_thread(
                    client.responses.create,
                    model=vision_model,  # configから取得したモデル名を使用
                    instructions=instructions,
                    input=input_messages,
                    max_output_tokens=1000,
                )

                # OpenAI Responses usage is available as response.usage when
                # the endpoint reports it.  Do not estimate image token cost
                # when this field is absent.
                self._record_direct_vision_usage(
                    response,
                    provider="openai",
                    requested_model=vision_model,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    usage_client=usage_client,
                    usage_context=usage_context,
                )

                return privacy_gateway.restore(
                    getattr(response, "output_text", "") or ""
                ) or None
            
        except Exception as e:
            logger.error(f"Error generating response with images: {e}", exc_info=True)
            return None
    
    def set_character(self, character: str, user_id: Optional[int] = None, guild_id: Optional[int] = None):
        """Set character for context
        
        Args:
            character: Character name
            user_id: Discord user ID (optional)
            guild_id: Discord guild ID (optional)
        """
        context = self._get_or_create_context(user_id, guild_id)
        context['character'] = character
        
        # 現在のコンテキストのキャラクターを変更
        if (
            user_id is not None
            and self.user_contexts.get(self._context_key(user_id, guild_id)) == context
        ) or (guild_id is not None and self.guild_contexts.get(guild_id) == context):
            self.character_name = character
            self.character_config = self.config.get_character_config(character)
            self._init_common_components()
    
    def clear_context(self, user_id: Optional[int] = None, guild_id: Optional[int] = None):
        """Clear conversation context
        
        Args:
            user_id: Discord user ID (optional)
            guild_id: Discord guild ID (optional)
        """
        if user_id is not None:
            # A rotated durable session may prefill again.  Invalidate the
            # once-per-memory-namespace guard while clearing its local copy.
            prefill_attempts = getattr(self, "_memory_prefill_attempts", None)
            if prefill_attempts is not None:
                prefill_attempts.pop(
                    self._build_memory_user_id(user_id, guild_id),
                    None,
                )
            keys = [
                key for key in self.user_contexts
                if key[1] == user_id and (guild_id is None or key[0] == guild_id)
            ]
            for key in keys:
                if 'history_manager' in self.user_contexts[key]:
                    self.user_contexts[key]['history_manager'].clear()
        elif guild_id and guild_id in self.guild_contexts:
            if 'history_manager' in self.guild_contexts[guild_id]:
                self.guild_contexts[guild_id]['history_manager'].clear()
