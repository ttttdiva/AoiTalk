"""
スキルシステム - YAML ローダー / セーバー

config/skills/*.yaml からスキル定義を読み込み・保存する。
"""
import logging
from pathlib import Path
from typing import List, Optional

import yaml

from .models import SkillDefinition, SkillTriggerMode
from .registry import get_skill_registry, register_skill

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parents[2] / "config" / "skills"


def load_skill_from_yaml(path: Path) -> Optional[SkillDefinition]:
    """YAMLファイルからスキルを1つ読み込む"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        trigger_str = data.get("trigger_mode", "both")
        try:
            trigger_mode = SkillTriggerMode(trigger_str)
        except ValueError:
            trigger_mode = SkillTriggerMode.BOTH

        return SkillDefinition(
            name=data.get("name", path.stem),
            description=data.get("description", ""),
            prompt_template=data.get("prompt_template", "{input}"),
            trigger_mode=trigger_mode,
            aliases=data.get("aliases", []),
            bound_tools=data.get("bound_tools", []),
            examples=data.get("examples", []),
            tags=data.get("tags", []),
            parameters=data.get("parameters", {}),
            source_path=str(path),
        )
    except Exception as e:
        logger.error(f"[SkillLoader] {path} の読み込みに失敗: {e}")
        return None


def load_all_skills(skills_dir: Optional[Path] = None) -> List[SkillDefinition]:
    """スキルディレクトリ内の全YAMLを読み込みレジストリに登録"""
    directory = skills_dir or SKILLS_DIR
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)
        logger.info(f"[SkillLoader] スキルディレクトリを作成: {directory}")
        return []

    skills: List[SkillDefinition] = []
    for yaml_file in sorted(directory.glob("*.yaml")):
        skill = load_skill_from_yaml(yaml_file)
        if skill:
            register_skill(skill)
            skills.append(skill)

    logger.info(f"[SkillLoader] {len(skills)}個のスキルを読み込みました")
    return skills


def save_skill_to_yaml(skill: SkillDefinition, skills_dir: Optional[Path] = None) -> bool:
    """スキルをYAMLファイルに保存"""
    directory = skills_dir or SKILLS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{skill.name}.yaml"

    data = {
        "name": skill.name,
        "description": skill.description,
        "prompt_template": skill.prompt_template,
        "trigger_mode": skill.trigger_mode.value,
        "aliases": skill.aliases,
        "bound_tools": skill.bound_tools,
        "examples": skill.examples,
        "tags": skill.tags,
        "parameters": skill.parameters,
    }

    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        logger.info(f"[SkillLoader] 保存: {skill.name} -> {path}")
        return True
    except Exception as e:
        logger.error(f"[SkillLoader] {skill.name} の保存に失敗: {e}")
        return False


def delete_skill_yaml(name: str, skills_dir: Optional[Path] = None) -> bool:
    """スキルYAMLファイルを削除"""
    directory = skills_dir or SKILLS_DIR
    path = directory / f"{name}.yaml"
    if path.exists():
        path.unlink()
        return True
    return False
