"""Model-facing tool for the user's existing Microsoft Edge tabs."""

from __future__ import annotations

from typing import Any

from .core import ToolDefinition, ToolParam


def build_browser_agent_tool(config: Any, *, client: Any = None) -> ToolDefinition:
    from ..services.browser_agent_models import BrowserAgentSettings
    settings = BrowserAgentSettings.from_config(config)
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

        effective = getattr(client, "config", None)
        if effective is None:
            effective = config
        if not BrowserAgentSettings.from_config(effective).enabled:
            return {"success": False, "status": "failed", "reason": "browser_agent_disabled"}
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
            effective, client=client, bridge=bridge
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
            "PC接続で選択したPCのMicrosoft Edgeを、Bridgeの既存helperで操作する。"
            "Bridge GUIで明示選択した既存targetを使い、start_urlはそのtarget上で開く。"
            "接続・target選択・候補判断に失敗した場合は停止する。返された観測に基づいて結果を説明する。"
            + ("候補判断にはJevを利用する。" if settings.jev_enabled else "")
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
                "選択済みtargetで開くURL。省略時は同じtargetを使う",
                required=False,
                default="",
            ),
            ToolParam(
                "tab_id",
                "integer",
                "現在のBridge接続から返されたtab handle。省略可",
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


def build_dedicated_browser_agent_tool(config: Any, *, client: Any = None) -> ToolDefinition:
    async def execute(goal: str = "", start_url: str = "", tab_id: int | None = None,
                      completion_text: str = "", completion_url: str = "", device_id: str = "", resume_run_id: str = ""):
        from ..services.browser_agent_models import BrowserAgentSettings
        from ..services.dedicated_jev_runtime import DedicatedJevRuntime
        from ..services.pc_bridge_service import resolve_control_bridge
        from ..services.pc_bridge_store import PcBridgeError
        from ..services.turn_context import get_turn_context

        effective = getattr(client, "config", None)
        if effective is None:
            effective = config
        settings = BrowserAgentSettings.from_config(effective)
        if not settings.enabled or not settings.dedicated_enabled:
            return {"success": False, "status": "failed", "reason": "browser_dedicated_disabled"}
        turn = get_turn_context()
        if not turn.user_id or not turn.session_id:
            return {"success": False, "status": "failed", "reason": "browser_authenticated_session_required"}
        try:
            bridge = await resolve_control_bridge(device_id or None, dedicated=True)
        except PcBridgeError as exc:
            return {"success": False, "status": "failed", "reason": str(exc)}
        service = DedicatedJevRuntime(effective, client=client, bridge=bridge)
        if resume_run_id:
            return await service.resume(resume_run_id)
        if not goal.strip():
            return {"success": False, "status": "failed", "reason": "browser_goal_required"}
        return await service.run_goal(
            goal=goal, start_url=start_url, tab_id=tab_id,
            completion_text=completion_text, completion_url=completion_url)

    ordinary = build_browser_agent_tool(config, client=client)
    return ToolDefinition(
        name="browser_agent_dedicated", owner="browser_agent", function=execute,
        description=("PC接続で選択したPCのBridgeが所有する専用ブラウザで作業する。"
                     "通常のブラウザとは別のログイン保存領域を使う。ログインが必要な画面ではユーザーへ引き継ぎ、"
                     "認証情報をチャットへ求めず、同じ作業を再開する。最終送信・予約の確認は別途必要。"),
        parameters=[*(param if param.name != "goal" else ToolParam("goal", "string", "新規作業の依頼。再開時は省略可", required=False, default="") for param in ordinary.parameters),
                    ToolParam("resume_run_id", "string", "保存済み作業のrun_id。元の依頼を再入力せず、未実行部分から再開", required=False, default="")],
        is_async=True, side_effect="writes", risk="normal", supports_parallel=False, timeout_seconds=3600)
