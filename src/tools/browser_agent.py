"""Model-facing tool for the user's existing Microsoft Edge tabs."""

from __future__ import annotations

from typing import Any

from .core import ToolDefinition, ToolParam


def build_browser_agent_tool(config: Any, *, client: Any = None) -> ToolDefinition:
    async def execute(
        goal: str,
        start_url: str = "",
        tab_id: int | None = None,
        completion_text: str = "",
        completion_url: str = "",
        device_id: str = "",
    ) -> dict[str, Any]:
        from ..services.browser_agent_service import BrowserAgentService
        from ..services.turn_context import get_turn_context

        turn = get_turn_context()
        if not turn.user_id or not turn.session_id:
            return {
                "success": False,
                "status": "failed",
                "reason": "browser_authenticated_session_required",
            }
        from ..services.pc_bridge_service import resolve_control_bridge
        from ..services.pc_bridge_store import PcBridgeError
        try:
            bridge = await resolve_control_bridge(device_id or None)
        except PcBridgeError as exc:
            return {"success": False, "status": "failed", "reason": str(exc)}
        service = BrowserAgentService(
            getattr(client, "config", None) or config, client=client, bridge=bridge
        )
        return await service.run_goal(
            goal=goal,
            start_url=start_url,
            tab_id=tab_id,
            completion_text=completion_text,
            completion_url=completion_url,
        )

    return ToolDefinition(
        name="browser_agent",
        description=(
            "PC接続で選択したPCのMicrosoft Edgeのログイン済みタブをBridge経由で操作する。"
            "goalに作業全体を書く。start_url省略時は拡張で選択中のタブ、URL指定時は既存の同一URLのタブまたは同じEdge内の新規タブを使う。"
            "tab_idで既存タブも指定できる。隔離ブラウザは使わない。Jevが利用不能なら現在のLLMで続ける。"
            "結果は返されたobservationに基づいて説明する。接続がない場合は拡張接続が必要と報告する。"
        ),
        function=execute,
        parameters=[
            ToolParam("device_id", "string", "操作先PC。省略時はPC接続設定の選択を使用", required=False, default=""),
            ToolParam(
                "goal", "string", "現在のタブで行う操作・入力値・読む内容を含む作業全体"
            ),
            ToolParam(
                "start_url",
                "string",
                "開くURL。省略すると選択中のタブをそのまま使う",
                required=False,
                default="",
            ),
            ToolParam(
                "tab_id",
                "integer",
                "操作する既存EdgeタブのID。省略可能",
                required=False,
            ),
            ToolParam(
                "completion_text",
                "string",
                "最終画面で確認する文字列",
                required=False,
                default="",
            ),
            ToolParam(
                "completion_url",
                "string",
                "期待する最終URL",
                required=False,
                default="",
            ),
        ],
        is_async=True,
        owner="browser_agent",
        side_effect="writes",
        risk="normal",
        supports_parallel=False,
        timeout_seconds=930,
    )
