"""
Discord bot implementation for AoiTalk
"""

import asyncio
import logging
import mimetypes
import os
import re
import sys
import weakref
from typing import Dict, Optional, Any
from urllib.parse import urlsplit

import discord
from discord.ext import commands

from ..config import Config
from ..runtime_features import runtime_feature_manager
from .handlers.command_handler import CommandHandler
from .handlers.session_handler import SessionHandler
from .handlers.voice_handler import VoiceHandler
from .logging import setup_discord_logging

logger = logging.getLogger(__name__)


def _safe_print(message: str) -> None:
    """Print Discord startup diagnostics even on cp932 Windows consoles."""
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(message.encode(encoding, errors="replace").decode(encoding, errors="replace"))


class AoiTalkBot(commands.Bot):
    """Discord Bot for AoiTalk voice assistant"""
    
    def __init__(self, config: Config) -> None:
        # Discord Bot設定
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True
        # intents.members = True  # 特権インテント（必要な場合のみ有効化）
        
        super().__init__(command_prefix='/', intents=intents)
        
        self.config = config
        self.session_handler = SessionHandler(config)
        self.voice_handler = VoiceHandler(config)
        self.voice_handler._bot_instance = self  # Set bot instance reference
        self.command_handler = CommandHandler(self)
        self._command_sync_done = False
        self._background_tasks: set[asyncio.Task] = set()
        self._close_lock = asyncio.Lock()
        self._resources_closed = False
        # IDs of text replies emitted by this bot.  Once a channel has a
        # tracked response, unknown bot-authored references (slash commands,
        # VC announcements, or another bot) are not treated as chat turns.
        self._bot_text_reply_ids: set[int] = set()
        self._bot_text_reply_channels: set[int] = set()
        
        # グローバル設定
        self.default_character = config.get('default_character', 'ずんだもん')
        self.default_mode = self.session_handler.default_mode  # 'text' or 'voice'
        
    async def setup_hook(self) -> None:
        """Bot起動時の初期設定"""
        # コマンドの登録
        await self.command_handler.setup_commands()

    def _remember_bot_text_reply(self, sent: Any, channel_id: Any) -> None:
        """Remember a text reply without retaining the Discord message object."""
        if sent is None:
            return
        try:
            if channel_id is not None:
                self._bot_text_reply_channels.add(int(channel_id))
            message_id = getattr(sent, "id", None)
            if message_id is not None:
                self._bot_text_reply_ids.add(int(message_id))
                # Bound process-local bookkeeping; Discord IDs are snowflakes
                # and the set is only a routing hint, not durable history.
                if len(self._bot_text_reply_ids) > 2048:
                    self._bot_text_reply_ids = set(
                        sorted(self._bot_text_reply_ids)[-1024:]
                    )
        except (TypeError, ValueError):
            return

    def _is_bot_text_reply_reference(
        self,
        referenced: Any,
        *,
        reference_id: Any,
        channel_id: Any,
    ) -> bool:
        """Classify a referenced bot message as a normal text reply.

        A slash-command/VC announcement is also authored by the bot, so an
        author-ID check alone is too broad.  Messages emitted by
        ``message.reply`` carry a source ``reference``.  For an unknown
        channel (e.g. after restart), accept that shape; once this process has
        tracked a channel, accept only the exact IDs it emitted.  Real
        discord.py messages always expose the ``reference`` attribute,
        including a ``None`` value for slash/VC posts; a missing attribute is
        therefore treated as an untrusted adapter payload.
        """
        if referenced is None:
            return False
        author = getattr(referenced, "author", None)
        bot_id = getattr(self.user, "id", None)
        is_bot = (
            (bot_id is not None and getattr(author, "id", None) == bot_id)
            or author is self.user
        )
        if not is_bot:
            return False
        if reference_id in self._bot_text_reply_ids:
            return True

        # discord.py marks interaction-originated messages explicitly.  Keep
        # this check before the source-reference fallback for adapters that
        # expose both fields.
        if getattr(referenced, "interaction_metadata", None) is not None:
            return False
        if getattr(referenced, "interaction", None) is not None:
            return False
        message_type = str(getattr(referenced, "type", "")).lower()
        if any(marker in message_type for marker in ("application_command", "interaction", "slash")):
            return False

        if not hasattr(referenced, "reference"):
            return False
        source_reference = getattr(referenced, "reference", None)
        has_source_reference = bool(
            source_reference is not None
            and getattr(source_reference, "message_id", None) is not None
        )
        if not has_source_reference:
            return False

        # A tracked channel may still receive an older normal LLM reply after
        # a process restart or local ledger eviction.  Keep it routable when
        # its source reference resolves to a human message; slash/VC posts do
        # not have this human-source reply shape.  Apply the bot-source guard
        # for unknown channels too, otherwise a bot-to-bot reference could be
        # mistaken for a user follow-up after the local ledger is lost.
        source_resolved = getattr(source_reference, "resolved", None)
        source_author = getattr(source_resolved, "author", None)
        if source_author is not None and (
            (bot_id is not None and getattr(source_author, "id", None) == bot_id)
            or source_author is self.user
        ):
            return False
        return True

    async def close(self) -> None:
        """Close Discord and always drain AoiTalk-owned resources.

        ``run_bot`` is not the only lifecycle entry point: tests, embedded
        integrations, and reconnect handlers may call ``await bot.close()``
        directly.  Keep cleanup idempotent so a ``run_bot`` finally block and
        an external close can safely overlap.
        """
        async with self._close_lock:
            if not self._resources_closed:
                self._resources_closed = True
                for task in list(getattr(self, "_background_tasks", set())):
                    if task and not task.done():
                        task.cancel()
                tasks = list(getattr(self, "_background_tasks", set()))
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await self.session_handler.shutdown()
                except Exception as exc:
                    logger.error(
                        "Discord session handler shutdown failed: %s",
                        exc,
                        exc_info=True,
                    )
                try:
                    await self.voice_handler.cleanup()
                except Exception as exc:
                    logger.error(
                        "Discord voice handler cleanup failed: %s",
                        exc,
                        exc_info=True,
                    )
            await super().close()

    def _should_sync_commands(self) -> tuple[bool, bool, str]:
        """Return command sync settings from env/config."""
        should_sync_by_env = os.getenv('DISCORD_SYNC_COMMANDS', 'false').lower() == 'true'
        should_sync_by_config = bool(self.config.get('discord.sync_commands', False))
        scope = (
            os.getenv('DISCORD_SYNC_COMMAND_SCOPE')
            or self.config.get('discord.sync_command_scope', 'guild')
            or 'guild'
        ).lower()
        return should_sync_by_env or should_sync_by_config, should_sync_by_env, scope

    def _get_configured_sync_guild_ids(self) -> list[int]:
        """Return configured guild IDs, or all joined guilds when unset."""
        raw_env = os.getenv('DISCORD_SYNC_GUILD_IDS', '').strip()
        if raw_env:
            values = [item.strip() for item in raw_env.split(',') if item.strip()]
        else:
            values = self.config.get('discord.sync_guild_ids', []) or []

        guild_ids: list[int] = []
        for value in values:
            try:
                guild_ids.append(int(value))
            except (TypeError, ValueError):
                logger.warning("Ignoring invalid Discord sync guild ID: %s", value)

        if guild_ids:
            return guild_ids

        return [guild.id for guild in self.guilds]

    async def _sync_application_commands(self) -> None:
        """Sync slash commands to Discord when explicitly enabled.

        Guild sync is intentionally supported because it updates immediately, while
        global command propagation can take much longer.
        """
        should_sync, should_sync_by_env, scope = self._should_sync_commands()
        if not should_sync or self._command_sync_done:
            return

        self._command_sync_done = True
        should_sync_by_config = bool(self.config.get('discord.sync_commands', False))
        sync_global = scope in {'global', 'both', 'guild_and_global'}
        sync_guilds = scope in {'guild', 'both', 'guild_and_global'}

        if not sync_global and not sync_guilds:
            logger.warning("Unknown Discord command sync scope '%s'; falling back to guild sync", scope)
            sync_guilds = True

        logger.info(
            "Syncing Discord slash commands (env=%s, config=%s, scope=%s)",
            should_sync_by_env,
            should_sync_by_config,
            scope,
        )

        if sync_global:
            try:
                synced = await self.tree.sync()
                logger.info("Synced %d global Discord command(s)", len(synced))
                _safe_print(f"✅ Discordグローバルコマンド同期: {len(synced)}件")
            except Exception as exc:
                logger.error("Failed to sync global Discord commands: %s", exc, exc_info=True)
                _safe_print(f"⚠️ Discordグローバルコマンド同期失敗: {exc}")

        if sync_guilds:
            guild_ids = self._get_configured_sync_guild_ids()
            if not guild_ids:
                logger.warning("No joined guilds available for Discord guild command sync")
                return

            for guild_id in guild_ids:
                try:
                    guild = discord.Object(id=guild_id)
                    self.tree.copy_global_to(guild=guild)
                    synced = await self.tree.sync(guild=guild)
                    logger.info("Synced %d Discord command(s) to guild %s", len(synced), guild_id)
                    _safe_print(f"✅ Discordギルドコマンド同期: guild={guild_id}, {len(synced)}件")
                except Exception as exc:
                    logger.error(
                        "Failed to sync Discord commands to guild %s: %s",
                        guild_id,
                        exc,
                        exc_info=True,
                    )
                    _safe_print(f"⚠️ Discordギルドコマンド同期失敗: guild={guild_id}, {exc}")
    
    async def on_ready(self) -> None:
        """Bot接続完了時のイベント"""
        logger.info(f'Logged in as {self.user} (ID: {self.user.id})')
        logger.info(f'Connected to {len(self.guilds)} guild(s)')
        from . import lifecycle

        lifecycle.mark_ready(user=self.user, guild_count=len(self.guilds))
        _safe_print(f'✅ Discord Bot ログイン成功: {self.user} (ID: {self.user.id})')
        _safe_print(f'✅ 接続サーバー数: {len(self.guilds)}')
        await self._sync_application_commands()
        
        # Start voice handler timeout check task
        if self.voice_handler._timeout_check_task is None or self.voice_handler._timeout_check_task.done():
            self.voice_handler._timeout_check_task = asyncio.create_task(
                self.voice_handler._periodic_timeout_check()
            )
            logger.info("Started voice handler timeout check task")
        
        # ステータスの設定
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="/help でヘルプを表示"
            )
        )
    
    async def on_message(self, message: discord.Message) -> None:
        """メッセージ受信時のイベント"""
        if not runtime_feature_manager.feature_enabled("discord_text"):
            return

        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        guild_id = getattr(guild, "id", None)
        channel_id = getattr(channel, "id", None)
        author_id = getattr(author, "id", None)
        bot_id = getattr(self.user, "id", None)
        self._log_stage(
            "message_received",
            message=message,
            guild_id=guild_id,
            channel_id=channel_id,
            author_id=author_id,
        )

        # Bot自身のメッセージはオブジェクト同一性ではなくIDで無視する。
        if (bot_id is not None and author_id == bot_id) or author is self.user:
            return

        # DMは現在サポートしない
        if isinstance(message.channel, discord.DMChannel):
            try:
                await message.channel.send("申し訳ありませんが、DMはサポートしていません。")
            except Exception as exc:
                self._log_stage("reply_send_failed", message=message, exception=exc)
            return

        if guild is None or guild_id is None:
            logger.warning("Discord message has no guild; ignoring channel=%s author=%s", channel_id, author_id)
            return
        if author_id is None:
            logger.warning("Discord message has no author ID; ignoring guild=%s channel=%s", guild_id, channel_id)
            return

        # メンションまたはBotへの返信を判定
        content_raw = str(getattr(message, "content", "") or "")
        mention_ids = {
            getattr(mentioned, "id", None)
            for mentioned in (getattr(message, "mentions", None) or [])
        }
        mention_ids.discard(None)
        raw_mention = bool(
            bot_id is not None
            and re.search(rf"<@!?{re.escape(str(bot_id))}>", content_raw)
        )
        is_mention = bool(
            (bot_id is not None and bot_id in mention_ids)
            or raw_mention
            or (self.user is not None and self.user in (getattr(message, "mentions", None) or []))
        )
        self._log_stage(
            "mention_detected",
            message=message,
            guild_id=guild_id,
            channel_id=channel_id,
            author_id=author_id,
            mention=is_mention,
        )
        is_reply_to_bot = False
        reference = getattr(message, "reference", None)
        reference_id = getattr(reference, "message_id", None) if reference else None
        if reference and reference_id:
            try:
                resolved = getattr(reference, "resolved", None)
                resolved_author = getattr(resolved, "author", None) if resolved is not None else None
                # discord.py may provide a DeletedReferencedMessage sentinel
                # with no author.  Treat it as unresolved and fetch by ID so a
                # normal bot reply remains routable when the cache is stale.
                if resolved is not None and resolved_author is not None:
                    is_reply_to_bot = self._is_bot_text_reply_reference(
                        resolved,
                        reference_id=reference_id,
                        channel_id=channel_id,
                    )
                else:
                    referenced_msg = await message.channel.fetch_message(reference_id)
                    referenced_author = getattr(referenced_msg, "author", None)
                    if referenced_author is None:
                        self._log_stage(
                            "reply_fetch_failed",
                            message=message,
                            guild_id=guild_id,
                            channel_id=channel_id,
                            author_id=author_id,
                            reference_id=reference_id,
                            exception=ValueError("referenced message has no author"),
                        )
                    else:
                        is_reply_to_bot = self._is_bot_text_reply_reference(
                            referenced_msg,
                            reference_id=reference_id,
                            channel_id=channel_id,
                        )
            except Exception as exc:
                self._log_stage(
                    "reply_fetch_failed",
                    message=message,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    author_id=author_id,
                    reference_id=reference_id,
                    exception=exc,
                )

        if is_mention or is_reply_to_bot:
            trigger = "mentioned" if is_mention else "replied_to"
            logger.info(
                "Discord bot trigger=%s guild=%s channel=%s author=%s",
                trigger,
                guild_id,
                channel_id,
                author_id,
            )

            # 処理開始フラグとしてリアクションを付与
            try:
                await message.add_reaction('🍧')
            except Exception as exc:
                self._log_stage(
                    "reaction_failed",
                    message=message,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    author_id=author_id,
                    exception=exc,
                )

            # セッション取得または作成
            try:
                session = await self.session_handler.get_or_create_session(
                    guild_id=guild_id,
                    user_id=author_id
                )
            except Exception as exc:
                self._log_stage("session_resolve_failed", message=message, exception=exc)
                try:
                    await message.reply("セッションの準備に失敗しました。しばらくしてから再試行してください。")
                except Exception as send_exc:
                    self._log_stage("reply_send_failed", message=message, exception=send_exc)
                return
            self._log_stage(
                "session_resolved",
                message=message,
                guild_id=guild_id,
                channel_id=channel_id,
                author_id=author_id,
                session_id=getattr(session, "conversation_id", None) or getattr(session, "id", None),
            )

            # メンションを除去してメッセージを処理
            content = content_raw
            if bot_id is not None:
                content = re.sub(rf"<@!?{re.escape(str(bot_id))}>", "", content)
            content = content.strip()

            # 画像添付があるか確認
            image_urls = []
            if message.attachments:
                for attachment in message.attachments:
                    attachment_url = getattr(attachment, "url", None)
                    content_type = getattr(attachment, "content_type", None)
                    # Discord CDN URLs are signed; query parameters often hide
                    # the filename extension from ``mimetypes.guess_type``.
                    # Infer from the path only so ``content_type=None`` PNG/JPEG
                    # attachments still enter the multimodal pipeline without
                    # ever logging or parsing the signature query.
                    attachment_path = urlsplit(str(attachment_url or "")).path
                    inferred_type = content_type or mimetypes.guess_type(attachment_path)[0]
                    if attachment_url and inferred_type and inferred_type.startswith('image/'):
                        image_urls.append(attachment_url)

            # テキストまたは画像がある場合は処理
            if content or image_urls:
                logger.info(
                    "Processing Discord message guild=%s channel=%s author=%s images=%s",
                    guild_id,
                    channel_id,
                    author_id,
                    len(image_urls),
                )
                await self._process_text_message(message, content, session, image_urls)
            else:
                # Mention-only messages should be explicit rather than silently
                # disappearing.  This is deliberately outside the generation
                # queue because it does not represent a user turn.
                try:
                    sent = await message.reply("メッセージ本文または画像を添付して話しかけてください。")
                    self._remember_bot_text_reply(sent, channel_id)
                    self._log_stage("reply_sent", message=message, guild_id=guild_id, channel_id=channel_id, author_id=author_id)
                except Exception as exc:
                    self._log_stage("reply_send_failed", message=message, guild_id=guild_id, channel_id=channel_id, author_id=author_id, exception=exc)

        # コマンド処理は親クラスに委譲
        await super().on_message(message)
    
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ) -> None:
        """ボイスステート更新時のイベント"""
        # Bot自身の変更は無視
        if member == self.user:
            return
        
        # ボイスチャンネルでの処理
        await self.voice_handler.handle_voice_state_update(member, before, after)
    
    def _log_stage(self, stage: str, *, message: Any = None, guild_id: Any = None,
                   channel_id: Any = None, author_id: Any = None, session_id: Any = None,
                   exception: Any = None, **extra: Any) -> None:
        """Emit structured Discord lifecycle logs without message contents."""
        if message is not None:
            guild_id = guild_id if guild_id is not None else getattr(getattr(message, "guild", None), "id", None)
            channel_id = channel_id if channel_id is not None else getattr(getattr(message, "channel", None), "id", None)
            author_id = author_id if author_id is not None else getattr(getattr(message, "author", None), "id", None)
        payload = {
            "stage": stage,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "author_id": author_id,
            "session_id": session_id,
        }
        payload.update({key: value for key, value in extra.items() if value is not None})
        if exception is not None:
            payload["exception"] = repr(exception)
        logger.info("discord_stage %s", payload)

    async def _send_response(
        self,
        message: Optional[discord.Message],
        response: str,
        session: Any,
        *,
        message_id: Any = None,
        channel_id: Any = None,
        guild_id: Any = None,
        author_id: Any = None,
    ) -> None:
        """Send one logical response without retaining source Discord objects.

        Queue callbacks normally receive ``message=None`` and resolve the
        source/channel from primitive IDs at send time.  A weak reference is
        used only when a lightweight adapter supports it; real discord.py
        messages that cannot be weak-referenced still use this ID route.
        """
        source_message = message
        channel = getattr(source_message, "channel", None) if source_message is not None else None
        if source_message is not None:
            message_id = message_id if message_id is not None else getattr(source_message, "id", None)
            channel_id = channel_id if channel_id is not None else getattr(channel, "id", None)
            guild_id = guild_id if guild_id is not None else getattr(getattr(source_message, "guild", None), "id", None)
            author_id = author_id if author_id is not None else getattr(getattr(source_message, "author", None), "id", None)
        else:
            # Resolve the channel from the bot cache first; fetch only for a
            # cache miss.  Neither object is retained by the queue callback.
            try:
                get_channel = getattr(self, "get_channel", None)
                if callable(get_channel) and channel_id is not None:
                    channel = get_channel(int(channel_id))
            except Exception as exc:
                self._log_stage(
                    "reply_fetch_failed",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    author_id=author_id,
                    reference_id=message_id,
                    exception=exc,
                )
            if channel is None:
                try:
                    fetch_channel = getattr(self, "fetch_channel", None)
                    if callable(fetch_channel) and channel_id is not None:
                        channel = await fetch_channel(int(channel_id))
                except Exception as exc:
                    self._log_stage(
                        "reply_fetch_failed",
                        guild_id=guild_id,
                        channel_id=channel_id,
                        author_id=author_id,
                        reference_id=message_id,
                        exception=exc,
                    )
            if channel is not None and message_id is not None:
                fetch_message = getattr(channel, "fetch_message", None)
                if callable(fetch_message):
                    try:
                        source_message = await fetch_message(int(message_id))
                    except Exception as exc:
                        # Deleted source/permission failures still permit a
                        # channel.send fallback below.
                        self._log_stage(
                            "reply_fetch_failed",
                            guild_id=guild_id,
                            channel_id=channel_id,
                            author_id=author_id,
                            reference_id=message_id,
                            exception=exc,
                        )
                    if source_message is not None:
                        channel = getattr(source_message, "channel", None) or channel

        if channel is None:
            self._log_stage(
                "reply_send_failed",
                guild_id=guild_id,
                channel_id=channel_id,
                author_id=author_id,
                session_id=getattr(session, "conversation_id", None),
                exception=RuntimeError("Discord reply channel unavailable"),
            )
            return

        async def send_reply_or_channel(content: str, **kwargs: Any) -> Any:
            """Reply to the source when possible, then fall back to send."""
            nonlocal source_message
            if source_message is not None and callable(getattr(source_message, "reply", None)):
                try:
                    return await source_message.reply(content, **kwargs)
                except Exception as exc:
                    # A source can be deleted or become inaccessible between
                    # fetch and send.  Keep the response routable through the
                    # channel while preserving a failure stage for the first
                    # attempted route.
                    self._log_stage(
                        "reply_send_failed",
                        guild_id=guild_id,
                        channel_id=channel_id,
                        author_id=author_id,
                        session_id=getattr(session, "conversation_id", None),
                        exception=exc,
                    )
                    source_message = None
            return await channel.send(content, **kwargs)

        generated_image_path: Optional[str] = None
        response = str(response or "")
        image_match = re.search(r"\[GENERATED_IMAGE:(.*?)\]", response)
        if image_match:
            generated_image_path = image_match.group(1).strip()
            response = response.replace(image_match.group(0), "").strip()
        try:
            if generated_image_path and os.path.exists(generated_image_path):
                sent = await send_reply_or_channel(
                    response or "画像を生成しました。",
                    file=discord.File(generated_image_path),
                )
                self._remember_bot_text_reply(sent, channel_id)
            elif generated_image_path:
                response += "\n(生成された画像ファイルが見つかりませんでした)"

            if not generated_image_path or not os.path.exists(generated_image_path):
                chunks = [response[i:i + 2000] for i in range(0, len(response), 2000)] or [""]
                sent = await send_reply_or_channel(chunks[0])
                self._remember_bot_text_reply(sent, channel_id)
                for chunk in chunks[1:]:
                    sent = await channel.send(chunk)
                    self._remember_bot_text_reply(sent, channel_id)
            self._log_stage(
                "reply_sent",
                guild_id=guild_id,
                channel_id=channel_id,
                author_id=author_id,
                session_id=getattr(session, "conversation_id", None),
            )
        except Exception as exc:
            self._log_stage(
                "reply_send_failed",
                guild_id=guild_id,
                channel_id=channel_id,
                author_id=author_id,
                session_id=getattr(session, "conversation_id", None),
                exception=exc,
            )
            return

        if (
            response
            and (guild_id is not None)
            and runtime_feature_manager.feature_enabled("discord_vc_output")
            and self.voice_handler.is_connected(guild_id)
        ):
            task = asyncio.create_task(
                self._play_response_audio(
                    guild_id,
                    response,
                    getattr(session, "character", None) or self.default_character,
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _process_text_message(self, message: discord.Message, content: str, session: Any, image_urls: Optional[list] = None) -> None:
        """テキストメッセージの処理"""
        try:
            guild_id = getattr(getattr(message, "guild", None), "id", None)
            channel_id = getattr(getattr(message, "channel", None), "id", None)
            author_id = getattr(getattr(message, "author", None), "id", None)
            if session.assistant is None:
                from .modes.discord_mode import DiscordMode

                session.assistant = DiscordMode(
                    config=self.config,
                    character=session.character or self.default_character,
                )
                self._log_stage("assistant_ready", message=message, session_id=getattr(session, "id", None))

            assistant = session.assistant
            memory_actor_id = (
                f"discord:{guild_id if guild_id is not None else 'dm'}:{author_id}"
            )

            # Resolve the durable row before handing the ID to the LLM.  The
            # runtime UUID remains available as an independent turn identity.
            resolver = getattr(self.session_handler, "ensure_conversation_session", None)
            if callable(resolver):
                try:
                    await resolver(session, session.assistant)
                except Exception as exc:
                    self._log_stage("session_resolve_failed", message=message, exception=exc)
            # Snapshot both identities before any await that can race with
            # /clear.  Queue admission compares this captured pair against
            # the reset epoch/current identity, so a turn that arrived before
            # rotation cannot be silently persisted in the new row.
            durable_session_id = getattr(session, "conversation_id", None)
            runtime_session_id = (
                getattr(session, "runtime_id", None)
                or getattr(session, "id", None)
            )
            session_epoch = getattr(assistant, "_session_reset_epochs", {}).get(
                (int(guild_id), int(author_id)),
                0,
            ) if guild_id is not None and author_id is not None else 0
            self._log_stage(
                "session_resolved",
                message=message,
                session_id=durable_session_id,
                runtime_session_id=runtime_session_id,
            )

            if not getattr(session, "memory_prefilled", False):
                # Native AgentLLM/Gemini clients load the durable
                # ConversationSession inside their provider request.  Legacy
                # user-scoped prefill would duplicate that history (and could
                # leak rows from a different durable session), so let the
                # assistant's explicit capability helper decide the path.
                should_prefill = True
                prefill_policy = getattr(
                    session.assistant,
                    "should_prefill_context_from_memory",
                    None,
                )
                if callable(prefill_policy):
                    try:
                        should_prefill = bool(prefill_policy())
                    except Exception as exc:
                        self._log_stage(
                            "memory_prefill_policy_failed",
                            message=message,
                            exception=exc,
                        )

                # Set before awaiting so two simultaneous messages cannot
                # prefill the same history twice.  A native provider is marked
                # complete here even though no legacy fetch is performed.
                session.memory_prefilled = True
                if should_prefill:
                    try:
                        prefill_kwargs = {
                            "user_id": author_id,
                            "guild_id": guild_id,
                            "session_id": durable_session_id,
                            "runtime_session_id": runtime_session_id,
                        }
                        try:
                            await session.assistant.prefill_context_from_memory(
                                **prefill_kwargs,
                            )
                        except TypeError as exc:
                            # Legacy assistants may only accept user/guild.  Drop
                            # the identity extensions only when the signature
                            # explicitly rejects those keyword names.
                            message_text = str(exc)
                            if not any(
                                key in message_text
                                for key in ("session_id", "runtime_session_id")
                            ):
                                raise
                            await session.assistant.prefill_context_from_memory(
                                user_id=author_id,
                                guild_id=guild_id,
                            )
                    except Exception as exc:
                        self._log_stage("memory_prefill_failed", message=message, exception=exc)
                    finally:
                        self._log_stage("memory_prefill_finished", message=message, session_id=getattr(session, "conversation_id", None))
                else:
                    self._log_stage(
                        "memory_prefill_skipped",
                        message=message,
                        session_id=durable_session_id,
                    )

            # Queue items must not retain a Discord Message/Channel object.
            # Some lightweight adapters support weak references, which keeps
            # their existing test behavior; real discord.py messages that do
            # not support weakrefs use the primitive-ID resolver in
            # ``_send_response`` instead.
            try:
                message_ref = weakref.ref(message)
            except TypeError:
                message_ref = None
            source_message_id = getattr(message, "id", None)
            source_channel_id = channel_id
            source_guild_id = guild_id
            source_author_id = author_id

            def publish_session_identity(target_assistant: Any) -> None:
                set_identity = getattr(target_assistant, "set_session_identity", None)
                if not callable(set_identity):
                    return
                identity_kwargs = {
                    "user_id": author_id,
                    "guild_id": guild_id,
                    "session_id": durable_session_id,
                    "runtime_session_id": runtime_session_id,
                    "force": False,
                    "epoch": session_epoch,
                }
                for _ in range(2):
                    try:
                        set_identity(**identity_kwargs)
                        return
                    except TypeError as exc:
                        optional_key = next(
                            (
                                key
                                for key in ("epoch", "force")
                                if key in str(exc)
                            ),
                            None,
                        )
                        if optional_key is None:
                            raise
                        identity_kwargs.pop(optional_key, None)
                set_identity(**identity_kwargs)

            def typing_factory() -> Any:
                """Resolve a typing context lazily without retaining Channel."""
                try:
                    get_channel = getattr(self, "get_channel", None)
                    channel_obj = (
                        get_channel(int(source_channel_id))
                        if callable(get_channel) and source_channel_id is not None
                        else None
                    )
                    typing_method = getattr(channel_obj, "typing", None)
                    return typing_method() if callable(typing_method) else None
                except Exception:
                    logger.debug("Discord typing context resolution failed", exc_info=True)
                    return None

            async def reply_callback(response: str) -> None:
                target = message_ref() if message_ref is not None else None
                await self._send_response(
                    target,
                    response,
                    session,
                    message_id=source_message_id,
                    channel_id=source_channel_id,
                    guild_id=source_guild_id,
                    author_id=source_author_id,
                )

            if hasattr(assistant, "enqueue_turn"):
                # Publish the identity immediately before admission.  This
                # lets /clear's reset barrier reject stale payloads while
                # allowing messages carrying the newly-rotated row/runtime to
                # pass as soon as the barrier opens.
                publish_session_identity(assistant)
                await assistant.enqueue_turn(
                    content=content,
                    image_urls=image_urls or [],
                    user_id=author_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=source_message_id,
                    actor_id=memory_actor_id,
                    session_id=durable_session_id,
                    runtime_session_id=runtime_session_id,
                    reply=reply_callback,
                    typing=typing_factory,
                )
                return

            # Compatibility for test doubles/legacy assistants without the
            # queue API.  Keep one response path and durable ID semantics.
            kwargs = {
                "user_id": author_id,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "message_id": source_message_id,
                "actor_id": memory_actor_id,
                "session_id": durable_session_id,
                "runtime_session_id": runtime_session_id,
            }
            publish_session_identity(assistant)
            optional_kwargs = ["runtime_session_id", "channel_id", "message_id", "actor_id"]
            while True:
                try:
                    if image_urls:
                        response = await assistant.process_text_with_images(content, image_urls, **kwargs)
                    else:
                        response = await assistant.process_text(content, **kwargs)
                    break
                except TypeError as exc:
                    # Legacy test doubles/integrations may not know the newer
                    # identity/channel keywords.  Drop only a keyword named
                    # in the signature error; internal TypeErrors propagate.
                    message_text = str(exc)
                    removable = next(
                        (key for key in optional_kwargs if key in message_text and key in kwargs),
                        None,
                    )
                    if removable is None:
                        raise
                    kwargs.pop(removable, None)
            await reply_callback(response)
        except Exception as exc:
            self._log_stage("generation_failed", message=message, session_id=getattr(session, "conversation_id", None), exception=exc)
            try:
                await message.reply("申し訳ありません。エラーが発生しました。")
            except Exception as send_exc:
                self._log_stage("reply_send_failed", message=message, exception=send_exc)

    async def _play_response_audio(self, guild_id: int, response: str, character: str) -> None:
        """Generate and play VC audio without extending Discord typing status."""
        try:
            logger.info("Generating VC TTS for response guild=%s chars=%s", guild_id, len(response or ""))
            audio_data = await self.voice_handler._generate_tts(response, character)
            if not audio_data:
                logger.warning("Failed to generate TTS audio")
                return

            logger.info("TTS audio generated successfully, size: %d bytes", len(audio_data))
            await self.voice_handler.play_audio(guild_id, audio_data)
            logger.info("TTS playback completed")
        except asyncio.CancelledError:
            logger.info("VC TTS playback task cancelled")
            raise
        except Exception as e:
            logger.error("Error in VC TTS playback task: %s", e, exc_info=True)


async def run_bot(config: Config) -> None:
    """Botを実行"""
    # 環境変数を強制的に再読み込み
    from dotenv import load_dotenv
    load_dotenv(override=True)

    log_path = setup_discord_logging()
    logger.info("Discord bot log file: %s", log_path)
    
    # トークン取得（Discord_TOKENを優先）
    token = os.getenv('Discord_TOKEN') or os.getenv('DISCORD_BOT_TOKEN')
    logger.info("Discord token loaded: %s", bool(token))
    
    # デバッグモードのチェック
    is_debug_mode = os.getenv('DEBUG_MODE', 'false').lower() == 'true' or os.getenv('CLAUDE_CODE_ENVIRONMENT') == 'true'
    
    if not token:
        _safe_print("\n❌ Discord Bot トークンが設定されていません")
        _safe_print("\n設定方法:")
        _safe_print("1. .env.sample を .env にコピーしてください")
        _safe_print("2. DISCORD_BOT_TOKEN に Discord Bot のトークンを設定してください")
        _safe_print("3. Discord Developer Portal (https://discord.com/developers/applications) で")
        _safe_print("   Bot を作成してトークンを取得できます")
        _safe_print("\n詳細は README.md を参照してください")
        
        # デバッグモードの場合はモックを使用
        if is_debug_mode:
            _safe_print("\n🔧 デバッグモード: モックを使用します")
            from .discord_bot_mock import run_mock_bot
            await run_mock_bot(config)
            return
        else:
            raise ValueError("Discord bot token not found in environment variables")
    
    # Bot作成
    _safe_print(f"[DEBUG] Botを作成中...")
    bot = AoiTalkBot(config)
    _safe_print(f"[DEBUG] Bot作成完了")
    
    try:
        _safe_print(f"[DEBUG] Bot.start()を呼び出し中...")
        await bot.start(token)
    except discord.LoginFailure as e:
        _safe_print("\n❌ Discord Bot のログインに失敗しました")
        _safe_print("\n考えられる原因:")
        _safe_print("1. トークンが無効または期限切れです")
        _safe_print("2. インターネット接続に問題があります")
        _safe_print("3. Discord APIがダウンしている可能性があります")
        _safe_print(f"\nエラー詳細: {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        _safe_print(f"\n❌ Discord Bot の起動に失敗しました: {e}")
        raise
    finally:
        # ``AoiTalkBot.close`` owns worker/session/voice cleanup and is
        # idempotent for direct callers as well as this finally block.
        await bot.close()


def main() -> None:
    """スタンドアロンでBotを起動"""
    # ログ設定
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 設定読み込み
    config = Config()
    
    # Bot実行
    asyncio.run(run_bot(config))


if __name__ == "__main__":
    main()
