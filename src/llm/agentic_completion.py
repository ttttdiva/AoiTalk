"""Provider-independent agentic completion review loop."""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Optional

from .generation_policy import GenerationProfile, get_client_generation_policy
from .tool_policy import project_progress_review_active

AsyncStreamCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
AsyncRunOnce = Callable[[str], Awaitable[str]]
SyncRunOnce = Callable[[str], str]
SyncEventCallback = Callable[[str, dict[str, Any]], None]

DEFAULT_AGENTIC_MAX_ROUNDS = 2
DEFAULT_WORK_MAX_ROUNDS = 120
DEFAULT_PROJECT_PROGRESS_MAX_ROUNDS = 120
AGENTIC_MAX_ROUNDS_CAP = 1000
WORK_GENERATION_PROFILES = {
    GenerationProfile.ASSISTED_WORK,
    GenerationProfile.AUTONOMOUS_WORK,
    GenerationProfile.REVIEW,
}

WORK_REQUEST_TERMS = (
    "更新",
    "修正",
    "作成",
    "登録",
    "整理",
    "完成",
    "反映",
    "確認して",
    "調べて",
    "見て",
    "直して",
    "update",
    "fix",
    "create",
    "register",
    "organize",
    "verify",
    "inspect",
)

INCOMPLETE_RESPONSE_PATTERNS = (
    "まず",
    "これから",
    "次に",
    "確認する",
    "確認します",
    "調査する",
    "調査します",
    "実行する",
    "実行します",
    "更新が必要",
    "必要があります",
    "必要です",
    "will ",
    "i will",
    "let me",
    "next,",
)

COMPLETION_EVIDENCE_TERMS = (
    "確認しました",
    "調査しました",
    "更新しました",
    "登録しました",
    "作成しました",
    "反映しました",
    "検証しました",
    "完了",
    "結果を確認しました",
    "tool result",
    "verified",
    "updated",
    "created",
    "registered",
)

UNVERIFIED_TOOL_FAILURE_TERMS = (
    "ツール実行の検証に失敗しました",
    "作業が完了していません",
    "完了していません",
    "必須ツール",
    "required tool",
)


def render_messages_for_review(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role") or "message")
        content = message.get("content", "")
        if isinstance(content, list):
            content_text = "\n".join(str(part) for part in content)
        else:
            content_text = str(content)
        lines.append(f"{role}:\n{content_text}")
    return "\n\n".join(lines)


def agentic_completion_enabled(client: object, user_input: str | None = None) -> bool:
    if user_input and project_progress_review_active(user_input):
        return True
    return get_client_generation_policy(client).agentic_completion_enabled


def _config_get(config: object, key: str) -> Any:
    if config is None:
        return None
    if hasattr(config, "get"):
        try:
            value = config.get(key, None)  # type: ignore[call-arg]
        except TypeError:
            value = config.get(key)  # type: ignore[call-arg]
        if value is not None:
            return value
    if isinstance(config, dict):
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current
    return None


def _config_int(config: object, key: str, default: int) -> int:
    raw_value = _config_get(config, key)
    try:
        return int(raw_value) if raw_value is not None else default
    except (TypeError, ValueError):
        return default


def agentic_max_rounds(client: object, user_input: str | None = None) -> int:
    config = getattr(client, "config", None)
    value = _config_int(
        config,
        "agentic_completion.max_rounds",
        DEFAULT_AGENTIC_MAX_ROUNDS,
    )
    policy = get_client_generation_policy(client)
    if policy.profile in WORK_GENERATION_PROFILES:
        profile_key = policy.profile.value
        value = max(
            value,
            _config_int(
                config,
                "agentic_completion.work_max_rounds",
                DEFAULT_WORK_MAX_ROUNDS,
            ),
            _config_int(
                config,
                f"agentic_completion.{profile_key}_max_rounds",
                DEFAULT_WORK_MAX_ROUNDS,
            ),
        )
    if user_input and project_progress_review_active(user_input):
        value = max(
            value,
            _config_int(
                config,
                "agentic_completion.project_progress_max_rounds",
                DEFAULT_PROJECT_PROGRESS_MAX_ROUNDS,
            ),
        )
    return max(0, min(value, AGENTIC_MAX_ROUNDS_CAP))


def response_looks_like_unfinished_work(
    user_input: str | None,
    response: str | None,
) -> bool:
    """Detect plan-only output that must not be treated as completed work."""
    text = str(response or "").strip()
    if not text:
        return True
    lowered = text.casefold()
    if any(term.casefold() in lowered for term in UNVERIFIED_TOOL_FAILURE_TERMS):
        return True

    request = str(user_input or "").casefold()
    if request and not any(term.casefold() in request for term in WORK_REQUEST_TERMS):
        return False
    if any(term.casefold() in lowered for term in COMPLETION_EVIDENCE_TERMS):
        return False
    return any(term.casefold() in lowered for term in INCOMPLETE_RESPONSE_PATTERNS)


def parse_agentic_review_decision(content: str) -> dict[str, str]:
    text = str(content or "").strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {"status": "done", "reason": "review did not return JSON"}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"status": "done", "reason": "review JSON parse failed"}
    if not isinstance(payload, dict):
        return {"status": "done", "reason": "review payload was not an object"}
    status = str(payload.get("status") or "done").strip().lower()
    if status not in {"done", "continue"}:
        status = "done"
    return {
        "status": status,
        "reason": str(payload.get("reason") or "").strip(),
        "next_request": str(payload.get("next_request") or "").strip(),
    }


def unfinished_work_decision(user_input: str | None, response: str) -> dict[str, str]:
    return {
        "status": "continue",
        "reason": "latest response describes planned work or failed tool verification",
        "next_request": (
            "Continue the original request now. Use the necessary tools, verify "
            "the resulting external state, and do not answer with only a plan."
        ),
    }


def build_incomplete_work_failure_response(
    *,
    user_input: str | None,
    latest_response: str,
) -> str:
    request = str(user_input or "").strip()
    prefix = (
        "作業が完了していません。"
        "必要なツール実行または検証が完了しなかったため、成功として扱えません。"
    )
    if request:
        prefix = f"{prefix}\n対象依頼: {request}"
    if latest_response:
        prefix = f"{prefix}\n最後の応答: {latest_response}"
    return prefix


def build_agentic_review_prompt(
    *,
    original_context: str,
    latest_response: str,
    round_index: int,
    user_input: str | None = None,
) -> str:
    lines = [
        "You are the completion verifier for this AoiTalk agent run.",
        "Review whether the user's request has actually been completed.",
        "Decide from the original request, available tool hints, confirmed tool results in the context, and the latest assistant response.",
        "If the request needed current facts, external state, files, project records, or utility/tool-backed information, do not mark done unless the response is grounded in confirmed tool results or direct evidence.",
        "If the response says a tool can check something, but does not provide the requested result, request one more focused continuation step.",
        "If the request produced or changed files, artifacts, tasks, records, or other external state, inspect the result with the available specialist tools before deciding.",
        "Examples: for an Excel file, verify that the file exists and that its workbook content matches the requested sheets, columns, rows, and dates; for code changes, inspect diffs or run narrow checks if available.",
        "If verification fails or important work remains, request one more focused continuation step.",
        "Do not ask the user to do verification that the agent can do with tools.",
    ]
    if user_input and project_progress_review_active(user_input):
        lines.extend(
            [
                "For project progress review, do not mark done unless the run checked current project evidence with project tools, and if project Docs/record tables were updated, progress was checked again after that update.",
                "Continue when the answer is based only on a first shallow result, when stored evidence is insufficient and project files have not been inspected/refreshed, or when needed external current facts have not been searched.",
            ]
        )
    lines.extend(
        [
            "Return exactly one JSON object and no markdown:",
            '{"status":"done","reason":"..."}',
            '{"status":"continue","reason":"...","next_request":"..."}',
            "",
            f"Review round: {round_index}",
            "",
            "Original conversation context:",
            original_context,
            "",
            "Latest assistant response:",
            latest_response,
        ]
    )
    return "\n".join(lines)


def build_agentic_continuation_context(
    *,
    original_context: str,
    latest_response: str,
    decision: dict[str, str],
) -> str:
    next_request = decision.get("next_request") or (
        "Continue the work needed to satisfy the original request."
    )
    return "\n".join(
        [
            "Continue this AoiTalk agent run because verification found unfinished or invalid work.",
            "Use specialist tools as needed, then produce a corrected final response.",
            "",
            "Original conversation context:",
            original_context,
            "",
            "Previous assistant response:",
            latest_response,
            "",
            "Verification result:",
            decision.get("reason", ""),
            "",
            "Required continuation:",
            next_request,
        ]
    )


async def run_agentic_completion_loop_async(
    *,
    client: object,
    run_once: AsyncRunOnce,
    context: str,
    stream_callback: Optional[AsyncStreamCallback] = None,
    user_input: str | None = None,
    initial_response: str | None = None,
) -> str:
    if not agentic_completion_enabled(client, user_input):
        if initial_response is not None:
            return initial_response
        return await run_once(context)

    if stream_callback:
        await stream_callback(
            "stream_start",
            {"status": "agentic", "message": "作業を実行しています"},
        )

    response = initial_response if initial_response is not None else await run_once(context)
    max_rounds = agentic_max_rounds(client, user_input)
    for round_index in range(1, max_rounds + 1):
        if stream_callback:
            await stream_callback(
                "status_update",
                {
                    "status": "agentic_review",
                    "message": "結果を検証しています",
                },
            )

        review_prompt = build_agentic_review_prompt(
            original_context=context,
            latest_response=response,
            round_index=round_index,
            user_input=user_input,
        )
        review_response = await run_once(review_prompt)
        decision = parse_agentic_review_decision(str(review_response or ""))
        if decision["status"] != "continue" and response_looks_like_unfinished_work(
            user_input,
            response,
        ):
            decision = unfinished_work_decision(user_input, response)
        if stream_callback:
            await stream_callback(
                "agentic_review",
                {
                    "round": round_index,
                    "status": decision["status"],
                    "reason": decision.get("reason", ""),
                    "next_request": decision.get("next_request", ""),
                    "review_response": str(review_response or ""),
                },
            )
        if decision["status"] != "continue":
            break

        if stream_callback:
            await stream_callback(
                "status_update",
                {
                    "status": "agentic_continue",
                    "message": "不足分を再実行しています",
                },
            )

        continuation_context = build_agentic_continuation_context(
            original_context=context,
            latest_response=response,
            decision=decision,
        )
        response = await run_once(continuation_context)
    else:
        if response_looks_like_unfinished_work(user_input, response):
            response = build_incomplete_work_failure_response(
                user_input=user_input,
                latest_response=response,
            )

    if max_rounds == 0 and response_looks_like_unfinished_work(user_input, response):
        response = build_incomplete_work_failure_response(
            user_input=user_input,
            latest_response=response,
        )

    if stream_callback:
        await stream_callback("stream_end", {"content": response})

    return response


def run_agentic_completion_loop_sync(
    *,
    client: object,
    run_once: SyncRunOnce,
    context: str,
    user_input: str | None = None,
    initial_response: str | None = None,
    event_callback: Optional[SyncEventCallback] = None,
) -> str:
    if not agentic_completion_enabled(client, user_input):
        if initial_response is not None:
            return initial_response
        return run_once(context)

    response = initial_response if initial_response is not None else run_once(context)
    max_rounds = agentic_max_rounds(client, user_input)
    for round_index in range(1, max_rounds + 1):
        review_prompt = build_agentic_review_prompt(
            original_context=context,
            latest_response=response,
            round_index=round_index,
            user_input=user_input,
        )
        review_response = run_once(review_prompt)
        decision = parse_agentic_review_decision(str(review_response or ""))
        if decision["status"] != "continue" and response_looks_like_unfinished_work(
            user_input,
            response,
        ):
            decision = unfinished_work_decision(user_input, response)
        if event_callback:
            event_callback(
                "agentic_review",
                {
                    "round": round_index,
                    "status": decision["status"],
                    "reason": decision.get("reason", ""),
                    "next_request": decision.get("next_request", ""),
                    "review_response": str(review_response or ""),
                },
            )
        if decision["status"] != "continue":
            break

        continuation_context = build_agentic_continuation_context(
            original_context=context,
            latest_response=response,
            decision=decision,
        )
        response = run_once(continuation_context)
    else:
        if response_looks_like_unfinished_work(user_input, response):
            response = build_incomplete_work_failure_response(
                user_input=user_input,
                latest_response=response,
            )

    if max_rounds == 0 and response_looks_like_unfinished_work(user_input, response):
        response = build_incomplete_work_failure_response(
            user_input=user_input,
            latest_response=response,
        )

    return response
