import asyncio
import pyaudio
import numpy as np
import queue
import threading
from typing import Optional, Dict, Any, Union
from ..base_input import BaseAudioInput


class MicrophoneInput(BaseAudioInput):
    """ローカルマイク入力の実装"""
    
    def __init__(self, 
                 device_index: Optional[int] = None,
                 sample_rate: int = 16000,
                 chunk_size: int = 1024,
                 channels: int = 1):
        """マイク入力を初期化
        
        Args:
            device_index: 音声入力デバイスのインデックス
            sample_rate: サンプリングレート（Hz）
            chunk_size: バッファあたりのフレーム数
            channels: オーディオチャンネル数
        """
        # 基底クラスの初期化
        super().__init__(sample_rate, channels)
        
        self.device_index = device_index
        self.chunk_size = chunk_size
        
        self.audio = pyaudio.PyAudio()
        self.stream = None
        self._record_thread: Optional[threading.Thread] = None
        
    def _create_queue(self) -> Union[asyncio.Queue, queue.Queue]:
        """キューの作成（通常のQueueを使用）"""
        return queue.Queue()
        
    async def _initialize_resources(self) -> None:
        """リソースの初期化"""
        # PyAudioストリームを開く
        self.stream = self.audio.open(
            format=pyaudio.paFloat32,
            channels=self.channels,
            rate=self.sample_rate,
            input=True,
            input_device_index=self.device_index,
            frames_per_buffer=self.chunk_size
        )
        
        # 録音スレッドを開始
        self._record_thread = threading.Thread(target=self._record_worker)
        self._record_thread.daemon = True
        self._record_thread.start()
        
    async def _cleanup_resources(self) -> None:
        """リソースのクリーンアップ"""
        if self._record_thread:
            self._record_thread.join(timeout=1.0)
            
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
                
    async def _read_all_available(self) -> list[np.ndarray]:
        """利用可能なすべてのデータを読み取る"""
        audio_chunks = []
        while not self._audio_queue.empty():
            try:
                chunk = self._audio_queue.get_nowait()
                audio_chunks.append(chunk)
            except queue.Empty:
                break
        return audio_chunks
        
    async def _read_duration(self, duration: float) -> list[np.ndarray]:
        """指定時間分のデータを読み取る"""
        audio_chunks = []
        samples_needed = int(duration * self.sample_rate)
        samples_read = 0
        
        # タイムアウトを設定
        timeout = duration + 1.0
        start_time = asyncio.get_event_loop().time()
        
        while samples_read < samples_needed:
            # タイムアウトチェック
            if asyncio.get_event_loop().time() - start_time > timeout:
                break
                
            try:
                # 非同期でキューから取得
                chunk = await asyncio.get_event_loop().run_in_executor(
                    None, self._audio_queue.get, True, 0.1
                )
                audio_chunks.append(chunk)
                samples_read += len(chunk)
            except queue.Empty:
                await asyncio.sleep(0.01)
                
        return audio_chunks
        
    @property
    def metadata(self) -> Dict[str, Any]:
        """入力ソースのメタデータ"""
        device_info = {}
        if self.device_index is not None:
            try:
                info = self.audio.get_device_info_by_index(self.device_index)
                device_info = {
                    'name': info.get('name', 'Unknown'),
                    'max_channels': info.get('maxInputChannels', 0),
                    'default_sample_rate': info.get('defaultSampleRate', 0)
                }
            except:
                pass
                
        return {
            'type': 'microphone',
            'device_index': self.device_index,
            'sample_rate': self.sample_rate,
            'channels': self.channels,
            'chunk_size': self.chunk_size,
            'device_info': device_info
        }
        
    def _record_worker(self) -> None:
        """録音ワーカースレッド"""
        while self._active and self.stream:
            try:
                # 音声データを読み取る
                data = self.stream.read(self.chunk_size, exception_on_overflow=False)
                
                # float32に変換（-1.0 ~ 1.0）
                audio_array = np.frombuffer(data, dtype=np.float32)
                
                # キューに追加
                self._audio_queue.put(audio_array)
                
                # コールバックを呼び出す
                self._invoke_callback(audio_array)
                    
            except Exception as e:
                if self._active:
                    print(f"Recording error: {e}")
                    
    def __del__(self):
        """デストラクタ"""
        if hasattr(self, 'audio'):
            self.audio.terminate()