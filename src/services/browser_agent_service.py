"""Shared Jev/DOM execution on the explicitly selected Bridge backend."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import time
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from .browser_agent_models import BrowserAgentError, BrowserAgentSettings, BrowserPlan
from .browser_agent_planner import build_browser_plan
from .candidate_decision_service import CandidateDecisionError, CandidateDecisionRouter, llm_choice_callback
from .browser_run_state import BrowserRunState, ReplanNeeded
from pc_bridge.edge import EdgeBridgeError
from .pc_bridge_store import PcBridgeError
from .jev_decision_service import JevDecisionService
from .decision_receipt_service import finalize_decision_receipt
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
        self._requires_final_confirmation = getattr(bridge, "browser_backend", "") == "owned_profile"
        self.operation_lock = getattr(bridge, "operation_lock", _BROWSER_WORK)
        self.router = decision_router
        self._owned_jev: JevDecisionService | None = None
        self.run_id = uuid4().hex
        self.tab_id = None
        self.trace: list[dict[str, Any]] = []
        self.observation: dict[str, Any] = {}
        self._material_states: dict[str, Any] = {}
        self._started_at = time.perf_counter()
        self.state = BrowserRunState()
        self.replans = 0
        self.step_count = 0
        self.planner_calls = 0
        self._decision_receipt = None
        self.normal_decisions = 0
        self.exception_decisions = 0
        self.scope_requests = 0
        self._checkpoint_data = None
        self._checkpoint_cursor = 0
        self._checkpoint_phase = "ready"
        self._receipts_unknown = False
        self._timeout_context = None

    def _dedicated_backend(self):
        return getattr(self.bridge, "browser_backend", "") == "owned_profile"

    def _run_scoped_backend(self):
        return self._helper_backend() or self._dedicated_backend()

    async def _save_checkpoint(self, phase):
        if not self._dedicated_backend() or self._checkpoint_data is None:
            return
        from .turn_context import get_turn_context
        self._checkpoint_phase = phase
        data = {**self._checkpoint_data, "cursor": self._checkpoint_cursor, "phase": phase,
                "tab_id": self.tab_id, "state": {"salt": self.state.salt, "targets": sorted(self.state.targets),
                    "performed": self.state.performed, "recovery_states": sorted(self.state.recovery_states),
                    "browser_context": getattr(self, "_browser_context", None), "receipts_unknown": self._receipts_unknown}}
        saved = await self.bridge.request("checkpoint_save", run_id=self.run_id,
                                         session_id=get_turn_context().session_id, checkpoint=data)
        if saved.get("saved") is not True:
            raise BrowserAgentError("browser_checkpoint_save_unverified")

    async def _before_dispatch(self):
        await self._save_checkpoint("in_flight")

    async def _confirm_final(self, step, candidate, *, target=None):
        if not self._requires_final_confirmation or step.action != "click":
            return {}
        import re
        label = str(candidate.get("label", "")) + " " + step.instruction
        if candidate.get("type") != "submit" and not re.search(
                r"送信|予約|購入|確定|注文|申[し込]*込|公開|投稿|\b(?:submit|pay|book|publish|post|purchase|confirm)\b", label, re.I):
            return {}
        from .human_interaction import get_human_interaction_manager, HumanInteractionKind
        from .turn_context import get_turn_context
        await self._save_checkpoint("final_confirmation")
        manager, turn = get_human_interaction_manager(), get_turn_context()
        if manager is None:
            raise BrowserAgentError("browser_final_confirmation_required")
        material = self._material_states.get(target)
        if (not self._dedicated_backend() or getattr(self.bridge, "supports_material_state_guard", False) is not True
                or not isinstance(material, dict) or material.get("complete") is not True
                or material.get("tab_id") != self.tab_id
                or material.get("document_id") != candidate.get("document_id")
                or material.get("target_stable_id") != candidate.get("stable_id")):
            raise BrowserAgentError("browser_final_material_guard_required")
        # Freeze the approval conditions before awaiting the human answer.
        # Private state goes only to the user approval boundary and Bridge act.
        material = copy.deepcopy(material)
        material_digest = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        facts = material["facts_text"].split("\n", 1)[0]
        values = " / ".join(f"{item['label']}: {item['display_value']}" for item in material["controls"])
        response = await manager.request_interaction(kind=HumanInteractionKind.ASK_USER_QUESTION,
            payload={"question": f"確認対象: {facts}\n入力内容: {values}\n最終操作を実行しますか？", "input_type": "single_choice",
                     "choices": ["最終操作を実行", "作業を中止"], "allow_multiple": False, "allow_free_text": False},
            session_id=turn.session_id or "", user_id=turn.user_id or "", timeout_seconds=600)
        if not isinstance(response, dict) or response.get("cancelled") or response.get("selected_choices") != ["最終操作を実行"]:
            raise BrowserAgentError("browser_final_confirmation_required")
        return {"expected_material_state": material, "expected_material_state_digest": material_digest}

    async def _login_handoff(self):
        from .browser_login_handoff import wait_for_login
        started = asyncio.get_running_loop().time()
        deadline = self._timeout_context.when() if self._timeout_context is not None else None
        if self._timeout_context is not None:
            self._timeout_context.reschedule(None)
        try:
            choice = await wait_for_login(self)
            if choice == "switch":
                await self._switch_to_ordinary()
            else:
                await self._save_checkpoint("ready")
        finally:
            if self._timeout_context is not None and deadline is not None and not self._timeout_context.expired():
                self._timeout_context.reschedule(deadline + asyncio.get_running_loop().time() - started)

    async def _switch_to_ordinary(self):
        from .pc_bridge_service import RemoteControlBridge
        from .human_interaction import get_human_interaction_manager, HumanInteractionKind
        from .turn_context import get_turn_context
        old = self.bridge
        await self._save_checkpoint("switched")
        stopped = await old.request("cancel_login", run_id=self.run_id, tab_id=self.tab_id)
        if stopped.get("cancelled") is not True:
            raise BrowserAgentError("browser_switch_stop_unverified")
        if self._receipts_unknown:
            raise BrowserAgentError("browser_switch_receipt_unknown")
        normal = RemoteControlBridge(old.owner, old.device_id, old.hub)
        if normal._connection is not old._connection:
            raise BrowserAgentError("browser_switch_connection_changed")
        if normal.browser_backend != "agent_browser" or not normal._connection.edge_connected:
            raise BrowserAgentError("browser_switch_ordinary_not_ready")
        # Readiness never grants account authority. A separate human account
        # confirmation is required before any input/navigation on this target.
        manager, turn = get_human_interaction_manager(), get_turn_context()
        if manager is None:
            raise BrowserAgentError("browser_switch_account_unverified")
        label = "対象アカウントを確認して続ける"
        response = await manager.request_interaction(kind=HumanInteractionKind.ASK_USER_QUESTION,
            payload={"question": f"PC {old.device_id} の普段のブラウザで、今回の対象アカウントと作業状態を確認してください。",
                     "input_type": "single_choice", "choices": [label, "作業を中止"],
                     "allow_multiple": False, "allow_free_text": False},
            user_id=turn.user_id or "", session_id=turn.session_id or "", timeout_seconds=600)
        if not isinstance(response, dict) or response.get("cancelled") or response.get("selected_choices") != [label]:
            raise BrowserAgentError("browser_switch_account_unverified")
        self.bridge = normal
        target = await self._resolve_target(tab_id=None, url="")
        self.tab_id = target["tab_id"]
        self._event("browser_switched", account_confirmed=True, previous_authority_revoked=True)
        # Apply the ordinary helper's established strict decision policy.
        if self._owned_jev is not None:
            self.router = CandidateDecisionRouter(llm_choose=llm_choice_callback(self.client), jev=self._owned_jev,
                jev_enabled=self.settings.jev_enabled, local_only=current_effective_privacy_mode(self.config) == "local_only",
                default_decision_kind="browser_dom", require_single_batch=True, allow_llm_fallback=False)

    def _helper_backend(self):
        return getattr(self.bridge, "browser_backend", "extension") == "agent_browser"

    async def _resolve_target(self, *, tab_id, url):
        params = {"tab_id": tab_id, "url": url}
        if self._run_scoped_backend():
            params["run_id"] = self.run_id
        return await self.bridge.request("resolve_tab", **params)

    async def _release_helper_run(self, result):
        if not self._run_scoped_backend():
            return
        verified = False
        retained = False
        try:
            async with asyncio.timeout(5):
                cleanup = await self.bridge.request("end_run", run_id=self.run_id)
            verified = cleanup.get("ended") is True and cleanup.get("refs_invalidated") is True
            retained = cleanup.get("connection_retained") is True
        except asyncio.CancelledError:
            self._event("helper_run_released", verified=False, connection_retained=False)
            raise
        except Exception:
            pass
        self._event("helper_run_released", verified=verified, connection_retained=retained)
        if not verified and result is not None:
            result.update(status="failed", success=False, completion_verified=False,
                          reason="agent_browser_run_release_unverified")

    def _finish_decision(self, outcome, *, validation_result=None):
        receipt = finalize_decision_receipt(self._decision_receipt, outcome=outcome,
                                            validation_result=validation_result)
        if receipt is not None:
            self._event("decision_outcome", decision_id=receipt.decision_id, receipt=receipt.to_dict())
        self._decision_receipt = None

    def _event(self, event: str, **fields: Any):
        self.trace.append({"event": event, "elapsed_ms": round((time.perf_counter() - self._started_at) * 1000), **fields})

    def _result(self, status: str, reason: str = ""):
        return {
            "run_id": self.run_id,
            "status": status,
            "reason": reason,
            "tab_id": self.tab_id,
            "completion_verified": status == "completed",
            "fallback_reason": self.router.fallback_reason if self.router else "",
            "observation": self.state.redact(self.observation),
            "trace": self.trace,
            "success": status in {"completed", "steps_completed"},
            "metrics": {**getattr(self.router, "metrics", {}), "replans": self.replans,
                        "planner_calls": self.planner_calls, "steps": self.step_count,
                        "normal_candidate_decisions": self.normal_decisions,
                        "exception_candidate_decisions": self.exception_decisions,
                        "candidate_scope_requests": self.scope_requests,
                        "total_ms": round((time.perf_counter() - self._started_at) * 1000)},
        }

    async def _observe(self, action="read"):
        started = time.perf_counter()
        params = {"tab_id": self.tab_id, "action": action}
        if self._run_scoped_backend():
            params["run_id"] = self.run_id
        self.observation = await self.bridge.request("observe", **params)
        # Form values belong only to private human approval/dispatch boundaries.
        # General planning and trace must never see the snapshot.
        self._material_states = self.observation.pop("material_states", {})
        if self._dedicated_backend() and self.observation.get("authentication_required") is True:
            self.observation = {}
            await self._login_handoff()
            return await self._observe(action)
        self._event("observation", action=action,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    candidate_count=len(self.observation.get("candidates", {})),
                    coverage=self.observation.get("coverage", {}))
        if self._helper_backend() and (self.observation.get("error") == "candidate_scope_required"
                or self.observation.get("coverage", {}).get("complete") is False):
            self.scope_requests += 1
            self._event("candidate_scope_required", provider_calls=0, coverage=self.observation.get("coverage", {}))
            raise BrowserAgentError("candidate_scope_required")
        return self.observation

    async def _observe_after_unknown(self):
        # A lost response may follow an executed submit. Observe, never replay.
        try:
            async with asyncio.timeout(2):
                await self._observe()
        except Exception:
            self._event("reobservation_unavailable")

    async def _plan(self, *, goal, start_url, completion_text, completion_url, recovery_reason=""):
        self.planner_calls += 1
        started = time.perf_counter()
        plan = await build_browser_plan(
            self.client, start_url=start_url, goal=goal,
            completion_text=completion_text, completion_url=completion_url,
            max_steps=max(1, self.settings.max_steps - self.step_count), timeout_seconds=45,
            observation=self.state.observation(self.observation, planning=True),
            completed_steps=self.state.performed[-12:], recovery_reason=recovery_reason,
        )
        self.state.register(plan)
        self._event("plan_created", step_count=len(plan.steps), tab_id=self.tab_id,
                    duration_ms=round((time.perf_counter() - started) * 1000), recovery_reason=recovery_reason)
        return plan

    async def _execute(self, plan, *, adaptive=False):
        if self.router is None:
            self._owned_jev = JevDecisionService(
                OutboundPrivacyGateway(self.config),
                model=self.settings.jev_model, timeout_seconds=5, max_retries=0,
            )
            self.router = CandidateDecisionRouter(
                llm_choose=llm_choice_callback(self.client),
                jev=self._owned_jev,
                jev_enabled=self.settings.jev_enabled,
                local_only=current_effective_privacy_mode(self.config) == "local_only",
                default_decision_kind="browser_dom",
                require_single_batch=self._helper_backend(),
                allow_llm_fallback=not self._helper_backend(),
            )
        if self._helper_backend() and (
            not isinstance(self.router, CandidateDecisionRouter)
            or not self.router._require_single_batch
            or self.router._allow_llm_fallback
            or getattr(self.router._jev, "max_retries", None) != 0
        ):
            raise BrowserAgentError("browser_helper_router_policy_required")
        if self._owned_jev is not None:
            # All steps and adaptive replans share one run-local HTTP pool.
            async with self._owned_jev.session():
                return await self._execute_plan(plan, adaptive=adaptive)
        # An injected router (and its Jev client) stays owned by the caller.
        return await self._execute_plan(plan, adaptive=adaptive)

    async def _execute_plan(self, plan, *, adaptive=False):
        self.state.register(plan)
        remaining = list(plan.steps)
        if self._checkpoint_data is not None:
            # Progress belongs to the run, even after a login backend switch.
            self._checkpoint_data["plan"] = plan.model_dump()
            remaining = remaining[self._checkpoint_cursor:]
            await self._save_checkpoint("ready")
        while remaining:
            from ..llm.generation_cancellation import raise_if_generation_interrupted
            raise_if_generation_interrupted()
            if self.step_count >= self.settings.max_steps:
                raise BrowserAgentError("browser_step_budget_exceeded")
            step = remaining.pop(0)
            self.step_count += 1
            number = self.step_count
            self._event("step_started", step=number, action=step.action)
            try:
                await self._step(step, number, adaptive=adaptive, goal=plan.goal)
            except ReplanNeeded as exc:
                if not adaptive:
                    raise
                if self.replans >= 2 or self.step_count >= self.settings.max_steps:
                    raise BrowserAgentError("browser_replan_budget_exceeded") from None
                # A new observation is required before revising the remaining
                # plan. Neither a timeout nor a dispatched failure enters here.
                await self._observe("click")
                projection = self.state.observation(self.observation, planning=True)
                signature = self.state.digest([str(exc), projection, self.state.performed])
                if signature in self.state.recovery_states:
                    raise BrowserAgentError("browser_replan_no_progress") from None
                self.state.recovery_states.add(signature)
                self.replans += 1
                self._event("replan_requested", step=number, reason=str(exc))
                repaired = await self._plan(
                    goal=plan.goal, start_url=plan.start_url,
                    completion_text=plan.completion_text, completion_url=plan.completion_url,
                    recovery_reason=str(exc),
                )
                self._checkpoint_cursor = 0
                if self._checkpoint_data is not None:
                    self._checkpoint_data["plan"] = repaired.model_dump()
                remaining = list(repaired.steps)
                continue
            # Post-dispatch failed assertions never replay or replan a write.
            try:
                await self._verify(step.expected_text, step.expected_url)
            except BaseException:
                self._finish_decision("postcondition_failed")
                raise
            self._finish_decision("verified" if step.expected_text or step.expected_url else "performed")
            self._checkpoint_cursor += 1
            await self._save_checkpoint("ready")
        await self._verify(plan.completion_text, plan.completion_url)
        if not plan.completion_text and not plan.completion_url:
            await self._observe()
        self._event("run_completed")
        await self._save_checkpoint("completed")
        return self._result("completed" if plan.completion_text or plan.completion_url else "steps_completed")

    async def _step(self, step, number, *, adaptive, goal):
        self._decision_receipt = None
        try:
            return await self._step_impl(step, number, adaptive=adaptive, goal=goal)
        except asyncio.CancelledError:
            self._finish_decision("cancelled")
            raise
        except ReplanNeeded:
            self._finish_decision("rejected", validation_result="host_revalidation_failed")
            raise
        except BaseException:
            self._finish_decision("failed")
            raise

    async def _step_impl(self, step, number, *, adaptive, goal):
        if step.action in {"type", "click", "select", "check", "uncheck"}:
            for attempt in range(2):
                observed = await self._observe(step.action)
                raw = observed.get("candidates", {})
                if not raw:
                    if attempt == 0:
                        await asyncio.sleep(0.2)
                        continue
                    raise ReplanNeeded("browser_no_candidates")
                target_ids = {f"e{i + 1}": key for i, key in enumerate(raw)}
                candidates = {key: raw[value] for key, value in target_ids.items()}
                state = self.state.observation(observed)
                if adaptive:
                    state["progress"] = self.state.redact({"goal": goal[:2000], "executed": self.state.performed[-6:]})
                started = time.perf_counter()
                try:
                    choice = await self.router.choose(state,
                        self.state.redact(f"Operation: {step.action}. Target: {step.instruction}"),
                        self.state.redact(candidates))
                except CandidateDecisionError as exc:
                    if str(exc) == "candidate_not_found":
                        raise ReplanNeeded(str(exc)) from None
                    if self._helper_backend() and str(exc) == "candidate_scope_required":
                        self.scope_requests += 1
                        self._event("candidate_scope_required", provider_calls=0,
                                    coverage=getattr(self.router, "last_coverage", {}))
                    raise
                if self._helper_backend():
                    if attempt == 0 and self.replans == 0:
                        self.normal_decisions += 1
                    else:
                        self.exception_decisions += 1
                self._decision_receipt = getattr(choice, "receipt", None)
                decision_id = self._decision_receipt.decision_id if self._decision_receipt else None
                self._event("decision", step=number, action=step.action, decision_id=decision_id, **asdict(choice),
                            duration_ms=round((time.perf_counter() - started) * 1000))
                if adaptive and self.state.performed and (getattr(choice, "stuck", None) or 0) >= 0.9:
                    raise ReplanNeeded("browser_progress_stalled")
                if choice.choice not in candidates or choice.choice not in target_ids:
                    raise ReplanNeeded("host_candidate_missing")
                candidate = candidates[choice.choice]
                keys = self.state.keys(step, candidate, observed.get("url"))
                if adaptive:
                    self.state.check(keys)
                self._decision_receipt = finalize_decision_receipt(
                    self._decision_receipt, host_action=step.action, host_candidate=choice.choice,
                    outcome="dispatching", validation_result="host_candidate_valid",
                )
                self._event("action_proposed", step=number, action=step.action, decision_id=decision_id,
                            target_ref=target_ids[choice.choice], stable_id=candidate.get("stable_id"),
                            coverage=getattr(self.router, "last_coverage", {}))
                material_params = await self._confirm_final(step, candidate, target=target_ids[choice.choice])
                await self._before_dispatch()
                try:
                    action_params = {"tab_id": self.tab_id, "action": step.action,
                                     "target": target_ids[choice.choice], "value": step.value, **material_params}
                    if self._run_scoped_backend():
                        action_params["run_id"] = self.run_id
                    outcome = await self.bridge.request("act", **action_params)
                except asyncio.CancelledError:
                    self._finish_decision("unknown")
                    self._receipts_unknown = True
                    self._event("action_outcome_unknown", step=number, action=step.action)
                    await self._observe_after_unknown()
                    raise
                except (EdgeBridgeError, PcBridgeError, TimeoutError) as exc:
                    if str(exc) == "edge_stale_element":
                        self._finish_decision("stale", validation_result="bridge_stale")
                        if attempt == 0:
                            continue
                        raise ReplanNeeded("edge_stale_element") from None
                    self._finish_decision("unknown")
                    self._receipts_unknown = True
                    self._event("action_outcome_unknown", step=number, action=step.action, decision_id=decision_id)
                    await self._observe_after_unknown()
                    raise
                self.tab_id = outcome.get("tab_id", self.tab_id)
                if self._dedicated_backend() and outcome.get("authentication_required") is True:
                    # The driver proves no action was dispatched on a login
                    # document. Re-observe after the human handoff; never use
                    # the stale pre-login candidate reference.
                    if outcome.get("executed") is not False:
                        self._receipts_unknown = True
                        self._finish_decision("unknown", validation_result="login_outcome_unverified")
                        raise BrowserAgentError("browser_login_outcome_unverified")
                    self._finish_decision("rejected", validation_result="login_handoff_not_dispatched")
                    await self._save_checkpoint("awaiting_login")
                    await self._login_handoff()
                    if attempt == 0:
                        continue
                    raise ReplanNeeded("browser_login_document_changed")
                if self._dedicated_backend() and outcome.get("executed") is not True:
                    self._receipts_unknown = outcome.get("executed") is not False
                self._event("action_result", step=number, action=step.action, decision_id=decision_id,
                            executed=outcome.get("executed"),
                            execution_status=outcome.get("execution_status", "unknown"),
                            verification=outcome.get("verification", {"status": "unknown"}))
                if outcome.get("ok") is False:
                    error = outcome.get("error", "edge_action_failed")
                    if outcome.get("executed") is False and error in {
                        "edge_stale_element", "edge_target_not_actionable", "edge_target_covered",
                        "edge_target_moving", "edge_option_not_found", "edge_option_ambiguous",
                    }:
                        self._finish_decision("stale" if error == "edge_stale_element" else "rejected",
                                              validation_result="bridge_rejected")
                        if error == "edge_stale_element" and attempt == 0:
                            continue
                        raise ReplanNeeded(error)
                    await self._observe_after_unknown()
                    self._finish_decision("failed" if outcome.get("executed") is False else "unknown")
                    raise BrowserAgentError(error)
                if adaptive:
                    self.state.record(step, candidate, keys, outcome)
                self._event("action_completed", step=number, action=step.action, decision_id=decision_id)
                return
        elif step.action in {"navigate", "back", "scroll"}:
            for attempt in range(2):
                if self._dedicated_backend():
                    await self._observe()
                action_params = {"tab_id": self.tab_id, "url": step.url, "direction": step.value or "down"}
                if self._run_scoped_backend():
                    action_params["run_id"] = self.run_id
                await self._before_dispatch()
                try:
                    outcome = await self.bridge.request(step.action, **action_params)
                except (asyncio.CancelledError, EdgeBridgeError, PcBridgeError, TimeoutError):
                    self._event("action_outcome_unknown", step=number, action=step.action)
                    self._receipts_unknown = True
                    await self._observe_after_unknown()
                    raise
                if self._dedicated_backend() and outcome.get("authentication_required") is True:
                    if outcome.get("executed") is not False:
                        self._receipts_unknown = True
                        self._event("action_outcome_unknown", step=number, action=step.action)
                        raise BrowserAgentError("browser_login_outcome_unverified")
                    await self._login_handoff()
                    if attempt == 0:
                        continue
                    raise ReplanNeeded("browser_login_document_changed")
                if outcome.get("ok") is False or (self._run_scoped_backend() and outcome.get("executed") is not True):
                    self._receipts_unknown = outcome.get("executed") is not False
                    self._event("action_outcome_unknown" if outcome.get("executed") is not False else "action_rejected",
                                step=number, action=step.action)
                    await self._observe_after_unknown()
                    raise BrowserAgentError(outcome.get("error") or "agent_browser_outcome_unknown")
                break
            self.tab_id = outcome.get("tab_id", self.tab_id)
            if adaptive:
                self.state.record(step, {}, set(), outcome)
        elif step.action == "wait":
            await asyncio.sleep(float(step.value or "0.5"))
        elif step.action in {"read", "done"}:
            await self._observe()

    async def _verify(self, text="", url=""):
        if not text and not url:
            return
        end = asyncio.get_running_loop().time() + 10
        while True:
            if self._helper_backend():
                matched = await self.bridge.request("verify", tab_id=self.tab_id,
                    run_id=self.run_id, expected_text=text, expected_url=url)
                if set(matched) != {"matched"} or type(matched["matched"]) is not bool:
                    raise BrowserAgentError("agent_browser_verify_response_invalid")
                if matched["matched"]:
                    return
                if asyncio.get_running_loop().time() >= end:
                    raise BrowserAgentError("browser_expectation_failed")
                await asyncio.sleep(0.25)
                continue
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
        if self._dedicated_backend() and not self.settings.dedicated_enabled:
            return self._result("failed", "browser_dedicated_disabled")
        if self.bridge is None:
            from .pc_bridge_service import resolve_control_bridge
            try:
                self.bridge = await resolve_control_bridge()
            except Exception as exc:
                return self._result("failed", str(exc))
            self.operation_lock = getattr(self.bridge, "operation_lock", _BROWSER_WORK)
        if not self.operation_lock.acquire(blocking=False):
            return self._result("failed", "edge_browser_busy")
        result = None
        try:
            async with asyncio.timeout(self.settings.timeout_seconds) as timeout_context:
                self._timeout_context = timeout_context
                target = await self._resolve_target(tab_id=tab_id, url=start_url)
                self.tab_id = target["tab_id"]
                self._browser_context = target.get("browser_context")
                self._checkpoint_cursor = 0
                self._checkpoint_data = {"goal": goal, "start_url": start_url, "plan": None,
                    "completion_text": completion_text, "completion_url": completion_url}
                await self._save_checkpoint("ready")
                await self._observe("click")
                plan = await self._plan(
                    goal=goal, start_url=self.observation.get("url", start_url),
                    completion_text=completion_text, completion_url=completion_url,
                )
                result = await self._execute(plan, adaptive=True)
                return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._event("run_stopped", error=type(exc).__name__)
            return self._result(
                "failed",
                "browser_timeout" if isinstance(exc, TimeoutError) else str(exc),
            )
        finally:
            # Revoke run refs, retaining the approved Bridge-lifetime CDP connection.
            try:
                await self._release_helper_run(result)
            finally:
                self.operation_lock.release()

    async def resume(self, run_id: str):
        """Restore only a definitely undispatched checkpoint on its same tab."""
        from .turn_context import get_turn_context
        from uuid import UUID
        if not self.settings.enabled or not self.settings.dedicated_enabled or not self._dedicated_backend():
            return self._result("failed", "browser_dedicated_disabled")
        try:
            self.run_id = UUID(run_id).hex
        except (ValueError, TypeError, AttributeError):
            return self._result("failed", "browser_resume_run_invalid")
        if not self.operation_lock.acquire(blocking=False):
            return self._result("failed", "edge_browser_busy")
        result = None
        try:
            loaded = await self.bridge.request("checkpoint_load", run_id=self.run_id,
                                               session_id=get_turn_context().session_id)
            data = loaded["checkpoint"]
            if data["phase"] not in {"ready", "awaiting_login", "final_confirmation"} or data["state"].get("receipts_unknown"):
                raise BrowserAgentError("browser_resume_outcome_unverified")
            self._checkpoint_data = {key: data[key] for key in ("goal", "start_url", "plan", "completion_text", "completion_url")}
            self._checkpoint_cursor = data["cursor"]
            self.state.salt = data["state"]["salt"]
            self.state.targets = set(data["state"]["targets"])
            self.state.performed = data["state"]["performed"]
            self.state.recovery_states = set(data["state"]["recovery_states"])
            target = await self._resolve_target(tab_id=data["tab_id"], url="")
            self.tab_id = target["tab_id"]
            self._browser_context = target.get("browser_context")
            if self._browser_context != data["state"].get("browser_context"):
                raise BrowserAgentError("browser_resume_context_changed")
            async with asyncio.timeout(self.settings.timeout_seconds) as timeout_context:
                self._timeout_context = timeout_context
                if data["phase"] == "awaiting_login":
                    await self._login_handoff()
                await self._observe("click")
                plan = BrowserPlan.model_validate(data["plan"]) if data["plan"] else await self._plan(
                    goal=data["goal"], start_url=data["start_url"], completion_text=data["completion_text"],
                    completion_url=data["completion_url"])
                if type(self._checkpoint_cursor) is not int or not 0 <= self._checkpoint_cursor <= len(plan.steps):
                    raise BrowserAgentError("browser_resume_checkpoint_invalid")
                self.step_count = self._checkpoint_cursor
                result = await self._execute(plan, adaptive=True)
                return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._result("failed", "browser_timeout" if isinstance(exc, TimeoutError) else str(exc))
        finally:
            try:
                await self._release_helper_run(result)
            finally:
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
        if self._dedicated_backend() and not self.settings.dedicated_enabled:
            return self._result("failed", "browser_dedicated_disabled")
        if self.bridge is None:
            from .pc_bridge_service import resolve_control_bridge
            try:
                self.bridge = await resolve_control_bridge()
            except Exception as exc:
                return self._result("failed", str(exc))
            self.operation_lock = getattr(self.bridge, "operation_lock", _BROWSER_WORK)
        if not self.operation_lock.acquire(blocking=False):
            return self._result("failed", "edge_browser_busy")
        result = None
        try:
            async with asyncio.timeout(self.settings.timeout_seconds) as timeout_context:
                self._timeout_context = timeout_context
                target = await self._resolve_target(tab_id=tab_id, url=plan.start_url)
                self.tab_id = target["tab_id"]
                self._browser_context = target.get("browser_context")
                self._checkpoint_cursor = 0
                self._checkpoint_data = {"goal": plan.goal, "start_url": plan.start_url, "plan": plan.model_dump(),
                    "completion_text": plan.completion_text, "completion_url": plan.completion_url}
                result = await self._execute(plan)
                return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._result(
                "failed",
                "browser_timeout" if isinstance(exc, TimeoutError) else str(exc),
            )
        finally:
            try:
                await self._release_helper_run(result)
            finally:
                self.operation_lock.release()
