"""
設定ファイルのバリデーション処理
"""
import os
from typing import Dict, Any, List, Optional
from pathlib import Path
import yaml
from pydantic import BaseModel, Field, field_validator, ValidationError
from typing import Union


class TTSEngineConfig(BaseModel):
    """TTSエンジン設定のバリデーションモデル"""
    host: str = "127.0.0.1"
    port: int = Field(gt=0, le=65535)
    use_gpu: Optional[bool] = False


class SpeechRecognitionEngineConfig(BaseModel):
    """音声認識エンジン設定のバリデーションモデル"""
    model: str
    language: Optional[str] = "ja"
    temperature: Optional[float] = Field(0.0, ge=0.0, le=1.0)
    fp16: Optional[bool] = True
    chunk_length: Optional[float] = Field(1.0, gt=0.0)


class MemoryConfig(BaseModel):
    """メモリ設定のバリデーションモデル"""
    enabled: bool = True
    max_context_tokens: int = Field(8000, gt=0)
    history_batch_size: int = Field(100, gt=0)
    similarity_threshold: float = Field(0.3, ge=0.0, le=1.0)
    preload_embedding_model: bool = False


class ReasoningConfig(BaseModel):
    """推論モード設定のバリデーションモデル"""
    enabled: bool = False
    complexity_threshold: float = Field(0.6, ge=0.0, le=1.0)
    max_steps: int = Field(5, gt=0)
    step_timeout: int = Field(30, gt=0)
    display_mode: str = Field("progress", pattern="^(silent|progress|detailed|debug)$")


class DatabaseConfig(BaseModel):
    """データベース設定のバリデーションモデル"""
    # PostgreSQL only - no configuration needed


class Config(BaseModel):
    """メイン設定のバリデーションモデル"""
    default_character: str
    llm_model: str
    llm_provider: str = Field(pattern="^(openai|gemini|gemini-cli|claude-cli|codex-cli|sglang)$")
    mode: str = Field(pattern="^(terminal|voice_chat|discord)$")
    device_index: int = Field(0, ge=0)
    
    memory: Optional[MemoryConfig] = MemoryConfig()
    reasoning: Optional[ReasoningConfig] = ReasoningConfig()
    
    @field_validator('llm_model')
    def validate_llm_model(cls, v, info):
        """LLMモデルとプロバイダーの組み合わせを検証"""
        provider = info.data.get('llm_provider')
        if provider == 'openai' and not v.startswith(('gpt-', 'o1-')):
            raise ValueError(f"OpenAI provider requires model starting with 'gpt-' or 'o1-', got: {v}")
        if provider == 'gemini' and not v.startswith('gemini-'):
            raise ValueError(f"Gemini provider requires model starting with 'gemini-', got: {v}")
        # SGLang: any model name is valid (HuggingFace model names, local paths, etc.)
        return v


class ConfigValidator:
    """設定ファイルのバリデーター"""
    
    def __init__(self, base_config_path: str = "config/config.yaml"):
        self.base_config_path = Path(base_config_path)
        self.errors: List[str] = []
        
    def load_yaml(self, file_path: Path) -> Optional[Dict[str, Any]]:
        """YAMLファイルを読み込む"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            self.errors.append(f"Failed to load {file_path}: {str(e)}")
            return None
            
    def merge_configs(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """設定を再帰的にマージする"""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self.merge_configs(result[key], value)
            else:
                result[key] = value
        return result
        
    def validate(self, environment: Optional[str] = None) -> bool:
        """設定ファイルをバリデートする
        
        Args:
            environment: 環境名（development, production等）
            
        Returns:
            bool: バリデーション成功時True
        """
        self.errors.clear()
        
        # ベース設定を読み込む
        base_config = self.load_yaml(self.base_config_path)
        if not base_config:
            return False
            
        # 環境別設定を読み込んでマージ
        if environment:
            env_config_path = self.base_config_path.parent / f"{environment}.yaml"
            if env_config_path.exists():
                env_config = self.load_yaml(env_config_path)
                if env_config:
                    base_config = self.merge_configs(base_config, env_config)
                    
        # 必須フィールドの存在チェック
        required_fields = ['default_character', 'llm_model', 'llm_provider', 'mode']
        for field in required_fields:
            if field not in base_config:
                self.errors.append(f"Required field '{field}' is missing")
                
        # Pydanticでバリデーション
        try:
            Config(**base_config)
        except ValidationError as e:
            for error in e.errors():
                field_path = '.'.join(str(loc) for loc in error['loc'])
                self.errors.append(f"{field_path}: {error['msg']}")
                
        # キャラクター設定の存在チェック
        default_char = base_config.get('default_character')
        if default_char:
            # ずんだもん -> zundamon のような変換を試みる
            char_file_name = default_char.replace('ずんだもん', 'zundamon')
            char_config_path = self.base_config_path.parent / "characters" / f"{char_file_name}.yaml"
            if not char_config_path.exists():
                # 元の名前でも試す
                char_config_path = self.base_config_path.parent / "characters" / f"{default_char}.yaml"
                if not char_config_path.exists():
                    self.errors.append(f"Character config for '{default_char}' not found")
        
        return len(self.errors) == 0
                
    def get_errors(self) -> List[str]:
        """エラーメッセージのリストを取得"""
        return self.errors
        
    def print_errors(self):
        """エラーメッセージを表示"""
        if self.errors:
            print("Configuration validation errors:")
            for error in self.errors:
                print(f"  - {error}")
        else:
            print("Configuration validation successful!")


def validate_config(environment: Optional[str] = None) -> bool:
    """設定ファイルをバリデートする便利関数
    
    Args:
        environment: 環境名（development, production等）
        
    Returns:
        bool: バリデーション成功時True
    """
    validator = ConfigValidator()
    is_valid = validator.validate(environment)
    if not is_valid:
        validator.print_errors()
    return is_valid


if __name__ == "__main__":
    # 環境変数から環境名を取得
    env = os.environ.get("AIVTUBER_ENV", "development")
    
    print(f"Validating configuration for environment: {env}")
    if validate_config(env):
        print("✅ All configuration checks passed!")
    else:
        print("❌ Configuration validation failed!")
        exit(1)
