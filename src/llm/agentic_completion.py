"""Provider-independent agentic completion review loop."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import Enum
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
CompletionEvidence = Mapping[str, Any]
CompletionEvidenceProvider = Callable[[], CompletionEvidence | None]


class SimpleTaskMutationCompletionState(str, Enum):
    """Provider-independent semantic state for one simple task-create request."""

    NOT_APPLICABLE = "not_applicable"
    PENDING_OR_BLOCKED = "pending_or_blocked"
    COMPLETE = "complete"

DEFAULT_AGENTIC_MAX_ROUNDS = 2
DEFAULT_WORK_MAX_ROUNDS = 120
DEFAULT_PROJECT_PROGRESS_MAX_ROUNDS = 120
DEFAULT_REVIEW_MAX_ROUNDS = 2
AGENTIC_MAX_ROUNDS_CAP = 1000
WORK_GENERATION_PROFILES = {
    GenerationProfile.ASSISTED_WORK,
    GenerationProfile.AUTONOMOUS_WORK,
}

# These mutations have a structured result that can prove the requested task
# state without a second model call.  Artifact/document mutations deliberately
# stay outside this set: they retain the normal verifier path even when their
# tool call itself reports success.
DETERMINISTIC_TASK_MUTATION_TOOLS = frozenset(
    {
        "create_task",
        "update_task",
        "delete_task",
        "assign_task",
        "schedule_task",
    }
)
# Completion fast-path is intentionally narrower than the mutation inventory.
# Only ``create_task`` has a stable, fully projected receipt contract across
# every supported provider.  Other task mutations continue through the
# generic all-success/reviewer path until their own postcondition projection
# is explicitly introduced.
AUTHORITATIVE_TASK_MUTATION_TOOLS = frozenset({"create_task"})

_TASK_CREATE_IMPERATIVE_MARKERS = (
    "作って",
    "作成して",
    "作成してください",
    "作成お願いします",
    "作成をお願いします",
    "登録して",
    "登録してください",
    "登録お願いします",
    "追加して",
    "追加してください",
    "入れて",
    "create task",
    "create a task",
    "create_task",
    "register a task",
    "make a task",
    "add a task",
)
_TASK_COMPLEX_SCOPE_MARKERS = (
    "excel",
    "xlsx",
    "xls",
    "csv",
    "spreadsheet",
    "スプレッドシート",
    "ワークスペース",
    "フォルダ",
    "ドキュメント",
    "docs",
    "wbs",
    "工程表",
    "案件情報",
    "プロジェクト情報",
    "課題管理",
    "db",
    "database",
    "データベース",
    "テーブルを",
    "台帳",
    "レコード",
    "ファイルを加工",
    "ファイルを編集",
    "ファイルを更新",
    "資料を加工",
    "資料を編集",
    "画像生成",
    "画像を",
    "動画生成",
    "動画を",
    "音声生成",
    "音声を",
    "web検索",
    "ウェブ検索",
    "アプリを",
    "アプリで",
    "アプリ操作",
    "マクロ",
    "コード修正",
    "コードを修正",
    "コードを書",
    "ファイル作成",
    "ファイルを作って",
    "ファイルを作成",
    "資料を作って",
    "資料を作成",
    "ネットで調べ",
    "webで調べ",
    "ウェブで調べ",
    "メールを送って",
    "メール送って",
    "メッセージを送って",
    "送信して",
)
_SIMPLE_TASK_POST_CREATE_BLOCKED_TOOLS = frozenset(
    {
        "web_search",
        "search_web",
        "deep_research",
        "send_email",
        "send_message",
        "create_file",
        "write_file",
        "edit_file",
        "delete_file",
        "execute_command",
        "generate_image",
        "media_assistant",
        "agent_team_delegate",
        "create_record_table",
        "sync_wbs_tasks",
        "sync_issue_table",
        "patch_project_information_doc",
        "organize_project_information_from_folder",
        "attach_project_information_reference",
        "submit_plan_for_approval",
    }
)


def _simple_task_has_unexpected_successful_mutation(
    records: Sequence[Any] | None,
) -> bool:
    """Reject a simple-create proof when another task mutation succeeded.

    The simple fast-path proves exactly one ``create_task`` operation.  A
    successful update/delete/assign/schedule in the same attempt is not an
    optional diagnostic: it is an additional side effect whose target and
    ordering the create receipt does not prove.  Failed attempts remain in
    the audit trail but do not poison a later, authoritative create.
    """

    unexpected = DETERMINISTIC_TASK_MUTATION_TOOLS - {"create_task"}
    return any(
        _extract_tool_call_name(record).casefold() in unexpected
        and _audit_tool_call_successful(record)
        for record in (records or ())
    )


def _task_create_imperative_present(text: str | None) -> bool:
    normalized = str(text or "").casefold()
    return any(marker.casefold() in normalized for marker in _TASK_CREATE_IMPERATIVE_MARKERS)


def _task_post_create_followup_present(text: str | None) -> bool:
    normalized = str(text or "").casefold()
    create_positions = [
        normalized.find(marker.casefold())
        for marker in _TASK_CREATE_IMPERATIVE_MARKERS
        if normalized.find(marker.casefold()) >= 0
    ]
    if not create_positions:
        return "作成したタスク" in normalized
    create_position = min(create_positions)
    followup_markers = (
        "タスクの内容を確認",
        "作成したタスクを確認",
        "作成後に確認",
        "作成後に変更",
        "作成後に修正",
        "作成後に削除",
        "作成後に送",
        "その内容を確認",
        "その内容を変更",
        "その内容を修正",
        "その内容を削除",
        "その内容を送",
        "確認してから",
        "確認したら",
    )
    return any(
        (position := normalized.find(marker.casefold())) > create_position
        for marker in followup_markers
        if normalized.find(marker.casefold()) >= 0
    )


def requested_deterministic_task_mutation_tools(text: str | None) -> set[str]:
    """Resolve the required task mutations for the current user request.

    The broad project-policy classifier intentionally recognizes words such as
    「変更」/「削除」 anywhere in a message.  Reservation/source content often
    contains those words (for example, a changed booking email) while the
    requested operation is still one new task.  When an explicit create
    imperative is present and no *task-targeted* second mutation is requested,
    keep the semantic contract to one create operation.
    """

    try:
        required = {
            str(name).strip().casefold()
            for name in project_management_required_mutation_tools(str(text or ""))
            if str(name).strip().casefold() in DETERMINISTIC_TASK_MUTATION_TOOLS
        }
    except Exception:
        return set()
    if "create_task" not in required:
        return required
    if not _task_create_imperative_present(text):
        return required
    if any(
        marker.casefold() in str(text or "").casefold()
        for marker in _TASK_COMPLEX_SCOPE_MARKERS
    ):
        return required
    task_targeted_secondary_markers = (
        "既存タスク",
        "既存のタスク",
        "そのタスク",
        "タスクを更新",
        "タスクの更新",
        "タスクを変更",
        "タスクの変更",
        "タスクを修正",
        "タスクの修正",
        "タスクを削除",
        "タスクの削除",
        "タスクを割り当て",
        "タスクに割り当て",
        "タスクを完了",
        "タスクをクローズ",
        "作成後に更新",
        "作成したタスク",
        "作成してから更新",
        "作成してから変更",
        "作成してから修正",
        "作成してから削除",
        "作成してから割り当て",
        "作成したら更新",
        "作成したら変更",
        "作成したら修正",
        "作成したら削除",
        "作った後に更新",
        "作った後に変更",
        "作った後に削除",
        "create then update",
        "create and update",
        "create task then",
        "update task",
        "modify task",
        "change task",
        "delete task",
        "assign task",
        "close task",
    )
    if not any(
        marker.casefold() in str(text or "").casefold()
        for marker in task_targeted_secondary_markers
    ):
        return {"create_task"}
    # Scheduling represented by create_task fields is not a second required
    # operation even when the surrounding request mentions a calendar.
    required.discard("schedule_task")
    return required

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


def _success_marker(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {
        "1",
        "true",
        "yes",
        "ok",
        "success",
        "succeeded",
    }


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
        error = record.get("error") or record.get("failure") or record.get(
            "error_message"
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
        error = getattr(record, "error", None) or getattr(record, "failure", None)

    if str(error or "").strip():
        return False
    explicit_marker = _success_marker(explicit_success)
    if explicit_marker is False:
        return False

    if isinstance(result, dict):
        result_marker = _success_marker(result.get("success"))
        if result_marker is False or str(result.get("error") or "").strip():
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
        if isinstance(payload, dict):
            payload_marker = _success_marker(payload.get("success"))
            if payload_marker is False or str(payload.get("error") or "").strip():
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
            indicators: list[bool] = []
            for key in ("items", "tasks", "candidates", "results"):
                if key in result and not isinstance(result.get(key), list):
                    return None
                value = result.get(key)
                if isinstance(value, list):
                    indicators.append(len(value) == 0)
            for key in ("count", "total"):
                if key in result and (
                    isinstance(result.get(key), bool)
                    or not isinstance(result.get(key), int)
                ):
                    return None
                value = result.get(key)
                if isinstance(value, int):
                    indicators.append(value == 0)
            if indicators and all(item == indicators[0] for item in indicators):
                return indicators[0]
        return None
    return None


def _semantic_record_result(record: Any) -> Any:
    if isinstance(record, Mapping):
        if record.get("result") is not None:
            return record.get("result")
        if record.get("output") is not None:
            return record.get("output")
        return record.get("model_output")
    result = getattr(record, "result", None)
    if result is not None:
        return result
    result = getattr(record, "output", None)
    if result is not None:
        return result
    return getattr(record, "model_output", None)


def _semantic_record_arguments(record: Any) -> Mapping[str, Any]:
    value = (
        record.get("arguments")
        if isinstance(record, Mapping)
        else getattr(record, "arguments", None)
    )
    return value if isinstance(value, Mapping) else {}


def _decode_semantic_result(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _semantic_task_payload(record: Any) -> dict[str, Any] | None:
    payload = _decode_semantic_result(_semantic_record_result(record))
    if not isinstance(payload, dict):
        return None
    if payload.get("success") is False:
        return None
    if not (payload.get("id") or payload.get("task_id")):
        for key in ("task", "data", "item", "result"):
            nested = payload.get(key)
            if isinstance(nested, dict) and (
                nested.get("id") or nested.get("task_id")
            ):
                payload = {**payload, **nested}
                break
    return payload


def _search_result_unambiguous_nonempty(record: Any) -> bool:
    """Return true only for a successful search with explicit candidates."""

    if not _audit_tool_call_successful(record):
        return False
    payload = _decode_semantic_result(_semantic_record_result(record))
    if isinstance(payload, list):
        return bool(payload)
    if not isinstance(payload, dict):
        return False
    for key in ("items", "tasks", "candidates", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return bool(value)
    for key in ("count", "total"):
        value = payload.get(key)
        if isinstance(value, int):
            return value > 0
    return False


def _semantic_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        from datetime import datetime

        candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(candidate)
        return parsed.replace(tzinfo=None).isoformat()
    except (TypeError, ValueError):
        return " ".join(text.split()).casefold()


def _semantic_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _simple_task_create_postcondition(record: Any) -> bool:
    """Validate the structured receipt for a simple ``create_task`` call."""

    if _extract_tool_call_name(record).casefold() != "create_task":
        return False
    if not _audit_tool_call_successful(record):
        return False
    arguments = _semantic_record_arguments(record)
    title = str(arguments.get("title") or "").strip()
    if not title:
        return False
    payload = _semantic_task_payload(record)
    if not isinstance(payload, dict):
        return False
    task_id = str(payload.get("id") or payload.get("task_id") or "").strip()
    if not task_id or str(payload.get("title") or "").strip() != title:
        return False
    if (
        bool(arguments.get("auto_close_on_due"))
        or str(arguments.get("assignee_ids") or "").strip()
        or str(arguments.get("recurrence_rrule") or "").strip()
    ):
        return False

    aliases: dict[str, tuple[str, ...]] = {
        "description": ("description",),
        "start_at": ("start_at", "starts_at"),
        "end_at": ("end_at", "ends_at"),
        "project_id": ("project_id", "project"),
        "parent_task_id": ("parent_task_id", "parent_id"),
        "all_day": ("all_day",),
        "priority": ("priority",),
    }
    requested_due_date = str(arguments.get("due_date") or "").strip()
    if requested_due_date:
        observed = payload.get("due_date") or payload.get("start_at")
        if not observed or str(observed).strip()[:10] != requested_due_date[:10]:
            return False
        if not _semantic_bool(payload.get("all_day")):
            return False

    for key, field_aliases in aliases.items():
        expected = arguments.get(key)
        if key == "all_day" and requested_due_date:
            expected = True
        if expected in (None, ""):
            continue
        observed_key = next((item for item in field_aliases if item in payload), None)
        if observed_key is None:
            return False
        observed = payload.get(observed_key)
        if key == "all_day":
            if _semantic_bool(observed) != _semantic_bool(expected):
                return False
        elif key in {"start_at", "end_at"}:
            if _semantic_datetime(observed) != _semantic_datetime(expected):
                return False
        elif key in {"project_id", "project"}:
            observed_project_id = str(payload.get("project_id") or "").strip()
            observed_project_name = str(
                payload.get("project_name") or payload.get("project") or ""
            ).strip()
            if key == "project_id":
                if not observed_project_id or observed_project_id.casefold() != str(
                    expected
                ).strip().casefold():
                    return False
            elif str(expected).strip().casefold() not in {
                observed_project_id.casefold(),
                observed_project_name.casefold(),
            }:
                return False
        elif str(observed or "") != str(expected):
            return False
    return True


def _simple_task_post_create_has_blocked_work(
    records: Sequence[Any] | None,
) -> bool:
    items = list(records or ())
    successful_create_indexes = [
        index
        for index, record in enumerate(items)
        if _extract_tool_call_name(record).casefold() == "create_task"
        and _audit_tool_call_successful(record)
    ]
    if len(successful_create_indexes) != 1:
        return False
    create_index = successful_create_indexes[0]
    return any(
        _extract_tool_call_name(record).casefold()
        in _SIMPLE_TASK_POST_CREATE_BLOCKED_TOOLS
        and _audit_tool_call_successful(record)
        for record in items[create_index + 1 :]
    )


def _simple_task_request_is_explicit(text: str) -> bool:
    normalized = str(text or "").casefold()
    if not normalized:
        return False
    if mutation_execution_forbidden(normalized):
        return False
    if any(marker.casefold() in normalized for marker in _TASK_COMPLEX_SCOPE_MARKERS):
        return False
    if _task_post_create_followup_present(normalized):
        return False
    confirmation_markers = (
        "してもいい",
        "してもよい",
        "しても大丈夫",
        "作ってもいい",
        "作ってもよい",
        "作っていい",
        "作って良い",
        "作成していい",
        "作成してよい",
        "作成して良い",
        "登録していい",
        "登録してよい",
        "登録して良い",
        "追加していい",
        "追加してよい",
        "追加して良い",
        "許可",
        "承認",
        "is it okay",
        "may i",
        "can i",
        "should i",
    )
    if any(marker in normalized for marker in confirmation_markers):
        return False
    question_markers = (
        "方法",
        "使い方",
        "教えて",
        "できますか",
        "できるか",
        "確認して",
        "確認したい",
        "かどうか",
        "?",
        "？",
    )
    imperative_markers = (
        "作って",
        "作ってください",
        "作成して",
        "作成してください",
        "作成お願いします",
        "作成をお願いします",
        "登録して",
        "登録してください",
        "登録お願いします",
        "追加して",
        "追加してください",
        "入れて",
        "create ",
        "create_task",
        "register a task",
        "make a task",
        "add a task",
    )
    conditional_markers = ("作成予定", "作る予定", "作成するつもり", "作るつもり", "作成を検討", "作成を考えて")
    if any(marker in normalized for marker in conditional_markers) and not any(
        marker in normalized for marker in imperative_markers
    ):
        return False
    if any(marker in normalized for marker in question_markers) and not any(
        marker in normalized for marker in imperative_markers
    ):
        return False
    if any(marker in normalized for marker in ("?", "？")) and any(
        marker in normalized
        for marker in ("いい", "良い", "大丈夫", "可能", "できますか")
    ):
        return False
    return _task_create_imperative_present(normalized)


def simple_task_mutation_completion_state(
    user_input: str | None,
    audit_tool_calls: Sequence[Any] | None,
) -> SimpleTaskMutationCompletionState:
    """Classify a simple task-create request without provider/model state.

    The helper is deliberately conservative.  It ignores unrelated records
    *after* a proven create, but a failed/ambiguous search, a missing receipt,
    or a second successful create keeps the state pending/blocked.  It is used
    as the compatibility fallback when a provider has no attempt-local
    completion ledger; TurnExecution's richer ledger remains authoritative for
    concurrent/continuation turns.
    """

    request = str(user_input or "")
    if not _simple_task_request_is_explicit(request):
        return SimpleTaskMutationCompletionState.NOT_APPLICABLE
    required = requested_deterministic_task_mutation_tools(request)
    if "create_task" not in required:
        return SimpleTaskMutationCompletionState.NOT_APPLICABLE
    required.discard("schedule_task")
    if required != {"create_task"}:
        return SimpleTaskMutationCompletionState.NOT_APPLICABLE

    records = list(audit_tool_calls or ())
    if _simple_task_has_unexpected_successful_mutation(records):
        return SimpleTaskMutationCompletionState.PENDING_OR_BLOCKED
    create_records = [
        record
        for record in records
        if _extract_tool_call_name(record).casefold() == "create_task"
    ]
    successful_create_indexes = {
        index
        for index, record in enumerate(records)
        if _extract_tool_call_name(record).casefold() == "create_task"
        and _audit_tool_call_successful(record)
    }
    successful_creates = [
        records[index] for index in sorted(successful_create_indexes)
    ]
    if len(successful_creates) != 1:
        return SimpleTaskMutationCompletionState.PENDING_OR_BLOCKED
    create_index = next(iter(successful_create_indexes))
    successful_create = records[create_index]
    # A required create failure after a successful create is not an optional
    # diagnostic failure and must not be collapsed into success.
    if any(
        not _audit_tool_call_successful(record)
        for index, record in enumerate(records)
        if _extract_tool_call_name(record).casefold() == "create_task"
        if index > create_index
    ):
        return SimpleTaskMutationCompletionState.PENDING_OR_BLOCKED
    preceding = records[:create_index]
    if successful_empty_task_search(preceding) is not True:
        return SimpleTaskMutationCompletionState.PENDING_OR_BLOCKED
    if not _simple_task_create_postcondition(successful_create):
        return SimpleTaskMutationCompletionState.PENDING_OR_BLOCKED
    if _simple_task_post_create_has_blocked_work(records):
        return SimpleTaskMutationCompletionState.PENDING_OR_BLOCKED
    return SimpleTaskMutationCompletionState.COMPLETE


def _simple_task_duplicate_search_blocked(
    user_input: str | None,
    audit_tool_calls: Sequence[Any] | None,
) -> bool:
    """Detect a malformed/failed duplicate search that must stop mutation."""

    request = str(user_input or "")
    if not _simple_task_request_is_explicit(request):
        return False
    required = requested_deterministic_task_mutation_tools(request)
    required.discard("schedule_task")
    if required != {"create_task"}:
        return False
    records = list(audit_tool_calls or ())
    if _simple_task_has_unexpected_successful_mutation(records):
        return True
    search_indexes = [
        index
        for index, record in enumerate(records)
        if _extract_tool_call_name(record).casefold()
        in {"search_task_candidates", "list_tasks"}
    ]
    if not search_indexes:
        return False

    # If a create was attempted, only the latest search immediately before
    # that create is the duplicate precondition.  A later optional search
    # failure cannot revoke a previously proven create.
    create_indexes = [
        index
        for index, record in enumerate(records)
        if _extract_tool_call_name(record).casefold() == "create_task"
    ]
    if create_indexes:
        first_create = create_indexes[0]
        preceding_searches = [index for index in search_indexes if index < first_create]
        if preceding_searches:
            search_record = records[preceding_searches[-1]]
            search_state = successful_empty_task_search(
                records[: preceding_searches[-1] + 1]
            )
            if search_state is True:
                return False
            # A valid non-empty candidate result is a safe duplicate stop,
            # unless the model nevertheless attempted a create afterward.
            if search_state is False and _search_result_unambiguous_nonempty(
                search_record
            ):
                return True
            return True
        return True
    search_state = successful_empty_task_search(records)
    if search_state is True:
        return False
    # Existing candidates are an intentional read-only outcome, not an
    # ambiguous search failure.  Let the normal completion path report that
    # no new task was created.
    if search_state is False:
        return False
    return True


def _completion_evidence_records_for_search(
    completion_evidence: CompletionEvidence | None,
    audit_tool_calls: Sequence[Any] | None,
) -> list[Any]:
    records: list[Any] = []
    if isinstance(completion_evidence, Mapping):
        for key in ("search_records", "required_tool_records", "tool_records"):
            records.extend(_completion_evidence_records(completion_evidence.get(key)))
    records.extend(list(audit_tool_calls or ()))
    return records


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

    required = requested_deterministic_task_mutation_tools(text)
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


def _task_mutation_postcondition_failed(
    completion_evidence: CompletionEvidence | None,
    *,
    user_input: str | None,
    audit_tool_calls: Sequence[Any] | None,
) -> bool:
    """Return whether an attempted task mutation explicitly failed proof.

    This is a narrow fail-closed breaker for a producer that knows a required
    task mutation was attempted but could not verify its structured result
    (for example, a requested schedule was omitted from the create receipt).
    It intentionally does not infer failure from an absent evidence mapping or
    from a read-only/ambiguous request.
    """

    if not isinstance(completion_evidence, Mapping):
        return False
    postconditions_verified = completion_evidence.get("postconditions_verified")
    if postconditions_verified is None:
        postconditions_verified = completion_evidence.get("postcondition_verified")
    if postconditions_verified is None:
        postconditions_verified = completion_evidence.get(
            "required_mutations_satisfied"
        )
    if postconditions_verified is not False:
        return False

    required_names = set(
        _completion_evidence_tool_names(completion_evidence.get("required_tools"))
    )
    required_names.intersection_update(DETERMINISTIC_TASK_MUTATION_TOOLS)
    if not required_names:
        required_names = {
            name.casefold()
            for name in requested_deterministic_task_mutation_tools(
                str(user_input or "")
            )
        }
    if not required_names:
        return False

    evidence_records = _completion_evidence_records(
        completion_evidence.get("required_tool_records")
    )
    evidence_records.extend(
        _completion_evidence_records(completion_evidence.get("tool_records"))
    )
    all_records = [*evidence_records, *list(audit_tool_calls or ())]
    # A postcondition failure only matters after that required operation was
    # actually attempted.  This avoids turning a plain request with no tool
    # call, or an unrelated optional failure, into a synthetic failure claim.
    attempted_names = {
        _extract_tool_call_name(record).casefold()
        for record in all_records
        if _extract_tool_call_name(record)
    }
    return bool(required_names.intersection(attempted_names))


def response_definitely_incomplete_after_review(
    response: str | None,
    *,
    user_input: str | None = None,
    audit_tool_calls: Sequence[Any] | None = None,
    completion_evidence: CompletionEvidence | None = None,
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
    if _task_mutation_postcondition_failed(
        completion_evidence,
        user_input=user_input,
        audit_tool_calls=audit_tool_calls,
    ):
        return True
    if _response_claims_unproven_task_creation(
        response,
        user_input=user_input,
        audit_tool_calls=audit_tool_calls,
        completion_evidence=completion_evidence,
    ):
        return True
    if (
        not isinstance(completion_evidence, Mapping)
        or completion_evidence.get("authoritative") is not True
    ) and _simple_task_duplicate_search_blocked(
        user_input,
        _completion_evidence_records_for_search(
            completion_evidence,
            audit_tool_calls,
        ),
    ):
        return True
    return response_promises_unexecuted_tool(response, audit_tool_calls)


def apply_deterministic_incomplete_override(
    decision: dict[str, str],
    *,
    user_input: str | None,
    response: str | None,
    audit_tool_calls: Sequence[Any] | None = None,
    completion_evidence: CompletionEvidence | None = None,
) -> dict[str, str]:
    """Force continuation when reviewer says done but the answer is still incomplete."""

    if decision.get("status") == "continue":
        return decision
    if response_definitely_incomplete_after_review(
        response,
        user_input=user_input,
        audit_tool_calls=audit_tool_calls,
        completion_evidence=completion_evidence,
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


def _completion_evidence_tool_names(value: Any) -> tuple[str, ...]:
    """Normalize required-tool names carried by the attempt-local ledger."""

    if isinstance(value, Mapping):
        values: Sequence[Any] = tuple(
            key
            for key, satisfied in value.items()
            if satisfied is True
        )
    elif isinstance(value, str):
        values: Sequence[Any] = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        return ()
    names: list[str] = []
    for item in values:
        name = str(item or "").strip().casefold()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _completion_evidence_records(value: Any) -> list[Any]:
    """Read a record list without allowing arbitrary mappings as records."""

    if isinstance(value, Mapping):
        # A mapping keyed by tool name is accepted for callers that keep a
        # compact required-operation ledger instead of a list of records.
        return list(value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _latest_required_records_successful(
    records: Sequence[Any],
    required_names: set[str],
) -> bool:
    """Require the latest record for every required mutation to succeed."""

    latest: dict[str, Any] = {}
    for record in records:
        name = _extract_tool_call_name(record).casefold()
        if name in required_names:
            latest[name] = record
    return all(
        name in latest and _audit_tool_call_successful(latest[name])
        for name in required_names
    )


def _authoritative_task_mutation_completion(
    completion_evidence: CompletionEvidence | None,
    *,
    user_input: str | None,
    audit_tool_calls: Sequence[Any] | None,
) -> tuple[bool, list[Any]]:
    """Validate attempt-local proof for a deterministic task mutation.

    ``audit_tool_calls`` is intentionally not used as the proof of
    completion: it can include optional/superseded failures from earlier
    continuation attempts.  The caller supplies a separate attempt-local
    ledger where ``postconditions_verified`` (or the backwards compatible
    ``required_mutations_satisfied`` alias) is set only after the required
    mutation's structured result proves the requested postcondition.
    The audit list remains available for diagnostics and the existing
    missing-mutation guard.
    """

    if not isinstance(completion_evidence, Mapping):
        return False, []
    if completion_evidence.get("authoritative") is not True:
        return False, []
    request = str(user_input or "")
    if request.strip() and not _simple_task_request_is_explicit(request):
        return False, []
    required_mutations_satisfied = completion_evidence.get(
        "required_mutations_satisfied"
    )
    if required_mutations_satisfied is None:
        required_mutations_satisfied = completion_evidence.get(
            "postconditions_verified"
        )
    if required_mutations_satisfied is None:
        required_mutations_satisfied = completion_evidence.get(
            "postcondition_verified"
        )
    if required_mutations_satisfied is not True:
        return False, []
    # A producer may expose this optional, more explicit spelling.  If it is
    # present, a false value must always fail closed; an absent value is
    # covered by ``required_mutations_satisfied`` for backwards compatibility.
    if (
        completion_evidence.get("postcondition_verified") is False
        or completion_evidence.get("postconditions_verified") is False
    ):
        return False, []
    required_failure = completion_evidence.get("required_failure")
    if required_failure not in (None, False, "", (), [], {}):
        return False, []

    required_names = _completion_evidence_tool_names(
        completion_evidence.get("required_tools")
    )
    if not required_names or any(
        name not in AUTHORITATIVE_TASK_MUTATION_TOOLS for name in required_names
    ):
        return False, []
    requested_mutations = requested_deterministic_task_mutation_tools(request)
    if request.strip() and set(required_names) != requested_mutations:
        # The attempt-local ledger cannot narrow a user request from a
        # multi-mutation operation to one create merely by omitting the other
        # required tool.
        return False, []

    satisfied_names = _completion_evidence_tool_names(
        completion_evidence.get("satisfied_tools")
    )
    if "satisfied_tools" in completion_evidence and not set(required_names).issubset(
        satisfied_names
    ):
        return False, []

    required_records = _completion_evidence_records(
        completion_evidence.get("required_tool_records")
    )
    if not required_records:
        # A compact producer may expose only the attempt-local ``tool_records``
        # list.  Derive the required subset from that list; never derive it
        # from the all-attempt audit history, which may contain stale failures.
        attempt_candidates = _completion_evidence_records(
            completion_evidence.get("tool_records")
        )
        required_records = [
            record
            for record in attempt_candidates
            if _extract_tool_call_name(record).casefold() in required_names
        ]
    if not required_records:
        return False, []

    # Every required operation must have a concrete successful record in this
    # logical attempt.  Optional failures elsewhere in the audit history are
    # deliberately ignored here.
    if not _latest_required_records_successful(
        required_records,
        set(required_names),
    ):
        return False, []

    attempt_records = _completion_evidence_records(
        completion_evidence.get("tool_records")
    )
    if attempt_records:
        if not _latest_required_records_successful(
            attempt_records,
            set(required_names),
        ):
            return False, []
    else:
        # The required records themselves are the minimum attempt evidence for
        # legacy producers that do not expose the full attempt list.
        attempt_records = list(required_records)

    # A create receipt cannot authorize a turn that also committed another
    # deterministic task mutation.  Check both the attempt-local sequence and
    # any ordered audit records supplied by the caller; optional failures are
    # harmless, but a successful second side effect is ambiguous and must
    # remain fail-closed.
    if _simple_task_has_unexpected_successful_mutation(
        [*list(audit_tool_calls or ()), *attempt_records]
    ):
        return False, []

    if "create_task" in requested_mutations:
        # A task create is authoritative only after the duplicate-candidate
        # search also succeeded with no candidates.  A producer may publish
        # that fact directly; otherwise retain the search record from the
        # audit ledger as the independent precondition.  ``required_tool_records``
        # intentionally contains only mutation records.
        duplicate_search_succeeded = completion_evidence.get(
            "duplicate_search_succeeded"
        )
        if duplicate_search_succeeded is not None:
            if duplicate_search_succeeded is not True:
                return False, []
        explicit_search_records = _completion_evidence_records(
            completion_evidence.get("search_records")
        )
        if duplicate_search_succeeded is True and explicit_search_records:
            # The producer has already associated this successful empty search
            # with the mutation chain.  Do not let a later optional search
            # failure in the cumulative audit ledger replace that proof.
            search_records = explicit_search_records
        else:
            search_records = explicit_search_records
            search_records.extend(list(audit_tool_calls or ()))
            search_records.extend(attempt_records)
        if successful_empty_task_search(search_records) is not True:
            return False, []
    if _simple_task_post_create_has_blocked_work(
        attempt_records if attempt_records else list(audit_tool_calls or ())
    ):
        return False, []

    return True, attempt_records


def _response_claims_unproven_task_creation(
    response: str | None,
    *,
    user_input: str | None,
    audit_tool_calls: Sequence[Any] | None,
    completion_evidence: CompletionEvidence | None,
) -> bool:
    """Reject a success sentence when no authoritative create proof exists."""

    request = str(user_input or "")
    if not _simple_task_request_is_explicit(request):
        return False
    if requested_deterministic_task_mutation_tools(request) != {"create_task"}:
        return False
    semantic_completion, _ = _authoritative_task_mutation_completion(
        completion_evidence,
        user_input=user_input,
        audit_tool_calls=audit_tool_calls,
    )
    if completion_evidence is None:
        semantic_completion = (
            simple_task_mutation_completion_state(
                user_input,
                audit_tool_calls,
            )
            is SimpleTaskMutationCompletionState.COMPLETE
        )
    if semantic_completion:
        return False
    text = str(response or "").strip().casefold()
    if not text:
        return False
    if any(
        marker in text
        for marker in (
            "失敗",
            "できません",
            "できなかった",
            "未完了",
            "作成していません",
            "作成しません",
            "作成できません",
            "not created",
            "failed",
            "cannot create",
        )
    ):
        return False
    return any(
        marker in text
        for marker in (
            "作成しました",
            "作成済み",
            "作成完了",
            "登録しました",
            "登録済み",
            "登録完了",
            "登録完了しました",
            "追加しました",
            "追加済み",
            "追加完了",
            "追加完了しました",
            "登録されています",
            "追加されています",
            "作成されています",
            "タスクを作成",
            "タスクが登録",
            "タスクが追加",
            "タスク登録完了",
            "タスク追加完了",
            "タスク作成完了",
            "created the task",
            "task created",
            "task successfully created",
            "task has been created",
            "task is created",
            "created successfully",
            "registered the task",
            "added the task",
        )
    )


def agentic_review_short_circuits_done(
    *,
    client: object,
    user_input: str | None,
    response: str | None,
    completion_confirmed: bool = False,
    audit_tool_calls: Sequence[Any] | None = None,
    completion_evidence: CompletionEvidence | None = None,
) -> bool:
    """Skip model review only for a mechanically confirmed complete tool turn.

    ``completion_confirmed`` is produced by the provider/tool loop only after
    it reached a normal final stop with successful tool executions.  An
    attempt-local ``completion_evidence`` mapping is an explicit alternative
    for deterministic task mutations: it can prove that the required mutation
    and postcondition succeeded even when the all-record audit ledger contains
    an optional/superseded failure.  It is necessary but deliberately not
    sufficient:

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

    semantic_completion, semantic_records = _authoritative_task_mutation_completion(
        completion_evidence,
        user_input=user_input,
        audit_tool_calls=audit_tool_calls,
    )
    # Older/local providers do not publish the richer attempt-local mapping.
    # Derive the same narrow semantic state from their audit records rather
    # than allowing a generic completion boolean to claim a compact or
    # malformed task receipt.  The full audit list is never filtered.
    if completion_evidence is None:
        semantic_state = simple_task_mutation_completion_state(
            user_input,
            audit_tool_calls,
        )
        if semantic_state is SimpleTaskMutationCompletionState.COMPLETE:
            semantic_completion = True
            semantic_records = list(audit_tool_calls or ())
        elif _simple_task_duplicate_search_blocked(
            user_input,
            _completion_evidence_records_for_search(
                completion_evidence,
                audit_tool_calls,
            ),
        ):
            # An ambiguous/failed duplicate search is a hard mutation
            # boundary.  Do not let a provider's generic completion boolean or
            # reviewer wording turn that state into a successful create.
            return False
    request_text = str(user_input or "")
    requested_task_mutations = requested_deterministic_task_mutation_tools(
        request_text
    )
    if (
        requested_task_mutations
        and not _simple_task_request_is_explicit(request_text)
        and any(
            _extract_tool_call_name(record).casefold()
            in DETERMINISTIC_TASK_MUTATION_TOOLS
            for record in _completion_evidence_records_for_search(
                completion_evidence,
                audit_tool_calls,
            )
        )
    ):
        # A task-shaped result embedded in artifact/project/multi-scope work
        # cannot use the simple-create fast path, even if the provider's
        # generic completion bit says the latest call stopped normally.
        return False
    if not completion_confirmed and not semantic_completion:
        return False

    # When TurnExecution supplies a semantic ledger for a task request, a
    # non-authoritative ledger is an explicit statement that the required
    # mutation/postcondition is not proven.  Do not fall back to the legacy
    # all-success completion marker in that case (which would otherwise treat
    # a compact ``{id: ...}`` receipt as success).  Legacy callers that do not
    # provide an evidence mapping retain their historical behavior.
    if (
        isinstance(completion_evidence, Mapping)
        and completion_evidence.get("authoritative") is not True
        and requested_deterministic_task_mutation_tools(str(user_input or ""))
        & DETERMINISTIC_TASK_MUTATION_TOOLS
    ):
        return False

    text = str(response or "").strip()
    if not text and not semantic_completion:
        return False

    records = list(audit_tool_calls or ())
    if semantic_completion and not records:
        records = list(semantic_records)
    if not records:
        # Never trust a boolean completion marker without the run's concrete
        # tool evidence.  This also prevents stale/shared client state from
        # suppressing review.
        return False
    # The ordinary completion marker still requires an all-successful audit
    # ledger.  An attempt-local authoritative mutation proof is the explicit
    # exception: unrelated/superseded failures remain in ``records`` for
    # audit/UI, but do not reopen an already satisfied task request.
    if not semantic_completion and any(
        not _audit_tool_call_successful(record) for record in records
    ):
        return False

    policy = get_client_generation_policy(client)
    if policy.profile == GenerationProfile.REVIEW:
        return False

    request = str(user_input or "")
    if project_progress_review_active(request):
        return False

    explicit_capabilities = command_capabilities_from_text(request)
    # ``task_update`` is the trusted command capability used by the structured
    # task mutation controller.  Once its attempt-local postcondition proof is
    # authoritative, it must not force a redundant verifier call.  Other
    # explicit capabilities (web/artifact/progress/review work) remain
    # verification-sensitive and keep the fail-closed reviewer pass.
    verification_sensitive_capabilities = (
        EXPLICIT_COMPLETION_CAPABILITIES - {"task_update"}
    )
    if explicit_capabilities.intersection(verification_sensitive_capabilities):
        return False
    if "task_update" in explicit_capabilities and not semantic_completion:
        return False

    if not semantic_completion and response_looks_like_incomplete_final_answer(text):
        return False

    if text and response_definitely_incomplete_after_review(
        text,
        user_input=user_input,
        audit_tool_calls=records,
        completion_evidence=completion_evidence,
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
        "review_protocol_error": True,
    }


def parse_agentic_review_decision(
    content: str,
) -> dict[str, str | bool | None]:
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
        "review_protocol_error": False,
    }


def _reviewer_echoed_latest_response(
    decision: dict[str, str | bool | None],
    review_response: Any,
    latest_response: Any,
) -> bool:
    """Fail closed when a provider ignored the verifier prompt entirely."""

    if decision.get("review_protocol_error") is not True:
        return False
    review_text = str(review_response or "").strip()
    latest_text = str(latest_response or "").strip()
    return bool(review_text and latest_text and review_text == latest_text)


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
    completion_evidence_provider: CompletionEvidenceProvider | None = None,
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
    completion_evidence: CompletionEvidence | None = None
    audit_tool_calls: Sequence[Any] | None = None
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
        completion_evidence = (
            completion_evidence_provider()
            if completion_evidence_provider is not None
            else None
        )
        if agentic_review_short_circuits_done(
            client=client,
            user_input=user_input,
            response=response,
            completion_confirmed=completion_confirmed,
            audit_tool_calls=audit_tool_calls,
            completion_evidence=completion_evidence,
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
            parsed_decision = parse_agentic_review_decision(
                str(review_response or "")
            )
            reviewer_echo = _reviewer_echoed_latest_response(
                parsed_decision, review_response, response
            )
            decision = parsed_decision
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
                completion_evidence=completion_evidence,
            )
        if response_promises_future_tool_use(response) and not completion_confirmed:
            reviewer_echo = False
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
        if reviewer_echo:
            break
        if decision["status"] != "continue":
            review_verified = True
            break
        if _task_mutation_postcondition_failed(
            completion_evidence,
            user_input=user_input,
            audit_tool_calls=audit_tool_calls,
        ) or (
            (
                not isinstance(completion_evidence, Mapping)
                or completion_evidence.get("authoritative") is not True
            )
            and _simple_task_duplicate_search_blocked(
                user_input,
                _completion_evidence_records_for_search(
                    completion_evidence,
                    audit_tool_calls,
                ),
            )
        ) or _response_claims_unproven_task_creation(
            response,
            user_input=user_input,
            audit_tool_calls=audit_tool_calls,
            completion_evidence=completion_evidence,
        ):
            # A required mutation was attempted but its receipt is explicitly
            # incomplete.  Do not launch an automatic update/retry that could
            # amplify the original mutation; surface the fail-closed result.
            review_verified = False
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

    # Refresh the attempt-local providers after any continuation (and even
    # when the configured review budget is zero).  A continuation can repair a
    # task receipt, while a pre-proven deterministic mutation must not be
    # downgraded merely because the verifier loop had no rounds.
    if audit_tool_calls_provider is not None:
        audit_tool_calls = audit_tool_calls_provider()
    if completion_evidence_provider is not None:
        completion_evidence = completion_evidence_provider()

    postcondition_failure = _task_mutation_postcondition_failed(
        completion_evidence,
        user_input=user_input,
        audit_tool_calls=audit_tool_calls,
    )
    semantic_completion, _ = _authoritative_task_mutation_completion(
        completion_evidence,
        user_input=user_input,
        audit_tool_calls=audit_tool_calls,
    )
    if postcondition_failure:
        # Do not echo a model sentence claiming that the task was created when
        # its receipt did not prove the requested fields.
        response = build_incomplete_work_failure_response(
            user_input=user_input,
            latest_response="",
        )
    elif not review_verified and not semantic_completion:
        response = build_incomplete_work_failure_response(
            user_input=user_input,
            latest_response=(
                ""
                if _response_claims_unproven_task_creation(
                    response,
                    user_input=user_input,
                    audit_tool_calls=audit_tool_calls,
                    completion_evidence=completion_evidence,
                )
                else response
            ),
        )
    elif response_looks_like_unfinished_work(user_input, response) and not (
        semantic_completion and not str(response or "").strip()
    ):
        response = build_incomplete_work_failure_response(
            user_input=user_input,
            latest_response=response,
        )

    if (
        max_rounds == 0
        and response_looks_like_unfinished_work(user_input, response)
        and not (semantic_completion and not str(response or "").strip())
    ):
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
    completion_evidence_provider: CompletionEvidenceProvider | None = None,
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
    completion_evidence: CompletionEvidence | None = None
    audit_tool_calls: Sequence[Any] | None = None
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
        completion_evidence = (
            completion_evidence_provider()
            if completion_evidence_provider is not None
            else None
        )
        if agentic_review_short_circuits_done(
            client=client,
            user_input=user_input,
            response=response,
            completion_confirmed=completion_confirmed,
            audit_tool_calls=audit_tool_calls,
            completion_evidence=completion_evidence,
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
            parsed_decision = parse_agentic_review_decision(
                str(review_response or "")
            )
            reviewer_echo = _reviewer_echoed_latest_response(
                parsed_decision, review_response, response
            )
            decision = parsed_decision
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
                completion_evidence=completion_evidence,
            )
        if response_promises_future_tool_use(response) and not completion_confirmed:
            reviewer_echo = False
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
        if reviewer_echo:
            break
        if decision["status"] != "continue":
            review_verified = True
            break
        if _task_mutation_postcondition_failed(
            completion_evidence,
            user_input=user_input,
            audit_tool_calls=audit_tool_calls,
        ) or (
            (
                not isinstance(completion_evidence, Mapping)
                or completion_evidence.get("authoritative") is not True
            )
            and _simple_task_duplicate_search_blocked(
                user_input,
                _completion_evidence_records_for_search(
                    completion_evidence,
                    audit_tool_calls,
                ),
            )
        ) or _response_claims_unproven_task_creation(
            response,
            user_input=user_input,
            audit_tool_calls=audit_tool_calls,
            completion_evidence=completion_evidence,
        ):
            # Do not issue an automatic follow-up mutation after an explicit
            # postcondition failure; return a clear fail-closed response.
            review_verified = False
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

    # Refresh the terminal attempt evidence before applying fail-closed checks
    # (the async loop above follows the same rule).
    if audit_tool_calls_provider is not None:
        audit_tool_calls = audit_tool_calls_provider()
    if completion_evidence_provider is not None:
        completion_evidence = completion_evidence_provider()

    postcondition_failure = _task_mutation_postcondition_failed(
        completion_evidence,
        user_input=user_input,
        audit_tool_calls=audit_tool_calls,
    )
    semantic_completion, _ = _authoritative_task_mutation_completion(
        completion_evidence,
        user_input=user_input,
        audit_tool_calls=audit_tool_calls,
    )
    if postcondition_failure:
        # Keep an explicitly unverified task receipt fail-closed even when the
        # final work-profile continuation was allowed to return normally; do
        # not echo its stale success claim in the failure response.
        response = build_incomplete_work_failure_response(
            user_input=user_input,
            latest_response="",
        )
    elif not review_verified and not semantic_completion:
        response = build_incomplete_work_failure_response(
            user_input=user_input,
            latest_response=(
                ""
                if _response_claims_unproven_task_creation(
                    response,
                    user_input=user_input,
                    audit_tool_calls=audit_tool_calls,
                    completion_evidence=completion_evidence,
                )
                else response
            ),
        )
    elif response_looks_like_unfinished_work(user_input, response) and not (
        semantic_completion and not str(response or "").strip()
    ):
        response = build_incomplete_work_failure_response(
            user_input=user_input,
            latest_response=response,
        )

    if (
        max_rounds == 0
        and response_looks_like_unfinished_work(user_input, response)
        and not (semantic_completion and not str(response or "").strip())
    ):
        response = build_incomplete_work_failure_response(
            user_input=user_input,
            latest_response=response,
        )

    return response
