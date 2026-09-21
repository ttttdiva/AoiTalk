"""
キーワード検出システム初期化
"""

from typing import Optional, Any, Dict
import logging
from ...features import Features
from ...utils.logging_config import FILE_ONLY_LOG_EXTRA
from .manager import get_keyword_manager
from .detectors.speech_rate_detector import SpeechRateDetector
from .detectors.character_switch_detector import CharacterSwitchDetector


logger = logging.getLogger(__name__)


def initialize_keyword_detectors(llm_client: Optional[Any] = None, config: Optional[Any] = None) -> None:
    """
    キーワード検出システムを初期化
    
    Args:
        llm_client: LLMクライアント
        config: 設定オブジェクト
    """
    manager = get_keyword_manager()
    
    # 設定からキーワード検出設定を取得
    keyword_config = _get_keyword_config(config)
    
    # キーワード検出全体が無効な場合は何もしない
    if not keyword_config.get('enabled', True):
        logger.info(
            "Keyword detection is disabled by configuration",
        )
        return
    
    # Spotify LLM検出器を登録
    try:
        spotify_config = keyword_config.get('spotify', {})
        # Keyword detection is a separate toggle from Integration
        # availability.  Spotify detection is effective only when both are
        # enabled; the Integration defaults to disabled and therefore avoids
        # any Spotify-specific LLM call on fresh installs.
        spotify_enabled = bool(
            _spotify_integration_enabled(config)
            and spotify_config.get('enabled', True)
        )
        
        if spotify_enabled and Features.entertainment():
            from .detectors.spotify_detector import SpotifyLLMKeywordDetector

            spotify_detector = SpotifyLLMKeywordDetector(
                enabled=spotify_enabled,
                llm_client=llm_client,
                config=config,
            )
            # 設定パラメータを検出器に渡す
            if hasattr(spotify_detector, 'use_llm_extraction'):
                spotify_detector.use_llm_extraction = spotify_config.get('use_llm_extraction', True)
            if hasattr(spotify_detector, 'confidence_threshold'):
                spotify_detector.confidence_threshold = spotify_config.get('confidence_threshold', 0.7)
            if hasattr(spotify_detector, 'fallback_to_regex'):
                spotify_detector.fallback_to_regex = spotify_config.get('fallback_to_regex', True)

            manager.register_detector(spotify_detector)
            logger.info(
                "Spotify keyword detector registered (enabled=%s)",
                spotify_enabled,
            )
        else:
            # A process may reinitialize after the Integration toggle changes;
            # remove any previously registered detector so a stale instance
            # cannot issue Spotify LLM/tool calls while disabled.
            manager.unregister_detector("spotify")
            logger.info(
                "Spotify keyword detector disabled",
            )

    except Exception:
        logger.warning(
            "Spotify keyword detector setup failed; continuing without it",
            exc_info=True,
            extra=FILE_ONLY_LOG_EXTRA,
        )
    
    # 話速調整検出器を登録
    try:
        speech_rate_config = keyword_config.get('speech_rate', {})
        speech_rate_enabled = speech_rate_config.get('enabled', True)
        
        if speech_rate_enabled:
            speech_rate_detector = SpeechRateDetector(
                enabled=speech_rate_enabled,
                llm_client=llm_client,
                config=config
            )
            # 設定パラメータを検出器に渡す
            if hasattr(speech_rate_detector, 'use_llm_extraction'):
                speech_rate_detector.use_llm_extraction = speech_rate_config.get('use_llm_extraction', True)
            if hasattr(speech_rate_detector, 'confidence_threshold'):
                speech_rate_detector.confidence_threshold = speech_rate_config.get('confidence_threshold', 0.7)
            if hasattr(speech_rate_detector, 'fallback_to_regex'):
                speech_rate_detector.fallback_to_regex = speech_rate_config.get('fallback_to_regex', True)

            manager.register_detector(speech_rate_detector)
            logger.info(
                "Speech-rate keyword detector registered (enabled=%s)",
                speech_rate_enabled,
            )
        else:
            logger.info(
                "Speech-rate keyword detector disabled",
            )

    except Exception:
        logger.warning(
            "Speech-rate keyword detector setup failed; continuing without it",
            exc_info=True,
            extra=FILE_ONLY_LOG_EXTRA,
        )
    
    # キャラクター切り替え検出器を登録
    try:
        character_switch_config = keyword_config.get('character_switch', {})
        character_switch_enabled = character_switch_config.get('enabled', True)
        
        if character_switch_enabled:
            character_switch_detector = CharacterSwitchDetector(
                enabled=character_switch_enabled,
                config=config
            )

            manager.register_detector(character_switch_detector)
            logger.info(
                "Character-switch keyword detector registered (enabled=%s)",
                character_switch_enabled,
            )
        else:
            logger.info(
                "Character-switch keyword detector disabled",
            )

    except Exception:
        logger.warning(
            "Character-switch keyword detector setup failed; continuing without it",
            exc_info=True,
            extra=FILE_ONLY_LOG_EXTRA,
        )
    
    # 将来的に他の検出器を追加する場合はここに記述
    # 例: TTS制御、モード切り替えなど
    
    # 初期化完了ログ
    status = manager.get_status()
    logger.info(
        "Keyword detection initialized (detectors=%s, enabled=%s)",
        status['total_detectors'],
        status['enabled_detectors'],
    )


def _get_keyword_config(config: Optional[Any]) -> Dict[str, Any]:
    """
    設定からキーワード検出設定を取得
    
    Args:
        config: 設定オブジェクト
        
    Returns:
        キーワード検出設定辞書
    """
    default_config = {
        'enabled': True,
        'llm_model': 'gpt-5.6-luna',
        'spotify': {
            'enabled': False,
            'use_llm_extraction': True,
            'confidence_threshold': 0.7,
            'fallback_to_regex': True
        },
        'speech_rate': {
            'enabled': True,
            'use_llm_extraction': True,
            'confidence_threshold': 0.7,
            'fallback_to_regex': True
        },
        'character_switch': {
            'enabled': True
        }
    }
    
    if not config:
        return default_config
    
    try:
        # Config object のパターン
        if hasattr(config, 'keyword_detection'):
            keyword_config = config.keyword_detection
            if isinstance(keyword_config, dict):
                # Dict型設定をマージ
                result = default_config.copy()
                result.update(keyword_config)
                return result
            elif hasattr(keyword_config, '__dict__'):
                # Object型設定を辞書に変換
                return vars(keyword_config)
        
        # Dict型のパターン
        elif isinstance(config, dict) and 'keyword_detection' in config:
            result = default_config.copy()
            result.update(config['keyword_detection'])
            return result
        elif hasattr(config, 'get'):
            configured = config.get('keyword_detection', {})
            if isinstance(configured, dict):
                result = default_config.copy()
                result.update(configured)
                return result
    
    except Exception:
        logger.warning(
            "Keyword detection configuration could not be read; using defaults",
            exc_info=True,
            extra=FILE_ONLY_LOG_EXTRA,
        )
    
    return default_config


def _spotify_integration_enabled(config: Optional[Any]) -> bool:
    """Read canonical Shared Integration availability; fail closed."""

    if config is None:
        return False
    try:
        if isinstance(config, dict):
            value = config
            for part in ('integrations', 'spotify', 'enabled'):
                if not isinstance(value, dict) or part not in value:
                    return False
                value = value[part]
            return bool(value)
        getter = getattr(config, 'get', None)
        if callable(getter):
            value = getter('integrations.spotify.enabled', None)
            return bool(value) if value is not None else False
    except Exception:
        return False
    return False


def get_llm_client_for_keywords(config: Optional[Any] = None):
    """
    キーワード検出用のLLMクライアントを取得
    
    Args:
        config: 設定オブジェクト
        
    Returns:
        LLMクライアント
    """
    try:
        from ...llm.manager import AgentLLMClient
        from ...config import Config
        import os
        
        # 設定からキーワード検出設定を取得
        keyword_config = _get_keyword_config(config)
        llm_model = keyword_config.get('llm_model', 'gpt-5.6-luna')
        
        # 設定を読み込み（引数のconfigが無い場合のみ）
        if not config:
            config = Config()
        
        api_key = os.getenv('OPENAI_API_KEY')

        if not api_key:
            logger.warning(
                "OPENAI_API_KEY is not configured; keyword LLM extraction is disabled",
            )
            return None
        
        # 設定に応じたLLMクライアントを作成
        llm_client = AgentLLMClient(
            api_key=api_key,
            model=llm_model,  # 設定ファイルからモデルを取得
            config=config
        )
        
        logger.info(
            "Keyword LLM client initialized (model=%s)",
            llm_model,
        )
        return llm_client

    except Exception:
        logger.warning(
            "Keyword LLM client initialization failed; using regex fallback",
            exc_info=True,
            extra=FILE_ONLY_LOG_EXTRA,
        )
        return None


def setup_keyword_detection(config: Optional[Any] = None) -> None:
    """
    キーワード検出システムのセットアップ
    
    Args:
        config: 設定オブジェクト
    """
    try:
        # LLMクライアントを取得
        llm_client = get_llm_client_for_keywords(config)
        
        # キーワード検出器を初期化
        initialize_keyword_detectors(llm_client, config)
        
    except Exception:
        logger.warning(
            "Keyword detection setup failed; continuing without keyword extraction",
            exc_info=True,
            extra=FILE_ONLY_LOG_EXTRA,
        )
        # エラーが発生してもシステムは継続動作
