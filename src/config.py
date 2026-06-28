"""
Configuration management for Voice Assistant
"""
import os
import copy
from pathlib import Path
from typing import Dict, Any, Optional, List, Union
import yaml
from dotenv import load_dotenv
import logging
from .config_validator import ConfigValidator
from .app_config_store import (
    load_app_config_sync,
    save_app_config_sync,
    update_app_config_key_sync,
)
from .security.field_crypto import redact_secret_value

logger = logging.getLogger(__name__)

DEFAULT_MOBILE_UI_CONFIG: Dict[str, Any] = {
    'enabled': True,
    'default_view': 'chat',
    'quick_commands': [
        {
            'id': 'status_check',
            'label': '状況確認',
            'hint': '現在の状態を一言で報告',
            'action': 'send_message',
            'payload': '現在の進行状況と次のアクションを手短に教えてください。',
            'icon': 'status',
            'accent': 'indigo',
            'category': 'セッション'
        },
        {
            'id': 'memory_summary',
            'label': '会話要約',
            'hint': 'ここまでの内容を要約',
            'action': 'send_message',
            'payload': '会話の重要ポイントを3行以内で要約してください。',
            'icon': 'sparkles',
            'accent': 'violet',
            'category': 'メモ'
        },
        {
            'id': 'character_suggest',
            'label': 'キャラ提案',
            'hint': 'おすすめキャラを提案',
            'action': 'send_message',
            'payload': '今の気分に合いそうなキャラクターを1人提案してください。',
            'icon': 'user',
            'accent': 'cyan',
            'category': 'キャラクター'
        },
        {
            'id': 'clear_chat',
            'label': '履歴クリア',
            'hint': 'チャットを一掃',
            'action': 'clear_chat',
            'icon': 'trash',
            'accent': 'rose',
            'category': 'メンテ',
            'requires_confirmation': True,
            'confirmation_text': 'チャット履歴をクリアしますか？'
        }
    ]
}


class Config:
    """Configuration manager for the Voice Assistant"""
    
    # 環境変数マッピング
    ENV_MAPPINGS = {
        'openai_api_key': 'OPENAI_API_KEY',
        'openrouter_api_key': 'OPENROUTER_API_KEY',
        'openrouter_base_url': 'OPENROUTER_BASE_URL',
        'openrouter_site_url': 'OPENROUTER_SITE_URL',
        'openrouter_app_name': 'OPENROUTER_APP_NAME',
        'discord_bot_token': 'DISCORD_BOT_TOKEN',
        'gemini_api_key': 'GEMINI_API_KEY',
        'ollama_api_key': 'OLLAMA_API_KEY',
        'ollama_base_url': 'OLLAMA_BASE_URL',
        'ollama_model': 'OLLAMA_MODEL',
        'openai_compatible_local_api_key': 'OPENAI_COMPATIBLE_LOCAL_API_KEY',
        'openai_compatible_local_base_url': 'OPENAI_COMPATIBLE_LOCAL_BASE_URL',
        'openai_compatible_local_model': 'OPENAI_COMPATIBLE_LOCAL_MODEL',
        'nijivoice_api_key': 'NIJIVOICE_API_KEY',
        'openweather_api_key': 'OPENWEATHER_API_KEY',
        'voicevox_engine_path': 'VOICEVOX_ENGINE_PATH',
        'voiceroid_engine_path': 'VOICEROID_ENGINE_PATH',
        'coeiroink_engine_path': 'COEIROINK_ENGINE_PATH',
        'aivoice_engine_path': 'AIVOICE_ENGINE_PATH',
        'cevio_engine_path': 'CEVIO_ENGINE_PATH',
        'aivisspeech_engine_path': 'AIVISSPEECH_ENGINE_PATH',
    }
    
    # TTSエンジン設定
    TTS_ENGINES = ['voicevox', 'voiceroid', 'cevio', 'aivoice', 'aivisspeech', 'irodori_tts', 'miotts']
    
    # グローバル必須環境変数は持たない。
    # LLM APIキーは llm_provider ごとに validate_config() で確認する。
    REQUIRED_ENV_VARS: List[str] = []
    
    def __init__(self, config_path: Optional[str] = None):
        """Initialize configuration manager
        
        Args:
            config_path: Path to config.yaml file. If None, uses default location.
        """
        self.root_dir = Path(__file__).parent.parent
        self.config_path = Path(config_path) if config_path else self.root_dir / "config" / "config.yaml"
        
        # Load environment variables
        load_dotenv(self.root_dir / ".env")
        
        # Load configuration
        self.config = self._load_config()
        
        # 環境変数の検証
        self._validate_environment()
        
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from the database, seeding from legacy YAML if present."""
        config = load_app_config_sync(self.config_path)
            
        # 環境別設定をマージ
        environment = os.environ.get("AIVTUBER_ENV")
        if environment:
            env_config_path = self.config_path.parent / f"{environment}.yaml"
            if env_config_path.exists():
                with open(env_config_path, 'r', encoding='utf-8') as f:
                    env_config = yaml.safe_load(f) or {}
                    config = self._merge_configs(config, env_config)
            
        # 環境変数をマージ
        self._merge_env_variables(config)
        
        # TTS設定を初期化
        self._initialize_tts_settings(config)
        
        # 特殊な環境変数設定
        self._set_special_env_settings(config)

        # モバイルUI設定を読み込み
        self._load_mobile_ui_settings(config)

        # 設定のバリデーション
        if self.config_path.exists():
            validator = ConfigValidator(str(self.config_path))
            if not validator.validate(environment):
                logger.warning("Configuration validation failed:")
                for error in validator.get_errors():
                    logger.warning(f"  - {error}")
        
        return config
        
    def _merge_configs(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """設定を再帰的にマージする"""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_configs(result[key], value)
            else:
                result[key] = value
        return result
    
    def _expand_path_vars(self, path: str) -> str:
        """Expand path variables based on platform
        
        Args:
            path: Path string with environment variables
            
        Returns:
            Expanded path string
        """
        if not path:
            return path
            
        # Replace Unix-style $HOME with appropriate Windows equivalent
        if os.name == 'nt' and '$HOME' in path:
            home_path = os.path.expanduser('~')
            path = path.replace('$HOME', home_path)
        
        # Now expand any remaining variables
        return os.path.expandvars(path)
    
    def _merge_env_variables(self, config: Dict[str, Any]) -> None:
        """環境変数をconfig辞書にマージ"""
        for config_key, env_var in self.ENV_MAPPINGS.items():
            value = os.getenv(env_var, '')
            if value and config_key.endswith('_path'):
                value = self._expand_path_vars(value)
            config[config_key] = value
    
    def _initialize_tts_settings(self, config: Dict[str, Any]) -> None:
        """TTS設定を初期化"""
        if 'tts_settings' not in config:
            config['tts_settings'] = {}
        
        # 各TTSエンジンの設定を初期化
        for engine in self.TTS_ENGINES:
            if engine not in config['tts_settings']:
                config['tts_settings'][engine] = {}
            
            # フォールバックパスの設定
            self._set_fallback_paths(config, engine)
    
    def _set_fallback_paths(self, config: Dict[str, Any], engine: str) -> None:
        """TTSエンジンのフォールバックパスを設定"""
        engine_config = config['tts_settings'][engine]
        if 'fallback_paths' not in engine_config:
            engine_config['fallback_paths'] = {}
        
        fallback_env = f'{engine.upper()}_ENGINE_FALLBACK_PATH'
        fallback_path = os.getenv(fallback_env)
        
        if fallback_path:
            platform_key = 'windows' if os.name == 'nt' else 'linux'
            engine_config['fallback_paths'][platform_key] = self._expand_path_vars(fallback_path)
    
    def _set_special_env_settings(self, config: Dict[str, Any]) -> None:
        """特殊な環境変数設定を処理"""
        # VOICEVOX特有の設定 - config.yamlの値を優先
        voicevox_config = config['tts_settings']['voicevox']
        if 'host' not in voicevox_config:
            voicevox_config['host'] = os.getenv('VOICEVOX_HOST', '127.0.0.1')
        if 'port' not in voicevox_config:
            voicevox_config['port'] = int(os.getenv('VOICEVOX_PORT', '50021'))
        
        # AivisSpeech特有の設定 - config.yamlの値を優先
        aivisspeech_config = config['tts_settings']['aivisspeech']
        if 'host' not in aivisspeech_config:
            aivisspeech_config['host'] = os.getenv('AIVISSPEECH_HOST', '127.0.0.1')
        if 'port' not in aivisspeech_config:
            aivisspeech_config['port'] = int(os.getenv('AIVISSPEECH_PORT', '10101'))

        # MioTTS embedded runtime settings. Use AOITALK_* names to avoid
        # conflicting with upstream MioTTS-Inference variables.
        miotts_config = config['tts_settings']['miotts']
        miotts_env_map = {
            'model_id': 'AOITALK_MIOTTS_MODEL_ID',
            'codec_model_id': 'AOITALK_MIOTTS_CODEC_MODEL_ID',
            'refs_dir': 'AOITALK_MIOTTS_REFS_DIR',
            'presets_dir': 'AOITALK_MIOTTS_PRESETS_DIR',
            'cache_dir': 'AOITALK_MIOTTS_CACHE_DIR',
            'device': 'AOITALK_MIOTTS_DEVICE',
            'dtype': 'AOITALK_MIOTTS_DTYPE',
        }
        for key, env_name in miotts_env_map.items():
            value = os.getenv(env_name)
            if value:
                miotts_config[key] = value
        if os.getenv('AOITALK_MIOTTS_DEFAULT_PRESET_ID'):
            miotts_config['default_preset_id'] = os.getenv('AOITALK_MIOTTS_DEFAULT_PRESET_ID')
        if os.getenv('AOITALK_MIOTTS_MAX_TEXT_LENGTH'):
            miotts_config['max_text_length'] = int(os.getenv('AOITALK_MIOTTS_MAX_TEXT_LENGTH', '300'))
        if os.getenv('AOITALK_MIOTTS_MAX_REFERENCE_MB'):
            miotts_config['max_reference_mb'] = int(os.getenv('AOITALK_MIOTTS_MAX_REFERENCE_MB', '20'))
        if os.getenv('AOITALK_MIOTTS_MAX_REFERENCE_SECONDS'):
            miotts_config['max_reference_seconds'] = float(
                os.getenv('AOITALK_MIOTTS_MAX_REFERENCE_SECONDS', '20.0')
            )
        if os.getenv('AOITALK_MIOTTS_TEMPERATURE'):
            miotts_config['temperature'] = float(os.getenv('AOITALK_MIOTTS_TEMPERATURE', '0.8'))
        if os.getenv('AOITALK_MIOTTS_TOP_P'):
            miotts_config['top_p'] = float(os.getenv('AOITALK_MIOTTS_TOP_P', '1.0'))
        if os.getenv('AOITALK_MIOTTS_MAX_TOKENS'):
            miotts_config['max_tokens'] = int(os.getenv('AOITALK_MIOTTS_MAX_TOKENS', '700'))

        # Azure TTS設定
        azure_region = os.getenv('AZURE_TTS_REGION')
        if azure_region:
            if 'azure' not in config['tts_settings']:
                config['tts_settings']['azure'] = {}
            config['tts_settings']['azure']['region'] = azure_region

    def _load_mobile_ui_settings(self, config: Dict[str, Any]) -> None:
        """モバイルUI設定を読み込む"""
        mobile_config = copy.deepcopy(DEFAULT_MOBILE_UI_CONFIG)

        mobile_ui_path = self.config_path.parent / 'mobile_ui.yaml'
        if mobile_ui_path.exists():
            try:
                with open(mobile_ui_path, 'r', encoding='utf-8') as f:
                    loaded = yaml.safe_load(f) or {}
                    if 'mobile_ui' in loaded:
                        loaded = loaded['mobile_ui'] or {}
                    mobile_config = self._merge_configs(mobile_config, loaded)
            except Exception as exc:
                logger.warning(f"モバイルUI設定の読み込みに失敗しました: {exc}")

        config['mobile_ui'] = mobile_config
        
    def _validate_environment(self) -> None:
        """必須環境変数の検証"""
        missing_vars = []
        for var in self.REQUIRED_ENV_VARS:
            if not os.getenv(var):
                missing_vars.append(var)
        
        if missing_vars:
            logger.warning(f"次の必須環境変数が設定されていません: {', '.join(missing_vars)}")
            logger.warning("一部の機能が利用できない可能性があります。")
    
    def validate_config(self) -> Dict[str, List[str]]:
        """設定の完全性を検証し、問題のリストを返す
        
        Returns:
            Dict[str, List[str]]: カテゴリごとの問題リスト
        """
        issues = {
            'errors': [],
            'warnings': []
        }
        
        # 必須設定の確認
        if not self.get('llm_model'):
            issues['errors'].append('llm_modelが設定されていません')
            
        if not self.get('default_character'):
            issues['errors'].append('default_characterが設定されていません')
            
        # API キーの確認
        provider = str(self.config.get('llm_provider', '')).lower()
        if provider == 'openai' and not self.config.get('openai_api_key'):
            issues['warnings'].append('OpenAI APIキーが設定されていません')
        if provider == 'openrouter' and not self.config.get('openrouter_api_key'):
            issues['warnings'].append('OpenRouter APIキーが設定されていません')

        return issues
        
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by key
        
        Args:
            key: Configuration key (supports dot notation)
            default: Default value if key not found
            
        Returns:
            Configuration value
        """
        keys = key.split('.')
        value = self.config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
                
        return value
    
    def set(self, key: str, value: Any) -> None:
        """Set configuration value by key
        
        Args:
            key: Configuration key (supports dot notation)
            value: Value to set
        """
        keys = key.split('.')
        config = self.config
        
        # Navigate to the parent of the target key
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        
        # Set the final key
        config[keys[-1]] = value

    def save_to_file(self, key: str, value: Any) -> bool:
        """Save a specific configuration value to the database.
        
        This method updates both the in-memory config and the file.
        
        Args:
            key: Configuration key (supports dot notation)
            value: Value to set
            
        Returns:
            True if save was successful, False otherwise
        """
        try:
            # First update in-memory config
            self.set(key, value)
            
            if not update_app_config_key_sync(key, value):
                return False
            logger.info("Config saved: %s = %s", key, redact_secret_value(key, value))
            return True
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            return False
        
        
    def get_character_config(self, character_name: str) -> Dict[str, Any]:
        """Load character configuration from DB.

        Args:
            character_name: Name or slug of the character

        Returns:
            Character configuration dictionary (YAML互換形式)
        """
        db_config = self._get_character_from_db(character_name)
        if db_config is not None:
            return db_config

        raise FileNotFoundError(f"Character configuration not found: {character_name}")

    def _get_character_from_db(self, character_name: str) -> Optional[Dict[str, Any]]:
        """DBからキャラクターを取得し、YAML互換形式に変換する。"""
        try:
            from .services.character_service import get_character_for_prompt, _run_sync
            char = _run_sync(get_character_for_prompt(character_name))
            if char is None:
                return None
            return self._db_char_to_yaml_format(char)
        except Exception:
            return None

    @staticmethod
    def _db_char_to_yaml_format(char: Dict[str, Any]) -> Dict[str, Any]:
        """DB Character dictをYAML互換形式に変換する。"""
        return {
            "name": char.get("name", ""),
            "voice": {
                "engine": char.get("voice_engine", ""),
                "voice_name": char.get("voice_name", ""),
                "voice_id": char.get("voice_id", ""),
                "speaker_id": char.get("speaker_id"),
                "parameters": char.get("voice_parameters", {}),
            },
            "personality": {
                "greeting": char.get("greeting", ""),
                "invalidContentReply": char.get("invalid_content_reply", ""),
                "fallbackReply": char.get("fallback_reply", ""),
                "goodbyeReply": char.get("goodbye_reply", ""),
                "details": char.get("system_prompt", ""),
            },
            "recognition_aliases": char.get("recognition_aliases", []),
            # DB固有フィールド（新機能用）
            "_db_character": char,
        }
    
    def get_available_characters(self) -> list[str]:
        """Get list of available character names from DB."""
        try:
            from .services.character_service import list_characters, _run_sync
            db_chars = _run_sync(list_characters(enabled_only=True))
            return sorted(c["name"] for c in db_chars)
        except Exception:
            default_character = self.get("default_character")
            return [default_character] if default_character else []
            
    @property
    def llm_model(self) -> str:
        """Get LLM model name"""
        model = self.get('llm_model')
        if model is None:
            raise ValueError("llm_model must be specified in app configuration")
        return model
        
    @property
    def default_character(self) -> str:
        """Get default character name"""
        character = self.get('default_character')
        if character is None:
            raise ValueError("default_character must be specified in app configuration")
        return character
        
    @property
    def device_index(self) -> int:
        """Get audio device index"""
        device = self.get('device_index')
        if device is None:
            raise ValueError("device_index must be specified in app configuration")
        return device
        
    @property
    def voicevox_path(self) -> str:
        """Get VOICEVOX engine path with proper fallback logic"""
        # First check new TTS settings
        tts_settings = self.get('tts_settings', {})
        voicevox_config = tts_settings.get('voicevox', {})
        if 'engine_path' in voicevox_config and voicevox_config['engine_path']:
            expanded_path = self._expand_path_vars(voicevox_config['engine_path'])
            if os.path.exists(expanded_path):
                return expanded_path
        
        # Try legacy environment variable
        primary_path = self.get('voicevox_engine_path', '')
        if primary_path:
            expanded_primary = self._expand_path_vars(primary_path)
            if os.path.exists(expanded_primary):
                return expanded_primary
                
        # Try fallback paths from config
        fallback_paths = voicevox_config.get('fallback_paths', {})
        if os.name == 'nt' and 'windows' in fallback_paths and fallback_paths['windows']:
            fallback_path = self._expand_path_vars(fallback_paths['windows'])
            if os.path.exists(fallback_path):
                return fallback_path
        elif os.name != 'nt' and 'linux' in fallback_paths and fallback_paths['linux']:
            fallback_path = self._expand_path_vars(fallback_paths['linux'])
            if os.path.exists(fallback_path):
                return fallback_path
        
        # Last resort defaults
        if os.name == 'nt':
            default_path = r"%USERPROFILE%\AppData\Local\Programs\VOICEVOX\vv-engine\run.exe"
        else:
            default_path = "$HOME/voicevox_core/linux-nvidia/run"
            
        return self._expand_path_vars(default_path)
        
    def get_tts_settings(self) -> dict:
        """Get TTS settings"""
        return self.get('tts_settings', {})
    
    def get_chat_window_config(self) -> dict:
        """Get chat window configuration

        Returns:
            Chat window configuration dictionary
        """
        return self.get('chat_window', {
            'title': 'AoiTalk チャット',
            'size': [800, 600]
        })

    def get_mobile_ui_config(self) -> dict:
        """Get mobile UI configuration"""
        return copy.deepcopy(self.get('mobile_ui', DEFAULT_MOBILE_UI_CONFIG))
    
    def get_memory_config(self) -> dict:
        """Get memory configuration
        
        Returns:
            Memory configuration dictionary
        """
        return self.get('memory', {})
    
    def get_conversation_logging_config(self) -> dict:
        """Get conversation logging configuration (now unified under memory settings)
        
        Returns:
            Conversation logging configuration dictionary
        """
        # Use memory settings since logging is now unified with memory
        memory_config = self.get_memory_config()
        default_config = {
            'save_user_messages': True,
            'save_assistant_messages': True,
            'save_system_messages': False,
            'save_function_calls': True,
            'save_successful_only': False,
            'log_retention_days': 365,
            'auto_cleanup_enabled': True,
            'exclude_patterns': []
        }
        
        # Merge with memory config
        for key in default_config:
            if key in memory_config:
                default_config[key] = memory_config[key]
        
        return default_config
    
    @property
    def memory_enabled(self) -> bool:
        """Check if memory features are enabled"""
        return self.get('memory', {}).get('enabled', True)
    
    @property
    def conversation_logging_enabled(self) -> bool:
        """Check if conversation logging is enabled (now unified with memory_enabled)"""
        return self.memory_enabled
    
    @property
    def save_user_messages(self) -> bool:
        """Check if user messages should be saved"""
        return self.get_conversation_logging_config().get('save_user_messages', True)
    
    @property
    def save_assistant_messages(self) -> bool:
        """Check if assistant messages should be saved"""
        return self.get_conversation_logging_config().get('save_assistant_messages', True)
    
    @property
    def save_system_messages(self) -> bool:
        """Check if system messages should be saved"""
        return self.get_conversation_logging_config().get('save_system_messages', False)
    
    @property
    def save_function_calls(self) -> bool:
        """Check if function calls should be saved"""
        return self.get_conversation_logging_config().get('save_function_calls', True)
    
    @property
    def save_successful_only(self) -> bool:
        """Check if only successful interactions should be saved"""
        return self.get_conversation_logging_config().get('save_successful_only', False)
    
    @property
    def log_retention_days(self) -> int:
        """Get log retention period in days"""
        return self.get_conversation_logging_config().get('log_retention_days', 365)
    
    @property
    def auto_cleanup_enabled(self) -> bool:
        """Check if auto cleanup is enabled"""
        return self.get_conversation_logging_config().get('auto_cleanup_enabled', True)
    
    @property
    def exclude_patterns(self) -> list:
        """Get list of exclude patterns for logging"""
        return self.get_conversation_logging_config().get('exclude_patterns', [])
