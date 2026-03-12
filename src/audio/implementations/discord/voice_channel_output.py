import asyncio
import io
import discord
import numpy as np
from typing import Optional, Union
from ..base_output import BaseAudioOutput


class VoiceChannelOutput(BaseAudioOutput):
    """Discord音声チャンネル出力の実装"""
    
    def __init__(self, voice_client: discord.VoiceClient,
                 default_sample_rate: int = 48000):
        """Discord音声出力を初期化
        
        Args:
            voice_client: Discord音声クライアント
            default_sample_rate: デフォルトのサンプリングレート
        """
        # 基底クラスの初期化（Discordは2チャンネル固定）
        super().__init__(default_sample_rate, channels=2)
        
        self.voice_client = voice_client
        self._current_source: Optional[discord.AudioSource] = None
        
    async def _prepare_audio_data(self, audio_data: Union[bytes, np.ndarray], 
                                  format: str, sample_rate: Optional[int]) -> Union[bytes, np.ndarray]:
        """音声データを再生用に準備する"""
        if isinstance(audio_data, np.ndarray):
            # 基底クラスのメソッドを使用して変換
            return self._convert_numpy_to_bytes(audio_data, np.int16)
        return audio_data
        
    async def _play_internal(self, audio_data: Union[bytes, np.ndarray], 
                            format: str, sample_rate: int) -> None:
        """実際の再生処理"""
        try:
            # メモリ上でFFmpegオーディオソースを作成
            audio_io = io.BytesIO(audio_data)
            
            # FFmpegPCMAudioソースを作成
            ffmpeg_options = {
                'pipe': True,
                'options': f'-f {format}'
            }
            
            if format == 'wav':
                # WAVの場合は直接再生
                source = discord.FFmpegPCMAudio(audio_io, **ffmpeg_options)
            else:
                # その他のフォーマットは変換
                source = discord.FFmpegPCMAudio(audio_io, **ffmpeg_options)
                
            # 音量調整を適用
            if self._volume != 1.0:
                source = discord.PCMVolumeTransformer(source, volume=self._volume)
                
            self._current_source = source
            
            # 再生完了を待つためのイベント
            done_event = asyncio.Event()
            
            def after_playback(error):
                if error:
                    print(f"[VoiceChannelOutput] Playback error: {error}")
                self._playing = False
                self._current_source = None
                # イベントループで実行
                asyncio.run_coroutine_threadsafe(
                    self._set_done_event(done_event),
                    asyncio.get_event_loop()
                )
                
            # Discord音声クライアントで再生
            self.voice_client.play(source, after=after_playback)
            
            # 再生完了を待つ（オプション）
            # await done_event.wait()
            
        except Exception as e:
            self._playing = False
            self._current_source = None
            raise
            
    async def _set_done_event(self, event: asyncio.Event):
        """イベントをセットする（非同期コンテキスト用）"""
        event.set()
        
    async def _stop_internal(self) -> None:
        """実際の停止処理"""
        if self.voice_client.is_playing():
            self.voice_client.stop()
        self._current_source = None
        
    async def _pause_internal(self) -> None:
        """実際の一時停止処理"""
        if self.voice_client.is_playing():
            self.voice_client.pause()
            
    async def _resume_internal(self) -> None:
        """実際の再開処理"""
        if self.voice_client.is_paused():
            self.voice_client.resume()
            
    def is_playing(self) -> bool:
        """再生中かどうか"""
        return self.voice_client.is_playing()
        
    def _apply_volume_change(self) -> None:
        """音量変更を適用する"""
        # 現在再生中のソースがPCMVolumeTransformerの場合は音量を更新
        if (self._current_source and 
            isinstance(self._current_source, discord.PCMVolumeTransformer)):
            self._current_source.volume = self._volume
            
    def get_sample_rate(self) -> int:
        """出力のサンプリングレートを取得"""
        # Discordは48000Hz固定
        return 48000