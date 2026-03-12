import asyncio
import numpy as np
from typing import Optional, Dict, Any, Union
from collections import defaultdict
from discord.ext import voice_recv
import discord
from ..base_input import BaseAudioInput
from queue import Queue


class VoiceChannelInput(BaseAudioInput):
    """Discord音声チャンネル入力の実装"""
    
    def __init__(self, voice_client: voice_recv.VoiceRecvClient, 
                 user_id: Optional[int] = None,
                 sample_rate: int = 48000,
                 channels: int = 2):
        """Discord音声入力を初期化
        
        Args:
            voice_client: Discord音声クライアント（voice_recv対応）
            user_id: 特定ユーザーの音声のみを取得する場合のユーザーID
            sample_rate: サンプリングレート（Discordは48000Hz固定）
            channels: チャンネル数（Discordは2ch固定）
        """
        # 基底クラスの初期化
        super().__init__(sample_rate, channels)
        
        self.voice_client = voice_client
        self.user_id = user_id
        
        # ユーザー別の音声バッファ
        self.audio_buffers: Dict[int, bytearray] = defaultdict(bytearray)
        
        # カスタムシンク
        self._audio_sink: Optional[CustomAudioSink] = None
        
    def _create_queue(self) -> Union[asyncio.Queue, Queue]:
        """キューの作成（asyncio.Queueを使用）"""
        return asyncio.Queue()
        
    async def _initialize_resources(self) -> None:
        """リソースの初期化"""
        # カスタムシンクを作成してリスニング開始
        if hasattr(self.voice_client, 'listen'):
            self._audio_sink = CustomAudioSink(self)
            self.voice_client.listen(self._audio_sink)
            
    async def _cleanup_resources(self) -> None:
        """リソースのクリーンアップ"""
        # リスニングを停止
        if hasattr(self.voice_client, 'stop_listening'):
            self.voice_client.stop_listening()
            
        # バッファをクリア
        self.audio_buffers.clear()
                
    async def _read_all_available(self) -> list[np.ndarray]:
        """利用可能なすべてのデータを読み取る"""
        audio_chunks = []
        while not self._audio_queue.empty():
            try:
                chunk = await asyncio.wait_for(
                    self._audio_queue.get(), 
                    timeout=0.1
                )
                audio_chunks.append(chunk)
            except asyncio.TimeoutError:
                break
        return audio_chunks
        
    async def _read_duration(self, duration: float) -> list[np.ndarray]:
        """指定時間分のデータを読み取る"""
        audio_chunks = []
        samples_needed = int(duration * self.sample_rate * self.channels)
        samples_read = 0
        timeout = duration + 1.0
        
        start_time = asyncio.get_event_loop().time()
        
        while samples_read < samples_needed:
            if asyncio.get_event_loop().time() - start_time > timeout:
                break
                
            try:
                chunk = await asyncio.wait_for(
                    self._audio_queue.get(),
                    timeout=0.1
                )
                audio_chunks.append(chunk)
                samples_read += len(chunk)
            except asyncio.TimeoutError:
                await asyncio.sleep(0.01)
                
        return audio_chunks
        
    def is_active(self) -> bool:
        """入力がアクティブかどうか"""
        return self._active and self.voice_client.is_connected()
        
    @property
    def metadata(self) -> Dict[str, Any]:
        """入力ソースのメタデータ"""
        channel_info = {}
        if self.voice_client.channel:
            channel_info = {
                'name': self.voice_client.channel.name,
                'id': self.voice_client.channel.id,
                'guild': self.voice_client.guild.name,
                'members': len(self.voice_client.channel.members)
            }
            
        return {
            'type': 'discord_voice_channel',
            'sample_rate': self.sample_rate,
            'channels': self.channels,
            'user_filter': self.user_id,
            'channel_info': channel_info
        }
        
    def _on_voice_receive(self, pcm_data: bytes, user: discord.User):
        """音声データ受信時の処理（内部用）"""
        if not self._active:
            return
            
        # 特定ユーザーのフィルタリング
        if self.user_id and user.id != self.user_id:
            return
            
        # PCMデータをfloat32に変換
        audio_array = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
        
        # キューに追加
        asyncio.create_task(self._audio_queue.put(audio_array))
        
        # コールバックを呼び出す
        self._invoke_callback(audio_array)


class CustomAudioSink(voice_recv.AudioSink):
    """VoiceChannelInput用のカスタムシンク"""
    
    def __init__(self, voice_input: VoiceChannelInput):
        super().__init__()
        self.voice_input = voice_input
        
    def wants_opus(self):
        """PCMデータを受信するためFalseを返す"""
        return False
        
    def write(self, user, data):
        """音声データを処理"""
        if not user or user.bot:
            return
            
        # PCMデータを取得
        if hasattr(data, 'pcm'):
            pcm_data = data.pcm
            self.voice_input._on_voice_receive(pcm_data, user)
            
    def cleanup(self):
        """クリーンアップ処理"""
        pass