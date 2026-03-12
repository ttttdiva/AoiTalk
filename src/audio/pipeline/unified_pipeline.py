"""統合音声処理パイプライン

重複処理を排除し、最適化された音声処理フローを提供
"""
import numpy as np
from typing import Dict, Any, Optional, Tuple
from .processor import AudioProcessor
from .detector import VoiceActivityDetector
from .resampler import AudioResampler
from ..interfaces.processor import AudioProcessorInterface


class UnifiedAudioPipeline:
    """統合された音声処理パイプライン
    
    AudioProcessor、VoiceActivityDetector、AudioResamplerの機能を
    統合し、重複処理を排除した効率的な処理を提供
    """
    
    def __init__(self):
        """統合パイプラインを初期化"""
        self.processor = AudioProcessor()
        self.detector = VoiceActivityDetector()
        self.resampler = AudioResampler()
        
        # RMS計算のキャッシュ
        self._rms_cache: Dict[int, float] = {}
        
        # バッファプール（メモリ効率化）
        self._buffer_pool: Dict[int, list[np.ndarray]] = {}
        
    def process_pipeline(self, 
                        audio_data: np.ndarray, 
                        config: Dict[str, Any]) -> Dict[str, Any]:
        """音声データを統合パイプラインで処理
        
        Args:
            audio_data: 入力音声データ
            config: 処理設定
                - voice_threshold: 音声検出閾値
                - original_rate: 元のサンプリングレート
                - target_rate: 目標サンプリングレート（オプション）
                - remove_noise: ノイズ除去を行うか（オプション）
                
        Returns:
            処理結果の辞書:
                - audio: 処理済み音声データ
                - is_voice: 音声が検出されたか
                - volume: 音量レベル（0.0-1.0）
                - rms: RMS値
                - vad_segments: 音声区間情報（オプション）
        """
        # 1. 正規化（一度だけ実行）
        normalized = self.normalize(audio_data)
        
        # 2. RMS計算（一度だけ実行、キャッシュ活用）
        data_hash = hash(normalized.tobytes())
        if data_hash in self._rms_cache:
            rms = self._rms_cache[data_hash]
        else:
            rms = float(np.sqrt(np.mean(normalized**2)))
            self._rms_cache[data_hash] = rms
            
        # キャッシュサイズ制限
        if len(self._rms_cache) > 1000:
            self._rms_cache.clear()
            
        # 3. 音声検出と音量計算（RMSを再利用）
        voice_threshold = config.get('voice_threshold', 0.01)
        is_voice = rms > voice_threshold
        volume = float(np.clip(rms, 0.0, 1.0))
        
        # 4. ノイズ除去（オプション）
        if config.get('remove_noise', False):
            normalized = self.processor.remove_noise(normalized)
            
        # 5. リサンプリング（必要な場合のみ）
        processed = normalized
        if 'target_rate' in config and 'original_rate' in config:
            if config['target_rate'] != config['original_rate']:
                processed = self.resampler.resample(
                    normalized,
                    config['original_rate'],
                    config['target_rate']
                )
                
        # 6. VAD処理（オプション）
        vad_segments = None
        if config.get('detect_segments', False):
            self.detector.reset()
            segments = []
            
            # フレーム単位で処理
            frame_size = int(config['original_rate'] * 0.02)  # 20ms
            for i in range(0, len(normalized), frame_size):
                frame = normalized[i:i+frame_size]
                if len(frame) == frame_size:
                    is_speech, energy = self.detector.process_frame(frame)
                    if is_speech:
                        # フレームインデックスを時間に変換
                        segments.append((i / config['original_rate'], (i + frame_size) / config['original_rate']))
                        
            vad_segments = segments
            
        return {
            'audio': processed,
            'is_voice': is_voice,
            'volume': volume,
            'rms': rms,
            'vad_segments': vad_segments
        }
        
    def normalize(self, audio_data: np.ndarray, inplace: bool = False) -> np.ndarray:
        """音声データを正規化（-1.0 ~ 1.0）
        
        Args:
            audio_data: 入力音声データ
            inplace: インプレース処理を行うか
            
        Returns:
            正規化された音声データ
        """
        if not inplace:
            audio_data = audio_data.copy()
            
        # インプレース正規化
        np.clip(audio_data, -1.0, 1.0, out=audio_data)
        return audio_data
        
    def detect_voice(self, audio_data: np.ndarray) -> bool:
        """音声検出（RMSベース）
        
        Args:
            audio_data: 音声データ
            
        Returns:
            音声が検出されたか
        """
        rms = np.sqrt(np.mean(audio_data**2))
        return rms > 0.01
        
    def calculate_volume(self, audio_data: np.ndarray) -> float:
        """音量レベルを計算
        
        Args:
            audio_data: 音声データ
            
        Returns:
            音量レベル（0.0 ~ 1.0）
        """
        rms = np.sqrt(np.mean(audio_data**2))
        return float(np.clip(rms, 0.0, 1.0))
        
    def remove_noise(self, audio_data: np.ndarray, noise_level: float = 0.01) -> np.ndarray:
        """基本的なノイズ除去
        
        Args:
            audio_data: 音声データ
            noise_level: ノイズレベル閾値
            
        Returns:
            ノイズ除去後の音声データ
        """
        return self.processor.remove_noise(audio_data, noise_level)
        
    def resample(self, audio_data: np.ndarray, 
                 original_rate: int, target_rate: int) -> np.ndarray:
        """サンプリングレートを変換
        
        Args:
            audio_data: 音声データ
            original_rate: 元のサンプリングレート
            target_rate: 目標サンプリングレート
            
        Returns:
            リサンプリングされた音声データ
        """
        return self.resampler.resample(audio_data, original_rate, target_rate)
        
    def get_buffer(self, size: int) -> np.ndarray:
        """バッファプールからバッファを取得
        
        Args:
            size: バッファサイズ
            
        Returns:
            numpy配列バッファ
        """
        if size not in self._buffer_pool:
            self._buffer_pool[size] = []
            
        pool = self._buffer_pool[size]
        if pool:
            return pool.pop()
        else:
            return np.zeros(size, dtype=np.float32)
            
    def release_buffer(self, buffer: np.ndarray) -> None:
        """バッファをプールに返却
        
        Args:
            buffer: 返却するバッファ
        """
        size = len(buffer)
        if size in self._buffer_pool:
            # バッファをクリア
            buffer.fill(0)
            self._buffer_pool[size].append(buffer)
            
            # プールサイズ制限
            if len(self._buffer_pool[size]) > 10:
                self._buffer_pool[size] = self._buffer_pool[size][-10:]