"""既存の /inbox Task・メール記録をプロジェクト配下のInbox項目へ移行する。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, text

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.memory.database import get_database_manager
from src.memory.models import (
    KnowledgeEdge,
    KnowledgeNode,
    KnowledgeNodePlacement,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
    Project,
    Task,
    TaskReference,
)
from src.services.docs_graph_service import DocsGraphService
from src.services.project_information_docs import is_default_inbox_project
from src.services.work_intake_docs_service import WorkIntakeDocsService


def _status_label(task_status: str) -> str:
    return {
        "closed": "完了",
        "review": "レビュー待ち",
        "on_hold": "確認待ち",
        "in_progress": "対応中",
    }.get(str(task_status or ""), "対応中")


def _metadata_with_inbox(task: Task, node_id: UUID) -> dict[str, Any]:
    metadata = dict(task.task_metadata or {})
    work_intake = dict(metadata.get("work_intake") or {})
    work_intake["inbox_item_id"] = str(node_id)
    metadata["work_intake"] = work_intake
    return metadata


def _partition_explicit_ids(
    items: list[Any],
    allowed_ids: set[UUID] | None,
) -> tuple[list[Any], list[Any]]:
    """Select only caller-approved rows and leave every other row untouched."""
    allowed = allowed_ids or set()
    selected = [item for item in items if item.id in allowed]
    remaining = [item for item in items if item.id not in allowed]
    return selected, remaining


async def _collect(
    session,
    *,
    allowed_unlinked_mail_ids: set[UUID] | None = None,
    allowed_archive_task_ids: set[UUID] | None = None,
) -> dict[str, Any]:
    projects = list(
        (
            await session.execute(
                select(Project).where(Project.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    default_ids = {project.id for project in projects if is_default_inbox_project(project)}
    real_projects = {project.id: project for project in projects if project.id not in default_ids}

    tasks = list(
        (
            await session.execute(
                select(Task).where(
                    Task.source == "work_intake",
                    Task.deleted_at.is_(None),
                    Task.archived_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    task_refs = list(
        (
            await session.execute(
                select(TaskReference).where(
                    TaskReference.reference_type == "docs_node",
                    TaskReference.relation_type == "source",
                )
            )
        )
        .scalars()
        .all()
    )
    refs_by_task: dict[UUID, list[UUID]] = {}
    referenced_mail_ids: set[UUID] = set()
    work_intake_task_ids = {task.id for task in tasks}
    for ref in task_refs:
        if ref.task_id not in work_intake_task_ids or not ref.target_id:
            continue
        try:
            target_id = UUID(str(ref.target_id))
        except (TypeError, ValueError):
            continue
        refs_by_task.setdefault(ref.task_id, []).append(target_id)
        referenced_mail_ids.add(target_id)

    mail_nodes = list(
        (
            await session.execute(
                select(KnowledgeNode).where(
                    KnowledgeNode.archived_at.is_(None),
                    KnowledgeNode.system_key.like("project_mail:%"),
                    ~KnowledgeNode.system_key.like("project_mail_management:%"),
                )
            )
        )
        .scalars()
        .all()
    )
    existing_inbox_item_keys = set(
        (
            await session.execute(
                select(KnowledgeNode.system_key).where(
                    KnowledgeNode.archived_at.is_(None),
                    KnowledgeNode.system_key.like("project_inbox_item:%"),
                )
            )
        )
        .scalars()
        .all()
    )
    inbox_item_nodes = list(
        (
            await session.execute(
                select(KnowledgeNode).where(
                    KnowledgeNode.archived_at.is_(None),
                    KnowledgeNode.system_key.like("project_inbox_item:%"),
                )
            )
        )
        .scalars()
        .all()
    )
    inbox_item_nodes = [
        node
        for node in inbox_item_nodes
        if str(node.system_key or "").count(":") == 2
    ]
    inbox_management_nodes = list(
        (
            await session.execute(
                select(KnowledgeNode).where(
                    KnowledgeNode.archived_at.is_(None),
                    KnowledgeNode.system_key.like("project_inbox_management:%"),
                )
            )
        )
        .scalars()
        .all()
    )
    legacy_inbox_roots = list(
        (
            await session.execute(
                select(KnowledgeNode)
                .join(
                    KnowledgeNodeSupertag,
                    KnowledgeNodeSupertag.node_id == KnowledgeNode.id,
                )
                .join(
                    KnowledgeSupertag,
                    KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id,
                )
                .where(
                    KnowledgeNode.project_id.in_(default_ids),
                    KnowledgeNode.parent_id.is_(None),
                    KnowledgeNode.archived_at.is_(None),
                    KnowledgeNode.title == "Inbox 案件情報",
                    KnowledgeSupertag.system_key == "project_info",
                )
            )
        )
        .scalars()
        .all()
    )
    legacy_root_dependencies: dict[UUID, dict[str, int]] = {}
    for root in legacy_inbox_roots:
        descendants = list(
            (
                await session.execute(
                    select(KnowledgeNode.id).where(
                        KnowledgeNode.root_page_id == root.id
                    )
                )
            )
            .scalars()
            .all()
        )
        node_ids = [root.id, *descendants]
        task_count = len(
            list(
                (
                    await session.execute(
                        select(Task.id).where(Task.knowledge_node_id.in_(node_ids))
                    )
                )
                .scalars()
                .all()
            )
        )
        ref_count = len(
            list(
                (
                    await session.execute(
                        select(TaskReference.id).where(
                            TaskReference.target_id.in_(
                                [str(node_id) for node_id in node_ids]
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
        )
        edge_count = len(
            list(
                (
                    await session.execute(
                        select(KnowledgeEdge.id).where(
                            KnowledgeEdge.source_node_id.in_(node_ids)
                            | KnowledgeEdge.target_node_id.in_(node_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
        )
        placement_count = len(
            list(
                (
                    await session.execute(
                        select(KnowledgeNodePlacement.id).where(
                            KnowledgeNodePlacement.node_id.in_(node_ids)
                            | KnowledgeNodePlacement.parent_node_id.in_(node_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
        )
        legacy_root_dependencies[root.id] = {
            "tasks": task_count,
            "task_references": ref_count,
            "edges": edge_count,
            "placements": placement_count,
        }
    migratable_tasks = [
        task
        for task in tasks
        if task.project_id in real_projects and task.knowledge_node_id is None
    ]
    already_bound_tasks = [
        task
        for task in tasks
        if task.project_id in real_projects and task.knowledge_node_id is not None
    ]
    default_tasks = [task for task in tasks if task.project_id in default_ids]
    explicit_archive_tasks, skipped_default_tasks = _partition_explicit_ids(
        default_tasks,
        allowed_archive_task_ids,
    )
    unlinked_mails = [
        node
        for node in mail_nodes
        if node.project_id in real_projects
        and node.id in (allowed_unlinked_mail_ids or set())
        and node.id not in referenced_mail_ids
        and (
            f"project_inbox_item:{node.project_id}:"
            f"{hashlib.sha256(f'migration-mail:{node.id}'.encode('utf-8')).hexdigest()}"
        )
        not in existing_inbox_item_keys
    ]
    return {
        "all_projects": {project.id: project for project in projects},
        "projects": real_projects,
        "refs_by_task": refs_by_task,
        "migratable_tasks": migratable_tasks,
        "already_bound_tasks": already_bound_tasks,
        "explicit_archive_tasks": explicit_archive_tasks,
        "skipped_default_tasks": skipped_default_tasks,
        "unlinked_mails": unlinked_mails,
        "mail_nodes": mail_nodes,
        "inbox_item_nodes": inbox_item_nodes,
        "inbox_management_nodes": inbox_management_nodes,
        "legacy_inbox_roots": legacy_inbox_roots,
        "legacy_root_dependencies": legacy_root_dependencies,
    }


def _report(collected: dict[str, Any]) -> dict[str, Any]:
    return {
        "migratable_task_count": len(collected["migratable_tasks"]),
        "already_bound_task_count": len(collected["already_bound_tasks"]),
        "archive_only_mail_candidate_count": len(collected["unlinked_mails"]),
        "mail_node_count": len(collected["mail_nodes"]),
        "inbox_management_node_count": len(collected["inbox_management_nodes"]),
        "inbox_item_count": len(collected["inbox_item_nodes"]),
        "explicit_archive_task_count": len(collected["explicit_archive_tasks"]),
        "skipped_default_inbox_task_count": len(collected["skipped_default_tasks"]),
        "legacy_inbox_project_root_count": len(collected["legacy_inbox_roots"]),
        "projects": [
            {
                "id": str(project_id),
                "name": project.name,
                "migratable_tasks": sum(
                    task.project_id == project_id
                    for task in collected["migratable_tasks"]
                ),
                "archive_only_mail_candidates": sum(
                    node.project_id == project_id
                    for node in collected["unlinked_mails"]
                ),
            }
            for project_id, project in collected["projects"].items()
            if any(
                task.project_id == project_id
                for task in collected["migratable_tasks"]
            )
            or any(
                node.project_id == project_id
                for node in collected["unlinked_mails"]
            )
        ],
        "task_ids": [str(task.id) for task in collected["migratable_tasks"]],
        "archive_only_mail_candidate_ids": [
            str(node.id) for node in collected["unlinked_mails"]
        ],
        "explicit_archive_task_ids": [
            str(task.id) for task in collected["explicit_archive_tasks"]
        ],
        "skipped_default_inbox_task_ids": [
            str(task.id) for task in collected["skipped_default_tasks"]
        ],
        "legacy_inbox_project_root_ids": [
            str(node.id) for node in collected["legacy_inbox_roots"]
        ],
        "legacy_inbox_project_root_dependencies": {
            str(node_id): counts
            for node_id, counts in collected["legacy_root_dependencies"].items()
        },
    }


def _write_backup(collected: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "work_inbox_items_before_apply",
        "audit": _report(collected),
        "tasks": [
            {
                "id": str(task.id),
                "project_id": str(task.project_id),
                "knowledge_node_id": (
                    str(task.knowledge_node_id) if task.knowledge_node_id else None
                ),
                "title": task.title,
                "source": task.source,
                "archived_at": (
                    task.archived_at.isoformat() if task.archived_at else None
                ),
                "task_metadata": task.task_metadata or {},
            }
            for task in [
                *collected["migratable_tasks"],
                *collected["explicit_archive_tasks"],
            ]
        ],
        "legacy_inbox_projects": [
            {
                "project_id": str(node.project_id),
                "root_node_id": str(node.id),
                "project_knowledge_node_id": (
                    str(collected["all_projects"][node.project_id].knowledge_node_id)
                    if node.project_id in collected["all_projects"]
                    and collected["all_projects"][node.project_id].knowledge_node_id
                    else str(node.id)
                ),
            }
            for node in collected["legacy_inbox_roots"]
        ],
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


async def _apply(session, collected: dict[str, Any]) -> dict[str, Any]:
    created_items: list[str] = []
    bound_tasks: list[str] = []
    archive_only_items: list[str] = []
    archived_explicit_tasks: list[str] = []
    archived_legacy_roots: list[str] = []
    refs_by_task = collected["refs_by_task"]

    for task in collected["migratable_tasks"]:
        project = collected["projects"][task.project_id]
        service = WorkIntakeDocsService(session)
        source_ids = refs_by_task.get(task.id, [])
        item = await service.create_item(
            project_id=task.project_id,
            user_id=task.created_by or project.owner_id,
            source_key=f"migration-task:{task.id}",
            title=task.title,
            classification="request",
            instruction="",
            summary=task.description or "",
            status=_status_label(task.status),
            source_node_ids=source_ids,
            source_refs=[{"type": "task", "id": str(task.id)}],
            has_mail=bool(source_ids),
        )
        await service.bind_task(
            item_id=item.node_id,
            task_id=task.id,
            user_id=task.created_by or project.owner_id,
        )
        task.task_metadata = _metadata_with_inbox(task, item.node_id)
        created_items.append(str(item.node_id))
        bound_tasks.append(str(task.id))

    for mail_node in collected["unlinked_mails"]:
        project = collected["projects"][mail_node.project_id]
        item = await WorkIntakeDocsService(session).create_item(
            project_id=mail_node.project_id,
            user_id=mail_node.created_by or project.owner_id,
            source_key=f"migration-mail:{mail_node.id}",
            title=mail_node.title,
            classification="information_share",
            instruction="",
            summary="既存のメール原本をInbox項目として一覧化しました。",
            status="保存のみ",
            source_node_ids=[mail_node.id],
            source_refs=[{"type": "docs_node", "id": str(mail_node.id)}],
            has_mail=True,
        )
        created_items.append(str(item.node_id))
        archive_only_items.append(str(item.node_id))

    archived_at = datetime.utcnow()
    for task in collected["explicit_archive_tasks"]:
        task.archived_at = archived_at
        task.updated_at = archived_at
        archived_explicit_tasks.append(str(task.id))

    for root in collected["legacy_inbox_roots"]:
        dependencies = collected["legacy_root_dependencies"].get(root.id, {})
        if any(int(value or 0) for value in dependencies.values()):
            raise ValueError(
                f"依存関係がある旧Inbox案件情報は自動整理できません: "
                f"{root.id} {dependencies}"
            )
        project = await session.get(Project, root.project_id)
        if project is None or not is_default_inbox_project(project):
            raise ValueError(
                f"旧Inbox案件情報の所属確認に失敗しました: {root.id}"
            )
        await DocsGraphService(session).archive_subtree(
            root=root,
            user_id=root.updated_by or root.created_by or project.owner_id,
        )
        if project.knowledge_node_id == root.id:
            project.knowledge_node_id = None
        archived_legacy_roots.append(str(root.id))

    await session.commit()
    return {
        "created_or_reused_item_ids": created_items,
        "bound_task_ids": bound_tasks,
        "archive_only_item_ids": archive_only_items,
        "archived_explicit_task_ids": archived_explicit_tasks,
        "archived_legacy_inbox_root_ids": archived_legacy_roots,
    }


async def main_async(args: argparse.Namespace) -> int:
    manager = get_database_manager()
    session = await manager.get_session()
    try:
        allowed_unlinked_mail_ids = {
            UUID(value) for value in (args.unlinked_mail_id or [])
        }
        allowed_archive_task_ids = {
            UUID(value) for value in (args.archive_task_id or [])
        }
        collected = await _collect(
            session,
            allowed_unlinked_mail_ids=allowed_unlinked_mail_ids,
            allowed_archive_task_ids=allowed_archive_task_ids,
        )
        report = _report(collected)
        if args.mode == "audit":
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.mode == "apply":
            await session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('aoitalk-work-inbox-migration-v1'))"
                )
            )
            collected = await _collect(
                session,
                allowed_unlinked_mail_ids=allowed_unlinked_mail_ids,
                allowed_archive_task_ids=allowed_archive_task_ids,
            )
            report = _report(collected)
            backup_path = Path(args.backup) if args.backup else Path(
                "artifacts"
            ) / f"work-inbox-migration-{datetime.now():%Y%m%d-%H%M%S}.json"
            _write_backup(collected, backup_path)
            result = await _apply(session, collected)
            verified = _report(
                await _collect(
                    session,
                    allowed_unlinked_mail_ids=allowed_unlinked_mail_ids,
                    allowed_archive_task_ids=allowed_archive_task_ids,
                )
            )
            print(
                json.dumps(
                    {
                        "backup": str(backup_path.resolve()),
                        "before": report,
                        "applied": result,
                        "after": verified,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        raise ValueError(f"unsupported mode: {args.mode}")
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["audit", "apply"])
    parser.add_argument(
        "--backup",
        help="apply前の復旧用メタデータJSON。省略時はartifacts配下へ生成。",
    )
    parser.add_argument(
        "--unlinked-mail-id",
        action="append",
        default=[],
        help=(
            "タスク参照のない既存メールをInbox項目化する場合のメールノードUUID。"
            "受付単位を推測しないため、明示したIDだけを対象にする。"
        ),
    )
    parser.add_argument(
        "--archive-task-id",
        action="append",
        default=[],
        help=(
            "既定Inboxプロジェクト上の検証用Taskを整理する場合のTask UUID。"
            "名称から推測せず、明示したIDだけをアーカイブする。"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parse_args())))
