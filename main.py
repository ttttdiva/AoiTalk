#!/usr/bin/env python3
"""
AoiTalk Voice Assistant - Refactored Main Entry Point

This is the main entry point for the AoiTalk Voice Assistant Framework.
The core functionality has been refactored into modular components in src/assistant/.
"""

from __future__ import annotations

import atexit
import asyncio
import sys
import signal
import os
import argparse
import logging
import warnings
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from typing import TYPE_CHECKING, Optional, Union, Any
from argparse import Namespace

from src.utils.startup_timing import get_startup_timer
from src.utils.startup_console import safe_startup_reason, startup_console
from src.utils.logging_config import FILE_ONLY_LOG_EXTRA


_startup_timer = get_startup_timer()
logger = logging.getLogger(__name__)
_main_import_phase = _startup_timer.start_phase("startup.import.main")
_main_import_phase_finished = False


def _finish_main_import_phase(status: str = "ok") -> None:
    """Close the import span even when module initialization aborts early.

    Most startup spans use a context manager, but the module-level imports
    necessarily execute before ``main()`` can establish one.  Registering a
    fail-open atexit fallback keeps the diagnostic span closed on import-time
    exceptions without changing the exception or cleanup semantics.
    """

    global _main_import_phase_finished
    if _main_import_phase_finished:
        return
    _main_import_phase_finished = True
    _startup_timer.finish_phase(_main_import_phase, status=status)


atexit.register(_finish_main_import_phase, "error")

# Windows cp932環境でUnicode絵文字がprint時にクラッシュする問題を回避
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Load environment variables from .env file
load_dotenv()

# Initialize Feature Flags system
from src.features import Features

# Suppress known third-party import warnings on the normal operator path.
# ``AOITALK_DEBUG=true`` intentionally leaves warnings visible as part of the
# verbose diagnostics contract.
_debug_mode_requested = os.getenv("AOITALK_DEBUG", "").strip().lower() == "true"
if not _debug_mode_requested:
    # This warning comes from external libraries (transformers, etc.).
    warnings.filterwarnings("ignore", category=FutureWarning, message=".*weights_only.*")
    # google-generativeai emits a multi-line package deprecation notice during
    # import.  The warning is formatted with embedded newlines, so use a
    # DOTALL expression; the default regex ``.`` otherwise stops at the first
    # blank line and misses it.
    warnings.filterwarnings(
        "ignore",
        category=FutureWarning,
        message=r"(?s).*google\.generativeai.*",
    )
    # Depending on the installed release the warning's originating module
    # varies between the package and our lazy image tool.
    warnings.filterwarnings(
        "ignore",
        category=FutureWarning,
        module=r"(?:google\.generativeai|src\.tools\.image_generation)(?:\..*)?",
    )

# Apply LD_LIBRARY_PATH fix for Mem0 (Linux only)
# Prepend the required path to fix SQLite issue
if sys.platform.startswith("linux"):
    sqlite_lib_path = '/usr/lib/x86_64-linux-gnu'
    current_ld_path = os.environ.get('LD_LIBRARY_PATH', '')
    if sqlite_lib_path not in current_ld_path:
        os.environ['LD_LIBRARY_PATH'] = f"{sqlite_lib_path}:{current_ld_path}" if current_ld_path else sqlite_lib_path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent))

from src.runtime_features import runtime_feature_manager
from src.bot.service import discord_bot_service
from src.utils.logging_config import setup_default_logging
from src.utils.windows_optimization import apply_windows_optimizations

if TYPE_CHECKING:
    from src.config import Config
    from src.assistant.modes.terminal_mode import TerminalMode
    from src.assistant.modes.voice_chat_mode import VoiceChatMode

    # Keep the precise return type for static checkers without importing the
    # mode modules during normal startup.  At runtime ``AssistantMode`` is
    # intentionally an ``Any`` alias so ``typing.get_type_hints`` can still
    # resolve the future annotation without defeating lazy imports.
    AssistantMode = Union[TerminalMode, VoiceChatMode]
else:
    class _LazyConfigMeta(type):
        """Proxy the historical ``main.Config`` export without eager imports."""

        @staticmethod
        def _real_config():
            from src.config import Config as RealConfig

            return RealConfig

        def __getattr__(cls, name):
            return getattr(cls._real_config(), name)

        def __instancecheck__(cls, instance):
            return isinstance(instance, cls._real_config())

        def __subclasscheck__(cls, subclass):
            return issubclass(subclass, cls._real_config())

    class Config(metaclass=_LazyConfigMeta):
        """Lazy compatibility wrapper for callers importing ``main.Config``."""

        def __new__(cls, *args, **kwargs):
            return cls._real_config()(*args, **kwargs)

    AssistantMode = Any

_finish_main_import_phase()


def create_assistant(config: Config) -> Optional[AssistantMode]:
    """Create assistant based on enabled local adapters.

    Runtime feature flags decide whether the local audio adapter is attached
    to the always-on WebUI runtime.
    
    Args:
        config: Configuration object
        
    Returns:
        Assistant instance
        
    Raises:
        ValueError: If mode is not supported
    """
    if runtime_feature_manager.local_audio_enabled:
        from src.assistant.modes.voice_chat_mode import VoiceChatMode

        return VoiceChatMode(config)
    from src.assistant.modes.terminal_mode import TerminalMode

    return TerminalMode(config)


def _preflight_enterprise_field_crypto() -> None:
    """Resolve the production field-crypto key before starting services.

    Enterprise application/config data is encrypted at rest.  Probe the same
    provider chain used by encrypt/decrypt (KMS/keyring command, explicitly
    allowed test env key, or platform fallback) before any frontend/Caddy or
    assistant process is started so a missing/broken key fails early.
    """

    if not Features.is_enterprise():
        return
    from src.security.field_crypto import get_data_key

    try:
        get_data_key()
    except Exception as exc:
        raise RuntimeError(
            "Enterprise field crypto key readiness failed: "
            f"{safe_startup_reason(exc)}"
        ) from exc


def parse_arguments() -> Namespace:
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description='AoiTalk Voice Assistant')
    parser.add_argument(
        '--skip-services',
        action='store_true',
        help='Frontend/Caddy の起動をスキップ（モード切替時用）'
    )
    return parser.parse_args()


async def main() -> None:
    """Main async function"""
    # Parse command line arguments
    args = parse_arguments()

    startup_console.reset_ready()
    startup_console.stage("AoiTalk を起動しています...")
    startup_console.stage(
        f"[OK] Python {sys.version_info.major}.{sys.version_info.minor}"
    )

    # Establish the application log before invoking platform/database
    # startup helpers.  Their detailed diagnostics stay in the file while
    # startup_console owns the concise operator-facing progress stream.
    debug_mode = os.getenv('AOITALK_DEBUG', '').lower() == 'true'
    from src.utils.log_layout import get_log_layout
    from src.utils.log_housekeeping import run_log_housekeeping

    project_root = Path(__file__).resolve().parent
    layout = get_log_layout(project_root)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = layout.app_log_path(timestamp).resolve()

    with _startup_timer.phase("startup.logging.configure"):
        log_config = setup_default_logging(debug=debug_mode)

    # ログファイルハンドラーを追加
    with _startup_timer.phase("startup.logging.file_handler"):
        log_config.add_file_handler(log_file)

    startup_console.configure(
        detail_log_path=log_file,
        startup_timing_path=_startup_timer.log_path.resolve(),
    )
    os.environ["AOITALK_APP_LOG_PATH"] = str(log_file)
    logger.info(
        "Application startup log file attached: %s",
        log_file,
        extra=FILE_ONLY_LOG_EXTRA,
    )

    try:
        with _startup_timer.phase("startup.features.initialize"):
            Features.initialize()
    except Exception as exc:
        startup_console.error(
            f"Feature initialization failed: {safe_startup_reason(exc)}"
        )
        logger.exception("Feature initialization failed", extra=FILE_ONLY_LOG_EXTRA)
        raise

    # ログディレクトリの作成とファイルログの有効化
    with _startup_timer.phase("startup.logging.directory"):
        layout.migrate_legacy_paths()
        # Keep both streams that this process is about to append to.  A user
        # may configure a fixed startup JSONL path; protecting only the app
        # log would allow retention pruning to unlink that active file before
        # the first timing event is written.
        run_log_housekeeping(
            layout,
            active_paths={log_file, _startup_timer.log_path.resolve()},
        )

    # セッションIDを設定（フィードバック追跡用）
    from src.utils.app_session import set_session_id
    set_session_id(timestamp)
    try:
        layout.app_latest_pointer().write_text(str(log_file.resolve()), encoding="utf-8")
    except Exception as exc:
        logger.debug("Could not update latest app-log pointer: %s", exc, extra=FILE_ONLY_LOG_EXTRA)

    # Windows環境での最適化を適用 after logging is ready
    import platform
    is_windows = platform.system() == "Windows"
    if is_windows:
        startup_console.stage("[START] Windows")
    with _startup_timer.phase("startup.windows.optimizations"):
        apply_windows_optimizations()
    if is_windows:
        startup_console.stage("[OK] Windows")

    # PostgreSQLサービスの起動確認（Windows環境のみ）
    if is_windows:
        startup_console.stage("[START] PostgreSQL")
        from src.utils.windows_optimization import get_windows_optimizer
        optimizer = get_windows_optimizer()
        with _startup_timer.phase("startup.windows.postgresql.ensure"):
            postgresql_ready = bool(optimizer.ensure_postgresql_running())
        # AsyncIOのエラーログ抑制 (ConnectionResetError対策)
        with _startup_timer.phase("startup.windows.asyncio.error_suppression"):
            optimizer.suppress_asyncio_errors()
        if postgresql_ready:
            startup_console.stage("[OK] PostgreSQL")
        else:
            startup_console.warning(
                "PostgreSQLサービスを起動できませんでした。"
                "サービス名/ポートを確認してください。"
            )

    # Fail before database/config/service startup in Enterprise.  Personal
    # installations retain their existing lazy key-provider behavior.
    try:
        with _startup_timer.phase("startup.enterprise.field_crypto_preflight"):
            _preflight_enterprise_field_crypto()
    except Exception as exc:
        startup_console.error(
            f"Enterprise crypto readiness failed: {safe_startup_reason(exc)}"
        )
        logger.exception(
            "Enterprise field crypto preflight failed",
            extra=FILE_ONLY_LOG_EXTRA,
        )
        raise
    
    require_database = os.getenv("AOITALK_REQUIRE_DATABASE", "").lower() in {
        "1", "true", "yes", "on"
    } or Features.is_enterprise()

    # DBをConfig生成より先に確定させる。DB保存設定を正本にするため、
    # Config() が app_config_settings をseed/defaultへフォールバックしないようにする。
    # Enterprise/Dockerではmigration失敗を「メモリ無効」で隠して起動しない。
    use_postgresql = os.getenv("USE_POSTGRESQL", "true").lower() not in {
        "0", "false", "no", "off"
    }
    database_attempted = False
    database_ready = False
    if require_database or use_postgresql:
        startup_console.stage("[START] Database")
        database_attempted = True
        try:
            from src.memory.database import get_database_manager
            with _startup_timer.phase("startup.database.manager"):
                db_manager = get_database_manager()
            with _startup_timer.phase("startup.database.initialize"):
                db_ok = await db_manager.initialize()
            database_ready = db_ok
            if not db_ok:
                detail = getattr(db_manager, "last_error", None)
                message = "PostgreSQLのmigration/接続確認に失敗しました"
                if detail:
                    message += f": {safe_startup_reason(detail, limit=180)}"
                else:
                    message += "。PostgreSQLサービスと接続設定を確認してください"
                if require_database:
                    raise RuntimeError(message)
                startup_console.warning(f"[Memory] {message}。メモリ機能は無効で継続します。")
            else:
                startup_console.stage("[OK] Database")
        except Exception as e:
            if require_database:
                startup_console.error(
                    f"Database readiness failed: {safe_startup_reason(e)}"
                )
                logger.exception("Enterprise database readiness failed", extra=FILE_ONLY_LOG_EXTRA)
                raise RuntimeError(f"Enterprise database readiness failed: {e}") from e
            startup_console.warning(
                f"[Memory] PostgreSQL接続確認でエラー: {safe_startup_reason(e)}"
            )
            logger.warning("PostgreSQL readiness check failed; continuing without memory", extra=FILE_ONLY_LOG_EXTRA)

    # Load configuration only after the optional early DB readiness attempt.
    try:
        with _startup_timer.phase("startup.config.generate"):
            from src.config import Config as ConfigClass

            config = ConfigClass()
            # Config may have reloaded dotenv/profile selectors; refresh the
            # feature snapshot so startup diagnostics reflect the effective
            # Enterprise fail-closed state rather than the pre-config cache.
            Features.reset_cache()
            Features.initialize()
    except Exception as exc:
        startup_console.error(
            f"Config readiness failed: {safe_startup_reason(exc)}"
        )
        logger.exception("Application config generation failed", extra=FILE_ONLY_LOG_EXTRA)
        raise

    # USE_POSTGRESQL=falseでも、設定DBを使うmemory設定が有効なら一度だけ初期化する。
    memory_enabled = config.get('memory', {}).get('enabled', True)
    if memory_enabled and not database_attempted:
        startup_console.stage("[START] Database")
        try:
            from src.memory.database import get_database_manager
            with _startup_timer.phase("startup.database.manager"):
                db_manager = get_database_manager()
            with _startup_timer.phase("startup.database.initialize"):
                database_ready = await db_manager.initialize()
            if not database_ready:
                detail = getattr(db_manager, "last_error", None)
                message = "PostgreSQLのmigration/接続確認に失敗しました"
                if detail:
                    message += f": {safe_startup_reason(detail, limit=180)}"
                else:
                    message += "。PostgreSQLサービスと接続設定を確認してください"
                if require_database:
                    raise RuntimeError(message)
                startup_console.warning(f"[Memory] {message}。メモリ機能は無効で継続します。")
            else:
                startup_console.stage("[OK] Database")
        except Exception as e:
            if require_database:
                startup_console.error(
                    f"Database readiness failed: {safe_startup_reason(e)}"
                )
                logger.exception("Enterprise database readiness failed", extra=FILE_ONLY_LOG_EXTRA)
                raise RuntimeError(f"Enterprise database readiness failed: {e}") from e
            startup_console.warning(
                f"[Memory] PostgreSQL接続確認でエラー: {safe_startup_reason(e)}"
            )
            logger.warning("PostgreSQL readiness check failed; continuing without memory", extra=FILE_ONLY_LOG_EXTRA)

    # Frontend をDB準備後に起動し、Caddy は FastAPI readiness 後に起動する。
    caddy_start_callback = None
    service_cleanup = None
    if not args.skip_services:
        from src.service_manager import (
            get_operator_web_url,
            kill_services,
            start_caddy,
            start_services,
        )
        startup_console.stage("[START] Frontend")
        try:
            with _startup_timer.phase("startup.services.start"):
                start_services(config)
        except Exception as exc:
            startup_console.error(
                f"Frontend startup failed: {safe_startup_reason(exc)}"
            )
            logger.exception("Frontend/service startup failed", extra=FILE_ONLY_LOG_EXTRA)
            raise
        startup_console.stage("[OK] Frontend")
        service_cleanup = kill_services
        atexit.register(service_cleanup)

        def caddy_start_callback(_host: str, ready_port: int):
            caddy_result = start_caddy(config, ready_fastapi_port=ready_port)
            # Return the actual Caddy endpoint so BaseAssistant can expose it
            # in the one-shot operator-facing startup summary.
            if isinstance(caddy_result, str) and caddy_result.strip():
                return caddy_result.strip()
            return get_operator_web_url(config)
    else:
        startup_console.stage("[SKIP] Frontend services")

    runtime_feature_manager.configure(config)

    discord_bot_service.configure(config)
    if runtime_feature_manager.discord_enabled:
        if not Features.discord_bot():
            startup_console.warning(
                "Discord Bot機能は無効化されています (FEATURE_DISCORD_BOT=false)"
            )
            logger.info(
                "Enable Discord Bot with FEATURE_DISCORD_BOT=true",
                extra=FILE_ONLY_LOG_EXTRA,
            )
        else:
            startup_console.stage("Discord Botサービスをバックグラウンドで起動します")
            await discord_bot_service.ensure_started(config)

    # Create and run the always-on WebUI runtime with optional local audio.
    try:
        with _startup_timer.phase("startup.assistant.create"):
            assistant = create_assistant(config)
    except ValueError as e:
        startup_console.error(f"設定エラー: {safe_startup_reason(e)}")
        logger.exception("Assistant configuration error", extra=FILE_ONLY_LOG_EXTRA)
        if service_cleanup:
            service_cleanup()
            atexit.unregister(service_cleanup)
        if require_database:
            raise
        return
    if caddy_start_callback:
        assistant.set_web_interface_ready_callback(caddy_start_callback)

    # Display runtime information
    status = runtime_feature_manager.status()
    print("\n🧩 AoiTalk Runtime")
    print(f"入力: {', '.join(status['input_adapters'])}")
    print(f"出力: {', '.join(status['output_adapters'])}")
    if getattr(assistant, "mode", None) == "voice_chat":
        print("🎤 ローカル音声アダプタ: ON")
    else:
        print("💬 ローカル音声アダプタ: OFF")

    # Setup signal handler for graceful shutdown
    def signal_handler(*_: Any) -> None:
        assistant.running = False
        if hasattr(assistant, 'voice_handler') and assistant.voice_handler:
            assistant.voice_handler.interrupt_flag = True
        print("\n🛑 終了シグナルを受信しました")

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)

    try:
        await assistant.run()
    except Exception as e:
        if getattr(assistant, "_startup_failed", False):
            # _start_web_interface already emitted a concise actionable
            # startup error and retained the traceback in the app log. Do not
            # swallow it in Personal mode: run.bat must report failure.
            raise
        startup_console.error(f"アシスタント実行エラー: {safe_startup_reason(e)}")
        logger.exception(
            "Assistant execution failed",
            extra=FILE_ONLY_LOG_EXTRA,
        )
        if require_database:
            raise
    finally:
        try:
            if 'assistant' in locals() and hasattr(assistant, 'cleanup'):
                await assistant.cleanup()
        finally:
            try:
                await discord_bot_service.stop()
            finally:
                if service_cleanup:
                    service_cleanup()
                    atexit.unregister(service_cleanup)

    # Cleanup tasks (executed for all modes)


if __name__ == "__main__":
    """Entry point"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 プログラムを終了します")
    except Exception as e:
        startup_console.error(f"予期しないエラー: {safe_startup_reason(e)}")
        logger.exception("Unhandled AoiTalk startup/runtime error", extra=FILE_ONLY_LOG_EXTRA)
        sys.exit(1)
