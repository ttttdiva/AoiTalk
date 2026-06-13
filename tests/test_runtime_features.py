from pathlib import Path

import yaml

from src.runtime_features import RuntimeFeatureManager


class _ConfigStub:
    def __init__(self, path: Path, runtime_features=None) -> None:
        self.config_path = path
        self.config = {}
        if runtime_features is not None:
            self.config["runtime_features"] = runtime_features

    def get(self, key: str, default=None):
        current = self.config
        for part in str(key).split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def save_to_file(self, key: str, value):
        current = self.config
        parts = key.split(".")
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = value
        self.config_path.write_text(
            yaml.safe_dump(self.config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return True


def test_configured_runtime_features_enable_discord_adapters(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("runtime_features: {}\n", encoding="utf-8")
    manager = RuntimeFeatureManager()
    manager.configure(
        _ConfigStub(
            config_path,
            runtime_features={
                "discord_bot": True,
                "discord_text": True,
                "discord_vc_input": True,
                "discord_vc_output": True,
                "tts": True,
            },
        )
    )

    status = manager.status()

    assert status["features"]["web_ui"] is True
    assert status["features"]["discord_bot"] is True
    assert status["features"]["discord_vc_input"] is True
    assert status["features"]["discord_vc_output"] is True
    assert "discord_text" in status["input_adapters"]
    assert "discord_vc_audio" in status["output_adapters"]


def test_runtime_feature_update_persists_and_keeps_webui_on(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("runtime_features: {}\n", encoding="utf-8")
    config = _ConfigStub(config_path)
    manager = RuntimeFeatureManager()
    manager.configure(config)

    status = manager.update_feature("discord_vc_input", True)

    assert status["features"]["web_ui"] is True
    assert status["features"]["discord_bot"] is True
    assert status["features"]["discord_text"] is True
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["runtime_features"]["discord_vc_input"] is True
    assert saved["runtime_features"]["discord_bot"] is True


def test_enabling_discord_text_also_enables_bot(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("runtime_features: {}\n", encoding="utf-8")
    config = _ConfigStub(config_path)
    manager = RuntimeFeatureManager()
    manager.configure(config)

    status = manager.update_feature("discord_text", True)

    assert status["features"]["discord_bot"] is True
    assert status["features"]["discord_text"] is True


def test_webui_cannot_be_disabled(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("runtime_features: {}\n", encoding="utf-8")
    manager = RuntimeFeatureManager()
    manager.configure(_ConfigStub(config_path))

    try:
        manager.update_feature("web_ui", False)
    except ValueError as exc:
        assert "WebUI" in str(exc)
    else:
        raise AssertionError("web_ui disable should fail")
