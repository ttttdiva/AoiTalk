"""AoiTalk統一ログシステム

プロジェクト全体で統一されたログフォーマットとロガーを提供
"""
import logging
import sys
from typing import Optional, Dict, Any
from datetime import datetime
from pathlib import Path
import json


class ColoredFormatter(logging.Formatter):
    """カラー出力対応のフォーマッター"""
    
    # カラーコード
    COLORS = {
        'DEBUG': '\033[36m',    # Cyan
        'INFO': '\033[32m',     # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',    # Red
        'CRITICAL': '\033[35m', # Magenta
    }
    RESET = '\033[0m'
    
    def __init__(self, fmt: Optional[str] = None, use_color: bool = True):
        """カラーフォーマッターを初期化
        
        Args:
            fmt: ログフォーマット文字列
            use_color: カラー出力を使用するか
        """
        super().__init__(fmt)
        self.use_color = use_color and sys.stdout.isatty()
        
    def format(self, record: logging.LogRecord) -> str:
        """ログレコードをフォーマット"""
        if self.use_color and record.levelname in self.COLORS:
            record.levelname_colored = (
                f"{self.COLORS[record.levelname]}{record.levelname}{self.RESET}"
            )
        else:
            record.levelname_colored = record.levelname
            
        return super().format(record)


class StructuredFormatter(logging.Formatter):
    """構造化ログ（JSON）フォーマッター"""
    
    def format(self, record: logging.LogRecord) -> str:
        """ログレコードをJSON形式でフォーマット"""
        log_data = {
            'timestamp': datetime.utcnow().isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
        }
        
        # extra属性があれば追加
        if hasattr(record, 'extra_data'):
            log_data['extra'] = record.extra_data
            
        # 例外情報があれば追加
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)
            
        return json.dumps(log_data, ensure_ascii=False)


class AoiTalkLogger:
    """AoiTalk用の統一ロガー"""
    
    # デフォルトフォーマット
    DEFAULT_FORMAT = '[%(asctime)s] %(levelname_colored)-8s [%(name)s] %(message)s'
    DEFAULT_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
    
    # ロガーのキャッシュ
    _loggers: Dict[str, logging.Logger] = {}
    _initialized = False
    
    @classmethod
    def setup_logging(cls, 
                     level: str = 'INFO',
                     log_file: Optional[str] = None,
                     use_color: bool = True,
                     use_structured: bool = False) -> None:
        """ログシステムを初期化
        
        Args:
            level: ログレベル
            log_file: ログファイルパス（オプション）
            use_color: カラー出力を使用するか
            use_structured: 構造化ログを使用するか
        """
        if cls._initialized:
            return
            
        # ルートロガーの設定
        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, level.upper()))
        
        # 既存のハンドラーをクリア
        root_logger.handlers.clear()
        
        # コンソールハンドラー
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, level.upper()))
        
        if use_structured:
            console_formatter = StructuredFormatter()
        else:
            console_formatter = ColoredFormatter(
                fmt=cls.DEFAULT_FORMAT,
                use_color=use_color
            )
            console_formatter.datefmt = cls.DEFAULT_DATE_FORMAT
            
        console_handler.setFormatter(console_formatter)
        root_logger.addHandler(console_handler)
        
        # ファイルハンドラー（オプション）
        if log_file:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setLevel(getattr(logging, level.upper()))
            
            # ファイルは常に構造化ログ
            file_formatter = StructuredFormatter()
            file_handler.setFormatter(file_formatter)
            root_logger.addHandler(file_handler)
            
        cls._initialized = True
        
    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        """名前付きロガーを取得
        
        Args:
            name: ロガー名（通常は__name__）
            
        Returns:
            設定済みのロガー
        """
        if not cls._initialized:
            cls.setup_logging()
            
        if name not in cls._loggers:
            logger = logging.getLogger(name)
            cls._loggers[name] = logger
            
        return cls._loggers[name]
        
    @classmethod
    def log_with_context(cls, logger: logging.Logger, level: str, 
                        message: str, **context) -> None:
        """コンテキスト情報付きでログ出力
        
        Args:
            logger: ロガーインスタンス
            level: ログレベル
            message: ログメッセージ
            **context: コンテキスト情報
        """
        # extraデータとして記録
        extra = {'extra_data': context} if context else {}
        getattr(logger, level.lower())(message, extra=extra)


# 便利関数
def get_logger(name: str) -> logging.Logger:
    """ロガーを取得する便利関数
    
    Args:
        name: ロガー名（通常は__name__）
        
    Returns:
        設定済みのロガー
    """
    return AoiTalkLogger.get_logger(name)


def setup_logging(**kwargs) -> None:
    """ログシステムを初期化する便利関数
    
    Args:
        **kwargs: AoiTalkLogger.setup_loggingの引数
    """
    AoiTalkLogger.setup_logging(**kwargs)


# ログデコレーター
def log_execution(level: str = 'DEBUG'):
    """関数の実行をログ出力するデコレーター
    
    Args:
        level: ログレベル
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            logger = get_logger(func.__module__)
            
            # 実行開始をログ
            AoiTalkLogger.log_with_context(
                logger, level,
                f"Executing {func.__name__}",
                function=func.__name__,
                args_count=len(args),
                kwargs_keys=list(kwargs.keys())
            )
            
            try:
                # 関数を実行
                result = func(*args, **kwargs)
                
                # 成功をログ
                AoiTalkLogger.log_with_context(
                    logger, level,
                    f"Completed {func.__name__}",
                    function=func.__name__,
                    success=True
                )
                
                return result
                
            except Exception as e:
                # エラーをログ
                AoiTalkLogger.log_with_context(
                    logger, 'ERROR',
                    f"Error in {func.__name__}: {str(e)}",
                    function=func.__name__,
                    error_type=type(e).__name__,
                    error_message=str(e)
                )
                raise
                
        return wrapper
    return decorator


# モジュール固有のロガー設定
def configure_module_logger(module_name: str, level: Optional[str] = None) -> logging.Logger:
    """モジュール固有のロガーを設定
    
    Args:
        module_name: モジュール名
        level: ログレベル（オプション）
        
    Returns:
        設定済みのロガー
    """
    logger = get_logger(module_name)
    
    if level:
        logger.setLevel(getattr(logging, level.upper()))
        
    return logger


# ログレベルの動的変更
def set_log_level(logger_name: str, level: str) -> None:
    """特定のロガーのレベルを動的に変更
    
    Args:
        logger_name: ロガー名
        level: 新しいログレベル
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, level.upper()))
    
    # ハンドラーのレベルも更新
    for handler in logger.handlers:
        handler.setLevel(getattr(logging, level.upper()))