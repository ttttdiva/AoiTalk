"""Discord slash command handler"""

import asyncio
import io
import logging
import textwrap
from typing import Optional, List

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

from ...mode_switch import mode_switch_manager, ModeSwitchError
from ..utils.nanobanana_service import NanobananaProService


class CommandHandler:
    """Handle Discord slash commands"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._setup_commands()
        self.nanobanana_service = NanobananaProService()
    
    def _setup_commands(self):
        """Setup slash commands"""
        
        @self.bot.tree.command(name="join", description="ボイスチャンネルに参加します")
        async def join(interaction: discord.Interaction):
            """Join voice channel command"""
            await self._handle_join(interaction)
        
        @self.bot.tree.command(name="leave", description="ボイスチャンネルから退出します")
        async def leave(interaction: discord.Interaction):
            """Leave voice channel command"""
            await self._handle_leave(interaction)
        
        @self.bot.tree.command(name="character", description="使用するキャラクターを変更します")
        @app_commands.describe(name="キャラクター名")
        async def character(interaction: discord.Interaction, name: str):
            """Change character command"""
            await self._handle_character(interaction, name)
        
        @self.bot.tree.command(name="mode", description="動作モードを変更します")
        @app_commands.describe(mode="動作モード (text/voice)")
        @app_commands.choices(mode=[
            app_commands.Choice(name="テキスト", value="text"),
            app_commands.Choice(name="音声", value="voice")
        ])
        async def mode(interaction: discord.Interaction, mode: str):
            """Change mode command"""
            await self._handle_mode(interaction, mode)

        @self.bot.tree.command(name="systemmode", description="AoiTalk全体のモードを切り替えます")
        @app_commands.describe(mode="terminal / voice_chat / discord")
        @app_commands.choices(mode=[
            app_commands.Choice(name="Terminal", value="terminal"),
            app_commands.Choice(name="Voice Chat", value="voice_chat"),
            app_commands.Choice(name="Discord", value="discord")
        ])
        async def system_mode(interaction: discord.Interaction, mode: str):
            await self._handle_system_mode(interaction, mode)
        
        @self.bot.tree.command(name="status", description="現在のステータスを表示します")
        async def status(interaction: discord.Interaction):
            """Show status command"""
            await self._handle_status(interaction)

        @self.bot.tree.command(name="nanobanana", description="nanobanana proを検索し、イメージを生成します")
        async def nanobanana(interaction: discord.Interaction):
            """Fetch Nanobanana Pro info and hero image"""
            await self._handle_nanobanana(interaction)

        @self.bot.tree.command(name="settings", description="設定を表示・変更します")
        async def settings(interaction: discord.Interaction):
            """Settings command"""
            await self._handle_settings(interaction)
        
        @self.bot.tree.command(name="help", description="ヘルプを表示します")
        async def help(interaction: discord.Interaction):
            """Help command"""
            await self._handle_help(interaction)
        
        @self.bot.tree.command(name="clear", description="会話履歴をクリアします")
        async def clear(interaction: discord.Interaction):
            """Clear conversation history"""
            await self._handle_clear(interaction)
        
        @self.bot.tree.command(name="play", description="Spotifyで音楽を再生します")
        @app_commands.describe(query="曲名、アーティスト名、またはプレイリスト名")
        async def play(interaction: discord.Interaction, query: str):
            """Play music on Spotify"""
            await self._handle_spotify_play(interaction, query)
        
        @self.bot.tree.command(name="pause", description="Spotify再生を一時停止します")
        async def pause(interaction: discord.Interaction):
            """Pause Spotify playback"""
            await self._handle_spotify_pause(interaction)
        
        @self.bot.tree.command(name="skip", description="次の曲にスキップします")
        async def skip(interaction: discord.Interaction):
            """Skip to next track"""
            await self._handle_spotify_skip(interaction)
        
        @self.bot.tree.command(name="queue", description="曲をキューに追加します")
        @app_commands.describe(query="曲名またはアーティスト名")
        async def queue(interaction: discord.Interaction, query: str):
            """Add song to queue"""
            await self._handle_spotify_queue(interaction, query)
        
        @self.bot.tree.command(name="nowplaying", description="現在再生中の曲を表示します")
        async def nowplaying(interaction: discord.Interaction):
            """Show now playing track"""
            await self._handle_spotify_nowplaying(interaction)
    
    async def setup_commands(self):
        """Setup commands (called from bot setup_hook)"""
        # Commands are already set up in __init__
        logger.info("Commands have been set up")
    
    async def _handle_join(self, interaction: discord.Interaction):
        """Handle join command"""
        # ユーザーがボイスチャンネルに接続しているか確認
        if not interaction.user.voice:
            await interaction.response.send_message(
                "ボイスチャンネルに接続してから、このコマンドを使用してください。",
                ephemeral=True
            )
            return
        
        # すでに接続している場合
        if self.bot.voice_handler.is_connected(interaction.guild_id):
            voice_client = self.bot.voice_handler.get_voice_client(interaction.guild_id)
            if voice_client and voice_client.channel == interaction.user.voice.channel:
                await interaction.response.send_message(
                    "すでに同じボイスチャンネルに接続しています。",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "別のボイスチャンネルに接続中です。先に `/leave` を使用してください。",
                    ephemeral=True
                )
            return
        
        # 接続には時間がかかる場合があるため、deferする
        await interaction.response.defer()

        try:
            # ボイスチャンネルに接続
            channel = interaction.user.voice.channel
            voice_client = await self.bot.voice_handler.connect_voice_channel(channel)
            
            if voice_client:
                # セッションを作成
                session = await self.bot.session_handler.get_or_create_session(
                    guild_id=interaction.guild_id,
                    user_id=interaction.user.id
                )
                session.voice_channel_id = channel.id
                
                # ボイスチャンネルの全ユーザーのセッションを音声モードに設定
                for member in channel.members:
                    if not member.bot:
                        user_session = await self.bot.session_handler.get_or_create_session(
                            guild_id=interaction.guild_id,
                            user_id=member.id
                        )
                        user_session.mode = 'voice'
                        logger.info(f"Set voice mode for user {member.name} ({member.id})")
                
                await interaction.followup.send(
                    f"🎤 **{channel.name}** に接続しました！\n"
                    f"音声モードで会話を開始できます。\n"
                    f"💡 ヒント: マイクで話しかけると応答します。"
                )
                
                logger.info(f"Joined voice channel: {channel.name} in guild: {interaction.guild.name}")
            else:
                await interaction.followup.send(
                    "ボイスチャンネルへの接続に失敗しました。",
                    ephemeral=True
                )
            
        except Exception as e:
            logger.error(f"Failed to join voice channel: {e}")
            await interaction.followup.send(
                "ボイスチャンネルへの接続に失敗しました。",
                ephemeral=True
            )
    
    async def _handle_leave(self, interaction: discord.Interaction):
        """Handle leave command"""
        if not self.bot.voice_handler.is_connected(interaction.guild_id):
            await interaction.response.send_message(
                "ボイスチャンネルに接続していません。",
                ephemeral=True
            )
            return
        
        try:
            voice_client = self.bot.voice_handler.get_voice_client(interaction.guild_id)
            channel_name = voice_client.channel.name if voice_client else "Unknown"
            
            await self.bot.voice_handler.disconnect_voice_channel(interaction.guild_id)
            
            await interaction.response.send_message(
                f"👋 **{channel_name}** から退出しました。"
            )
            
            logger.info(f"Left voice channel: {channel_name} in guild: {interaction.guild.name}")
            
        except Exception as e:
            logger.error(f"Failed to leave voice channel: {e}")
            await interaction.response.send_message(
                "ボイスチャンネルからの退出に失敗しました。",
                ephemeral=True
            )
    
    async def _handle_character(self, interaction: discord.Interaction, name: str):
        """Handle character command"""
        try:
            # 利用可能なキャラクターを確認
            available_characters = self.bot.config.get_available_characters()
            
            if name not in available_characters:
                characters_list = "\n".join([f"• {char}" for char in available_characters])
                await interaction.response.send_message(
                    f"❌ キャラクター **{name}** は存在しません。\n\n"
                    f"利用可能なキャラクター:\n{characters_list}",
                    ephemeral=True
                )
                return
            
            # セッションのキャラクターを変更
            session = await self.bot.session_handler.get_or_create_session(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id
            )
            session.character = name
            
            # DiscordModeのキャラクターも変更
            if session.assistant:
                session.assistant.set_character(name, user_id=interaction.user.id)
            
            await interaction.response.send_message(
                f"✅ キャラクターを **{name}** に変更しました！"
            )
            
        except Exception as e:
            logger.error(f"Failed to change character: {e}")
            await interaction.response.send_message(
                "キャラクターの変更に失敗しました。",
                ephemeral=True
            )
    
    async def _handle_mode(self, interaction: discord.Interaction, mode: str):
        """Handle mode command"""
        try:
            session = await self.bot.session_handler.get_or_create_session(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id
            )
            
            old_mode = session.mode
            session.mode = mode
            
            mode_name = "テキスト" if mode == "text" else "音声"
            await interaction.response.send_message(
                f"✅ モードを **{mode_name}** に変更しました！"
            )
            
            logger.info(f"Changed mode from {old_mode} to {mode} for user {interaction.user.name}")
            
        except Exception as e:
            logger.error(f"Failed to change mode: {e}")
            await interaction.response.send_message(
                "モードの変更に失敗しました。",
                ephemeral=True
            )

    async def _handle_system_mode(self, interaction: discord.Interaction, mode: str):
        """Handle global system mode switch"""
        user_id = interaction.user.id
        if not mode_switch_manager.is_discord_actor_allowed(user_id):
            await interaction.response.send_message(
                "このコマンドを実行する権限がありません。",
                ephemeral=True
            )
            return

        try:
            result = await mode_switch_manager.request_switch(
                mode,
                source="discord",
                actor_id=str(user_id)
            )
            message = result.get('message') or 'モード切り替えを開始しました'
            await interaction.response.send_message(
                f"🔁 {self._format_system_mode(mode)} モードに切り替えます。\n{message}\n"
                "⚠️ 数秒後にBotが再起動します。",
                ephemeral=True
            )
        except ModeSwitchError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    def _format_system_mode(self, mode: str) -> str:
        mapping = {
            'terminal': 'Terminal',
            'voice_chat': 'Voice Chat',
            'discord': 'Discord'
        }
        return mapping.get(mode, mode)
    
    async def _handle_status(self, interaction: discord.Interaction):
        """Handle status command"""
        try:
            session = await self.bot.session_handler.get_or_create_session(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id
            )
            
            # ボイスチャンネル接続状態
            voice_status = "未接続"
            if interaction.guild.voice_client:
                voice_status = f"接続中: {interaction.guild.voice_client.channel.name}"
            
            # モードとキャラクター
            mode_name = "テキスト" if session.mode == "text" else "音声"
            
            embed = discord.Embed(
                title="📊 現在のステータス",
                color=discord.Color.blue()
            )
            embed.add_field(name="ボイスチャンネル", value=voice_status, inline=False)
            embed.add_field(name="動作モード", value=mode_name, inline=True)
            embed.add_field(name="キャラクター", value=session.character or self.bot.default_character, inline=True)
            embed.add_field(name="セッションID", value=f"`{session.id[:8]}...`", inline=True)
            
            await interaction.response.send_message(embed=embed)
            
        except Exception as e:
            logger.error(f"Failed to show status: {e}")
            await interaction.response.send_message(
                "ステータスの取得に失敗しました。",
                ephemeral=True
            )

    async def _handle_nanobanana(self, interaction: discord.Interaction):
        """Handle Nanobanana Pro info + image generation"""
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=True)
        except Exception as exc:
            logger.warning("Failed to defer nanobanana command: %s", exc)

        try:
            summary = await asyncio.to_thread(self.nanobanana_service.fetch_summary)
            description = self.nanobanana_service.build_embed_description(summary)
            image_bytes, prompt = await asyncio.to_thread(
                self.nanobanana_service.generate_image,
                summary
            )

            embed = discord.Embed(
                title="Nanobanana Pro 最新サマリー",
                description=description,
                color=discord.Color.gold()
            )
            prompt_text = textwrap.shorten(prompt, width=1000, placeholder="...")
            embed.add_field(name="画像生成プロンプト", value=prompt_text, inline=False)
            status_text = "検索+画像生成完了" if image_bytes else "検索のみ (OPENAI_API_KEY未設定または失敗)"
            embed.add_field(name="処理状況", value=status_text, inline=True)
            embed.set_footer(text="情報ソース: web_searchツール (OpenAI Agents)")

            if image_bytes:
                buffer = io.BytesIO(image_bytes)
                buffer.seek(0)
                files = [discord.File(buffer, filename="nanobanana_pro.png")]
                embed.set_image(url="attachment://nanobanana_pro.png")
                await interaction.followup.send(embed=embed, files=files)
            else:
                await interaction.followup.send(embed=embed)

        except Exception as exc:  # pragma: no cover - Discord runtime path
            logger.error("Nanobanana command failed: %s", exc, exc_info=True)
            error_message = "nanobanana proの情報取得に失敗しました。後でもう一度お試しください。"
            if interaction.response.is_done():
                await interaction.followup.send(error_message, ephemeral=True)
            else:
                await interaction.response.send_message(error_message, ephemeral=True)

    async def _handle_settings(self, interaction: discord.Interaction):
        """Handle settings command"""
        # TODO: 設定画面の実装（ボタンやセレクトメニューを使用）
        await interaction.response.send_message(
            "⚙️ 設定機能は現在開発中です。\n"
            "個別のコマンド（`/character`, `/mode`）を使用してください。",
            ephemeral=True
        )
    
    async def _handle_help(self, interaction: discord.Interaction):
        """Handle help command"""
        embed = discord.Embed(
            title="🤖 AoiTalk Bot ヘルプ",
            description="AI音声アシスタントBotの使い方",
            color=discord.Color.green()
        )
        
        embed.add_field(
            name="基本的な使い方",
            value=(
                "1. ボイスチャンネルに参加後、`/join` でBotを呼びます\n"
                "2. テキストモード: Botをメンションして会話\n"
                "3. 音声モード: マイクで話しかけて会話\n"
                "4. `/leave` でBotを退出させます"
            ),
            inline=False
        )
        
        embed.add_field(
            name="コマンド一覧",
            value=(
                "**基本コマンド**\n"
                "`/join` - ボイスチャンネルに参加\n"
                "`/leave` - ボイスチャンネルから退出\n"
                "`/character <名前>` - キャラクター変更\n"
                "`/mode <text/voice>` - モード切替\n"
                "`/status` - 現在の状態を表示\n"
                "`/clear` - 会話履歴をクリア\n"
                "`/help` - このヘルプを表示\n\n"
                "**Spotify音楽コマンド**\n"
                "`/play <曲名>` - 音楽を再生\n"
                "`/pause` - 再生を一時停止\n"
                "`/skip` - 次の曲にスキップ\n"
                "`/queue <曲名>` - キューに追加\n"
                "`/nowplaying` - 現在の曲を表示"
            ),
            inline=False
        )
        
        embed.add_field(
            name="Tips",
            value=(
                "• テキストモードではBotをメンションして話しかけてください\n"
                "• 音声モードは現在開発中です\n"
                "• キャラクターごとに異なる性格で応答します"
            ),
            inline=False
        )
        
        await interaction.response.send_message(embed=embed)
    
    async def _handle_clear(self, interaction: discord.Interaction):
        """Handle clear command"""
        try:
            session = await self.bot.session_handler.get_or_create_session(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id
            )
            
            # DiscordModeのコンテキストをクリア
            if session.assistant:
                session.assistant.clear_context(user_id=interaction.user.id)
            
            await interaction.response.send_message(
                "🗑️ 会話履歴をクリアしました。\n"
                "新しい会話を始めることができます。"
            )
            
        except Exception as e:
            logger.error(f"Failed to clear history: {e}")
            await interaction.response.send_message(
                "会話履歴のクリアに失敗しました。",
                ephemeral=True
            )
    
    async def _handle_spotify_play(self, interaction: discord.Interaction, query: str):
        """Handle Spotify play command"""
        await interaction.response.defer()
        
        try:
            # SpotifyAgentを使用して再生
            from src.agents.spotify_agent import SpotifyAgent
            spotify_agent = SpotifyAgent()
            
            # 曲を再生
            result = spotify_agent.play_song_now(query)
            
            if "再生を開始しました" in result or "再生しています" in result:
                embed = discord.Embed(
                    title="🎵 再生開始",
                    description=result,
                    color=discord.Color.green()
                )
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(f"⚠️ {result}")
                
        except Exception as e:
            logger.error(f"Spotify play error: {e}")
            await interaction.followup.send(
                "❌ 再生に失敗しました。Spotify認証を確認してください。",
                ephemeral=True
            )
    
    async def _handle_spotify_pause(self, interaction: discord.Interaction):
        """Handle Spotify pause command"""
        try:
            from src.agents.spotify_agent import SpotifyAgent
            spotify_agent = SpotifyAgent()
            
            result = spotify_agent.pause_spotify()
            
            embed = discord.Embed(
                title="⏸️ 一時停止",
                description=result,
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed)
            
        except Exception as e:
            logger.error(f"Spotify pause error: {e}")
            await interaction.response.send_message(
                "❌ 一時停止に失敗しました。",
                ephemeral=True
            )
    
    async def _handle_spotify_skip(self, interaction: discord.Interaction):
        """Handle Spotify skip command"""
        try:
            from src.agents.spotify_agent import SpotifyAgent
            spotify_agent = SpotifyAgent()
            
            result = spotify_agent.skip_spotify_track()
            
            embed = discord.Embed(
                title="⏭️ スキップ",
                description=result,
                color=discord.Color.blue()
            )
            await interaction.response.send_message(embed=embed)
            
        except Exception as e:
            logger.error(f"Spotify skip error: {e}")
            await interaction.response.send_message(
                "❌ スキップに失敗しました。",
                ephemeral=True
            )
    
    async def _handle_spotify_queue(self, interaction: discord.Interaction, query: str):
        """Handle Spotify queue command"""
        await interaction.response.defer()
        
        try:
            from src.agents.spotify_agent import SpotifyAgent
            spotify_agent = SpotifyAgent()
            
            result = spotify_agent.queue_song(query)
            
            if "キューに追加しました" in result:
                embed = discord.Embed(
                    title="📋 キューに追加",
                    description=result,
                    color=discord.Color.purple()
                )
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(f"⚠️ {result}")
                
        except Exception as e:
            logger.error(f"Spotify queue error: {e}")
            await interaction.followup.send(
                "❌ キューへの追加に失敗しました。",
                ephemeral=True
            )
    
    async def _handle_spotify_nowplaying(self, interaction: discord.Interaction):
        """Handle Spotify now playing command"""
        try:
            from src.agents.spotify_agent import SpotifyAgent
            spotify_agent = SpotifyAgent()
            
            result = spotify_agent.get_spotify_status()
            
            # 再生中の情報を整形
            if "現在" in result and "再生中" in result:
                embed = discord.Embed(
                    title="🎧 現在再生中",
                    description=result,
                    color=discord.Color.green()
                )
            else:
                embed = discord.Embed(
                    title="🎧 再生状態",
                    description=result,
                    color=discord.Color.grey()
                )
            
            await interaction.response.send_message(embed=embed)
            
        except Exception as e:
            logger.error(f"Spotify now playing error: {e}")
            await interaction.response.send_message(
                "❌ 再生状態の取得に失敗しました。",
                ephemeral=True
            )
