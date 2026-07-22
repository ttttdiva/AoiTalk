"""Yomi Linter と TTS アダプター間の安定したデータ型。"""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, List


class TTSYomiPolicy(str, Enum):
    DISABLED = "disabled"
    DETECT_ONLY = "detect_only"
    DICTIONARY = "dictionary"
    TEXT_REWRITE = "text_rewrite"


@dataclass(frozen=True)
class Detection:
    surface: str
    start: int
    end: int
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PreflightResult:
    original_text: str
    detections: List[Detection]
    model_id: str
    tts_engine: str
    dictionary_applied: bool
    final_text: str
    policy: TTSYomiPolicy

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["policy"] = self.policy.value
        return value
