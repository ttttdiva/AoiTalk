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

# Apply LD_LIBRARY_PATH fix for Mem0
# Prepend the required path to fix SQLite issue
sqlite_lib_path = '/usr/lib/x86_64-linux-gnu'
current_ld_path = os.environ.get('LD_LIBRARY_PATH', '')
if sqlite_lib_path not in current_ld_path:
    os.environ['LD_LIBRARY_PATH'] = f"{sqlite_lib_path}:{current_ld_path}" if current_ld_path else sqlite_lib_path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent))

from src.config import Config
from src.assistant.modes.terminal_mode import TerminalMode
from src.assistant.modes.voice_chat_mode import VoiceChatMode
from src.mode_switch import mode_switch_manager
from src.utils.logging_config import setup_default_logging
from src.utils.windows_optimization import apply_windows_optimizations


def create_assistant(config: Config) -> Optional[Union[TerminalMode, VoiceChatMode]]:
    """Create assistant based on configuration mode
    
    Args:
        config: Configuration object
        
    Returns:
        Assistant instance
        
    Raises:
        ValueError: If mode is not supported
    """
    mode = config.get('mode', 'terminal')
    
    if mode == 'terminal':
        return TerminalMode(config)
    elif mode == 'voice_chat':
        return VoiceChatMode(config)
    elif mode == 'discord':
        # Discord Botモードは別の方法で起動
        return None
    else:
        raise ValueError(f"未対応のモード: {mode}. 'terminal', 'voice_chat', または 'discord' を指定してください")


def parse_arguments() -> Namespace:
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description='AoiTalk Voice Assistant')
    parser.add_argument(
        '--mode',
        choices=['terminal', 'voice_chat', 'discord'],
        help='Override configuration mode'
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
    
    # Load configuration
    config = Config()

    # Check PostgreSQL availability early to surface issues before first message
    if config.get('memory', {}).get('enabled', True):
        try:
            from src.memory.database import get_database_manager
            db_manager = get_database_manager()
            db_ok = await db_manager.initialize()
            if not db_ok:
                print("笞・・ [Memory] PostgreSQL接続に失敗しました。メモリ機能は無効で継続します。")
        except Exception as e:
            print(f"笞・・ [Memory] PostgreSQL接続確認でエラー: {e}")
    
    # Override mode if specified
    if args.mode:
        config.config['mode'] = args.mode
    
    # Configure mode switch manager so WebUI/Discord can trigger restarts
    mode_switch_manager.configure(
        config,
        entrypoint=Path(__file__).resolve(),
        python_executable=sys.executable,
    )

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
    
    # Start ClickUp sync service in background if configured
    clickup_sync_task = None
    clickup_service = None
    clickup_enabled = config.get('clickup_sync', {}).get('enabled', True)
    if clickup_enabled and os.getenv('CLICKUP_API_KEY') and os.getenv('CLICKUP_TEAM_ID'):
        # ClickUp同期は一切待たずに完全バックグラウンドで実行
        async def start_clickup_background():
            try:
                # 遅延インポートでメイン処理をブロックしない
                await asyncio.sleep(0.1)  # 最小限の遅延
                from src.memory.database import get_database_manager
                from src.memory.clickup_sync import get_clickup_sync_service
                
                db_manager = get_database_manager()
                sync_interval = config.get('clickup_sync', {}).get('sync_interval_minutes', 15)
                clickup_service = await get_clickup_sync_service(db_manager, sync_interval)
                
                if clickup_service:
                    async with clickup_service:
                        await clickup_service.sync_tasks()
                        await clickup_service.start_background_sync()
            except Exception as e:
                print(f"⚠️ ClickUp同期エラー (バックグラウンド): {e}")
        
        # 完全に非同期で開始、メイン処理は一切待たない
        clickup_sync_task = asyncio.create_task(start_clickup_background())
        print(f"✅ ClickUp同期をバックグラウンドで開始")
    
    # Check if Discord mode
    current_mode = config.get('mode', 'terminal')
    if current_mode == 'discord':
        # Check Feature Flag first
        if not Features.discord_bot():
            print("\n❌ Discord Bot機能は無効化されています (FEATURE_DISCORD_BOT=false)")
            print("💡 有効にするには .env に FEATURE_DISCORD_BOT=true を設定してください")
            return
        
        # Discord Bot mode
        print("\n🤖 AoiTalk Discord Bot モード")
        print("Discord Botとして起動します...")
        
        
        # Run Discord bot
        try:
            from src.bot.discord_bot import run_bot
            await run_bot(config)
        except ValueError as e:
            # Token not found error - already handled with detailed message
            if "Discord bot token not found" in str(e):
                return  # Exit gracefully
            else:
                print(f"❌ Discord Bot設定エラー: {e}")
                return
        except ImportError as e:
            print(f"❌ Discord Botモジュールの読み込みに失敗しました: {e}")
            print("必要な依存パッケージがインストールされているか確認してください")
            print("pip install discord.py")
            return
        except Exception as e:
            print(f"❌ Discord Bot実行エラー: {e}")
            import traceback
            traceback.print_exc()
            return
    else:
        # Regular assistant modes
        # Create assistant based on mode
        try:
            assistant = create_assistant(config)
        except ValueError as e:
            print(f"❌ 設定エラー: {e}")
            return
        
        # Display mode information
        mode_messages = {
            'terminal': "\n💬 AoiTalk Terminal Assistant（ターミナルモード）",
            'voice_chat': "\n🎤💬 AoiTalk Voice & Chat Assistant（音声&チャットモード）"
        }
        
        print(mode_messages.get(assistant.mode, f"\n🤖 AoiTalk Assistant（{assistant.mode}モード）"))
        
        # Setup signal handler for graceful shutdown
        def signal_handler(*_: Any) -> None:
            assistant.running = False
            if hasattr(assistant, 'voice_handler'):
                assistant.voice_handler.interrupt_flag = True
            print("\n🛑 終了シグナルを受信しました")
            
        signal.signal(signal.SIGINT, signal_handler)
        
        # Run assistant
        try:
            await assistant.run()
        except Exception as e:
            print(f"❌ アシスタント実行エラー: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Ensure cleanup is called for regular assistant
            if 'assistant' in locals() and hasattr(assistant, 'cleanup'):
                await assistant.cleanup()
    
    # Cleanup tasks (executed for all modes)
    # Cancel ClickUp sync task if running
    if clickup_sync_task:
        clickup_sync_task.cancel()
        try:
            await clickup_sync_task
        except asyncio.CancelledError:
            pass
    
    # Close ClickUp service
    if clickup_service and hasattr(clickup_service, '__aexit__'):
        await clickup_service.__aexit__(None, None, None)


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
