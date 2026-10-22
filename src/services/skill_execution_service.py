"""Execute an existing canonical Skill and persist its business outcome."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, inspect, or_, select, update
from sqlalchemy.exc import IntegrityError

from ..memory.models.skill_execution import SkillExecution
from ..skills.routine import RoutineDefinition, digest, effective_values, format_value, routine_for
from ..skills.routine_facts import fact_value, normalized
from .skill_browser_execution import SkillBrowserAgent, SkillBrowserBridge, objective_evidence
from .skill_learning_service import SkillLearningService
from .skill_target_io import SkillTargetLockTimeout, async_skill_target_lock
from .turn_context import get_turn_context


def skill_capability_enabled(config: Any, *, browser: bool = False) -> bool:
    from ..llm.tool_packs import _strict_config_bool

    try:
        if not _strict_config_bool(config.get("use_tools", True), default=True):
            return False
        from ..llm.tool_policy import _policy_config_get
        if not _strict_config_bool(_policy_config_get(config, "runtime.target_enable_tools", True), default=True):
            return False
        if not _strict_config_bool(config.get("skills", {}).get("enabled", True), default=True):
            return False
        if browser:
            from .browser_agent_models import BrowserAgentSettings
            return BrowserAgentSettings.from_config(config).enabled
        return True
    except (AttributeError, TypeError, ValueError):
        return False


class SkillExecutionStore:
    # Longer than the bounded whole execution (915s); protects separate hosts,
    # where the existing target file lock alone cannot prove an owner is gone.
    LEASE_SECONDS = 960

    @asynccontextmanager
    async def session(self):
        from ..memory.database import get_database_manager

        session = await get_database_manager().get_session()
        try:
            yield session
        finally:
            await session.close()

    async def get(self, user_id, request_id):
        async with self.session() as session:
            return await session.scalar(select(SkillExecution).where(
                SkillExecution.user_id == user_id, SkillExecution.request_id == request_id))

    async def blocking(self, active_key, commit_key):
        async with self.session() as session:
            return await session.scalar(select(SkillExecution).where(or_(
                SkillExecution.active_key == active_key,
                SkillExecution.commit_key == commit_key if commit_key else False)))

    async def create(self, record):
        async with self.session() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            session.expunge(record)

    async def claim(self, record):
        owner = uuid4().hex
        async with self.session() as session:
            now = await self._now(session)
            claimed = await session.execute(update(SkillExecution).where(
                SkillExecution.id == record.id,
                or_(SkillExecution.lease_until.is_(None), SkillExecution.lease_until < now),
                SkillExecution.status.not_in(("succeeded", "failed")),
            ).values(lease_owner=owner, lease_until=now + timedelta(seconds=self.LEASE_SECONDS)))
            await session.commit()
            if claimed.rowcount != 1:
                return None
        fresh = await self.get(record.user_id, record.request_id)
        fresh._claim_owner = owner
        return fresh

    async def assert_owner(self, record):
        async with self.session() as session:
            now = await self._now(session)
            owner = await session.scalar(select(SkillExecution.id).where(
                SkillExecution.id == record.id,
                SkillExecution.lease_owner == getattr(record, "_claim_owner", None),
                SkillExecution.lease_until > now))
            if not owner or not getattr(record, "_claim_owner", None):
                raise ValueError("skill_execution_lease_lost")

    async def save(self, record, *, release=False):
        owner = getattr(record, "_claim_owner", None)
        if not owner:
            raise ValueError("skill_execution_lease_required")
        async with self.session() as session:
            now = await self._now(session)
            record.updated_at = now
            record.lease_owner = None if release else owner
            record.lease_until = None if release else now + timedelta(seconds=self.LEASE_SECONDS)
            # Column attributes carry ciphertext; do not update via public JSON properties.
            values = {attr.columns[0].name: getattr(record, attr.key)
                      for attr in inspect(SkillExecution).column_attrs if attr.key not in {"id", "created_at"}}
            saved = await session.execute(update(SkillExecution.__table__).where(
                SkillExecution.id == record.id, SkillExecution.lease_owner == owner,
                SkillExecution.lease_until > now).values(**values))
            if saved.rowcount != 1:
                await session.rollback()
                raise ValueError("skill_execution_lease_lost")
            await session.commit()

    @staticmethod
    async def _now(session):
        # One database clock is authoritative across worker hosts. Normalize
        # PostgreSQL timestamptz and SQLite's naive UTC to the existing column type.
        now = await session.scalar(select(func.current_timestamp()))
        return now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now


class SkillExecutionService:
    def __init__(self, config: Any, *, client: Any = None, store: Any = None,
                 learning: Any = None, bridge_resolver: Any = None,
                 browser_factory: Any = SkillBrowserAgent):
        self.config = config
        self.client = client
        self.store = store or SkillExecutionStore()
        self.learning = learning or SkillLearningService()
        self.bridge_resolver = bridge_resolver
        self.browser_factory = browser_factory

    def gate(self):
        if not skill_capability_enabled(self.current_config(), browser=True):
            raise ValueError("skill_execution_disabled")

    def current_config(self):
        current = getattr(self.client, "config", None)
        return self.config if current is None else current

    async def resolve(self, name):
        from ..skills.loader import load_all_skills
        from ..skills.registry import get_skill_registry

        # Global learned procedures use the existing YAML authority. Project
        # SKILL.md does not support structured parameters; do not invent a second format.
        load_all_skills(self.learning._global_skills_dir)
        registry = get_skill_registry()
        skill = registry.get_by_alias(name) or registry.get(name)
        if skill is None and name in {"いつものあれ", "いつもの", "usual"}:
            routines = [s for s in registry.get_all() if routine_for(s.to_dict())]
            if len(routines) == 1:
                skill = routines[0]
        if skill is None:
            return None
        snapshot = self.learning.prepare_usage_snapshot(skill, project_id=None)
        # Do not execute an alias removed in the canonical snapshot.
        if normalized(name) not in {"いつものあれ", "いつもの", "usual"} and normalized(name).casefold() not in {
            normalized(snapshot.name).casefold(), *(normalized(a).casefold() for a in snapshot.payload.get("aliases", []))
        }:
            return None
        return snapshot

    async def run(self, *, skill_name: str, input_text: str = "", overrides: dict[str, Any] | None = None,
                  request_id: str = "", resume: bool = False, cancel: bool = False, device_id: str = ""):
        self.gate()
        turn = get_turn_context()
        if not turn.user_id or not turn.session_id or not (turn.message_id or turn.client_message_id):
            raise ValueError("skill_authenticated_turn_required")
        if (resume or cancel) and not request_id:
            raise ValueError("skill_resume_request_id_required")
        if resume and cancel:
            raise ValueError("skill_resume_cancel_conflict")
        if len(request_id) > 160:
            raise ValueError("skill_request_id_invalid")
        existing = await self.store.get(turn.user_id, request_id) if request_id else None
        if existing is not None:
            if existing.session_id != turn.session_id:
                raise ValueError("skill_resume_session_mismatch")
            frozen = existing.context
            if ((overrides and (resume or overrides != frozen.get("overrides", {}))) or
                    (input_text and (resume or input_text != frozen.get("input_text", ""))) or
                    (device_id and device_id != frozen.get("device_id"))):
                raise ValueError("skill_resume_inputs_frozen")
            path = existing.skill_path
        else:
            if resume or cancel:
                raise ValueError("skill_execution_not_found")
            snapshot = await self.resolve(skill_name)
            if snapshot is None:
                return {"success": False, "status": "needs_user", "reason": "skill_name_required",
                        "next_action": "manage_skill(action='list')で候補を選んでください"}
            spec = routine_for(snapshot.payload)
            if spec is None:
                raise ValueError("skill_has_no_browser_routine")
            values = effective_values(snapshot.payload, overrides or {}, input_text)
            rendered_spec = spec.render(values)
            goal = format_value(snapshot.payload["prompt_template"], values)
            path = snapshot.canonical_path
            request_id = request_id or digest(["skill-request", turn.user_id, snapshot.name,
                                               turn.client_message_id or turn.message_id])
        active_key = digest([turn.user_id, path])
        record = None
        try:
            # Reuse the existing cross-process Skill lock. A second invoke never waits
            # behind a live interaction and then silently dispatches another booking.
            async with async_skill_target_lock(Path("skill-execution") / active_key, timeout=0):
                record = await self.store.get(turn.user_id, request_id)
                created_now = record is None
                if record is None:
                    commit_key = (digest([turn.user_id, fact_value("target", rendered_spec.confirmation["target"]),
                                          fact_value("datetime", rendered_spec.confirmation["datetime"])])
                                  if rendered_spec.intent == "commit" else None)
                    blocker = await self.store.blocking(active_key, commit_key)
                    if blocker:
                        return {**blocker.to_dict(), "duplicate_prevented": True}
                    record = SkillExecution(
                        id=digest([turn.user_id, request_id]), user_id=turn.user_id,
                        session_id=turn.session_id, request_id=request_id, skill_name=snapshot.name,
                        skill_path=path, skill_hash=snapshot.sha256,
                        input_digest=digest([goal, values, rendered_spec.model_dump()]),
                        active_key=active_key, commit_key=commit_key, status="running", reason="",
                        commit_dispatched=False, steps_completed=0,
                    )
                    record.context = {"spec": rendered_spec.model_dump(), "goal": goal,
                                      "values": values, "overrides": overrides or {}, "input_text": input_text,
                                      "device_id": device_id, "tab_id": None}
                    record.evidence = {}
                    try:
                        await self.store.create(record)
                    except IntegrityError:
                        winner = await self.store.get(turn.user_id, request_id) or await self.store.blocking(active_key, commit_key)
                        if winner is None:
                            raise
                        return {**winner.to_dict(), "duplicate_prevented": True}
                else:
                    if existing is None and record.input_digest != digest([goal, values, rendered_spec.model_dump()]):
                        raise ValueError("skill_request_id_input_conflict")
                    if record.status in {"succeeded", "failed"} or record.context.get("cancelled"):
                        return record.to_dict()
                    if record.status != "running" and not (resume or cancel):
                        return record.to_dict()
                prior_status = record.status
                claimed = await self.store.claim(record)
                if claimed is None:
                    winner = await self.store.get(turn.user_id, request_id)
                    if winner.status in {"succeeded", "failed"}:
                        return winner.to_dict()
                    return {**winner.to_dict(), "status": "running", "resume_required": False,
                            "reason": "skill_execution_busy"}
                record = claimed
                if not created_now:
                    if prior_status == "running":
                        record.status = "unknown" if record.commit_dispatched else "interrupted"
                        record.reason = "skill_previous_execution_interrupted"
                    if not (resume or cancel):
                        await self._finish(record)
                        return record.to_dict()
                if cancel:
                    if record.commit_dispatched:
                        record.status, record.reason = "unknown", "skill_cancel_after_dispatch_requires_reconcile"
                    else:
                        if record.context.get("browser_run_id"):
                            try:
                                await self._cancel_owned_run(record)
                            except Exception:
                                record.status, record.reason = "needs_user", "skill_browser_cancel_unverified"
                                await self._finish(record)
                                return record.to_dict()
                        record.context = {**record.context, "cancelled": True}
                        record.status, record.reason = "interrupted", "skill_execution_cancelled"
                    await self._finish(record)
                    return record.to_dict()
                async with asyncio.timeout(915):
                    return await self._execute(record)
        except SkillTargetLockTimeout:
            record = await self.store.get(turn.user_id, request_id)
            return record.to_dict() if record else {"success": False, "status": "running",
                                                    "request_id": request_id, "reason": "skill_execution_busy"}
        except Exception:
            if record is not None and record.commit_dispatched:
                # DB/lease/transport faults must never turn a possibly sent
                # booking into a known failure or lose its resumable identity.
                return {**record.to_dict(), "success": False, "status": "unknown",
                        "completion_verified": False, "resume_required": True,
                        "reason": "skill_execution_persistence_or_transport_uncertain"}
            if record is not None and record.status == "interrupted":
                return record.to_dict()
            raise

    async def _cancel_owned_run(self, record):
        """Revoke the retained exact run through the existing Bridge controller."""
        if record.context.get("browser_backend") != "owned_profile":
            raise ValueError("skill_browser_cancel_unverified")
        resolver = self.bridge_resolver
        if resolver is None:
            from .pc_bridge_service import resolve_control_bridge
            bridge = await resolve_control_bridge(record.context.get("device_id") or None,
                                                  dedicated=True, cleanup_only=True)
        else:
            bridge = await resolver(record.context.get("device_id") or None, dedicated=True)
        if (getattr(bridge, "browser_backend", "") != "owned_profile" or
                getattr(bridge, "device_id", None) != record.context.get("device_id")):
            raise ValueError("skill_browser_cancel_target_changed")
        self._check_owned_binding(record, bridge, retained=True)
        if not bridge.operation_lock.acquire(blocking=False):
            raise ValueError("skill_browser_busy")
        try:
            await self.store.assert_owner(record)
            run_id = record.context["browser_run_id"]
            ended = await bridge.request("end_run", run_id=run_id)
            if ended.get("paused") is True:
                cancelled = await bridge.request("cancel_login", run_id=run_id, tab_id=record.context.get("tab_id"))
                if cancelled.get("cancelled") is not True:
                    raise ValueError("skill_browser_cancel_unverified")
                ended = await bridge.request("end_run", run_id=run_id)
            if ended.get("ended") is not True or ended.get("refs_invalidated") is not True or ended.get("paused") is True:
                raise ValueError("skill_browser_cancel_unverified")
            context = dict(record.context)
            context.pop("browser_run_id", None)
            context.pop("browser_run_paused", None)
            record.context = context
        finally:
            bridge.operation_lock.release()

    @staticmethod
    def _check_owned_binding(record, bridge, *, retained):
        if getattr(bridge, "owner", None) != record.user_id:
            raise ValueError("skill_browser_owner_changed")
        epoch = getattr(bridge, "connection_epoch", None)
        bound_epoch = record.context.get("browser_connection_epoch")
        if not epoch or (retained and bound_epoch != epoch):
            raise ValueError("skill_browser_connection_changed")
        return epoch

    async def _execute(self, record):
        spec = RoutineDefinition.model_validate(record.context["spec"])
        bridge = None
        adapter = None
        try:
            resolver = self.bridge_resolver
            if resolver is None:
                from .pc_bridge_service import resolve_control_bridge
                resolver = resolve_control_bridge
            from .browser_agent_models import BrowserAgentSettings
            settings = BrowserAgentSettings.from_config(self.current_config())
            retained = bool(record.context.get("browser_run_id"))
            dedicated = (
                record.context.get("browser_backend") == "owned_profile"
                if record.commit_dispatched or retained else settings.dedicated_enabled
            )
            if dedicated and not settings.dedicated_enabled:
                record.status = "unknown" if record.commit_dispatched else "needs_user"
                record.reason = "skill_reconcile_browser_disabled" if record.commit_dispatched else "skill_resume_browser_disabled"
                await self._finish(record)
                return record.to_dict()
            bridge = await resolver(record.context.get("device_id") or None, dedicated=dedicated)
            context = dict(record.context)
            backend = getattr(bridge, "browser_backend", "extension")
            if (record.commit_dispatched or retained) and context.get("browser_backend", backend) != backend:
                raise ValueError("skill_reconcile_browser_changed")
            if backend == "owned_profile":
                context["browser_connection_epoch"] = self._check_owned_binding(record, bridge,
                    retained=retained or record.commit_dispatched)
            context["browser_backend"] = backend
            context["device_id"] = getattr(bridge, "device_id", context.get("device_id", ""))
            record.context = context
            record.status = "running"
            await self.store.save(record)
            from .agent_run_service import get_current_agent_run_id
            reconciling = record.commit_dispatched

            def gate():
                self.gate()
                if backend == "owned_profile" and not BrowserAgentSettings.from_config(self.current_config()).dedicated_enabled:
                    raise ValueError("skill_execution_disabled")
                if not reconciling:
                    snapshot = self.learning.snapshot_target(target_scope="global", target_name=record.skill_name)
                    if snapshot.sha256 != record.skill_hash:
                        raise ValueError("skill_definition_changed")

            adapter = SkillBrowserBridge(bridge, spec=spec, record=record, save=self.store.save,
                                         gate=gate, agent_run_id=get_current_agent_run_id() or "",
                                         reconcile=record.commit_dispatched,
                                         assert_owner=lambda: self.store.assert_owner(record))
            if record.commit_dispatched:
                # No planner, navigation, form filling, or submit on uncertain resume.
                # Rebind only the exact previously selected tab; no URL search/new tab.
                if context.get("tab_id") is None:
                    raise ValueError("skill_reconcile_target_required")
                service = self.browser_factory(self.config, client=self.client, bridge=adapter)
                if not service.operation_lock.acquire(blocking=False):
                    raise ValueError("skill_browser_busy")
                result = {"status": "steps_completed"}
                try:
                    target = await service._resolve_target(tab_id=context["tab_id"], url="")
                    service.tab_id = target["tab_id"]
                    await service._observe()
                finally:
                    try:
                        await service._release_helper_run(result)
                    finally:
                        service.operation_lock.release()
            else:
                service = self.browser_factory(self.config, client=self.client, bridge=adapter)
                if backend == "owned_profile" and context.get("browser_run_id"):
                    # A paused owned driver retains its run. Reuse the existing
                    # encrypted checkpoint and login controller, never a new run.
                    result = await service.resume(context["browser_run_id"])
                else:
                    result = await service.run_goal(goal=record.context["goal"], start_url=spec.start_url,
                                                    tab_id=context.get("tab_id"),
                                                    completion_text=spec.completion_text,
                                                    completion_url=spec.completion_url)
            evidence = (objective_evidence(spec, adapter.latest, record.evidence)
                        if not record.commit_dispatched or adapter.observation_generation > adapter.commit_generation else None)
            if (not record.commit_dispatched and (result or {}).get("reason") == "browser_login_cancelled" and
                    not record.context.get("browser_run_id")):
                record.context = {**record.context, "cancelled": True}
                record.status, record.reason = "interrupted", "skill_execution_cancelled"
            elif evidence and (spec.intent == "check" or record.commit_dispatched):
                record.status, record.reason = "succeeded", ""
                record.evidence = {**(record.evidence or {}), **evidence}
                if result and result.get("reason") == "agent_browser_run_release_unverified":
                    record.evidence = {**record.evidence, "browser_cleanup_verified": False}
            elif record.commit_dispatched:
                record.status, record.reason = "unknown", adapter.stop_reason or "skill_completion_evidence_missing"
            elif adapter.stop_reason:
                record.status, record.reason = "needs_user", adapter.stop_reason
                if adapter.stop_reason == "routine_approval_rejected":
                    record.status = "interrupted"
                elif adapter.stop_reason == "routine_commit_not_executed":
                    record.status = "failed"
                elif adapter.stop_reason in {"routine_bridge_interrupted", "routine_bridge_unknown"}:
                    record.status = "interrupted"
            elif backend == "owned_profile" and record.context.get("browser_run_id"):
                record.status, record.reason = "needs_user", "skill_browser_resume_unverified"
            else:
                record.status, record.reason = "failed", "skill_browser_failed"
        except asyncio.CancelledError:
            record.status = "unknown" if record.commit_dispatched else "interrupted"
            record.reason = "skill_execution_interrupted"
            await self._finish(record)
            raise
        except Exception:
            record.status = "unknown" if record.commit_dispatched else "needs_user"
            record.reason = adapter.stop_reason if adapter and adapter.stop_reason else "skill_bridge_or_definition_unavailable"
        await self._finish(record)
        result = record.to_dict()
        if record.reason in {"routine_login_handoff", "routine_bridge_needs_user"}:
            result["handoff"] = {"device_id": record.context.get("device_id"), "tab_id": record.context.get("tab_id"),
                                 **record.context.get("bridge_handoff", {}),
                                 "instruction": "ブラウザ画面でログインしてください。password/OTPはチャットへ送らず、完了後にこのrequest_idで再開してください。"}
        if record.status == "unknown":
            result["next_action"] = "同じrequest_idで再開すると、選択tabの完了画面・履歴を読み取って照合します。確定操作は再送しません。"
        return result

    async def _finish(self, record):
        cancelled = record.context.get("cancelled") and not record.commit_dispatched
        if record.status in {"succeeded", "failed"} or cancelled:
            record.active_key = None
        if (record.status == "failed" and not record.commit_dispatched) or cancelled:
            record.commit_key = None
        await self.store.save(record, release=True)
