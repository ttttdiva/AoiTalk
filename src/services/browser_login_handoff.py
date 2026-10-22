"""Login waits carry no field values and never authorize a final action."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .browser_agent_models import BrowserAgentError

CONTINUE = "専用ブラウザで続ける"
SWITCH = "普段のブラウザへ切り替える"
RESUME = "ログイン完了・再開"
CANCEL = "作業を中止"


@dataclass
class LoginWaitState:
    last_activity: float
    activity: tuple | None = None
    choice_started: float | None = None
    phase: str = "login"

    def update(self, now, status):
        if self.phase == "final_confirmation":
            return "wait"
        marker = (status.get("document_id"), status.get("activity_count", 0))
        if self.activity is not None and marker != self.activity:
            self.last_activity, self.choice_started = now, None
        self.activity = marker
        if self.choice_started is not None:
            return "switch" if now - self.choice_started >= 10 else "wait"
        if now - self.last_activity >= 120:
            self.choice_started = now
            return "choose"
        return "wait"

    def continue_dedicated(self, now):
        self.last_activity, self.choice_started = now, None


async def wait_for_login(service, *, ask=None, clock=time.monotonic, poll_seconds=0.5):
    """Keep the live task suspended; all mutable commands remain driver-blocked."""
    status = await service.bridge.request("pause_login", run_id=service.run_id, tab_id=service.tab_id)
    context = status["browser_context"]
    state = LoginWaitState(clock())
    service._event("login_handoff", device_id=getattr(service.bridge, "device_id", None), tab_id=service.tab_id)
    await service._save_checkpoint("awaiting_login")
    if ask is None:
        from .human_interaction import get_human_interaction_manager, HumanInteractionKind
        from .turn_context import get_turn_context
        from .agent_run_service import get_current_agent_run_id
        manager, turn = get_human_interaction_manager(), get_turn_context()
        if manager is None:
            raise BrowserAgentError("browser_login_user_interface_unavailable")

        async def ask(question, choices):
            return await manager.request_interaction(
                kind=HumanInteractionKind.ASK_USER_QUESTION,
                payload={"question": question, "input_type": "single_choice", "choices": choices,
                         "allow_multiple": False, "allow_free_text": False},
                agent_run_id=get_current_agent_run_id() or "", user_id=turn.user_id or "",
                session_id=turn.session_id or "", timeout_seconds=3600)

    pc = getattr(service.bridge, "device_id", "PC接続で選択したPC")
    message = f"PC {pc} の専用ブラウザを表示しました。この画面でログインだけお願いします。認証情報はこのチャットへ入力しないでください。"
    question = asyncio.create_task(ask(message, [RESUME, CANCEL]))
    completed_polls = 0
    try:
        while True:
            status = await service.bridge.request("auth_status", run_id=service.run_id, tab_id=service.tab_id)
            if status.get("browser_context") != context or status.get("tab_id") != service.tab_id:
                raise BrowserAgentError("browser_login_context_changed")
            if status.get("document_visible") is not True:
                raise BrowserAgentError("browser_login_target_changed")
            if question.done():
                response = question.result()
                selected = (response or {}).get("selected_choices", []) if isinstance(response, dict) else []
                answer = selected[0] if len(selected) == 1 else None
                if not isinstance(response, dict) or response.get("cancelled") or answer == CANCEL:
                    await service.bridge.request("cancel_login", run_id=service.run_id, tab_id=service.tab_id)
                    raise BrowserAgentError("browser_login_cancelled")
                if answer == SWITCH:
                    return "switch"
                if answer == CONTINUE:
                    state.continue_dedicated(clock())
                elif answer == RESUME and not status.get("authentication_required"):
                    break
                question = asyncio.create_task(ask(message, [RESUME, CANCEL]))
            # Two structural observations avoid resuming during an incomplete
            # navigation. No credential fields or screenshot enter the model.
            completed_polls = completed_polls + 1 if not status.get("authentication_required") else 0
            if completed_polls >= 2:
                break
            transition = state.update(clock(), status)
            if transition == "choose":
                question.cancel()
                await asyncio.gather(question, return_exceptions=True)
                question = asyncio.create_task(ask("ログイン待ちです。続けるブラウザを選んでください。", [CONTINUE, SWITCH]))
            elif transition == "switch":
                return "switch"
            elif state.choice_started is None and state.phase == "choice":
                question.cancel()
                await asyncio.gather(question, return_exceptions=True)
                question = asyncio.create_task(ask(message, [RESUME, CANCEL]))
            state.phase = "choice" if state.choice_started is not None else "login"
            await asyncio.sleep(poll_seconds)
        resumed = await service.bridge.request("resume_login", run_id=service.run_id, tab_id=service.tab_id)
        if resumed.get("resumed") is not True or resumed.get("browser_context") != context:
            raise BrowserAgentError("browser_login_resume_unverified")
        service._event("login_resumed", tab_id=service.tab_id)
        return "resume"
    finally:
        question.cancel()
        await asyncio.gather(question, return_exceptions=True)
