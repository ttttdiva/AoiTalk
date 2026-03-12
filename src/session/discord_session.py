from typing import Dict, Any, Optional
import discord
from .base import BaseSession


class DiscordSession(BaseSession):
    """Discordモード用セッション"""
    
    def __init__(self, 
                 user: discord.User,
                 guild: Optional[discord.Guild] = None,
                 channel: Optional[discord.VoiceChannel] = None,
                 character: Optional[str] = None,
                 session_id: Optional[str] = None):
        """Discordセッションを初期化
        
        Args:
            user: Discordユーザー
            guild: Discordギルド（サーバー）
            channel: Discord音声チャンネル
            character: 使用するキャラクター名
            session_id: セッションID（オプション）
        """
        # ユーザーIDを含むセッションIDを生成
        if not session_id:
            session_id = f"discord_{user.id}_{guild.id if guild else 'dm'}"
            
        super().__init__(
            session_id=session_id,
            mode="discord",
            character=character
        )
        
        # Discord固有の情報
        self.user = user
        self.guild = guild
        self.channel = channel
        
        # Discord固有の設定
        self.settings.update({
            'voice_detection_mode': 'auto',  # auto, manual, push-to-talk
            'noise_suppression': True,
            'echo_cancellation': True,
            'auto_gain_control': True
        })
        
    async def initialize(self) -> bool:
        """セッションを初期化
        
        Returns:
            初期化が成功したかどうか
        """
        try:
            # Discordユーザーの設定を読み込む（将来的な拡張用）
            # await self._load_discord_user_settings()
            
            # ユーザーのロールに基づいて権限を設定
            if self.guild and isinstance(self.user, discord.Member):
                self._setup_permissions(self.user)
                
            return True
        except Exception as e:
            print(f"[DiscordSession] Failed to initialize: {e}")
            return False
            
    async def cleanup(self):
        """セッションをクリーンアップ"""
        # Discordセッションのクリーンアップ処理
        self.is_active = False
        
        # 音声チャンネルから切断（必要に応じて）
        # この処理は通常VoiceHandlerで行われるため、ここでは状態のみ更新
        self.channel = None
        
    def get_user_info(self) -> Dict[str, Any]:
        """ユーザー情報を取得
        
        Returns:
            ユーザー情報の辞書
        """
        info = {
            'user_id': str(self.user.id),
            'username': self.user.name,
            'discriminator': self.user.discriminator,
            'display_name': self.user.display_name,
            'type': 'discord',
            'is_bot': self.user.bot
        }
        
        if self.guild:
            info.update({
                'guild_id': str(self.guild.id),
                'guild_name': self.guild.name
            })
            
        if self.channel:
            info.update({
                'channel_id': str(self.channel.id),
                'channel_name': self.channel.name
            })
            
        # メンバー固有の情報（ニックネーム、ロールなど）
        if isinstance(self.user, discord.Member):
            info.update({
                'nickname': self.user.nick,
                'roles': [role.name for role in self.user.roles if role.name != '@everyone']
            })
            
        return info
        
    def _setup_permissions(self, member: discord.Member):
        """メンバーのロールに基づいて権限を設定
        
        Args:
            member: Discordメンバー
        """
        # 管理者権限チェック
        is_admin = member.guild_permissions.administrator
        
        # 特定のロールに基づく設定
        role_names = [role.name.lower() for role in member.roles]
        
        # VIP/Premiumユーザーの設定
        if any(role in role_names for role in ['vip', 'premium', 'supporter']):
            self.settings['max_message_length'] = 1000
            self.settings['priority_queue'] = True
        else:
            self.settings['max_message_length'] = 500
            self.settings['priority_queue'] = False
            
        # 管理者の設定
        if is_admin:
            self.settings['admin_commands'] = True
            self.settings['bypass_cooldown'] = True
        else:
            self.settings['admin_commands'] = False
            self.settings['bypass_cooldown'] = False
            
    async def update_voice_channel(self, channel: Optional[discord.VoiceChannel]):
        """音声チャンネルを更新
        
        Args:
            channel: 新しい音声チャンネル（Noneで切断）
        """
        self.channel = channel
        self.update_activity()
        
        # チャンネル変更をコンテキストに記録
        if channel:
            self.update_context({
                'voice_channel_change': {
                    'channel_id': str(channel.id),
                    'channel_name': channel.name,
                    'member_count': len(channel.members)
                }
            })
        else:
            self.update_context({
                'voice_channel_change': {
                    'disconnected': True
                }
            })