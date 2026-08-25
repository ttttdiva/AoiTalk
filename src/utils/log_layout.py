"""AoiTalk ログディレクトリレイアウトと旧パスからの移行。"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class LogLayout:
    """プロジェクトルートから logs/ 配下の正本パスを解決する。"""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.logs_root = self.project_root / "logs"

    @property
    def app_dir(self) -> Path:
        return self.logs_root / "app"

    @property
    def web_dir(self) -> Path:
        return self.logs_root / "web"

    @property
    def models_dir(self) -> Path:
        return self.logs_root / "models"

    @property
    def startup_dir(self) -> Path:
        return self.logs_root / "startup"

    @property
    def desktop_dir(self) -> Path:
        return self.logs_root / "desktop"

    @property
    def ops_dir(self) -> Path:
        return self.logs_root / "ops"

    @property
    def discord_dir(self) -> Path:
        return self.logs_root / "discord"

    def ensure_dirs(self) -> None:
        """ログ用サブディレクトリを作成する。"""
        for directory in (
            self.app_dir,
            self.web_dir,
            self.models_dir,
            self.startup_dir,
            self.desktop_dir,
            self.ops_dir,
            self.discord_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def app_log_path(self, timestamp: str | None = None) -> Path:
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.app_dir / f"app_{timestamp}.log"

    def app_latest_pointer(self) -> Path:
        return self.app_dir / "latest.log"

    def frontend_log_path(self) -> Path:
        return self.web_dir / "frontend.log"

    def caddy_access_log(self) -> Path:
        return self.web_dir / "caddy-access.log"

    def caddy_runtime_log(self) -> Path:
        return self.web_dir / "caddy-runtime.log"

    def llama_cpp_log(self) -> Path:
        return self.models_dir / "llama_cpp.log"

    def exo_log(self) -> Path:
        return self.models_dir / "exo.log"

    def mlx_lm_log(self) -> Path:
        return self.models_dir / "mlx_lm.log"

    def sglang_server_log(self) -> Path:
        return self.models_dir / "sglang_server.log"

    def sglang_server_error_log(self) -> Path:
        return self.models_dir / "sglang_server_error.log"

    def desktop_backend_log(self) -> Path:
        return self.desktop_dir / "desktop-tauri-backend.log"

    def ddns_update_log(self) -> Path:
        return self.ops_dir / "ddns_update.log"

    def startup_timing_path(self, run_id: str) -> Path:
        return self.startup_dir / f"startup_timing_{run_id}.jsonl"

    def migrate_legacy_paths(self) -> None:
        """旧 logs/ 直下・services/ のファイルを新ディレクトリへ移す（存在時のみ）。"""
        self.ensure_dirs()
        services_dir = self.logs_root / "services"

        for legacy_app in self.logs_root.glob("app_*.log"):
            _move_file_fail_open(legacy_app, self.app_dir / legacy_app.name)

        for legacy_startup in self.logs_root.glob("startup_timing_*.jsonl"):
            _move_file_fail_open(
                legacy_startup, self.startup_dir / legacy_startup.name
            )

        if services_dir.is_dir():
            for name in ("frontend.log",):
                _move_file_fail_open(
                    services_dir / name, self.web_dir / name
                )
            for legacy_frontend in services_dir.glob("frontend-*.log"):
                _move_file_fail_open(
                    legacy_frontend, self.web_dir / legacy_frontend.name
                )
            for legacy_caddy in services_dir.glob("caddy-access*"):
                _move_file_fail_open(
                    legacy_caddy, self.web_dir / legacy_caddy.name
                )
            for legacy_caddy in services_dir.glob("caddy-runtime*"):
                _move_file_fail_open(
                    legacy_caddy, self.web_dir / legacy_caddy.name
                )
            for model_name in (
                "llama_cpp.log",
                "exo.log",
                "mlx_lm.log",
            ):
                _move_file_fail_open(
                    services_dir / model_name,
                    self.models_dir / model_name,
                )
            for legacy_model in services_dir.glob("llama_cpp*.log"):
                if legacy_model.name == "llama_cpp.log":
                    continue
                _move_file_fail_open(
                    legacy_model, self.models_dir / legacy_model.name
                )
            for legacy_model in services_dir.glob("exo*.log"):
                if legacy_model.name == "exo.log":
                    continue
                _move_file_fail_open(
                    legacy_model, self.models_dir / legacy_model.name
                )
            for legacy_model in services_dir.glob("mlx_lm*.log"):
                if legacy_model.name == "mlx_lm.log":
                    continue
                _move_file_fail_open(
                    legacy_model, self.models_dir / legacy_model.name
                )

        for legacy_sglang in (
            self.logs_root / "sglang_server.log",
            self.logs_root / "sglang_server_error.log",
        ):
            if legacy_sglang.is_file():
                _move_file_fail_open(
                    legacy_sglang,
                    self.models_dir / legacy_sglang.name,
                )

        _move_file_fail_open(
            self.logs_root / "desktop-tauri-backend.log",
            self.desktop_backend_log(),
        )
        for legacy_desktop in self.logs_root.glob("desktop-tauri-backend*.log"):
            if legacy_desktop.name == "desktop-tauri-backend.log":
                continue
            _move_file_fail_open(
                legacy_desktop,
                self.desktop_dir / legacy_desktop.name,
            )

        _move_file_fail_open(
            self.logs_root / "ddns_update.log",
            self.ddns_update_log(),
        )
        _move_file_fail_open(
            self.logs_root / "ddns_update.log.1",
            self.ops_dir / "ddns_update.log.1",
        )


def _move_file_fail_open(source: Path, destination: Path) -> None:
    if not source.is_file():
        return
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return
        shutil.move(str(source), str(destination))
    except Exception as exc:
        logger.warning(
            "旧ログパスの移行に失敗しました（続行します）: %s -> %s (%s)",
            source,
            destination,
            exc,
        )


def get_log_layout(project_root: Path | None = None) -> LogLayout:
    if project_root is None:
        project_root = Path(__file__).resolve().parents[2]
    return LogLayout(project_root)
