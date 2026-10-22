"""Skill-owned safety adapter; the existing browser remains the executor.

No site scripts, browser lifecycle, provider fallback, or Bridge replay live here.
Only learned preparation controls may run without a fresh material approval.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from ..skills.routine import RoutineDefinition, assert_public_inputs, digest, public_url
from ..skills.routine_facts import completion_statement, labels_for, normalized, verified_facts
from .browser_agent_service import BrowserAgentService
from .human_interaction import HumanInteractionKind, get_human_interaction_manager


class SkillBrowserStop(RuntimeError):
    pass


def material_control(label: str) -> bool:
    # A taught preparation label cannot authorize an agreement or an obvious
    # final write. Unmatched material controls stop for the person to inspect.
    return bool(re.search(
        r"確定|予約する|予約を申し込|同意|契約|購入|決済|支払う|注文する|注文を|送信|削除|"
        r"\b(confirm|submit|book|reserve|purchase|buy|pay|agree|consent|delete)\b", label, re.I))


def credential_screen(observation: dict[str, Any]) -> bool:
    markers = ("password", "パスワード", "one-time-code", "one_time_code", "otp", "認証コード",
               "verification code", "verification_code", "ワンタイム", "確認コード", "暗証番号")
    for candidate in observation.get("candidates", {}).values():
        marker = normalized(" ".join(str(candidate.get(k) or "") for k in
                                     ("type", "autocomplete", "label", "name")))
        marker = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", marker).casefold()
        if any(k in marker for k in markers):
            return True
        if candidate.get("href") and not public_url(str(candidate["href"])):
            return True
    # read observations can omit all actionable controls. Visible credential
    # labels still force handoff before any page text is offered to a model.
    text = normalized(observation.get("text")).casefold()
    if any(k in text for k in markers if k != "otp") or re.search(r"\botp\b", text):
        return True
    for text_value in [observation.get("text", ""), *(c.get("context", "") for c in observation.get("candidates", {}).values())]:
        if any(not public_url(url) for url in re.findall(r"https?://[^\s<>\"']+", str(text_value))):
            return True
    if observation.get("url") and not public_url(str(observation["url"])):
        return True
    return observation.get("requires_user_login") is True


def objective_evidence(spec: RoutineDefinition, observed: dict[str, Any],
                       approval: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if credential_screen(observed):
        return None
    if observed.get("coverage", {}).get("truncated"):
        return None
    text = normalized(observed.get("text"))
    if not completion_statement(str(observed.get("text") or ""), spec.completion_text,
                                labels_for(spec.result_fields, spec.result_labels)):
        return None
    if spec.completion_url and observed.get("url", "").rstrip("/") != spec.completion_url.rstrip("/"):
        return None
    proof = verified_facts(text, spec.result_fields, spec.result_labels)
    if proof is None:
        return None
    if spec.intent == "commit":
        approved_facts = (approval or {}).get("facts")
        if (not approved_facts or digest(approved_facts) != (approval or {}).get("facts_digest") or
                proof["facts"] != approved_facts):
            return None
    # Only verified public facts and hashes; never save page/DOM/input values.
    return {"kind": "completion_page", "url": observed.get("url", ""),
            "completion_text": spec.completion_text, **proof, "facts_digest": digest(proof["facts"]),
            "observation_digest": digest({"url": observed.get("url"), "text": text}),
            "observed_at": observed.get("observed_at")}


async def request_material_approval(*, summary: str, arguments: dict[str, Any],
                                    context: dict[str, Any], user_id: str,
                                    session_id: str, agent_run_id: str = "",
                                    tool_name: str = "run_skill") -> bool | None:
    """Reuse the full existing WebUI plan preview and digest response contract."""
    from ..llm.planning_policy import ApprovedPlan, build_material_action_preview, plan_binding_for

    manager = get_human_interaction_manager()
    if manager is None:
        return None
    plan = ApprovedPlan(plan_id=digest(context), revision=1, objective=summary, raw_text=summary,
                        actions=({"tool": tool_name, "args": arguments},),
                        context_selection=context)
    binding = plan_binding_for(plan)
    result = await manager.request_interaction(
        kind=HumanInteractionKind.PLAN_APPROVAL,
        payload={"plan_text": summary, "summary": summary,
                 "actions": ["approve", "feedback", "cancel"], "plan_revision": 1,
                 "plan_binding": binding, "plan_digest": binding["plan_digest"],
                 "action_digest": binding["action_digest"],
                 "material_action_preview": build_material_action_preview(plan.actions),
                 "material_action_preview_digest": binding["material_action_preview_digest"]},
        user_id=user_id, session_id=session_id, agent_run_id=agent_run_id, revision=1,
    )
    if result is None:
        return None
    return (result.get("action") == "approve" and result.get("revision") == 1 and
            result.get("plan_binding") == binding and
            result.get("action_digest") == binding["action_digest"] and
            result.get("material_action_preview_digest") == binding["material_action_preview_digest"])


class SkillBrowserBridge:
    """Fence and approve a material action immediately before Bridge dispatch."""

    def __init__(self, bridge: Any, *, spec: RoutineDefinition, record: Any,
                 save: Any, gate: Any, agent_run_id: str = "", reconcile: bool = False,
                 assert_owner: Any = None):
        self.bridge = bridge
        self.spec = spec
        self.record = record
        self.save = save
        self.gate = gate
        self.agent_run_id = agent_run_id
        self.reconcile = reconcile
        self.assert_owner = assert_owner
        self.latest: dict[str, Any] = {}
        self.observation_generation = 0
        self.commit_generation = 0
        self.stop_reason = ""
        self._material_states: dict[str, Any] = {}
        self.browser_backend = getattr(bridge, "browser_backend", "extension")
        self.operation_lock = getattr(bridge, "operation_lock", None)
        # The existing login controller validates the same owner/connection
        # when offering the ordinary-browser handoff.
        for name in ("owner", "device_id", "hub", "_connection"):
            setattr(self, name, getattr(bridge, name, None))

    def _stop(self, reason: str):
        self.stop_reason = reason
        raise SkillBrowserStop(reason)

    def _observe(self, observation):
        if self.browser_backend == "owned_profile" and observation.get("authentication_required") is True and not self.reconcile:
            self._material_states = {}
            self.latest = {"authentication_required": True, "candidates": {}}
            self.stop_reason = "routine_login_handoff"
            self.observation_generation += 1
            return self.latest
        if credential_screen(observation):
            # The raw response remains inside this boundary, never in planner/trace/record.
            self._stop("routine_login_handoff")
        if self.stop_reason == "routine_login_handoff":
            self.stop_reason = ""
        # Actual form values are a private transport contract. They are never
        # offered to a model, even when a backend erroneously includes tokens.
        self._material_states = observation.get("material_states") or {}
        safe = {k: observation[k] for k in
                ("url", "text", "tab_id", "document_id", "coverage", "observed_at", "error") if k in observation}
        safe["candidates"] = {
            key: {k: v for k, v in item.items() if k in
                  {"stable_id", "document_id", "frame_id", "tag", "role", "type", "label", "context",
                   "heading", "href", "name", "autocomplete", "disabled", "actionable", "enabled",
                   "options", "option_count", "options_truncated"}}
            for key, item in observation.get("candidates", {}).items()
        }
        for item in safe["candidates"].values():
            item["document_id"] = item.get("document_id") or observation.get("document_id")
        self.latest = safe
        self.observation_generation += 1
        return safe

    def _material_state(self, target, candidate, params):
        if getattr(self.bridge, "supports_material_state_guard", False) is not True:
            self._stop("routine_material_state_guard_unavailable")
        state = self._material_states.get(target)
        if (not isinstance(state, dict) or state.get("version") != 1 or state.get("complete") is not True or
                state.get("tab_id") != params.get("tab_id") or not candidate.get("document_id") or
                not candidate.get("stable_id") or state.get("document_id") != candidate.get("document_id") or
                state.get("target_stable_id") != candidate.get("stable_id") or
                set(state) != {"version", "complete", "tab_id", "document_id", "target_stable_id", "controls", "url", "facts_text"} or
                not isinstance(state.get("url"), str) or not isinstance(state.get("facts_text"), str) or
                len(state["facts_text"]) > 8192 or
                not isinstance(state.get("controls"), list) or len(state["controls"]) > 32):
            self._stop("routine_material_state_unverified")
        if credential_screen({"url": state["url"], "text": state["facts_text"]}):
            self._stop("routine_login_handoff")
        if (state["url"] != self.latest.get("url") or
                verified_facts(state["facts_text"], self.spec.confirmation, self.spec.confirmation_labels) !=
                verified_facts(self.latest.get("text", ""), self.spec.confirmation, self.spec.confirmation_labels)):
            self._stop("routine_material_state_changed")
        actual = {}
        stable_ids = set()
        for control in state["controls"]:
            if (not isinstance(control, dict) or set(control) != {"stable_id", "label", "type", "value", "display_value"} or
                    any(not isinstance(v, str) or len(v) > 1200 for v in control.values()) or
                    control["type"] not in {"text", "date", "time", "select", "radio", "checkbox", "account"} or
                    not control["stable_id"] or control["stable_id"] in stable_ids):
                self._stop("routine_material_state_unverified")
            stable_ids.add(control["stable_id"])
            label = normalized(control["label"])
            if label in actual or not label:
                self._stop("routine_material_state_ambiguous")
            try:
                assert_public_inputs({label: [control["value"], control["display_value"]]})
            except ValueError:
                self._stop("routine_login_handoff")
            actual[label] = normalized(control["display_value"])
        if actual != {normalized(k): normalized(v) for k, v in self.spec.form_values.items()}:
            self._stop("routine_material_state_changed")
        # Stable ordering is part of the Bridge atomic compare contract.
        return {**state, "controls": sorted(state["controls"], key=lambda c: c["stable_id"])}, actual

    @staticmethod
    def _binding(observed, candidate, params, *, facts, state):
        return {"url": observed.get("url"), "text_digest": digest(normalized(observed.get("text"))),
                "tab_id": params.get("tab_id"), "action": params.get("action"),
                "value_digest": digest(params.get("value", "")),
                "facts_digest": digest(facts), "material_state_digest": digest(state),
                "candidate": {k: candidate.get(k) for k in
                              ("stable_id", "document_id", "frame_id", "tag", "role", "type", "label", "context", "heading", "href")}}

    async def request(self, command, **params):
        # Release the exact owned run even if capabilities changed while waiting.
        # Transport ownership and its run/connection checks still apply.
        if command != "end_run":
            self.gate()
        if self.assert_owner is not None:
            await self.assert_owner()
        if command != "end_run":
            self.gate()
        if self.reconcile and command not in {"resolve_tab", "observe", "verify", "end_run"}:
            self._stop("routine_reconcile_read_only")
        if command == "resolve_tab":
            result = await self._checked(await self.bridge.request(command, **params))
            context = dict(self.record.context)
            context["tab_id"] = result.get("tab_id")
            if self.browser_backend == "owned_profile" and params.get("run_id"):
                context["browser_run_id"] = params["run_id"]
            self.record.context = context
            await self.save(self.record)
            if result.get("url") and not public_url(str(result["url"])):
                self._stop("routine_login_handoff")
            return result
        if command == "observe":
            return self._observe(await self._checked(await self.bridge.request(command, **params), allow_login_handoff=True))
        if command == "act":
            handoff = await self._before_action(params)
            if handoff is not None:
                return handoff
            try:
                self.gate()
                if self.assert_owner is not None:
                    await self.assert_owner()
                # assert_owner awaits the DB. Recheck immediately before calling
                # the transport; no intervening await may admit a disabled act.
                self.gate()
            except BaseException:
                if self.record.commit_dispatched:
                    await self._clear_undispatched_commit()
                raise
        transport_params = dict(params)
        if command == "act":
            from .pc_bridge_service import RemoteControlBridge
            if isinstance(self.bridge, RemoteControlBridge):
                # The relay may await its command queue and gate notification.
                # Carry authority to its actual socket dispatch boundary.
                async def preflight():
                    if self.assert_owner is not None:
                        await self.assert_owner()
                    self.gate()
                transport_params.update(dispatch_preflight=preflight, dispatch_gate=self.gate)
        result = await self._checked(await self.bridge.request(command, **transport_params),
                                     allow_login_handoff=command in {"act", "pause_login", "auth_status", "resume_login", "cancel_login"})
        if command == "act" and self.record.commit_dispatched and result.get("executed") is False:
            await self._clear_undispatched_commit()
            if result.get("authentication_required") is True:
                self.stop_reason = "routine_login_handoff"
                return result  # Existing login controller reobserves and approves afresh.
            # A definite rejection is safe to retry only through a new explicit resume/approval.
            self._stop("routine_commit_not_executed")
        if command == "act" and result.get("authentication_required") is True and result.get("executed") is False:
            self.stop_reason = "routine_login_handoff"
            return result
        if (command == "end_run" and self.browser_backend == "owned_profile" and
                result.get("ended") is True and result.get("refs_invalidated") is True):
            context = dict(self.record.context)
            if result.get("paused") is True:
                context["browser_run_paused"] = True
            else:
                context.pop("browser_run_id", None)
                context.pop("browser_run_paused", None)
            self.record.context = context
            await self.save(self.record)
        if command == "cancel_login" and result.get("cancelled") is True and self.record.context.get("browser_run_id"):
            context = dict(self.record.context)
            context.pop("browser_run_id", None)
            context.pop("browser_run_paused", None)
            self.record.context = context
            await self.save(self.record)
        if command in {"act", "navigate", "back", "scroll"} and result.get("ok") is not False:
            self.record.steps_completed += 1
            await self.save(self.record)
        return result

    async def _clear_undispatched_commit(self):
        # Only called before transport invocation or with explicit executed=False.
        # Failed persistence remains conservative: never permit an uncertain replay.
        evidence = self.record.evidence
        self.record.commit_dispatched = False
        self.record.evidence = {}
        try:
            await self.save(self.record)
        except BaseException:
            self.record.commit_dispatched = True
            self.record.evidence = evidence
            raise

    async def _checked(self, result, *, allow_login_handoff=False):
        # Browser ownership includes dedicated-browser selection and login idle
        # timers. Consume its state, retaining this execution ID and write fence.
        status = result.get("status")
        if status in {"needs_user", "interrupted", "unknown"}:
            handoff = result.get("handoff") or {}
            context = dict(self.record.context)
            context["bridge_handoff"] = {key: handoff[key] for key in
                                         ("state", "browser_backend", "next_browser_backend", "handoff_id")
                                         if isinstance(handoff.get(key), (str, int, bool))}
            self.record.context = context
            await self.save(self.record)
            self._stop("routine_bridge_" + status)
        if (allow_login_handoff and self.browser_backend == "owned_profile" and not self.reconcile and
                result.get("authentication_required") is True):
            # Let the existing controller suspend the run. Credential page
            # content and URLs never enter planning, traces or durable state.
            safe = {key: result[key] for key in
                    ("authentication_required", "document_visible", "tab_id", "document_id", "activity_count",
                     "paused", "resumed", "cancelled", "executed") if isinstance(result.get(key), (str, int, bool))}
            context = result.get("browser_context")
            if isinstance(context, dict):
                safe["browser_context"] = {key: context[key] for key in ("context_id", "context_epoch")
                                           if isinstance(context.get(key), str)}
            return safe
        if credential_screen(result):
            self._stop("routine_login_handoff")
        return result

    async def _before_action(self, params):
        if self.record.commit_dispatched:
            self._stop("routine_commit_already_dispatched")
        candidate = self.latest.get("candidates", {}).get(params.get("target"))
        if not candidate:
            self._stop("routine_target_unverified")
        label = normalized(candidate.get("label"))
        action = params.get("action")
        preparation = {normalized(v) for v in self.spec.preparation_labels}
        if action in {"type", "select"} and label in preparation and label != normalized(self.spec.commit_label):
            return  # Only taught inputs; Browser type never appends Enter/submit.
        is_commit = action == "click" and label == normalized(self.spec.commit_label)
        if action == "click" and not is_commit and label in preparation and not material_control(label):
            return
        if self.spec.intent != "commit" or action != "click" or label != normalized(self.spec.commit_label):
            self._stop("routine_unlearned_material_action")
        proof = verified_facts(self.latest.get("text", ""), self.spec.confirmation, self.spec.confirmation_labels)
        scoped = verified_facts(candidate.get("context", ""), self.spec.confirmation, self.spec.confirmation_labels)
        if self.latest.get("coverage", {}).get("truncated") or proof is None or scoped != proof:
            self._stop("routine_confirmation_facts_missing")
        state, actual_form = self._material_state(params["target"], candidate, params)
        context = self._binding(self.latest, candidate, params, facts=proof["facts"], state=state)
        context.update(request_id=self.record.request_id, skill_hash=self.record.skill_hash,
                       device_id=self.record.context.get("device_id"))
        arguments = {"request_id": self.record.request_id, "skill_name": self.record.skill_name,
                     "action": action, "control": label, **proof["fields"], "form_values": actual_form}
        approved = await request_material_approval(
            summary="この内容で確定します: " + " / ".join(proof["fields"].values()) +
                    (" / 入力: " + " / ".join(k + "=" + v for k, v in actual_form.items()) if actual_form else ""),
            arguments=arguments, context=context,
            user_id=self.record.user_id, session_id=self.record.session_id,
            agent_run_id=self.agent_run_id,
        )
        if approved is not True:
            self._stop("routine_approval_timeout" if approved is None else "routine_approval_rejected")
        self.gate()
        if self.assert_owner is not None:
            await self.assert_owner()
        self.gate()
        observe_params = {"tab_id": params["tab_id"], "action": action}
        if "run_id" in params:
            observe_params["run_id"] = params["run_id"]
        observation = await self._checked(await self.bridge.request("observe", **observe_params), allow_login_handoff=True)
        fresh = self._observe(observation)
        if fresh.get("authentication_required") is True:
            # The old approval cannot authorize a new authenticated document.
            # Report proven non-dispatch to the existing BrowserAgent login loop.
            self.record.evidence = {}
            await self.save(self.record)
            return {**observation, "executed": False}
        fresh_proof = verified_facts(fresh.get("text", ""), self.spec.confirmation, self.spec.confirmation_labels)
        if fresh.get("coverage", {}).get("truncated") or fresh_proof != proof:
            self._stop("routine_approval_content_changed")
        matches = []
        for key, value in fresh.get("candidates", {}).items():
            if value.get("stable_id") != candidate.get("stable_id"):
                continue
            fresh_state, _ = self._material_state(key, value, params)
            if (verified_facts(value.get("context", ""), self.spec.confirmation, self.spec.confirmation_labels) == proof and
                    self._binding(fresh, value, params, facts=proof["facts"], state=fresh_state) ==
                    {k: v for k, v in context.items() if k not in {"request_id", "skill_hash", "device_id"}}):
                matches.append((key, fresh_state))
        if len(matches) != 1:
            self._stop("routine_approval_content_changed")
        params["target"] = matches[0][0]
        params["expected_material_state"] = matches[0][1]
        params["expected_material_state_digest"] = digest(matches[0][1])
        self.record.evidence = {"approval_digest": digest(context), "confirmation": proof["fields"],
                                "facts": proof["facts"], "facts_digest": digest(proof["facts"]),
                                "material_state_digest": digest(state)}
        self.record.commit_dispatched = True
        self.commit_generation = self.observation_generation
        # Durable fence BEFORE send. Crash/timeout after here can only reconcile, never replay.
        await self.save(self.record)


class SkillBrowserAgent(BrowserAgentService):
    async def _confirm_final(self, step, candidate, *, target=None):
        if isinstance(self.bridge, SkillBrowserBridge):
            # This adapter owns the bound material approval and durable fence
            # immediately before act. Its private form snapshot must not be
            # copied into the general browser's observation/planning state.
            return {}
        return await super()._confirm_final(step, candidate, target=target)

    async def _switch_to_ordinary(self):
        adapter = self.bridge
        await super()._switch_to_ordinary()
        ordinary = self.bridge
        if isinstance(adapter, SkillBrowserBridge):
            # The generic controller changes its transport after account
            # confirmation. Keep the Skill fence around that new transport.
            self.bridge = adapter
            adapter.bridge = ordinary
            adapter.browser_backend = ordinary.browser_backend
            adapter.operation_lock = ordinary.operation_lock
            for name in ("owner", "device_id", "hub", "_connection"):
                setattr(adapter, name, getattr(ordinary, name, None))
            adapter.record.context = {**adapter.record.context, "browser_backend": ordinary.browser_backend,
                                      "tab_id": self.tab_id}
            await adapter.save(adapter.record)

    async def _execute_plan(self, plan, *, adaptive=False):
        result = await super()._execute_plan(plan, adaptive=adaptive)
        # The helper verify boolean is insufficient business evidence. Observe while run refs live.
        await self._observe()
        return result
