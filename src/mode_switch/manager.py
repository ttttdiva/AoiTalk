"""Centralized mode switch orchestration"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Set


class ModeSwitchError(Exception):
    """Raised when mode switching cannot proceed"""


@dataclass
class ModeSwitchRequest:
    target_mode: str
    source: str
    actor_id: Optional[str]
    requested_at: float

    def to_payload(self) -> Dict[str, Any]:
        payload = asdict(self)
        return payload


class ModeSwitchManager:
    """Singleton-like manager coordinating app restarts between modes"""

    VALID_MODES: Set[str] = {"terminal", "voice_chat", "discord"}

    def __init__(self) -> None:
        self._config = None
        self._config_path: Optional[Path] = None
        self._current_mode: str = "terminal"
        self._next_mode: Optional[str] = None
        self._lock = asyncio.Lock()
        self._restart_task: Optional[asyncio.Task] = None
        self._last_request: Optional[ModeSwitchRequest] = None
        self._restart_delay: float = 3.0
        self._python_executable: str = sys.executable
        self._entrypoint: Optional[Path] = None
        self._allowed_discord_users: Set[str] = set()
        self._enabled_sources: Set[str] = {"web_ui", "discord"}
        self._restart_callback = None

    def configure(
        self,
        config,
        *,
        entrypoint: Optional[Path] = None,
        python_executable: Optional[str] = None,
        enabled_sources: Optional[Sequence[str]] = None,
    ) -> None:
        """Attach configuration and runtime metadata"""
        self._config = config
        self._config_path = Path(getattr(config, "config_path"))
        self._current_mode = config.get("mode", "terminal")
        settings = config.get("mode_switch", {})
        self._restart_delay = float(settings.get("restart_delay_seconds", 3))
        allowed_ids = settings.get("allowed_discord_user_ids", [])
        self._allowed_discord_users = {str(uid) for uid in allowed_ids}
        if python_executable:
            self._python_executable = python_executable
        if entrypoint:
            self._entrypoint = Path(entrypoint)
        else:
            self._entrypoint = Path(__file__).resolve().parents[1] / "main.py"
        if enabled_sources is not None:
            self._enabled_sources = set(enabled_sources)

    def get_status(self) -> Dict[str, Any]:
        """Expose current switching state"""
        return {
            "current_mode": self._current_mode,
            "next_mode": self._next_mode,
            "pending_request": self._last_request.to_payload() if self._last_request else None,
            "restart_in_progress": self._restart_task is not None,
            "allowed_modes": sorted(self.VALID_MODES),
            "restart_delay_seconds": self._restart_delay,
        }

    def is_discord_actor_allowed(self, actor_id: int | str) -> bool:
        return str(actor_id) in self._allowed_discord_users

    def set_restart_callback(self, callback) -> None:
        """Override restart handler (useful for testing)"""
        self._restart_callback = callback

    async def request_switch(self, target_mode: str, *, source: str, actor_id: Optional[str] = None) -> Dict[str, Any]:
        """Request switching to a different mode"""
        target = (target_mode or "").strip()
        if not target:
            raise ModeSwitchError("切り替え先モードを指定してください")
        if target not in self.VALID_MODES:
            raise ModeSwitchError(f"未対応のモードです: {target}")
        if source not in self._enabled_sources:
            raise ModeSwitchError("このインターフェースからはモード切り替えできません")

        async with self._lock:
            if target == self._current_mode:
                raise ModeSwitchError("既にこのモードで動作しています")
            if self._restart_task and not self._restart_task.done():
                raise ModeSwitchError("別のモード切り替えが進行中です")
            if source == "discord":
                if not actor_id:
                    raise ModeSwitchError("実行ユーザーを確認できませんでした")
                if not self.is_discord_actor_allowed(actor_id):
                    raise ModeSwitchError("この操作を行う権限がありません")

            # Persist upcoming mode so restart uses correct value even without CLI args
            self._persist_mode_setting(target)
            self._next_mode = target
            self._last_request = ModeSwitchRequest(
                target_mode=target,
                source=source,
                actor_id=str(actor_id) if actor_id else None,
                requested_at=time.time(),
            )
            self._restart_task = asyncio.create_task(self._restart_after_delay(target))

        return {
            "message": f"{target} モードへの切り替えを開始します",
            "restart_delay_seconds": self._restart_delay,
            "next_mode": target,
        }

    def _persist_mode_setting(self, target_mode: str) -> None:
        if not self._config_path or not self._config_path.exists():
            raise ModeSwitchError("config.yaml が見つかりません")

        try:
            original_lines = self._config_path.read_text(encoding="utf-8").splitlines(True)
        except Exception as exc:
            raise ModeSwitchError(f"設定ファイルの読み込みに失敗しました: {exc}") from exc

        updated_lines = []
        replaced = False
        for line in original_lines:
            stripped = line.lstrip()
            is_top_level = len(line) == len(stripped)
            if stripped.startswith("mode:") and not replaced and is_top_level:
                indent = line[: len(line) - len(stripped)]
                comment = ""
                if "#" in stripped:
                    comment = stripped[stripped.index("#") :].rstrip()
                new_line = f"{indent}mode: {target_mode}"
                if comment:
                    new_line += f" {comment}"
                updated_lines.append(new_line + "\n")
                replaced = True
            else:
                updated_lines.append(line)

        if not replaced:
            updated_lines.insert(0, f"mode: {target_mode}\n")

        backup_path = self._config_path.with_suffix(".mode_switch.bak")
        backup_path.write_text("".join(original_lines), encoding="utf-8")
        self._config_path.write_text("".join(updated_lines), encoding="utf-8")

    async def _restart_after_delay(self, target_mode: str) -> None:
        try:
            await asyncio.sleep(self._restart_delay)
            handler = self._restart_callback or self._perform_restart
            result = handler(target_mode)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            return

    def _perform_restart(self, target_mode: str) -> None:
        entrypoint = self._entrypoint or (Path(__file__).resolve().parents[1] / "main.py")
        python_exec = self._python_executable or sys.executable
        command = [python_exec, str(entrypoint), "--mode", target_mode]
        print(f"\n🔁 {target_mode} モードへ再起動します: {' '.join(command)}")
        os.execv(python_exec, command)


mode_switch_manager = ModeSwitchManager()
