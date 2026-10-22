"""Windows observation and Computer Use; device routing stays server-owned."""
from typing import Any
from .core import ToolDefinition, ToolParam


def build_computer_use_tool(config: Any, *, client=None):
    async def execute(goal='', action='observe', device_id='', text='', keys=None,
                      x=None, y=None, x2=None, y2=None, window_id=None, amount=-3, use_vision=False, observation_id=None, target=None):
        from ..services.pc_bridge_service import resolve_control_bridge
        from ..services.pc_bridge_store import PcBridgeError
        from ..services.computer_use_service import ComputerUseService
        try:
            bridge = await resolve_control_bridge(device_id or None)
        except PcBridgeError as exc:
            return {'success': False, 'reason': str(exc)}
        params = {k: v for k, v in dict(text=text, keys=keys, x=x, y=y, x2=x2, y2=y2,
                                       window_id=window_id, amount=amount, observation_id=observation_id, target=target).items() if v is not None}
        return await ComputerUseService(getattr(client, 'config', None) or config, client, bridge).run(
            goal=goal, action=action, params=params, use_vision=use_vision)

    return ToolDefinition(
        name='computer_use',
        description='設定のPC接続で選択したPCのWindowsを操作する。goalで目的を渡すと画面観測と操作を繰り返す。'
                    'ブラウザDOMはbrowser_agent、Windowsアプリや座標操作はこのツール。'
                    '画面やウィンドウの読み取りだけならcomputer_observeを使う。'
                    'goal省略時はactionを1回実行。observeはウィンドウ・UI要素・画面のURLを返す。'
                    'use_vision=trueは既存の画像認識モデルも利用する。Jevは必須ではない。'
                    '接続PC未選択・切断時は停止しサーバーPCには切り替えない。',
        function=execute, is_async=True, owner='computer_use', side_effect='writes', risk='normal',
        supports_parallel=False, timeout_seconds=630,
        parameters=[
            ToolParam('goal', 'string', '実行したいWindows作業。省略時は単発操作', required=False, default=''),
            ToolParam('action', 'string', '単発操作', required=False, default='observe', enum=['observe','screenshot','windows','focus','click','double_click','right_click','move','drag','press','type','scroll']),
            ToolParam('device_id', 'string', '省略時は設定で選択済みのPC。別の登録PCのIDも指定可', required=False, default=''),
            ToolParam('observation_id', 'string', '直前のobserveのID。対象PC・前面・配置を操作前に検証する', required=False),
            ToolParam('target', 'string', '直前のobserveで返されたUIA対象ref。座標を推測せず対象を検証する', required=False),
            ToolParam('text', 'string', 'typeで入力する文字列', required=False, default=''),
            ToolParam('keys', 'array', 'pressで同時押しするキー。例 CTRL,A', required=False, schema={'type':'array','items':{'type':'string'}}),
            *[ToolParam(name, 'integer', '実デスクトップ座標／ウィンドウID', required=False) for name in ('x','y','x2','y2','window_id')],
            ToolParam('amount', 'integer', 'scroll量。負数で下方向', required=False, default=-3),
            ToolParam('use_vision', 'boolean', '既存の画像認識モデルで画面も解析する', required=False, default=False),
        ],
    )


def build_computer_observe_tool(config: Any, *, client=None):
    """Expose only bounded observations while retaining the shared PC routing.

    Keep ``computer_use`` mutation-capable: even an apparently read-only goal
    can enter its autonomous action loop. This separate, closed schema cannot
    accept a goal or action parameters, and validates the action at execution
    time as well as advertising an enum to providers.
    """
    computer_use = build_computer_use_tool(config, client=client)

    async def execute(action='observe', device_id='', use_vision=False):
        if action not in ('observe', 'screenshot', 'windows'):
            return {'success': False, 'reason': 'computer_observe_action_invalid'}
        return await computer_use.execute_async(
            goal='', action=action, device_id=device_id, use_vision=use_vision,
        )

    return ToolDefinition(
        name='computer_observe',
        description='設定のPC接続で選択したPCのWindowsを読み取り専用で1回観測する。'
                    'observeはウィンドウ・UI要素・画面のURL、screenshotは画面、windowsはウィンドウ一覧を返す。'
                    '読み取り専用の依頼にはこのツールを使う。goal、自動操作、前面化、クリック、入力は受け付けない。'
                    '操作が必要な依頼にはcomputer_useを使う。'
                    '接続PC未選択・切断時は停止しサーバーPCには切り替えない。',
        function=execute, is_async=True, owner='computer_use', side_effect='none', risk='normal',
        supports_parallel=False, timeout_seconds=630,
        parameters=[
            ToolParam('action', 'string', '読み取り操作', required=False, default='observe',
                      enum=['observe', 'screenshot', 'windows']),
            ToolParam('device_id', 'string', '省略時は設定で選択済みのPC。別の登録PCのIDも指定可', required=False, default=''),
            ToolParam('use_vision', 'boolean', '既存の画像認識モデルで画面も解析する', required=False, default=False),
        ],
    )
