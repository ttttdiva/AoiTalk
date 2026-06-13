"""Global registry for audio-related callbacks."""

from typing import Callable, Optional, Awaitable

# callback type: async (bgm_id: str, volume: float) -> None
_bgm_callback: Optional[Callable[[str, float], Awaitable[None]]] = None

def set_bgm_callback(callback: Callable[[str, float], Awaitable[None]]) -> None:
    """Set the global BGM change callback."""
    global _bgm_callback
    _bgm_callback = callback

async def trigger_bgm_change(bgm_id: str, volume: float = 0.5) -> None:
    """Trigger the BGM change callback."""
    if _bgm_callback:
        await _bgm_callback(bgm_id, volume)
