from abc import ABC, abstractmethod
from typing import Optional, Union
import numpy as np


class AudioOutputInterface(ABC):
    """音声出力の共通インターフェース"""
    
    @abstractmethod
    async def play(self, audio_data: Union[bytes, np.ndarray], 
                   format: str = "wav", sample_rate: Optional[int] = None) -> None:
        """音声データを再生する
        
        Args:
            audio_data: 音声データ（バイナリまたはnumpy配列）
            format: 音声フォーマット（wav, mp3, etc.）
            sample_rate: サンプリングレート（指定しない場合はデフォルト）
        """
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """再生を停止する"""
        pass
    
    @abstractmethod
    async def pause(self) -> None:
        """再生を一時停止する"""
        pass
    
    @abstractmethod
    async def resume(self) -> None:
        """再生を再開する"""
        pass
    
    @abstractmethod
    def is_playing(self) -> bool:
        """再生中かどうか"""
        pass
    
    @abstractmethod
    def get_volume(self) -> float:
        """現在の音量を取得（0.0 ~ 1.0）"""
        pass
    
    @abstractmethod
    def set_volume(self, volume: float) -> None:
        """音量を設定（0.0 ~ 1.0）"""
        pass
    
    @abstractmethod
    def get_sample_rate(self) -> int:
        """出力のサンプリングレートを取得"""
        pass