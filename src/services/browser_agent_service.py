"""Operate existing Edge tabs through the installed AoiTalk extension."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from .browser_agent_models import BrowserAgentError, BrowserAgentSettings, BrowserPlan
from .browser_agent_planner import build_browser_plan
from .candidate_decision_service import CandidateDecisionRouter, llm_choice_callback
from pc_bridge.edge import EdgeBridgeError
from .jev_decision_service import JevDecisionService
from .outbound_privacy_service import (
    OutboundPrivacyGateway,
    current_effective_privacy_mode,
)

_BROWSER_WORK = threading.Lock()


class BrowserAgentService:
    def __init__(self, config: Any, *, client: Any, bridge=None, decision_router=None):
        self.config = config
        self.settings = BrowserAgentSettings.from_config(config)
        self.client = client
        self.bridge = bridge
        self.operation_lock = getattr(bridge, "operation_lock", _BROWSER_WORK)
        self.router = decision_router
        self.run_id = uuid4().hex
        self.tab_id = None
        self.trace: list[dict[str, Any]] = []
        self.observation: dict[str, Any] = {}

    def _event(self, event: str, **fields: Any):
        self.trace.append({"event": event, **fields})

    def _result(self, status: str, reason: str = ""):
        return {
            "run_id": self.run_id,
            "status": status,
            "reason": reason,
            "tab_id": self.tab_id,
            "completion_verified": status == "completed",
            "fallback_reason": self.router.fallback_reason if self.router else "",
            "observation": self.observation,
            "trace": self.trace,
            "success": status in {"completed", "steps_completed"},
        }

    async def _observe(self, action="read"):
        self.observation = await self.bridge.request(
            "observe", tab_id=self.tab_id, action=action
        )
        return self.observation

    async def _execute(self, plan):
        self.router = self.router or CandidateDecisionRouter(
            llm_choose=llm_choice_callback(self.client),
            jev=JevDecisionService(
                OutboundPrivacyGateway(self.config),
                model=self.settings.jev_model,
                timeout_seconds=5,
                max_retries=0,
            ),
            jev_enabled=self.settings.jev_enabled,
            local_only=current_effective_privacy_mode(self.config) == "local_only",
        )
        for number, step in enumerate(plan.steps, 1):
            from ..llm.generation_cancellation import raise_if_generation_interrupted

            raise_if_generation_interrupted()
            self._event("step_started", step=number, action=step.action)
            if step.action in {"type", "click", "select", "check", "uncheck"}:
                for attempt in range(2):
                    observed = await self._observe(step.action)
                    raw_candidates = observed.get("candidates", {})
                    target_ids = {
                        f"e{index + 1}": identifier
                        for index, identifier in enumerate(raw_candidates)
                    }
                    candidates = {
                        key: raw_candidates[identifier]
                        for key, identifier in target_ids.items()
                    }
                    if not candidates:
                        raise BrowserAgentError("browser_no_candidates")
                    state = {k: v for k, v in observed.items() if k != "candidates"}
                    choice = await self.router.choose(
                        state,
                        f"Operation: {step.action}. Target: {step.instruction}",
                        candidates,
                    )
                    self._event(
                        "decision", step=number, action=step.action, **asdict(choice)
                    )
                    try:
                        outcome = await self.bridge.request(
                            "act",
                            tab_id=self.tab_id,
                            action=step.action,
                            target=target_ids[choice.choice],
                            value=step.value,
                        )
                        self.tab_id = outcome.get("tab_id", self.tab_id)
                        break
                    except EdgeBridgeError as exc:
                        if str(exc) != "edge_stale_element" or attempt:
                            raise
                self._event("action_completed", step=number, action=step.action)
            elif step.action in {"navigate", "back", "scroll"}:
                outcome = await self.bridge.request(
                    step.action,
                    tab_id=self.tab_id,
                    url=step.url,
                    direction=step.value or "down",
                )
                self.tab_id = outcome.get("tab_id", self.tab_id)
            elif step.action == "wait":
                await asyncio.sleep(float(step.value or "0.5"))
            await self._verify(step.expected_text, step.expected_url)
        await self._verify(plan.completion_text, plan.completion_url)
        self._event("run_completed")
        return self._result(
            "completed"
            if plan.completion_text or plan.completion_url
            else "steps_completed"
        )

    async def _verify(self, text="", url=""):
        end = asyncio.get_running_loop().time() + (10 if text or url else 0)
        while True:
            observed = await self._observe()
            if (
                not text
                or " ".join(text.split()) in " ".join(observed.get("text", "").split())
            ) and (not url or observed.get("url", "").rstrip("/") == url.rstrip("/")):
                return
            if asyncio.get_running_loop().time() >= end:
                raise BrowserAgentError("browser_expectation_failed")
            await asyncio.sleep(0.25)

    async def run_goal(
        self,
        *,
        goal: str,
        start_url: str = "",
        tab_id: int | None = None,
        completion_text: str = "",
        completion_url: str = "",
    ):
        if not self.settings.enabled:
            return self._result("failed", "browser_agent_disabled")
        if not self.operation_lock.acquire(blocking=False):
            return self._result("failed", "edge_browser_busy")
        try:
            if self.bridge is None:
                from .pc_bridge_service import resolve_control_bridge
                self.bridge = await resolve_control_bridge()
            async with asyncio.timeout(self.settings.timeout_seconds):
                target = await self.bridge.request(
                    "resolve_tab", tab_id=tab_id, url=start_url
                )
                self.tab_id = target["tab_id"]
                await self._observe()
                plan = await build_browser_plan(
                    self.client,
                    start_url=target["url"],
                    goal=goal,
                    completion_text=completion_text,
                    completion_url=completion_url,
                    max_steps=self.settings.max_steps,
                    timeout_seconds=45,
                )
                self._event(
                    "plan_created", step_count=len(plan.steps), tab_id=self.tab_id
                )
                return await self._execute(plan)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._event("run_stopped", error=type(exc).__name__)
            return self._result(
                "failed",
                "browser_timeout" if isinstance(exc, TimeoutError) else str(exc),
            )
        finally:
            # The user's Edge, tabs and login state are deliberately left open.
            self.operation_lock.release()

    async def run(
        self, plan: BrowserPlan | dict[str, Any], *, tab_id: int | None = None
    ):
        """Typed executor entry for repeatable integration tests and callers."""
        plan = BrowserPlan.model_validate(plan)
        if len(plan.steps) > self.settings.max_steps:
            return self._result("failed", "browser_step_budget_exceeded")
        if not self.settings.enabled:
            return self._result("failed", "browser_agent_disabled")
        if not self.operation_lock.acquire(blocking=False):
            return self._result("failed", "edge_browser_busy")
        try:
            if self.bridge is None:
                from .pc_bridge_service import resolve_control_bridge
                self.bridge = await resolve_control_bridge()
            async with asyncio.timeout(self.settings.timeout_seconds):
                target = await self.bridge.request(
                    "resolve_tab", tab_id=tab_id, url=plan.start_url
                )
                self.tab_id = target["tab_id"]
                return await self._execute(plan)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._result(
                "failed",
                "browser_timeout" if isinstance(exc, TimeoutError) else str(exc),
            )
        finally:
            self.operation_lock.release()
