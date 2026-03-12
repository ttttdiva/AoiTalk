"""utilsモジュールのテスト"""
import pytest


class TestAppSession:
    def test_set_and_get_session_id(self):
        from src.utils import app_session

        app_session.set_session_id("20260212_120000")
        assert app_session.get_session_id() == "20260212_120000"

    def test_initial_session_id_is_overridable(self):
        from src.utils import app_session

        app_session.set_session_id("first")
        app_session.set_session_id("second")
        assert app_session.get_session_id() == "second"


class TestExceptions:
    def test_base_exception_with_code(self):
        from src.utils.exceptions import AoiTalkException

        e = AoiTalkException("test error", code="TEST")
        assert str(e) == "[TEST] test error"
        assert e.code == "TEST"

    def test_base_exception_without_code(self):
        from src.utils.exceptions import AoiTalkException

        e = AoiTalkException("test error")
        assert str(e) == "test error"
        assert e.code is None

    def test_configuration_error(self):
        from src.utils.exceptions import ConfigurationError

        e = ConfigurationError("bad key", config_key="llm_model")
        assert e.code == "CONFIG_ERROR"
        assert e.details["config_key"] == "llm_model"

    def test_audio_errors(self):
        from src.utils.exceptions import AudioInputError, AudioOutputError

        e_in = AudioInputError("no mic", device_name="mic0")
        assert e_in.code == "AUDIO_INPUT_ERROR"
        assert e_in.details["device"] == "mic0"

        e_out = AudioOutputError("no speaker")
        assert e_out.code == "AUDIO_OUTPUT_ERROR"

    def test_tts_error(self):
        from src.utils.exceptions import TTSError

        e = TTSError("fail", engine="voicevox", character="ずんだもん")
        assert e.details["engine"] == "voicevox"
        assert e.details["character"] == "ずんだもん"

    def test_validation_error(self):
        from src.utils.exceptions import ValidationError

        e = ValidationError("bad value", field="port", value=99999, expected="1-65535")
        assert e.code == "VALIDATION_ERROR"
        assert e.details["field"] == "port"

    def test_format_error_message(self):
        from src.utils.exceptions import ConfigurationError, format_error_message

        e = ConfigurationError("bad", config_key="x")
        msg = format_error_message(e, include_details=True)
        assert "config_key=x" in msg

    def test_format_error_message_no_details(self):
        from src.utils.exceptions import AoiTalkException, format_error_message

        e = AoiTalkException("simple")
        msg = format_error_message(e, include_details=True)
        assert msg == "simple"

    def test_is_retryable_error(self):
        from src.utils.exceptions import (
            ConnectionError,
            ResourceError,
            AudioInputError,
            ConfigurationError,
            is_retryable_error,
        )

        assert is_retryable_error(ConnectionError("timeout")) is True
        assert is_retryable_error(ResourceError("file temporarily locked")) is True
        assert is_retryable_error(ResourceError("not found")) is False
        assert is_retryable_error(AudioInputError("device busy")) is True
        assert is_retryable_error(ConfigurationError("bad")) is False


class TestI18n:
    def test_default_language_is_ja(self):
        from src.utils.i18n import I18nManager, Language

        mgr = I18nManager()
        assert mgr.current_language == Language.JA

    def test_get_translation_ja(self):
        from src.utils.i18n import I18nManager, Language

        mgr = I18nManager()
        assert mgr.get("error") == "エラー"
        assert mgr.get("success") == "成功"

    def test_get_translation_en(self):
        from src.utils.i18n import I18nManager, Language

        mgr = I18nManager(default_language=Language.EN)
        mgr.set_language(Language.EN)
        assert mgr.get("error") == "Error"

    def test_get_with_format_params(self):
        from src.utils.i18n import I18nManager

        mgr = I18nManager()
        msg = mgr.get("device_not_found", device="mic0")
        assert "mic0" in msg

    def test_fallback_to_key(self):
        from src.utils.i18n import I18nManager

        mgr = I18nManager()
        assert mgr.get("nonexistent_key") == "nonexistent_key"

    def test_has_translation(self):
        from src.utils.i18n import I18nManager, Language

        mgr = I18nManager()
        assert mgr.has_translation("error") is True
        assert mgr.has_translation("nonexistent") is False

    def test_add_translation(self):
        from src.utils.i18n import I18nManager, Language

        mgr = I18nManager()
        mgr.add_translation(Language.JA, "custom_key", "カスタム")
        assert mgr.get("custom_key") == "カスタム"

    def test_get_available_languages(self):
        from src.utils.i18n import I18nManager, Language

        mgr = I18nManager()
        langs = mgr.get_available_languages()
        assert Language.JA in langs
        assert Language.EN in langs

    def test_language_switch(self):
        from src.utils.i18n import I18nManager, Language

        mgr = I18nManager()
        mgr.set_language(Language.EN)
        assert mgr.get("error") == "Error"
        mgr.set_language(Language.JA)
        assert mgr.get("error") == "エラー"

    def test_fallback_to_default_language(self):
        from src.utils.i18n import I18nManager, Language

        mgr = I18nManager(default_language=Language.JA)
        # 韓国語に切り替えるが翻訳がないのでJAにフォールバック
        mgr.set_language(Language.KO)
        assert mgr.get("error") == "エラー"
