"""
ワークフローシステム - Markdownローダー / セーバー

config/workflows/*.md からワー��フロー定義を読み込み・保存する。
YAMLフロントマター + Markdown本文の形式。
"""
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / "config" / "workflows"


@dataclass
class WorkflowDefinition:
    """ファイルベースのワークフロー定義"""
    name: str
    description: str = ""
    trigger: str = "manual"  # manual / schedule
    schedule: str = ""  # cron式（trigger=schedule時）
    enabled: bool = True
    content: str = ""  # Markdown本文（手順）
    source_path: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "trigger": self.trigger,
            "schedule": self.schedule,
            "enabled": self.enabled,
            "content": self.content,
            "metadata": self.metadata,
        }


def load_workflow_from_md(path: Path) -> Optional[WorkflowDefinition]:
    """Markdownファイルからワークフローを1つ読み込む"""
    try:
        text = path.read_text(encoding="utf-8")

        # --- で区切られたフロント���ターをパース
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                metadata = yaml.safe_load(parts[1]) or {}
                body = parts[2].strip()
            else:
                metadata = {}
                body = text
        else:
            metadata = {}
            body = text

        return WorkflowDefinition(
            name=metadata.get("name", path.stem),
            description=metadata.get("description", ""),
            trigger=metadata.get("trigger", "manual"),
            schedule=metadata.get("schedule", ""),
            enabled=metadata.get("enabled", True),
            content=body,
            source_path=str(path),
            metadata=metadata,
        )
    except Exception as e:
        logger.error(f"[WorkflowLoader] {path} の読み込みに失敗: {e}")
        return None


def load_all_workflows(workflows_dir: Optional[Path] = None) -> List[WorkflowDefinition]:
    """ワークフローディレクトリ内の全.mdを読み込む"""
    directory = workflows_dir or WORKFLOWS_DIR
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)
        logger.info(f"[WorkflowLoader] ワークフローディレクトリを作成: {directory}")
        return []

    workflows: List[WorkflowDefinition] = []
    for md_file in sorted(directory.glob("*.md")):
        wf = load_workflow_from_md(md_file)
        if wf:
            workflows.append(wf)

    logger.info(f"[WorkflowLoader] {len(workflows)}個のワークフローを読み込みました")
    return workflows


def save_workflow_to_md(workflow: WorkflowDefinition, workflows_dir: Optional[Path] = None) -> bool:
    """ワークフローをMarkdownファイルに保存"""
    directory = workflows_dir or WORKFLOWS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{workflow.name}.md"

    frontmatter = {
        "name": workflow.name,
        "description": workflow.description,
        "trigger": workflow.trigger,
        "enabled": workflow.enabled,
    }
    if workflow.schedule:
        frontmatter["schedule"] = workflow.schedule

    try:
        fm_text = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False)
        content = f"---\n{fm_text}---\n\n{workflow.content}\n"
        path.write_text(content, encoding="utf-8")
        logger.info(f"[WorkflowLoader] ���存: {workflow.name} -> {path}")
        return True
    except Exception as e:
        logger.error(f"[WorkflowLoader] {workflow.name} の保存に失敗: {e}")
        return False


def delete_workflow_md(name: str, workflows_dir: Optional[Path] = None) -> bool:
    """ワークフローMarkdownファイルを削除"""
    directory = workflows_dir or WORKFLOWS_DIR
    path = directory / f"{name}.md"
    if path.exists():
        path.unlink()
        logger.info(f"[WorkflowLoader] 削除: {name}")
        return True
    return False


def get_workflow(name: str, workflows_dir: Optional[Path] = None) -> Optional[WorkflowDefinition]:
    """名前でワークフローを取得"""
    directory = workflows_dir or WORKFLOWS_DIR
    path = directory / f"{name}.md"
    if path.exists():
        return load_workflow_from_md(path)
    return None
