#!/usr/bin/env python
"""Non-interactive entry point for Windows Task Scheduler Automation triggers."""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.memory.database import DatabaseManager  # noqa: E402
from src.memory.models import AutomationProgram, AutomationProgramRevision, User  # noqa: E402
from src.services.media_operations_automation_service import MediaOperationsAutomationService  # noqa: E402


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="aoi automation", description="Run an AoiTalk Automation Program non-interactively.")
    sub = value.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Trigger one Automation Program")
    run.add_argument("program_id")
    run.add_argument("--trigger-key", default=None, help="Explicit occurrence key. Omit to derive from the recipe's dedupe period.")
    return value


def derived_trigger_key(trigger: dict, now: datetime) -> str:
    period = str(trigger.get("dedupe_period") or "daily").strip().lower()
    if period == "minute": token = now.strftime("%Y-%m-%dT%H:%M")
    elif period == "hourly": token = now.strftime("%Y-%m-%dT%H")
    else: token = now.strftime("%Y-%m-%d")
    return f"task-scheduler:{period}:{token}"


async def run_program(program_id: str, trigger_key: str | None) -> int:
    manager = DatabaseManager()
    try:
        if not await manager.initialize():
            print("ERROR database_unavailable", file=sys.stderr)
            return 3
        session = await manager.get_session()
        try:
            try:
                parsed = UUID(program_id)
            except ValueError:
                print("ERROR invalid_program_id", file=sys.stderr)
                return 4
            program = await session.scalar(select(AutomationProgram).where(AutomationProgram.id == parsed).limit(1))
            if program is None:
                print("ERROR program_not_found", file=sys.stderr)
                return 4
            revision = await session.scalar(select(AutomationProgramRevision).where(AutomationProgramRevision.program_id == program.id).order_by(AutomationProgramRevision.version.desc()).limit(1))
            user = await session.get(User, program.owner_user_id)
            if revision is None or user is None or not user.is_active:
                print("ERROR automation_owner_or_revision_unavailable", file=sys.stderr)
                return 4
            actor = {"id": str(user.id), "user_id": str(user.id), "role": user.role or "user", "actor_type": "system"}
            occurrence = trigger_key or derived_trigger_key(dict(revision.trigger_json or {}), datetime.now(timezone.utc))
            service = MediaOperationsAutomationService(session=session)
            result = await service.trigger_program(session, actor, program.id, trigger_key=occurrence, trigger_kind="windows_task_scheduler", execute=True)
            # Deliberately output only opaque IDs/state; no recipe, prompt, URL or credential material.
            print(f"AutomationRun ID={result['id']} state={result['state']}")
            if result["state"] == "failed": return 1
            if result["state"] == "uncertain": return 2
            return 0
        finally:
            await session.close()
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        await manager.close()


def main() -> int:
    args = parser().parse_args()
    if args.command == "run":
        return asyncio.run(run_program(args.program_id, args.trigger_key))
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
