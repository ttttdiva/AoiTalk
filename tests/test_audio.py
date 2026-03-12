"""audioモジュールのテスト（外部サービス不要）"""
import numpy as np
import pytest


class TestHallucinationFilter:
    def test_empty_text_is_hallucination(self):
        from src.audio.hallucination_filter import HallucinationFilter

        f = HallucinationFilter()
        assert f.is_hallucination("") is True
        assert f.is_hallucination("   ") is True
        assert f.is_hallucination(None) is True

    def test_normal_text_not_hallucination(self):
        from src.audio.hallucination_filter import HallucinationFilter

        f = HallucinationFilter()
        assert f.is_hallucination("こんにちは、今日はいい天気ですね") is False

    def test_whisper_patterns_detected(self):
        from src.audio.hallucination_filter import HallucinationFilter

        f = HallucinationFilter()
        assert f.is_hallucination("ご視聴ありがとうございました", engine="whisper") is True
        assert f.is_hallucination("チャンネル登録お願いします", engine="whisper") is True
        assert f.is_hallucination("[音楽]", engine="whisper") is True
        assert f.is_hallucination("thank you for watching", engine="whisper") is True

    def test_whisper_patterns_not_detected_without_engine(self):
        from src.audio.hallucination_filter import HallucinationFilter

        f = HallucinationFilter()
        # エンジン指定なしではwhisperパターンは検出されない
        assert f.is_hallucination("ご視聴ありがとうございました") is False

    def test_repetitive_hallucination(self):
        from src.audio.hallucination_filter import HallucinationFilter

        f = HallucinationFilter()
        repeated = "テスト。テスト。テスト。テスト。"
        assert f.is_hallucination(repeated) is True

    def test_consecutive_repetition(self):
        from src.audio.hallucination_filter import HallucinationFilter

        f = HallucinationFilter()
        text = "ええ、ええ、ええ"
        assert f.is_hallucination(text) is True

    def test_non_repetitive_text(self):
        from src.audio.hallucination_filter import HallucinationFilter

        f = HallucinationFilter()
        text = "今日は天気がいいね。明日は雨かもしれない。"
        assert f.is_hallucination(text) is False

    def test_custom_max_repetitions(self):
        from src.audio.hallucination_filter import HallucinationFilter

        f = HallucinationFilter(config={"max_repetitions": 5})
        # 3回繰り返しはmax_repetitions=5では検出されない
        text = "テスト、テスト、テスト"
        assert f.is_hallucination(text) is False


class TestAudioResampler:
    def test_same_rate_returns_original(self):
        from src.audio.pipeline.resampler import AudioResampler

        rs = AudioResampler()
        data = np.array([1.0, 2.0, 3.0])
        result = rs.resample(data, 16000, 16000)
        np.testing.assert_array_equal(result, data)

    def test_resample_changes_length(self):
        from src.audio.pipeline.resampler import AudioResampler

        rs = AudioResampler()
        data = np.random.randn(16000)  # 1秒分の16kHz音声
        result = rs.resample(data, 16000, 8000, method="linear")
        # 8000Hzにダウンサンプリング→長さは約半分
        assert abs(len(result) - 8000) < 10

    def test_resample_upsample(self):
        from src.audio.pipeline.resampler import AudioResampler

        rs = AudioResampler()
        data = np.random.randn(8000)
        result = rs.resample(data, 8000, 16000, method="linear")
        assert abs(len(result) - 16000) < 10

    def test_convert_stereo_to_mono(self):
        from src.audio.pipeline.resampler import AudioResampler

        rs = AudioResampler()
        stereo = np.array([[1.0, 3.0], [2.0, 4.0], [3.0, 5.0]])
        mono = rs.convert_stereo_to_mono(stereo)
        assert mono.ndim == 1
        assert len(mono) == 3
        np.testing.assert_array_almost_equal(mono, [2.0, 3.0, 4.0])

    def test_convert_stereo_to_mono_already_mono(self):
        from src.audio.pipeline.resampler import AudioResampler

        rs = AudioResampler()
        mono = np.array([1.0, 2.0, 3.0])
        result = rs.convert_stereo_to_mono(mono)
        np.testing.assert_array_equal(result, mono)

    def test_convert_mono_to_stereo(self):
        from src.audio.pipeline.resampler import AudioResampler

        rs = AudioResampler()
        mono = np.array([1.0, 2.0, 3.0])
        stereo = rs.convert_mono_to_stereo(mono)
        assert stereo.shape == (3, 2)
        np.testing.assert_array_equal(stereo[:, 0], mono)
        np.testing.assert_array_equal(stereo[:, 1], mono)

    def test_convert_mono_to_stereo_already_stereo(self):
        from src.audio.pipeline.resampler import AudioResampler

        rs = AudioResampler()
        stereo = np.array([[1.0, 2.0], [3.0, 4.0]])
        result = rs.convert_mono_to_stereo(stereo)
        np.testing.assert_array_equal(result, stereo)

    def test_change_speed_no_change(self):
        from src.audio.pipeline.resampler import AudioResampler

        rs = AudioResampler()
        data = np.array([1.0, 2.0, 3.0, 4.0])
        result = rs.change_speed(data, 1.0)
        np.testing.assert_array_equal(result, data)

    def test_change_speed_double(self):
        from src.audio.pipeline.resampler import AudioResampler

        rs = AudioResampler()
        data = np.random.randn(1000)
        result = rs.change_speed(data, 2.0)
        assert abs(len(result) - 500) < 5

    def test_change_speed_half(self):
        from src.audio.pipeline.resampler import AudioResampler

        rs = AudioResampler()
        data = np.random.randn(1000)
        result = rs.change_speed(data, 0.5)
        assert abs(len(result) - 2000) < 5


class TestVoiceActivityDetector:
    def test_silent_frame(self):
        from src.audio.pipeline.detector import VoiceActivityDetector

        vad = VoiceActivityDetector(sample_rate=16000, speech_threshold=0.02)
        silent_frame = np.zeros(320)  # 20ms分
        is_speech, energy = vad.process_frame(silent_frame)
        assert bool(is_speech) is False
        assert energy == 0.0

    def test_loud_frame(self):
        from src.audio.pipeline.detector import VoiceActivityDetector

        vad = VoiceActivityDetector(sample_rate=16000, speech_threshold=0.02)
        loud_frame = np.ones(320) * 0.5
        is_speech, energy = vad.process_frame(loud_frame)
        assert bool(is_speech) is True
        assert energy > 0

    def test_reset(self):
        from src.audio.pipeline.detector import VoiceActivityDetector

        vad = VoiceActivityDetector()
        vad.is_speaking = True
        vad.silence_frame_count = 10
        vad.speech_frame_count = 5
        vad.energy_history.append(0.1)

        vad.reset()
        assert vad.is_speaking is False
        assert vad.silence_frame_count == 0
        assert vad.speech_frame_count == 0
        assert len(vad.energy_history) == 0

    def test_get_recommended_buffer_size(self):
        from src.audio.pipeline.detector import VoiceActivityDetector

        vad = VoiceActivityDetector(sample_rate=16000)
        size = vad.get_recommended_buffer_size()
        assert size == 16000 * 32  # 32秒分

    def test_adaptive_threshold_with_history(self):
        from src.audio.pipeline.detector import VoiceActivityDetector

        vad = VoiceActivityDetector(sample_rate=16000, speech_threshold=0.02)
        # 低エネルギーの履歴を入れる
        for _ in range(20):
            vad.energy_history.append(0.001)
        threshold = vad._get_threshold()
        # 適応的閾値は固定閾値と平均+1.5σの間
        assert threshold >= vad.speech_threshold

    def test_threshold_falls_back_with_short_history(self):
        from src.audio.pipeline.detector import VoiceActivityDetector

        vad = VoiceActivityDetector(sample_rate=16000, speech_threshold=0.05)
        # 履歴が少ない場合は固定閾値
        for _ in range(3):
            vad.energy_history.append(0.1)
        threshold = vad._get_threshold()
        assert threshold == vad.speech_threshold

    def test_process_audio_detects_speech(self):
        from src.audio.pipeline.detector import VoiceActivityDetector

        vad = VoiceActivityDetector(
            sample_rate=16000,
            speech_threshold=0.01,
            speech_duration=0.05,
            silence_duration=0.1,
        )
        # 無音 + 音声 + 無音
        silence = np.zeros(8000)
        speech = np.random.randn(8000) * 0.5
        audio = np.concatenate([silence, speech, silence])
        has_speech, start_idx, end_idx = vad.process_audio(audio)
        assert has_speech is True
        assert start_idx >= 0
        assert end_idx > start_idx
