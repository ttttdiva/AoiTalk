"""
Skills パッケージ — プロンプトテンプレートベースのスキルシステム

起動時に config/skills/*.yaml を自動読み込みする。
"""
import logging

from ..utils.logging_config import FILE_ONLY_LOG_EXTRA

logger = logging.getLogger(__name__)

from .models import SkillDefinition, SkillTriggerMode
from .registry import SkillRegistry, get_skill_registry, register_skill
from .loader import load_all_skills, save_skill_to_yaml, delete_skill_yaml

# パッケージインポート時にスキルを自動読み込み
_loaded_skills = load_all_skills()
if _loaded_skills:
    logger.info(
        "Skill registry initialized (%s skills registered)",
        len(_loaded_skills),
        extra=FILE_ONLY_LOG_EXTRA,
    )

__all__ = [
    "SkillDefinition",
    "SkillTriggerMode",
    "SkillRegistry",
    "get_skill_registry",
    "register_skill",
    "load_all_skills",
    "save_skill_to_yaml",
    "delete_skill_yaml",
]
