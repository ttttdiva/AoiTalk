"""Runtime feature state for AoiTalk.

Runtime behavior is expressed as explicit input/output adapters so WebUI,
local audio, and Discord can be reasoned about separately.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable

from .features import Features


LOCAL_AUDIO_KEYS = {"local_mic", "local_speaker"}
DISCORD_AUDIO_KEYS = {"discord_vc_input", "discord_vc_output"}
ENTERPRISE_DISABLED_RUNTIME_KEYS = {
    "local_mic",
    "local_speaker",
    "tts",
    "discord_bot",
    "discord_text",
    "discord_vc_input",
    "discord_vc_output",
}


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
        else:
            self._features = dict(DEFAULT_RUNTIME_FEATURES)

        # Docker/Enterprise runs are supervised services, not interactive
        # terminal sessions. A closed stdin must never make main.py exit
        # and trigger the container restart policy. Keep the config value
        # available for local runs, while making the explicit headless
        # deployment contract authoritative.
        if os.getenv("AOITALK_HEADLESS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            self._features["console_input"] = False
        self._apply_profile_constraints()
        # Startup configuration is a complete candidate state, so apply the
        # same dependency invariant used by runtime update/restore routes.
        # Invalid values must fail before services start rather than silently
        # coercing e.g. the string "false" to True.
        self._validate_candidate_features(self._features)
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
        return self.update_features({key: enabled}, persist=persist)

    def update_features(
        self,
        changes: Dict[str, bool],
        *,
        persist: bool = True,
    ) -> Dict[str, Any]:
        """Validate and apply a group of feature changes as one state update."""
        if not changes:
            raise ValueError("runtime feature changes must not be empty")
        self._validate_grouped_changes(changes)

        candidate = dict(self._features)
        for key, enabled in changes.items():
            self._validate_feature_change(key, enabled)
            self._apply_feature_change(candidate, key, enabled)

        candidate["web_ui"] = True
        self._apply_profile_constraints_to(candidate)
        self._validate_candidate_features(candidate)
        self._replace_features(candidate, persist=persist)
        return self.status()

    @staticmethod
    def _validate_grouped_changes(changes: Dict[str, bool]) -> None:
        """Reject explicit dependency conflicts independent of mapping order."""
        if changes.get("discord_bot") is False and any(
            changes.get(key) is True
            for key in ("discord_text", "discord_vc_input", "discord_vc_output")
        ):
            raise ValueError(
                "discord_bot cannot be disabled while a Discord adapter is enabled"
            )
        if changes.get("discord_text") is False and any(
            changes.get(key) is True
            for key in ("discord_vc_input", "discord_vc_output")
        ):
            raise ValueError(
                "discord_text cannot be disabled while a Discord VC adapter is enabled"
            )
        if changes.get("tts") is False and any(
            changes.get(key) is True for key in LOCAL_AUDIO_KEYS
        ):
            raise ValueError("tts cannot be disabled while local audio is enabled")

    @staticmethod
    def _validate_candidate_features(features: Dict[str, bool]) -> None:
        """Reject a complete state that leaves any runtime dependency broken.

        Dependency checks must run after all requested changes and their
        enable-side cascades have been applied.  This catches conflicts that
        are implicit in the previous state, such as disabling ``tts`` while a
        local speaker or Discord VC output remains enabled.
        """
        if any(features.get(key) for key in LOCAL_AUDIO_KEYS) and not features.get(
            "tts"
        ):
            raise ValueError("tts cannot be disabled while local audio is enabled")
        if any(
            features.get(key)
            for key in ("discord_text", "discord_vc_input", "discord_vc_output")
        ) and not features.get("discord_bot"):
            raise ValueError("discord_bot cannot be disabled while a Discord adapter is enabled")
        if any(features.get(key) for key in ("discord_vc_input", "discord_vc_output")) and not features.get(
            "discord_text"
        ):
            raise ValueError("discord_text cannot be disabled while a Discord VC adapter is enabled")
        if features.get("discord_vc_output") and not features.get("tts"):
            raise ValueError("tts cannot be disabled while Discord VC output is enabled")

    def snapshot_features(self) -> Dict[str, bool]:
        """Return a detached feature snapshot for route-level compensation."""
        return dict(self._features)

    def restore_features(
        self,
        snapshot: Dict[str, bool],
        *,
        persist: bool = True,
    ) -> Dict[str, Any]:
        """Restore a previously captured complete feature snapshot."""
        candidate = self._normalize_features(snapshot)
        candidate["web_ui"] = True
        self._apply_profile_constraints_to(candidate)
        self._validate_candidate_features(candidate)
        self._replace_features(candidate, persist=persist)
        return self.status()

    def _validate_feature_change(self, key: str, enabled: bool) -> None:
        if key not in FEATURE_DEFINITIONS:
            raise ValueError(f"unsupported runtime feature: {key}")
        if key == "web_ui" and not enabled:
            raise ValueError("WebUIは常時有効です")
        if Features.is_enterprise() and key in ENTERPRISE_DISABLED_RUNTIME_KEYS and enabled:
            raise ValueError("Enterpriseプロファイルではこの機能を有効化できません")
        if (
            key == "console_input"
            and enabled
            and os.getenv("AOITALK_HEADLESS", "").strip().lower()
            in {"1", "true", "yes", "on"}
        ):
            raise ValueError("ヘッドレス実行中はコンソール入力を有効化できません")

    @staticmethod
    def _apply_feature_change(features: Dict[str, bool], key: str, enabled: bool) -> None:
        features[key] = bool(enabled)
        if key.startswith("discord_") and key != "discord_bot" and enabled:
            features["discord_bot"] = True
            features["discord_text"] = True
        if key in LOCAL_AUDIO_KEYS and enabled:
            features["tts"] = True

    def _replace_features(self, candidate: Dict[str, bool], *, persist: bool) -> None:
        previous = dict(self._features)
        try:
            if persist:
                self._persist_features(candidate)
            self._features = dict(candidate)
            self._update_config_memory()
        except Exception:
            self._features = previous
            self._update_config_memory()
            raise

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
        unknown = sorted(set(configured) - set(FEATURE_DEFINITIONS))
        if unknown:
            raise ValueError(
                "unsupported runtime feature(s): " + ", ".join(unknown)
            )
        for key in FEATURE_DEFINITIONS:
            if key in configured:
                value = configured[key]
                if not isinstance(value, bool):
                    raise ValueError(f"runtime feature {key} must be a boolean")
                base[key] = value
        base["web_ui"] = True
        return base

    def _apply_profile_constraints(self) -> None:
        self._apply_profile_constraints_to(self._features)

    @staticmethod
    def _apply_profile_constraints_to(features: Dict[str, bool]) -> None:
        if Features.is_enterprise():
            for key in ENTERPRISE_DISABLED_RUNTIME_KEYS:
                features[key] = False

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

    def _persist_features(self, features: Dict[str, bool]) -> None:
        if not self._config:
            return
        if hasattr(self._config, "save_to_file"):
            if not self._config.save_to_file("runtime_features", dict(features)):
                raise RuntimeError("Failed to persist runtime features")
        else:
            self._config.config["runtime_features"] = dict(features)

    def _update_config_memory(self) -> None:
        if self._config and hasattr(self._config, "config"):
            self._config.config["runtime_features"] = dict(self._features)


runtime_feature_manager = RuntimeFeatureManager()


class RuntimeFeatureRollbackError(RuntimeError):
    """The requested update failed and its prior state could not be restored."""


class RuntimeFeatureCoordinator:
    """Serialize runtime mutations and compensate process side effects."""

    def __init__(self, manager: RuntimeFeatureManager) -> None:
        self.manager = manager
        self._lock = asyncio.Lock()

    async def update_features(
        self,
        changes: Dict[str, bool],
        *,
        config: Any,
        discord_service: Any,
    ) -> Dict[str, Any]:
        async with self._lock:
            previous = self.manager.snapshot_features()
            state_updated = False
            try:
                self.manager.update_features(changes, persist=True)
                state_updated = True
                await self._sync_discord_service(config, discord_service)
                return self.manager.status()
            except BaseException as exc:
                if state_updated:
                    try:
                        self.manager.restore_features(previous, persist=True)
                        await self._sync_discord_service(config, discord_service)
                    except BaseException as rollback_exc:
                        raise RuntimeFeatureRollbackError(
                            "ランタイム機能の変更と復元に失敗しました。"
                            "サーバー状態を確認してください。"
                        ) from rollback_exc
                raise

    async def update_feature(
        self,
        feature: str,
        enabled: bool,
        *,
        config: Any,
        discord_service: Any,
    ) -> Dict[str, Any]:
        return await self.update_features(
            {feature: enabled},
            config=config,
            discord_service=discord_service,
        )

    async def _sync_discord_service(self, config: Any, discord_service: Any) -> None:
        discord_service.configure(config)
        if self.manager.discord_enabled:
            service_status = await discord_service.ensure_started(config)
        else:
            service_status = await discord_service.stop()
        if service_status.get("state") == "failed":
            raise RuntimeError(
                str(
                    service_status.get("last_error")
                    or "Discord Bot state change failed"
                )
            )


runtime_feature_coordinator = RuntimeFeatureCoordinator(runtime_feature_manager)


def supported_feature_keys() -> Iterable[str]:
    return FEATURE_DEFINITIONS.keys()
