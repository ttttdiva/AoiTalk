from abc import ABC, abstractmethod
from typing import Optional, Tuple
import numpy as np


class AudioProcessorInterface(ABC):
    """音声処理の共通インターフェース"""
    
    @abstractmethod
    def process(self, audio_data: np.ndarray, sample_rate: int) -> np.ndarray:
        """音声データを処理する
        
        Args:
            audio_data: 入力音声データ
            sample_rate: サンプリングレート
            
        Returns:
            処理済み音声データ
        """
        pass
    
    @abstractmethod
    def detect_voice(self, audio_data: np.ndarray, sample_rate: int) -> bool:
        """音声が含まれているか検出する"""
        pass
    
    @abstractmethod
    def calculate_volume(self, audio_data: np.ndarray) -> float:
        """音量を計算する（0.0 ~ 1.0）"""
        pass
    
    @abstractmethod
    def resample(self, audio_data: np.ndarray, 
                 original_rate: int, target_rate: int) -> np.ndarray:
        """サンプリングレートを変換する"""
        pass
    
    @abstractmethod
    def normalize(self, audio_data: np.ndarray) -> np.ndarray:
        """音声データを正規化する（-1.0 ~ 1.0）"""
        pass
    
    @abstractmethod
    def remove_noise(self, audio_data: np.ndarray, 
                     sample_rate: int) -> np.ndarray:
        """ノイズを除去する"""
        pass
    
    @abstractmethod
    def detect_silence(self, audio_data: np.ndarray, 
                      sample_rate: int,
                      threshold: float = 0.01,
                      duration: float = 0.5) -> Tuple[int, int]:
        """無音区間を検出する
        
        Returns:
            (開始インデックス, 終了インデックス)
        """
        pass