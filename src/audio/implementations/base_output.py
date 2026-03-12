"""音声出力実装の基底クラス"""
import asyncio
from abc import ABC, abstractmethod
from typing import Optional, Union
import numpy as np
from ..interfaces.output import AudioOutputInterface


class BaseAudioOutput(AudioOutputInterface, ABC):
    """音声出力実装の基底クラス
    
    共通の状態管理、音量制御、データ変換処理を提供
    """
    
    def __init__(self, default_sample_rate: int, channels: int = 1):
        """基底クラスの初期化
        
        Args:
            default_sample_rate: デフォルトのサンプリングレート（Hz）
            channels: チャンネル数
        """
        self.default_sample_rate = default_sample_rate
        self.channels = channels
        
        # 共通の状態管理
        self._playing = False
        self._paused = False
        self._volume = 1.0
        
    async def play(self, audio_data: Union[bytes, np.ndarray], 
                   format: str = "wav", sample_rate: Optional[int] = None) -> None:
        """音声データを再生する
        
        Args:
            audio_data: 音声データ（バイナリまたはnumpy配列）
            format: 音声フォーマット（wav, mp3, etc.）
            sample_rate: サンプリングレート
        """
        # 再生中の場合は停止
        if self._playing:
            await self.stop()
            await asyncio.sleep(0.1)  # 短い待機時間
            
        self._playing = True
        self._paused = False
        
        try:
            # データを準備
            prepared_data = await self._prepare_audio_data(audio_data, format, sample_rate)
            
            # 実装固有の再生処理
            await self._play_internal(prepared_data, format, sample_rate or self.default_sample_rate)
            
        except Exception as e:
            self._playing = False
            self._paused = False
            raise Exception(f"Failed to play audio: {e}")
            
    @abstractmethod
    async def _prepare_audio_data(self, audio_data: Union[bytes, np.ndarray], 
                                  format: str, sample_rate: Optional[int]) -> Union[bytes, np.ndarray]:
        """音声データを再生用に準備する（実装固有）"""
        pass
        
    @abstractmethod
    async def _play_internal(self, audio_data: Union[bytes, np.ndarray], 
                            format: str, sample_rate: int) -> None:
        """実際の再生処理（実装固有）"""
        pass
        
    async def stop(self) -> None:
        """再生を停止する"""
        self._playing = False
        self._paused = False
        await self._stop_internal()
        
    @abstractmethod
    async def _stop_internal(self) -> None:
        """実際の停止処理（実装固有）"""
        pass
        
    async def pause(self) -> None:
        """再生を一時停止する"""
        if self._playing:
            self._paused = True
            await self._pause_internal()
            
    async def resume(self) -> None:
        """再生を再開する"""
        if self._paused:
            self._paused = False
            await self._resume_internal()
            
    @abstractmethod
    async def _pause_internal(self) -> None:
        """実際の一時停止処理（実装固有）"""
        pass
        
    @abstractmethod
    async def _resume_internal(self) -> None:
        """実際の再開処理（実装固有）"""
        pass
        
    def is_playing(self) -> bool:
        """再生中かどうか"""
        return self._playing and not self._paused
        
    def get_volume(self) -> float:
        """現在の音量を取得（0.0 ~ 1.0）"""
        return self._volume
        
    def set_volume(self, volume: float) -> None:
        """音量を設定（0.0 ~ 1.0）"""
        self._volume = max(0.0, min(1.0, volume))
        self._apply_volume_change()
        
    @abstractmethod
    def _apply_volume_change(self) -> None:
        """音量変更を適用する（実装固有）"""
        pass
        
    def get_sample_rate(self) -> int:
        """出力のサンプリングレートを取得"""
        return self.default_sample_rate
        
    def _convert_numpy_to_bytes(self, audio_data: np.ndarray, 
                               target_dtype: np.dtype = np.int16) -> bytes:
        """numpy配列をバイト列に変換する共通処理
        
        Args:
            audio_data: 音声データのnumpy配列
            target_dtype: 変換先のデータ型
            
        Returns:
            変換されたバイト列
        """
        if audio_data.dtype == np.float32 and target_dtype == np.int16:
            # float32 を int16 に変換
            audio_data = (audio_data * 32767).astype(np.int16)
        elif audio_data.dtype != target_dtype:
            audio_data = audio_data.astype(target_dtype)
            
        return audio_data.tobytes()
        
    def _apply_volume_to_array(self, audio_data: np.ndarray) -> np.ndarray:
        """numpy配列に音量を適用する共通処理
        
        Args:
            audio_data: 音声データのnumpy配列
            
        Returns:
            音量調整済みのnumpy配列
        """
        if self._volume != 1.0:
            return (audio_data * self._volume).astype(audio_data.dtype)
        return audio_data