"""
LLM tool permission manager.

Manages user permission requests for LLM initiated actions such as external
search, file writes/deletes, and command execution. When the active generation
policy requires confirmation, a request is sent to the WebUI and execution waits
for the user's approve/deny response.
"""

import asyncio
import contextvars
import hmac
import hashlib
import json
import logging
import os
import re
import shlex
import uuid
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

from .operations_direct import OPERATIONS_MUTATION_TOOL_NAMES

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
    # Tool arguments/descriptions can contain prompts, paths, or credentials;
    # callers should inspect the explicit fields rather than accidentally
    # serializing a pending request via ``repr`` into logs or diagnostics.
    tool_args: Dict[str, Any] = field(repr=False)
    description: str = field(repr=False)
    status: PermissionStatus = PermissionStatus.PENDING
    future: Optional[asyncio.Future] = field(default=None, repr=False)
    loop: Optional[asyncio.AbstractEventLoop] = field(default=None, repr=False)
    # セッション承認キャッシュ用のキー。None ならキャッシュ対象外。
    cache_key: Optional[Tuple[str, str, str]] = field(default=None, repr=False)
    scope: str = "once"
    user_id: Optional[str] = field(default=None, repr=False)
    session_id: Optional[str] = field(default=None, repr=False)
    # v2 egress review transaction metadata.  Ordinary tool permission keeps
    # the historical fields above and never needs these values.
    contract_version: int = 1
    review_nonce: Optional[str] = field(default=None, repr=False)
    binding_digest: Optional[str] = field(default=None, repr=False)
    egress_transaction: bool = False
    descriptor: Dict[str, Any] = field(default_factory=dict, repr=False)
    original_payload: Any = field(default=None, repr=False)
    candidate_payload: Any = field(default=None, repr=False)


def get_permission_request_scope() -> tuple[Optional[str], Optional[str]]:
    """Return the user/session scope carried by the current generation task."""
    value = get_permission_session_key()
    if "|" not in value:
        return None, None
    user_id, session_id = value.split("|", 1)
    return user_id or None, session_id or None


def _canonical_json(value: Any) -> str:
    """Serialize a value deterministically for an egress binding digest.

    The digest is a protocol binding, not an audit projection.  Bytes are
    represented by a type/length/hash marker so this helper never needs to
    copy binary content into a JSON event or log line.
    """

    def normalize(node: Any) -> Any:
        if isinstance(node, (bytes, bytearray, memoryview)):
            raw = bytes(node)
            return {
                "__bytes__": True,
                "length": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        if isinstance(node, Mapping):
            return {str(key): normalize(item) for key, item in node.items()}
        if isinstance(node, (list, tuple)):
            return [normalize(item) for item in node]
        if isinstance(node, (set, frozenset)):
            return sorted((normalize(item) for item in node), key=repr)
        if node is ...:
            return "__ellipsis__"
        return node

    try:
        return json.dumps(
            normalize(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        return repr(value)


def payload_digest(value: Any) -> str:
    """Return a stable SHA-256 digest for a transaction payload."""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _constant_time_equal(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(str(left), str(right))
    except Exception:
        return False


_EVENT_MEDIA_KEYS = {
    "image",
    "image_url",
    "input_image",
    "input_audio",
    "audio",
    "video",
    "media",
    "inline_data",
    "source",
    "content_block",
    "base64",
    "base64_data",
    "content_base64",
    "data_base64",
    "image_base64",
    "audio_base64",
    "video_base64",
    "reference_base64",
    "reference_audio_base64",
}


def _safe_event_payload(value: Any, *, media_context: bool = False) -> Any:
    """Project payloads for the browser without exposing raw media bytes."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {
            "kind": "binary",
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "redacted": True,
        }

    if isinstance(value, str):
        text = value.strip()
        is_data_url = text.lower().startswith(("data:image/", "data:audio/", "data:video/"))
        is_b64 = media_context and len(text) >= 4 and len(text) % 4 == 0 and re.fullmatch(
            r"[A-Za-z0-9+/]+={0,2}", text
        )
        if is_data_url or is_b64:
            if is_data_url:
                header, _, body = text.partition(",")
                mime = header[5:].split(";", 1)[0]
                raw_repr = body.encode("utf-8", "replace")
            else:
                mime = "application/octet-stream"
                raw_repr = text.encode("ascii", "ignore")
            return {
                "kind": "media",
                "mime": mime,
                "size": len(raw_repr),
                "sha256": hashlib.sha256(raw_repr).hexdigest(),
                "redacted": True,
            }
        return value
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key or "").strip().lower().replace("-", "_")
            child_media = media_context or normalized in _EVENT_MEDIA_KEYS
            projected[str(key)] = _safe_event_payload(item, media_context=child_media)
        return projected
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_event_payload(item, media_context=media_context) for item in value]
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except Exception:
        text = repr(value)
        return {
            "kind": "opaque",
            "type": type(value).__name__,
            "sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
            "redacted": True,
        }


def _safe_event_payload_text(value: Any) -> str:
    """Return the canonical text shown by the v2 review editor."""

    projected = _safe_event_payload_projection(value)
    if projected is _EVENT_PROJECTION_FAILED:
        return "[REDACTED_PAYLOAD]"
    if isinstance(projected, str):
        return projected
    try:
        return json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return "[REDACTED_PAYLOAD]"


def _review_event_payload_text(value: Any) -> str:
    """Serialize exact review evidence while withholding raw media bytes.

    The authenticated review dialog must show the true pre-transform
    original and the exact candidate; applying the secret redactor here would
    make the UI evidence differ from the transaction values.  ``_safe_event_payload``
    still projects binary/data-URL media to a digest descriptor, so raw media
    never crosses the websocket.  This helper is used only for the interactive
    v2 event; audit/diagnostic projections continue to use the secret-safe
    ``_safe_event_payload_text`` path.
    """

    projected = _safe_event_payload(value)
    try:
        if isinstance(projected, str):
            return projected
        return json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        # Never stringify an opaque value into a websocket event.  A digest
        # descriptor is safe and still gives the reviewer a stable reference.
        return "[REDACTED_PAYLOAD]"


_EVENT_PROJECTION_FAILED = object()


def _safe_event_payload_projection(value: Any) -> Any:
    """Project an event payload without exposing raw credential material."""

    projected = _safe_event_payload(value)
    # Egress events are sent over the websocket and may be inspected by a
    # browser extension or a test/logger projection.  Media is handled above;
    # apply the shared secret-only display redactor to all remaining values so
    # labelled credentials, bearer/JWT/API tokens, and credential-keyed fields
    # cannot appear in the review event.  Import lazily to keep this module's
    # historical standalone import path free of a service-level cycle.
    try:
        from ..services.outbound_privacy_service import redact_secret_for_local_display

        return redact_secret_for_local_display(projected)
    except Exception:
        # A projection failure must not fall back to ``str(projected)`` (which
        # could contain the very secret that caused the failure).
        return _EVENT_PROJECTION_FAILED


def _scope_is_real(user_id: Optional[str], session_id: Optional[str]) -> bool:
    """Reject synthetic/default identifiers for strict egress review."""

    normalized_user = str(user_id or "").strip().casefold()
    normalized_session = str(session_id or "").strip().casefold()
    return bool(
        normalized_user
        and normalized_session
        and normalized_user not in {"default", "default_user", "none", "null"}
        and normalized_session not in {"default", "default_session", "none", "null"}
    )


_FINAL_PAYLOAD_UNSET = object()


def build_egress_binding_digest(
    *,
    request_id: str,
    review_nonce: str,
    descriptor: Mapping[str, Any],
    original_payload: Any,
    candidate_payload: Any,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    final_payload: Any = _FINAL_PAYLOAD_UNSET,
) -> str:
    """Bind one review to its exact request, scope, route and candidate.

    Responses must echo this digest.  Any route/payload/scope mutation then
    fails closed before a sender is invoked.  Only hashes of payload values
    are included in the binding envelope; the raw values stay in memory for
    the duration of the transaction and are never written to audit logs.
    """

    envelope = {
        "contract_version": 2,
        "request_id": str(request_id),
        "review_nonce": str(review_nonce),
        "user_id": str(user_id or ""),
        "session_id": str(session_id or ""),
        "descriptor": {str(key): value for key, value in descriptor.items()},
        "original_digest": payload_digest(original_payload),
        "candidate_digest": payload_digest(candidate_payload),
    }
    if final_payload is not _FINAL_PAYLOAD_UNSET:
        envelope["final_digest"] = payload_digest(final_payload)
    return hashlib.sha256(_canonical_json(envelope).encode("utf-8")).hexdigest()


def build_egress_final_binding_digest(
    *,
    binding_digest: str,
    final_payload: Any,
) -> str:
    """Bind the exact user-edited final wire value to one v2 approval.

    ``binding_digest`` already covers request identity, route, scope,
    original, and candidate.  Chaining the final payload digest keeps the
    initial challenge stable while allowing the server to attest the exact
    editor value that the gateway must send.
    """

    envelope = {
        "contract_version": 2,
        "binding_digest": str(binding_digest),
        "final_digest": payload_digest(final_payload),
    }
    return hashlib.sha256(_canonical_json(envelope).encode("utf-8")).hexdigest()


class EgressApproval(str):
    """String-compatible v2 approval carrying non-sensitive binding metadata."""

    def __new__(
        cls,
        value: str,
        *,
        request_id: str,
        review_nonce: str,
        binding_digest: str,
        final_binding_digest: str,
    ):
        instance = super().__new__(cls, value)
        instance.request_id = request_id
        instance.review_nonce = review_nonce
        instance.binding_digest = binding_digest
        instance.final_binding_digest = final_binding_digest
        return instance


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
    | OPERATIONS_MUTATION_TOOL_NAMES
    | PROJECT_MANAGEMENT_MUTATION_TOOLS
    | DOCS_MUTATION_TOOLS
)

MUTATION_TOOLS = (
    FILE_WRITE_TOOLS
    | FILE_DELETE_TOOLS
    | COMMAND_TOOLS
    | OPERATIONS_MUTATION_TOOL_NAMES
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
        # v2 review transactions are one-shot.  Keep only opaque token pairs
        # after completion so a late/replayed browser response cannot resolve
        # a newly-created request that happens to reuse an id in a test or
        # embedding.
        self._consumed_egress_tokens: set[tuple[str, str]] = set()

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
        permission_user_id, permission_session_id = get_permission_request_scope()
        # Validate Enterprise scope before both the AUTO_APPROVE shortcut and
        # the ordinary session-approval cache.  Otherwise a synthetic/default
        # context could inherit a previously cached mutation approval.
        if _enterprise_permission_scope_required() and not (
            permission_user_id and permission_session_id
        ):
            logger.error(
                "[ExternalLLMPermission] Enterprise permission request has no user/session scope; denying"
            )
            return False

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
                
        except Exception:
            # Exception text from a provider/UI callback can echo tool
            # arguments or credentials.  Keep diagnostics stable and
            # non-sensitive; the permission result itself remains fail-closed.
            logger.error("[ExternalLLMPermission] Error requesting permission")
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
        # v2 canonical egress transaction fields.  These are optional so the
        # legacy advanced-model prompt API remains source compatible; a
        # caller that supplies ``egress_transaction=True`` always gets the
        # strict v2 contract regardless of generation AUTO_APPROVE policy.
        egress_transaction: bool = False,
        original_payload: Any = None,
        candidate_payload: Any = None,
        action: str = "",
        transport: str = "",
        destination: str = "",
        tool: str = "",
        review_nonce: str = "",
        binding_digest: str = "",
        contract_version: int = 1,
    ) -> Optional[str]:
        """Ask the WebUI to approve or edit a prompt before an external model call."""
        # Prompt text is a legacy textual editor input.  Reject malformed
        # values rather than invoking ``.strip`` on arbitrary objects (or
        # silently stringifying a mapping that could contain raw credentials).
        if not isinstance(prompt, str):
            logger.warning("[ExternalLLMPermission] Malformed external model prompt")
            return None
        if redacted_prompt is not None and not isinstance(redacted_prompt, str):
            logger.warning("[ExternalLLMPermission] Malformed redacted prompt")
            return None
        outbound_prompt = (redacted_prompt or "").strip() or prompt
        # Egress approval is independent of ordinary tool permission.  In
        # particular, an autonomous/AUTO_APPROVE generation policy may not
        # suppress this confirmation.  ``original_payload`` and
        # ``candidate_payload`` are deliberately explicit for v2, including
        # when they happen to be equal in direct mode.
        if type(egress_transaction) is not bool:
            # Do not let a string/number supplied by an embedding accidentally
            # opt into (or out of) the strict protocol through Python truthiness.
            logger.warning("[ExternalLLMPermission] Malformed egress transaction flag")
            return None
        # A v2 transaction is an exact protocol, not a truthy/convertible
        # option.  Keep legacy callers on v1, but reject strings such as
        # ``"2"`` rather than silently upgrading an untrusted request.
        inferred_egress = (
            egress_transaction
            or original_payload is not None
            or candidate_payload is not None
            or contract_version == 2
        )
        if inferred_egress and (type(contract_version) is not int or contract_version != 2):
            logger.warning("[ExternalLLMPermission] Malformed egress contract version")
            return None
        egress_transaction = (
            inferred_egress
        )

        if egress_transaction:
            confirm = True
            contract_version = 2
            if type(provider) is not str or not provider.strip():
                logger.warning("[ExternalLLMPermission] Malformed egress provider")
                return None
            if type(model) is not str:
                logger.warning("[ExternalLLMPermission] Malformed egress model")
                return None
            if any(type(value) is not str for value in (action, transport, destination, tool)):
                logger.warning("[ExternalLLMPermission] Malformed egress descriptor")
                return None
        if not confirm:
            return outbound_prompt

        if self._broadcast_callback is None:
            logger.warning("[ExternalLLMPermission] No broadcast callback set, denying external model prompt")
            return None

        permission_user_id, permission_session_id = get_permission_request_scope()
        if egress_transaction and not _scope_is_real(
            permission_user_id, permission_session_id
        ):
            logger.error(
                "[ExternalLLMPermission] External egress review has no real user/session scope; denying"
            )
            return None
        if _enterprise_permission_scope_required() and not _scope_is_real(
            permission_user_id, permission_session_id
        ):
            logger.error(
                "[ExternalLLMPermission] External-model prompt has no user/session scope; denying"
            )
            return None

        request_id = str(uuid.uuid4())
        if egress_transaction:
            review_nonce = str(review_nonce or uuid.uuid4().hex)
            descriptor = {
                "action": str(action or ""),
                "transport": str(transport or ""),
                "destination": str(destination or ""),
                "provider": str(provider or ""),
                "tool": str(tool or ""),
                "model": str(model or ""),
            }
            if not all(
                isinstance(descriptor[key], str) and descriptor[key].strip()
                for key in ("action", "transport", "destination", "provider")
            ):
                logger.warning(
                    "[ExternalLLMPermission] Egress descriptor is missing required metadata"
                )
                return None
            if not isinstance(notify, bool):
                logger.warning("[ExternalLLMPermission] Malformed egress notify flag")
                return None
            if redaction_findings is not None and not isinstance(redaction_findings, list):
                logger.warning("[ExternalLLMPermission] Malformed egress findings")
                return None
            # The gateway normally computes this before entering the manager,
            # but computing it here as a defensive fallback keeps direct
            # manager embeddings bound as well.
            if not binding_digest:
                binding_digest = build_egress_binding_digest(
                    request_id=request_id,
                    review_nonce=review_nonce,
                    descriptor=descriptor,
                    original_payload=(prompt if original_payload is None else original_payload),
                    candidate_payload=(outbound_prompt if candidate_payload is None else candidate_payload),
                    user_id=permission_user_id,
                    session_id=permission_session_id,
                )
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
            contract_version=2 if egress_transaction else 1,
            review_nonce=review_nonce or None,
            binding_digest=binding_digest or None,
            egress_transaction=egress_transaction,
            descriptor=(descriptor if egress_transaction else {}),
            original_payload=original_payload,
            candidate_payload=(candidate_payload if candidate_payload is not None else outbound_prompt),
        )
        self._pending_requests[request_id] = request

        try:
            event_data: dict[str, Any] = {
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
            }
            if egress_transaction:
                # Never include the old v1 aliases as the authoritative value:
                # the browser must use candidate_payload and return a
                # final_payload string tied to the nonce/digest below.
                event_data.pop("prompt", None)
                event_data.pop("original_prompt", None)
                event_data.pop("redacted_prompt", None)
                # The authenticated v2 dialog is the one place where the
                # reviewer must see the true pre-transform original and the
                # exact candidate.  Keep raw media as digest descriptors, but
                # do not apply the secret redactor: masking itself is
                # represented by the candidate value and masking_status.
                original_event_text = _review_event_payload_text(original_payload)
                candidate_event_text = _review_event_payload_text(
                    candidate_payload
                    if candidate_payload is not None
                    else outbound_prompt
                )
                # The browser editor requires non-empty textual projections;
                # an empty/malformed transaction is denied before it can wait
                # for a UI response and eventually time out.
                if not original_event_text.strip() or not candidate_event_text.strip():
                    self._deny_egress_request(request, "empty_payload_projection")
                    return None
                event_data.update(
                    {
                        "contract_version": 2,
                        "review_nonce": review_nonce,
                        "binding_digest": binding_digest,
                        "action": descriptor["action"],
                        "transport": descriptor["transport"],
                        "destination": descriptor["destination"],
                        "tool": descriptor["tool"],
                        "original_payload": original_event_text,
                        "candidate_payload": candidate_event_text,
                        "masking_status": (
                            "MASKING_APPLIED"
                            if original_payload is not None
                            and payload_digest(original_payload)
                            != payload_digest(
                                candidate_payload
                                if candidate_payload is not None
                                else outbound_prompt
                            )
                            else "MASKING_UNNECESSARY"
                        ),
                    }
                )
            await self._broadcast_callback(
                {
                    "type": "external_model_prompt_request",
                    "data": event_data,
                }
            )
            result = await asyncio.wait_for(future, timeout=self._timeout_seconds)
            if egress_transaction:
                if (
                    not isinstance(result, Mapping)
                    or type(result.get("approved")) is not bool
                    or result.get("approved") is not True
                ):
                    return None
                final_payload = result.get("final_payload")
                if not isinstance(final_payload, str) or not final_payload.strip():
                    # Do not fall back to candidate on a malformed v2
                    # response.  The transport must be denied instead.
                    return None
                final_binding_digest = result.get("final_binding_digest")
                if not isinstance(final_binding_digest, str) or not final_binding_digest:
                    # Compatibility callbacks may resolve the future directly
                    # with the old v2 shape.  Compute the attestation from
                    # the pending immutable request rather than accepting an
                    # unbound final value.
                    final_binding_digest = build_egress_final_binding_digest(
                        binding_digest=str(
                            result.get("binding_digest") or request.binding_digest or ""
                        ),
                        final_payload=final_payload,
                    )
                return EgressApproval(
                    final_payload,
                    request_id=str(result.get("request_id") or request_id),
                    review_nonce=str(result.get("review_nonce") or review_nonce),
                    binding_digest=str(
                        result.get("binding_digest") or binding_digest or ""
                    ),
                    final_binding_digest=final_binding_digest,
                )
            if isinstance(result, dict) and result.get("approved"):
                edited_prompt = str(result.get("prompt") or "").strip()
                return edited_prompt or outbound_prompt
            return None
        except asyncio.TimeoutError:
            logger.warning("[ExternalLLMPermission] External model prompt request timed out: %s", request_id)
            request.status = PermissionStatus.TIMEOUT
            return None
        except Exception:
            # Do not interpolate callback/transport exception text: provider
            # SDKs frequently include the rejected prompt or authorization
            # header in their exception message.
            logger.error("[ExternalLLMPermission] External model prompt request failed")
            return None
        finally:
            if egress_transaction:
                self._consumed_egress_tokens.add((request_id, str(review_nonce)))
            self._pending_requests.pop(request_id, None)

    async def request_external_egress_review(
        self,
        *,
        original_payload: Any,
        candidate_payload: Any,
        provider: str,
        model: str = "",
        descriptor: Optional[Mapping[str, Any]] = None,
        description: str = "",
        notify: bool = True,
        redaction_findings: Optional[list[dict[str, str]]] = None,
        source_kind: str = "external_egress",
        risk_level: str = "high",
        semantic_status: str = "",
        warning: str = "",
    ) -> Optional[str]:
        """Issue one strict v2 egress review transaction.

        This named wrapper is convenient for adapters that do not need the
        legacy prompt terminology.  It serializes the candidate only for the
        compatibility ``prompt`` field; the authoritative event fields are
        the typed original/candidate payloads.
        """

        try:
            prompt = json.dumps(candidate_payload, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            prompt = str(candidate_payload)
        details = dict(descriptor or {})
        return await self.request_external_model_prompt(
            prompt,
            redacted_prompt=prompt,
            redaction_findings=redaction_findings,
            provider=provider,
            model=model,
            description=description,
            confirm=True,
            notify=notify,
            request_kind="external_data_review",
            source_kind=source_kind,
            risk_level=risk_level,
            semantic_status=semantic_status,
            warning=warning,
            egress_transaction=True,
            original_payload=original_payload,
            candidate_payload=candidate_payload,
            action=str(details.get("action") or ""),
            transport=str(details.get("transport") or ""),
            destination=str(details.get("destination") or ""),
            tool=str(details.get("tool") or ""),
            contract_version=2,
        )

    def _deny_egress_request(self, request: PermissionRequest, reason: str) -> None:
        """Resolve a v2 request as denied and consume its one-shot token."""

        request.status = PermissionStatus.DENIED
        token = (str(request.request_id), str(request.review_nonce or ""))
        self._consumed_egress_tokens.add(token)
        payload = {
            "approved": False,
            "final_payload": "",
            "contract_version": 2,
            "review_nonce": str(request.review_nonce or ""),
            "binding_digest": str(request.binding_digest or ""),
        }
        if request.future and not request.future.done():
            if request.loop and request.loop.is_running():
                request.loop.call_soon_threadsafe(request.future.set_result, payload)
            else:
                request.future.set_result(payload)
        logger.warning("[ExternalLLMPermission] Egress review denied (%s)", reason)
    
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
        if not isinstance(request_id, str) or not request_id.strip():
            logger.warning("[ExternalLLMPermission] Malformed permission request ID")
            return
        if type(approved) is not bool:
            logger.warning("[ExternalLLMPermission] Malformed permission approval: %s", request_id)
            return
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
        final_payload: Any = None,
        contract_version: Any = None,
        review_nonce: Any = None,
        binding_digest: Any = None,
        scope: Any = "once",
        requester_user_id: Optional[str] = None,
        requester_session_id: Optional[str] = None,
    ):
        """Handle user response for an external model prompt request."""
        if not isinstance(request_id, str) or not request_id.strip():
            logger.warning("[ExternalLLMPermission] Malformed external prompt request ID")
            return
        request = self._pending_requests.get(request_id)
        if not request:
            # Unknown/expired ids are intentionally indistinguishable from a
            # replay to callers.  Never resurrect a completed transaction.
            logger.warning(
                "[ExternalLLMPermission] Unknown external model prompt request ID: %s",
                request_id,
            )
            return

        scope_matches = not (
            (request.user_id and request.user_id != str(requester_user_id or ""))
            or (request.session_id and request.session_id != str(requester_session_id or ""))
        )
        if request.egress_transaction:
            token = (str(request.request_id), str(request.review_nonce or ""))
            if token in self._consumed_egress_tokens or (
                request.future is not None and request.future.done()
            ):
                logger.warning(
                    "[ExternalLLMPermission] Replayed external egress response: %s",
                    request_id,
                )
                return
            if not scope_matches:
                self._deny_egress_request(request, "scope_mismatch")
                return
            if type(approved) is not bool:
                self._deny_egress_request(request, "malformed_approval")
                return
            if type(contract_version) is not int or contract_version != 2:
                self._deny_egress_request(request, "stale_contract")
                return
            if not isinstance(review_nonce, str) or not review_nonce:
                self._deny_egress_request(request, "missing_nonce")
                return
            if not isinstance(binding_digest, str) or not binding_digest:
                self._deny_egress_request(request, "missing_binding")
                return
            if review_nonce != request.review_nonce or not _constant_time_equal(
                binding_digest, request.binding_digest or ""
            ):
                self._deny_egress_request(request, "binding_mismatch")
                return
            # Egress approvals are transaction-scoped.  A session grant would
            # make a later route/payload eligible without a fresh digest.
            if type(scope) is not str or scope not in {"", "once"}:
                self._deny_egress_request(request, "invalid_scope")
                return
            if not isinstance(final_payload, str):
                self._deny_egress_request(request, "malformed_final_payload")
                return
            final_binding_digest = ""
            if approved:
                final_binding_digest = build_egress_final_binding_digest(
                    binding_digest=binding_digest,
                    final_payload=final_payload,
                )
            payload = {
                "approved": approved,
                "final_payload": final_payload if approved else "",
                "contract_version": 2,
                "request_id": request.request_id,
                "review_nonce": review_nonce,
                "binding_digest": binding_digest,
                "final_binding_digest": final_binding_digest,
            }
            self._consumed_egress_tokens.add(token)
        else:
            if not scope_matches:
                logger.warning(
                    "[ExternalLLMPermission] External prompt scope mismatch: %s",
                    request_id,
                )
                return
            if type(approved) is not bool:
                logger.warning(
                    "[ExternalLLMPermission] Malformed external model approval: %s",
                    request_id,
                )
                return
            request.status = PermissionStatus.APPROVED if approved else PermissionStatus.DENIED
            payload = {"approved": approved, "prompt": prompt}

        request.status = PermissionStatus.APPROVED if approved else PermissionStatus.DENIED

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
    egress_transaction: bool = False,
    original_payload: Any = None,
    candidate_payload: Any = None,
    action: str = "",
    transport: str = "",
    destination: str = "",
    tool: str = "",
    review_nonce: str = "",
    binding_digest: str = "",
    contract_version: int = 1,
) -> Optional[str]:
    if type(egress_transaction) is not bool:
        # Keep malformed v2 flags fail-closed even when the process is
        # between server lifecycles and no permission manager is installed.
        return None
    manager = get_permission_manager()
    if manager is None:
        outbound_prompt = (redacted_prompt or "").strip() or prompt
        # Missing UI/manager is fail-closed for all egress transactions,
        # including when an autonomous caller attempted ``confirm=False``.
        is_v2 = type(contract_version) is int and contract_version == 2
        inferred_egress = bool(
            egress_transaction
            or original_payload is not None
            or candidate_payload is not None
            or is_v2
        )
        return outbound_prompt if not (confirm or inferred_egress) else None

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
        egress_transaction=egress_transaction,
        original_payload=original_payload,
        candidate_payload=candidate_payload,
        action=action,
        transport=transport,
        destination=destination,
        tool=tool,
        review_nonce=review_nonce,
        binding_digest=binding_digest,
        contract_version=contract_version,
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
    except Exception:
        logger.error("[ExternalLLMPermission] Permission check failed")
        return False
