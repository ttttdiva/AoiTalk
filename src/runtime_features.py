"""Runtime feature state for AoiTalk.

Runtime behavior is expressed as explicit input/output adapters so WebUI,
local audio, and Discord can be reasoned about separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable


LOCAL_AUDIO_KEYS = {"local_mic", "local_speaker"}
DISCORD_AUDIO_KEYS = {"discord_vc_input", "discord_vc_output"}


@dataclass(frozen=True)
class RuntimeFeatureDefinition:
    key: str
    label: str
    description: str
    restart_required: bool = False


FEATURE_DEFINITIONS: Dict[str, RuntimeFeatureDefinition] = {
    "web_ui": RuntimeFeatureDefinition(
        "web_ui",
        "WebUI",
        "Web interface, BFF, and TRPG screens. This is always enabled.",
    ),
    "local_mic": RuntimeFeatureDefinition(
        "local_mic",
        "ローカルマイク入力",
        "Use the local microphone through the local audio pipeline.",
        restart_required=True,
    ),
    "local_speaker": RuntimeFeatureDefinition(
        "local_speaker",
        "ローカルスピーカー出力",
        "Play synthesized speech on the local machine.",
        restart_required=True,
    ),
    "tts": RuntimeFeatureDefinition(
        "tts",
        "読み上げ",
        "Enable text-to-speech synthesis for configured output targets.",
    ),
    "discord_bot": RuntimeFeatureDefinition(
        "discord_bot",
        "Discord Bot",
        "Run the Discord bot service alongside the WebUI runtime.",
    ),
    "discord_text": RuntimeFeatureDefinition(
        "discord_text",
        "Discordテキスト",
        "Accept mentions/replies and slash commands on Discord.",
    ),
    "discord_vc_input": RuntimeFeatureDefinition(
        "discord_vc_input",
        "Discord VC音声入力",
        "Use Discord voice gateway audio as speech input.",
    ),
    "discord_vc_output": RuntimeFeatureDefinition(
        "discord_vc_output",
        "Discord VC音声出力",
        "Send synthesized speech to Discord voice channels.",
    ),
    "console_input": RuntimeFeatureDefinition(
        "console_input",
        "コンソール入力",
        "Keep the local terminal prompt available for maintenance.",
    ),
}


DEFAULT_RUNTIME_FEATURES: Dict[str, bool] = {
    "web_ui": True,
    "local_mic": False,
    "local_speaker": False,
    "tts": False,
    "discord_bot": False,
    "discord_text": False,
    "discord_vc_input": False,
    "discord_vc_output": False,
    "console_input": True,
}


class RuntimeFeatureManager:
    """Owns runtime feature flags."""

    def __init__(self) -> None:
        self._config = None
        self._features: Dict[str, bool] = dict(DEFAULT_RUNTIME_FEATURES)

    def configure(self, config) -> None:
        self._config = config
        configured = config.get("runtime_features", None)

        if isinstance(configured, dict):
            self._features = self._normalize_features(configured)
            self._features["web_ui"] = True
            self._update_config_memory()
            return

        self._features = dict(DEFAULT_RUNTIME_FEATURES)
        self._update_config_memory()

    def status(self) -> Dict[str, Any]:
        features = dict(self._features)
        output_targets = []
        if features.get("local_speaker") and features.get("tts"):
            output_targets.append("local")
        if features.get("discord_vc_output") and features.get("discord_bot") and features.get("tts"):
            output_targets.append("discord_vc")
        output_targets.append("web_log")

        return {
            "web_ui_always_on": True,
            "features": features,
            "definitions": [
                {
                    "key": definition.key,
                    "label": definition.label,
                    "description": definition.description,
                    "restart_required": definition.restart_required,
                }
                for definition in FEATURE_DEFINITIONS.values()
            ],
            "input_adapters": self._input_adapters(features),
            "output_adapters": self._output_adapters(features),
            "tts_output_targets": output_targets,
            "local_audio_enabled": self.local_audio_enabled,
            "discord_enabled": self.discord_enabled,
        }

    def update_feature(self, key: str, enabled: bool, *, persist: bool = True) -> Dict[str, Any]:
        if key not in FEATURE_DEFINITIONS:
            raise ValueError(f"unsupported runtime feature: {key}")
        if key == "web_ui" and not enabled:
            raise ValueError("WebUIは常時有効です")

        self._features[key] = bool(enabled)
        self._features["web_ui"] = True

        if key.startswith("discord_") and key != "discord_bot" and enabled:
            self._features["discord_bot"] = True
            self._features["discord_text"] = True
        if key == "discord_bot" and not enabled:
            self._features["discord_text"] = False
            self._features["discord_vc_input"] = False
            self._features["discord_vc_output"] = False
        if key in LOCAL_AUDIO_KEYS and enabled:
            self._features["tts"] = True

        self._update_config_memory()
        if persist:
            self._persist_all()
        return self.status()

    @property
    def local_audio_enabled(self) -> bool:
        return bool(self._features.get("local_mic") or self._features.get("local_speaker"))

    @property
    def discord_enabled(self) -> bool:
        return bool(self._features.get("discord_bot"))

    def feature_enabled(self, key: str) -> bool:
        return bool(self._features.get(key))

    def _normalize_features(self, configured: Dict[str, Any]) -> Dict[str, bool]:
        base = dict(DEFAULT_RUNTIME_FEATURES)
        for key in FEATURE_DEFINITIONS:
            if key in configured:
                base[key] = bool(configured[key])
        base["web_ui"] = True
        return base

    def _input_adapters(self, features: Dict[str, bool]) -> list[str]:
        adapters = ["web_text"]
        if features.get("console_input"):
            adapters.append("console_text")
        if features.get("local_mic"):
            adapters.append("local_mic")
        if features.get("discord_bot") and features.get("discord_text"):
            adapters.append("discord_text")
        if features.get("discord_bot") and features.get("discord_vc_input"):
            adapters.append("discord_vc_audio")
        return adapters

    def _output_adapters(self, features: Dict[str, bool]) -> list[str]:
        adapters = ["web_log"]
        if features.get("local_speaker") and features.get("tts"):
            adapters.append("local_speaker")
        if features.get("discord_bot") and features.get("discord_text"):
            adapters.append("discord_text")
        if features.get("discord_bot") and features.get("discord_vc_output") and features.get("tts"):
            adapters.append("discord_vc_audio")
        return adapters

    def _persist_all(self) -> None:
        if not self._config:
            return
        if hasattr(self._config, "save_to_file"):
            for key, value in self._features.items():
                self._config.save_to_file(f"runtime_features.{key}", value)
        else:
            self._config.config["runtime_features"] = dict(self._features)

    def _update_config_memory(self) -> None:
        if self._config and hasattr(self._config, "config"):
            self._config.config["runtime_features"] = dict(self._features)


runtime_feature_manager = RuntimeFeatureManager()


def supported_feature_keys() -> Iterable[str]:
    return FEATURE_DEFINITIONS.keys()
