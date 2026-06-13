"""
Heartbeatシステム - ランナー

asyncioバックグラウンドタスクで定期的にHeartbeatを実行し、
LLMで条件を評価して必要な場合に通知する。
"""
import asyncio
import inspect
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

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
        self._heartbeat_timeout_seconds = float(
            os.getenv("AOITALK_HEARTBEAT_TIMEOUT_SECONDS", "60")
        )

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
        self._defer_initial_runs()
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
        return await self._execute_heartbeat_with_timeout(heartbeat, force=True)

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

            await self._execute_heartbeat_with_timeout(heartbeat)

    def _defer_initial_runs(self) -> None:
        """Avoid running all configured heartbeats during API startup."""
        now = datetime.now().timestamp()
        registry = get_heartbeat_registry()
        for heartbeat in registry.get_enabled():
            self._last_run.setdefault(heartbeat.name, now)

    def _timeout_result(
        self, heartbeat: HeartbeatDefinition, *, force: bool = False
    ) -> Dict[str, Any]:
        now = datetime.now()
        if not force:
            self._last_run[heartbeat.name] = now.timestamp()
        result = {
            "heartbeat_name": heartbeat.name,
            "executed_at": now.isoformat(),
            "status": "timeout",
            "response": (
                f"Heartbeat timed out after {self._heartbeat_timeout_seconds:.1f}s"
            ),
            "is_alert": False,
            "action_results": [],
        }
        self._last_results[heartbeat.name] = result
        return result

    async def _execute_heartbeat_with_timeout(
        self, heartbeat: HeartbeatDefinition, force: bool = False
    ) -> Dict[str, Any]:
        try:
            return await asyncio.wait_for(
                self._execute_heartbeat(heartbeat, force=force),
                timeout=self._heartbeat_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.error(
                "[HeartbeatRunner] %s timed out after %.1fs",
                heartbeat.name,
                self._heartbeat_timeout_seconds,
            )
            return self._timeout_result(heartbeat, force=force)

    def _is_local_openai_compatible_client(self) -> bool:
        if self._llm_client is None:
            return False
        module_name = type(self._llm_client).__module__
        return "openai_compatible_local" in module_name

    async def _is_llm_ready_for_heartbeat(self) -> bool:
        if not self._is_local_openai_compatible_client():
            return True

        health_check = getattr(self._llm_client, "health_check", None)
        if not callable(health_check):
            return True

        try:
            if inspect.iscoroutinefunction(health_check):
                result = await health_check()
            else:
                result = await asyncio.to_thread(health_check)
        except Exception as exc:
            logger.warning("[HeartbeatRunner] LLM health check failed: %s", exc)
            return False

        if isinstance(result, dict):
            return bool(result.get("ok", False))
        return bool(result)

    def _llm_unavailable_result(self, heartbeat: HeartbeatDefinition) -> Dict[str, Any]:
        now = datetime.now()
        self._last_run[heartbeat.name] = now.timestamp()
        result = {
            "heartbeat_name": heartbeat.name,
            "executed_at": now.isoformat(),
            "status": "llm_unavailable",
            "response": "Local LLM is not ready; heartbeat skipped.",
            "is_alert": False,
            "action_results": [],
        }
        self._last_results[heartbeat.name] = result
        return result

    async def _get_project_contexts(self) -> List[Tuple[str, Dict[str, Any]]]:
        """DBから案件ファイラーを持つ全プロジェクトのコンテキストを取得"""
        try:
            from sqlalchemy import select
            from ..memory.database import get_database_manager
            from ..memory.models import Project
            from ..services.project_context import build_project_context

            db_manager = get_database_manager()
            session = await db_manager.get_session()
            try:
                result = await session.execute(select(Project))
                projects = result.scalars().all()
            finally:
                await session.close()

            contexts = []
            for p in projects:
                ctx = build_project_context(p)
                if ctx and (ctx.get("project_storage_path") or ctx.get("workspace_root")):
                    contexts.append((getattr(p, "name", "unknown"), ctx))
            return contexts
        except Exception as e:
            logger.error(f"[HeartbeatRunner] プロジェクトコンテキスト取得エラー: {e}")
            return []

    async def _execute_heartbeat(self, heartbeat: HeartbeatDefinition, force: bool = False) -> Dict[str, Any]:
        """Heartbeatを実行（プロジェクトごとにコンテキスト付きで評価）"""
        now = datetime.now()
        result = {
            "heartbeat_name": heartbeat.name,
            "executed_at": now.isoformat(),
            "status": "skipped",
            "response": None,
            "is_alert": False,
            "action_results": [],
        }

        if not self._llm_client:
            result["status"] = "no_llm_client"
            self._last_results[heartbeat.name] = result
            return result

        if not force and not await self._is_llm_ready_for_heartbeat():
            return self._llm_unavailable_result(heartbeat)

        if not force and not self._is_in_active_hours(heartbeat):
            result["status"] = "outside_active_hours"
            self._last_results[heartbeat.name] = result
            return result

        # 案件ファイラーを持つプロジェクト一覧を取得
        project_contexts = await self._get_project_contexts()

        if not project_contexts:
            # プロジェクトなし → コンテキストなしで実行（フォールバック）
            return await self._execute_heartbeat_single(heartbeat, result, now)

        # プロジェクトごとにチェックリストを実行
        all_responses = []
        any_alert = False

        for project_name, project_context in project_contexts:
            try:
                from ..services.project_context import (
                    format_project_context_for_prompt,
                    set_runtime_project_context,
                    reset_runtime_project_context,
                )

                token = set_runtime_project_context(project_context)
                try:
                    context_block = format_project_context_for_prompt(project_context)
                    prompt = HEARTBEAT_PROMPT_TEMPLATE.format(checklist=heartbeat.checklist)
                    full_prompt = f"{context_block}\n\n{prompt}"

                    response = await self._llm_client.generate_response_async(full_prompt)
                    is_ok = self._is_heartbeat_ok(response)

                    if not is_ok:
                        any_alert = True
                        all_responses.append(f"[{project_name}] {response}")

                    logger.info(
                        f"[HeartbeatRunner] {heartbeat.name} ({project_name}): "
                        f"{'OK' if is_ok else 'ALERT'}"
                    )
                finally:
                    reset_runtime_project_context(token)
            except Exception as e:
                all_responses.append(f"[{project_name}] エラー: {e}")
                any_alert = True
                logger.error(
                    f"[HeartbeatRunner] {heartbeat.name} ({project_name}) 実行エラー: {e}"
                )

        self._last_run[heartbeat.name] = now.timestamp()

        if any_alert:
            combined = "\n\n".join(all_responses)
            result["status"] = "alert"
            result["response"] = combined
            result["is_alert"] = True
            if heartbeat.notify_channel == "websocket":
                await self._notify(heartbeat, combined, now)
        else:
            result["status"] = "ok"
            result["response"] = HEARTBEAT_OK

        result["action_results"] = await self._run_actions(heartbeat, result)
        self._last_results[heartbeat.name] = result
        return result

    async def _execute_heartbeat_single(
        self, heartbeat: HeartbeatDefinition, result: Dict[str, Any], now: datetime
    ) -> Dict[str, Any]:
        """プロジェクトコンテキストなしでHeartbeatを実行（フォールバック）"""
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

        result["action_results"] = await self._run_actions(heartbeat, result)
        self._last_results[heartbeat.name] = result
        return result

    async def _run_actions(
        self, heartbeat: HeartbeatDefinition, result: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Heartbeatに紐づくアクションを実行する。

        action:
          type: run_script | run_skill | webhook | create_task | notify
          run_on: alert | ok | always (default: alert)
          config: アクション固有設定
        """
        action_results: List[Dict[str, Any]] = []
        for action in heartbeat.actions or []:
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("type") or "").strip()
            run_on = str(action.get("run_on") or "alert").strip().lower()
            if not action_type:
                continue
            if run_on != "always" and run_on != result.get("status"):
                continue

            config = action.get("config") if isinstance(action.get("config"), dict) else {}
            started = datetime.utcnow().isoformat()
            try:
                output = await self._execute_action(action_type, config, result)
                action_results.append(
                    {
                        "type": action_type,
                        "status": "ok",
                        "started_at": started,
                        "result": output,
                    }
                )
            except Exception as exc:
                logger.error(
                    "[HeartbeatRunner] action failed: heartbeat=%s type=%s error=%s",
                    heartbeat.name,
                    action_type,
                    exc,
                )
                action_results.append(
                    {
                        "type": action_type,
                        "status": "error",
                        "started_at": started,
                        "error": str(exc),
                    }
                )
        return action_results

    async def _execute_action(
        self, action_type: str, config: Dict[str, Any], result: Dict[str, Any]
    ) -> Dict[str, Any]:
        if action_type == "run_script":
            return await self._action_run_script(config)
        if action_type == "run_skill":
            return await self._action_run_skill(config, result)
        if action_type == "webhook":
            return await self._action_webhook(config, result)
        if action_type == "create_task":
            return await self._action_create_task(config, result)
        if action_type == "notify":
            message = str(config.get("message") or result.get("response") or "")
            await self._notify(
                HeartbeatDefinition(
                    name=str(config.get("name") or "heartbeat-action"),
                    description="",
                    checklist="",
                ),
                message,
                datetime.utcnow(),
            )
            return {"status": "sent"}
        raise ValueError(f"Unsupported heartbeat action type: {action_type}")

    async def _action_run_script(self, config: Dict[str, Any]) -> Dict[str, Any]:
        command = config.get("command")
        if not command:
            raise ValueError("run_script requires command")
        timeout = int(config.get("timeout_seconds") or 300)
        cwd = str(config.get("cwd") or os.getcwd())
        env = os.environ.copy()
        extra_env = config.get("env")
        if isinstance(extra_env, dict):
            env.update({str(k): str(v) for k, v in extra_env.items()})

        if isinstance(command, list):
            proc = await asyncio.create_subprocess_exec(
                *[str(part) for part in command],
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                str(command),
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"script timed out after {timeout}s")
        return {
            "returncode": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[-4000:],
            "stderr": stderr.decode("utf-8", errors="replace")[-4000:],
        }

    async def _action_run_skill(
        self, config: Dict[str, Any], result: Dict[str, Any]
    ) -> Dict[str, Any]:
        skill_name = str(config.get("skill_name") or "")
        if not skill_name:
            raise ValueError("run_skill requires skill_name")
        input_text = str(config.get("input") or result.get("response") or "")
        from ..skills.executor import invoke_skill

        rendered = invoke_skill(skill_name, input_text)
        return {"skill_name": skill_name, "rendered": rendered}

    async def _action_webhook(
        self, config: Dict[str, Any], result: Dict[str, Any]
    ) -> Dict[str, Any]:
        import httpx

        url = str(config.get("url") or "")
        if not url:
            raise ValueError("webhook requires url")
        method = str(config.get("method") or "POST").upper()
        payload = config.get("payload")
        if payload is None:
            payload = {
                "heartbeat": result.get("heartbeat_name"),
                "status": result.get("status"),
                "response": result.get("response"),
            }
        async with httpx.AsyncClient(timeout=float(config.get("timeout_seconds") or 30)) as client:
            response = await client.request(method, url, json=payload)
            response.raise_for_status()
        return {"status_code": response.status_code}

    async def _action_create_task(
        self, config: Dict[str, Any], result: Dict[str, Any]
    ) -> Dict[str, Any]:
        import uuid
        from ..memory.database import get_database_manager
        from ..services.task_management_service import TaskManagementService

        user_id = config.get("user_id")
        if not user_id:
            raise ValueError("create_task requires user_id")
        title = str(config.get("title") or f"Heartbeat alert: {result.get('heartbeat_name')}")
        description = str(config.get("description") or result.get("response") or "")
        project_id = config.get("project_id")

        db_manager = get_database_manager()
        session = await db_manager.get_session()
        try:
            service = TaskManagementService(broadcaster=self._broadcast_fn)
            task = await service.create_task(
                session,
                user_id=uuid.UUID(str(user_id)),
                title=title,
                description=description,
                project_id=uuid.UUID(str(project_id)) if project_id else None,
                status=str(config.get("status") or "todo"),
                priority=config.get("priority"),
                task_metadata={
                    "source": "heartbeat",
                    "heartbeat_name": result.get("heartbeat_name"),
                },
            )
            return {"task_id": str(task.get("id")), "title": task.get("title")}
        finally:
            await session.close()

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
