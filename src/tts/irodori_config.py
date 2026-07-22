"""AoiTalk-owned configuration policy for the embedded Irodori-TTS runtime."""

from __future__ import annotations

from typing import Any, MutableMapping


IRODORI_TTS_CHECKPOINT = "Aratako/Irodori-TTS-600M-v3-VoiceDesign"


def normalize_irodori_settings(settings: MutableMapping[str, Any]) -> bool:
    """Migrate an Irodori settings mapping to AoiTalk's single-model policy."""
    changed = False

    if settings.get("hf_checkpoint") != IRODORI_TTS_CHECKPOINT:
        settings["hf_checkpoint"] = IRODORI_TTS_CHECKPOINT
        changed = True

    if "voice_design_checkpoint" in settings:
        settings.pop("voice_design_checkpoint", None)
        changed = True

    # 30 seconds was the unconditional v2 default. Dropping only that legacy
    # value restores v3 duration prediction while preserving explicit overrides.
    if settings.get("seconds") == 30 or settings.get("seconds") == 30.0:
        settings.pop("seconds", None)
        changed = True

    return changed
