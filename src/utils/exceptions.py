"""AoiTalk共通例外クラス

プロジェクト全体で使用される例外クラスを定義
"""
from typing import Optional, Dict, Any


class AoiTalkException(Exception):
    """AoiTalkの基底例外クラス"""
    
    def __init__(self, message: str, code: Optional[str] = None, 
                 details: Optional[Dict[str, Any]] = None):
        """基底例外を初期化
        
        Args:
            message: エラーメッセージ
            code: エラーコード（オプション）
            details: 詳細情報（オプション）
        """
        super().__init__(message)
        self.code = code
        self.details = details or {}
        
    def __str__(self) -> str:
        """文字列表現を返す"""
        if self.code:
            return f"[{self.code}] {super().__str__()}"
        return super().__str__()


class ConfigurationError(AoiTalkException):
    """設定関連のエラー"""
    
    def __init__(self, message: str, config_key: Optional[str] = None):
        """設定エラーを初期化
        
        Args:
            message: エラーメッセージ
            config_key: 問題のある設定キー
        """
        details = {"config_key": config_key} if config_key else {}
        super().__init__(message, code="CONFIG_ERROR", details=details)


class AudioError(AoiTalkException):
    """音声処理関連のエラー"""
    pass


class AudioInputError(AudioError):
    """音声入力関連のエラー"""
    
    def __init__(self, message: str, device_name: Optional[str] = None):
        """音声入力エラーを初期化
        
        Args:
            message: エラーメッセージ
            device_name: デバイス名
        """
        details = {"device": device_name} if device_name else {}
        super().__init__(message, code="AUDIO_INPUT_ERROR", details=details)


class AudioOutputError(AudioError):
    """音声出力関連のエラー"""
    
    def __init__(self, message: str, device_name: Optional[str] = None):
        """音声出力エラーを初期化
        
        Args:
            message: エラーメッセージ
            device_name: デバイス名
        """
        details = {"device": device_name} if device_name else {}
        super().__init__(message, code="AUDIO_OUTPUT_ERROR", details=details)


class TTSError(AoiTalkException):
    """TTS（音声合成）関連のエラー"""
    
    def __init__(self, message: str, engine: Optional[str] = None, 
                 character: Optional[str] = None):
        """TTSエラーを初期化
        
        Args:
            message: エラーメッセージ
            engine: TTSエンジン名
            character: キャラクター名
        """
        details = {}
        if engine:
            details["engine"] = engine
        if character:
            details["character"] = character
        super().__init__(message, code="TTS_ERROR", details=details)


class RecognitionError(AoiTalkException):
    """音声認識関連のエラー"""
    
    def __init__(self, message: str, model: Optional[str] = None):
        """音声認識エラーを初期化
        
        Args:
            message: エラーメッセージ
            model: 認識モデル名
        """
        details = {"model": model} if model else {}
        super().__init__(message, code="RECOGNITION_ERROR", details=details)


class ProcessingError(AoiTalkException):
    """処理パイプライン関連のエラー"""
    
    def __init__(self, message: str, stage: Optional[str] = None):
        """処理エラーを初期化
        
        Args:
            message: エラーメッセージ
            stage: 処理ステージ名
        """
        details = {"stage": stage} if stage else {}
        super().__init__(message, code="PROCESSING_ERROR", details=details)


class ModelError(AoiTalkException):
    """モデル関連のエラー"""
    
    def __init__(self, message: str, model_name: Optional[str] = None, 
                 model_type: Optional[str] = None):
        """モデルエラーを初期化
        
        Args:
            message: エラーメッセージ
            model_name: モデル名
            model_type: モデルタイプ
        """
        details = {}
        if model_name:
            details["model_name"] = model_name
        if model_type:
            details["model_type"] = model_type
        super().__init__(message, code="MODEL_ERROR", details=details)


class ResourceError(AoiTalkException):
    """リソース関連のエラー"""
    
    def __init__(self, message: str, resource_type: Optional[str] = None, 
                 resource_path: Optional[str] = None):
        """リソースエラーを初期化
        
        Args:
            message: エラーメッセージ
            resource_type: リソースタイプ
            resource_path: リソースパス
        """
        details = {}
        if resource_type:
            details["resource_type"] = resource_type
        if resource_path:
            details["resource_path"] = resource_path
        super().__init__(message, code="RESOURCE_ERROR", details=details)


class ConnectionError(AoiTalkException):
    """接続関連のエラー"""
    
    def __init__(self, message: str, service: Optional[str] = None, 
                 host: Optional[str] = None, port: Optional[int] = None):
        """接続エラーを初期化
        
        Args:
            message: エラーメッセージ
            service: サービス名
            host: ホスト名
            port: ポート番号
        """
        details = {}
        if service:
            details["service"] = service
        if host:
            details["host"] = host
        if port:
            details["port"] = port
        super().__init__(message, code="CONNECTION_ERROR", details=details)


class ValidationError(AoiTalkException):
    """バリデーション関連のエラー"""
    
    def __init__(self, message: str, field: Optional[str] = None, 
                 value: Any = None, expected: Optional[str] = None):
        """バリデーションエラーを初期化
        
        Args:
            message: エラーメッセージ
            field: フィールド名
            value: 実際の値
            expected: 期待される値の説明
        """
        details = {}
        if field:
            details["field"] = field
        if value is not None:
            details["value"] = str(value)
        if expected:
            details["expected"] = expected
        super().__init__(message, code="VALIDATION_ERROR", details=details)


# エラーハンドリングのヘルパー関数
def format_error_message(error: AoiTalkException, include_details: bool = True) -> str:
    """エラーメッセージをフォーマット
    
    Args:
        error: AoiTalkException インスタンス
        include_details: 詳細情報を含めるか
        
    Returns:
        フォーマットされたエラーメッセージ
    """
    message = str(error)
    
    if include_details and error.details:
        details_str = ", ".join(f"{k}={v}" for k, v in error.details.items())
        message = f"{message} ({details_str})"
        
    return message


def is_retryable_error(error: Exception) -> bool:
    """リトライ可能なエラーかどうかを判定
    
    Args:
        error: 例外インスタンス
        
    Returns:
        リトライ可能かどうか
    """
    # 接続エラーは基本的にリトライ可能
    if isinstance(error, ConnectionError):
        return True
        
    # 一時的なリソースエラーもリトライ可能
    if isinstance(error, ResourceError):
        # ファイルロックなどの一時的なエラーを想定
        return "temporarily" in str(error).lower() or "locked" in str(error).lower()
        
    # AudioInputError/AudioOutputErrorで一時的なエラーの場合
    if isinstance(error, (AudioInputError, AudioOutputError)):
        return "temporarily" in str(error).lower() or "busy" in str(error).lower()
        
    return False