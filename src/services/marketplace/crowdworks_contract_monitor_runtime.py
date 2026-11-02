"""CrowdWorks contract monitor work source and execution adapter."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import false, func, select

from ...memory.models.operations import EngagementOpportunity, ExternalAction
from ..agent_work_runtime import WorkCandidate
from .crowdworks_action import CrowdWorksPolicy
from .crowdworks_agent_scan import CrowdWorksAgentScanWorkSource


class _LeaseLost(Exception):
    """The coordinator no longer owns this work claim."""


class CrowdWorksContractMonitorWorkSource:
    """Discover monitor work for the explicitly selected native-scan binding."""

    source_type = "marketplace_crowdworks_contract_monitor"
    intent_key = "marketplace.crowdworks.contract_monitor"
    execution_adapter = "crowdworks_contract_monitor"

    def __init__(self, *, policy_service: Any, agent_scan_source: CrowdWorksAgentScanWorkSource):
        self.policies = policy_service
        self.binding_source = agent_scan_source

    @staticmethod
    def _stopped(connection: Any) -> bool:
        metadata = connection.metadata_json or {}
        state = metadata.get("crowdworks_scan_state") if isinstance(metadata, dict) else None
        failures = state.get("unit_failures") if isinstance(state, dict) else None
        monitor = failures.get("contract_monitor") if isinstance(failures, dict) else None
        return isinstance(monitor, dict) and monitor.get("stopped") is True

    async def _has_target(self, session: Any, connection_id: Any) -> bool:
        stopped = func.coalesce(
            EngagementOpportunity.metadata_json["crowdworks_unit_failures"]["contract_monitor"]["stopped"].as_boolean(),
            false(),
        )
        submitted = select(ExternalAction.id).where(
            ExternalAction.opportunity_id == EngagementOpportunity.id,
            ExternalAction.action_type == "engagement.submit_application",
            ExternalAction.status == "succeeded",
        ).exists()
        target = await session.scalar(
            select(EngagementOpportunity.id).where(
                EngagementOpportunity.connection_id == connection_id,
                EngagementOpportunity.status.in_(("open", "escrow_pending")),
                submitted,
                ~stopped,
            ).limit(1)
        )
        return target is not None

    async def discover(self, session: Any, *, now: datetime | None = None) -> list[WorkCandidate]:
        now = now or datetime.now(timezone.utc)
        utc_now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        binding = await self.binding_source.selected_policy_binding(session, now=utc_now)
        if binding is None or self._stopped(binding.connection):
            return []
        if not await self._has_target(session, binding.connection.id):
            return []
        try:
            config = CrowdWorksPolicy.model_validate(binding.revision.constraints_json)
            capabilities = tuple(self.policies.registry.require_executable(
                "engagement.submit_application"
            ).required_capabilities or ())
        except Exception:
            return []
        bucket = int(utc_now.timestamp()) // max(60, int(config.tick_interval_minutes) * 60)
        pin = f"{binding.revision.content_hash}:{binding.agent_revision.id}:{bucket}"
        connection_id = str(binding.connection.id)
        project_id = str(binding.connection.project_id) if binding.connection.project_id else None
        return [WorkCandidate(
            source_type=self.source_type, source_id=connection_id,
            source_revision="scan:" + hashlib.sha256(pin.encode("utf-8")).hexdigest(),
            intent_key=self.intent_key, domain="marketplace", project_id=project_id,
            assigned_agent_id=str(binding.policy.agent_id),
            agent_revision_id=str(binding.agent_revision.id), required_capabilities=capabilities,
            execution_adapter=self.execution_adapter, priority=10, max_attempts=3,
            concurrency_key=f"crowdworks:{connection_id}",
            metadata={"policy_id": str(binding.policy.id),
                      "policy_revision_id": str(binding.revision.id),
                      "policy_content_hash": str(binding.revision.content_hash),
                      "connection_id": connection_id,
                      "owner_user_id": str(binding.connection.owner_user_id),
                      "project_id": project_id},
        )]

    async def refresh(self, session: Any, claim: Any) -> bool:
        metadata = getattr(claim, "metadata", None)
        if not isinstance(metadata, dict) or any(
            getattr(claim, key, None) != value for key, value in (
                ("source_type", self.source_type), ("intent_key", self.intent_key),
                ("execution_adapter", self.execution_adapter),
            )
        ):
            return False
        if not all(metadata.get(key) for key in (
            "policy_id", "policy_revision_id", "policy_content_hash", "connection_id", "owner_user_id"
        )):
            return False
        if not getattr(claim, "assigned_agent_id", None) or not getattr(claim, "agent_revision_id", None):
            return False
        binding = await self.binding_source.selected_policy_binding(
            session, connection_id=metadata["connection_id"], owner_user_id=metadata["owner_user_id"],
            policy_revision_id=metadata["policy_revision_id"],
            agent_id=getattr(claim, "assigned_agent_id", None),
            agent_revision_id=getattr(claim, "agent_revision_id", None),
        )
        if binding is None:
            return False
        connection_id = str(binding.connection.id)
        project_id = str(binding.connection.project_id or "")
        try:
            capabilities = tuple(self.policies.registry.require_executable(
                "engagement.submit_application"
            ).required_capabilities or ())
        except Exception:
            return False
        return bool(
            str(binding.policy.id) == metadata["policy_id"]
            and str(binding.revision.content_hash) == metadata["policy_content_hash"]
            and str(getattr(claim, "source_id", "")) == connection_id
            and getattr(claim, "concurrency_key", None) == f"crowdworks:{connection_id}"
            and str(getattr(claim, "project_id", None) or "") == project_id
            and str(metadata.get("project_id") or "") == project_id
            and tuple(getattr(claim, "required_capabilities", ()) or ()) == capabilities
        )

class CrowdWorksContractMonitorExecutionAdapter:
    """Execute one bounded monitor unit for the current selected binding."""

    adapter_key = CrowdWorksContractMonitorWorkSource.execution_adapter

    def __init__(self, *, source: CrowdWorksContractMonitorWorkSource, config: Any = None):
        self.source = source
        self.config = config

    def required_capabilities(self, claim: Any):
        return tuple(getattr(claim, "required_capabilities", ()) or ())

    async def execute(self, claim, *, coordinator, session=None, run_id=None, run=None, **_kwargs):
        import asyncio
        import inspect
        import math
        from types import SimpleNamespace
        from uuid import UUID
        from ..agent_work_runtime import ExecutionOutcome
        from .crowdworks_browser_provider import resolve_browser_bridge
        from .crowdworks_contract_monitor import CrowdWorksContractMonitor
        from .crowdworks_source import CrowdWorksSource

        async def check_lease():
            renew = getattr(coordinator, "renew_lease", None)
            if not callable(renew):
                raise _LeaseLost()
            renewed = renew(claim)
            if inspect.isawaitable(renewed):
                renewed = await renewed
            if renewed is not True:
                raise _LeaseLost()

        def outcome(classification, code=None, *, domain_status=None, domain_ref=None, **result):
            return ExecutionOutcome(classification=classification, error_code=code, result=result,
                                    domain_status=domain_status, domain_ref=domain_ref)

        if session is None or not run_id:
            return outcome("blocked", "crowdworks_runtime_context_missing")
        # The same coordinator drain gate applies before any browser operation.
        try:
            from ..restart_drain import is_restart_draining
            draining = bool(is_restart_draining())
            predicate = getattr(coordinator, "preemptible_drain_requested", None)
            if callable(predicate):
                decision = predicate(claim)
                draining = draining or bool(await decision if inspect.isawaitable(decision) else decision)
            elif type(predicate) is bool:
                draining = draining or predicate
            else:
                event = getattr(coordinator, "_stop_event", None)
                draining = draining or bool(event.is_set() if event is not None else False)
        except Exception:
            draining = True
        if draining:
            return outcome("cancelled", "crowdworks_drain_requested", status="skipped")
        if not await self.source.refresh(session, claim):
            return outcome("blocked", "crowdworks_policy_binding_missing", status="skipped")
        meta = claim.metadata
        binding = await self.source.binding_source.selected_policy_binding(
            session, connection_id=meta["connection_id"], owner_user_id=meta["owner_user_id"],
            policy_revision_id=meta["policy_revision_id"], agent_id=claim.assigned_agent_id,
            agent_revision_id=claim.agent_revision_id,
        )
        if binding is None or str(binding.policy.id) != meta["policy_id"] or str(binding.revision.content_hash) != meta["policy_content_hash"]:
            return outcome("blocked", "crowdworks_policy_binding_missing", status="skipped")
        config = self.config if self.config is not None else getattr(coordinator, "config", None)
        value = None
        if isinstance(config, dict):
            value = config.get("crowdworks.work_runtime.unit_timeout_seconds")
            if value is None:
                value = config
                for part in ("crowdworks", "work_runtime", "unit_timeout_seconds"):
                    value = value.get(part) if isinstance(value, dict) else None
        elif callable(getattr(config, "get", None)):
            try:
                value = config.get("crowdworks.work_runtime.unit_timeout_seconds", None)
            except TypeError:
                value = config.get("crowdworks.work_runtime.unit_timeout_seconds")
        timeout = min(1800.0, max(1.0, float(value))) if type(value) in (int, float) and math.isfinite(value) else 45.0
        diagnostics = SimpleNamespace(last_contract_monitor_target_id=None, last_browser_backend=None,
                                      last_browser_reason_code=None, last_browser_skip_code=None)
        browser_source = CrowdWorksSource(config=config)
        try:
            async with asyncio.timeout(timeout):
                resolution = await resolve_browser_bridge(str(binding.connection.owner_user_id), config=config, device_id=None)
                if resolution.ready is not True or resolution.bridge is None or resolution.backend not in {"local", "bridge"}:
                    skip = resolution.skip_code if resolution.skip_code in {"skipped_browser_unavailable", "skipped_browser_login_required"} else "skipped_browser_unavailable"
                    await session.rollback()
                    await self._safe_notify(session, UUID(meta["connection_id"]), skip, 0, resolution.reason_code, coordinator=coordinator, claim=claim)
                    return outcome("succeeded", domain_status="precondition_skipped", domain_ref=meta["connection_id"], status="skipped", skip_code=skip, accepted_count=None)
                if not await self.source.refresh(session, claim):
                    return outcome("blocked", "crowdworks_policy_binding_missing", status="skipped")
                monitor = CrowdWorksContractMonitor(resolve_reusable_tab=browser_source._resolve_reusable_tab, diagnostics=diagnostics)
                accepted = await monitor.monitor_contracts(session, owner_user_id=str(binding.connection.owner_user_id),
                    connection_id=binding.connection.id, browser_bridge=resolution.bridge, browser_backend=resolution.backend,
                    check_lease=check_lease)
                if accepted is None:
                    await session.rollback()
                    await self._safe_notify(session, UUID(meta["connection_id"]), "skipped_browser_unavailable", 0, diagnostics.last_browser_reason_code, coordinator=coordinator, claim=claim)
                    return outcome("succeeded", domain_status="precondition_skipped", domain_ref=meta["connection_id"], status="skipped", skip_code="skipped_browser_unavailable", accepted_count=None)
                if diagnostics.last_contract_monitor_target_id is None:
                    return outcome("succeeded", domain_status="contract_monitored", domain_ref=meta["connection_id"], status="completed", accepted_count=0)
                return outcome("succeeded", domain_status="contract_monitored", domain_ref=meta["connection_id"], status="completed", accepted_count=accepted)
        except asyncio.CancelledError:
            await session.rollback()
            raise
        except _LeaseLost:
            await session.rollback()
            return outcome("cancelled", "crowdworks_lease_lost", status="skipped")
        except Exception as exc:
            # Known browser readiness exceptions are preconditions, not failed units.
            code = getattr(exc, "code", None)
            if not isinstance(code, str):
                code = str(exc)
            reason = getattr(exc, "reason_code", None)
            known = {"browser_login_required", "browser_backend_unavailable", "browser_owner_mismatch", "browser_unavailable", "broker_unavailable", "cdp_disconnected", "bridge_device_not_found", "bridge_device_offline", "bridge_disconnected", "crowdworks_browser_session_unavailable", "browser_restart_limit", "broker_executable_missing", "broker_mode_legacy", "broker_mode_unknown", "broker_start_failed", "broker_start_timeout", "gateway_root_missing", "bridge_select_a_pc_first", "bridge_capability_unavailable", "bridge_browser_command_unavailable", "bridge_jev_runtime_required", "edge_broker_browser_disconnected", "edge_broker_operation_outcome_unknown", "agent_browser_session_faulted", "agent_browser_operation_outcome_unknown"}
            if code in known or reason in known or code in {"skipped_browser_unavailable", "skipped_browser_login_required"}:
                detail = reason if reason in known else code
                skip = "skipped_browser_login_required" if detail == "browser_login_required" or code == "skipped_browser_login_required" else "skipped_browser_unavailable"
                await session.rollback()
                await self._safe_notify(session, UUID(meta["connection_id"]), skip, 0, detail, coordinator=coordinator, claim=claim)
                return outcome("succeeded", domain_status="precondition_skipped", domain_ref=meta["connection_id"], status="skipped", skip_code=skip, accepted_count=None)
            try:
                entry = await self._failure(session, UUID(meta["connection_id"]), diagnostics.last_contract_monitor_target_id, exc, coordinator=coordinator, claim=claim, check_lease=check_lease)
            except _LeaseLost:
                await session.rollback()
                return outcome("cancelled", "crowdworks_lease_lost", status="skipped")
            return outcome("succeeded", domain_status="unit_failed", domain_ref=meta["connection_id"], status="unit_failed", failure_code="crowdworks_unit_failure_limit_reached" if entry.get("stopped") else "unit_timeout" if isinstance(exc, TimeoutError) else "crowdworks_unit_failed", accepted_count=None, failure_count=entry.get("count", 0))

    async def _failure(self, session, connection_id, target_id, error, *, coordinator=None, claim=None, check_lease=None):
        from .crowdworks_diagnostics import _safe_scan_exception_diagnostics
        from uuid import UUID
        from ...memory.models.operations import ExternalConnection
        await session.rollback()
        connection = await session.get(ExternalConnection, connection_id, populate_existing=True)
        if connection is None:
            return {}
        target = None
        if target_id:
            try:
                target = await session.get(EngagementOpportunity, UUID(str(target_id)), populate_existing=True)
            except (ValueError, TypeError):
                pass
        if target is not None and target.connection_id != connection_id:
            target = None
        metadata = dict(connection.metadata_json or {})
        state = dict(metadata.get("crowdworks_scan_state") or {})
        target_metadata = dict(target.metadata_json or {}) if target is not None else None
        failures = dict((target_metadata.get("crowdworks_unit_failures") if target is not None else state.get("unit_failures")) or {})
        previous = failures.get("contract_monitor")
        previous = previous if isinstance(previous, dict) else {}
        def number(value, fallback=0):
            return value if type(value) is int and value >= 0 else fallback
        count = min(3, number(previous.get("count")) + 1)
        diagnostic = _safe_scan_exception_diagnostics(error)
        name = diagnostic["exception_class"]
        source = diagnostic["exception_source"]
        sqlstate = diagnostic["sqlstate"]
        now = datetime.now(timezone.utc).isoformat()
        entry = {"count": count, "stopped": count >= 3, "last_failed_at": now, "exception_class": name,
                 "exception_source": source, "sqlstate": sqlstate,
                 "total_failure_count": min(1000000, number(previous.get("total_failure_count"), number(previous.get("count"))) + 1),
                 "resume_count": min(3, number(previous.get("resume_count")))}
        if isinstance(previous.get("last_resumed_at"), str):
            entry["last_resumed_at"] = previous["last_resumed_at"]
        if isinstance(error, TimeoutError):
            entry.update(failure_code="unit_timeout", subcode="unit_timeout")
        failures["contract_monitor"] = entry
        if target is not None:
            target_metadata["crowdworks_unit_failures"] = failures
            target.metadata_json = target_metadata
        else:
            state["unit_failures"] = failures
        code = "crowdworks_unit_failure_limit_reached" if entry["stopped"] else "unit_timeout" if isinstance(error, TimeoutError) else "crowdworks_unit_failed"
        last = {"kind": "contract_monitor", "code": code, "at": now, "count": count, "stopped": entry["stopped"], "exception_class": name, "exception_source": source, "sqlstate": sqlstate}
        if isinstance(error, TimeoutError):
            last["subcode"] = "unit_timeout"
        state["last_unit_failure"] = last
        metadata["crowdworks_scan_state"] = state
        connection.metadata_json = metadata
        if check_lease is not None:
            await check_lease()
        await session.commit()
        if entry["stopped"]:
            await self._safe_notify(session, connection_id, "crowdworks_unit_failure_limit_reached", count, None, coordinator=coordinator, claim=claim)
        return entry

    async def _safe_notify(self, session, connection_id, reason, count, detail, *, coordinator=None, claim=None):
        try:
            await self._notify(session, connection_id, reason, count, detail, coordinator=coordinator, claim=claim)
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass

    async def _notify(self, session, connection_id, reason, count, detail, *, coordinator=None, claim=None):
        import inspect
        from ...memory.models.operations import ExternalConnection
        from .crowdworks_scan_notifications import CrowdWorksScanNotifications, ScanNotificationResult

        renew = getattr(coordinator, "renew_lease", None)
        if not callable(renew):
            return
        renewed = renew(claim)
        if inspect.isawaitable(renewed):
            renewed = await renewed
        if renewed is not True:
            return
        connection = await session.get(ExternalConnection, connection_id, populate_existing=True)
        if connection is None:
            return
        service = CrowdWorksScanNotifications()
        attempted_at = datetime.now(timezone.utc)
        if not await service._begin_scan_notification_attempt(
            session, connection=connection, reason_code=reason, now=attempted_at,
        ):
            return
        # Reservation commits; do not publish after the claim has been reclaimed.
        renewed = renew(claim)
        if inspect.isawaitable(renewed):
            renewed = await renewed
        if renewed is not True:
            return
        sent = await service._persist_scan_notification(
            session, connection=connection, reason_code=reason, count=count,
            unit_kind="contract_monitor", detail_code=detail, now=attempted_at,
        )
        await service._record_scan_notification_outcome(
            session, connection=connection, result=ScanNotificationResult(),
            reason_code=reason, sent=sent, now=datetime.now(timezone.utc),
            update_last_scan=False,
        )
