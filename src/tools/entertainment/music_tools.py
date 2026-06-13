"""
音楽・BGM再生関連のツール
"""

import logging
from typing import Optional
from ..core import tool
from ...utils.audio_globals import trigger_bgm_change

logger = logging.getLogger(__name__)

@tool
async def play_bgm(bgm_id: str, volume: float = 0.5) -> str:
    """BGMを再生または切り替える。

    シナリオのシーンに合わせた雰囲気の音楽を再生するために使用する。
    
    Args:
        bgm_id: 再生するBGMの識別子（例: "mysterious_forest", "battle_theme", "peaceful_village"）
               またはYouTubeのURL。
        volume: 音量 (0.0 から 1.0)

    Returns:
        再生開始の確認メッセージ
    """
    try:
        await trigger_bgm_change(bgm_id, volume)
        logger.info("BGM切り替えリクエスト: %s (vol=%s)", bgm_id, volume)
        return f"BGMを '{bgm_id}' に切り替えました（音量: {volume}）。"
    except Exception as e:
        logger.error("BGM切り替えエラー: %s", e)
        return f"BGMの切り替え中にエラーが発生しました: {e}"

@tool
async def stop_bgm() -> str:
    """現在再生中のBGMを停止する。

    Returns:
        停止の確認メッセージ
    """
    try:
        await trigger_bgm_change("stop", 0.0)
        return "BGMを停止しました。"
    except Exception as e:
        logger.error("BGM停止エラー: %s", e)
        return f"BGMの停止中にエラーが発生しました: {e}"
