"""
ログ設定の統一管理
"""
import logging
import sys
from typing import Optional, Dict, Any
from pathlib import Path


class LoggingConfig:
    """統一されたログ設定管理クラス"""
    
    # デフォルトのログフォーマット
    DEFAULT_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    DEFAULT_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
    
    # サプレスするロガー
    SUPPRESSED_LOGGERS = [
        'spotipy',
        'urllib3.util.retry',
        'urllib3.connectionpool',
        'urllib3',
        'httpx',
        'httpcore',
        'spotify',
    ]
    
    def __init__(self, 
                 level: str = 'INFO',
                 format_string: Optional[str] = None,
                 date_format: Optional[str] = None,
                 log_file: Optional[Path] = None):
        """
        ログ設定を初期化
        
        Args:
            level: ログレベル (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            format_string: ログフォーマット文字列
            date_format: 日付フォーマット文字列
            log_file: ログファイルのパス（Noneの場合はコンソールのみ）
        """
        self.level = getattr(logging, level.upper())
        self.format_string = format_string or self.DEFAULT_FORMAT
        self.date_format = date_format or self.DEFAULT_DATE_FORMAT
        self.log_file = log_file
        
        # ログ設定を適用
        self._configure_logging()
        
    def _configure_logging(self) -> None:
        """ログ設定を適用"""
        # ルートロガーの設定
        root_logger = logging.getLogger()
        root_logger.setLevel(self.level)
        
        # 既存のハンドラーをクリア
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # フォーマッターの作成
        formatter = logging.Formatter(self.format_string, self.date_format)
        
        # コンソールハンドラーの設定
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(self.level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
        
        # ファイルハンドラーの設定（指定された場合）
        if self.log_file:
            file_handler = logging.FileHandler(self.log_file, encoding='utf-8')
            file_handler.setLevel(self.level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        
        # ノイズの多いロガーをサプレス
        self._suppress_noisy_loggers()
        
        # Spotifyレート制限メッセージのフィルターを追加
        root_logger.addFilter(SpotipyRateLimitFilter())
        
    def _suppress_noisy_loggers(self) -> None:
        """ノイズの多いロガーのレベルを上げる"""
        for logger_name in self.SUPPRESSED_LOGGERS:
            logger = logging.getLogger(logger_name)
            logger.setLevel(logging.ERROR)
            
    def set_module_level(self, module_name: str, level: str) -> None:
        """特定のモジュールのログレベルを設定
        
        Args:
            module_name: モジュール名
            level: ログレベル文字列
        """
        logger = logging.getLogger(module_name)
        logger.setLevel(getattr(logging, level.upper()))
        
    def add_file_handler(self, file_path: Path, level: Optional[str] = None) -> None:
        """ファイルハンドラーを追加
        
        Args:
            file_path: ログファイルのパス
            level: このハンドラーのログレベル（Noneの場合はルートレベルを使用）
        """
        handler_level = getattr(logging, level.upper()) if level else self.level
        
        formatter = logging.Formatter(self.format_string, self.date_format)
        file_handler = logging.FileHandler(file_path, encoding='utf-8')
        file_handler.setLevel(handler_level)
        file_handler.setFormatter(formatter)
        
        root_logger = logging.getLogger()
        root_logger.addHandler(file_handler)


class SpotipyRateLimitFilter(logging.Filter):
    """Spotipyのレート制限メッセージをフィルタリング"""
    
    def filter(self, record: logging.LogRecord) -> bool:
        """レート制限メッセージをフィルタリング
        
        Args:
            record: ログレコード
            
        Returns:
            bool: メッセージを表示する場合True
        """
        return "Your application has reached a rate/request limit" not in record.getMessage()


def setup_default_logging(debug: bool = False) -> LoggingConfig:
    """デフォルトのログ設定をセットアップ
    
    Args:
        debug: デバッグモードを有効にするか
        
    Returns:
        LoggingConfig: 設定済みのLoggingConfigインスタンス
    """
    level = 'DEBUG' if debug else 'INFO'
    return LoggingConfig(level=level)


def setup_discord_logging(log_file: Path, debug: bool = False) -> LoggingConfig:
    """Discord Bot用のログ設定をセットアップ
    
    Args:
        log_file: ログファイルのパス
        debug: デバッグモードを有効にするか
        
    Returns:
        LoggingConfig: 設定済みのLoggingConfigインスタンス
    """
    level = 'DEBUG' if debug else 'INFO'
    config = LoggingConfig(level=level, log_file=log_file)
    
    # Discord関連のモジュールはDEBUGレベルに設定
    if debug:
        config.set_module_level('discord', 'DEBUG')
        config.set_module_level('discord.voice_client', 'DEBUG')
        config.set_module_level('discord.gateway', 'INFO')
        
    return config