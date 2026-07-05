"""Runtime policy for deciding whether a tool call should execute."""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Optional

from .generation_policy import GenerationProfile, get_current_generation_policy


_current_user_input: ContextVar[Optional[str]] = ContextVar(
    "tool_policy_current_user_input",
    default=None,
)

VALID_COMMAND_CAPABILITIES: set[str] = {
    "web_search",
    "image_generation",
    "project_db_update",
    "project_progress_review",
    "task_update",
    "wbs_sync",
}

PROJECT_COMMAND_CAPABILITIES: set[str] = {
    "project_db_update",
    "project_progress_review",
    "task_update",
    "wbs_sync",
}

COMMAND_CAPABILITY_CONTEXT_HEADER = "## AoiTalk Command Context"
COMMAND_CAPABILITY_LINE_PREFIX = "Command capabilities:"


@dataclass(frozen=True)
class ToolPolicyDecision:
    allowed: bool
    reason: str


PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES: set[str] = {
    "organize_project_information_from_folder",
    "patch_project_information_doc",
    "attach_project_information_reference",
    "upsert_project_qa_entry",
    "archive_project_qa_entry",
    "configure_project_management_files",
    "create_record_table",
    "append_record_rows",
    "update_record_row",
    "delete_record_rows",
    "delete_record_table",
    "create_task",
    "update_task",
    "delete_task",
    "assign_task",
    "schedule_task",
    "start_timer",
    "stop_timer",
    "log_time",
    "sync_issue_table",
    "sync_wbs_tasks",
}

PROJECT_MANAGEMENT_READ_TOOL_NAMES: set[str] = {
    "get_project_context",
    "list_projects",
    "list_record_tables",
    "list_project_information",
    "render_project_diagram",
    "list_project_tasks_changed_since",
    "list_tasks",
    "list_calendar",
    "get_time_report",
    "get_project_issues",
    "get_project_progress",
    "get_upcoming_wbs_tasks",
    "summarize_project_requests",
}

PROJECT_MANAGEMENT_TOOL_NAMES: set[str] = (
    PROJECT_MANAGEMENT_READ_TOOL_NAMES | PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES
)

DOCS_MUTATION_TOOL_NAMES: set[str] = {
    "docs_create_nodes",
    "docs_update_node",
    "docs_set_fields",
    "docs_add_tag",
    "docs_remove_tag",
    "docs_move_node",
    "docs_archive_node",
}

DOCS_READ_TOOL_NAMES: set[str] = {
    "docs_search",
    "docs_outline",
    "docs_query",
}

DOCS_TOOL_NAMES: set[str] = DOCS_READ_TOOL_NAMES | DOCS_MUTATION_TOOL_NAMES

SEARCH_TOOL_NAMES: set[str] = {
    "web_search",
    "grok_x_search",
    "knowledge_search",
    "knowledge_read",
    "knowledge_status",
    "search_memory",
}

FILESYSTEM_READ_TOOL_NAMES: set[str] = {
    "list_workspace_files",
    "find_workspace_items",
    "inspect_workspace_tree",
    "read_workspace_file",
    "get_workspace_file_info",
    "download_user_file",
    "list_user_files",
    "get_user_file_info",
    "execute_command",
    "view_file",
    "list_directory",
    "search_files",
    "get_repo_map",
}

FILESYSTEM_MUTATION_TOOL_NAMES: set[str] = {
    "create_workspace_directory",
    "upload_workspace_file",
    "delete_workspace_item",
    "move_workspace_item",
    "upload_user_file",
    "delete_user_file",
    "create_file",
    "delete_file",
    "append_to_file",
    "edit_file",
    "insert_to_file",
    "undo_edit",
}

FILESYSTEM_TOOL_NAMES: set[str] = (
    FILESYSTEM_READ_TOOL_NAMES | FILESYSTEM_MUTATION_TOOL_NAMES
)


def set_current_user_input(user_input: Optional[str]) -> Token:
    return _current_user_input.set(user_input)


def reset_current_user_input(token: Token) -> None:
    _current_user_input.reset(token)


def get_current_user_input() -> Optional[str]:
    return _current_user_input.get()


def sanitize_command_capabilities(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    raw_values: list[Any]
    if isinstance(value, str):
        raw_values = re.split(r"[, \t\r\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        return ()

    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        item = str(raw or "").strip().lower()
        if item not in VALID_COMMAND_CAPABILITIES or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def command_capabilities_from_text(text: str) -> set[str]:
    raw = str(text or "")
    if COMMAND_CAPABILITY_LINE_PREFIX not in raw:
        return set()
    capabilities: set[str] = set()
    for line in raw.splitlines():
        if not line.startswith(COMMAND_CAPABILITY_LINE_PREFIX):
            continue
        _, raw_caps = line.split(":", 1)
        capabilities.update(sanitize_command_capabilities(raw_caps))
    return capabilities


def command_capability_active(text: str, capability: str) -> bool:
    return capability in command_capabilities_from_text(text)


def build_command_capability_context(
    message: str,
    capabilities: Any,
) -> str:
    sanitized = sanitize_command_capabilities(capabilities)
    if not sanitized:
        return message

    guidance: list[str] = []
    if "web_search" in sanitized:
        guidance.append(
            "- `web_search`: use direct public web search tools before answering."
        )
        guidance.append(
            "- Choose the search query from the current user request and the "
            "provided conversation history, then call `web_search`; do not use "
            "the slash command text itself as the query."
        )
    if "image_generation" in sanitized:
        guidance.append(
            "- `image_generation`: use the media/image generation tool path; do not answer as plain text only."
        )
    if "project_db_update" in sanitized:
        guidance.append(
            "- `project_db_update`: use direct project information Docs tools for durable project knowledge."
        )
        guidance.append(
            "- Before writing project information Docs, read `list_project_information`, preserve existing headings, patch the relevant section/block with `patch_project_information_doc`, and include `change_summary` plus `source_refs_json` when evidence exists."
        )
        guidance.append(
            "- Do not write unsupported claims as settled body text; put them under 要確認 or create an unanswered candidate Q&A."
        )
    if "project_progress_review" in sanitized:
        guidance.append(
            "- `project_progress_review`: run an evidence-driven project progress review for the current project."
        )
        guidance.append(
            "- Start from `get_project_progress`, then keep using project, record-table, task, file, and web-search tools as needed. Do not stop after the first tool result if evidence is insufficient, stale, or changed by a DB update."
        )
        guidance.append(
            "- If project evidence is missing or stale, inspect/refresh the selected project filer root with `organize_project_information_from_folder` using `apply=true` when appropriate, then re-run `get_project_progress` before the final answer."
        )
    if "task_update" in sanitized:
        guidance.append(
            "- `task_update`: use direct task tools when creating, updating, or organizing tasks."
        )
    if "wbs_sync" in sanitized:
        guidance.append(
            "- `wbs_sync`: use direct WBS/project task synchronization tools."
        )

    return "\n".join(
        [
            COMMAND_CAPABILITY_CONTEXT_HEADER,
            f"{COMMAND_CAPABILITY_LINE_PREFIX} {', '.join(sanitized)}",
            *guidance,
            "",
            "Current user request:",
            message,
        ]
    )


def command_capabilities_for_current_turn_text(
    text: str,
    capabilities: Any = None,
) -> tuple[str, ...]:
    """Add command capabilities implied by the current user turn only."""
    sanitized = sanitize_command_capabilities(capabilities)
    if "web_search" not in sanitized and _looks_like_search_request(text):
        return (*sanitized, "web_search")
    return sanitized


def looks_like_filesystem_request(text: str) -> bool:
    return _looks_like_filesystem_request(text)


def looks_like_project_management_request(text: str) -> bool:
    return _looks_like_project_management_request(text)


def looks_like_project_progress_review_request(text: str) -> bool:
    return _looks_like_project_progress_review_request(text)


def project_progress_review_active(text: str) -> bool:
    return _looks_like_project_progress_review_request(text)


def looks_like_project_management_mutation_request(text: str) -> bool:
    return bool(project_management_required_mutation_tools(text))


def looks_like_deferred_project_fact_request(text: str) -> bool:
    policy_text = _extract_effective_user_request(text)
    normalized = policy_text.casefold()
    if not normalized.strip():
        return False

    features = _project_management_fact_features(normalized)
    if not features["has_durable_project_fact"] or features["is_lookup_only"]:
        return False

    # Explicit project-information update requests are handled synchronously
    # by the root direct project tools. This helper only identifies incidental
    # durable notes that accompany another primary action.
    return "patch_project_information_doc" not in project_management_required_mutation_tools(
        policy_text
    )


def looks_like_utility_request(text: str) -> bool:
    return _looks_like_utility_request(text)


def looks_like_media_request(text: str) -> bool:
    return _looks_like_media_request(text)


def looks_like_search_request(text: str) -> bool:
    return _looks_like_search_request(text)


def looks_like_memory_request(text: str) -> bool:
    return _looks_like_memory_request(text)


def looks_like_bare_search_followup_request(text: str) -> bool:
    return _looks_like_bare_search_followup_request(text)


def _extract_effective_user_request(text: str) -> str:
    raw = str(text or "")
    markers = (
        "\nUser request:\n",
        "\r\nUser request:\r\n",
        "User request:\n",
        "\nCurrent user request:\n",
        "\r\nCurrent user request:\r\n",
        "Current user request:\n",
    )
    for marker in markers:
        if marker in raw:
            return raw.rsplit(marker, 1)[-1].strip()
    return raw.strip()


def project_management_required_mutation_tools(text: str) -> set[str]:
    command_capabilities = command_capabilities_from_text(text)
    command_tools: set[str] = set()
    if "project_db_update" in command_capabilities:
        command_tools.update(
            {
                "organize_project_information_from_folder",
                "patch_project_information_doc",
                "attach_project_information_reference",
            }
        )
    if "task_update" in command_capabilities:
        command_tools.update({"create_task", "update_task"})
    if "wbs_sync" in command_capabilities:
        command_tools.add("sync_wbs_tasks")
    if command_tools:
        return command_tools

    policy_text = _extract_effective_user_request(text)
    normalized = policy_text.casefold()
    if not normalized.strip():
        return set()

    task_terms = (
        "タスク",
    )
    project_info_terms = (
        "案件情報",
        "プロジェクト情報",
        "案件情報docs",
        "プロジェクト情報docs",
        "案件情報db",
        "案件情報DB",
        "案件db",
        "案件DB",
        "プロジェクトdb",
        "プロジェクトDB",
    )
    record_table_terms = (
        "レコードテーブル",
        "dbテーブル",
        "DBテーブル",
        "台帳",
        "一覧表",
        "案件情報db",
        "案件情報DB",
        "案件db",
        "案件DB",
    )
    database_terms = (
        "db",
        "DB",
        "データベース",
        "docs",
        "台帳",
        "一覧表",
    )
    project_database_phrases = (
        "プロジェクト専用db",
        "プロジェクトdb",
        "専用db",
        "案件専用db",
        "案件db",
        "案件情報db",
        "案件情報docs",
        "プロジェクト情報docs",
        "プロジェクトDB",
        "案件DB",
        "案件情報DB",
    )
    wbs_terms = (
        "WBS",
        "工程表",
    )
    issue_terms = (
        "課題管理",
        "課題管理表",
    )
    durable_fact_terms = (
        "決定",
        "確定",
        "決まった",
        "要確認",
        "未確認",
        "見込み",
        "らしい",
        "かもしれない",
        "リスク",
        "課題",
        "遅れ",
        "遅延",
        "延期",
        "前倒し",
        "変更になった",
    )
    lookup_terms = (
        "\u4eca\u65e5",
        "\u672c\u65e5",
        "\u671f\u9650",
        "\u4f55",
        "\u6559\u3048\u3066",
        "\u4e00\u89a7",
        "\u8868\u793a",
        "\u78ba\u8a8d",
        "教えて",
        "見せて",
        "表示",
        "一覧",
        "知りたい",
        "確認したい",
        "?",
        "？",
    )
    folder_terms = (
        "フォルダ",
        "ワークスペース",
        "ファイラー",
        "資料",
        "ファイル",
    )
    create_terms = (
        "\u4f5c\u6210",
        "\u4f5c\u6210\u3057\u3066",
        "\u4f5c\u3063\u3066",
        "\u8ffd\u52a0",
        "\u767b\u9332",
        "追加",
        "作成",
        "登録",
        "入れて",
        "残して",
        "まとめ",
        "整理",
        "完成",
        "作って",
        "作成して",
        "登録して",
        "反映",
        "db化",
        "DB化",
        "データベース化",
    )
    update_terms = (
        "\u66f4\u65b0",
        "\u5909\u66f4",
        "\u4fee\u6b63",
        "\u5b8c\u4e86",
        "更新",
        "変更",
        "修正",
        "完了",
        "完成",
        "整理",
        "同期",
        "反映",
    )
    delete_terms = (
        "\u524a\u9664",
        "\u6d88\u3057",
        "消し",
        "削除",
    )
    schedule_terms = (
        "\u671f\u9650",
        "\u4e88\u5b9a",
        "\u30b9\u30b1\u30b8\u30e5\u30fc\u30eb",
        "\u30ab\u30ec\u30f3\u30c0\u30fc",
        "スケジュール",
        "カレンダー",
    )
    explicit_fact_persistence_terms = (
        "残して",
        "登録",
        "記録",
        "保存",
        "覚えて",
        "メモ",
        "案件情報",
        "プロジェクト情報",
    )
    fact_note_persistence_terms = (
        "残して",
        "記録",
        "覚えて",
        "メモ",
    )
    database_fact_persistence_terms = (*fact_note_persistence_terms, "保存")

    has_task = any(term.casefold() in normalized for term in task_terms)
    has_project_info = any(term.casefold() in normalized for term in project_info_terms)
    has_record_table = any(term.casefold() in normalized for term in record_table_terms)
    has_wbs = any(term.casefold() in normalized for term in wbs_terms)
    has_issue = any(term.casefold() in normalized for term in issue_terms)
    has_folder = any(term.casefold() in normalized for term in folder_terms)
    has_database_reference = any(term.casefold() in normalized for term in database_terms)
    has_fact_note_persistence = any(
        term.casefold() in normalized for term in fact_note_persistence_terms
    )
    has_database_fact_persistence = any(
        term.casefold() in normalized for term in database_fact_persistence_terms
    )
    has_project_reference = _contains_any(
        normalized,
        ("案件", "プロジェクト"),
    )
    has_durable_project_fact = (
        (has_project_reference or has_project_info or has_wbs or has_issue)
        and (
            any(term.casefold() in normalized for term in durable_fact_terms)
            or has_fact_note_persistence
        )
    )
    has_project_info_database = has_project_info and any(
        term.casefold() in normalized for term in database_terms
    )
    has_create_or_update = any(
        term.casefold() in normalized for term in create_terms + update_terms
    )
    is_short_database_update = (
        has_database_reference
        and has_create_or_update
        and not has_task
        and not has_database_fact_persistence
        and len(normalized.strip()) <= 80
    )
    has_project_database = has_database_reference and (
        has_project_info_database
        or has_record_table
        or is_short_database_update
        or any(term.casefold() in normalized for term in project_database_phrases)
    )
    is_lookup_only = (
        any(term.casefold() in normalized for term in lookup_terms)
        and not has_create_or_update
    )
    tools: set[str] = set()
    if has_task and any(term.casefold() in normalized for term in create_terms):
        tools.add("create_task")
    if has_task and any(term.casefold() in normalized for term in update_terms):
        tools.add("update_task")
    if has_task and any(term.casefold() in normalized for term in delete_terms):
        tools.add("delete_task")
    if any(term.casefold() in normalized for term in schedule_terms) and any(
        term.casefold() in normalized for term in create_terms + update_terms
    ):
        tools.add("schedule_task")
    if (has_wbs or has_project_database) and has_create_or_update:
        tools.add("sync_wbs_tasks")
    if (has_issue or has_project_database) and has_create_or_update:
        tools.add("sync_issue_table")
    if (has_project_info and has_folder and has_create_or_update) or (
        has_project_database and has_create_or_update
    ):
        tools.add("organize_project_information_from_folder")
    if has_project_info and has_create_or_update:
        tools.add("patch_project_information_doc")
    if (
        has_durable_project_fact
        and not is_lookup_only
        and any(term.casefold() in normalized for term in explicit_fact_persistence_terms)
        and not (has_task or has_wbs or has_issue or has_record_table)
    ):
        tools.add("patch_project_information_doc")
    if (
        has_database_reference
        and has_database_fact_persistence
        and not is_lookup_only
        and not (has_task or has_wbs or has_issue or has_record_table)
    ):
        tools.add("patch_project_information_doc")
    if (has_record_table or has_project_database) and has_create_or_update:
        tools.add("create_record_table")
    return tools


def _project_management_fact_features(normalized: str) -> dict[str, bool]:
    project_info_terms = (
        "案件情報",
        "プロジェクト情報",
        "案件情報docs",
        "プロジェクト情報docs",
        "案件情報db",
        "案件情報DB",
        "案件db",
        "案件DB",
        "プロジェクトdb",
        "プロジェクトDB",
    )
    wbs_terms = (
        "WBS",
        "工程表",
    )
    issue_terms = (
        "課題管理",
        "課題管理表",
    )
    durable_fact_terms = (
        "決定",
        "確定",
        "決まった",
        "要確認",
        "未確認",
        "見込み",
        "らしい",
        "かもしれない",
        "リスク",
        "課題",
        "遅れ",
        "遅延",
        "延期",
        "前倒し",
        "変更になった",
    )
    lookup_terms = (
        "教えて",
        "見せて",
        "表示",
        "一覧",
        "知りたい",
        "確認したい",
        "?",
        "？",
    )
    create_terms = (
        "追加",
        "作成",
        "登録",
        "入れて",
        "残して",
        "まとめ",
        "整理",
        "完成",
        "作って",
        "反映",
        "db化",
        "DB化",
    )
    update_terms = (
        "更新",
        "変更",
        "修正",
        "完了",
        "完成",
        "整理",
        "同期",
        "反映",
    )

    has_project_info = any(term.casefold() in normalized for term in project_info_terms)
    has_wbs = any(term.casefold() in normalized for term in wbs_terms)
    has_issue = any(term.casefold() in normalized for term in issue_terms)
    has_project_reference = _contains_any(
        normalized,
        ("案件", "プロジェクト"),
    )
    has_durable_project_fact = (
        (has_project_reference or has_project_info or has_wbs or has_issue)
        and any(term.casefold() in normalized for term in durable_fact_terms)
    )
    has_create_or_update = any(
        term.casefold() in normalized for term in create_terms + update_terms
    )
    is_lookup_only = (
        any(term.casefold() in normalized for term in lookup_terms)
        and not has_create_or_update
    )
    return {
        "has_durable_project_fact": has_durable_project_fact,
        "is_lookup_only": is_lookup_only,
    }


def is_memory_search_enabled(config: Any) -> bool:
    if config is None:
        return True
    memory = config.get("memory", {}) if hasattr(config, "get") else {}
    if not isinstance(memory, dict):
        return False
    return bool(memory.get("enabled", True) and memory.get("enable_search", False))


def check_tool_call_allowed(
    tool_name: str,
    *,
    user_input: Optional[str] = None,
    tool_args: Optional[dict[str, Any]] = None,
    config: Any = None,
) -> ToolPolicyDecision:
    text = _combined_text(user_input, tool_args)
    policy = get_current_generation_policy()

    if policy.profile == GenerationProfile.REVIEW and _looks_like_mutation_tool_call(
        tool_name,
        text,
    ):
        return ToolPolicyDecision(
            False,
            "review mode does not allow mutation-capable tool calls",
        )

    if tool_name == "search_memory":
        if not is_memory_search_enabled(config):
            return ToolPolicyDecision(
                False,
                "memory semantic search is disabled in configuration",
            )
        if not _looks_like_memory_request(text):
            return ToolPolicyDecision(
                False,
                "the request does not depend on prior conversation history",
            )
        return ToolPolicyDecision(True, "request refers to prior conversation history")

    if tool_name == "utility_assistant":
        if _looks_like_utility_request(text):
            return ToolPolicyDecision(True, "request explicitly asks for time, weather, or calculation work")
        return ToolPolicyDecision(
            False,
            "the request does not ask for time, weather, or calculation work",
        )

    return ToolPolicyDecision(True, "tool is not restricted by runtime policy")


def format_blocked_tool_result(tool_name: str, decision: ToolPolicyDecision) -> str:
    return (
        f"Tool policy blocked `{tool_name}`: {decision.reason}. "
        f"Do not call `{tool_name}` again for this user request. "
        "Answer directly, or use direct search tools only when public, fresh, or time-sensitive information is required."
    )


def _combined_text(user_input: Optional[str], tool_args: Optional[dict[str, Any]]) -> str:
    if user_input and str(user_input).strip():
        return str(user_input).strip()
    return _tool_args_text(tool_args)


def _tool_args_text(tool_args: Optional[dict[str, Any]]) -> str:
    parts: list[str] = []
    if tool_args:
        for value in tool_args.values():
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts).strip()


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(term.casefold() in lowered for term in terms)


def _looks_like_mutation_tool_call(tool_name: str, text: str) -> bool:
    normalized = str(text or "")
    if tool_name in PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES:
        return True
    if tool_name in DOCS_MUTATION_TOOL_NAMES:
        return True
    if tool_name in FILESYSTEM_MUTATION_TOOL_NAMES:
        return True
    if tool_name == "execute_command":
        return _contains_any(
            normalized,
            (
                "create",
                "delete",
                "remove",
                "move",
                "edit",
                "append",
                "insert",
                "save",
                "write",
                "upload",
                "作成",
                "削除",
                "移動",
                "編集",
                "追記",
                "保存",
                "書き込",
                "アップロード",
            ),
        )
    return False


def _looks_like_filesystem_followup_request(
    user_input: str,
    tool_args: Optional[dict[str, Any]],
) -> bool:
    args_text = _tool_args_text(tool_args)
    if not user_input or not args_text or not _looks_like_filesystem_request(args_text):
        return False
    return _contains_any(
        user_input,
        (
            "\u30bb\u30b0\u30e1\u30f3\u30c8",
            "\u30bb\u30b0\u30e1\u30f3\u30c8\u8868",
            "\u69cb\u6210",
            "\u8a2d\u5b9a",
            "\u8a2d\u8a08",
            "\u30d1\u30e9\u30e1\u30fc\u30bf",
            "\u30b3\u30f3\u30d5\u30a3\u30b0",
            "\u8868\u51fa\u529b",
            "\u8868\u306b",
            "\u4e00\u89a7",
            "\u62bd\u51fa",
        ),
    )


def _looks_like_memory_request(text: str) -> bool:
    return _contains_any(
        text,
        (
            "前回",
            "以前",
            "過去",
            "この前",
            "さっき",
            "覚えて",
            "記憶",
            "会話履歴",
            "話した",
            "言った",
            "remember",
            "previously mentioned",
            "told you",
        ),
    )


def _looks_like_search_request(text: str) -> bool:
    if command_capability_active(text, "web_search"):
        return True

    normalized = str(text or "").casefold()
    if not normalized.strip():
        return False

    explicit_web_terms = (
        "Web検索",
        "web検索",
        "ウェブ検索",
        "ネット検索",
        "インターネット検索",
    )
    if _contains_any(normalized, explicit_web_terms):
        return True

    if _looks_like_filesystem_request(normalized):
        return False
    if _looks_like_project_management_request(normalized):
        return False
    if _looks_like_memory_request(normalized):
        return False

    web_research_terms = (
        "検索",
        "調べて",
        "調べる",
        "調査して",
        "調査する",
    )
    return _contains_any(normalized, web_research_terms)


def _looks_like_media_request(text: str) -> bool:
    if command_capability_active(text, "image_generation"):
        return True

    normalized = str(text or "").casefold()
    if not normalized.strip():
        return False
    return _contains_any(
        normalized,
        (
            "画像生成",
            "画像を生成",
            "絵を生成",
            "イラスト生成",
            "image generation",
            "generate image",
            "generate an image",
            "comfyui",
            "youtube",
            "niconico",
            "ニコニコ",
            "bgm",
        ),
    )


def _looks_like_bare_search_followup_request(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False

    compact = re.sub(r"[\s\u3000。、．，,！!？?「」『』（）()\[\]【】\"'`]+", "", raw.casefold())
    exact_japanese = {
        "検索",
        "検索して",
        "検索してね",
        "検索してください",
        "検索しろ",
        "検索してくれ",
        "web検索して",
        "web検索してね",
        "ウェブ検索して",
        "ウェブ検索してね",
        "それ検索して",
        "それを検索して",
        "それを検索してね",
        "これ検索して",
        "これを検索して",
        "調べて",
        "調べてね",
        "調べてください",
        "それ調べて",
        "それを調べて",
        "これ調べて",
        "これを調べて",
        "ちゃんと検索して",
        "ちゃんと検索してね",
    }
    if compact in exact_japanese:
        return True

    english = re.sub(r"[^a-z0-9]+", " ", raw.casefold()).strip()
    exact_english = {
        "search",
        "search it",
        "search that",
        "search this",
        "please search",
        "web search",
        "look it up",
        "look that up",
        "look this up",
        "please look it up",
    }
    return english in exact_english


def _looks_like_filesystem_request(text: str) -> bool:
    normalized = str(text or "")
    if _contains_any(
        text,
        (
            "ファイル",
            "フォルダ",
            "ワークスペース",
            "ファイラー",
            "資料",
            "文書",
            "ドキュメント",
            "設計書",
            "仕様書",
            "議事録",
            "手順書",
            "添付",
            "アップロード",
            "案件資料",
            "案件フォルダ",
        ),
    ):
        return True

    if re.search(
        r"(?i)(^|[\s:：])(?:[A-Za-z0-9_.-]+[\\/])+(?:[A-Za-z0-9_.-]+)?",
        normalized,
    ):
        return True

    if re.search(
        r"(?i)\b[A-Za-z0-9_.-]+\.(?:txt|md|csv|json|docx|xlsx|pptx|pdf|py|ts|tsx|js|jsx|html|css|yaml|yml|toml|ini)\b",
        normalized,
    ):
        return True

    return False


def _looks_like_utility_request(text: str) -> bool:
    normalized = str(text or "").casefold()
    if not normalized.strip():
        return False

    utility_terms = (
        "\u4eca\u306f\u4f55\u6642",
        "\u4eca\u4f55\u6642",
        "\u4f55\u6642",
        "\u73fe\u5728\u6642\u523b",
        "\u73fe\u5728\u306e\u6642\u523b",
        "\u73fe\u5728\u306e\u65e5\u6642",
        "\u4eca\u306e\u6642\u9593",
        "\u4eca\u65e5\u306e\u65e5\u4ed8",
        "\u5929\u6c17",
        "\u6c17\u6e29",
        "\u8a08\u7b97",
        "\u96fb\u5353",
        "what time",
        "current time",
        "current date",
        "current datetime",
        "weather",
        "temperature",
        "calculate",
        "calculator",
    )
    if _contains_any(normalized, utility_terms):
        return True

    # Arithmetic-only requests such as "2+2" or "15% of 320" should go to
    # the utility specialist, but ordinary prose containing a number should not.
    compact = "".join(ch for ch in normalized if not ch.isspace())
    has_operator = any(op in compact for op in ("+", "-", "*", "/", "^", "%", "\u00d7", "\u00f7"))
    has_digit = any(ch.isdigit() for ch in compact)
    return has_operator and has_digit and len(compact) <= 80


def _looks_like_project_management_request(text: str) -> bool:
    if command_capabilities_from_text(text) & PROJECT_COMMAND_CAPABILITIES:
        return True

    if _contains_any(text, ("進捗", "進行状況")) and _contains_any(
        text,
        (
            "案件",
            "プロジェクト",
            "タスク",
            "予定",
            "スケジュール",
        ),
    ):
        return True

    if _contains_any(
        text,
        (
            "案件情報",
            "案件情報Docs",
            "案件情報DB",
            "案件DB",
            "プロジェクト情報",
            "プロジェクト情報Docs",
            "プロジェクトDB",
            "タスク",
            "台帳",
            "WBS",
            "工程表",
            "課題管理",
            "課題管理表",
            "レコードテーブル",
            "DBテーブル",
            "予定",
            "スケジュール",
            "カレンダー",
            "期限",
        ),
    ):
        return True
    if (
        _contains_any(text, ("DB", "データベース"))
        and _contains_any(
            text,
            (
                "更新",
                "整理",
                "作成",
                "登録",
                "反映",
                "保存",
                "記録",
                "メモ",
                "覚えて",
                "残して",
                "DB化",
                "データベース化",
            ),
        )
        and len(str(text or "").strip()) <= 80
    ):
        return True
    return False


def _looks_like_project_progress_review_request(text: str) -> bool:
    if command_capability_active(text, "project_progress_review"):
        return True

    normalized = str(text or "")
    if not normalized.strip():
        return False
    if _contains_any(normalized, ("進捗", "進行状況")) and _contains_any(
        normalized,
        (
            "案件",
            "プロジェクト",
            "PJ",
            "タスク",
            "予定",
            "スケジュール",
        ),
    ):
        return True
    if _contains_any(
        normalized,
        (
            "案件進捗",
            "プロジェクト進捗",
            "進捗確認",
            "状況確認",
            "遅延確認",
            "progress review",
            "project progress",
        ),
    ):
        return True
    return False
