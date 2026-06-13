"""
スキル・ワークフロー作成ツール

チャットからLLMが直接スキルやワークフローを作成するためのツール。
runtime_tool_registry に登録して使用する。
"""

import re
from .core import tool as tool_decorator


_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,98}[a-z0-9]$")


def _validate_name(name: str) -> str:
    """名前のバリデーション。不正なら例外を送出。"""
    name = name.strip().lower().replace(" ", "_")
    if not _NAME_PATTERN.match(name):
        raise ValueError(
            f"名前 '{name}' は無効です。小文字英数字・ハイフン・アンダースコアのみ使用可能（2〜100文字）"
        )
    return name


@tool_decorator
def create_skill(name: str, description: str, prompt_template: str) -> str:
    """新しいスキルを作成してconfig/skills/に保存する。チャットでユーザーが「この手順をスキルにして」等と言った場合に使用。

    Args:
        name: スキル名（小文字英数字、ハイフン、アンダースコア）
        description: スキルの説明（日本語OK）
        prompt_template: プロンプトテンプレート。{input}でユーザー入力を参照可能
    """
    try:
        name = _validate_name(name)

        from ..skills.models import SkillDefinition, SkillTriggerMode
        from ..skills.loader import save_skill_to_yaml
        from ..skills.registry import register_skill

        skill = SkillDefinition(
            name=name,
            description=description,
            prompt_template=prompt_template,
            trigger_mode=SkillTriggerMode.BOTH,
        )

        if not save_skill_to_yaml(skill):
            return f"スキル '{name}' の保存に失敗しました"

        register_skill(skill)
        return f"スキル '{name}' を作成しました（config/skills/{name}.yaml）"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"スキル作成エラー: {e}"
