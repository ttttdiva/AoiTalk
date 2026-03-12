"""
Skills パッケージ — プロンプトテンプレートベースのスキルシステム

起動時に config/skills/*.yaml を自動読み込みする。
"""
from .models import SkillDefinition, SkillTriggerMode
from .registry import SkillRegistry, get_skill_registry, register_skill
from .loader import load_all_skills, save_skill_to_yaml, delete_skill_yaml

# パッケージインポート時にスキルを自動読み込み
_loaded_skills = load_all_skills()
if _loaded_skills:
    print(f"[Skills] {len(_loaded_skills)}個のスキルを登録しました")

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
