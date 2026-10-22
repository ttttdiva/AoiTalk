"""Lifespan worker for the versioned 87 CLI, with durable DB/project reconciliation."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import uuid
from pathlib import Path

from sqlalchemy import select, text

from ..memory.database import get_db_session
from ..services.storage_roots import assert_no_links
from . import production, service
from .models import CharacterRig, CharacterRigBuild
from .package_import import FORMAT, MAX_UPLOAD, PackageError, immutable_copy, validate_package

logger = logging.getLogger(__name__)


def project_root(tool_path):
    # Path resolution is read-only; the authorized87 CLI creates the project.
    path = Path(tool_path) / "projects"
    assert_no_links(path, allow_missing=True)
    return path


def project_path(project_id, tool_path):
    value = str(uuid.UUID(project_id))
    path = project_root(tool_path) / value
    assert_no_links(path, allow_missing=True)
    return path


async def update(build_id, stage=None, **values):
    async with await get_db_session() as db, db.begin():
        build = (await db.execute(select(CharacterRigBuild).where(CharacterRigBuild.id == build_id).with_for_update())).scalar_one()
        if build.status == "archived":
            return False
        if (build.config or {}).get("cancel_requested") and stage not in {None, "cancelled"}:
            return False
        if stage is not None:
            build.status = stage
        for key, value in values.items():
            setattr(build, key, value)
        return True


async def cancellation(build_id):
    async with await get_db_session() as db:
        build = await db.get(CharacterRigBuild, build_id)
        return build is None or bool((build.config or {}).get("cancel_requested"))


def report(result):
    # Do not expose host executable/recipe paths, environment, logs or agent prompts.
    output = {"project_status": str(result.get("status", ""))[:60], "stage": str(result.get("stage", ""))[:100],
              "artistic_review_required": True}
    for key in ("problems", "deficits"):
        entries = result.get(key, [])
        output[key] = []
        if isinstance(entries, list):
            for item in entries[:64]:
                if isinstance(item, str):
                    output[key].append(item[:500] if not (":/" in item or ":\\" in item or item.startswith("/")) else "製作プロジェクトの入力・環境を確認してください")
                elif isinstance(item, dict):
                    output[key].append({k: v for k, v in item.items() if k in {"role", "kind", "message", "crop"} and isinstance(v, (str, int, float, bool, list))})
    return output


async def run_project_agent(workspace, build_id, owner):
    """Use existing runner/settings and an enforced file scope; no host fallback.

    No secret grants/network capability. The runner must fail closed if its
    configured sandbox backend is unavailable. Never invokes a separate agent CLI.
    """
    from dataclasses import replace
    from ..agent_harness.config import AgentHarnessSettings
    from ..agent_harness.models import WorkItem
    from ..agent_harness.runner import build_runner
    from ..security.agent_run_scope import run_scope_context
    from ..security.harness_execution_scope import ResourceLimits, harness_execution_scope_context
    from ..services.harness_execution_scope_service import build_autonomous_agent_harness_scope

    config = await production.config_snapshot()
    settings = AgentHarnessSettings.from_config(config, root_dir=production.REPO_ROOT)
    if not settings.enabled:
        raise production.ProductionError("既存のAgent Harnessが無効です。制作素材の準備には既存エージェント設定を有効にしてください", 409)
    if not settings.execution_enabled:
        raise production.ProductionError("製作agentの実行scopeが無効です", 503)
    scope = build_autonomous_agent_harness_scope(
        workspace_path=workspace, harness_workspace_root=workspace.parent,
        resource_limits=ResourceLimits.from_values(settings.execution_resource_limits),
        run_id=str(build_id), audit_id="rig-production:" + str(build_id))
    mutable = tuple(workspace / name for name in ("recipe", "material", ".aoitalk-harness-scratch"))
    scope = replace(scope, write_roots=mutable, delete_roots=mutable)
    instruction_file = workspace / "agent-instructions.md"
    assert_no_links(instruction_file, allow_missing=False)
    if instruction_file.stat().st_size > 65536:
        raise production.ProductionError("製作手順が大きすぎます", 409)
    prompt = ("Work only inside the mounted project. Author recipe/material data using the existing project instructions. "
              "Do not run integration commands outside the scope, access global87 source, change immutable input/source or project identity, "
              "send images/data to external services, or claim artistic QA. The host runs trusted87 CLI stages. "
              "Report missing artwork as needs_input.\n" + instruction_file.read_text(encoding="utf-8"))
    item = WorkItem(id=str(build_id), identifier="rig:" + str(build_id), title="See-through project authoring",
                    project_id=workspace.name, source_type="character_rig", metadata={"owner": owner})
    with harness_execution_scope_context(scope), run_scope_context(scope.to_agent_run_scope()):
        result = await build_runner(settings).run(work_item=item, workspace=workspace, prompt=prompt, attempt=1)
    if not result.success:
        raise production.ProductionError("製作agentが完了できません。scope/入力を確認してください", 503)
    return result


async def run_build(build_id):
    async with await get_db_session() as db:
        build = await db.get(CharacterRigBuild, build_id)
        rig = await db.get(CharacterRig, build.rig_id) if build else None
        if not build or not rig or service.model_format(build) != FORMAT:
            return
        config = dict(build.config or {})
        source_path, rig_id, owner, character_id = build.source_path, rig.id, rig.owner_id, str(rig.character_id)
    cancel = asyncio.Event()
    path = Path(config["production_path"])
    pid = str(uuid.UUID(config["project_id"]))
    args = ["--project", pid, "--projects-root", str(project_root(path))]
    watch_task = None
    try:
        saved = await production.settings()
        if saved.get("enabled") is not True:
            await update(build_id, "needs_input", error="製作機能はOFFです")
            return
        production.require_personal()
        if production.effective_path(saved) != path:
            raise production.ProductionError("製作環境が変更されました。元の設定を復元してください", 409)
        source = service.root_path(source_path)
        assert_no_links(source, allow_missing=False)
        if hashlib.sha256(source.read_bytes()).hexdigest() != config["source_sha256"]:
            raise production.ProductionError("immutable sourceのhashが一致しません", 409)
        await update(build_id, "creating")
        # Idempotent create verifies owner, character and source on every restart.
        state = await production.cli(path, "project-create", ["--source", str(source), "--project-id", pid,
                                                            "--owner", owner, "--character", character_id,
                                                            "--projects-root", str(project_root(path))])
        if state.get("project_id") != pid or state.get("owner") != owner or state.get("character") != character_id:
            raise production.ProductionError("製作projectの所有者が一致しません", 409)
        if state.get("source_sha256") != config["source_sha256"]:
            raise production.ProductionError("製作projectのsource hashが一致しません", 409)

        async def watch():
            while True:
                if await cancellation(build_id):
                    cancel.set()
                    with contextlib.suppress(Exception):
                        await production.cli(path, "project-cancel", args, timeout=30)
                    return
                await asyncio.sleep(1)
        watch_task = asyncio.create_task(watch())
        if await cancellation(build_id):
            await production.cli(path, "project-cancel", args)
            await update(build_id, "cancelled")
            return
        await update(build_id, "running")
        # Always recheck 87 status/source. Its process locks own GPU/stage caches.
        state = await production.cli(path, "project-status", args)
        while state.get("status") == "running" and not cancel.is_set():
            await asyncio.sleep(3)
            state = await production.cli(path, "project-status", args)
        if state.get("status") != "completed":
            command = "project-resume" if config.get("resume_requested") else "project-run"
            state = await production.cli(path, command, args, timeout=7200, cancel=cancel)
        if cancel.is_set():
            await update(build_id, "cancelled")
            return
        if state.get("status") == "needs_input" and state.get("stage") != "environment":
            agent_task = asyncio.create_task(run_project_agent(project_path(pid, path), build_id, owner))
            cancel_task = asyncio.create_task(cancel.wait())
            try:
                done, _pending = await asyncio.wait({agent_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
                if cancel_task in done:
                    agent_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError): await agent_task
                    await update(build_id, "cancelled")
                    return
                await agent_task
            finally:
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError): await cancel_task
            state = await production.cli(path, "project-run", args, timeout=7200, cancel=cancel)
        if state.get("status") in {"needs_input", "cancelled", "failed"}:
            await update(build_id, state["status"], report=report(state), error=None)
            return
        await update(build_id, "exporting", report=report(state))
        # Export reconciles 87 project/cache before returning immutable package.
        state = await production.cli(path, "project-export", args, timeout=600, cancel=cancel)
        if state.get("status") != "completed" or not state.get("output_package"):
            await update(build_id, "needs_input", report=report(state), error="製作packageがまだ完成していません")
            return
        candidate = Path(state["output_package"])
        assert_no_links(candidate, allow_missing=False)
        if not candidate.resolve().is_relative_to(project_path(pid, path).resolve() / "history"):
            raise production.ProductionError("製作packageがプロジェクト外を参照しています", 409)
        if candidate.stat().st_size > MAX_UPLOAD:
            raise production.ProductionError("製作packageが大きすぎます", 413)
        await update(build_id, "validating")
        package = await asyncio.to_thread(validate_package, candidate.read_bytes())
        if package.manifest["project_id"] != pid or package.manifest["source_sha256"] != config["source_sha256"]:
            raise production.ProductionError("製作packageのproject/source identityが一致しません", 409)
        relative = f"{rig_id}/packages/{package.digest}"
        # Hold rig lock while immutable copy and final candidate publication happen.
        async with await get_db_session() as db, db.begin():
            rig = (await db.execute(select(CharacterRig).where(CharacterRig.id == rig_id).with_for_update())).scalar_one()
            build = (await db.execute(select(CharacterRigBuild).where(CharacterRigBuild.id == build_id).with_for_update())).scalar_one()
            if build.status == "archived":
                return
            if (build.config or {}).get("cancel_requested"):
                build.status = "cancelled"
                return
            await asyncio.to_thread(immutable_copy, package, service.root_path(relative))
            build.asset_path, build.status, build.error = relative, "ready", None
            build.config = service.package_config(package, build.config)
            build.report = {**report(state), "validated": True}
    except asyncio.CancelledError:
        # Persisted phase remains resumable; process() kills its own child tree.
        raise
    except Exception as exc:
        if await cancellation(build_id):
            await update(build_id, "cancelled")
        elif getattr(exc, "code", None) == "busy":
            await update(build_id, "running", error=None)
        else:
            logger.exception("See-through production build %s failed", build_id)
            message = str(exc) if isinstance(exc, (production.ProductionError, PackageError)) else "製作処理に失敗しました"
            await update(build_id, "failed", error=message[:1000])
    finally:
        if watch_task:
            watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch_task


class ProductionWorker:
    def __init__(self):
        self.task = None

    async def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.loop(), name="seethrough-production")

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def loop(self):
        while True:
            try:
                async with await get_db_session() as lock_db, lock_db.begin():
                    acquired = True
                    if lock_db.bind.dialect.name == "postgresql":
                        acquired = (await lock_db.execute(text("SELECT pg_try_advisory_xact_lock(4102026, 931)"))).scalar()
                    if acquired:
                        async with await get_db_session() as db:
                            rows = (await db.execute(select(CharacterRigBuild).where(
                                CharacterRigBuild.status.not_in(service.FINAL)
                            ).order_by(CharacterRigBuild.created_at))).scalars().all()
                            build = next((b for b in rows if service.model_format(b) == FORMAT and (b.config or {}).get("production_path")), None)
                            bid = build.id if build else None
                        if bid:
                            await run_build(bid)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("See-through worker poll failed")
            await asyncio.sleep(3)
