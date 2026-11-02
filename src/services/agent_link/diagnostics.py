"""Secret-free diagnostic projection for agent link operations."""

from __future__ import annotations

from dataclasses import dataclass
import math

from .codes import LinkErrorCode, normalize_code

_STAGES = frozenset({"prepare", "send", "observe", "health", "journal", "wait", "recover"})


@dataclass(frozen=True)
class LinkDiagnostic:
    code: LinkErrorCode
    stage: str
    http_status: int | None
    elapsed_ms: int
    turn_id_prefix: str

    def format(self) -> str:
        status = "none" if self.http_status is None else str(self.http_status)
        return (
            f"code={self.code.value} stage={self.stage} http_status={status} "
            f"elapsed_ms={self.elapsed_ms} turn_id_prefix={self.turn_id_prefix}"
        )


def make_diagnostic(
    *,
    code: object,
    stage: object,
    http_status: object = None,
    elapsed_ms: object = 0,
    turn_id: object = "",
) -> LinkDiagnostic:
    stage_value = str(stage) if str(stage) in _STAGES else "internal"
    status = (
        int(http_status)
        if type(http_status) is int and 100 <= http_status <= 599
        else None
    )
    try:
        elapsed = float(elapsed_ms)
    except (TypeError, ValueError, OverflowError):
        elapsed = 0.0
    if not math.isfinite(elapsed):
        elapsed = 0.0
    elapsed = max(0, min(int(elapsed), 2_147_483_647))
    raw_turn_id = str(turn_id or "")
    prefix = "".join(char for char in raw_turn_id if char.isalnum())[:8]
    return LinkDiagnostic(
        code=normalize_code(code),
        stage=stage_value,
        http_status=status,
        elapsed_ms=elapsed,
        turn_id_prefix=prefix,
    )
