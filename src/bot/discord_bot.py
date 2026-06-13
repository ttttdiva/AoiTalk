"""
Discord bot implementation for AoiTalk
"""

import asyncio
import logging
import os
import sys
from typing import Dict, Optional, Any

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
        
        # グローバル設定
        self.default_character = config.get('default_character', 'ずんだもん')
        self.default_mode = self.session_handler.default_mode  # 'text' or 'voice'
        
    async def setup_hook(self) -> None:
        """Bot起動時の初期設定"""
        # コマンドの登録
        await self.command_handler.setup_commands()

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

        # Bot自身のメッセージは無視
        if message.author == self.user:
            return

        # DMは現在サポートしない
        if isinstance(message.channel, discord.DMChannel):
            await message.channel.send("申し訳ありませんが、DMはサポートしていません。")
            return

        # メンションまたはBotへの返信を判定
        is_mention = self.user in message.mentions
        is_reply_to_bot = False
        if message.reference and message.reference.message_id:
            try:
                if message.reference.resolved and isinstance(message.reference.resolved, discord.Message):
                    is_reply_to_bot = message.reference.resolved.author == self.user
                else:
                    referenced_msg = await message.channel.fetch_message(message.reference.message_id)
                    is_reply_to_bot = referenced_msg.author == self.user
            except Exception:
                pass

        if is_mention or is_reply_to_bot:
            trigger = "mentioned" if is_mention else "replied to"
            logger.info(f"Bot {trigger} by {message.author.name} in {message.guild.name}")

            # 処理開始フラグとしてリアクションを付与
            try:
                await message.add_reaction('🍧')
            except Exception as e:
                logger.warning(f"Failed to add reaction: {e}")

            # セッション取得または作成
            session = await self.session_handler.get_or_create_session(
                guild_id=message.guild.id,
                user_id=message.author.id
            )
            logger.info(f"Session mode: {session.mode}")

            # メンションを除去してメッセージを処理
            content = message.content.replace(f'<@{self.user.id}>', '').strip()

            # 画像添付があるか確認
            image_urls = []
            if message.attachments:
                for attachment in message.attachments:
                    if attachment.content_type and attachment.content_type.startswith('image/'):
                        image_urls.append(attachment.url)

            # テキストまたは画像がある場合は処理
            if content or image_urls:
                logger.info(f"Processing message with content: '{content}', images: {len(image_urls)}")
                await self._process_text_message(message, content, session, image_urls)
            else:
                logger.warning("No content or images to process")

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
    
    async def _process_text_message(self, message: discord.Message, content: str, session: Any, image_urls: Optional[list] = None) -> None:
        """テキストメッセージの処理"""
        try:
            response = ""
            generated_image_path: Optional[str] = None

            # Keep Discord's typing indicator only around AI response generation.
            async with message.channel.typing():
                # DiscordModeインスタンスを取得または作成
                if session.assistant is None:
                    from .modes.discord_mode import DiscordMode

                    session.assistant = DiscordMode(
                        config=self.config,
                        character=session.character or self.default_character
                    )

                # 既存メモリから会話履歴を復元
                if not getattr(session, 'memory_prefilled', False):
                    try:
                        guild_id = message.guild.id if message.guild else None
                        await session.assistant.prefill_context_from_memory(
                            user_id=message.author.id,
                            guild_id=guild_id
                        )
                    finally:
                        session.memory_prefilled = True

                # メッセージを処理（画像付きの場合は画像も送信）
                if image_urls:
                    response = await session.assistant.process_text_with_images(
                        content,
                        image_urls,
                        user_id=message.author.id,
                        guild_id=message.guild.id if message.guild else None
                    )
                else:
                    response = await session.assistant.process_text(
                        content,
                        user_id=message.author.id,
                        guild_id=message.guild.id if message.guild else None
                    )

                # Check for generated image tag
                import re
                image_match = re.search(r'\[GENERATED_IMAGE:(.*?)\]', response)
                if image_match:
                    generated_image_path = image_match.group(1).strip()
                    # Remove tag from response
                    response = response.replace(image_match.group(0), "").strip()

            if generated_image_path:
                if os.path.exists(generated_image_path):
                    try:
                        file = discord.File(generated_image_path)
                        await message.reply(response or "画像を生成しました。", file=file)
                        return
                    except Exception as e:
                        logger.error(f"Failed to send generated image: {e}")
                        response += f"\n(画像の送信に失敗しました: {e})"
                else:
                    response += "\n(生成された画像ファイルが見つかりませんでした)"

            # 応答を送信（2000文字制限を考慮）
            if len(response) <= 2000:
                await message.reply(response)
            else:
                # 長い応答は分割して送信
                chunks = [response[i:i+2000] for i in range(0, len(response), 2000)]
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await message.reply(chunk)
                    else:
                        await message.channel.send(chunk)

            # VCへの読み上げは返信送信後に別タスクで行い、typingを延長しない。
            if (
                response
                and message.guild
                and runtime_feature_manager.feature_enabled("discord_vc_output")
                and self.voice_handler.is_connected(message.guild.id)
            ):
                task = asyncio.create_task(
                    self._play_response_audio(
                        message.guild.id,
                        response,
                        session.character or self.default_character,
                    )
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

        except Exception as e:
            logger.error(f"Error processing text message: {e}", exc_info=True)
            await message.reply("申し訳ありません。エラーが発生しました。")

    async def _play_response_audio(self, guild_id: int, response: str, character: str) -> None:
        """Generate and play VC audio without extending Discord typing status."""
        try:
            logger.info("Generating VC TTS for response: %r", response[:50])
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
        for task in list(getattr(bot, '_background_tasks', set())):
            task.cancel()
        if getattr(bot, '_background_tasks', None):
            await asyncio.gather(*bot._background_tasks, return_exceptions=True)

        # Cleanup voice handler
        if hasattr(bot, 'voice_handler'):
            await bot.voice_handler.cleanup()
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
