import asyncio
import io
import wave
import pyaudio
import numpy as np
import threading
from typing import Optional, Union
from ..base_output import BaseAudioOutput


class SpeakerOutput(BaseAudioOutput):
    """ローカルスピーカー出力の実装"""
    
    def __init__(self, 
                 device_index: Optional[int] = None,
                 sample_rate: int = 24000,
                 channels: int = 1,
                 chunk_size: int = 1024):
        """スピーカー出力を初期化
        
        Args:
            device_index: 音声出力デバイスのインデックス
            sample_rate: デフォルトのサンプリングレート（Hz）
            channels: チャンネル数
            chunk_size: チャンクサイズ
        """
        # 基底クラスの初期化
        super().__init__(sample_rate, channels)
        
        self.device_index = device_index
        self.chunk_size = chunk_size
        
        self.audio = pyaudio.PyAudio()
        self.stream = None
        self._should_stop = False
        
        # スレッドセーフティ
        self.lock = threading.RLock()
        self._play_thread: Optional[threading.Thread] = None
        
    async def _prepare_audio_data(self, audio_data: Union[bytes, np.ndarray], 
                                  format: str, sample_rate: Optional[int]) -> Union[bytes, np.ndarray]:
        """音声データを再生用に準備する"""
        if isinstance(audio_data, np.ndarray):
            # numpy配列の場合はWAVに変換
            return self._numpy_to_wav(audio_data, sample_rate or self.default_sample_rate)
        return audio_data
        
    async def _play_internal(self, audio_data: Union[bytes, np.ndarray], 
                            format: str, sample_rate: int) -> None:
        """実際の再生処理"""
        self._should_stop = False
        # 非同期で再生
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._play_sync, audio_data)
        
    def _play_sync(self, audio_data: bytes):
        """同期的に音声を再生"""
        audio_io = io.BytesIO(audio_data)
        
        try:
            with wave.open(audio_io, 'rb') as wav_file:
                # WAVパラメータを取得
                frames = wav_file.getnframes()
                sample_rate = wav_file.getframerate()
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                
                # PyAudioストリームを開く
                with self.lock:
                    if self.stream:
                        self.stream.stop_stream()
                        self.stream.close()
                        
                    # PyAudioフォーマットを取得
                    pa_format = self.audio.get_format_from_width(sample_width)
                    
                    self.stream = self.audio.open(
                        format=pa_format,
                        channels=channels,
                        rate=sample_rate,
                        output=True,
                        output_device_index=self.device_index,
                        frames_per_buffer=self.chunk_size
                    )
                
                # 音声を再生
                wav_file.rewind()
                data = wav_file.readframes(self.chunk_size)
                
                while data and not self._should_stop:
                    if not self._paused:
                        # 音量調整
                        if self._volume != 1.0:
                            audio_array = np.frombuffer(data, dtype=np.int16)
                            audio_array = self._apply_volume_to_array(audio_array)
                            data = audio_array.tobytes()
                            
                        self.stream.write(data)
                        data = wav_file.readframes(self.chunk_size)
                    else:
                        # 一時停止中
                        asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.1))
                        
        except Exception as e:
            print(f"[SpeakerOutput] Playback error: {e}")
        finally:
            with self.lock:
                if self.stream:
                    self.stream.stop_stream()
                    self.stream.close()
                    self.stream = None
                self._playing = False
                
    async def _stop_internal(self) -> None:
        """実際の停止処理"""
        self._should_stop = True
        
        # ストリームをクローズ
        with self.lock:
            if self.stream:
                try:
                    self.stream.stop_stream()
                    self.stream.close()
                except:
                    pass
                self.stream = None
                
    async def _pause_internal(self) -> None:
        """実際の一時停止処理"""
        # ストリームベースの再生では特別な処理不要
        pass
        
    async def _resume_internal(self) -> None:
        """実際の再開処理"""
        # ストリームベースの再生では特別な処理不要
        pass
        
    def _apply_volume_change(self) -> None:
        """音量変更を適用する"""
        # リアルタイムで音量調整を行うため特別な処理不要
        pass
        
    def _numpy_to_wav(self, audio_data: np.ndarray, sample_rate: int) -> bytes:
        """numpy配列をWAVバイナリに変換"""
        # 基底クラスのメソッドを使用して変換
        audio_bytes = self._convert_numpy_to_bytes(audio_data, np.int16)
            
        # WAVファイルを作成
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(2)  # 16ビット
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_bytes)
            
        buffer.seek(0)
        return buffer.read()
        
    def __del__(self):
        """デストラクタ"""
        if hasattr(self, 'audio'):
            self.audio.terminate()