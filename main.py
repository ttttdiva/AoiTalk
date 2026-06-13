#!/usr/bin/env python3
"""
AoiTalk Voice Assistant - Refactored Main Entry Point

This is the main entry point for the AoiTalk Voice Assistant Framework.
The core functionality has been refactored into modular components in src/assistant/.
"""

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
from typing import Optional, Union, Any
from argparse import Namespace

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
from src.assistant.modes.terminal_mode import TerminalMode
from src.assistant.modes.voice_chat_mode import VoiceChatMode
from src.runtime_features import runtime_feature_manager
from src.bot.service import discord_bot_service
from src.utils.logging_config import setup_default_logging
from src.utils.windows_optimization import apply_windows_optimizations


def create_assistant(config: Config) -> Optional[Union[TerminalMode, VoiceChatMode]]:
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
        return VoiceChatMode(config)
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
    apply_windows_optimizations()
    
    # PostgreSQLサービスの起動確認（Windows環境のみ）
    import platform
    if platform.system() == "Windows":
        from src.utils.windows_optimization import get_windows_optimizer
        optimizer = get_windows_optimizer()
        optimizer.ensure_postgresql_running()
        # AsyncIOのエラーログ抑制 (ConnectionResetError対策)
        optimizer.suppress_asyncio_errors()
    
    # ログ設定をセットアップ
    debug_mode = os.getenv('AOITALK_DEBUG', '').lower() == 'true'
    log_config = setup_default_logging(debug=debug_mode)
    
    # ログディレクトリの作成とファイルログの有効化
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # 日時ごとのログファイル名を作成 (起動ごとに別ファイル)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = log_dir / f"app_{timestamp}.log"
    
    # セッションIDを設定（フィードバック追跡用）
    from src.utils.app_session import set_session_id
    set_session_id(timestamp)
    
    # ログファイルハンドラーを追加
    log_config.add_file_handler(log_file)
    print(f"📝 ログファイル: {log_file}")
    logging.getLogger(__name__).info("Application startup log file attached: %s", log_file)
    
    # Load configuration
    config = Config()

    # Frontend + Caddy を子プロセスとして起動（モード切替時はスキップ）
    if not args.skip_services:
        from src.service_manager import start_services
        start_services(config)

    # Check PostgreSQL availability early to surface issues before first message
    if config.get('memory', {}).get('enabled', True):
        try:
            from src.memory.database import get_database_manager
            db_manager = get_database_manager()
            db_ok = await db_manager.initialize()
            if not db_ok:
                print("⚠️ [Memory] PostgreSQL接続に失敗しました。メモリ機能は無効で継続します。")
        except Exception as e:
            print(f"⚠️ [Memory] PostgreSQL接続確認でエラー: {e}")
    
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
        assistant = create_assistant(config)
    except ValueError as e:
        print(f"❌ 設定エラー: {e}")
        return

    # Display runtime information
    status = runtime_feature_manager.status()
    print("\n🧩 AoiTalk Runtime")
    print(f"入力: {', '.join(status['input_adapters'])}")
    print(f"出力: {', '.join(status['output_adapters'])}")
    if isinstance(assistant, VoiceChatMode):
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

    try:
        await assistant.run()
    except Exception as e:
        print(f"❌ アシスタント実行エラー: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if 'assistant' in locals() and hasattr(assistant, 'cleanup'):
            await assistant.cleanup()
        await discord_bot_service.stop()

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
