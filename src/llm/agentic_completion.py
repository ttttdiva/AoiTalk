"""Provider-independent agentic completion review loop."""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Optional, Sequence

from .generation_policy import GenerationProfile, get_client_generation_policy
from .tool_policy import (
    command_capabilities_from_text,
    mutation_execution_forbidden,
    project_management_required_mutation_tools,
    project_progress_review_active,
)

AsyncStreamCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
AsyncRunOnce = Callable[[str], Awaitable[str]]
SyncRunOnce = Callable[[str], str]
SyncEventCallback = Callable[[str, dict[str, Any]], None]

DEFAULT_AGENTIC_MAX_ROUNDS = 2
DEFAULT_WORK_MAX_ROUNDS = 120
DEFAULT_PROJECT_PROGRESS_MAX_ROUNDS = 120
DEFAULT_REVIEW_MAX_ROUNDS = 2
AGENTIC_MAX_ROUNDS_CAP = 1000
WORK_GENERATION_PROFILES = {
    GenerationProfile.ASSISTED_WORK,
    GenerationProfile.AUTONOMOUS_WORK,
}

# Only a trusted command context can activate the fallback plan-only check.
# Natural words such as 「調べて」「確認して」「見て」 are ordinary user
# prose and must not force an additional provider turn.
EXPLICIT_COMPLETION_CAPABILITIES = frozenset(
    {
        "web_search",
        "image_generation",
        "work_intake",
        "workspace_file_operation",
        "project_db_update",
        "project_progress_review",
        "task_update",
        "wbs_sync",
    }
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
    "呼び出します",
    "更新が必要",
    "必要があります",
    "必要です",
    "will ",
    "i will",
    "let me",
    "will call",
    "let me call",
    "next,",
)

FUTURE_TOOL_USE_PATTERNS = (
    "呼び出します",
    "will call",
    "i will call",
    "let me call",
)

FAST_PATH_FUTURE_SELF_ACTION_REGEXES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"これから(?:追加で)?[^。]*?(?:確認|調査|実行|まとめ)(?:します|する)(?!して)",
        r"引き続き[^。]*?(?:確認|調査|実行|まとめ)(?:します|する)(?!して)",
        r"続けて[^。]*?(?:確認|調査|実行|まとめ)(?:します|する)(?!して)",
        r"次に[^。]*?(?:結果を)?まとめ(?:ます|る)(?!して)",
        r"次に[^。]*?(?:確認|調査|実行)(?:します|する)(?!して)",
    )
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

    # Review is a bounded verification pass, not a work execution profile.
    # Keep it independent from the larger work/project-progress budgets even
    # when the same client carries one of those settings or the prompt happens
    # to look like a managed-workspace/project-progress request.
    if policy.profile == GenerationProfile.REVIEW:
        review_value = _config_int(
            config,
            "agentic_completion.review_max_rounds",
            DEFAULT_REVIEW_MAX_ROUNDS,
        )
        return max(0, min(review_value, AGENTIC_MAX_ROUNDS_CAP))

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


def response_promises_future_tool_use(response: str | None) -> bool:
    lowered = str(response or "").casefold()
    if not lowered:
        return False
    return any(pattern.casefold() in lowered for pattern in FUTURE_TOOL_USE_PATTERNS)


def response_looks_like_incomplete_final_answer(response: str | None) -> bool:
    """Conservative signal used before fast-path review short-circuit."""

    return response_maybe_incomplete_for_fast_path(response)


def response_maybe_incomplete_for_fast_path(response: str | None) -> bool:
    text = str(response or "").strip()
    if not text:
        return True
    lowered = text.casefold()
    if any(term.casefold() in lowered for term in UNVERIFIED_TOOL_FAILURE_TERMS):
        return True
    if response_promises_future_tool_use(text):
        return True
    return any(pattern.search(text) for pattern in FAST_PATH_FUTURE_SELF_ACTION_REGEXES)


def _extract_tool_call_name(record: Any) -> str:
    if isinstance(record, dict):
        for key in ("tool", "name", "tool_name"):
            value = str(record.get(key) or "").strip()
            if value:
                return value
        return ""
    for attr in ("tool", "name", "tool_name"):
        if hasattr(record, attr):
            value = str(getattr(record, attr, "") or "").strip()
            if value:
                return value
    return ""


def _audit_tool_call_names(records: Sequence[Any] | None) -> set[str]:
    names: set[str] = set()
    for record in records or []:
        name = _extract_tool_call_name(record)
        if name:
            names.add(name.casefold())
    return names


def _audit_tool_call_successful(record: Any) -> bool:
    if isinstance(record, dict):
        explicit_success = record.get("successful")
        if explicit_success is None:
            explicit_success = record.get("success")
        result = (
            record.get("result")
            if record.get("result") is not None
            else record.get("output")
            if record.get("output") is not None
            else record.get("model_output")
        )
    else:
        explicit_success = getattr(record, "successful", None)
        if explicit_success is None:
            explicit_success = getattr(record, "success", None)
        result = getattr(record, "result", None)
        if result is None:
            result = getattr(record, "output", None)
        if result is None:
            result = getattr(record, "model_output", None)

    if isinstance(explicit_success, bool):
        return explicit_success
    if explicit_success is not None:
        return str(explicit_success).strip().casefold() in {
            "1",
            "true",
            "yes",
            "ok",
            "success",
            "succeeded",
        }

    if isinstance(result, dict):
        if result.get("success") is False:
            return False
        return True

    result_text = str(result or "").strip()
    lowered = result_text.casefold()
    if lowered.startswith(
        (
            "error:",
            "tool execution error:",
            "tool not found:",
        )
    ):
        return False
    if result_text.startswith("{") and result_text.endswith("}"):
        try:
            payload = json.loads(result_text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and payload.get("success") is False:
            return False
    return True


def successful_empty_task_search(records: Sequence[Any] | None) -> bool | None:
    """Return whether the latest task search succeeded with no candidates.

    ``None`` is deliberately used for an absent, failed, or ambiguous search;
    callers must not turn that state into a mutation request.
    """

    for record in reversed(list(records or [])):
        if _extract_tool_call_name(record).casefold() not in {
            "search_task_candidates",
            "list_tasks",
        }:
            continue
        if not _audit_tool_call_successful(record):
            return None
        if isinstance(record, dict):
            result = record.get("result", record.get("output"))
        else:
            result = getattr(record, "result", None)
            if result is None:
                result = getattr(record, "output", None)
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                return None
        if isinstance(result, list):
            return len(result) == 0
        if isinstance(result, dict):
            for key in ("items", "tasks", "candidates", "results"):
                value = result.get(key)
                if isinstance(value, list):
                    return len(value) == 0
            for key in ("count", "total"):
                value = result.get(key)
                if isinstance(value, int):
                    return value == 0
        return None
    return None


def required_project_mutation_tools_missing(
    user_input: str | None,
    audit_tool_calls: Sequence[Any] | None,
) -> tuple[str, ...]:
    """Return deterministic project mutations still missing from this work turn.

    Trusted slash/command capabilities are intentionally excluded here because
    their controller may expose multiple alternative mutation tools.  This
    guard is for ordinary chat prose where the existing deterministic parser
    identifies a concrete requested mutation such as ``create_task``.
    """

    text = str(user_input or "").strip()
    if (
        not text
        or mutation_execution_forbidden(text)
        or command_capabilities_from_text(text)
    ):
        return ()
    if not audit_tool_calls:
        # Do not turn a plain-text answer into a synthetic mutation request
        # when no tool call was attempted.  The guard is for the specific
        # search/read-then-mutate failure mode and therefore requires the
        # current turn's tool ledger.
        return ()

    normalized = text.casefold()
    question_markers = (
        "方法",
        "使い方",
        "使えるか",
        "できますか",
        "できるか",
        "教えて",
        "確認したか",
        "確認して",
        "確認したい",
        "一覧",
        "表示",
        "かどうか",
        "?",
        "？",
    )
    confirmation_markers = (
        "いいか",
        "よいか",
        "いい？",
        "よい？",
        "問題ないか",
        "大丈夫か",
        "してもいい",
        "してもよい",
        "しても大丈夫",
        "許可",
        "承認",
        "is it okay",
        "may i",
        "can i",
        "should i",
    )
    imperative_markers = (
        "作って",
        "作成して",
        "登録して",
        "追加して",
        "入れて",
        "更新して",
        "変更して",
        "修正して",
        "削除して",
        "消して",
        "割り当てて",
        "スケジュールして",
        "完了にして",
        "クローズして",
    )
    if any(marker in normalized for marker in confirmation_markers):
        return ()
    if any(marker in normalized for marker in question_markers) and not any(
        marker in normalized for marker in imperative_markers
    ):
        return ()

    required = {
        name
        for name in project_management_required_mutation_tools(text)
        if name
        in {
            "create_task",
            "update_task",
            "delete_task",
            "assign_task",
            "schedule_task",
        }
    }
    if not required:
        return ()

    # create_task/update_task can carry start/end/due scheduling fields
    # themselves.  Do not require an additional schedule_task call when the
    # primary task mutation already represents the requested operation.
    if "create_task" in required or "update_task" in required:
        required.discard("schedule_task")

    if "create_task" in required:
        # Creating a task is only forced after a successful empty duplicate
        # search.  Existing candidates or a failed/ambiguous search must stay
        # read-only and let the model explain the result.
        if successful_empty_task_search(audit_tool_calls) is not True:
            required.discard("create_task")
            if not required:
                return ()

    successful = {
        name.casefold()
        for record in audit_tool_calls or ()
        if _audit_tool_call_successful(record)
        for name in [_extract_tool_call_name(record)]
        if name
    }
    return tuple(
        sorted(
            tool_name
            for tool_name in required
            if tool_name.casefold() not in successful
        )
    )


_EXPLICIT_TOOL_CALL_PROMISE_REGEXES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?P<tool>[a-z][a-z0-9_]{2,})を呼び出します",
        r"will call (?P<tool>[a-z][a-z0-9_]{2,})",
        r"let me call (?P<tool>[a-z][a-z0-9_]{2,})",
    )
)


def response_promises_unexecuted_tool(
    response: str | None,
    audit_tool_calls: Sequence[Any] | None,
) -> bool:
    text = str(response or "").strip()
    if not text:
        return False
    executed = _audit_tool_call_names(audit_tool_calls)
    for pattern in _EXPLICIT_TOOL_CALL_PROMISE_REGEXES:
        for match in pattern.finditer(text):
            tool_name = str(match.group("tool") or "").strip().casefold()
            if tool_name and tool_name not in executed:
                return True
    return False


def response_definitely_incomplete_after_review(
    response: str | None,
    *,
    user_input: str | None = None,
    audit_tool_calls: Sequence[Any] | None = None,
) -> bool:
    """Mechanical-only reasons that may override a reviewer done decision."""

    text = str(response or "").strip()
    if not text:
        return True
    lowered = text.casefold()
    if any(term.casefold() in lowered for term in UNVERIFIED_TOOL_FAILURE_TERMS):
        return True
    if required_project_mutation_tools_missing(
        user_input,
        audit_tool_calls,
    ):
        return True
    return response_promises_unexecuted_tool(response, audit_tool_calls)


def apply_deterministic_incomplete_override(
    decision: dict[str, str],
    *,
    user_input: str | None,
    response: str | None,
    audit_tool_calls: Sequence[Any] | None = None,
) -> dict[str, str]:
    """Force continuation when reviewer says done but the answer is still incomplete."""

    if decision.get("status") == "continue":
        return decision
    if response_definitely_incomplete_after_review(
        response,
        user_input=user_input,
        audit_tool_calls=audit_tool_calls,
    ):
        return unfinished_work_decision(user_input, str(response or ""))
    return decision


def tool_loop_completion_confirmed(
    records: Sequence[Any] | None,
    final_output: str | None,
    *,
    stopped_reason: str | None = None,
) -> bool:
    """Return True when a tool loop reached a normal final stop with successful tools."""

    # A repeated identical successful call is suppressed by the unified
    # runtime and followed by one tools-disabled final sampling.  That is a
    # normal successful stop even though its diagnostic reason is not "final".
    if str(stopped_reason or "").strip() not in {
        "final",
        "redundant_tool_call_suppressed",
    }:
        return False
    items = list(records or [])
    if not items or not str(final_output or "").strip():
        return False
    for record in items:
        if hasattr(record, "successful"):
            if not bool(record.successful):
                return False
            continue
        if isinstance(record, dict):
            result = str(record.get("result") or "")
        else:
            result = str(getattr(record, "result", "") or "")
        lowered = result.strip().lower()
        if (
            lowered.startswith("tool not found:")
            or lowered.startswith("error:")
            or "delegation error" in lowered
            or "requested mutation was not completed" in lowered
        ):
            return False
    return True


def agentic_review_short_circuits_done(
    *,
    client: object,
    user_input: str | None,
    response: str | None,
    completion_confirmed: bool = False,
    audit_tool_calls: Sequence[Any] | None = None,
) -> bool:
    """Skip model review only for a mechanically confirmed complete tool turn.

    ``completion_confirmed`` is produced by the provider/tool loop only after
    it reached a normal final stop with successful tool executions.  It is
    necessary but deliberately not sufficient:

    - there must also be a non-empty successful audit ledger for this turn;
    - empty/progress/future-action responses still require review;
    - an unexecuted promised tool or a deterministically missing project
      mutation still requires continuation/review;
    - Review profile, project-progress review, and trusted explicit command
      capabilities keep their verifier pass because they represent an
      explicitly verification-sensitive external operation.

    This leaves ordinary confirmed tool-backed answers on the fast path while
    preserving the existing fail-closed mutation/approval/planning boundary.
    """

    if not completion_confirmed:
        return False

    text = str(response or "").strip()
    if not text:
        return False

    records = list(audit_tool_calls or ())
    if not records:
        # Never trust a boolean completion marker without the run's concrete
        # tool evidence.  This also prevents stale/shared client state from
        # suppressing review.
        return False
    if any(not _audit_tool_call_successful(record) for record in records):
        return False

    policy = get_client_generation_policy(client)
    if policy.profile == GenerationProfile.REVIEW:
        return False

    request = str(user_input or "")
    if project_progress_review_active(request):
        return False

    explicit_capabilities = command_capabilities_from_text(request)
    if explicit_capabilities.intersection(EXPLICIT_COMPLETION_CAPABILITIES):
        return False

    if response_looks_like_incomplete_final_answer(text):
        return False

    if response_definitely_incomplete_after_review(
        text,
        user_input=user_input,
        audit_tool_calls=records,
    ):
        return False

    return True


def response_looks_like_unfinished_work(
    user_input: str | None,
    response: str | None,
    *,
    completion_confirmed: bool = False,
) -> bool:
    """Detect plan-only output that must not be treated as completed work.

    ``completion_confirmed`` is reserved for a trusted command handler that
    returned normally after completing its own work.  It is deliberately an
    explicit signal rather than another completion word: arbitrary prose in a
    response must continue to go through the normal incomplete-response
    detector.
    """
    text = str(response or "").strip()
    if not text:
        return True
    lowered = text.casefold()
    if any(term.casefold() in lowered for term in UNVERIFIED_TOOL_FAILURE_TERMS):
        return True
    if completion_confirmed:
        return False

    if response_promises_future_tool_use(text):
        return True
    capabilities = command_capabilities_from_text(str(user_input or ""))
    if not capabilities.intersection(EXPLICIT_COMPLETION_CAPABILITIES):
        return False
    if any(term.casefold() in lowered for term in COMPLETION_EVIDENCE_TERMS):
        return False
    return any(term.casefold() in lowered for term in INCOMPLETE_RESPONSE_PATTERNS)


def _review_parse_failure_decision(reason: str) -> dict[str, str | bool | None]:
    return {
        "status": "continue",
        "reason": reason,
        "next_request": (
            "Continue the original request now. Use the necessary tools, verify "
            "the resulting external state, and do not answer with only a plan."
        ),
        "user_request_satisfied": False,
    }


def parse_agentic_review_decision(content: str) -> dict[str, str]:
    text = str(content or "").strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return _review_parse_failure_decision("review did not return JSON")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return _review_parse_failure_decision("review JSON parse failed")
    if not isinstance(payload, dict):
        return _review_parse_failure_decision("review payload was not an object")
    status = str(payload.get("status") or "").strip().lower()
    if status not in {"done", "continue"}:
        return _review_parse_failure_decision("review status was invalid")
    user_request_satisfied = payload.get("user_request_satisfied")
    if user_request_satisfied is not None and not isinstance(
        user_request_satisfied, bool
    ):
        normalized = str(user_request_satisfied).strip().lower()
        if normalized in {"true", "1", "yes"}:
            user_request_satisfied = True
        elif normalized in {"false", "0", "no"}:
            user_request_satisfied = False
        else:
            user_request_satisfied = None
    return {
        "status": status,
        "reason": str(payload.get("reason") or "").strip(),
        "next_request": str(payload.get("next_request") or "").strip(),
        "user_request_satisfied": user_request_satisfied,
    }


def normalize_agentic_review_decision(
    decision: dict[str, str | bool | None],
    *,
    user_input: str | None,
    response: str | None,
) -> dict[str, str | bool | None]:
    """Convert reviewer done into continue when the user request is still unsatisfied."""

    if decision.get("status") != "done":
        return decision
    if decision.get("user_request_satisfied") is not True:
        return unfinished_work_decision(user_input, str(response or ""))
    return decision


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


def format_tool_execution_evidence(records: Sequence[Any] | None) -> str:
    """Format tool execution records into review-loop evidence text."""
    lines: list[str] = []
    total = 0
    for record in records or []:
        if isinstance(record, dict):
            tool = record.get("tool")
            arguments = record.get("arguments")
            result = record.get("result")
        else:
            tool = getattr(record, "tool", None)
            arguments = getattr(record, "arguments", None)
            result = getattr(record, "result", None)
        try:
            arguments_text = json.dumps(dict(arguments or {}), ensure_ascii=False)
        except Exception:  # noqa: BLE001
            arguments_text = str(arguments)
        if len(arguments_text) > 200:
            arguments_text = arguments_text[:200] + "…"
        result_text = str(result or "").strip()
        if len(result_text) > 500:
            result_text = result_text[:500] + "…"
        line = f"- {tool}({arguments_text}) -> {result_text}"
        if total + len(line) > 4000:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def build_agentic_review_prompt(
    *,
    original_context: str,
    latest_response: str,
    round_index: int,
    user_input: str | None = None,
    tool_evidence: str | None = None,
) -> str:
    lines = [
        "You are the completion verifier for this AoiTalk agent run.",
        "Review whether the user's request has actually been completed.",
        "Judge whether the latest assistant response directly answers the user's request with the requested result, not merely progress, intent, or a plan to answer later.",
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
    if tool_evidence:
        lines.append(
            "If the 'Confirmed tool executions in the latest run' section below lists tool executions, treat them as confirmed tool results backing the latest response."
        )
    lines.extend(
        [
            "Return exactly one JSON object and no markdown:",
            '{"status":"done","reason":"...","user_request_satisfied":true}',
            '{"status":"continue","reason":"...","next_request":"...","user_request_satisfied":false}',
            "Set user_request_satisfied to true only when the latest assistant response directly answers the user's request.",
            "",
            f"Review round: {round_index}",
        ]
    )
    if user_input:
        lines.extend(
            [
                "",
                "User request:",
                str(user_input),
            ]
        )
    lines.extend(
        [
            "",
            "Original conversation context:",
            original_context,
        ]
    )
    if tool_evidence:
        lines.extend(
            [
                "",
                "Confirmed tool executions in the latest run:",
                tool_evidence,
            ]
        )
    lines.extend(
        [
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
    tool_evidence: str | None = None,
) -> str:
    next_request = decision.get("next_request") or (
        "Continue the work needed to satisfy the original request."
    )
    sections = [
        "Continue this AoiTalk agent run because verification found unfinished or invalid work.",
        "Use specialist tools as needed, then produce a corrected final response.",
        "",
        "Original conversation context:",
        original_context,
    ]
    if tool_evidence:
        sections.extend(
            [
                "",
                "Confirmed tool executions in the latest run:",
                tool_evidence,
            ]
        )
    sections.extend(
        [
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
    return "\n".join(sections)


async def run_agentic_completion_loop_async(
    *,
    client: object,
    run_once: AsyncRunOnce,
    context: str,
    stream_callback: Optional[AsyncStreamCallback] = None,
    user_input: str | None = None,
    initial_response: str | None = None,
    tool_evidence_provider: Callable[[], str] | None = None,
    completion_confirmed_provider: Callable[[], bool] | None = None,
    audit_tool_calls_provider: Callable[[], Sequence[Any] | None] | None = None,
    run_review_once: AsyncRunOnce | None = None,
    run_continuation_once: AsyncRunOnce | None = None,
) -> str:
    if not agentic_completion_enabled(client, user_input):
        if initial_response is not None:
            return initial_response
        return await run_once(context)

    review_runner = run_review_once or run_once
    continuation_runner = run_continuation_once or run_once

    if stream_callback:
        await stream_callback(
            "stream_start",
            {"status": "agentic", "message": "作業を実行しています"},
        )

    response = initial_response if initial_response is not None else await run_once(context)
    max_rounds = agentic_max_rounds(client, user_input)
    review_verified = False
    for round_index in range(1, max_rounds + 1):
        if stream_callback:
            await stream_callback(
                "status_update",
                {
                    "status": "agentic_review",
                    "message": "結果を検証しています",
                },
            )

        tool_evidence = (
            tool_evidence_provider() if tool_evidence_provider is not None else None
        )
        completion_confirmed = (
            completion_confirmed_provider()
            if completion_confirmed_provider is not None
            else False
        )
        audit_tool_calls = (
            audit_tool_calls_provider()
            if audit_tool_calls_provider is not None
            else None
        )
        if agentic_review_short_circuits_done(
            client=client,
            user_input=user_input,
            response=response,
            completion_confirmed=completion_confirmed,
            audit_tool_calls=audit_tool_calls,
        ):
            review_verified = True
            break
        if response_promises_future_tool_use(response) and not completion_confirmed:
            decision = unfinished_work_decision(user_input, response)
            review_response = ""
        else:
            review_prompt = build_agentic_review_prompt(
                original_context=context,
                latest_response=response,
                round_index=round_index,
                user_input=user_input,
                tool_evidence=tool_evidence or None,
            )
            review_response = await review_runner(review_prompt)
            decision = parse_agentic_review_decision(str(review_response or ""))
            decision = normalize_agentic_review_decision(
                decision,
                user_input=user_input,
                response=response,
            )
            decision = apply_deterministic_incomplete_override(
                decision,
                user_input=user_input,
                response=response,
                audit_tool_calls=audit_tool_calls,
            )
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
            review_verified = True
            break

        if round_index >= max_rounds:
            if (
                getattr(get_client_generation_policy(client), "profile", None)
                in WORK_GENERATION_PROFILES
            ):
                continuation_context = build_agentic_continuation_context(
                    original_context=context,
                    latest_response=response,
                    decision=decision,
                    tool_evidence=tool_evidence or None,
                )
                response = await continuation_runner(continuation_context)
                review_verified = True
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
            tool_evidence=tool_evidence or None,
        )
        response = await continuation_runner(continuation_context)
    else:
        review_verified = False

    if not review_verified:
        response = build_incomplete_work_failure_response(
            user_input=user_input,
            latest_response=response,
        )
    elif response_looks_like_unfinished_work(user_input, response):
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
    tool_evidence_provider: Callable[[], str] | None = None,
    completion_confirmed_provider: Callable[[], bool] | None = None,
    audit_tool_calls_provider: Callable[[], Sequence[Any] | None] | None = None,
    run_review_once: SyncRunOnce | None = None,
    run_continuation_once: SyncRunOnce | None = None,
) -> str:
    if not agentic_completion_enabled(client, user_input):
        if initial_response is not None:
            return initial_response
        return run_once(context)

    review_runner = run_review_once or run_once
    continuation_runner = run_continuation_once or run_once

    response = initial_response if initial_response is not None else run_once(context)
    max_rounds = agentic_max_rounds(client, user_input)
    review_verified = False
    for round_index in range(1, max_rounds + 1):
        tool_evidence = (
            tool_evidence_provider() if tool_evidence_provider is not None else None
        )
        completion_confirmed = (
            completion_confirmed_provider()
            if completion_confirmed_provider is not None
            else False
        )
        audit_tool_calls = (
            audit_tool_calls_provider()
            if audit_tool_calls_provider is not None
            else None
        )
        if agentic_review_short_circuits_done(
            client=client,
            user_input=user_input,
            response=response,
            completion_confirmed=completion_confirmed,
            audit_tool_calls=audit_tool_calls,
        ):
            review_verified = True
            break
        if response_promises_future_tool_use(response) and not completion_confirmed:
            decision = unfinished_work_decision(user_input, response)
            review_response = ""
        else:
            review_prompt = build_agentic_review_prompt(
                original_context=context,
                latest_response=response,
                round_index=round_index,
                user_input=user_input,
                tool_evidence=tool_evidence or None,
            )
            review_response = review_runner(review_prompt)
            decision = parse_agentic_review_decision(str(review_response or ""))
            decision = normalize_agentic_review_decision(
                decision,
                user_input=user_input,
                response=response,
            )
            decision = apply_deterministic_incomplete_override(
                decision,
                user_input=user_input,
                response=response,
                audit_tool_calls=audit_tool_calls,
            )
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
            review_verified = True
            break

        if round_index >= max_rounds:
            if (
                getattr(get_client_generation_policy(client), "profile", None)
                in WORK_GENERATION_PROFILES
            ):
                continuation_context = build_agentic_continuation_context(
                    original_context=context,
                    latest_response=response,
                    decision=decision,
                    tool_evidence=tool_evidence or None,
                )
                response = continuation_runner(continuation_context)
                review_verified = True
            break

        continuation_context = build_agentic_continuation_context(
            original_context=context,
            latest_response=response,
            decision=decision,
            tool_evidence=tool_evidence or None,
        )
        response = continuation_runner(continuation_context)
    else:
        review_verified = False

    if not review_verified:
        response = build_incomplete_work_failure_response(
            user_input=user_input,
            latest_response=response,
        )
    elif response_looks_like_unfinished_work(user_input, response):
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
