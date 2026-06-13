"""Runtime policy for deciding whether a tool call should execute."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Optional


_current_user_input: ContextVar[Optional[str]] = ContextVar(
    "tool_policy_current_user_input",
    default=None,
)


@dataclass(frozen=True)
class ToolPolicyDecision:
    allowed: bool
    reason: str


def set_current_user_input(user_input: Optional[str]) -> Token:
    return _current_user_input.set(user_input)


def reset_current_user_input(token: Token) -> None:
    _current_user_input.reset(token)


def get_current_user_input() -> Optional[str]:
    return _current_user_input.get()


def looks_like_filesystem_request(text: str) -> bool:
    return _looks_like_filesystem_request(text)


def looks_like_project_management_request(text: str) -> bool:
    return _looks_like_project_management_request(text)


def looks_like_project_management_mutation_request(text: str) -> bool:
    return bool(project_management_required_mutation_tools(text))


def looks_like_utility_request(text: str) -> bool:
    return _looks_like_utility_request(text)


def project_management_required_mutation_tools(text: str) -> set[str]:
    normalized = str(text or "").casefold()
    if not normalized.strip():
        return set()

    task_terms = (
        "task",
        "todo",
        "タスク",
        "予定",
        "予約",
        "用事",
    )
    project_info_terms = (
        "project information",
        "project info",
        "案件情報",
        "プロジェクト情報",
        "案件情報db",
        "案件情報DB",
        "案件db",
        "案件DB",
        "重要資料",
        "資料フォルダ",
        "管理資料",
        "決定事項",
        "要確認",
        "カテゴリ",
        "fact",
        "facts",
    )
    record_table_terms = (
        "record table",
        "record-table",
        "dbtable",
        ".dbtable",
        "database table",
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
    wbs_terms = (
        "wbs",
        "WBS",
        "工程表",
        "進捗管理",
    )
    issue_terms = (
        "issue",
        "issues",
        "issue tracker",
        "課題",
        "課題管理",
        "課題管理表",
        "要確認",
        "確認事項",
    )
    folder_terms = (
        "folder",
        "directory",
        "workspace",
        "フォルダ",
        "ディレクトリ",
        "ワークスペース",
        "資料",
        "ファイル",
    )
    create_terms = (
        "add",
        "create",
        "register",
        "save",
        "record",
        "追加",
        "作成",
        "登録",
        "保存",
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
        "update",
        "edit",
        "change",
        "complete",
        "done",
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
        "delete",
        "remove",
        "消し",
        "削除",
    )
    schedule_terms = (
        "schedule",
        "calendar",
        "スケジュール",
        "カレンダー",
    )

    has_task = any(term.casefold() in normalized for term in task_terms)
    has_project_info = any(term.casefold() in normalized for term in project_info_terms)
    has_record_table = any(term.casefold() in normalized for term in record_table_terms)
    has_wbs = any(term.casefold() in normalized for term in wbs_terms)
    has_issue = any(term.casefold() in normalized for term in issue_terms)
    has_folder = any(term.casefold() in normalized for term in folder_terms)
    has_project_database = has_project_info and any(
        term in normalized
        for term in ("db", "database", "データベース", "台帳", "一覧表")
    )
    has_create_or_update = any(
        term.casefold() in normalized for term in create_terms + update_terms
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
    if has_project_info and has_folder and has_create_or_update:
        tools.add("organize_project_information_from_folder")
    if has_project_info and has_create_or_update:
        tools.update(
            {
                "upsert_project_info_category",
                "register_project_document",
                "upsert_project_fact",
            }
        )
    if (has_record_table or has_project_database) and has_create_or_update:
        tools.add("create_record_table")
    return tools


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

    if tool_name == "filesystem_assistant":
        if _looks_like_filesystem_request(text):
            return ToolPolicyDecision(True, "request explicitly refers to files or workspace content")
        return ToolPolicyDecision(
            False,
            "the request does not ask to inspect or modify files, folders, repositories, or workspace documents",
        )

    if tool_name == "project_management_assistant":
        if _looks_like_project_management_request(text):
            return ToolPolicyDecision(True, "request explicitly refers to project information, task, WBS, schedule, timer, or reporting work")
        return ToolPolicyDecision(
            False,
            "the request does not ask for project information, task, WBS, schedule, timer, or reporting work",
        )

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
        "Answer directly, or use `search_assistant` only when public, fresh, or time-sensitive information is required."
    )


def _combined_text(user_input: Optional[str], tool_args: Optional[dict[str, Any]]) -> str:
    if user_input and str(user_input).strip():
        return str(user_input).strip()

    parts: list[str] = []
    if tool_args:
        for value in tool_args.values():
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts).strip()


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(term.casefold() in lowered for term in terms)


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
            "previous",
            "earlier",
            "last time",
            "conversation history",
            "remember",
        ),
    )


def _looks_like_filesystem_request(text: str) -> bool:
    if _contains_any(
        text,
        (
            "ファイル",
            "フォルダ",
            "ディレクトリ",
            "パス",
            "ワークスペース",
            "リポジトリ",
            "ソース",
            "コード",
            "資料",
            "文書",
            "ドキュメント",
            "添付",
            "アップロード",
            "案件資料",
            "案件フォルダ",
            "workspace",
            "repository",
            "repo",
            "file",
            "folder",
            "directory",
            "path",
            "document",
            "attachment",
            "ファイル",
            "フォルダ",
            "ディレクトリ",
            "パス",
            "ワークスペース",
            "リポジトリ",
            "ソース",
            "コード",
            "資料",
            "文書",
            "ドキュメント",
            "添付",
            "アップロード",
            "案件資料",
            "案件フォルダ",
            "読める",
            "読む",
            "読んで",
            "見つけて",
            "探して",
            "下層",
            "配下",
            "階層",
            "構造",
            "最初に読む",
        ),
    ):
        return True

    return _contains_any(
        text,
        (
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".json",
            ".yaml",
            ".yml",
            ".md",
            ".txt",
            ".docx",
            ".xlsx",
            ".pdf",
            "src/",
            "docs/",
            "memory-bank/",
            "_projects/",
        ),
    )


def _looks_like_utility_request(text: str) -> bool:
    normalized = str(text or "").casefold()
    if not normalized.strip():
        return False

    utility_terms = (
        "current time",
        "what time",
        "time is it",
        "current date",
        "today's date",
        "todays date",
        "date today",
        "weather",
        "forecast",
        "temperature",
        "calculate",
        "calculation",
        "calculator",
        "compute",
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
    if _contains_any(
        text,
        (
            "案件",
            "プロジェクト",
            "タスク",
            "台帳",
            "データベース",
            "DB",
            "todo",
            "wbs",
            "工数",
            "タイマー",
            "時間記録",
            "作業ログ",
            "time report",
            "status report",
            "project",
            "record table",
            "database",
            "task",
            "timer",
            "案件",
            "プロジェクト",
            "タスク",
            "WBS",
            "予定",
            "スケジュール",
            "台帳",
            "データベース",
            "作業ログ",
            "工数",
        ),
    ):
        return True

    if _contains_any(
        text,
        (
            "スケジュール",
            "予定",
            "カレンダー",
            "schedule",
            "calendar",
        ),
    ):
        return True

    return _contains_any(
        text,
        (
            "進捗",
            "ステータス",
            "期限",
            "締切",
            "レポート",
            "依頼",
            "deadline",
            "report",
            "request",
        ),
    ) and _contains_any(text, ("案件", "プロジェクト", "タスク", "作業", "project", "task"))
