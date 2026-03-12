"""
スキルシステム - レジストリ

ToolRegistry と同じシングルトンパターンでスキルを管理する。
"""
import logging
from typing import Dict, List, Optional

from .models import SkillDefinition, SkillTriggerMode

logger = logging.getLogger(__name__)


class SkillRegistry:
    """バックエンド非依存のスキルレジストリ"""

    def __init__(self):
        self._skills: Dict[str, SkillDefinition] = {}
        self._alias_map: Dict[str, str] = {}  # alias(小文字) -> skill name

    def register(self, skill: SkillDefinition) -> None:
        """スキルを登録"""
        self._skills[skill.name] = skill
        # エイリアスマップ構築（名前自体もエイリアス）
        self._alias_map[skill.name.lower()] = skill.name
        for alias in skill.aliases:
            self._alias_map[alias.lower()] = skill.name
        logger.debug(f"[SkillRegistry] 登録: {skill.name}")

    def unregister(self, name: str) -> bool:
        """スキルを登録解除"""
        skill = self._skills.pop(name, None)
        if not skill:
            return False
        # エイリアスマップからも削除
        self._alias_map = {k: v for k, v in self._alias_map.items() if v != name}
        return True

    def get(self, name: str) -> Optional[SkillDefinition]:
        """名前でスキル取得"""
        return self._skills.get(name)

    def get_by_alias(self, alias: str) -> Optional[SkillDefinition]:
        """エイリアスでスキル取得"""
        skill_name = self._alias_map.get(alias.lower())
        if skill_name:
            return self._skills.get(skill_name)
        return None

    def get_all(self) -> List[SkillDefinition]:
        """全スキルを取得"""
        return list(self._skills.values())

    def get_names(self) -> List[str]:
        """全スキル名を取得"""
        return list(self._skills.keys())

    def get_auto_skills(self) -> List[SkillDefinition]:
        """LLM自動呼び出し可能なスキルを取得"""
        return [
            s for s in self._skills.values()
            if s.trigger_mode in (SkillTriggerMode.AUTO, SkillTriggerMode.BOTH)
        ]

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills


# グローバルシングルトン
_global_skill_registry = SkillRegistry()


def get_skill_registry() -> SkillRegistry:
    """グローバルスキルレジストリを取得"""
    return _global_skill_registry


def register_skill(skill: SkillDefinition) -> None:
    """グローバルレジストリにスキルを登録"""
    _global_skill_registry.register(skill)
