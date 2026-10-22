"""Optional provider timing contract. Times are relative to the exact audio segment."""

import math
from dataclasses import dataclass, field


def validate_visemes(value) -> list[dict[str, float]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 2048:
        raise ValueError("invalid viseme sequence")
    result, previous = [], 0.
    for item in value:
        if not isinstance(item, dict) or set(item) != {"start", "end", "open", "shape"}:
            raise ValueError("invalid viseme")
        if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in item.values()):
            raise ValueError("nonfinite viseme")
        if not (previous <= item["start"] < item["end"] <= 600 and 0 <= item["open"] <= 1 and -1 <= item["shape"] <= 1):
            raise ValueError("invalid viseme bounds")
        result.append(dict(item))
        previous = item["end"]
    return result


@dataclass(frozen=True)
class TimedSpeech:
    """An optional TTS manager synthesize_with_timing result; bytes remain supported."""

    audio: bytes
    visemes: list[dict[str, float]] = field(default_factory=list)
