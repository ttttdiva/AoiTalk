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
        'discord_bot_token': 'DISCORD_BOT_TOKEN',
        'gemini_api_key': 'GEMINI_API_KEY',
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
    TTS_ENGINES = ['voicevox', 'voiceroid', 'cevio', 'aivoice', 'aivisspeech']
    
    # 必須環境変数
    REQUIRED_ENV_VARS = ['OPENAI_API_KEY']
    
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
        """Load configuration from YAML file"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
            
        with open(self.config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
            
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
        if not self.config.get('openai_api_key'):
            issues['warnings'].append('OpenAI APIキーが設定されていません')
            
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
        """Save a specific configuration value to config.yaml
        
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
            
            # Read the existing file config
            with open(self.config_path, 'r', encoding='utf-8') as f:
                file_config = yaml.safe_load(f) or {}
            
            # Navigate to the parent and set the value
            keys = key.split('.')
            current = file_config
            for k in keys[:-1]:
                if k not in current:
                    current[k] = {}
                current = current[k]
            current[keys[-1]] = value
            
            # Write back to file
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(file_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            
            logger.info(f"Config saved: {key} = {value}")
            return True
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            return False
        
        
    def get_character_config(self, character_name: str) -> Dict[str, Any]:
        """Load character configuration
        
        Args:
            character_name: Name of the character
            
        Returns:
            Character configuration dictionary
        """
        # First, try direct character name lookup
        characters_dir = self.root_dir / 'config' / 'characters'
        
        # Try exact match first
        file_name = character_name.replace(' ', '_').lower() + '.yaml'
        character_path = characters_dir / file_name
        
        if character_path.exists():
            with open(character_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        
        # If not found, try all available character files
        for yaml_file in characters_dir.glob('*.yaml'):
            try:
                with open(yaml_file, 'r', encoding='utf-8') as f:
                    char_config = yaml.safe_load(f)
                    # Check if this character config matches the requested name
                    if self._character_name_matches(character_name, char_config, yaml_file.stem):
                        return char_config
            except Exception:
                continue
                
        raise FileNotFoundError(f"Character configuration not found: {character_name}")
    
    def _character_name_matches(self, requested_name: str, char_config: Dict[str, Any], file_stem: str) -> bool:
        """Check if a character config matches the requested name"""
        # Check against file stem
        if requested_name.lower() == file_stem.lower():
            return True
            
        # Check against name field in config
        if 'name' in char_config and requested_name.lower() == char_config['name'].lower():
            return True
            
        # Check against recognition aliases if they exist
        if 'recognition_aliases' in char_config:
            aliases = char_config['recognition_aliases']
            if isinstance(aliases, list):
                for alias in aliases:
                    if requested_name.lower() == alias.lower():
                        return True
                        
        return False
    
    def get_available_characters(self) -> list[str]:
        """Get list of available character names from config files"""
        characters_dir = self.root_dir / 'config' / 'characters'
        character_names = []
        
        for yaml_file in characters_dir.glob('*.yaml'):
            try:
                with open(yaml_file, 'r', encoding='utf-8') as f:
                    char_config = yaml.safe_load(f)
                    # Use the name field if available, otherwise use file stem
                    name = char_config.get('name', yaml_file.stem)
                    character_names.append(name)
            except Exception:
                # If config is invalid, use file stem as fallback
                character_names.append(yaml_file.stem)
                
        return sorted(character_names)
            
    @property
    def llm_model(self) -> str:
        """Get LLM model name"""
        model = self.get('llm_model')
        if model is None:
            raise ValueError("llm_model must be specified in config.yaml")
        return model
        
    @property
    def default_character(self) -> str:
        """Get default character name"""
        character = self.get('default_character')
        if character is None:
            raise ValueError("default_character must be specified in config.yaml")
        return character
        
    @property
    def device_index(self) -> int:
        """Get audio device index"""
        device = self.get('device_index')
        if device is None:
            raise ValueError("device_index must be specified in config.yaml")
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
    
    @property
    def chat_mode(self) -> bool:
        """Get chat mode setting"""
        return self.get('chat_mode', False)
    
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
