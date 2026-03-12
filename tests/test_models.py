"""Pydanticモデルのテスト"""
import pytest
from pydantic import ValidationError


class TestAudioModels:
    def test_recorder_config_defaults(self):
        from src.models.audio_models import RecorderConfig

        cfg = RecorderConfig()
        assert cfg.sample_rate == 16000
        assert cfg.channels == 1
        assert cfg.chunk_size == 1024

    def test_recorder_config_validation(self):
        from src.models.audio_models import RecorderConfig

        with pytest.raises(ValidationError):
            RecorderConfig(sample_rate=1000)  # 8000未満
        with pytest.raises(ValidationError):
            RecorderConfig(channels=5)  # 2チャンネル超

    def test_voice_config_defaults(self):
        from src.models.audio_models import VoiceConfig

        cfg = VoiceConfig()
        assert cfg.volume == 1.0
        assert cfg.sample_rate == 16000

    def test_voice_config_volume_range(self):
        from src.models.audio_models import VoiceConfig

        with pytest.raises(ValidationError):
            VoiceConfig(volume=-1.0)
        with pytest.raises(ValidationError):
            VoiceConfig(volume=3.0)

    def test_audio_config_composition(self):
        from src.models.audio_models import AudioConfig, RecorderConfig, VoiceConfig

        cfg = AudioConfig(
            recorder=RecorderConfig(sample_rate=44100),
            voice=VoiceConfig(volume=0.5),
        )
        assert cfg.recorder.sample_rate == 44100
        assert cfg.voice.volume == 0.5


class TestConfigModels:
    def test_base_config_defaults(self):
        from src.models.config_models import BaseConfig

        cfg = BaseConfig()
        assert cfg.mode == "terminal"
        assert cfg.default_character == "ずんだもん"
        assert cfg.debug is False

    def test_base_config_invalid_mode(self):
        from src.models.config_models import BaseConfig

        with pytest.raises(ValidationError):
            BaseConfig(mode="invalid_mode")

    def test_base_config_valid_modes(self):
        from src.models.config_models import BaseConfig

        for mode in ("terminal", "voice_chat", "discord"):
            cfg = BaseConfig(mode=mode)
            assert cfg.mode == mode

    def test_llm_config(self):
        from src.models.config_models import LLMConfig

        cfg = LLMConfig(engine="gemini", model="gemini-3-flash-preview", temperature=0.5)
        assert cfg.temperature == 0.5
        assert cfg.max_tokens == 4096

    def test_llm_config_temperature_range(self):
        from src.models.config_models import LLMConfig

        with pytest.raises(ValidationError):
            LLMConfig(temperature=-0.1)
        with pytest.raises(ValidationError):
            LLMConfig(temperature=2.5)

    def test_tts_config(self):
        from src.models.config_models import TTSConfig

        cfg = TTSConfig(engine="voicevox", speed=1.5)
        assert cfg.speed == 1.5
        assert cfg.enabled is True

    def test_tts_config_speed_range(self):
        from src.models.config_models import TTSConfig

        with pytest.raises(ValidationError):
            TTSConfig(speed=0.0)
        with pytest.raises(ValidationError):
            TTSConfig(speed=5.0)

    def test_speech_recognition_config(self):
        from src.models.config_models import SpeechRecognitionConfig

        cfg = SpeechRecognitionConfig(engine="whisper", model="large")
        assert cfg.language == "ja"
        assert cfg.hallucination_detection is True


class TestMessageModels:
    def test_user_message(self):
        from src.models.message_models import UserMessage

        msg = UserMessage(content="こんにちは", user_id="user123")
        assert msg.type == "user"
        assert msg.content == "こんにちは"
        assert msg.user_id == "user123"

    def test_assistant_message(self):
        from src.models.message_models import AssistantMessage

        msg = AssistantMessage(content="はいなのだ！", character="ずんだもん")
        assert msg.type == "assistant"
        assert msg.character == "ずんだもん"

    def test_assistant_confidence_range(self):
        from src.models.message_models import AssistantMessage

        with pytest.raises(ValidationError):
            AssistantMessage(content="test", confidence=1.5)
        with pytest.raises(ValidationError):
            AssistantMessage(content="test", confidence=-0.1)

    def test_system_message_valid_levels(self):
        from src.models.message_models import SystemMessage

        for level in ("debug", "info", "warning", "error"):
            msg = SystemMessage(content="test", level=level)
            assert msg.level == level

    def test_system_message_invalid_level(self):
        from src.models.message_models import SystemMessage

        with pytest.raises(ValidationError):
            SystemMessage(content="test", level="critical")

    def test_chat_message_valid_types(self):
        from src.models.message_models import ChatMessage

        for t in ("user", "assistant", "system"):
            msg = ChatMessage(type=t, message="test")
            assert msg.type == t

    def test_chat_message_invalid_type(self):
        from src.models.message_models import ChatMessage

        with pytest.raises(ValidationError):
            ChatMessage(type="bot", message="test")

    def test_voice_status(self):
        from src.models.message_models import VoiceStatus

        vs = VoiceStatus(ready=True, rms=0.05, recording=True)
        assert vs.ready is True
        assert vs.recording is True

    def test_voice_status_rms_non_negative(self):
        from src.models.message_models import VoiceStatus

        with pytest.raises(ValidationError):
            VoiceStatus(ready=True, rms=-1.0)
