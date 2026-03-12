import numpy as np
from typing import Optional, Tuple, Dict
from ..interfaces.processor import AudioProcessorInterface


class AudioProcessor(AudioProcessorInterface):
    """共通音声処理の実装
    
    Note: リサンプリング機能は AudioResampler を使用してください。
    音声検出とボリューム計算は UnifiedAudioPipeline で統合されています。
    """
    
    def __init__(self):
        """音声処理を初期化"""
        # ノイズ除去用のカーネルキャッシュ
        self._kernel_cache: Dict[int, np.ndarray] = {}
        
    def process(self, audio_data: np.ndarray, sample_rate: int) -> np.ndarray:
        """音声データを処理する
        
        Args:
            audio_data: 入力音声データ
            sample_rate: サンプリングレート
            
        Returns:
            処理済み音声データ
        """
        # 正規化
        audio_data = self.normalize(audio_data)
        
        # 簡易ノイズ除去
        audio_data = self.remove_noise(audio_data, sample_rate)
        
        return audio_data
        
    def detect_voice(self, audio_data: np.ndarray) -> bool:
        """音声が含まれているか検出する
        
        DEPRECATED: UnifiedAudioPipeline を使用してください。
        この機能は後方互換性のために残されています。
        """
        rms = np.sqrt(np.mean(audio_data**2))
        voice_threshold = 0.01  # -40dB相当
        return rms > voice_threshold
        
    def calculate_volume(self, audio_data: np.ndarray) -> float:
        """音量を計算する（0.0 ~ 1.0）
        
        DEPRECATED: UnifiedAudioPipeline を使用してください。
        この機能は後方互換性のために残されています。
        """
        rms = np.sqrt(np.mean(audio_data**2))
        return float(np.clip(rms, 0.0, 1.0))
        
    def resample(self, audio_data: np.ndarray, 
                 original_rate: int, target_rate: int) -> np.ndarray:
        """サンプリングレートを変換する
        
        DEPRECATED: AudioResampler クラスを使用してください。
        この機能は後方互換性のために残されています。
        """
        from .resampler import AudioResampler
        resampler = AudioResampler()
        return resampler.resample(audio_data, original_rate, target_rate)
        
    def normalize(self, audio_data: np.ndarray) -> np.ndarray:
        """音声データを正規化する（-1.0 ~ 1.0）"""
        # すでに正規化されている場合はそのまま返す
        if audio_data.dtype == np.float32 or audio_data.dtype == np.float64:
            return np.clip(audio_data, -1.0, 1.0)
            
        # int16の場合は変換
        if audio_data.dtype == np.int16:
            return audio_data.astype(np.float32) / 32768.0
            
        # その他の場合は最大値で正規化
        max_val = np.abs(audio_data).max()
        if max_val > 0:
            return audio_data.astype(np.float32) / max_val
        else:
            return audio_data.astype(np.float32)
            
    def remove_noise(self, audio_data: np.ndarray, 
                     noise_level: float = 0.01) -> np.ndarray:
        """ノイズを除去する（最適化版）"""
        # ノイズレベル以下の値を減衰
        mask = np.abs(audio_data) > noise_level
        
        # ソフトな閾値処理
        result = audio_data.copy()
        result[~mask] *= 0.1  # ノイズレベル以下を減衰
        
        # スムージング（オプション）
        window_size = 3
        if window_size in self._kernel_cache:
            kernel = self._kernel_cache[window_size]
        else:
            kernel = np.ones(window_size) / window_size
            self._kernel_cache[window_size] = kernel
            
        if len(result) >= window_size:
            result = np.convolve(result, kernel, mode='same')
            
        return result
        
    def detect_silence(self, audio_data: np.ndarray, 
                      sample_rate: int,
                      threshold: float = 0.01,
                      duration: float = 0.5) -> Tuple[int, int]:
        """無音区間を検出する
        
        Args:
            audio_data: 音声データ
            sample_rate: サンプリングレート
            threshold: 無音判定の閾値
            duration: 最小無音時間（秒）
            
        Returns:
            (開始インデックス, 終了インデックス) 無音区間が見つからない場合は(-1, -1)
        """
        # フレームサイズ（10ms）
        frame_size = int(sample_rate * 0.01)
        min_silence_frames = int(duration / 0.01)
        
        silence_start = -1
        silence_frames = 0
        
        for i in range(0, len(audio_data) - frame_size, frame_size):
            frame = audio_data[i:i + frame_size]
            frame_rms = np.sqrt(np.mean(frame**2))
            
            if frame_rms < threshold:
                if silence_start == -1:
                    silence_start = i
                silence_frames += 1
                
                if silence_frames >= min_silence_frames:
                    return (silence_start, i + frame_size)
            else:
                silence_start = -1
                silence_frames = 0
                
        return (-1, -1)