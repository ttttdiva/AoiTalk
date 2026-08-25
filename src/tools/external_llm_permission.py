"""
LLM tool permission manager.

Manages user permission requests for LLM initiated actions such as external
search, file writes/deletes, and command execution. When the active generation
policy requires confirmation, a request is sent to the WebUI and execution waits
for the user's approve/deny response.
"""

import asyncio
import contextvars
import logging
import os
import re
import shlex
import uuid
from typing import Any, Callable, Dict, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

DEFAULT_PERMISSION_SESSION_KEY = "default"

# 承認キャッシュを引くための会話セッション識別子。
# 誰もセットしなければ全体で1つの既定キーになる（単一ユーザー運用では十分）。
_current_permission_session_key: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aoitalk_permission_session_key",
    default=DEFAULT_PERMISSION_SESSION_KEY,
)


def set_permission_session_key(value: Optional[str]):
    """承認キャッシュのスコープとなるセッションキーを設定する。"""
    return _current_permission_session_key.set(
        str(value or DEFAULT_PERMISSION_SESSION_KEY)
    )


def reset_permission_session_key(token) -> None:
    _current_permission_session_key.reset(token)


def get_permission_session_key() -> str:
    return _current_permission_session_key.get()


class PermissionStatus(Enum):
    """Permission request status"""
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"


@dataclass
class PermissionRequest:
    """Represents a pending permission request"""
    request_id: str
    tool_name: str
    tool_args: Dict[str, Any]
    description: str
    status: PermissionStatus = PermissionStatus.PENDING
    future: Optional[asyncio.Future] = field(default=None, repr=False)
    loop: Optional[asyncio.AbstractEventLoop] = field(default=None, repr=False)
    # セッション承認キャッシュ用のキー。None ならキャッシュ対象外。
    cache_key: Optional[Tuple[str, str, str]] = field(default=None, repr=False)
    scope: str = "once"
    user_id: Optional[str] = field(default=None, repr=False)
    session_id: Optional[str] = field(default=None, repr=False)


def get_permission_request_scope() -> tuple[Optional[str], Optional[str]]:
    """Return the user/session scope carried by the current generation task."""
    value = get_permission_session_key()
    if "|" not in value:
        return None, None
    user_id, session_id = value.split("|", 1)
    return user_id or None, session_id or None


def _enterprise_permission_scope_required() -> bool:
    """Require an owner scope for permission prompts in Enterprise."""
    try:
        from ..features import Features

        return Features.is_enterprise()
    except Exception:
        # If the central profile resolver is unavailable, either selector
        # requesting Enterprise must still fail closed.
        return any(
            str(os.getenv(name) or "").strip().lower() == "enterprise"
            for name in ("AOITALK_PROFILE", "AIVTUBER_ENV")
        )


FILE_WRITE_TOOLS = {
    "create_file",
    "append_to_file",
    "edit_file",
    "insert_to_file",
    "undo_edit",
    "create_workspace_directory",
    "upload_workspace_file",
    "move_workspace_item",
    "copy_workspace_item",
    "docs_place_workspace_file",
    "upload_user_file",
}

FILE_DELETE_TOOLS = {
    "delete_file",
    "delete_workspace_item",
    "delete_user_file",
}

COMMAND_TOOLS = {"execute_command"}

EXTERNAL_SEARCH_TOOLS = {"web_search", "grok_x_search"}

PROJECT_MANAGEMENT_MUTATION_TOOLS = {
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

DOCS_MUTATION_TOOLS = {
    "docs_attach_workspace_file",
    "docs_place_workspace_file",
    "docs_ensure_inbox",
    "docs_create_nodes",
    "docs_update_node",
    "inbox_update_item",
    "docs_move_node",
    "docs_archive_node",
}

DEFAULT_PERMISSION_TOOLS = sorted(
    EXTERNAL_SEARCH_TOOLS
    | FILE_WRITE_TOOLS
    | FILE_DELETE_TOOLS
    | COMMAND_TOOLS
    | PROJECT_MANAGEMENT_MUTATION_TOOLS
    | DOCS_MUTATION_TOOLS
)

MUTATION_TOOLS = (
    FILE_WRITE_TOOLS
    | FILE_DELETE_TOOLS
    | COMMAND_TOOLS
    | PROJECT_MANAGEMENT_MUTATION_TOOLS
    | DOCS_MUTATION_TOOLS
)

# 取り返しのつかないコマンドを拾うためのパターン。
# CommandExecutor.DANGEROUS_PATTERNS とは別物で、こちらは「確認ダイアログを出すか」
# だけを決める。判定できないものは False（＝確認しない）に倒し、既定を自由側に保つ。
_DESTRUCTIVE_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 削除系
    re.compile(r"(?:^|[;&|]\s*)\s*(?:sudo\s+)?rm\b", re.IGNORECASE),
    re.compile(r"(?:^|[;&|]\s*)\s*(?:sudo\s+)?rmdir\b", re.IGNORECASE),
    re.compile(r"(?:^|[;&|]\s*)\s*(?:sudo\s+)?unlink\b", re.IGNORECASE),
    re.compile(r"(?:^|[;&|]\s*)\s*(?:sudo\s+)?shred\b", re.IGNORECASE),
    re.compile(r"\bdel\s+/[a-z]", re.IGNORECASE),
    re.compile(r"(?:^|[;&|]\s*)\s*(?:del|erase)\b", re.IGNORECASE),
    re.compile(r"\brd\s+/s\b", re.IGNORECASE),
    re.compile(r"\bRemove-Item\b", re.IGNORECASE),
    re.compile(r"\bClear-Content\b", re.IGNORECASE),
    # フォーマット・ディスク操作
    re.compile(r"(?:^|[;&|]\s*)\s*(?:sudo\s+)?mkfs(?:\.\w+)?\b", re.IGNORECASE),
    re.compile(r"(?:^|[;&|]\s*)\s*format\s+[a-z]:", re.IGNORECASE),
    re.compile(r"\bdiskpart\b", re.IGNORECASE),
    re.compile(r"\bFormat-Volume\b", re.IGNORECASE),
    re.compile(r"\bdd\s+[^|]*\bof=", re.IGNORECASE),
    # 上書きリダイレクト。>> の追記、`2>&1` のような fd 複製、
    # /dev/null・$null・NUL への破棄はファイルを壊さないので対象外。
    re.compile(
        r"(?<!>)>(?!>)(?!&)\s*(?!(?:/dev/null|\$null|nul\b))\S",
        re.IGNORECASE,
    ),
    re.compile(r"\bOut-File\b(?![^|]*-Append)", re.IGNORECASE),
    re.compile(r"\bSet-Content\b", re.IGNORECASE),
    # 破壊的な git 操作
    re.compile(r"\bgit\b[^|;&]*\breset\b[^|;&]*--hard\b", re.IGNORECASE),
    re.compile(r"\bgit\b[^|;&]*\bclean\b[^|;&]*-[a-z]*f", re.IGNORECASE),
    re.compile(r"\bgit\b[^|;&]*\bpush\b[^|;&]*(?:--force\b|-f\b)", re.IGNORECASE),
    re.compile(r"\bgit\b[^|;&]*\bcheckout\b[^|;&]*\s--\s", re.IGNORECASE),
    re.compile(r"\bgit\b[^|;&]*\bbranch\b[^|;&]*\s-D\b"),
    re.compile(r"\bgit\b[^|;&]*\bfilter-branch\b", re.IGNORECASE),
    # 権限・システム設定
    re.compile(r"(?:^|[;&|]\s*)\s*(?:sudo\s+)?chmod\b[^|;&]*-R\b", re.IGNORECASE),
    re.compile(r"(?:^|[;&|]\s*)\s*(?:sudo\s+)?chown\b[^|;&]*-R\b", re.IGNORECASE),
    re.compile(r"\breg\s+delete\b", re.IGNORECASE),
    # 破壊的なパッケージ/DB操作
    re.compile(r"\bdrop\s+(?:database|table|schema)\b", re.IGNORECASE),
    re.compile(r"\btruncate\s+table\b", re.IGNORECASE),
    re.compile(r"\bdocker\b[^|;&]*\b(?:rm|rmi|prune)\b", re.IGNORECASE),
)


def _command_looks_destructive(command: str) -> bool:
    """コマンド文字列が取り返しのつかない操作かどうかを判定する。

    確認ダイアログを出すかどうかだけを決める緩い判定で、判定できない場合は
    ``False``（確認しない）を返す。既定は自由側に倒す方針のため。
    """
    text = str(command or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _DESTRUCTIVE_COMMAND_PATTERNS)


def _command_program_name(command: str) -> str:
    """コマンド文字列から実行プログラム名だけを取り出す。"""
    text = str(command or "").strip()
    if not text:
        return ""
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        tokens = text.split()
    for token in tokens:
        cleaned = token.strip("\"'")
        if not cleaned or "=" in cleaned.split("/")[0].split("\\")[0]:
            # 環境変数の前置き（VAR=value cmd）は読み飛ばす
            continue
        return cleaned.casefold()
    return ""


_PATH_ARG_KEYS = (
    "path",
    "file_path",
    "filename",
    "src",
    "node_id",
    "name",
)


def build_approval_signature(tool_name: str, tool_args: Optional[Dict[str, Any]]) -> str:
    """承認キャッシュのキーに使う「同種の操作」を表す署名を作る。

    JSON の完全一致ではなく、``execute_command`` は実行プログラム名、
    ファイル系ツールは対象パスまでを見る粒度にする。
    """
    args = tool_args or {}
    if tool_name in COMMAND_TOOLS:
        return f"program:{_command_program_name(args.get('command', ''))}"
    if tool_name in (FILE_WRITE_TOOLS | FILE_DELETE_TOOLS):
        for key in _PATH_ARG_KEYS:
            value = args.get(key)
            if value:
                return f"path:{str(value)}"
        return "path:"
    return ""


class ExternalLLMPermissionManager:
    """
    Manages permission requests for external LLM API calls.
    
    When auto_approve is False, the manager will:
    1. Send a permission request to the WebUI via broadcast callback
    2. Wait for user response (approve/deny)
    3. Return the decision to the caller
    """
    
    def __init__(self, config=None):
        """
        Initialize the permission manager.
        
        Args:
            config: Application config object or dict
        """
        self.config = config
        self._pending_requests: Dict[str, PermissionRequest] = {}
        self._broadcast_callback: Optional[Callable] = None
        self._timeout_seconds = 300  # 5 minutes timeout
        # (session_key, tool_name, signature) -> セッション中は許可
        self._session_approvals: set[Tuple[str, str, str]] = set()

        # Load config
        self._load_config()

    def _load_config(self):
        """Load configuration settings"""
        self.auto_approve = True  # Default to current behavior outside agent modes
        self.enabled_tools = DEFAULT_PERMISSION_TOOLS.copy()
        # プロファイル名 -> PermissionPolicy。設定で既定より厳しくするための上書き。
        self.permission_policy_overrides: Dict[str, Any] = {}
        self.session_approval_cache_enabled = True

        if self.config is None:
            return

        # Get external_llm config
        external_llm_config = None
        if hasattr(self.config, 'get'):
            external_llm_config = self.config.get('external_llm', {})
        elif isinstance(self.config, dict):
            external_llm_config = self.config.get('external_llm', {})

        if external_llm_config:
            self.auto_approve = external_llm_config.get('auto_approve', True)
            self.enabled_tools = external_llm_config.get('tools', self.enabled_tools)
            self.session_approval_cache_enabled = bool(
                external_llm_config.get('session_approval_cache', True)
            )
            self.permission_policy_overrides = self._parse_policy_overrides(
                external_llm_config.get('permission_policy_overrides')
            )

        logger.info(
            "[ExternalLLMPermission] auto_approve=%s, tools=%s, "
            "policy_overrides=%s, session_approval_cache=%s",
            self.auto_approve,
            self.enabled_tools,
            {k: v.value for k, v in self.permission_policy_overrides.items()},
            self.session_approval_cache_enabled,
        )

    @staticmethod
    def _parse_policy_overrides(raw: Any) -> Dict[str, Any]:
        """``permission_policy_overrides`` を安全に解釈する。

        空文字・None・未知の文字列は黙って無視する。設定ミスで動かなくなるより、
        既定のポリシーで動くほうを優先するため。
        """
        from ..llm.generation_policy import resolve_permission_policy

        if not isinstance(raw, dict):
            return {}
        resolved: Dict[str, Any] = {}
        for profile_name, value in raw.items():
            policy = resolve_permission_policy(value)
            if policy is None:
                continue
            resolved[str(profile_name)] = policy
        return resolved

    def set_broadcast_callback(self, callback: Callable):
        """
        Set the callback for broadcasting permission requests to WebUI.
        
        Args:
            callback: Async function that takes a message dict and broadcasts to clients
        """
        self._broadcast_callback = callback
    
    def effective_permission_policy(self):
        """現在のプロファイルに適用される PermissionPolicy を返す。

        設定の ``permission_policy_overrides`` があればプロファイル既定より優先する。
        """
        # Import lazily because src.llm package initialization imports src.tools.
        # An eager import here creates a cycle before this module exposes its helpers.
        from ..llm.generation_policy import get_current_generation_policy

        policy = get_current_generation_policy()
        profile_name = getattr(policy.profile, "value", str(policy.profile))
        override = self.permission_policy_overrides.get(profile_name)
        if override is not None:
            return override
        return policy.permission_policy

    def is_permission_required(
        self,
        tool_name: str,
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Check if permission is required for the given tool.

        Args:
            tool_name: Name of the tool
            tool_args: Arguments passed to the tool（破壊性の判定に使う。省略可）

        Returns:
            True if permission is required
        """
        from ..llm.generation_policy import PermissionPolicy

        permission_policy = self.effective_permission_policy()
        if permission_policy == PermissionPolicy.AUTO_APPROVE:
            return False
        if permission_policy == PermissionPolicy.CONFIRM_DESTRUCTIVE:
            # 取り返しのつかない操作だけ確認する。
            # 作成・編集・追記・Docs更新・プロジェクト管理・検索・読み取りは確認しない。
            if tool_name in FILE_DELETE_TOOLS:
                return True
            if tool_name in COMMAND_TOOLS:
                command = str((tool_args or {}).get("command") or "")
                return _command_looks_destructive(command)
            return False
        if permission_policy == PermissionPolicy.CONFIRM_MUTATIONS:
            return tool_name in MUTATION_TOOLS
        if permission_policy == PermissionPolicy.CONFIRM_ALL_TOOLS:
            return tool_name in self.enabled_tools

        if self.auto_approve:
            return False
        return tool_name in self.enabled_tools

    def _approval_cache_key(
        self,
        tool_name: str,
        tool_args: Optional[Dict[str, Any]],
    ) -> Optional[Tuple[str, str, str]]:
        if not self.session_approval_cache_enabled:
            return None
        return (
            get_permission_session_key(),
            tool_name,
            build_approval_signature(tool_name, tool_args),
        )

    def is_approved_for_session(
        self,
        tool_name: str,
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """同じ署名の操作がこのセッションで既に許可済みかどうか。"""
        cache_key = self._approval_cache_key(tool_name, tool_args)
        return bool(cache_key and cache_key in self._session_approvals)

    def clear_session_approvals(self, session_key: Optional[str] = None) -> None:
        """セッション承認キャッシュを破棄する。"""
        if session_key is None:
            self._session_approvals.clear()
            return
        self._session_approvals = {
            key for key in self._session_approvals if key[0] != session_key
        }

    async def request_permission(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        description: str = ""
    ) -> bool:
        """
        Request permission from user for external LLM API call.
        
        Args:
            tool_name: Name of the tool
            tool_args: Arguments being passed to the tool
            description: Human-readable description of the action
            
        Returns:
            True if approved, False if denied or timeout
        """
        # Auto-approve if configured/current mode allows it.
        if not self.is_permission_required(tool_name, tool_args):
            return True

        # このセッションで同種の操作を既に許可済みなら、聞き直さない。
        cache_key = self._approval_cache_key(tool_name, tool_args)
        if cache_key and cache_key in self._session_approvals:
            logger.info(
                "[ExternalLLMPermission] セッション承認キャッシュにより自動許可: %s",
                cache_key,
            )
            return True

        # Require broadcast callback
        if self._broadcast_callback is None:
            logger.warning("[ExternalLLMPermission] No broadcast callback set, denying")
            return False

        permission_user_id, permission_session_id = get_permission_request_scope()
        if _enterprise_permission_scope_required() and not (
            permission_user_id and permission_session_id
        ):
            logger.error(
                "[ExternalLLMPermission] Enterprise permission request has no user/session scope; denying"
            )
            return False

        # Create request
        request_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        future = loop.create_future()

        request = PermissionRequest(
            request_id=request_id,
            tool_name=tool_name,
            tool_args=tool_args,
            description=description or self._generate_description(tool_name, tool_args),
            future=future,
            loop=loop,
            cache_key=cache_key,
            user_id=permission_user_id,
            session_id=permission_session_id,
        )

        self._pending_requests[request_id] = request

        # Broadcast permission request to WebUI
        try:
            await self._broadcast_callback({
                "type": "external_llm_permission_request",
                "data": {
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "description": request.description,
                    # WebUI に「1回だけ許可 / このセッション中は許可 / 拒否」を出させる
                    "scope_options": (
                        ["once", "session"] if cache_key else ["once"]
                    ),
                    "signature": cache_key[2] if cache_key else "",
                }
            })

            logger.info(f"[ExternalLLMPermission] Sent permission request: {request_id} for {tool_name}")
            
            # Wait for response with timeout
            try:
                result = await asyncio.wait_for(future, timeout=self._timeout_seconds)
                return result
            except asyncio.TimeoutError:
                logger.warning(f"[ExternalLLMPermission] Permission request timed out: {request_id}")
                request.status = PermissionStatus.TIMEOUT
                return False
                
        except Exception as e:
            logger.error(f"[ExternalLLMPermission] Error requesting permission: {e}")
            return False
        finally:
            # Clean up
            self._pending_requests.pop(request_id, None)

    async def request_external_model_prompt(
        self,
        prompt: str,
        *,
        redacted_prompt: str = "",
        redaction_findings: Optional[list[dict[str, str]]] = None,
        provider: str,
        model: str,
        description: str = "",
        confirm: bool = True,
        notify: bool = True,
        request_kind: str = "external_model_prompt",
        source_kind: str = "",
        risk_level: str = "",
        semantic_status: str = "",
        warning: str = "",
    ) -> Optional[str]:
        """Ask the WebUI to approve or edit a prompt before an external model call."""
        outbound_prompt = (redacted_prompt or "").strip() or prompt
        if not confirm:
            return outbound_prompt

        if self._broadcast_callback is None:
            logger.warning("[ExternalLLMPermission] No broadcast callback set, denying external model prompt")
            return None

        permission_user_id, permission_session_id = get_permission_request_scope()
        if _enterprise_permission_scope_required() and not (
            permission_user_id and permission_session_id
        ):
            logger.error(
                "[ExternalLLMPermission] Enterprise external-model prompt has no user/session scope; denying"
            )
            return None

        request_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        future = loop.create_future()

        request = PermissionRequest(
            request_id=request_id,
            tool_name=request_kind,
            tool_args={
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "redacted_prompt": outbound_prompt,
            },
            description=description
            or f"分担先モデル {provider}/{model} へ送信するプロンプトを確認してください",
            future=future,
            loop=loop,
            user_id=permission_user_id,
            session_id=permission_session_id,
        )
        self._pending_requests[request_id] = request

        try:
            await self._broadcast_callback(
                {
                    "type": "external_model_prompt_request",
                    "data": {
                        "request_id": request_id,
                        "provider": provider,
                        "model": model,
                        "prompt": prompt,
                        "original_prompt": prompt,
                        "redacted_prompt": outbound_prompt,
                        "redaction_findings": redaction_findings or [],
                        "description": request.description,
                        "notify": notify,
                        "source_kind": source_kind,
                        "risk_level": risk_level,
                        "semantic_status": semantic_status,
                        "warning": warning,
                    },
                }
            )
            result = await asyncio.wait_for(future, timeout=self._timeout_seconds)
            if isinstance(result, dict) and result.get("approved"):
                edited_prompt = str(result.get("prompt") or "").strip()
                return edited_prompt or outbound_prompt
            return None
        except asyncio.TimeoutError:
            logger.warning("[ExternalLLMPermission] External model prompt request timed out: %s", request_id)
            request.status = PermissionStatus.TIMEOUT
            return None
        except Exception as e:
            logger.error("[ExternalLLMPermission] External model prompt request failed: %s", e)
            return None
        finally:
            self._pending_requests.pop(request_id, None)
    
    def handle_permission_response(
        self,
        request_id: str,
        approved: bool,
        scope: str = "once",
        *,
        requester_user_id: Optional[str] = None,
        requester_session_id: Optional[str] = None,
    ):
        """
        Handle user response to permission request.

        Args:
            request_id: The request ID
            approved: True if user approved, False if denied
            scope: ``"once"`` なら今回だけ、``"session"`` ならセッション中は許可を記憶する
        """
        request = self._pending_requests.get(request_id)
        if not request:
            logger.warning(f"[ExternalLLMPermission] Unknown request ID: {request_id}")
            return
        if (
            request.user_id
            and request.user_id != str(requester_user_id or "")
        ) or (
            request.session_id
            and request.session_id != str(requester_session_id or "")
        ):
            logger.warning(
                "[ExternalLLMPermission] Permission response scope mismatch: %s",
                request_id,
            )
            return

        normalized_scope = str(scope or "once").strip().casefold()
        if normalized_scope not in ("once", "session"):
            normalized_scope = "once"
        request.scope = normalized_scope
        request.status = PermissionStatus.APPROVED if approved else PermissionStatus.DENIED

        if (
            approved
            and normalized_scope == "session"
            and self.session_approval_cache_enabled
            and request.cache_key
        ):
            self._session_approvals.add(request.cache_key)
            logger.info(
                "[ExternalLLMPermission] セッション承認をキャッシュしました: %s",
                request.cache_key,
            )

        if request.future and not request.future.done():
            if request.loop and request.loop.is_running():
                request.loop.call_soon_threadsafe(request.future.set_result, approved)
            else:
                request.future.set_result(approved)

        logger.info(
            "[ExternalLLMPermission] Permission response: %s -> %s (scope=%s)",
            request_id,
            "approved" if approved else "denied",
            normalized_scope,
        )

    def handle_external_model_prompt_response(
        self,
        request_id: str,
        approved: bool,
        prompt: str = "",
        *,
        requester_user_id: Optional[str] = None,
        requester_session_id: Optional[str] = None,
    ):
        """Handle user response for an external model prompt request."""
        request = self._pending_requests.get(request_id)
        if not request:
            logger.warning("[ExternalLLMPermission] Unknown external model prompt request ID: %s", request_id)
            return
        if (
            request.user_id
            and request.user_id != str(requester_user_id or "")
        ) or (
            request.session_id
            and request.session_id != str(requester_session_id or "")
        ):
            logger.warning(
                "[ExternalLLMPermission] External prompt scope mismatch: %s",
                request_id,
            )
            return

        request.status = PermissionStatus.APPROVED if approved else PermissionStatus.DENIED
        payload = {"approved": approved, "prompt": prompt}

        if request.future and not request.future.done():
            if request.loop and request.loop.is_running():
                request.loop.call_soon_threadsafe(request.future.set_result, payload)
            else:
                request.future.set_result(payload)

        logger.info(
            "[ExternalLLMPermission] External model prompt response: %s -> %s",
            request_id,
            "approved" if approved else "denied",
        )
    
    def _generate_description(self, tool_name: str, tool_args: Dict[str, Any]) -> str:
        """Generate a human-readable description of the action"""
        descriptions = {
            "web_search": lambda args: f"OpenAI APIによるWeb検索: 「{args.get('query', '')}」",
            "grok_x_search": lambda args: f"X (Twitter) 検索: 「{args.get('query', '')}」",
            "execute_command": lambda args: f"コマンド実行: {args.get('command', '')}",
            "create_file": lambda args: f"ファイル作成: {args.get('path', '')}",
            "append_to_file": lambda args: f"ファイル追記: {args.get('path', '')}",
            "edit_file": lambda args: f"ファイル編集: {args.get('path', '')}",
            "insert_to_file": lambda args: f"ファイル挿入: {args.get('path', '')}",
            "undo_edit": lambda args: f"ファイル編集の取り消し: {args.get('path', '')}",
            "delete_file": lambda args: f"ファイル/フォルダ削除: {args.get('path', '')}",
            "create_workspace_directory": lambda args: (
                f"ワークスペースフォルダ作成: {args.get('path', '')}/{args.get('name', '')}"
            ),
            "upload_workspace_file": lambda args: (
                f"ワークスペースファイル保存: {args.get('path', '')}/{args.get('filename', '')}"
            ),
            "delete_workspace_item": lambda args: f"ワークスペース項目削除: {args.get('path', '')}",
            "move_workspace_item": lambda args: (
                f"ワークスペース項目移動: {args.get('src', '')} -> {args.get('dest', '')}"
            ),
            "copy_workspace_item": lambda args: (
                f"ワークスペース項目コピー: {args.get('src', '')} -> {args.get('dest', '')}"
            ),
            "upload_user_file": lambda args: f"ユーザーファイル保存: {args.get('filename', '')}",
            "delete_user_file": lambda args: f"ユーザーファイル削除: {args.get('filename', '')}",
            "docs_create_nodes": lambda args: (
                f"Docsノード作成: 親「{args.get('parent', 'today')}」配下"
            ),
            "docs_attach_workspace_file": lambda args: (
                f"Docsへworkspaceファイル参照を追加: {args.get('file_path', '')}"
            ),
            "docs_place_workspace_file": lambda args: (
                f"workspaceファイル配置とDocs参照追加: "
                f"{args.get('src', '')} -> {args.get('dest', '')}"
            ),
            "docs_ensure_inbox": lambda args: "Docs Inboxを作成または確認",
            "docs_update_node": lambda args: (
                f"Docsノード更新: {args.get('title') or args.get('node_id', '')}"
            ),
            "inbox_update_item": lambda args: (
                f"Inbox項目更新: {args.get('node_id', '')}"
            ),
            "docs_move_node": lambda args: (
                f"Docsノード移動: {args.get('node_id', '')} -> {args.get('new_parent', '')}"
            ),
            "docs_archive_node": lambda args: f"Docsノードのアーカイブ: {args.get('node_id', '')}",
        }
        
        generator = descriptions.get(tool_name)
        if generator:
            return generator(tool_args)
        return f"{tool_name} を実行"


# Global instance (initialized by server)
_permission_manager: Optional[ExternalLLMPermissionManager] = None


def get_permission_manager() -> Optional[ExternalLLMPermissionManager]:
    """Get the global permission manager instance"""
    return _permission_manager


def set_permission_manager(manager: ExternalLLMPermissionManager):
    """Set the global permission manager instance"""
    global _permission_manager
    _permission_manager = manager


async def request_external_model_prompt(
    prompt: str,
    *,
    redacted_prompt: str = "",
    redaction_findings: Optional[list[dict[str, str]]] = None,
    provider: str,
    model: str,
    description: str = "",
    confirm: bool = True,
    notify: bool = True,
    request_kind: str = "external_model_prompt",
    source_kind: str = "",
    risk_level: str = "",
    semantic_status: str = "",
    warning: str = "",
) -> Optional[str]:
    manager = get_permission_manager()
    if manager is None:
        outbound_prompt = (redacted_prompt or "").strip() or prompt
        return outbound_prompt if not confirm else None

    return await manager.request_external_model_prompt(
        prompt,
        redacted_prompt=redacted_prompt,
        redaction_findings=redaction_findings,
        provider=provider,
        model=model,
        description=description,
        confirm=confirm,
        notify=notify,
        request_kind=request_kind,
        source_kind=source_kind,
        risk_level=risk_level,
        semantic_status=semantic_status,
        warning=warning,
    )


async def check_permission(tool_name: str, tool_args: Dict[str, Any], description: str = "") -> bool:
    """
    Convenience function to check permission for a tool.
    
    Args:
        tool_name: Name of the tool
        tool_args: Arguments being passed to the tool
        description: Human-readable description of the action
        
    Returns:
        True if approved (or no manager/auto-approve), False if denied
    """
    manager = get_permission_manager()
    if manager is None:
        return True
    
    return await manager.request_permission(tool_name, tool_args, description)


def check_permission_sync(
    tool_name: str,
    tool_args: Dict[str, Any],
    description: str = "",
    timeout: int = 360,
) -> bool:
    """Synchronously check permission from sync tool functions."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            context = contextvars.copy_context()
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    lambda: context.run(
                        asyncio.run,
                        check_permission(tool_name, tool_args, description),
                    )
                )
                return bool(future.result(timeout=timeout))
        return bool(asyncio.run(check_permission(tool_name, tool_args, description)))
    except RuntimeError:
        return bool(asyncio.run(check_permission(tool_name, tool_args, description)))
    except Exception as exc:
        logger.error("[ExternalLLMPermission] Permission check failed: %s", exc)
        return False
