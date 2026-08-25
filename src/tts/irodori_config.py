"""AoiTalk-owned configuration policy for the embedded Irodori-TTS runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, MutableMapping, Optional


IRODORI_TTS_CHECKPOINT = "Aratako/Irodori-TTS-v4.1-Small"
IRODORI_V3_CHECKPOINT = "Aratako/Irodori-TTS-600M-v3-VoiceDesign"

# ``irodori_model`` is the stable, human-facing character setting.  Keep the
# concrete Hugging Face repository ids in this module so UI/API/manager code
# cannot drift apart.  The selector intentionally uses lower-case ASCII so it
# can safely live in the existing JSON ``voice_parameters`` column.
IRODORI_MODEL_V4_1_SMALL = "v4.1-small"
IRODORI_MODEL_V3_VOICE_DESIGN = "v3-voice-design"
IRODORI_MODEL_CHECKPOINTS = {
    IRODORI_MODEL_V4_1_SMALL: IRODORI_TTS_CHECKPOINT,
    IRODORI_MODEL_V3_VOICE_DESIGN: IRODORI_V3_CHECKPOINT,
}


def normalize_irodori_model(value: Any) -> Optional[str]:
    """Return a canonical selector or ``None`` for an unknown/empty value.

    A few pre-release clients used labels rather than selector ids.  Accepting
    those aliases is harmless and makes old JSON settings load predictably,
    while unknown values are left to the caller's default policy.
    """

    if not isinstance(value, str):
        return None
    raw = value.strip().lower()
    aliases = {
        "v4": IRODORI_MODEL_V4_1_SMALL,
        "v4.1": IRODORI_MODEL_V4_1_SMALL,
        "v4.1 small": IRODORI_MODEL_V4_1_SMALL,
        "irodori-tts-v4.1-small": IRODORI_MODEL_V4_1_SMALL,
        "v3": IRODORI_MODEL_V3_VOICE_DESIGN,
        "v3 voice design": IRODORI_MODEL_V3_VOICE_DESIGN,
        "voice-design": IRODORI_MODEL_V3_VOICE_DESIGN,
        "irodori-tts-600m-v3-voicedesign": IRODORI_MODEL_V3_VOICE_DESIGN,
    }
    raw = aliases.get(raw, raw)
    return raw if raw in IRODORI_MODEL_CHECKPOINTS else None


def resolve_irodori_checkpoint(
    settings: Mapping[str, Any] | None = None,
    *,
    fallback_settings: Mapping[str, Any] | None = None,
) -> str:
    """Resolve one Irodori selector/settings mapping to a checkpoint string.

    Precedence is deliberately compatibility-first:

    1. An explicit ``hf_checkpoint`` (including a local path) is preserved.
    2. The legacy ``voice_design_checkpoint`` alias is promoted unchanged.
    3. A recognized ``irodori_model`` selector maps to its canonical HF repo.
    4. The optional fallback mapping is consulted (used for app-wide settings).
    5. v4.1 Small is the final default.

    This means introducing the selector never rewrites existing custom/local
    checkpoints, while a character-level selector still overrides an app-level
    default when no character checkpoint was explicitly persisted.
    """

    primary = settings if isinstance(settings, Mapping) else {}
    fallback = fallback_settings if isinstance(fallback_settings, Mapping) else {}

    # Character-level explicit checkpoint/legacy alias always wins.
    for source in (primary,):
        checkpoint = source.get("hf_checkpoint")
        if isinstance(checkpoint, str) and checkpoint.strip():
            return checkpoint.strip()
        legacy = source.get("voice_design_checkpoint")
        if isinstance(legacy, str) and legacy.strip():
            return legacy.strip()

    # A character selector is intentionally evaluated before app-wide values;
    # otherwise a normalized global v4 default would silently mask v3.
    if "irodori_model" in primary:
        selector = normalize_irodori_model(primary.get("irodori_model"))
        if selector is not None:
            return IRODORI_MODEL_CHECKPOINTS[selector]

    for source in (fallback,):
        checkpoint = source.get("hf_checkpoint")
        if isinstance(checkpoint, str) and checkpoint.strip():
            return checkpoint.strip()
        legacy = source.get("voice_design_checkpoint")
        if isinstance(legacy, str) and legacy.strip():
            return legacy.strip()
        selector = normalize_irodori_model(source.get("irodori_model"))
        if selector is not None:
            return IRODORI_MODEL_CHECKPOINTS[selector]

    return IRODORI_TTS_CHECKPOINT


def normalize_irodori_settings(settings: MutableMapping[str, Any]) -> bool:
    """Normalize Irodori settings without overriding an explicit checkpoint.

    Older AoiTalk releases wrote ``voice_design_checkpoint`` alongside a
    hard-coded v3 checkpoint.  Promote that legacy value only when no primary
    checkpoint exists, then remove the obsolete alias.  A user-selected v2,
    v3, local, or other Hugging Face checkpoint is intentionally preserved.
    """
    changed = False

    # Canonicalize the selector when present, but do not invent one for legacy
    # settings: callers can distinguish an absent value from a user selection.
    selector = None
    if "irodori_model" in settings:
        selector = normalize_irodori_model(settings.get("irodori_model"))
        if selector is not None and settings.get("irodori_model") != selector:
            settings["irodori_model"] = selector
            changed = True

    checkpoint = settings.get("hf_checkpoint")
    if not isinstance(checkpoint, str) or not checkpoint.strip():
        legacy_checkpoint = settings.get("voice_design_checkpoint")
        if isinstance(legacy_checkpoint, str) and legacy_checkpoint.strip():
            settings["hf_checkpoint"] = legacy_checkpoint.strip()
        elif selector is None:
            settings["hf_checkpoint"] = IRODORI_TTS_CHECKPOINT
        if "hf_checkpoint" in settings:
            changed = True

    if "voice_design_checkpoint" in settings:
        settings.pop("voice_design_checkpoint", None)
        changed = True

    # 30 seconds was the unconditional legacy default. Dropping only that
    # value lets v4 derive its 120-second reference limit and predicted output
    # duration from checkpoint metadata while preserving explicit overrides.
    if settings.get("seconds") == 30 or settings.get("seconds") == 30.0:
        settings.pop("seconds", None)
        changed = True

    return changed


__all__ = [
    "IRODORI_TTS_CHECKPOINT",
    "IRODORI_V3_CHECKPOINT",
    "IRODORI_MODEL_V4_1_SMALL",
    "IRODORI_MODEL_V3_VOICE_DESIGN",
    "IRODORI_MODEL_CHECKPOINTS",
    "normalize_irodori_model",
    "resolve_irodori_checkpoint",
    "normalize_irodori_settings",
]
