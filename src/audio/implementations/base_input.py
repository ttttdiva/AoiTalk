"""音声入力実装の基底クラス"""
import asyncio
from abc import ABC, abstractmethod
from typing import Optional, Callable, Dict, Any, Union
import numpy as np
from queue import Queue
from ..interfaces.input import AudioInputInterface


class BaseAudioInput(AudioInputInterface, ABC):
    """音声入力実装の基底クラス
    
    共通の状態管理、キュー処理、コールバック処理を提供
    """
    
    def __init__(self, sample_rate: int, channels: int):
        """基底クラスの初期化
        
        Args:
            sample_rate: サンプリングレート（Hz）
            channels: チャンネル数
        """
        self.sample_rate = sample_rate
        self.channels = channels
        
        # 共通の状態管理
        self._active = False
        self._callback: Optional[Callable[[np.ndarray], None]] = None
        
        # 音声データのキュー（実装によってasyncio.QueueまたはQueueを使用）
        self._audio_queue: Optional[Union[asyncio.Queue, Queue]] = None
        
    @abstractmethod
    async def _initialize_resources(self) -> None:
        """リソースの初期化（実装固有）"""
        pass
        
    @abstractmethod
    async def _cleanup_resources(self) -> None:
        """リソースのクリーンアップ（実装固有）"""
        pass
        
    @abstractmethod
    def _create_queue(self) -> Union[asyncio.Queue, Queue]:
        """キューの作成（実装に応じて適切な型を返す）"""
        pass
        
    async def start(self) -> None:
        """音声入力を開始する"""
        if self._active:
            return
            
        # キューを作成
        self._audio_queue = self._create_queue()
        
        # リソースを初期化
        await self._initialize_resources()
        
        self._active = True
        
    async def stop(self) -> None:
        """音声入力を停止する"""
        self._active = False
        
        # リソースをクリーンアップ
        await self._cleanup_resources()
        
        # キューをクリア
        await self._clear_queue()
        
    async def _clear_queue(self) -> None:
        """キューをクリアする"""
        if self._audio_queue is None:
            return
            
        if isinstance(self._audio_queue, asyncio.Queue):
            while not self._audio_queue.empty():
                try:
                    self._audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        else:
            # 通常のQueueの場合
            while not self._audio_queue.empty():
                try:
                    self._audio_queue.get_nowait()
                except:
                    break
                    
    async def read_audio(self, duration: Optional[float] = None) -> Optional[np.ndarray]:
        """音声データを読み取る
        
        Args:
            duration: 読み取る秒数（Noneの場合は利用可能なデータすべて）
            
        Returns:
            音声データのnumpy配列（float32, -1.0 ~ 1.0）
        """
        if not self._active or self._audio_queue is None:
            return None
            
        audio_chunks = []
        
        if duration is None:
            # 利用可能なすべてのデータを読み取る
            audio_chunks = await self._read_all_available()
        else:
            # 指定された時間分のデータを読み取る
            audio_chunks = await self._read_duration(duration)
            
        if not audio_chunks:
            return None
            
        # チャンクを結合
        return np.concatenate(audio_chunks)
        
    @abstractmethod
    async def _read_all_available(self) -> list[np.ndarray]:
        """利用可能なすべてのデータを読み取る（実装固有）"""
        pass
        
    @abstractmethod
    async def _read_duration(self, duration: float) -> list[np.ndarray]:
        """指定時間分のデータを読み取る（実装固有）"""
        pass
        
    def get_sample_rate(self) -> int:
        """サンプリングレートを取得する"""
        return self.sample_rate
        
    def is_active(self) -> bool:
        """入力がアクティブかどうか"""
        return self._active
        
    def set_callback(self, callback: Optional[Callable[[np.ndarray], None]]) -> None:
        """音声データ受信時のコールバックを設定"""
        self._callback = callback
        
    def _invoke_callback(self, audio_data: np.ndarray) -> None:
        """コールバックを呼び出す（共通処理）"""
        if self._callback:
            self._callback(audio_data)