"""
Heartbeatシステム - ランナー

asyncioバックグラウンドタスクで定期的にHeartbeatを実行し、
LLMで条件を評価して必要な場合に通知する。
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, Optional

from .models import HeartbeatDefinition
from .registry import get_heartbeat_registry

logger = logging.getLogger(__name__)

HEARTBEAT_OK = "HEARTBEAT_OK"

HEARTBEAT_PROMPT_TEMPLATE = """以下のチェックリストを確認してください。
すべて問題なければ「HEARTBEAT_OK」とだけ回答してください。
問題がある場合は、問題の内容を簡潔に報告してください。「HEARTBEAT_OK」は含めないでください。

チェックリスト:
{checklist}"""


class HeartbeatRunner:
    """Heartbeatのバックグラウンド実行を管理"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._llm_client = None
        self._broadcast_fn: Optional[Callable[[Dict[str, Any]], Coroutine]] = None
        self._last_run: Dict[str, float] = {}
        self._last_results: Dict[str, Dict[str, Any]] = {}
        self._check_interval = 60  # メインループのチェック間隔（秒）

    def set_llm_client(self, llm_client) -> None:
        """LLMクライアントを設定（後から注入）"""
        self._llm_client = llm_client

    def set_broadcast_fn(self, fn: Callable[[Dict[str, Any]], Coroutine]) -> None:
        """WebSocketブロードキャスト関数を設定"""
        self._broadcast_fn = fn

    async def start(self) -> None:
        """バックグラウンドタスクを開始"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("[HeartbeatRunner] 開始")

    async def stop(self) -> None:
        """バックグラウンドタスクを停止"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("[HeartbeatRunner] 停止")

    async def trigger(self, name: str) -> Optional[Dict[str, Any]]:
        """指定したHeartbeatを即時実行"""
        registry = get_heartbeat_registry()
        heartbeat = registry.get(name)
        if not heartbeat:
            return None
        return await self._execute_heartbeat(heartbeat, force=True)

    def get_status(self) -> Dict[str, Any]:
        """Runner全体のステータスを返す"""
        registry = get_heartbeat_registry()
        return {
            "running": self._running,
            "llm_client_set": self._llm_client is not None,
            "total_heartbeats": len(registry),
            "enabled_heartbeats": len(registry.get_enabled()),
            "last_results": self._last_results,
        }

    async def _run_loop(self) -> None:
        """メインループ: 定期的にHeartbeatをチェック"""
        logger.info("[HeartbeatRunner] バックグラウンドループ開始")
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[HeartbeatRunner] tickエラー: {e}")

            try:
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        """1回のチェックサイクル"""
        if not self._llm_client:
            return

        registry = get_heartbeat_registry()
        enabled = registry.get_enabled()
        if not enabled:
            return

        now = datetime.now().timestamp()

        for heartbeat in enabled:
            last = self._last_run.get(heartbeat.name, 0)
            interval_seconds = heartbeat.interval_minutes * 60

            if now - last < interval_seconds:
                continue

            if not self._is_in_active_hours(heartbeat):
                continue

            await self._execute_heartbeat(heartbeat)

    async def _execute_heartbeat(self, heartbeat: HeartbeatDefinition, force: bool = False) -> Dict[str, Any]:
        """Heartbeatを1つ実行"""
        now = datetime.now()
        result = {
            "heartbeat_name": heartbeat.name,
            "executed_at": now.isoformat(),
            "status": "skipped",
            "response": None,
            "is_alert": False,
        }

        if not self._llm_client:
            result["status"] = "no_llm_client"
            self._last_results[heartbeat.name] = result
            return result

        if not force and not self._is_in_active_hours(heartbeat):
            result["status"] = "outside_active_hours"
            self._last_results[heartbeat.name] = result
            return result

        prompt = HEARTBEAT_PROMPT_TEMPLATE.format(checklist=heartbeat.checklist)

        try:
            response = await self._llm_client.generate_response_async(prompt)
            self._last_run[heartbeat.name] = now.timestamp()

            is_ok = self._is_heartbeat_ok(response)
            result["status"] = "ok" if is_ok else "alert"
            result["response"] = response
            result["is_alert"] = not is_ok

            if not is_ok and heartbeat.notify_channel == "websocket":
                await self._notify(heartbeat, response, now)

            logger.info(
                f"[HeartbeatRunner] {heartbeat.name}: "
                f"{'OK' if is_ok else 'ALERT'}"
            )
        except Exception as e:
            result["status"] = "error"
            result["response"] = str(e)
            logger.error(f"[HeartbeatRunner] {heartbeat.name} 実行エラー: {e}")

        self._last_results[heartbeat.name] = result
        return result

    def _is_heartbeat_ok(self, response: str) -> bool:
        """レスポンスがHEARTBEAT_OKかどうか判定"""
        stripped = response.strip()
        return stripped.startswith(HEARTBEAT_OK) or stripped.endswith(HEARTBEAT_OK)

    def _is_in_active_hours(self, heartbeat: HeartbeatDefinition) -> bool:
        """active_hours内かどうか判定"""
        if not heartbeat.active_hours:
            return True

        start_str = heartbeat.active_hours.get("start")
        end_str = heartbeat.active_hours.get("end")
        if not start_str or not end_str:
            return True

        tz_name = heartbeat.active_hours.get("timezone")
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(tz_name) if tz_name else None
        except Exception:
            tz = None

        now = datetime.now(tz)
        current_time = now.strftime("%H:%M")
        return start_str <= current_time < end_str

    async def _notify(self, heartbeat: HeartbeatDefinition, message: str, timestamp: datetime) -> None:
        """WebSocketでアラートを通知"""
        if not self._broadcast_fn:
            return

        payload = {
            "type": "heartbeat_alert",
            "data": {
                "heartbeat_name": heartbeat.name,
                "message": message,
                "timestamp": timestamp.isoformat(),
            },
        }

        try:
            await self._broadcast_fn(payload)
        except Exception as e:
            logger.error(f"[HeartbeatRunner] 通知エラー: {e}")


# モジュールレベルのシングルトン
_global_runner = HeartbeatRunner()


def get_heartbeat_runner() -> HeartbeatRunner:
    return _global_runner
