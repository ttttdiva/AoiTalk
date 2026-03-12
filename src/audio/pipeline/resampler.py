import numpy as np
from scipy import signal
from typing import Optional, Dict, Tuple
import hashlib


class AudioResampler:
    """高品質な音声リサンプリング（キャッシュ機能付き）"""
    
    def __init__(self, cache_size: int = 100):
        """リサンプラーを初期化
        
        Args:
            cache_size: キャッシュの最大サイズ
        """
        self._filter_cache: Dict[str, Tuple[int, int]] = {}  # フィルタ設定のキャッシュ
        self._result_cache: Dict[str, np.ndarray] = {}  # 結果のキャッシュ
        self._cache_size = cache_size
        
    def resample(self, audio_data: np.ndarray, 
                 original_rate: int, 
                 target_rate: int,
                 method: str = 'auto') -> np.ndarray:
        """音声データをリサンプリング
        
        Args:
            audio_data: 入力音声データ
            original_rate: 元のサンプリングレート
            target_rate: 目標サンプリングレート
            method: リサンプリング方法 ('auto', 'polyphase', 'fft', 'linear')
            
        Returns:
            リサンプリングされた音声データ
        """
        if original_rate == target_rate:
            return audio_data
            
        # 自動選択
        if method == 'auto':
            # レート比に基づいて最適な方法を選択
            ratio = max(original_rate, target_rate) / min(original_rate, target_rate)
            if ratio > 10:
                method = 'fft'  # 大きな変換比の場合
            elif len(audio_data) > 100000:
                method = 'linear'  # 長いデータの場合
            else:
                method = 'polyphase'  # 通常の場合
            
        if method == 'polyphase':
            return self._resample_polyphase(audio_data, original_rate, target_rate)
        elif method == 'fft':
            return self._resample_fft(audio_data, original_rate, target_rate)
        else:
            return self._resample_linear(audio_data, original_rate, target_rate)
            
    def _get_cache_key(self, audio_data: np.ndarray, 
                      original_rate: int, target_rate: int, 
                      method: str) -> str:
        """キャッシュキーを生成"""
        # データのハッシュを計算（最初の1000サンプルのみ）
        data_sample = audio_data[:min(1000, len(audio_data))]
        data_hash = hashlib.md5(data_sample.tobytes()).hexdigest()[:8]
        return f"{data_hash}_{original_rate}_{target_rate}_{method}_{len(audio_data)}"
        
    def _check_cache(self, key: str) -> Optional[np.ndarray]:
        """キャッシュをチェック"""
        if key in self._result_cache:
            return self._result_cache[key].copy()
        return None
        
    def _update_cache(self, key: str, result: np.ndarray) -> None:
        """キャッシュを更新"""
        # キャッシュサイズ制限
        if len(self._result_cache) >= self._cache_size:
            # 最も古いエントリを削除
            oldest_key = next(iter(self._result_cache))
            del self._result_cache[oldest_key]
            
        self._result_cache[key] = result.copy()
            
    def _resample_polyphase(self, audio_data: np.ndarray, 
                           original_rate: int, 
                           target_rate: int) -> np.ndarray:
        """ポリフェーズフィルタによるリサンプリング（高品質）"""
        # キャッシュチェック
        cache_key = self._get_cache_key(audio_data, original_rate, target_rate, 'polyphase')
        cached_result = self._check_cache(cache_key)
        if cached_result is not None:
            return cached_result
            
        try:
            # GCDを使用してアップ/ダウンサンプリング率を最適化
            from math import gcd
            g = gcd(original_rate, target_rate)
            up = target_rate // g
            down = original_rate // g
            
            # フィルタキーを作成
            filter_key = f"{up}_{down}"
            
            # scipy.signalのresampleを使用
            num_samples = int(len(audio_data) * target_rate / original_rate)
            resampled = signal.resample_poly(
                audio_data, 
                up=up, 
                down=down,
                axis=0,
                padtype='constant'
            )
            
            # 長さを調整
            if len(resampled) > num_samples:
                resampled = resampled[:num_samples]
                
            # キャッシュに保存
            self._update_cache(cache_key, resampled)
                
            return resampled
            
        except Exception as e:
            # エラーの場合はFFTリサンプリングにフォールバック
            print(f"Polyphase resampling failed: {e}, falling back to FFT")
            return self._resample_fft(audio_data, original_rate, target_rate)
            
    def _resample_fft(self, audio_data: np.ndarray, 
                      original_rate: int, 
                      target_rate: int) -> np.ndarray:
        """FFTベースのリサンプリング（中品質）"""
        # キャッシュチェック
        cache_key = self._get_cache_key(audio_data, original_rate, target_rate, 'fft')
        cached_result = self._check_cache(cache_key)
        if cached_result is not None:
            return cached_result
            
        try:
            # FFTリサンプリング
            num_samples = int(len(audio_data) * target_rate / original_rate)
            resampled = signal.resample(audio_data, num_samples)
            
            # キャッシュに保存
            self._update_cache(cache_key, resampled)
            
            return resampled
            
        except Exception as e:
            # エラーの場合は線形補間にフォールバック
            print(f"FFT resampling failed: {e}, falling back to linear")
            return self._resample_linear(audio_data, original_rate, target_rate)
            
    def _resample_linear(self, audio_data: np.ndarray, 
                        original_rate: int, 
                        target_rate: int) -> np.ndarray:
        """線形補間によるリサンプリング（低品質だが高速）"""
        # キャッシュチェック（短いデータのみ）
        if len(audio_data) < 10000:
            cache_key = self._get_cache_key(audio_data, original_rate, target_rate, 'linear')
            cached_result = self._check_cache(cache_key)
            if cached_result is not None:
                return cached_result
                
        ratio = target_rate / original_rate
        new_length = int(len(audio_data) * ratio)
        
        # 元のインデックス
        old_indices = np.arange(len(audio_data))
        # 新しいインデックス
        new_indices = np.linspace(0, len(audio_data) - 1, new_length)
        
        # 線形補間
        resampled = np.interp(new_indices, old_indices, audio_data)
        
        # キャッシュに保存（短いデータのみ）
        if len(audio_data) < 10000:
            self._update_cache(cache_key, resampled)
        
        return resampled
        
    def convert_stereo_to_mono(self, audio_data: np.ndarray) -> np.ndarray:
        """ステレオ音声をモノラルに変換
        
        Args:
            audio_data: ステレオ音声データ（shape: (samples, 2)）
            
        Returns:
            モノラル音声データ（shape: (samples,)）
        """
        if audio_data.ndim == 1:
            # すでにモノラル
            return audio_data
            
        if audio_data.ndim == 2:
            if audio_data.shape[1] == 2:
                # ステレオをモノラルに変換（平均）
                return np.mean(audio_data, axis=1)
            elif audio_data.shape[1] == 1:
                # チャンネル次元を削除
                return audio_data.squeeze()
                
        return audio_data
        
    def convert_mono_to_stereo(self, audio_data: np.ndarray) -> np.ndarray:
        """モノラル音声をステレオに変換
        
        Args:
            audio_data: モノラル音声データ（shape: (samples,)）
            
        Returns:
            ステレオ音声データ（shape: (samples, 2)）
        """
        if audio_data.ndim == 2 and audio_data.shape[1] == 2:
            # すでにステレオ
            return audio_data
            
        if audio_data.ndim == 1:
            # モノラルをステレオに変換（同じ音を両チャンネルに）
            return np.stack([audio_data, audio_data], axis=1)
            
        return audio_data
        
    def change_speed(self, audio_data: np.ndarray, 
                    speed_factor: float) -> np.ndarray:
        """音声の速度を変更（ピッチは維持）
        
        Args:
            audio_data: 入力音声データ
            speed_factor: 速度変更係数（1.0が元の速度、2.0が2倍速）
            
        Returns:
            速度変更された音声データ
        """
        if speed_factor == 1.0:
            return audio_data
            
        # 単純なリサンプリングで速度変更
        new_length = int(len(audio_data) / speed_factor)
        indices = np.linspace(0, len(audio_data) - 1, new_length)
        
        return np.interp(indices, np.arange(len(audio_data)), audio_data)