"""Features（機能フラグ）モジュールのテスト"""
import os
import pytest


class TestFeatures:
    def setup_method(self):
        """各テスト前にキャッシュをリセット"""
        from src.features import Features

        Features.reset_cache()
        # テスト用の環境変数をクリア
        for key in list(os.environ.keys()):
            if key.startswith("FEATURE_") or key == "AOITALK_PROFILE":
                os.environ.pop(key, None)

    def teardown_method(self):
        """各テスト後にクリーンアップ"""
        from src.features import Features

        Features.reset_cache()
        for key in list(os.environ.keys()):
            if key.startswith("FEATURE_") or key == "AOITALK_PROFILE":
                os.environ.pop(key, None)

    def test_default_features(self):
        from src.features import Features

        assert Features.voice_input() is True
        assert Features.tts_output() is True
        assert Features.discord_bot() is True
        assert Features.entertainment() is True
        assert Features.code_agent() is False

    def test_env_override_enables(self):
        from src.features import Features

        os.environ["FEATURE_CODE_AGENT"] = "true"
        assert Features.is_enabled("code_agent") is True

    def test_env_override_disables(self):
        from src.features import Features

        os.environ["FEATURE_VOICE_INPUT"] = "false"
        assert Features.voice_input() is False

    def test_env_override_various_true_values(self):
        from src.features import Features

        for val in ("true", "1", "yes", "True", "YES"):
            Features.reset_cache()
            os.environ["FEATURE_CODE_AGENT"] = val
            assert Features.is_enabled("code_agent") is True

    def test_enterprise_profile(self):
        from src.features import Features

        os.environ["AOITALK_PROFILE"] = "enterprise"
        assert Features.voice_input() is False
        assert Features.tts_output() is False
        assert Features.code_agent() is True

    def test_personal_profile(self):
        from src.features import Features

        os.environ["AOITALK_PROFILE"] = "personal"
        assert Features.voice_input() is True
        assert Features.entertainment() is True

    def test_get_all_returns_dict(self):
        from src.features import Features

        result = Features.get_all()
        assert isinstance(result, dict)
        assert "voice_input" in result
        assert "code_agent" in result

    def test_unknown_feature_returns_false(self):
        from src.features import Features

        assert Features.is_enabled("nonexistent_feature") is False

    def test_env_has_priority_over_profile(self):
        from src.features import Features

        os.environ["AOITALK_PROFILE"] = "enterprise"
        os.environ["FEATURE_VOICE_INPUT"] = "true"
        # enterpriseではvoice_input=Falseだが、envで上書き
        assert Features.voice_input() is True

    def test_reset_cache(self):
        from src.features import Features

        Features.is_enabled("voice_input")
        Features.reset_cache()
        assert Features._profile_cache is None
        assert Features._initialized is False
