"""In-process daily Free Team AgentTask admission.

The shared heartbeat/coordinator owns the tick.  This service only decides
whether today's fixed, read-only task may be admitted and then reuses the
already-bound ``AgentTaskInvocationService``.  It never calls a provider.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID
from zoneinfo import ZoneInfo

import yaml

from ..features import Features
from ..memory.models import Agent, AgentRevision, User
from ..services.agent_task_invocation import (
    _approved_workspace_roots,
    _path_within,
    _task_prompt,
)
from ..services.command_workspace_confinement import inspect_existing_workspace
from ..services.free_team_service import (
    FreeTeamApiTaskPreflight,
    main_route_intent,
    preflight_free_team_api_task,
)

logger = logging.getLogger(__name__)

_TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
_DAILY_TIMEZONE = "Asia/Tokyo"
_DEFAULT_TIME = "09:30"
_GOAL = "自分の状態を確認して報告する"
_PROFILE_KEY = "free_team_daily_agent"
_WORKSPACE_RELATIVE = Path("cache") / "agent_workspaces" / "free-team-daily"


class FreeTeamDailyAgentTaskScheduler:
    """Admit at most one strict Free Team task per local calendar day."""

    def __init__(
        self,
        coordinator: Any,
        config: Any,
        *,
        profile_path: str | Path | None = None,
        workspace_path: str | Path | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.config = config
        self.profile_path = Path(profile_path) if profile_path is not None else None
        self._workspace_path_override = (
            Path(workspace_path) if workspace_path is not None else None
        )
        self._submitted_day: date | None = None
        self._retry_day: date | None = None
        self._retry_after: datetime | None = None
        self._retry_count = 0
        # Set for one tick after the exact AgentRevision v2 operator route has
        # been admitted.  It is intentionally not persisted as a selector.
        self._operator_route: Any | None = None

    def _defer_retry(self, local_now: datetime, reason: str) -> dict[str, Any]:
        if self._retry_day != local_now.date():
            self._retry_day = local_now.date()
            self._retry_count = 0
        self._retry_count += 1
        delay_minutes = min(5 * (2 ** min(self._retry_count - 1, 4)), 60)
        self._retry_after = local_now + timedelta(minutes=delay_minutes)
        logger.warning(
            "Free Team daily AgentTask deferred: date=%s reason=%s retry_minutes=%s",
            local_now.date().isoformat(),
            reason,
            delay_minutes,
        )
        return {
            "status": "retry_wait",
            "reason": reason,
            "retry_after": self._retry_after.isoformat(),
        }

    @property
    def workspace_path(self) -> Path:
        if self._workspace_path_override is not None:
            return self._workspace_path_override
        return Path(__file__).resolve().parents[2] / _WORKSPACE_RELATIVE

    def _profile_file(self) -> Path:
        if self.profile_path is not None:
            return self.profile_path
        return Path(__file__).resolve().parents[2] / "config" / "profiles" / f"{Features.profile_name()}.yaml"

    def _settings(self) -> tuple[dict[str, Any] | None, str | None]:
        path = self._profile_file()
        try:
            with path.open("r", encoding="utf-8") as handle:
                document = yaml.safe_load(handle) or {}
        except FileNotFoundError:
            return {"enabled": False}, None
        except (OSError, ValueError, yaml.YAMLError):
            return None, "profile_unavailable"
        if not isinstance(document, Mapping):
            return None, "profile_invalid"
        raw = document.get(_PROFILE_KEY, {})
        if raw in (None, {}):
            return {"enabled": False}, None
        if not isinstance(raw, Mapping):
            return None, "settings_invalid"
        return dict(raw), None

    @staticmethod
    def _local_now(now: datetime | None) -> datetime:
        zone = ZoneInfo(_DAILY_TIMEZONE)
        value = now or datetime.now(zone)
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        return value.astimezone(zone)

    @staticmethod
    def _parse_schedule(settings: Mapping[str, Any]) -> tuple[time | None, str | None]:
        enabled = settings.get("enabled", False)
        if type(enabled) is not bool:
            return None, "enabled_invalid"
        if not enabled:
            return None, None
        timezone_name = settings.get("timezone", _DAILY_TIMEZONE)
        if timezone_name != _DAILY_TIMEZONE:
            return None, "timezone_unsupported"
        time_text = settings.get("time", _DEFAULT_TIME)
        if not isinstance(time_text, str) or _TIME_RE.fullmatch(time_text) is None:
            return None, "time_invalid"
        hour, minute = (int(value) for value in time_text.split(":"))
        return time(hour=hour, minute=minute), None

    @staticmethod
    def _agent_uuid(settings: Mapping[str, Any]) -> tuple[str | None, str | None]:
        value = settings.get("agent_id")
        try:
            return str(UUID(str(value))), None
        except (TypeError, ValueError, AttributeError):
            return None, "agent_id_invalid"

    async def _service(self) -> Any | None:
        service = getattr(self.coordinator, "_agent_task_service", None)
        if service is None or not callable(getattr(service, "submit", None)):
            return None
        return service

    async def _owner_for_agent(self, service: Any, agent_id: str) -> tuple[str, str] | None:
        session_factory = getattr(service, "_session", None)
        if not callable(session_factory):
            return None
        session = await session_factory()
        try:
            agent = await session.get(Agent, UUID(agent_id))
            if agent is None or str(agent.state or "") != "active" or agent.created_by is None:
                return None
            user = await session.get(User, agent.created_by)
            if user is None or not bool(user.is_active):
                return None
            role = str(user.role or "").strip()
            if not role:
                return None
            return str(user.id), role
        finally:
            await session.close()

    async def _operator_route_for_agent(self, service: Any, agent_id: str) -> Any | None:
        """Resolve and validate the bound AgentRevision v2 operator pool route."""

        def valid_pool_route(route: Any) -> bool:
            provider = str(getattr(route, "provider", "") or "").casefold()
            model = str(getattr(route, "model", "") or "")
            backend = str(getattr(route, "backend", "") or "").casefold()
            pool_id = str(getattr(route, "pool_id", "") or "").strip()
            from .free_team_service import free_team_profile

            pools = free_team_profile(self.config).get("pools")
            return (
                provider == "routing-profile"
                and model == "free-team"
                and backend == "api"
                and bool(pool_id)
                and isinstance(pools, Mapping)
                and pool_id in pools
            )

        session_factory = getattr(service, "_session", None)
        if not callable(session_factory):
            # Lightweight scheduler fixtures may provide their bound route
            # directly; production services always expose a DB session.
            return self._operator_route if valid_pool_route(self._operator_route) else None
        session = await session_factory()
        try:
            revision = await session.get(AgentRevision, UUID(agent_id))
            if revision is None:
                return None
            from .agent_model_routing import AgentRoutingPins, resolve_agent_role_route
            route = resolve_agent_role_route(
                self.config,
                AgentRoutingPins.from_revision(revision),
                "operator",
            )
            return route if valid_pool_route(route) else None
        except Exception:
            # A daily automation must fail closed when the immutable Agent
            # route is unavailable or not a Free Team pool route.
            return None
        finally:
            await session.close()

    def _approved_empty_workspace(self) -> tuple[Path | None, str | None]:
        workspace = self.workspace_path
        roots = _approved_workspace_roots(self.config)
        if not roots or not any(_path_within(workspace, root) for root in roots):
            return None, "workspace_not_approved"
        try:
            if workspace.exists():
                inspect_existing_workspace(str(workspace))
                if any(workspace.iterdir()):
                    return None, "workspace_not_empty"
            else:
                # Creation is intentionally after the strict provider preflight.
                workspace.mkdir(parents=True, exist_ok=True)
                inspect_existing_workspace(str(workspace))
                if any(workspace.iterdir()):
                    return None, "workspace_not_empty"
        except (OSError, RuntimeError, ValueError):
            return None, "workspace_unavailable"
        return workspace, None

    async def _preflight(
        self,
        service: Any,
        *,
        prompt: str,
    ) -> FreeTeamApiTaskPreflight:
        from ..llm.manager import TargetConfig

        target_config = TargetConfig(
            self.config,
            {
                "llm_provider": "routing-profile",
                "llm_model": "free-team",
                **(
                    {
                        "routing_profiles.free-team.main_pool_id": str(
                            getattr(self._operator_route, "pool_id", "") or ""
                        )
                    }
                    if getattr(self._operator_route, "pool_id", None)
                    else {}
                ),
                "runtime.free_team_strict_complimentary_only": True,
                "runtime.free_team_api_task": True,
            },
        )
        intent = main_route_intent(target_config)
        refresher = getattr(getattr(service, "source", None), "openrouter_quota_refresher", None)
        return await preflight_free_team_api_task(
            intent,
            prompt=prompt,
            required_capabilities={"text"},
            config=target_config,
            openrouter_quota_refresher=refresher,
        )

    async def tick(self, now: datetime | None = None) -> dict[str, Any]:
        local_now = self._local_now(now)
        settings, settings_error = self._settings()
        if settings_error is not None:
            return self._defer_retry(local_now, settings_error)
        assert settings is not None
        enabled = settings.get("enabled", False)
        if type(enabled) is not bool:
            return self._defer_retry(local_now, "enabled_invalid")
        if not enabled:
            return {"status": "disabled"}

        scheduled_time, schedule_error = self._parse_schedule(settings)
        if schedule_error is not None:
            return self._defer_retry(local_now, schedule_error)
        assert scheduled_time is not None
        agent_id, agent_error = self._agent_uuid(settings)
        if agent_error is not None or agent_id is None:
            return self._defer_retry(local_now, agent_error or "agent_id_invalid")

        if local_now.time().replace(second=0, microsecond=0) < scheduled_time:
            return {
                "status": "not_due",
                "next_due_at": datetime.combine(local_now.date(), scheduled_time, tzinfo=local_now.tzinfo).isoformat(),
            }
        if self._submitted_day == local_now.date():
            return {"status": "already_submitted", "date": local_now.date().isoformat()}
        if (
            self._retry_day == local_now.date()
            and self._retry_after is not None
            and local_now < self._retry_after
        ):
            return {
                "status": "retry_wait",
                "retry_after": self._retry_after.isoformat(),
            }

        try:
            service = await self._service()
        except Exception:
            return self._defer_retry(local_now, "service_unavailable")
        if service is None:
            return self._defer_retry(local_now, "service_unavailable")
        try:
            owner = await self._owner_for_agent(service, agent_id)
        except Exception:
            return self._defer_retry(local_now, "agent_owner_unavailable")
        if owner is None:
            return self._defer_retry(local_now, "agent_owner_unavailable")

        try:
            self._operator_route = await self._operator_route_for_agent(service, agent_id)
        except Exception:
            self._operator_route = None
        if self._operator_route is None:
            return self._defer_retry(local_now, "agent_route_not_free_team")

        task_prompt = _task_prompt({"tool_free_execution": True, "goal": _GOAL})
        try:
            preflight = await self._preflight(service, prompt=task_prompt)
        except Exception:
            return self._defer_retry(local_now, "preflight_unavailable")
        if not preflight.eligible:
            return self._defer_retry(local_now, preflight.reason)

        workspace, workspace_error = self._approved_empty_workspace()
        if workspace_error is not None or workspace is None:
            return self._defer_retry(
                local_now, workspace_error or "workspace_unavailable"
            )

        owner_id, owner_role = owner
        idempotency_key = f"free-team-daily-{local_now.date().strftime('%Y%m%d')}"
        payload = {
            "goal": _GOAL,
            "workspace": str(workspace),
            "mode": "read_only",
            "allow_commands": False,
            "requires_command_verification": False,
            "agent_id": agent_id,
            "required_capabilities": ["workspace_read"],
        }
        try:
            task, created = await service.submit(
                payload,
                owner=owner_id,
                actor_role=owner_role,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "submit_failed") or "submit_failed")[:96]
            if re.fullmatch(r"[a-z0-9_:-]{1,96}", code.casefold()) is None:
                code = "submit_failed"
            return self._defer_retry(local_now, code.casefold())
        self._submitted_day = local_now.date()
        self._retry_day = None
        self._retry_after = None
        self._retry_count = 0
        task_id = str(task.get("task_id") or "") if isinstance(task, Mapping) else ""
        logger.info(
            "Free Team daily AgentTask admitted: date=%s agent_id=%s task_id=%s created=%s candidate_id=%s",
            local_now.date().isoformat(),
            agent_id,
            task_id,
            bool(created),
            preflight.candidate_id,
        )
        return {
            "status": "submitted" if created else "already_submitted",
            "created": bool(created),
            "task_id": task_id,
            "date": local_now.date().isoformat(),
            "candidate_id": preflight.candidate_id,
        }


__all__ = ["FreeTeamDailyAgentTaskScheduler"]
