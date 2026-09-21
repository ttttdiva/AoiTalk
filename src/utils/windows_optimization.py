"""
Windows環境専用の最適化ユーティリティ
起動速度とパフォーマンスの向上を目的とした設定とヘルパー関数
"""

import os
import platform
import psutil
import threading
import asyncio
from typing import Optional, Dict, Any, List
from pathlib import Path
import yaml
import logging

from src.utils.startup_timing import get_startup_timer
from src.utils.logging_config import FILE_ONLY_LOG_EXTRA


logger = logging.getLogger(__name__)


class WindowsOptimization:
    """Windows環境での最適化を管理するクラス"""
    
    def __init__(self, config_path: Optional[str] = None):
        """初期化
        
        Args:
            config_path: Windows最適化設定ファイルのパス
        """
        self.is_windows = platform.system() == "Windows"
        self.config = self._load_optimization_config(config_path)
        self._applied_optimizations: List[str] = []
        
    def _load_optimization_config(self, config_path: Optional[str] = None) -> Dict[str, Any]:
        """最適化設定を読み込む
        
        Args:
            config_path: 設定ファイルのパス
            
        Returns:
            最適化設定辞書
        """
        # デフォルト設定を直接返す（外部ファイルは使用しない）
        return {
            'database': {'connection_timeout': 10, 'command_timeout': 30},
            'memory': {'initialization_timeout': 30, 'background_preload': True},
            'system': {'process_priority': 'high'},
            'concurrency': {'max_workers': 4, 'io_bound_workers': 8}
        }
    
    def apply_system_optimizations(self) -> None:
        """システムレベルの最適化を適用"""
        if not self.is_windows:
            return
            
        try:
            # プロセス優先度の設定
            priority = self.config.get('system', {}).get('process_priority', 'normal')
            self._set_process_priority(priority)
            
            # CPU親和性の設定（必要に応じて）
            cpu_affinity = self.config.get('system', {}).get('cpu_affinity')
            if cpu_affinity is not None:
                self._set_cpu_affinity(cpu_affinity)
                
            logger.info(
                "Windows system optimizations applied",
            )
            self._applied_optimizations.append("system_optimization")

        except Exception as e:
            logger.warning(
                "Windows system optimization failed; continuing without it",
                exc_info=True,
                extra=FILE_ONLY_LOG_EXTRA,
            )
    
    def _set_process_priority(self, priority: str) -> None:
        """プロセス優先度を設定
        
        Args:
            priority: 優先度 ("normal", "high", "realtime")
        """
        if not self.is_windows:
            return
            
        try:
            process = psutil.Process()
            
            if priority == "high":
                process.nice(psutil.HIGH_PRIORITY_CLASS)
            elif priority == "realtime":
                process.nice(psutil.REALTIME_PRIORITY_CLASS)
            else:  # normal
                process.nice(psutil.NORMAL_PRIORITY_CLASS)
                
            logger.info(
                "Windows process priority set to %s",
                priority,
            )

        except Exception as e:
            logger.warning(
                "Unable to set Windows process priority; continuing",
                exc_info=True,
                extra=FILE_ONLY_LOG_EXTRA,
            )
    
    def _set_cpu_affinity(self, cpu_list: List[int]) -> None:
        """CPU親和性を設定
        
        Args:
            cpu_list: 使用するCPUのリスト
        """
        if not self.is_windows:
            return
            
        try:
            process = psutil.Process()
            process.cpu_affinity(cpu_list)
            logger.info(
                "Windows CPU affinity set: %s",
                cpu_list,
            )

        except Exception as e:
            logger.warning(
                "Unable to set Windows CPU affinity; continuing",
                exc_info=True,
                extra=FILE_ONLY_LOG_EXTRA,
            )
    
    def get_database_config_overrides(self) -> Dict[str, Any]:
        """データベース設定のオーバーライドを取得
        
        Returns:
            データベース設定のオーバーライド辞書
        """
        if not self.is_windows:
            return {}
            
        db_config = self.config.get('database', {})
        # asyncpgドライバー用の設定
        # connect_timeoutではなくtimeoutを使用
        return {
            'timeout': db_config.get('connection_timeout', 30),  # Windows環境では30秒に延長
            'command_timeout': db_config.get('command_timeout', 60),  # コマンドタイムアウトも延長
            'server_settings': {'tcp_keepalives_idle': '600', 'tcp_keepalives_interval': '30', 'tcp_keepalives_count': '3'}
        }
    
    def get_memory_config_overrides(self) -> Dict[str, Any]:
        """メモリ設定のオーバーライドを取得
        
        Returns:
            メモリ設定のオーバーライド辞書
        """
        if not self.is_windows:
            return {}
            
        memory_config = self.config.get('memory', {})
        return {
            'initialization_timeout': memory_config.get('initialization_timeout', 30),
            'background_preload': memory_config.get('background_preload', True)
        }
    
    def optimize_asyncio_settings(self) -> None:
        """AsyncIOの設定を最適化"""
        if not self.is_windows:
            return
            
        try:
            # Windows環境でのイベントループポリシーを設定
            if hasattr(asyncio, 'WindowsProactorEventLoopPolicy'):
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                logger.info(
                    "Windows ProactorEventLoopPolicy configured",
                )
                self._applied_optimizations.append("asyncio_optimization")

        except Exception as e:
            logger.warning(
                "AsyncIO Windows optimization failed; continuing",
                exc_info=True,
                extra=FILE_ONLY_LOG_EXTRA,
            )
            
    def suppress_asyncio_errors(self) -> None:
        """AsyncIOの特定のエラーログを抑制
        
        ConnectionResetError [WinError 10054] に関するログをフィルターします。
        """
        if not self.is_windows:
            return
            
        class ConnectionResetFilter(logging.Filter):
            def filter(self, record):
                # エラーメッセージや例外情報に WinError 10054 が含まれているか確認
                msg = record.getMessage()
                if "WinError 10054" in msg or "既存の接続はリモート ホストに強制的に切断されました" in msg:
                    return False
                
                # exc_info (例外情報) もチェック
                if record.exc_info:
                    exc_type, exc_value, _ = record.exc_info
                    if exc_value and ("WinError 10054" in str(exc_value) or "既存の接続はリモート ホストに強制的に切断されました" in str(exc_value)):
                        return False
                        
                return True

        try:
            logger = logging.getLogger("asyncio")
            logger.addFilter(ConnectionResetFilter())
            logger.info(
                "AsyncIO connection-reset error filter applied",
            )
            self._applied_optimizations.append("asyncio_error_filter")
        except Exception as e:
            logger.warning(
                "AsyncIO error filter could not be applied; continuing",
                exc_info=True,
                extra=FILE_ONLY_LOG_EXTRA,
            )
    
    def get_concurrency_settings(self) -> Dict[str, int]:
        """並行処理設定を取得
        
        Returns:
            並行処理設定辞書
        """
        concurrency_config = self.config.get('concurrency', {})
        return {
            'max_workers': concurrency_config.get('max_workers', 4),
            'io_bound_workers': concurrency_config.get('io_bound_workers', 8)
        }
    
    def pre_warm_components(self) -> None:
        """コンポーネントの事前ウォームアップ"""
        if not self.is_windows:
            return
            
        def warm_up_thread():
            """バックグラウンドでのウォームアップ処理"""
            try:
                # スレッドプールの事前作成
                import concurrent.futures
                settings = self.get_concurrency_settings()
                
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=settings['max_workers']
                ) as executor:
                    # ダミータスクで初期化
                    futures = [executor.submit(lambda: None) for _ in range(2)]
                    concurrent.futures.wait(futures, timeout=1.0)
                
                logger.info(
                    "Windows component warm-up completed",
                )
                self._applied_optimizations.append("component_warmup")

            except Exception as e:
                logger.warning(
                    "Windows component warm-up failed; continuing",
                    exc_info=True,
                    extra=FILE_ONLY_LOG_EXTRA,
                )
        
        # バックグラウンドで実行
        threading.Thread(target=warm_up_thread, daemon=True).start()
    
    def get_network_config_overrides(self) -> Dict[str, Any]:
        """ネットワーク設定のオーバーライドを取得
        
        Returns:
            ネットワーク設定のオーバーライド辞書
        """
        if not self.is_windows:
            return {}
            
        network_config = self.config.get('network', {})
        return {
            'timeout': network_config.get('timeout', 30),
            'connection_pooling': network_config.get('connection_pooling', True),
            'keep_alive': network_config.get('keep_alive', True)
        }
    
    def get_applied_optimizations(self) -> List[str]:
        """適用された最適化のリストを取得
        
        Returns:
            適用された最適化のリスト
        """
        return self._applied_optimizations.copy()
    
    def ensure_postgresql_running(self) -> bool:
        """PostgreSQLサービスが起動していることを確認し、必要に応じて起動する
        
        Returns:
            bool: True if service is running or started successfully
        """
        if not self.is_windows:
            return True
            
        try:
            import subprocess
            
            # 環境変数からサービス名を取得（デフォルト: postgresql-x64-16）
            service_name = os.getenv('POSTGRES_SERVICE_NAME', 'postgresql-x64-16')
            
            # サービスの状態を確認
            result = subprocess.run(
                ['sc', 'query', service_name],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            # サービスが存在しない場合
            if result.returncode != 0:
                logger.warning(
                    "PostgreSQL service '%s' was not found; set POSTGRES_SERVICE_NAME to the installed service",
                    service_name,
                )
                return False
            
            # STATE行を探して状態を確認
            is_running = False
            for line in result.stdout.split('\n'):
                if 'STATE' in line and 'RUNNING' in line:
                    is_running = True
                    break
            
            if is_running:
                logger.info(
                    "PostgreSQL service is already running",
                )
                return True

            # サービスが停止している場合は起動
            logger.info(
                "PostgreSQL service is stopped; starting it",
            )
            
            start_result = subprocess.run(
                ['net', 'start', service_name],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if start_result.returncode == 0:
                logger.info(
                    "PostgreSQL service started",
                )
                self._applied_optimizations.append("postgresql_service_start")

                # PostgreSQLが接続可能になるまで待機
                logger.info(
                    "Waiting for PostgreSQL to accept connections",
                )
                import time
                import socket
                
                postgres_host = os.getenv('POSTGRES_HOST', '127.0.0.1')
                postgres_port = int(os.getenv('POSTGRES_PORT', '5432'))
                
                max_wait = 30  # 最大30秒待機
                wait_interval = 1
                waited = 0
                
                while waited < max_wait:
                    try:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(2)
                        result = sock.connect_ex((postgres_host, postgres_port))
                        sock.close()

                        if result == 0:
                            logger.info(
                                "PostgreSQL accepted connections after %ss",
                                waited,
                            )
                            return True
                    except Exception:
                        pass
                    
                    time.sleep(wait_interval)
                    waited += wait_interval
                
                logger.warning(
                    "PostgreSQL did not accept connections within %ss; check the service and port",
                    max_wait,
                )
                return False
            else:
                error_msg = start_result.stderr.strip() if start_result.stderr else start_result.stdout.strip()
                error_msg = " ".join(error_msg.split())[:300]

                # 管理者権限が必要な場合
                if 'アクセスが拒否されました' in error_msg or 'Access is denied' in error_msg:
                    logger.warning(
                        "PostgreSQL service start requires administrator privileges; run 'net start %s' as administrator",
                        service_name,
                    )
                else:
                    logger.warning(
                        "PostgreSQL service could not be started%s",
                        f": {error_msg}" if error_msg else "",
                    )

                return False

        except subprocess.TimeoutExpired:
            logger.warning("PostgreSQL service status/start command timed out")
            return False
        except Exception as e:
            logger.error(
                "PostgreSQL service check failed",
                exc_info=True,
                extra=FILE_ONLY_LOG_EXTRA,
            )
            return False
    
    def print_optimization_summary(self) -> None:
        """最適化の適用状況を表示"""
        if not self.is_windows:
            logger.debug("Windows optimizations skipped on non-Windows host")
            return

        logger.info(
            "Windows optimization summary: %s",
            self._applied_optimizations or "none",
        )


# グローバルインスタンス
_windows_optimizer: Optional[WindowsOptimization] = None


def get_windows_optimizer() -> WindowsOptimization:
    """WindowsOptimizerのグローバルインスタンスを取得
    
    Returns:
        WindowsOptimizerインスタンス
    """
    global _windows_optimizer
    
    if _windows_optimizer is None:
        _windows_optimizer = WindowsOptimization()
    
    return _windows_optimizer


def apply_windows_optimizations() -> None:
    """Windows最適化を適用する便利関数"""
    optimizer = get_windows_optimizer()
    
    # システム最適化を適用
    optimizer.apply_system_optimizations()
    
    # AsyncIO設定を最適化
    optimizer.optimize_asyncio_settings()
    
    # コンポーネントの事前ウォームアップ
    optimizer.pre_warm_components()
    
    # PostgreSQLサービスの起動確認
    # ``main`` 直後の再確認とは別スパンにして、二重チェックの実コストを
    # 起動ログ上で分離できるようにする。計測は fail-open で、既存の呼出し
    # 順序・例外意味・戻り値（None）は変更しない。
    with get_startup_timer().phase("startup.windows.optimizations.postgresql.ensure"):
        optimizer.ensure_postgresql_running()
    
    # 適用状況を表示
    optimizer.print_optimization_summary()
