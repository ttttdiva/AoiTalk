"""Discord slash command handler"""

import asyncio
import io
import logging
import textwrap
from typing import Any, Callable, Optional, List

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

from ...runtime_features import runtime_feature_manager
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

        @self.bot.tree.command(name="feature", description="AoiTalkの機能トグルを変更します")
        @app_commands.describe(feature="変更する機能", enabled="ONにするか")
        @app_commands.choices(feature=[
            app_commands.Choice(name="Discord Bot", value="discord_bot"),
            app_commands.Choice(name="Discordテキスト", value="discord_text"),
            app_commands.Choice(name="Discord VC入力", value="discord_vc_input"),
            app_commands.Choice(name="Discord VC出力", value="discord_vc_output"),
            app_commands.Choice(name="読み上げ", value="tts"),
            app_commands.Choice(name="ローカルマイク", value="local_mic"),
            app_commands.Choice(name="ローカルスピーカー", value="local_speaker"),
        ])
        async def feature(interaction: discord.Interaction, feature: str, enabled: bool):
            await self._handle_feature(interaction, feature, enabled)
        
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

        @self.bot.tree.command(name="spotify_auth", description="Spotify認証URLを表示します")
        async def spotify_auth(interaction: discord.Interaction):
            """Start Spotify authorization"""
            await self._handle_spotify_auth(interaction)

        @self.bot.tree.command(name="spotify_code", description="Spotify認証コードを登録します")
        @app_commands.describe(code="リダイレクトURLのcodeパラメータ")
        async def spotify_code(interaction: discord.Interaction, code: str):
            """Complete Spotify authorization"""
            await self._handle_spotify_code(interaction, code)

        @self.bot.tree.command(name="search", description="Spotifyで音楽を検索します")
        @app_commands.describe(query="検索語", search_type="検索対象", limit="表示件数")
        @app_commands.choices(search_type=[
            app_commands.Choice(name="曲", value="track"),
            app_commands.Choice(name="アルバム", value="album"),
            app_commands.Choice(name="アーティスト", value="artist"),
            app_commands.Choice(name="プレイリスト", value="playlist")
        ])
        async def search(
            interaction: discord.Interaction,
            query: str,
            search_type: str = "track",
            limit: int = 5
        ):
            """Search Spotify music"""
            await self._handle_spotify_search(interaction, query, search_type, limit)

        @self.bot.tree.command(name="previous", description="前の曲に戻ります")
        async def previous(interaction: discord.Interaction):
            """Return to previous Spotify track"""
            await self._handle_spotify_previous(interaction)

        @self.bot.tree.command(name="show_queue", description="Spotify内部キューを表示します")
        async def show_queue(interaction: discord.Interaction):
            """Show internal Spotify queue"""
            await self._handle_spotify_show_queue(interaction)

        @self.bot.tree.command(name="clear_queue", description="Spotify内部キューをクリアします")
        async def clear_queue(interaction: discord.Interaction):
            """Clear internal Spotify queue"""
            await self._handle_spotify_clear_queue(interaction)

        @self.bot.tree.command(name="remove_queue", description="Spotify内部キューから指定位置の曲を削除します")
        @app_commands.describe(position="削除する曲の位置 (1始まり)")
        async def remove_queue(interaction: discord.Interaction, position: int):
            """Remove a track from the internal Spotify queue"""
            await self._handle_spotify_remove_queue(interaction, position)

        @self.bot.tree.command(name="playlists", description="Spotifyプレイリスト一覧を表示します")
        @app_commands.describe(limit="表示件数")
        async def playlists(interaction: discord.Interaction, limit: int = 20):
            """Show Spotify playlists"""
            await self._handle_spotify_playlists(interaction, limit)

        @self.bot.tree.command(name="create_playlist", description="Spotifyプレイリストを作成します")
        @app_commands.describe(name="プレイリスト名", description="説明", public="公開するか")
        async def create_playlist(
            interaction: discord.Interaction,
            name: str,
            description: str = "",
            public: bool = False
        ):
            """Create Spotify playlist"""
            await self._handle_spotify_create_playlist(interaction, name, description, public)

        @self.bot.tree.command(name="play_playlist", description="Spotifyプレイリストを再生します")
        @app_commands.describe(uri="SpotifyプレイリストURIまたはURL")
        async def play_playlist(interaction: discord.Interaction, uri: str):
            """Play Spotify playlist"""
            await self._handle_spotify_play_playlist(interaction, uri)

        @self.bot.tree.command(name="queue_playlist", description="Spotifyプレイリストをキューに追加します")
        @app_commands.describe(uri="SpotifyプレイリストURIまたはURL", shuffle="シャッフルして追加するか")
        async def queue_playlist(interaction: discord.Interaction, uri: str, shuffle: bool = False):
            """Queue Spotify playlist"""
            await self._handle_spotify_queue_playlist(interaction, uri, shuffle)

        @self.bot.tree.command(name="setavatar", description="Botのアイコン画像を変更します")
        @app_commands.describe(image="新しいアイコン画像 (PNG/JPEG/GIF, 10MB以下)")
        @app_commands.checks.has_permissions(administrator=True)
        async def setavatar(interaction: discord.Interaction, image: discord.Attachment):
            """Change bot avatar"""
            await self._handle_setavatar(interaction, image)

        @setavatar.error
        async def setavatar_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            if isinstance(error, app_commands.MissingPermissions):
                await interaction.response.send_message(
                    "このコマンドはサーバー管理者のみ使用できます。",
                    ephemeral=True
                )
            else:
                logger.error(f"setavatar command error: {error}")
                await interaction.response.send_message(
                    "コマンドの実行中にエラーが発生しました。",
                    ephemeral=True
                )
    
    async def setup_commands(self):
        """Setup commands (called from bot setup_hook)"""
        # Commands are already set up in __init__
        logger.info("Commands have been set up")

    async def _set_channel_members_voice_mode(self, guild_id: int, channel: discord.VoiceChannel) -> None:
        """Set all non-bot channel members to voice mode for this guild."""
        for member in channel.members:
            if member.bot:
                continue
            user_session = await self.bot.session_handler.get_or_create_session(
                guild_id=guild_id,
                user_id=member.id
            )
            user_session.mode = 'voice'
            user_session.voice_channel_id = channel.id
            logger.info("Set voice mode for user %s (%s)", member.name, member.id)
    
    async def _handle_join(self, interaction: discord.Interaction):
        """Handle join command"""
        if not runtime_feature_manager.feature_enabled("discord_vc_input"):
            await interaction.response.send_message(
                "Discord VC音声入力はOFFです。`/feature discord_vc_input true` で有効化してください。",
                ephemeral=True,
            )
            return

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
                listening = await self.bot.voice_handler.ensure_listening(interaction.guild_id)
                await self._set_channel_members_voice_mode(interaction.guild_id, interaction.user.voice.channel)
                status = "音声認識を待受中です。" if listening else "音声認識の再開に失敗しました。ログを確認してください。"
                await interaction.response.send_message(
                    f"すでに同じボイスチャンネルに接続しています。{status}",
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
                await self._set_channel_members_voice_mode(interaction.guild_id, channel)
                listening = self.bot.voice_handler.is_listening(interaction.guild_id)
                status = (
                    "音声認識を待受中です。"
                    if listening
                    else "VC接続は完了しましたが、音声認識の開始に失敗しました。Discordログを確認してください。"
                )

                await interaction.followup.send(
                    f"🎤 **{channel.name}** に接続しました！\n"
                    f"{status}\n"
                    f"💡 ヒント: マイクで話しかけると応答します。"
                )

                logger.info(
                    "Joined voice channel: guild=%s channel=%s listening=%s",
                    interaction.guild.name,
                    channel.name,
                    listening,
                )
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

            if mode == "voice":
                if not interaction.user.voice:
                    session.mode = old_mode
                    await interaction.response.send_message(
                        "音声モードにするには、先にボイスチャンネルへ参加してください。",
                        ephemeral=True
                    )
                    return

                channel = interaction.user.voice.channel
                if self.bot.voice_handler.is_connected(interaction.guild_id):
                    voice_client = self.bot.voice_handler.get_voice_client(interaction.guild_id)
                    if voice_client and voice_client.channel != channel:
                        session.mode = old_mode
                        await interaction.response.send_message(
                            "別のボイスチャンネルに接続中です。先に `/leave` を使用してください。",
                            ephemeral=True
                        )
                        return

                    listening = await self.bot.voice_handler.ensure_listening(interaction.guild_id)
                    await self._set_channel_members_voice_mode(interaction.guild_id, channel)
                    if listening:
                        session.mode = "voice"
                        await interaction.response.send_message(
                            f"✅ モードを **音声** に変更しました。**{channel.name}** で音声認識を待受中です。"
                        )
                    else:
                        session.mode = old_mode
                        await interaction.response.send_message(
                            "音声モードに変更しましたが、音声認識の開始に失敗しました。ログを確認してください。",
                            ephemeral=True
                        )
                    logger.info(f"Changed mode from {old_mode} to voice for user {interaction.user.name}")
                    return

                await interaction.response.defer()
                voice_client = await self.bot.voice_handler.connect_voice_channel(channel)
                if not voice_client:
                    session.mode = old_mode
                    await interaction.followup.send(
                        "音声モードに変更しましたが、ボイスチャンネルへの接続に失敗しました。",
                        ephemeral=True
                    )
                    return

                await self._set_channel_members_voice_mode(interaction.guild_id, channel)
                session.mode = "voice"
                listening = self.bot.voice_handler.is_listening(interaction.guild_id)
                status = (
                    "音声認識を待受中です。"
                    if listening
                    else "VC接続は完了しましたが、音声認識の開始に失敗しました。Discordログを確認してください。"
                )
                await interaction.followup.send(
                    f"✅ モードを **音声** に変更し、**{channel.name}** に接続しました。{status}"
                )
                logger.info(
                    "Changed mode from %s to voice for user %s channel=%s listening=%s",
                    old_mode,
                    interaction.user.name,
                    channel.name,
                    listening,
                )
                return

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

    async def _handle_feature(self, interaction: discord.Interaction, feature: str, enabled: bool):
        """Handle runtime feature toggle command."""
        user_id = interaction.user.id
        if not self._is_runtime_feature_actor_allowed(user_id):
            await interaction.response.send_message(
                "このコマンドを実行する権限がありません。",
                ephemeral=True,
            )
            return

        try:
            status = runtime_feature_manager.update_feature(feature, enabled, persist=True)
            definition = next(
                (item for item in status["definitions"] if item["key"] == feature),
                None,
            )
            restart_note = (
                "\n⚠️ この変更は次回起動または再起動後に完全反映されます。"
                if definition and definition["restart_required"]
                else ""
            )
            await interaction.response.send_message(
                f"✅ `{feature}` を `{'ON' if enabled else 'OFF'}` にしました。{restart_note}",
                ephemeral=True,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    def _is_runtime_feature_actor_allowed(self, actor_id: int | str) -> bool:
        allowed_ids = self.bot.config.get(
            "runtime_feature_permissions.allowed_discord_user_ids",
            [],
        )
        return str(actor_id) in {str(uid) for uid in allowed_ids}
    
    async def _handle_status(self, interaction: discord.Interaction):
        """Handle status command"""
        try:
            session = await self.bot.session_handler.get_or_create_session(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id
            )
            
            # ボイスチャンネル接続状態
            voice_status = "未接続"
            listening_status = "停止中"
            voice_info = self.bot.voice_handler.get_connection_status(interaction.guild_id)
            if voice_info["connected"]:
                voice_status = f"接続中: {voice_info['channel_name']}"
                listening_status = "待受中" if voice_info["listening"] else "停止中"
            
            # モードとキャラクター
            mode_name = "テキスト" if session.mode == "text" else "音声"
            
            embed = discord.Embed(
                title="📊 現在のステータス",
                color=discord.Color.blue()
            )
            embed.add_field(name="ボイスチャンネル", value=voice_status, inline=False)
            embed.add_field(name="音声認識", value=listening_status, inline=True)
            embed.add_field(name="動作モード", value=mode_name, inline=True)
            embed.add_field(name="キャラクター", value=session.character or self.bot.default_character, inline=True)
            embed.add_field(name="セッションID", value=f"`{session.id[:8]}...`", inline=True)
            embed.add_field(
                name="TTS",
                value=voice_info["tts_engine"] or "未初期化",
                inline=True
            )
            
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
        try:
            session = await self.bot.session_handler.get_or_create_session(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id
            )
            voice_info = self.bot.voice_handler.get_connection_status(interaction.guild_id)
            feature_status = runtime_feature_manager.status()
            characters = self.bot.config.get_available_characters()
            character_text = ", ".join(characters[:12]) if characters else "取得できませんでした"
            if len(characters) > 12:
                character_text += f" ほか{len(characters) - 12}件"

            embed = discord.Embed(
                title="⚙️ AoiTalk Discord 設定",
                description="現在のDiscordセッションとBot設定です。",
                color=discord.Color.blurple()
            )
            embed.add_field(
                name="セッション",
                value=(
                    f"モード: `{session.mode}`\n"
                    f"キャラクター: `{session.character or self.bot.default_character}`\n"
                    f"既定モード: `{self.bot.default_mode}`"
                ),
                inline=False
            )
            embed.add_field(
                name="音声",
                value=(
                    f"接続: `{voice_info['channel_name'] or '未接続'}`\n"
                    f"音声認識: `{'待受中' if voice_info['listening'] else '停止中'}`\n"
                    f"TTS: `{voice_info['tts_engine'] or '未初期化'}`\n"
                    f"サンプルレート/チャンネル: `{self.bot.voice_handler.sample_rate}Hz / {self.bot.voice_handler.channels}ch`"
                ),
                inline=False
            )
            features = feature_status["features"]
            embed.add_field(
                name="Runtime features",
                value=(
                    f"WebUI: `ON`\n"
                    f"読み上げ: `{'ON' if features.get('tts') else 'OFF'}`\n"
                    f"ローカル音声: `{'ON' if feature_status.get('local_audio_enabled') else 'OFF'}`\n"
                    f"Discord Bot: `{'ON' if features.get('discord_bot') else 'OFF'}`\n"
                    f"Discord VC入力/出力: `{'ON' if features.get('discord_vc_input') else 'OFF'} / {'ON' if features.get('discord_vc_output') else 'OFF'}`"
                ),
                inline=False
            )
            embed.add_field(
                name="Discordコマンド同期",
                value=(
                    f"有効: `{self.bot.config.get('discord.sync_commands', False)}`\n"
                    f"範囲: `{self.bot.config.get('discord.sync_command_scope', 'guild')}`"
                ),
                inline=True
            )
            embed.add_field(
                name="会話履歴",
                value=(
                    f"最大履歴: `{self.bot.config.get('discord.max_history_length', 20)}`\n"
                    f"復元件数: `{self.bot.config.get('discord.memory_prefill_message_count', 10)}`"
                ),
                inline=True
            )
            embed.add_field(name="利用可能キャラクター", value=character_text[:1024], inline=False)
            embed.set_footer(text="変更は /character, /mode, /feature, Spotify系コマンドで行います。")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to show settings: {e}", exc_info=True)
            await interaction.response.send_message(
                "設定の取得に失敗しました。",
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
            name="基本コマンド",
            value=(
                "`/join` - ボイスチャンネルに参加\n"
                "`/leave` - ボイスチャンネルから退出\n"
                "`/character <名前>` - キャラクター変更\n"
                "`/mode <text/voice>` - Discordセッション内の入力モード切替\n"
                "`/feature <機能> <true/false>` - AoiTalk機能トグル\n"
                "`/status` - 現在の状態を表示\n"
                "`/clear` - 会話履歴をクリア\n"
                "`/setavatar <画像>` - Botアイコン変更 (管理者)\n"
                "`/settings` - 現在の設定を表示\n"
                "`/nanobanana` - Nanobanana Pro情報と画像を生成\n"
                "`/help` - このヘルプを表示"
            ),
            inline=False
        )

        embed.add_field(
            name="Spotify音楽コマンド",
            value=(
                "`/spotify_auth` - Spotify認証URLを表示\n"
                "`/spotify_code <code>` - Spotify認証コードを登録\n"
                "`/search <検索語>` - 音楽を検索\n"
                "`/play <曲名>` - 音楽を再生\n"
                "`/pause` - 再生を一時停止\n"
                "`/skip` - 次の曲にスキップ\n"
                "`/previous` - 前の曲に戻る\n"
                "`/queue <曲名>` - キューに追加\n"
                "`/show_queue` / `/clear_queue` / `/remove_queue` - 内部キュー操作\n"
                "`/playlists` - プレイリスト一覧\n"
                "`/create_playlist` - プレイリスト作成\n"
                "`/play_playlist` / `/queue_playlist` - プレイリスト再生/追加\n"
                "`/nowplaying` - 現在の曲を表示"
            ),
            inline=False
        )
        
        embed.add_field(
            name="Tips",
            value=(
                "• テキストモードではBotをメンションして話しかけてください\n"
                "• 音声モードでは `/join` または `/mode voice` で音声認識を開始してください\n"
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
                memory_manager = getattr(session.assistant.llm_client, 'memory_manager', None)
                if memory_manager:
                    memory_user_id = session.assistant._build_memory_user_id(
                        interaction.user.id,
                        interaction.guild_id
                    )
                    await memory_manager.start_new_session(
                        memory_user_id,
                        session.character or self.bot.default_character
                    )
                session.memory_prefilled = True

            await interaction.response.send_message(
                "🗑️ 会話履歴をクリアしました。\n"
                "新しい会話を始めることができます。保存済みの直近履歴も次回復元されません。"
            )

        except Exception as e:
            logger.error(f"Failed to clear history: {e}")
            await interaction.response.send_message(
                "会話履歴のクリアに失敗しました。",
                ephemeral=True
            )
    
    async def _handle_setavatar(self, interaction: discord.Interaction, image: discord.Attachment):
        """Handle setavatar command"""
        ALLOWED_TYPES = {"image/png", "image/jpeg", "image/gif"}
        MAX_SIZE = 10 * 1024 * 1024  # 10MB

        # MIMEタイプ検証
        if not image.content_type or image.content_type not in ALLOWED_TYPES:
            await interaction.response.send_message(
                f"対応形式: PNG, JPEG, GIF\n受信: {image.content_type or '不明'}",
                ephemeral=True
            )
            return

        # サイズ検証
        if image.size > MAX_SIZE:
            await interaction.response.send_message(
                f"ファイルサイズが大きすぎます ({image.size // 1024}KB > 10MB)",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            avatar_bytes = await image.read()
            await self.bot.user.edit(avatar=avatar_bytes)
            await interaction.followup.send("アイコンを変更しました！")
            logger.info(f"Bot avatar changed by {interaction.user.name} ({interaction.user.id})")
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = getattr(e, 'retry_after', None)
                msg = f"レート制限中です。{retry_after:.0f}秒後に再試行してください。" if retry_after else "レート制限中です。しばらく待ってから再試行してください。"
                await interaction.followup.send(msg)
            else:
                logger.error(f"Failed to change avatar (HTTP {e.status}): {e}")
                await interaction.followup.send(f"アイコンの変更に失敗しました: {e.text}")
        except Exception as e:
            logger.error(f"Failed to change avatar: {e}")
            await interaction.followup.send("アイコンの変更に失敗しました。")

    async def _send_command_result(
        self,
        interaction: discord.Interaction,
        title: str,
        result: Any,
        *,
        ephemeral: bool = False,
        color: discord.Color = discord.Color.green()
    ) -> None:
        """Send a slash command result without exceeding Discord message limits."""
        text = str(result or "結果はありません。").strip()
        if len(text) <= 3900:
            embed = discord.Embed(title=title, description=text, color=color)
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
            return

        chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)]
        first = f"**{title}**\n{chunks[0]}"
        if interaction.response.is_done():
            await interaction.followup.send(first, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(first, ephemeral=ephemeral)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=ephemeral)

    async def _run_spotify_command(
        self,
        interaction: discord.Interaction,
        title: str,
        func: Callable[..., Any],
        *args: Any,
        ephemeral: bool = False,
        **kwargs: Any
    ) -> None:
        """Run blocking Spotify helpers off the Discord event loop."""
        await interaction.response.defer(ephemeral=ephemeral)
        try:
            def invoke():
                if hasattr(func, 'execute'):
                    call_kwargs = dict(kwargs)
                    for param, value in zip(getattr(func, 'parameters', []), args):
                        call_kwargs[param.name] = value
                    return func.execute(**call_kwargs)
                return func(*args, **kwargs)

            result = await asyncio.to_thread(invoke)
            await self._send_command_result(interaction, title, result, ephemeral=ephemeral)
        except Exception as e:
            logger.error("Spotify command failed: %s", e, exc_info=True)
            await interaction.followup.send(
                "❌ Spotifyコマンドの実行に失敗しました。認証と設定を確認してください。",
                ephemeral=True
            )

    async def _handle_spotify_auth(self, interaction: discord.Interaction):
        from ...tools.entertainment.spotify import setup_spotify_auth

        await self._run_spotify_command(
            interaction,
            "Spotify認証",
            setup_spotify_auth,
            ephemeral=True
        )

    async def _handle_spotify_code(self, interaction: discord.Interaction, code: str):
        from ...tools.entertainment.spotify import set_spotify_auth_code

        await self._run_spotify_command(
            interaction,
            "Spotify認証コード登録",
            set_spotify_auth_code,
            code,
            ephemeral=True
        )

    async def _handle_spotify_search(
        self,
        interaction: discord.Interaction,
        query: str,
        search_type: str,
        limit: int
    ):
        from ...tools.entertainment.spotify import search_spotify_music

        safe_limit = max(1, min(int(limit), 10))
        await self._run_spotify_command(
            interaction,
            "Spotify検索",
            search_spotify_music,
            query,
            search_type,
            safe_limit
        )

    async def _handle_spotify_previous(self, interaction: discord.Interaction):
        from ...tools.entertainment.spotify import previous_track

        await self._run_spotify_command(interaction, "前の曲", previous_track)

    async def _handle_spotify_show_queue(self, interaction: discord.Interaction):
        from ...tools.entertainment.spotify import show_queue

        await self._run_spotify_command(interaction, "Spotify内部キュー", show_queue)

    async def _handle_spotify_clear_queue(self, interaction: discord.Interaction):
        from ...tools.entertainment.spotify import clear_spotify_queue

        await self._run_spotify_command(interaction, "Spotify内部キュークリア", clear_spotify_queue)

    async def _handle_spotify_remove_queue(self, interaction: discord.Interaction, position: int):
        from ...tools.entertainment.spotify import remove_from_queue

        await self._run_spotify_command(
            interaction,
            "Spotify内部キュー削除",
            remove_from_queue,
            max(1, int(position))
        )

    async def _handle_spotify_playlists(self, interaction: discord.Interaction, limit: int):
        from ...tools.entertainment.spotify import get_spotify_user_playlists

        await self._run_spotify_command(
            interaction,
            "Spotifyプレイリスト",
            get_spotify_user_playlists,
            max(1, min(int(limit), 50)),
            ephemeral=True
        )

    async def _handle_spotify_create_playlist(
        self,
        interaction: discord.Interaction,
        name: str,
        description: str,
        public: bool
    ):
        from ...tools.entertainment.spotify import create_playlist

        await self._run_spotify_command(
            interaction,
            "Spotifyプレイリスト作成",
            create_playlist,
            name,
            description,
            public,
            ephemeral=True
        )

    async def _handle_spotify_play_playlist(self, interaction: discord.Interaction, uri: str):
        from ...tools.entertainment.spotify import play_playlist

        await self._run_spotify_command(interaction, "Spotifyプレイリスト再生", play_playlist, uri)

    async def _handle_spotify_queue_playlist(self, interaction: discord.Interaction, uri: str, shuffle: bool):
        from ...tools.entertainment.spotify import add_playlist_to_queue

        await self._run_spotify_command(
            interaction,
            "Spotifyプレイリストをキューに追加",
            add_playlist_to_queue,
            uri,
            shuffle
        )

    async def _handle_spotify_play(self, interaction: discord.Interaction, query: str):
        """Handle Spotify play command"""
        from ...tools.entertainment.spotify import play_song_now

        await interaction.response.defer()

        try:
            # Use direct Spotify tools for playback
            result = await asyncio.to_thread(play_song_now, query)
            
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
        from ...tools.entertainment.spotify import pause_spotify

        try:
            result = await asyncio.to_thread(pause_spotify)
            
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
        from ...tools.entertainment.spotify import skip_spotify_track

        try:
            result = await asyncio.to_thread(skip_spotify_track)
            
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
        from ...tools.entertainment.spotify import queue_song

        await interaction.response.defer()

        try:
            result = await asyncio.to_thread(queue_song, query)
            
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
        from ...tools.entertainment.spotify import get_spotify_status

        try:
            result = await asyncio.to_thread(get_spotify_status)
            
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
