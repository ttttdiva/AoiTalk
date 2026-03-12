"""
Discord bot implementation for AoiTalk
"""

import asyncio
import logging
import os
from typing import Dict, Optional, Any

import discord
from discord import app_commands
from discord.ext import commands

from ..assistant.base import BaseAssistant
from ..config import Config
from .handlers.command_handler import CommandHandler
from .handlers.session_handler import SessionHandler
from .handlers.voice_handler import VoiceHandler
from .modes.discord_mode import DiscordMode

logger = logging.getLogger(__name__)


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
        self.session_handler = SessionHandler()
        self.voice_handler = VoiceHandler(config)
        self.voice_handler._bot_instance = self  # Set bot instance reference
        self.command_handler = CommandHandler(self)
        
        # グローバル設定
        self.default_character = config.get('default_character', 'ずんだもん')
        self.default_mode = 'text'  # 'text' or 'voice'
        
    async def setup_hook(self) -> None:
        """Bot起動時の初期設定"""
        # コマンドの登録
        await self.command_handler.setup_commands()
        
        # 必要に応じてギルドコマンドを同期
        should_sync_by_env = os.getenv('DISCORD_SYNC_COMMANDS', 'false').lower() == 'true'
        should_sync_by_config = bool(self.config.get('discord.sync_commands', False))
        if should_sync_by_env or should_sync_by_config:
            try:
                synced = await self.tree.sync()
                logger.info(f"Synced {len(synced)} command(s) (env={should_sync_by_env}, config={should_sync_by_config})")
            except Exception as e:
                logger.error(f"Failed to sync commands: {e}")
    
    async def on_ready(self) -> None:
        """Bot接続完了時のイベント"""
        logger.info(f'Logged in as {self.user} (ID: {self.user.id})')
        logger.info(f'Connected to {len(self.guilds)} guild(s)')
        print(f'✅ Discord Bot ログイン成功: {self.user} (ID: {self.user.id})')
        print(f'✅ 接続サーバー数: {len(self.guilds)}')
        
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
            # typing indicatorを表示
            async with message.channel.typing():
                # DiscordModeインスタンスを取得または作成
                if session.assistant is None:
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
                    image_path = image_match.group(1).strip()
                    # Remove tag from response
                    response = response.replace(image_match.group(0), "").strip()
                    
                    if os.path.exists(image_path):
                        try:
                            # Send image
                            file = discord.File(image_path)
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
                
                # VCに参加している場合は音声読み上げ
                if self.voice_handler.is_connected(message.guild.id):
                    logger.info(f"Bot is in VC, generating TTS for response: '{response[:50]}...'")
                    audio_data = await self.voice_handler._generate_tts(response, session.character or self.default_character)
                    if audio_data:
                        logger.info(f"TTS audio generated successfully, size: {len(audio_data)} bytes")
                        await self.voice_handler.play_audio(message.guild.id, audio_data)
                        logger.info(f"TTS playback initiated")
                    else:
                        logger.warning(f"Failed to generate TTS audio")
                            
        except Exception as e:
            logger.error(f"Error processing text message: {e}", exc_info=True)
            await message.reply("申し訳ありません。エラーが発生しました。")


async def run_bot(config: Config) -> None:
    """Botを実行"""
    # 環境変数を強制的に再読み込み
    from dotenv import load_dotenv
    load_dotenv(override=True)
    
    # トークン取得（Discord_TOKENを優先）
    token = os.getenv('Discord_TOKEN') or os.getenv('DISCORD_BOT_TOKEN')
    logger.info(f"Token loaded: {token[:20]}...{token[-10:] if token else 'None'}")
    
    # デバッグモードのチェック
    is_debug_mode = os.getenv('DEBUG_MODE', 'false').lower() == 'true' or os.getenv('CLAUDE_CODE_ENVIRONMENT') == 'true'
    
    if not token:
        print("\n❌ Discord Bot トークンが設定されていません")
        print("\n設定方法:")
        print("1. .env.sample を .env にコピーしてください")
        print("2. DISCORD_BOT_TOKEN に Discord Bot のトークンを設定してください")
        print("3. Discord Developer Portal (https://discord.com/developers/applications) で")
        print("   Bot を作成してトークンを取得できます")
        print("\n詳細は README.md を参照してください")
        
        # デバッグモードの場合はモックを使用
        if is_debug_mode:
            print("\n🔧 デバッグモード: モックを使用します")
            from .discord_bot_mock import run_mock_bot
            await run_mock_bot(config)
            return
        else:
            raise ValueError("Discord bot token not found in environment variables")
    
    # Bot作成
    print(f"[DEBUG] Botを作成中...")
    bot = AoiTalkBot(config)
    print(f"[DEBUG] Bot作成完了")
    
    try:
        print(f"[DEBUG] Bot.start()を呼び出し中...")
        await bot.start(token)
    except discord.LoginFailure as e:
        print("\n❌ Discord Bot のログインに失敗しました")
        print("\n考えられる原因:")
        print("1. トークンが無効または期限切れです")
        print("2. インターネット接続に問題があります")
        print("3. Discord APIがダウンしている可能性があります")
        print(f"\nエラー詳細: {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        print(f"\n❌ Discord Bot の起動に失敗しました: {e}")
        raise
    finally:
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
