from abc import ABC, abstractmethod
from typing import Optional, Callable, Dict, Any
import numpy as np


class AudioInputInterface(ABC):
    """音声入力の共通インターフェース"""
    
    @abstractmethod
    async def start(self) -> None:
        """音声入力を開始する"""
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """音声入力を停止する"""
        pass
    
    @abstractmethod
    async def read_audio(self, duration: Optional[float] = None) -> Optional[np.ndarray]:
        """音声データを読み取る
        
        Args:
            duration: 読み取る秒数（Noneの場合は利用可能なデータすべて）
            
        Returns:
            音声データのnumpy配列（float32, -1.0 ~ 1.0）
        """
        pass
    
    @abstractmethod
    def get_sample_rate(self) -> int:
        """サンプリングレートを取得する"""
        pass
    
    @abstractmethod
    def is_active(self) -> bool:
        """入力がアクティブかどうか"""
        pass
    
    @property
    @abstractmethod
    def metadata(self) -> Dict[str, Any]:
        """入力ソースのメタデータ"""
        pass
    
    def set_callback(self, callback: Optional[Callable[[np.ndarray], None]]) -> None:
        """音声データ受信時のコールバックを設定"""
        self._callback = callback