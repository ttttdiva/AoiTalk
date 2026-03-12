"""
Feature Flags System for AoiTalk

モジュラー機能の有効/無効を制御するシステム。
環境変数またはプロファイルファイルからFeature Flagsを読み込む。

使い方:
    from src.features import Features
    
    if Features.voice_input():
        from src.audio import AudioManager
    
    # または
    if Features.is_enabled("voice_input"):
        ...
"""

import os
import logging
from pathlib import Path
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)


class Features:
    """Feature Flags manager for modular functionality"""
    
    # Default values (personal mode - all features enabled)
    DEFAULTS: Dict[str, bool] = {
        "voice_input": True,       # マイク入力 (音声認識)
        "tts_output": True,        # 音声読み上げ (TTS)
        "discord_bot": True,       # Discord Bot機能
        "crawler_status": True,    # クローラーステータス監視
        "entertainment": True,     # Spotify/YouTube/ニコニコ等
        "code_agent": False,       # コード保守エージェント（企業用）
    }
    
    # Enterprise profile - minimal features for code maintenance
    ENTERPRISE_DEFAULTS: Dict[str, bool] = {
        "voice_input": False,
        "tts_output": False,
        "discord_bot": False,
        "crawler_status": False,
        "entertainment": False,
        "code_agent": True,
    }
    
    _profile_cache: Optional[Dict[str, bool]] = None
    _initialized: bool = False
    
    @classmethod
    def _get_profile_settings(cls) -> Dict[str, bool]:
        """Load profile settings from YAML file if specified"""
        if cls._profile_cache is not None:
            return cls._profile_cache
        
        profile_name = os.getenv("AOITALK_PROFILE", "").lower()
        
        if profile_name == "enterprise":
            cls._profile_cache = cls.ENTERPRISE_DEFAULTS.copy()
            logger.info("Feature Flags: Enterprise プロファイルを使用")
        elif profile_name == "personal":
            cls._profile_cache = cls.DEFAULTS.copy()
            logger.info("Feature Flags: Personal プロファイルを使用")
        elif profile_name:
            # Try to load from YAML file
            profile_path = Path(__file__).parent.parent / "config" / "profiles" / f"{profile_name}.yaml"
            if profile_path.exists():
                try:
                    import yaml
                    with open(profile_path, 'r', encoding='utf-8') as f:
                        profile_data = yaml.safe_load(f) or {}
                    features = profile_data.get('features', {})
                    cls._profile_cache = {**cls.DEFAULTS, **features}
                    logger.info(f"Feature Flags: {profile_name} プロファイルを読み込みました")
                except Exception as e:
                    logger.warning(f"Feature Flags: プロファイル読み込みエラー: {e}")
                    cls._profile_cache = cls.DEFAULTS.copy()
            else:
                logger.warning(f"Feature Flags: プロファイル {profile_name} が見つかりません")
                cls._profile_cache = cls.DEFAULTS.copy()
        else:
            # No profile specified, use defaults
            cls._profile_cache = cls.DEFAULTS.copy()
        
        return cls._profile_cache
    
    @classmethod
    def is_enabled(cls, feature: str) -> bool:
        """
        Check if a feature is enabled.
        
        Priority order:
        1. Environment variable (FEATURE_XXXX)
        2. Profile settings (AOITALK_PROFILE)
        3. Default values
        
        Args:
            feature: Feature name (e.g., "voice_input", "tts_output")
            
        Returns:
            True if the feature is enabled
        """
        # 1. Check environment variable first (highest priority)
        env_key = f"FEATURE_{feature.upper()}"
        env_value = os.getenv(env_key)
        if env_value is not None:
            result = env_value.lower() in ("true", "1", "yes")
            if not cls._initialized:
                logger.debug(f"Feature '{feature}': {result} (from env: {env_key})")
            return result
        
        # 2. Check profile settings
        profile_settings = cls._get_profile_settings()
        if feature in profile_settings:
            result = profile_settings[feature]
            if not cls._initialized:
                logger.debug(f"Feature '{feature}': {result} (from profile)")
            return result
        
        # 3. Fall back to defaults
        result = cls.DEFAULTS.get(feature, False)
        if not cls._initialized:
            logger.debug(f"Feature '{feature}': {result} (from defaults)")
        return result
    
    @classmethod
    def initialize(cls) -> None:
        """Initialize and log all feature flags (call once at startup)"""
        if cls._initialized:
            return
        
        logger.info("=== Feature Flags ===")
        for feature in cls.DEFAULTS.keys():
            status = "有効" if cls.is_enabled(feature) else "無効"
            logger.info(f"  {feature}: {status}")
        logger.info("====================")
        cls._initialized = True
    
    @classmethod
    def get_all(cls) -> Dict[str, bool]:
        """Get all feature flags as a dictionary"""
        return {feature: cls.is_enabled(feature) for feature in cls.DEFAULTS.keys()}
    
    @classmethod
    def reset_cache(cls) -> None:
        """Reset cached profile settings (for testing)"""
        cls._profile_cache = None
        cls._initialized = False
    
    # Convenience class methods for specific features
    @classmethod
    def voice_input(cls) -> bool:
        """Check if voice input (microphone/ASR) is enabled"""
        return cls.is_enabled("voice_input")
    
    @classmethod
    def tts_output(cls) -> bool:
        """Check if TTS output is enabled"""
        return cls.is_enabled("tts_output")
    
    @classmethod
    def discord_bot(cls) -> bool:
        """Check if Discord bot functionality is enabled"""
        return cls.is_enabled("discord_bot")
    
    @classmethod
    def crawler_status(cls) -> bool:
        """Check if crawler status monitoring is enabled"""
        return cls.is_enabled("crawler_status")
    
    @classmethod
    def entertainment(cls) -> bool:
        """Check if entertainment features (Spotify, YouTube, etc.) are enabled"""
        return cls.is_enabled("entertainment")
    
    @classmethod
    def code_agent(cls) -> bool:
        """Check if code maintenance agent is enabled"""
        return cls.is_enabled("code_agent")
