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
        self._project_skills: Dict[str, Dict[str, SkillDefinition]] = {}
        self._project_aliases: Dict[str, Dict[str, str]] = {}

    def register(self, skill: SkillDefinition, project_id: str | None = None) -> None:
        """スキルを登録"""
        if project_id is not None:
            key = str(project_id)
            skills = self._project_skills.setdefault(key, {})
            aliases = self._project_aliases.setdefault(key, {})
            skills[skill.name] = skill
            aliases[skill.name.lower()] = skill.name
            for alias in skill.aliases:
                aliases[alias.lower()] = skill.name
            return
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

    def replace_project_skills(self, project_id: str, skills: List[SkillDefinition]) -> None:
        self._project_skills.pop(str(project_id), None)
        self._project_aliases.pop(str(project_id), None)
        for skill in skills:
            self.register(skill, project_id=str(project_id))

    def get(self, name: str, project_id: str | None = None) -> Optional[SkillDefinition]:
        """名前でスキル取得"""
        if project_id is not None:
            skill = self._project_skills.get(str(project_id), {}).get(name)
            if skill:
                return skill
        return self._skills.get(name)

    def get_by_alias(self, alias: str, project_id: str | None = None) -> Optional[SkillDefinition]:
        """エイリアスでスキル取得"""
        if project_id is not None:
            project_name = self._project_aliases.get(str(project_id), {}).get(alias.lower())
            if project_name:
                return self._project_skills[str(project_id)].get(project_name)
        skill_name = self._alias_map.get(alias.lower())
        if skill_name:
            return self._skills.get(skill_name)
        return None

    def get_all(self, project_id: str | None = None) -> List[SkillDefinition]:
        """全スキルを取得"""
        merged = dict(self._skills)
        if project_id is not None:
            merged.update(self._project_skills.get(str(project_id), {}))
        return list(merged.values())

    def get_names(self, project_id: str | None = None) -> List[str]:
        """全スキル名を取得"""
        return [skill.name for skill in self.get_all(project_id)]

    def get_auto_skills(self, project_id: str | None = None) -> List[SkillDefinition]:
        """LLM自動呼び出し可能なスキルを取得"""
        return [
            s for s in self.get_all(project_id)
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
