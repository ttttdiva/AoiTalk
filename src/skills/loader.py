"""
スキルシステム - YAML ローダー / セーバー

config/skills/*.yaml からスキル定義を読み込み・保存する。
"""
import logging
import re
from pathlib import Path
from typing import List, Optional
from uuid import UUID

import yaml

from .models import SkillDefinition, SkillTriggerMode
from .registry import get_skill_registry, register_skill
from ..services.project_workspace_cleanup import get_project_workspace_path
from ..services.skill_target_io import (
    SkillTargetTransitionPending,
    delete_skill_text,
    publish_skill_text,
    read_stable_skill_text,
)

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parents[2] / "config" / "skills"

# Keep the process-local global registry in sync when a canonical YAML is
# deleted at runtime.  Without this ledger, a previously loaded
# removed workflow entries could remain executable until process restart.
_LOADED_GLOBAL_SKILL_NAMES: set[str] = set()


def parse_skill_yaml_text(
    text: str,
    path: Path,
) -> Optional[SkillDefinition]:
    """Parse one already-stabilized YAML Skill without rereading the target."""
    try:
        data = yaml.safe_load(text) or {}

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


def load_skill_from_yaml(path: Path) -> Optional[SkillDefinition]:
    """YAMLファイルからスキルを1つ読み込む"""
    try:
        text = read_stable_skill_text(path)
    except SkillTargetTransitionPending:
        raise
    except Exception as exc:
        logger.error("[SkillLoader] %s の読み込みに失敗: %s", path, exc)
        return None
    if text is None:
        return None
    return parse_skill_yaml_text(text, path)


def load_all_skills(skills_dir: Optional[Path] = None) -> List[SkillDefinition]:
    """スキルディレクトリ内の全YAMLを読み込みレジストリに登録"""
    directory = skills_dir or SKILLS_DIR
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)
        logger.info(f"[SkillLoader] スキルディレクトリを作成: {directory}")
        return []

    skills: List[SkillDefinition] = []
    registry = get_skill_registry()
    yaml_files = sorted(directory.glob("*.yaml"))
    current_names = {path.stem for path in yaml_files}
    for stale_name in tuple(_LOADED_GLOBAL_SKILL_NAMES - current_names):
        registry.unregister(stale_name)
        _LOADED_GLOBAL_SKILL_NAMES.discard(stale_name)
    # Also revoke entries loaded by an older process image (or manually
    # registered in an embedding) whose source file has since disappeared.
    try:
        directory_resolved = directory.resolve()
        for loaded in tuple(registry.get_all()):
            source = getattr(loaded, "source_path", None)
            if not source:
                continue
            source_path = Path(str(source))
            if (
                source_path.parent.resolve() == directory_resolved
                and source_path.suffix.casefold() == ".yaml"
                and source_path.stem not in current_names
            ):
                registry.unregister(getattr(loaded, "name", source_path.stem))
    except (OSError, RuntimeError):
        logger.debug("Skill registry stale-source cleanup skipped", exc_info=True)
    for yaml_file in yaml_files:
        try:
            skill = load_skill_from_yaml(yaml_file)
        except SkillTargetTransitionPending:
            logger.warning(
                "[SkillLoader] 未完了 transition 中の Skill を fail-closed で除外: %s",
                yaml_file,
            )
            # A previous successful load may still be live in the global
            # registry.  Pending/invalid journal state must revoke that stale
            # executable entry as well as skipping the current file.
            registry.unregister(yaml_file.stem)
            _LOADED_GLOBAL_SKILL_NAMES.discard(yaml_file.stem)
            continue
        if skill:
            register_skill(skill)
            _LOADED_GLOBAL_SKILL_NAMES.add(skill.name)
            skills.append(skill)
        else:
            # Invalid/unavailable canonical files likewise cannot leave the
            # previously loaded version executable through the registry.
            registry.unregister(yaml_file.stem)
            _LOADED_GLOBAL_SKILL_NAMES.discard(yaml_file.stem)

    logger.info(f"[SkillLoader] {len(skills)}個のスキルを読み込みました")
    return skills


def load_skill_from_markdown(path: Path) -> Optional[SkillDefinition]:
    """Load a workspace ``SKILL.md`` with YAML frontmatter."""
    try:
        content = read_stable_skill_text(path)
    except SkillTargetTransitionPending:
        raise
    except Exception as exc:
        logger.warning(
            "[SkillLoader] 壊れた SKILL.md をスキップ: %s: %s",
            path,
            exc,
        )
        return None
    if content is None:
        return None
    try:
        match = re.fullmatch(
            r"---\r?\n(?P<frontmatter>.*?)\r?\n---(?:\r?\n)?(?P<body>.*)",
            content,
            flags=re.DOTALL,
        )
        if match is None:
            raise ValueError("YAML frontmatter がありません")
        frontmatter = match.group("frontmatter")
        body = match.group("body")
        data = yaml.safe_load(frontmatter) or {}
        if not isinstance(data, dict):
            raise ValueError("frontmatter は mapping である必要があります")
        name = str(data.get("name") or path.parent.name).strip()
        description = str(data.get("description") or "").strip()
        if not name:
            raise ValueError("name が空です")
        for field in ("aliases", "bound_tools"):
            if field in data and not isinstance(data[field], list):
                raise ValueError(f"{field} は配列である必要があります")
            if field in data and not all(isinstance(item, str) for item in data[field]):
                raise ValueError(f"{field} の要素は文字列である必要があります")
        if "parameters" in data and not isinstance(data["parameters"], dict):
            raise ValueError("parameters は mapping である必要があります")
        trigger_mode = SkillTriggerMode(str(data.get("trigger_mode", "both")))
        return SkillDefinition(
            name=name, description=description, prompt_template=body.strip(),
            aliases=list(data.get("aliases") or []), bound_tools=list(data.get("bound_tools") or []),
            trigger_mode=trigger_mode, parameters=dict(data.get("parameters") or {}), source_path=str(path),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SkillLoader] 壊れた SKILL.md をスキップ: %s: %s", path, exc)
        return None


def load_project_skills(project_id: str, *, workspace_root: str | Path | None = None) -> List[SkillDefinition]:
    workspace = get_project_workspace_path(UUID(str(project_id)), workspace_root=workspace_root)
    skills_root = workspace / ".agents" / "skills"
    skills = []
    for path in sorted(skills_root.glob("*/SKILL.md")):
        try:
            path.resolve().relative_to(workspace.resolve())
        except ValueError:
            logger.warning("workspace 外を指す SKILL.md を拒否: %s", path)
            continue
        try:
            skill = load_skill_from_markdown(path)
        except SkillTargetTransitionPending:
            logger.warning(
                "[SkillLoader] 未完了 transition 中の Project Skill を "
                "fail-closed で除外: %s",
                path,
            )
            continue
        if skill:
            skills.append(skill)
    get_skill_registry().replace_project_skills(str(project_id), skills)
    return skills


def serialize_skill_to_yaml(skill: SkillDefinition) -> str:
    """Serialize a Skill exactly once for atomic publication/hash calculation."""
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
    return yaml.dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )


def save_skill_to_yaml(skill: SkillDefinition, skills_dir: Optional[Path] = None) -> bool:
    """スキルをYAMLファイルに保存"""
    directory = skills_dir or SKILLS_DIR
    path = directory / f"{skill.name}.yaml"

    try:
        publish_skill_text(path, serialize_skill_to_yaml(skill))
        logger.info(f"[SkillLoader] 保存: {skill.name} -> {path}")
        return True
    except Exception as e:
        logger.error(f"[SkillLoader] {skill.name} の保存に失敗: {e}")
        return False


def delete_skill_yaml(name: str, skills_dir: Optional[Path] = None) -> bool:
    """スキルYAMLファイルを削除"""
    directory = skills_dir or SKILLS_DIR
    path = directory / f"{name}.yaml"
    try:
        return delete_skill_text(path)
    except Exception as exc:
        logger.error("[SkillLoader] %s の削除に失敗: %s", path, exc)
        return False
