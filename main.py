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


_startup_timer = get_startup_timer()
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
Features.initialize()

# Suppress FutureWarning for torch.load with weights_only=False
# This warning comes from external libraries (transformers, etc.)
warnings.filterwarnings("ignore", category=FutureWarning, message=".*weights_only.*")

# Apply LD_LIBRARY_PATH fix for Mem0 (Linux only)
# Prepend the required path to fix SQLite issue
if sys.platform.startswith("linux"):
    sqlite_lib_path = '/usr/lib/x86_64-linux-gnu'
    current_ld_path = os.environ.get('LD_LIBRARY_PATH', '')
    if sqlite_lib_path not in current_ld_path:
        os.environ['LD_LIBRARY_PATH'] = f"{sqlite_lib_path}:{current_ld_path}" if current_ld_path else sqlite_lib_path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent))

from src.config import Config
from src.runtime_features import runtime_feature_manager
from src.bot.service import discord_bot_service
from src.utils.logging_config import setup_default_logging
from src.utils.windows_optimization import apply_windows_optimizations

if TYPE_CHECKING:
    from src.assistant.modes.terminal_mode import TerminalMode
    from src.assistant.modes.voice_chat_mode import VoiceChatMode

    # Keep the precise return type for static checkers without importing the
    # mode modules during normal startup.  At runtime ``AssistantMode`` is
    # intentionally an ``Any`` alias so ``typing.get_type_hints`` can still
    # resolve the future annotation without defeating lazy imports.
    AssistantMode = Union[TerminalMode, VoiceChatMode]
else:
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

    # Windows環境での最適化を最初に適用
    with _startup_timer.phase("startup.windows.optimizations"):
        apply_windows_optimizations()
    
    # PostgreSQLサービスの起動確認（Windows環境のみ）
    import platform
    if platform.system() == "Windows":
        from src.utils.windows_optimization import get_windows_optimizer
        optimizer = get_windows_optimizer()
        with _startup_timer.phase("startup.windows.postgresql.ensure"):
            optimizer.ensure_postgresql_running()
        # AsyncIOのエラーログ抑制 (ConnectionResetError対策)
        with _startup_timer.phase("startup.windows.asyncio.error_suppression"):
            optimizer.suppress_asyncio_errors()
    
    # ログ設定をセットアップ
    debug_mode = os.getenv('AOITALK_DEBUG', '').lower() == 'true'
    with _startup_timer.phase("startup.logging.configure"):
        log_config = setup_default_logging(debug=debug_mode)
    
    # ログディレクトリの作成とファイルログの有効化
    from src.utils.log_layout import get_log_layout
    from src.utils.log_housekeeping import run_log_housekeeping

    project_root = Path(__file__).resolve().parent
    layout = get_log_layout(project_root)
    with _startup_timer.phase("startup.logging.directory"):
        layout.migrate_legacy_paths()
        run_log_housekeeping(layout)

    # 日時ごとのログファイル名を作成 (起動ごとに別ファイル)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = layout.app_log_path(timestamp)
    
    # セッションIDを設定（フィードバック追跡用）
    from src.utils.app_session import set_session_id
    set_session_id(timestamp)
    
    # ログファイルハンドラーを追加
    with _startup_timer.phase("startup.logging.file_handler"):
        log_config.add_file_handler(log_file)
    try:
        layout.app_latest_pointer().write_text(str(log_file.resolve()), encoding="utf-8")
    except Exception:
        pass
    print(f"📝 ログファイル: {log_file}")
    logging.getLogger(__name__).info("Application startup log file attached: %s", log_file)
    
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
        database_attempted = True
        try:
            from src.memory.database import get_database_manager
            with _startup_timer.phase("startup.database.manager"):
                db_manager = get_database_manager()
            with _startup_timer.phase("startup.database.initialize"):
                db_ok = await db_manager.initialize()
            database_ready = db_ok
            if not db_ok:
                message = "PostgreSQLのmigration/接続確認に失敗しました"
                if require_database:
                    raise RuntimeError(message)
                print(f"⚠️ [Memory] {message}。メモリ機能は無効で継続します。")
        except Exception as e:
            if require_database:
                raise RuntimeError(f"Enterprise database readiness failed: {e}") from e
            print(f"⚠️ [Memory] PostgreSQL接続確認でエラー: {e}")

    # Load configuration only after the optional early DB readiness attempt.
    with _startup_timer.phase("startup.config.generate"):
        config = Config()

    # USE_POSTGRESQL=falseでも、設定DBを使うmemory設定が有効なら一度だけ初期化する。
    memory_enabled = config.get('memory', {}).get('enabled', True)
    if memory_enabled and not database_attempted:
        try:
            from src.memory.database import get_database_manager
            with _startup_timer.phase("startup.database.manager"):
                db_manager = get_database_manager()
            with _startup_timer.phase("startup.database.initialize"):
                database_ready = await db_manager.initialize()
            if not database_ready:
                message = "PostgreSQLのmigration/接続確認に失敗しました"
                if require_database:
                    raise RuntimeError(message)
                print(f"⚠️ [Memory] {message}。メモリ機能は無効で継続します。")
        except Exception as e:
            if require_database:
                raise RuntimeError(f"Enterprise database readiness failed: {e}") from e
            print(f"⚠️ [Memory] PostgreSQL接続確認でエラー: {e}")

    # Frontend をDB準備後に起動し、Caddy は FastAPI readiness 後に起動する。
    caddy_start_callback = None
    service_cleanup = None
    if not args.skip_services:
        from src.service_manager import kill_services, start_caddy, start_services
        with _startup_timer.phase("startup.services.start"):
            start_services(config)
        service_cleanup = kill_services
        atexit.register(service_cleanup)
        caddy_start_callback = (
            lambda _host, ready_port: start_caddy(
                config,
                ready_fastapi_port=ready_port,
            )
        )

    runtime_feature_manager.configure(config)

    # Preload embedding model if memory is enabled and search is enabled
    # Windows optimization: Run embedding model preload in background to avoid blocking startup
    if config.get('memory', {}).get('enabled', True):
        memory_settings = config.get('memory', {})
        # Always preload embedding model if search is enabled (ignore preload_embedding_model setting)
        if memory_settings.get('enable_search', True):
            async def preload_embedding_background():
                try:
                    from src.memory.embedding import get_embedding_manager
                    embedding_manager = get_embedding_manager(memory_settings.get('embedding_model', 'all-MiniLM-L6-v2'))
                    await embedding_manager.preload_model()
                    print(f"✅ Embedding modelを起動時にpreloadしました (enable_search: true)")
                except Exception as e:
                    print(f"⚠️ Embedding model preloadに失敗: {e}")
            
            # Start background preload - don't await to avoid blocking startup
            asyncio.create_task(preload_embedding_background())
            print(f"📌 Embedding modelをバックグラウンドでpreload中... (enable_search: true)")
        else:
            print(f"📌 Embedding modelのpreloadをスキップしました (enable_search: false)")
    
    discord_bot_service.configure(config)
    if runtime_feature_manager.discord_enabled:
        if not Features.discord_bot():
            print("\n⚠️ Discord Bot機能は無効化されています (FEATURE_DISCORD_BOT=false)")
            print("💡 有効にするには .env に FEATURE_DISCORD_BOT=true を設定してください")
        else:
            print("\n🤖 Discord Botサービスをバックグラウンドで起動します")
            await discord_bot_service.ensure_started(config)

    # Create and run the always-on WebUI runtime with optional local audio.
    try:
        with _startup_timer.phase("startup.assistant.create"):
            assistant = create_assistant(config)
    except ValueError as e:
        print(f"❌ 設定エラー: {e}")
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
        print(f"❌ アシスタント実行エラー: {e}")
        import traceback
        traceback.print_exc()
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
        print(f"❌ 予期しないエラー: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
