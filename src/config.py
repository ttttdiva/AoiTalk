"""
Configuration management for Voice Assistant
"""
import os
import copy
import ipaddress
from pathlib import Path
from typing import Dict, Any, Optional, List, Union
from urllib.parse import urlsplit
import yaml
from dotenv import load_dotenv
import logging
from .config_validator import ConfigValidator
from .config_defaults import load_default_config
from .features import Features
from .app_config_store import (
    load_app_config_sync,
    save_app_config_sync,
    update_app_config_key_sync,
)
from .security.field_crypto import redact_secret_value
from .security.secret_env import load_secret_environment
from .tts.irodori_config import normalize_irodori_settings
from .config_errors import (
    CharacterLookupError,
    CharacterNotFoundError,
    build_character_lookup_error,
)

_HF_STANDARD_CACHE_ENV_KEYS = (
    "HF_HOME",
    "HF_HUB_CACHE",
    "HUGGINGFACE_HUB_CACHE",
    "TRANSFORMERS_CACHE",
)

logger = logging.getLogger(__name__)

# Direct API/CLI startup paths may provide Docker-style *_FILE settings
# without the Enterprise container entrypoint.  Resolve them before modules
# such as memory.config snapshot environment-backed dataclass defaults.
load_secret_environment()


def _is_loopback_url(url: str) -> bool:
    """Return whether an HTTP URL points back to the current container/host."""
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return False
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_enterprise_docker_sglang_contract(config: Dict[str, Any]) -> None:
    """Reject a persisted SGLang target that differs from Compose's server."""
    docker_mode = os.getenv("AOITALK_DOCKER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if (
        not Features.is_enterprise()
        or not docker_mode
        or str(config.get("llm_provider") or "").strip().lower() != "sglang"
    ):
        return

    from .llm.sglang_url import resolve_sglang_base_url, resolve_sglang_model

    mismatches: list[str] = []
    expected_model = os.getenv("SGLANG_MODEL", "").strip()
    effective_model = resolve_sglang_model(config).strip()
    if expected_model and effective_model != expected_model:
        mismatches.append(
            f"model: database={effective_model!r}, compose={expected_model!r}"
        )

    expected_base_url = os.getenv("SGLANG_BASE_URL", "").strip().rstrip("/")
    effective_base_url = resolve_sglang_base_url(config).strip().rstrip("/")
    if expected_base_url and _is_loopback_url(expected_base_url):
        mismatches.append(
            "base_url: Compose SGLang URL must use the sglang service name, "
            f"not loopback {expected_base_url!r}"
        )
    if effective_base_url and _is_loopback_url(effective_base_url):
        mismatches.append(
            "base_url: persisted SGLang URL must use the sglang service name, "
            f"not loopback {effective_base_url!r}"
        )
    if expected_base_url and effective_base_url != expected_base_url:
        mismatches.append(
            "base_url: "
            f"database={effective_base_url!r}, compose={expected_base_url!r}"
        )

    if mismatches:
        raise RuntimeError(
            "Enterprise SGLang configuration differs from the Compose service ("
            + "; ".join(mismatches)
            + "). Align the persisted LLM settings with SGLANG_MODEL and "
            "SGLANG_BASE_URL before starting AoiTalk."
        )

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
        'kimi_api_key': 'MOONSHOT_API_KEY',
        'kimi_base_url': 'MOONSHOT_BASE_URL',
        'deepseek_api_key': 'DEEPSEEK_API_KEY',
        'deepseek_base_url': 'DEEPSEEK_BASE_URL',
        'deepinfra_api_key': 'DEEPINFRA_TOKEN',
        'deepinfra_base_url': 'DEEPINFRA_BASE_URL',
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
        if config_path:
            self.config_path = Path(config_path)
        else:
            docker_config = self.root_dir / "config" / "config.docker.yaml"
            use_docker_config = os.getenv("AOITALK_DOCKER", "").lower() in {
                "1", "true", "yes", "on"
            }
            self.config_path = (
                docker_config
                if use_docker_config and docker_config.exists()
                else self.root_dir / "config" / "config.yaml"
            )
        
        # Load environment variables
        # Personal/local launches use the repository .env as the canonical
        # provider credential source.  Override stale Windows environment
        # variables here, then let an explicit deployment secret file win.
        preexisting_hf_cache_env = {
            key: os.environ[key]
            for key in _HF_STANDARD_CACHE_ENV_KEYS
            if key in os.environ
        }
        load_dotenv(self.root_dir / ".env", override=True)
        for key, value in preexisting_hf_cache_env.items():
            os.environ[key] = value
        # dotenv must not override a deployment secret file.  Re-read after
        # dotenv because a native launcher may load .env before Config.
        load_secret_environment()
        
        # Load configuration
        self.config = self._load_config()
        
        # 環境変数の検証
        self._validate_environment()
        
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from the database, seeding from legacy YAML if present."""
        seed_override = None
        environment = os.environ.get("AIVTUBER_ENV") or os.environ.get(
            "AOITALK_PROFILE"
        )
        if environment:
            env_config_path = self.config_path.parent / f"{environment}.yaml"
            if not env_config_path.exists() and Features.is_enterprise():
                raise FileNotFoundError(
                    f"Enterprise configuration overlay is missing: {env_config_path}"
                )
            if env_config_path.exists():
                with open(env_config_path, "r", encoding="utf-8") as f:
                    env_config = yaml.safe_load(f) or {}
                if self.config_path.exists():
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        base_seed = yaml.safe_load(f) or {}
                else:
                    base_seed = load_default_config()
                seed_override = self._merge_configs(base_seed, env_config)

                # The Enterprise overlay is shared by native Linux and
                # Compose.  A Docker container must seed its first DB row with
                # the Compose service name; 127.0.0.1 would point back to the
                # AoiTalk container itself.  This is a first-seed deployment
                # default; an existing DB remains authoritative.
                if os.getenv("AOITALK_DOCKER", "").lower() in {
                    "1", "true", "yes", "on"
                }:
                    docker_sglang_url = os.getenv(
                        "SGLANG_BASE_URL", "http://sglang:30000/v1"
                    ).strip().rstrip("/")
                    seed_override["sglang_base_url"] = docker_sglang_url
                    sglang_seed = seed_override.setdefault("sglang", {})
                    if isinstance(sglang_seed, dict):
                        sglang_seed["base_url"] = docker_sglang_url
                        sglang_seed["host"] = "sglang"

                # Compose passes SGLANG_MODEL even when the default Gemma
                # value is used.  Keep the first DB seed and the SGLang
                # container's served name identical; otherwise a deliberate
                # model override would start SGLang with one name while
                # AoiTalk still asks the database-seeded model for another.
                sglang_model = os.getenv("SGLANG_MODEL", "").strip()
                if sglang_model:
                    seed_override["llm_model"] = sglang_model
                    sglang_seed = seed_override.setdefault("sglang", {})
                    if isinstance(sglang_seed, dict):
                        sglang_seed["model"] = sglang_model

        # Enterprise overlayは初回DB seedへだけ適用し、既存DBの正本を
        # 毎回上書きしない。環境変数は下記でdeployment overrideとして適用する。
        config = load_app_config_sync(
            self.config_path,
            seed_override=seed_override,
        )

        # Existing DB values remain authoritative, but in Enterprise Compose
        # they must identify the model that the paired SGLang container serves.
        # Fail before frontend/Caddy/client startup instead of accepting a
        # healthy server that every chat request addresses incorrectly.
        validate_enterprise_docker_sglang_contract(config)

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
            # Web設定へ保存した接続情報は、空の.env項目で消さない。
            if config_key.startswith(('kimi_', 'deepseek_', 'deepinfra_')) and not value:
                continue
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

        normalize_irodori_settings(config['tts_settings']['irodori_tts'])
    
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

        if Features.is_enterprise():
            mobile_config['enabled'] = False

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
        if provider == 'kimi' and not self.config.get('kimi_api_key'):
            issues['warnings'].append('Kimi APIキーが設定されていません')
        if provider == 'deepseek' and not self.config.get('deepseek_api_key'):
            issues['warnings'].append('DeepSeek APIキーが設定されていません')
        if provider == 'deepinfra' and not self.config.get('deepinfra_api_key'):
            issues['warnings'].append('DeepInfra API tokenが設定されていません')

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
            if not update_app_config_key_sync(key, value):
                return False
            # DBが正本なので、永続化に成功してからメモリ上の値を更新する。
            # 先にsetするとDB障害時だけプロセス内と次回起動の値が乖離する。
            if key == "agent_team" or key.startswith("agent_team."):
                # Agent Team schema-v3 is a single canonical envelope.  A
                # dotted legacy write (for example ``agent_team.members``)
                # is accepted only as migration input by the store; copying
                # that value back into the live Config would reintroduce v2
                # keys after a successful save.  Read the persisted envelope
                # and replace only this managed branch in memory.
                persisted = load_app_config_sync()
                self.config["agent_team"] = copy.deepcopy(
                    persisted.get("agent_team", {})
                    if isinstance(persisted, dict)
                    else {}
                )
            else:
                self.set(key, value)
            logger.info("Config saved: %s = %s", key, redact_secret_value(key, value))
            return True
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            return False
        
        
    def get_character_config(
        self,
        character_name: str,
        *,
        request_id: object | None = None,
        trace_id: object | None = None,
    ) -> Dict[str, Any]:
        """Load character configuration from DB.

        Args:
            character_name: Name or slug of the character

        Returns:
            Character configuration dictionary (YAML互換形式)
        """
        db_config = self._get_character_from_db(
            character_name,
            request_id=request_id,
            trace_id=trace_id,
        )
        if db_config is not None:
            return db_config

        # Keep the historical FileNotFoundError contract so API routes expose
        # a 404 for a genuine missing row.  Database failures are raised from
        # _get_character_from_db as CharacterLookupError and never reach here.
        raise CharacterNotFoundError(character_name)

    def _get_character_from_db(
        self,
        character_name: str,
        *,
        request_id: object | None = None,
        trace_id: object | None = None,
    ) -> Optional[Dict[str, Any]]:
        """DBからキャラクターを取得し、YAML互換形式に変換する。

        ``None`` は有効なキャラクター行が存在しない場合だけ返す。
        PostgreSQL/SQLAlchemy/asyncpg の障害を ``None`` へ変換すると、
        呼び出し元が「設定なし」と誤認してしまうため、秘密値を含まない
        ``CharacterLookupError`` として分類・再送出する。
        """
        # Keep the service's domain exception identity when available.  The
        # sentinel avoids an unbound-local failure if importing the service
        # itself fails during database bootstrap.
        service_not_found_error_type = None
        try:
            from .services.character_service import (
                CharacterNotFoundError as _service_not_found_error_type,
                _run_sync,
                get_character_for_prompt,
            )

            service_not_found_error_type = _service_not_found_error_type

            char = _run_sync(get_character_for_prompt(character_name))
            if char is None:
                return None
            return self._db_char_to_yaml_format(char)
        except Exception as exc:
            # ``get_character_for_prompt`` currently returns None for a miss,
            # but retain compatibility with older service implementations that
            # raised their domain 404 exception instead.
            if (
                isinstance(exc, CharacterNotFoundError)
                or (
                    service_not_found_error_type is not None
                    and isinstance(exc, service_not_found_error_type)
                )
            ):
                return None
            # Some older character-service releases exposed a generic domain
            # exception with a 404 status.  Restrict this compatibility path
            # to that explicit class name; a database error carrying an
            # incidental ``status_code`` must still propagate as a DB failure.
            if (
                type(exc).__name__ == "CharacterNotFoundError"
                and getattr(exc, "status_code", None) == 404
            ):
                return None

            lookup_error = build_character_lookup_error(
                exc,
                trace_id=trace_id,
                request_id=request_id,
            )
            logger.error(
                "Character database lookup failed: category=%s trace_id=%s "
                "request_id=%s exception_type=%s detail=%s",
                lookup_error.category,
                lookup_error.trace_id,
                lookup_error.request_id or "-",
                lookup_error.original_type or type(exc).__name__,
                lookup_error.detail or "-",
            )
            # Do not chain the raw DBAPI exception.  SQLAlchemy/asyncpg
            # exception strings can contain a DSN credential; the typed error
            # is already recorded with a bounded redacted detail above.
            if isinstance(lookup_error, CharacterLookupError):
                raise lookup_error from None
            raise

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
    
    def get_available_character_options(self) -> list[dict[str, str]]:
        """Get stable character identities and display names from DB."""
        try:
            from .services.character_service import list_characters, _run_sync
            db_chars = _run_sync(list_characters(enabled_only=True))
            # An initialized database with no enabled rows is a legitimate
            # empty-catalog state.  Keep the legacy default-character fallback
            # only for that explicit result; connection/auth/schema failures
            # are handled below and must not become a plausible default list.
            if not db_chars:
                return self._default_character_options()
            options = [
                {"slug": str(c["slug"]), "name": str(c["name"])}
                for c in db_chars
                if c.get("slug") and c.get("name")
            ]
            has_canonical_project_manager = any(
                option["slug"] == "project_manager" for option in options
            )
            if has_canonical_project_manager:
                options = [
                    option
                    for option in options
                    if option["slug"] != "project_management_assistant"
                ]
            return sorted(options, key=lambda item: (item["name"], item["slug"]))
        except (ImportError, ModuleNotFoundError):
            # Character persistence is optional for lightweight/local installs
            # where the service module is intentionally unavailable.  Do not
            # use this fallback for runtime database failures from list query.
            return self._default_character_options()
        except CharacterLookupError:
            raise
        except Exception as exc:
            lookup_error = build_character_lookup_error(exc)
            logger.error(
                "Character list database lookup failed: category=%s trace_id=%s "
                "request_id=%s exception_type=%s detail=%s",
                lookup_error.category,
                lookup_error.trace_id,
                lookup_error.request_id or "-",
                lookup_error.original_type or type(exc).__name__,
                lookup_error.detail or "-",
            )
            raise lookup_error from None

    def _default_character_options(self) -> list[dict[str, str]]:
        """Return the local default only when the catalog is absent/optional."""

        default_character = self.get("default_character")
        return (
            [{"slug": default_character, "name": default_character}]
            if default_character
            else []
        )

    def get_available_characters(self) -> list[str]:
        """Get unique display names for legacy clients."""
        return sorted(
            {option["name"] for option in self.get_available_character_options()}
        )
            
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
