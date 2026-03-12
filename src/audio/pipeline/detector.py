import numpy as np
from typing import Tuple, Optional
from collections import deque


class VoiceActivityDetector:
    """音声アクティビティ検出器"""
    
    def __init__(self, 
                 sample_rate: int = 16000,
                 frame_duration_ms: int = 20,
                 silence_threshold: float = 0.01,
                 speech_threshold: float = 0.02,
                 silence_duration: float = 1.0,
                 speech_duration: float = 0.3):
        """VADを初期化
        
        Args:
            sample_rate: サンプリングレート
            frame_duration_ms: フレーム長（ミリ秒）
            silence_threshold: 無音判定の閾値
            speech_threshold: 音声判定の閾値
            silence_duration: 無音継続時間（秒）
            speech_duration: 音声継続時間（秒）
        """
        self.sample_rate = sample_rate
        self.frame_duration_ms = frame_duration_ms
        self.frame_size = int(sample_rate * frame_duration_ms / 1000)
        
        self.silence_threshold = silence_threshold
        self.speech_threshold = speech_threshold
        
        # 必要なフレーム数
        self.silence_frames_required = int(silence_duration * 1000 / frame_duration_ms)
        self.speech_frames_required = int(speech_duration * 1000 / frame_duration_ms)
        
        # 状態管理
        self.is_speaking = False
        self.silence_frame_count = 0
        self.speech_frame_count = 0
        
        # エネルギー履歴（適応的閾値用）
        self.energy_history = deque(maxlen=50)
        self.use_adaptive_threshold = True
        
    def process_frame(self, frame: np.ndarray) -> Tuple[bool, float]:
        """単一フレームを処理
        
        Args:
            frame: 音声フレーム
            
        Returns:
            (is_speech, energy) 音声かどうかとエネルギー値
        """
        # フレームのエネルギー（RMS）を計算
        energy = np.sqrt(np.mean(frame**2))
        
        # エネルギー履歴に追加
        self.energy_history.append(energy)
        
        # 閾値を決定
        threshold = self._get_threshold()
        
        # 音声判定
        is_speech = energy > threshold
        
        return is_speech, energy
        
    def process_audio(self, audio_data: np.ndarray) -> Tuple[bool, int, int]:
        """音声データ全体を処理して音声区間を検出
        
        Args:
            audio_data: 音声データ
            
        Returns:
            (has_speech, start_idx, end_idx) 音声の有無と区間
        """
        has_speech = False
        start_idx = -1
        end_idx = -1
        
        # フレーム単位で処理
        for i in range(0, len(audio_data) - self.frame_size, self.frame_size):
            frame = audio_data[i:i + self.frame_size]
            is_speech, energy = self.process_frame(frame)
            
            if is_speech:
                self.speech_frame_count += 1
                self.silence_frame_count = 0
                
                # 音声開始
                if not self.is_speaking and self.speech_frame_count >= self.speech_frames_required:
                    self.is_speaking = True
                    if start_idx == -1:
                        # 少し前から開始（プリバッファ）
                        start_idx = max(0, i - self.frame_size * 2)
                    has_speech = True
                    
            else:
                self.silence_frame_count += 1
                self.speech_frame_count = 0
                
                # 音声終了
                if self.is_speaking and self.silence_frame_count >= self.silence_frames_required:
                    self.is_speaking = False
                    # 少し後まで含める（ポストバッファ）
                    end_idx = min(len(audio_data), i + self.frame_size * 2)
                    break
                    
        # 最後まで音声が続いている場合
        if self.is_speaking and end_idx == -1:
            end_idx = len(audio_data)
            
        return has_speech, start_idx, end_idx
        
    def _get_threshold(self) -> float:
        """適応的な閾値を取得"""
        if not self.use_adaptive_threshold or len(self.energy_history) < 10:
            return self.speech_threshold
            
        # エネルギー履歴の統計値を計算
        energies = np.array(self.energy_history)
        mean_energy = np.mean(energies)
        std_energy = np.std(energies)
        
        # 適応的閾値（平均 + 1.5標準偏差）
        adaptive_threshold = mean_energy + 1.5 * std_energy
        
        # 固定閾値との間を取る
        return max(self.speech_threshold, min(adaptive_threshold, self.speech_threshold * 3))
        
    def reset(self):
        """検出器の状態をリセット"""
        self.is_speaking = False
        self.silence_frame_count = 0
        self.speech_frame_count = 0
        self.energy_history.clear()
        
    def is_voice_active(self) -> bool:
        """現在音声がアクティブかどうか"""
        return self.is_speaking
        
    def get_recommended_buffer_size(self) -> int:
        """推奨バッファサイズを取得（サンプル数）"""
        # 最大音声長 + プリ/ポストバッファ
        return int(self.sample_rate * 32)  # 32秒