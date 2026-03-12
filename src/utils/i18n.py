"""国際化（i18n）サポート

エラーメッセージやUIテキストの多言語対応を提供
"""
import json
import os
from typing import Dict, Any, Optional
from pathlib import Path
from enum import Enum


class Language(Enum):
    """サポート言語"""
    JA = "ja"  # 日本語
    EN = "en"  # 英語
    ZH = "zh"  # 中国語
    KO = "ko"  # 韓国語


class I18nManager:
    """国際化マネージャー"""
    
    def __init__(self, default_language: Language = Language.JA):
        """国際化マネージャーを初期化
        
        Args:
            default_language: デフォルト言語
        """
        self.default_language = default_language
        self.current_language = default_language
        self.translations: Dict[str, Dict[str, str]] = {}
        
        # 翻訳ファイルのベースディレクトリ
        self.translations_dir = Path(__file__).parent.parent / "locales"
        
        # デフォルトの翻訳を読み込み
        self._load_default_translations()
        
    def _load_default_translations(self) -> None:
        """デフォルトの翻訳を読み込み（ハードコード）"""
        # 日本語
        self.translations[Language.JA.value] = {
            # 共通
            "error": "エラー",
            "warning": "警告",
            "info": "情報",
            "success": "成功",
            "failed": "失敗",
            
            # 設定エラー
            "config_error": "設定エラー",
            "invalid_config_key": "無効な設定キー: {key}",
            "config_file_not_found": "設定ファイルが見つかりません: {path}",
            "config_validation_failed": "設定の検証に失敗しました",
            
            # 音声エラー
            "audio_error": "音声エラー",
            "audio_input_error": "音声入力エラー",
            "audio_output_error": "音声出力エラー",
            "device_not_found": "デバイスが見つかりません: {device}",
            "audio_format_unsupported": "サポートされていない音声フォーマット: {format}",
            
            # TTSエラー
            "tts_error": "音声合成エラー",
            "tts_engine_not_found": "TTSエンジンが見つかりません: {engine}",
            "character_not_found": "キャラクターが見つかりません: {character}",
            "tts_synthesis_failed": "音声合成に失敗しました",
            
            # 音声認識エラー
            "recognition_error": "音声認識エラー",
            "model_not_found": "モデルが見つかりません: {model}",
            "recognition_failed": "音声認識に失敗しました",
            "language_not_supported": "サポートされていない言語: {language}",
            
            # 処理エラー
            "processing_error": "処理エラー",
            "pipeline_error": "パイプラインエラー",
            "stage_failed": "処理ステージが失敗しました: {stage}",
            
            # リソースエラー
            "resource_error": "リソースエラー",
            "file_not_found": "ファイルが見つかりません: {path}",
            "insufficient_memory": "メモリが不足しています",
            "resource_locked": "リソースがロックされています: {resource}",
            
            # 接続エラー
            "connection_error": "接続エラー",
            "connection_failed": "接続に失敗しました: {service}",
            "connection_timeout": "接続がタイムアウトしました",
            "host_unreachable": "ホストに到達できません: {host}:{port}",
            
            # バリデーションエラー
            "validation_error": "検証エラー",
            "invalid_value": "無効な値: {field} = {value}",
            "required_field_missing": "必須フィールドがありません: {field}",
            "value_out_of_range": "値が範囲外です: {field} (期待値: {expected})",
        }
        
        # 英語
        self.translations[Language.EN.value] = {
            # Common
            "error": "Error",
            "warning": "Warning",
            "info": "Information",
            "success": "Success",
            "failed": "Failed",
            
            # Configuration errors
            "config_error": "Configuration Error",
            "invalid_config_key": "Invalid configuration key: {key}",
            "config_file_not_found": "Configuration file not found: {path}",
            "config_validation_failed": "Configuration validation failed",
            
            # Audio errors
            "audio_error": "Audio Error",
            "audio_input_error": "Audio Input Error",
            "audio_output_error": "Audio Output Error",
            "device_not_found": "Device not found: {device}",
            "audio_format_unsupported": "Unsupported audio format: {format}",
            
            # TTS errors
            "tts_error": "TTS Error",
            "tts_engine_not_found": "TTS engine not found: {engine}",
            "character_not_found": "Character not found: {character}",
            "tts_synthesis_failed": "TTS synthesis failed",
            
            # Recognition errors
            "recognition_error": "Recognition Error",
            "model_not_found": "Model not found: {model}",
            "recognition_failed": "Recognition failed",
            "language_not_supported": "Language not supported: {language}",
            
            # Processing errors
            "processing_error": "Processing Error",
            "pipeline_error": "Pipeline Error",
            "stage_failed": "Processing stage failed: {stage}",
            
            # Resource errors
            "resource_error": "Resource Error",
            "file_not_found": "File not found: {path}",
            "insufficient_memory": "Insufficient memory",
            "resource_locked": "Resource is locked: {resource}",
            
            # Connection errors
            "connection_error": "Connection Error",
            "connection_failed": "Connection failed: {service}",
            "connection_timeout": "Connection timeout",
            "host_unreachable": "Host unreachable: {host}:{port}",
            
            # Validation errors
            "validation_error": "Validation Error",
            "invalid_value": "Invalid value: {field} = {value}",
            "required_field_missing": "Required field missing: {field}",
            "value_out_of_range": "Value out of range: {field} (expected: {expected})",
        }
        
    def load_translations_from_file(self, language: Language, file_path: str) -> None:
        """翻訳ファイルを読み込み
        
        Args:
            language: 言語
            file_path: 翻訳ファイルのパス
        """
        with open(file_path, 'r', encoding='utf-8') as f:
            translations = json.load(f)
            self.translations[language.value] = translations
            
    def set_language(self, language: Language) -> None:
        """現在の言語を設定
        
        Args:
            language: 設定する言語
        """
        self.current_language = language
        
    def get(self, key: str, **kwargs) -> str:
        """翻訳されたテキストを取得
        
        Args:
            key: 翻訳キー
            **kwargs: 文字列フォーマット用のパラメータ
            
        Returns:
            翻訳されたテキスト
        """
        # 現在の言語の翻訳を取得
        lang_translations = self.translations.get(self.current_language.value, {})
        
        # キーが見つからない場合はデフォルト言語を試す
        if key not in lang_translations and self.current_language != self.default_language:
            lang_translations = self.translations.get(self.default_language.value, {})
            
        # それでも見つからない場合はキーをそのまま返す
        text = lang_translations.get(key, key)
        
        # パラメータを適用
        if kwargs:
            try:
                text = text.format(**kwargs)
            except KeyError:
                # フォーマットエラーの場合はそのまま返す
                pass
                
        return text
        
    def get_error_message(self, error_key: str, **kwargs) -> str:
        """エラーメッセージを取得
        
        Args:
            error_key: エラーキー
            **kwargs: 文字列フォーマット用のパラメータ
            
        Returns:
            翻訳されたエラーメッセージ
        """
        return self.get(error_key, **kwargs)
        
    def add_translation(self, language: Language, key: str, value: str) -> None:
        """翻訳を追加
        
        Args:
            language: 言語
            key: 翻訳キー
            value: 翻訳値
        """
        if language.value not in self.translations:
            self.translations[language.value] = {}
            
        self.translations[language.value][key] = value
        
    def has_translation(self, key: str, language: Optional[Language] = None) -> bool:
        """翻訳が存在するかチェック
        
        Args:
            key: 翻訳キー
            language: 言語（省略時は現在の言語）
            
        Returns:
            翻訳が存在するか
        """
        lang = language or self.current_language
        return key in self.translations.get(lang.value, {})
        
    def get_available_languages(self) -> list[Language]:
        """利用可能な言語のリストを取得
        
        Returns:
            言語のリスト
        """
        return [Language(lang) for lang in self.translations.keys()]
        
    def export_translations(self, language: Language, file_path: str) -> None:
        """翻訳をファイルにエクスポート
        
        Args:
            language: 言語
            file_path: エクスポート先のファイルパス
        """
        translations = self.translations.get(language.value, {})
        
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(translations, f, ensure_ascii=False, indent=2)


# グローバルインスタンス
_i18n_manager = I18nManager()


# 便利関数
def _(key: str, **kwargs) -> str:
    """翻訳を取得する便利関数
    
    Args:
        key: 翻訳キー
        **kwargs: 文字列フォーマット用のパラメータ
        
    Returns:
        翻訳されたテキスト
    """
    return _i18n_manager.get(key, **kwargs)


def set_language(language: Language) -> None:
    """言語を設定
    
    Args:
        language: 設定する言語
    """
    _i18n_manager.set_language(language)
    

def get_language() -> Language:
    """現在の言語を取得
    
    Returns:
        現在の言語
    """
    return _i18n_manager.current_language


def get_error_message(error_key: str, **kwargs) -> str:
    """エラーメッセージを取得
    
    Args:
        error_key: エラーキー
        **kwargs: 文字列フォーマット用のパラメータ
        
    Returns:
        翻訳されたエラーメッセージ
    """
    return _i18n_manager.get_error_message(error_key, **kwargs)