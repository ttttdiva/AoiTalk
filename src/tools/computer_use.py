"""One Windows Computer Use tool; device routing is resolved by the server."""
from typing import Any
from .core import ToolDefinition, ToolParam


def build_computer_use_tool(config: Any, *, client=None):
    async def execute(goal='', action='observe', device_id='', text='', keys=None,
                      x=None, y=None, x2=None, y2=None, window_id=None, amount=-3, use_vision=False):
        from ..services.pc_bridge_service import resolve_control_bridge
        from ..services.pc_bridge_store import PcBridgeError
        from ..services.computer_use_service import ComputerUseService
        try:
            bridge = await resolve_control_bridge(device_id or None)
        except PcBridgeError as exc:
            return {'success': False, 'reason': str(exc)}
        params = {k: v for k, v in dict(text=text, keys=keys, x=x, y=y, x2=x2, y2=y2,
                                       window_id=window_id, amount=amount).items() if v is not None}
        return await ComputerUseService(getattr(client, 'config', None) or config, client, bridge).run(
            goal=goal, action=action, params=params, use_vision=use_vision)

    return ToolDefinition(
        name='computer_use',
        description='設定のPC接続で選択したPCのWindowsを操作する。goalで目的を渡すと画面観測と操作を繰り返す。'
                    'ブラウザDOMはbrowser_agent、Windowsアプリや座標操作はこのツール。'
                    'goal省略時はactionを1回実行。observeはウィンドウ・UI要素・画面のURLを返す。'
                    'use_vision=trueは既存の画像認識モデルも利用する。Jevは必須ではない。'
                    '接続PC未選択・切断時は停止しサーバーPCには切り替えない。',
        function=execute, is_async=True, owner='computer_use', side_effect='writes', risk='normal',
        supports_parallel=False, timeout_seconds=630,
        parameters=[
            ToolParam('goal', 'string', '実行したいWindows作業。省略時は単発操作', required=False, default=''),
            ToolParam('action', 'string', '単発操作', required=False, default='observe', enum=['observe','screenshot','windows','focus','click','double_click','right_click','move','drag','press','type','scroll']),
            ToolParam('device_id', 'string', '省略時は設定で選択済みのPC。別の登録PCのIDも指定可', required=False, default=''),
            ToolParam('text', 'string', 'typeで入力する文字列', required=False, default=''),
            ToolParam('keys', 'array', 'pressで同時押しするキー。例 CTRL,A', required=False, schema={'type':'array','items':{'type':'string'}}),
            *[ToolParam(name, 'integer', '実デスクトップ座標／ウィンドウID', required=False) for name in ('x','y','x2','y2','window_id')],
            ToolParam('amount', 'integer', 'scroll量。負数で下方向', required=False, default=-3),
            ToolParam('use_vision', 'boolean', '既存の画像認識モデルで画面も解析する', required=False, default=False),
        ],
    )
