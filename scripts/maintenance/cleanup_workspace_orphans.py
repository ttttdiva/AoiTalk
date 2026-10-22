"""Audit and quarantine unreferenced persistent workspace data.

The default is a read-only audit. ``--apply`` moves candidates to a restorable
Recycle Bin batch. Restore a batch with ``--restore-batch`` after stopping
workspace writers. This command handles only Docs attachments, Project roots,
and User roots. Its reference reader also classifies Chat, App/run, generated
media, Files-root, and selected QA-manifest references so other lifecycles do
not make a workspace candidate look unowned.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.memory.database import get_database_manager
from src.memory.models import KnowledgeAttachment, Project, User
from src.services.workspace_gc import (
    WorkspaceGcReport,
    apply_workspace_gc,
    audit_workspace,
    resolve_workspaces_root,
    restore_workspace_gc,
)
from src.services.workspace_gc_references import load_workspace_reference_inventory


# Shared with other maintenance jobs only as a namespace for this process. The
# lock prevents two copies of this script from applying overlapping reports.
LOCK_KEY = 0x414F4957534743  # AOIWSGC


async def run(
    *,
    apply: bool,
    workspace_root: str | None = None,
    service_stopped: bool = False,
    restore_batch: str | None = None,
) -> int:
    if (apply or restore_batch) and not service_stopped:
        print(
            "--apply / --restore-batch は通常のworkspace writerを停止した状態でのみ実行できます。"
            "確認済みなら --service-stopped を付けて再実行してください。",
            file=sys.stderr,
        )
        return 2
    root = resolve_workspaces_root(workspace_root)
    if restore_batch:
        restored = restore_workspace_gc(restore_batch, workspace_root=root)
        print(
            json.dumps(
                {"mode": "restore", "result": restored},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    # Do not call DatabaseManager.get_session(): that path initializes Alembic
    # migrations as a side effect. This maintenance command only needs a direct
    # read-only transaction against an already-running schema.
    db_manager = get_database_manager()
    session = db_manager.SessionLocal()
    lock_acquired = False
    try:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_KEY})
        lock_acquired = True
        report = await _load_report(session, root)
        payload: dict[str, Any] = {
            "mode": "apply" if apply else "dry-run",
            "report": report.to_dict(),
        }
        if apply:
            # The advisory lock serializes maintenance jobs, but normal API
            # writers do not take it. Re-read DB ownership immediately before
            # moving so a just-committed upload/project/user is not moved
            # from a stale first snapshot. Operators should still run --apply
            # while the service is stopped (see docs/workspace_storage_cleanup.md).
            fresh_report = await _load_report(session, root)
            if _report_signature(report) != _report_signature(fresh_report):
                payload.update(
                    {
                        "mode": "aborted",
                        "reason": "監査後にDBまたはworkspaceの状態が変化しました。再実行してください。",
                        "report": fresh_report.to_dict(),
                    }
                )
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 2
            report = fresh_report
            payload["report"] = report.to_dict()
            payload["quarantined"] = apply_workspace_gc(report)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        if lock_acquired:
            await session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY}
            )
        await session.close()


async def _load_report(session, root: Path) -> WorkspaceGcReport:
    attachment_references = list(
        (await session.execute(select(KnowledgeAttachment.file_path))).scalars().all()
    )
    project_ids = list((await session.execute(select(Project.id))).scalars().all())
    user_ids = list((await session.execute(select(User.id))).scalars().all())
    references = await load_workspace_reference_inventory(
        session,
        workspace_root=root,
        repository_root=ROOT_DIR,
        qa_manifest_path=os.environ.get("AOITALK_EMPLOYEE_QA_MANIFEST") or None,
    )
    return audit_workspace(
        workspace_root=root,
        attachment_references=(
            *attachment_references,
            *references.workspace_references,
        ),
        project_ids=(*project_ids, *references.project_ids),
        user_ids=(*user_ids, *references.user_ids),
    )


def _report_signature(report: WorkspaceGcReport) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(str(path) for path in paths)
        for paths in (
            report.docs_orphans,
            report.project_orphans,
            report.user_orphans,
            report.empty_dirs,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ワークスペースの未参照ファイル・孤児ディレクトリを監査します（既定はdry-run）。"
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--apply",
        action="store_true",
        help="監査したDocs添付・orphan Project/User rootをRecycle Binへ移動する",
    )
    operation.add_argument(
        "--restore-batch",
        help="UUIDで指定したRecycle Bin batchを元のpathへ復元する（上書きしない）",
    )
    parser.add_argument(
        "--workspace-root",
        help="対象workspace root（既定: AOITALK_WORKSPACES_DIR または ./workspaces）",
    )
    parser.add_argument(
        "--service-stopped",
        action="store_true",
        help="--apply時に通常のworkspace writerを停止済みであることを明示する",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(
        run(
            apply=args.apply,
            workspace_root=args.workspace_root,
            service_stopped=args.service_stopped,
            restore_batch=args.restore_batch,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
