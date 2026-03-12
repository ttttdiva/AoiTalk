"""
スキルシステム - データモデル

SkillDefinition: バックエンド非依存のスキル定義
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum


class SkillTriggerMode(Enum):
    """スキルのトリガーモード"""
    MANUAL = "manual"   # /skill-name のみ
    AUTO = "auto"       # LLM自動判断のみ
    BOTH = "both"       # 両方


@dataclass
class SkillDefinition:
    """バックエンド非依存のスキル定義"""
    name: str
    description: str
    prompt_template: str
    trigger_mode: SkillTriggerMode = SkillTriggerMode.BOTH
    aliases: List[str] = field(default_factory=list)
    bound_tools: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    parameters: Dict[str, Any] = field(default_factory=dict)
    source_path: Optional[str] = None

    def render_prompt(self, user_input: str, **kwargs) -> str:
        """プロンプトテンプレートをレンダリング

        Args:
            user_input: ユーザーの入力テキスト
            **kwargs: パラメータのオーバーライド値

        Returns:
            レンダリング済みプロンプト
        """
        # デフォルト値をベースにkwargsでオーバーライド
        render_vars = {"input": user_input}
        for param_name, param_def in self.parameters.items():
            if isinstance(param_def, dict):
                render_vars[param_name] = kwargs.get(param_name, param_def.get("default", ""))
            else:
                render_vars[param_name] = kwargs.get(param_name, param_def)

        try:
            return self.prompt_template.format(**render_vars)
        except KeyError as e:
            # 未定義の変数があってもクラッシュしない
            return self.prompt_template.replace("{input}", user_input)

    def to_dict(self) -> Dict[str, Any]:
        """API応答用にシリアライズ"""
        return {
            "name": self.name,
            "description": self.description,
            "prompt_template": self.prompt_template,
            "trigger_mode": self.trigger_mode.value,
            "aliases": self.aliases,
            "bound_tools": self.bound_tools,
            "examples": self.examples,
            "tags": self.tags,
            "parameters": self.parameters,
        }
