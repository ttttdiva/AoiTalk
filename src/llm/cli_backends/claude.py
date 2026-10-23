"""
Claude Code CLI backend implementation

Usage: claude -p "prompt" --output-format stream-json --verbose [--model model]
       claude -p "prompt" --mcp-config '{"mcpServers": {...}}'
Docs: https://code.claude.com/docs/en/headless

出力は JSONL（stream-json）で受け取り、途中の assistant テキストと thinking を
ストリームイベントとして配信する。最終回答は ``{"type":"result"}`` 行の
``result`` フィールドだけを採用する。
"""

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import List, Dict, Any, Optional, Tuple
from ...utils.subprocess_env import build_aoitalk_subprocess_env
from ..cli_native_capabilities import NATIVE_EDIT, NATIVE_READ, NATIVE_SHELL
from .base import (
    CLIBackendBase,
    CLIEventCallback,
    CLIInvocationOptions,
    CLISessionCapabilities,
    _windows_hidden_creationflags,
)
from ..turn_stream_events import (
    emit_assistant_text,
    emit_thinking,
    emit_tool_end,
    emit_tool_start,
)

logger = logging.getLogger(__name__)

# result 行も assistant テキストも取れなかった時にユーザーへ返す固定文言。
# 生の JSONL / stderr をそのまま回答にしないためのフォールバック。
CLAUDE_EMPTY_RESULT_MESSAGE = (
    "Claude Code CLI から回答テキストを取得できませんでした。"
    "CLI のバージョンや認証状態を確認してください。"
)

# 古い CLI が stream-json を解さない場合、プロセス単位で旧形式へ切り替える。
_stream_json_supported = True


def _set_stream_json_supported(value: bool) -> None:
    global _stream_json_supported
    _stream_json_supported = bool(value)


def stream_json_supported() -> bool:
    """テスト・診断用に現在の出力形式判定を返す。"""
    return _stream_json_supported


def reset_stream_json_support() -> None:
    """プロセス単位のフォールバック判定を初期化する（テスト用）。"""
    _set_stream_json_supported(True)


# Agent Team CLI-native opt-in (mirrors ``codex.agent_team_cli_context``).
_active_agent_team_cli: ContextVar[dict[str, Any] | None] = ContextVar(
    "claude_active_agent_team_cli",
    default=None,
)

# CLI approval prompts are always bypassed by the backend. Scoped AoiTalk
# tool exposure and workspace execution boundaries remain independent.
_FORMAL_AGENT_CHILD_ENV_NAMES = frozenset(
    {
        "AOITALK_AGENT_RUN_ID",
        "AOITALK_AGENT_WORK_ITEM_ID",
        "AOITALK_AGENT_SESSION_ID",
        "AOITALK_AGENT_ID",
        "AOITALK_AGENT_REVISION_ID",
        "AOITALK_AGENT_BINDING_ID",
        "AOITALK_AGENT_BINDING_SNAPSHOT_HASH",
        "AOITALK_AGENT_CAPABILITY_MANIFEST_DIGEST",
        "AOITALK_AGENT_AUTHORITY_DIGEST",
        "AOITALK_AGENT_LEASE_OWNER",
        "AOITALK_AGENT_LEASE_TOKEN",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    }
)
_FORMAL_AGENT_DATABASE_ENV_NAMES = frozenset(
    {"POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"}
)
_MCP_SAFE_INLINE_ENV_NAMES = frozenset(
    {
        "BROWSER",
        "CI",
        "HOST",
        "HOSTNAME",
        "LANG",
        "LC_ALL",
        "NODE_ENV",
        "NO_COLOR",
        "PATH",
        "PORT",
        "PYTHON_DOTENV_DISABLED",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
        "TERM",
        "TZ",
    }
)


def _mcp_secret_env_name(name: str) -> bool:
    normalized = str(name or "").strip().upper()
    return any(
        marker in normalized
        for marker in (
            "API_KEY",
            "ACCESS_KEY",
            "PASSWORD",
            "TOKEN",
            "SECRET",
            "CREDENTIAL",
            "PRIVATE_KEY",
            "SESSION",
            "COOKIE",
        )
    )


@contextmanager
def agent_team_cli_context(*, workspace_access: str = "read"):
    """Mark a Claude Code invocation as an Agent Team CLI-native child.

    The workspace ceiling still governs AoiTalk's scoped tool exposure;
    CLI permission approval prompts are independently bypassed.
    """
    access = str(workspace_access or "read").strip().lower()
    if access not in {"read", "write"}:
        access = "read"
    token = _active_agent_team_cli.set({"workspace_access": access})
    try:
        yield
    finally:
        _active_agent_team_cli.reset(token)


def _scope_allows_write(scope: Any) -> bool:
    return str(getattr(scope, "workspace_access_level", "") or "") == "write"


# ---------------------------------------------------------------------------
# Version / capability detection
# ---------------------------------------------------------------------------

# Minimum Claude Code versions for explicitly named models, from
# https://code.claude.com/docs/en/model-config ("Minimum Claude Code
# Versions").  Aliases such as ``opus`` resolve inside the CLI, so an older CLI
# silently falls back to an older model instead of failing; the table lets
# AoiTalk warn about that instead of guessing.
CLAUDE_CODE_MODEL_MIN_VERSIONS: Dict[str, Tuple[int, int, int]] = {
    "claude-opus-5-5": (2, 1, 280),
    "claude-fable-5-1": (2, 1, 257),
    "fable": (2, 1, 257),
    "claude-opus-5": (2, 1, 219),
    "claude-sonnet-5": (2, 1, 197),
}
# The alias that users pick for "latest Opus" and the model it must reach.
CLAUDE_CODE_ALIAS_TARGETS: Dict[str, str] = {"opus": "claude-opus-5-5"}
# ``--effort ultracode`` requires Claude Code v2.1.203 or later.
CLAUDE_CODE_EFFORT_MIN_VERSIONS: Dict[str, Tuple[int, int, int]] = {
    "ultracode": (2, 1, 203),
}
# Flags AoiTalk relies on, probed from ``claude --help`` instead of versions.
CLAUDE_CODE_REQUIRED_FLAGS: Tuple[str, ...] = (
    "--output-format",
    "--model",
    "--effort",
    "--permission-mode",
    "--mcp-config",
    "--strict-mcp-config",
    "--session-id",
    "--resume",
)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_STATUS_CACHE_TTL_SECONDS = 300.0
_status_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_status_lock = threading.Lock()


def parse_claude_code_version(text: str) -> Optional[Tuple[int, int, int]]:
    match = _VERSION_RE.search(str(text or ""))
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _format_version(version: Tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def claude_code_model_requirement(model: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """Return the minimum CLI version for a model id or alias, if known."""
    key = str(model or "").strip().lower()
    if not key:
        return None
    key = key.split("[", 1)[0]  # ``opus[1m]`` -> ``opus``
    target = CLAUDE_CODE_ALIAS_TARGETS.get(key, key)
    return CLAUDE_CODE_MODEL_MIN_VERSIONS.get(target)


def claude_code_compatibility_warnings(
    version: Optional[Tuple[int, int, int]],
    *,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    flags: Optional[Dict[str, Optional[bool]]] = None,
) -> List[str]:
    warnings: List[str] = []
    model_key = str(model or "").strip()
    required = claude_code_model_requirement(model_key)
    if required and version and version < required:
        alias_target = CLAUDE_CODE_ALIAS_TARGETS.get(model_key.lower().split("[", 1)[0])
        if alias_target:
            warnings.append(
                f"Claude Code {_format_version(version)} では alias '{model_key}' が "
                f"{alias_target} を指しません。v{_format_version(required)} 以降へ更新してください"
                "（`claude update`）。"
            )
        else:
            warnings.append(
                f"モデル '{model_key}' には Claude Code v{_format_version(required)} 以降が必要です"
                f"（現在 {_format_version(version)}）。`claude update` で更新してください。"
            )
    effort_key = str(effort or "").strip().lower()
    effort_required = CLAUDE_CODE_EFFORT_MIN_VERSIONS.get(effort_key)
    if effort_required and version and version < effort_required:
        warnings.append(
            f"--effort {effort_key} には Claude Code v{_format_version(effort_required)} 以降が必要です。"
        )
    if flags:
        # ``False`` is reserved for an explicit negative capability probe.
        # Claude Code's help is intentionally sparse, so an omitted flag is
        # represented as ``None`` (unknown), never as unsupported.
        missing = [flag for flag, present in flags.items() if present is False]
        if missing:
            warnings.append(
                "この Claude Code は次の引数に対応していません: " + ", ".join(missing)
            )
    return warnings


def detect_claude_code_status(
    bin_path: Optional[str] = None,
    *,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Probe ``claude --version`` / ``--help`` without starting a generation."""
    binary = str(bin_path or os.getenv("CLAUDE_BIN", "claude") or "claude")
    now = time.monotonic()
    probe: Optional[Dict[str, Any]] = None
    if use_cache:
        with _status_lock:
            cached = _status_cache.get(binary)
            if cached and now - cached[0] < _STATUS_CACHE_TTL_SECONDS:
                probe = dict(cached[1])
    if probe is None:
        probe = _probe_claude_code(binary)
        with _status_lock:
            _status_cache[binary] = (now, dict(probe))
    version = tuple(probe["version_tuple"]) if probe.get("version_tuple") else None
    return {
        "installed": bool(probe.get("installed")),
        "version": probe.get("version"),
        "flags": dict(probe.get("flags") or {}),
        "model": model or None,
        "effort": effort or None,
        "model_min_version": (
            _format_version(req) if (req := claude_code_model_requirement(model)) else None
        ),
        "warnings": (
            claude_code_compatibility_warnings(
                version,  # type: ignore[arg-type]
                model=model,
                effort=effort,
                flags=probe.get("flags") or None,
            )
            if probe.get("installed")
            else ["Claude Code CLI が見つかりません。インストール後 `claude --version` で確認してください。"]
        ),
    }


def reset_claude_code_status_cache() -> None:
    with _status_lock:
        _status_cache.clear()


def _probe_claude_code(binary: str) -> Dict[str, Any]:
    resolved = shutil.which(binary)
    if not resolved:
        return {"installed": False, "version": None, "version_tuple": None, "flags": {}}

    def run(args: List[str]) -> str:
        try:
            result = subprocess.run(
                [resolved, *args],
                capture_output=True,
                check=False,
                timeout=10,
                env=build_aoitalk_subprocess_env(),
                creationflags=_windows_hidden_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        out = result.stdout or b""
        err = result.stderr or b""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        return f"{out}\n{err}"

    version_text = run(["--version"])
    version_tuple = parse_claude_code_version(version_text)
    help_text = run(["--help"]).lower()
    # ``claude --help`` is not a complete capability manifest.  Presence is
    # positive evidence; absence is unknown and must not become a warning.
    flags = (
        {flag: (True if flag in help_text else None) for flag in CLAUDE_CODE_REQUIRED_FLAGS}
        if help_text.strip()
        else {flag: None for flag in CLAUDE_CODE_REQUIRED_FLAGS}
    )
    return {
        "installed": True,
        "version": _format_version(version_tuple) if version_tuple else None,
        "version_tuple": list(version_tuple) if version_tuple else None,
        "flags": flags,
    }


def _block_text(value: Any) -> str:
    """content block（str / list / dict）から表示用テキストを取り出す。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "output", "result"):
            text = _block_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        return "\n".join(part for part in (_block_text(item) for item in value) if part)
    return ""


_DENIAL_SECRET_RE = re.compile(
    r"(?i)(bearer\s+\S+|(?:token|secret|password|passwd|api[_-]?key|authorization)\s*[=:]\s*\S+|sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{12,})"
)
_DENIAL_MAX_ITEMS = 10
_DENIAL_EXECUTABLE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{0,63}")
# userinfo/port/pathを許さず scheme://host だけを受理する。
_DENIAL_URL_RE = re.compile(r"https?://([A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)(?:/[^@\s]*)?")


_DENIAL_MAX_EXECUTABLES = 4
_DENIAL_ENV_ASSIGN_RE = re.compile(r"(?:\$env:)?[A-Za-z_][A-Za-z0-9_]*=", re.IGNORECASE)
_DENIAL_INLINE_FLAGS = frozenset({"-c", "-command", "-e"})
_DENIAL_EXE_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1")
_DENIAL_UNKNOWN = "[command]"


def _split_denial_command(command: str) -> Optional[Tuple[List[List[str]], Dict[str, bool]]]:
    """quote/escape-awareにtop-levelの && || ; | だけで区間分割し、単語へ分ける。

    引用内・escape・backtickは不透明。解釈に確信が持てない(不完全quote、escape、backtick)
    場合はNoneを返し、呼び出し側は[command]へ倒す。
    """
    shape = {key: False for key in ("chain", "pipe", "redirect", "subshell", "inline_code", "env_assignment")}
    segments: List[List[str]] = [[]]
    word: List[str] = []
    quote = ""
    certain = True

    def end_word() -> None:
        if word:
            segments[-1].append("".join(word))
            word.clear()

    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if quote:
            if ch == quote:
                quote = ""
            elif ch == "`" or (ch == "$" and nxt == "("):
                shape["subshell"] = True
                certain = False
            elif ch == "\\" and quote == '"' and nxt in ('"', "\\"):
                word.append(ch)
                i += 1
                ch = nxt
            word.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            word.append(ch)
        elif ch == "`":
            shape["subshell"] = True
            certain = False
            word.append(ch)
        elif ch == "\\" and nxt in ("'", '"', ";", "|", "&", "`"):
            certain = False
            word.append(ch)
        elif ch == "$" and nxt == "(":
            shape["subshell"] = True
            certain = False
            word.append(ch)
        elif ch in "()":
            shape["subshell"] = True
            certain = False
            word.append(ch)
        elif ch == "^" or (ch == "#" and not word):
            # cmdのcaret escapeやshell commentは解釈に確信が持てないためopaque。
            certain = False
            word.append(ch)
        elif ch.isspace():
            end_word()
        elif ch in ";|&" and (ch != "&" or nxt == "&"):
            end_word()
            if ch == "&" or ch == ";":
                shape["chain"] = True
                i += 1 if ch == ";" else 2
            elif nxt == "|":
                shape["chain"] = True
                i += 2
            else:
                shape["pipe"] = True
                i += 1
            segments.append([])
            continue
        else:
            if ch in "<>":
                shape["redirect"] = True
            word.append(ch)
        i += 1
    end_word()
    if quote or not certain:
        return None
    for words in segments:
        # `$env:X= value` / `X= value` は代入値が次のtokenに分離され得るためopaque。
        if any(t.lower().startswith("$env:") or t.endswith("=") for t in words):
            return None
    for words in segments:
        for token in words:
            if token.lower() in _DENIAL_INLINE_FLAGS:
                shape["inline_code"] = True
    return [words for words in segments if words], shape


def _segment_executable(words: List[str], shape: Dict[str, bool]) -> str:
    """区間先頭のlauncher/代入を読み飛ばし、素の実行ファイル名と確定できる場合だけ返す。"""
    for token in words:
        if token in ("&", "."):
            continue
        if _DENIAL_ENV_ASSIGN_RE.match(token):
            shape["env_assignment"] = True
            continue
        if "://" in token or any(char in token for char in ("'", '"', "@", "?", "=")):
            return _DENIAL_UNKNOWN
        name = re.split(r"[\\/]", token)[-1]
        lowered = name.lower()
        for suffix in _DENIAL_EXE_SUFFIXES:
            if lowered.endswith(suffix):
                name = name[: -len(suffix)]
                break
        if not _DENIAL_EXECUTABLE_RE.fullmatch(name):
            return _DENIAL_UNKNOWN
        return _DENIAL_SECRET_RE.sub("[redacted]", name)
    return _DENIAL_UNKNOWN


def _summarize_denial_command(command: str) -> Dict[str, Any]:
    shape = {key: False for key in ("chain", "pipe", "redirect", "subshell", "inline_code", "env_assignment")}
    parsed = _split_denial_command(command)
    if parsed is None:
        # 不完全quote/escape/subshellは全体をopaqueにし、shapeは見えた範囲の真偽値だけ返す。
        shape["subshell"] = "`" in command or "(" in command
        shape["env_assignment"] = bool(_DENIAL_ENV_ASSIGN_RE.match(command.lstrip()))
        return {"executables": [_DENIAL_UNKNOWN], "shape": shape}
    segments, shape = parsed
    executables: List[str] = []
    for words in segments:
        exe = _segment_executable(words, shape)
        if exe not in executables:
            executables.append(exe)
    return {"executables": (executables or [_DENIAL_UNKNOWN])[:_DENIAL_MAX_EXECUTABLES], "shape": shape}


def _denial_path_scope(value: str, observed_cwd: str) -> str:
    """観測cwdに対するinside_cwd/outside_cwd。cwd未観測は unknown。"""
    if not observed_cwd:
        return "unknown"
    try:
        base = os.path.normcase(os.path.normpath(observed_cwd))
        raw = value.strip()
        full = raw if os.path.isabs(raw) else os.path.join(observed_cwd, raw)
        full = os.path.normcase(os.path.normpath(full))
        return "inside_cwd" if os.path.commonpath([base, full]) == base else "outside_cwd"
    except ValueError:
        return "outside_cwd"


def _summarize_denial_target(tool_input: Any, observed_cwd: str = "") -> Dict[str, Any]:
    """拒否対象の短い要約。tool_input全量は保持せず、秘密を含み得る部分は落とす。"""
    if not isinstance(tool_input, dict):
        return {"target": ""}
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            parts = [part for part in re.split(r"[\\/]+", value.strip()) if part]
            target = _DENIAL_SECRET_RE.sub("[redacted]", "/".join(parts[-2:]))[:120]
            return {"target": target, "scope": _denial_path_scope(value, observed_cwd)}
    command = tool_input.get("command")
    url = tool_input.get("url")
    if isinstance(command, str) and command.strip():
        info = _summarize_denial_command(command)
        # 従来のtargetは先頭区間の実行ファイル名(確定できない場合は[command])。
        first = command.strip().split()[0]
        target = first if _DENIAL_EXECUTABLE_RE.fullmatch(first) and info["executables"][0] == first else _DENIAL_UNKNOWN
        return {"target": _DENIAL_SECRET_RE.sub("[redacted]", target)[:120], "command": info}
    if isinstance(url, str) and url.strip():
        match = _DENIAL_URL_RE.fullmatch(url.strip().split("?")[0].split("#")[0])
        target = match.group(0).split("/")[0] + "//" + match.group(1) if match else "[url]"
        return {"target": _DENIAL_SECRET_RE.sub("[redacted]", target)[:120]}
    return {"target": ""}


def _summarize_permission_denials(denials: Any, observed_cwd: str = "") -> Dict[str, Any]:
    """permission_denialsを件数 + tool_name/tool_use_id/対象要約へ縮約する。"""
    items_in = denials if isinstance(denials, list) else [denials]
    items: List[Dict[str, Any]] = []
    for denial in items_in[:_DENIAL_MAX_ITEMS]:
        if not isinstance(denial, dict):
            items.append({"tool_name": "", "tool_use_id": "", "target": ""})
            continue
        items.append(
            {
                "tool_name": str(denial.get("tool_name") or denial.get("tool") or "")[:80],
                "tool_use_id": str(denial.get("tool_use_id") or "")[:80],
                **_summarize_denial_target(denial.get("tool_input"), observed_cwd),
            }
        )
    return {"count": len(items_in), "items": items}


class ClaudeCLIBackend(CLIBackendBase):
    """Claude Code CLI backend implementation"""

    scoped_execution_delegate = True
    provider_state_env_name = "CLAUDE_CONFIG_DIR"
    provider_state_root_supported = True
    # Claude Code owns repository file search/read/edit and shell natively.
    native_repository_tools = True
    # --tools controls exposure; --allowedTools only controls permission.
    # Deny MCP tools too, including servers from a resumed native session.
    # Contract: https://code.claude.com/docs/en/cli-reference
    tool_free_args = ("--tools", "", "--disallowedTools", "*")
    supports_tool_free_capability_gates = True
    # Claude can expose a finite, explicitly named formal Agent MCP set via
    # ``--tools``. Unknown/ambient MCP schemas stay outside that allowlist.
    supports_formal_agent_mcp = True
    supports_capability_ceiling = True
    # Claude's provider state is redirected to the issued session cache root
    # and the invocation still runs through the scoped WSL+bwrap backend.
    supports_scoped_native_sessions = True
    scoped_native_supports_explicit_session_id = True

    _native_sessions_available: Optional[bool] = None
    _strict_mcp_config_available: Optional[bool] = None

    def __init__(
        self,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ):
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._stream_round = 0
        # 最終回答と同一のassistant textを二重配信しないためのバッファ。
        self._pending_assistant_texts: List[Tuple[str, int]] = []
        self._active_tool_uses: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        self._formal_mcp_child_env: Dict[str, str] = {}
        self._formal_mcp_context_key: tuple[str, str, str] | None = None
        # stream-json 行を1つでも観測したか（旧CLIフォールバック判定に使う）。
        self._saw_stream_json_line = False
        super().__init__()

    def _reset_stream_state(self) -> None:
        self._stream_round = 0
        self._pending_assistant_texts = []
        self._active_tool_uses = {}
        self._saw_stream_json_line = False

    def get_session_capabilities(self) -> CLISessionCapabilities:
        if self.__class__._native_sessions_available is None:
            self.__class__._native_sessions_available = self.cli_help_contains(
                os.getenv("CLAUDE_BIN", "claude"),
                ["--help"],
                "--resume",
                "--session-id",
                "--output-format",
            )
        supported = bool(self.__class__._native_sessions_available)
        return CLISessionCapabilities(
            native_sessions=supported,
            supports_resume=supported,
            supports_follow_up=supported,
            fallback_to_stateless=True,
            supports_explicit_session_id=supported,
            supports_detach=False,
        )

    @classmethod
    def formal_mcp_configuration_supported(cls, *, allow_probe: bool = True) -> bool:
        """Require a preflight-confirmed strict MCP flag before auth.

        Admission may request a cache-only read when an inherited scope was
        shadowed earlier. A currently empty ContextVar must not authorize a
        host probe that the original caller's scope would have denied.
        """

        if cls._strict_mcp_config_available is not None:
            return bool(cls._strict_mcp_config_available)
        if not allow_probe:
            return False
        # A scoped run must not launch a host help probe.  Deployment must
        # preflight the installed CLI before binding the native scope; an
        # unknown capability therefore fails closed here.
        try:
            from ...security.agent_run_scope import get_current_run_scope

            if get_current_run_scope() is not None:
                return False
        except Exception:
            return False
        try:
            status = detect_claude_code_status(
                os.getenv("CLAUDE_BIN", "claude"),
                use_cache=True,
            )
            flag = (status.get("flags") or {}).get("--strict-mcp-config")
            cls._strict_mcp_config_available = flag is True
        except Exception:
            cls._strict_mcp_config_available = False
        return bool(cls._strict_mcp_config_available)

    @classmethod
    def reset_formal_mcp_capability_cache(cls) -> None:
        cls._strict_mcp_config_available = None

    def create_native_session_id(self) -> Optional[str]:
        return str(uuid.uuid4())

    def native_repository_capabilities_for_scope(self, scope: Any) -> frozenset:
        """Classify native tools within AoiTalk's trusted repository boundary.

        Claude bypasses its own approval prompts, but the issued workspace
        scope and the Agent Team read ceiling remain authoritative.
        """
        capabilities = {NATIVE_READ}
        team = _active_agent_team_cli.get()
        if _scope_allows_write(scope) and (team is None or team.get("workspace_access") == "write"):
            capabilities.update({NATIVE_EDIT, NATIVE_SHELL})
        return frozenset(capabilities)

    def native_agent_tool_allowlist_args(
        self,
        capabilities,
        mcp_servers=(),
    ) -> list[str]:
        """Expose only the filesystem tools granted to a scoped Agent turn.

        Claude's deny-list alone is future-tool permissive.  ``--tools`` is
        therefore the primary Agent boundary; web, browser, delegation, and
        unknown provider tools remain absent even if Claude adds them later.
        """

        selected = {str(item).strip() for item in capabilities or ()}
        tools: list[str] = []
        if "read" in selected:
            tools.extend(("Read", "Glob", "Grep", "LS"))
        if "edit" in selected:
            tools.extend(("Write", "Edit", "MultiEdit", "NotebookEdit"))
        if "shell" in selected:
            tools.append("Bash")
        for server_name in mcp_servers or ():
            normalized = str(server_name or "").strip()
            if normalized and re.fullmatch(r"[A-Za-z0-9_-]+", normalized):
                tools.append(f"mcp__{normalized}__*")
        return ["--tools", ",".join(dict.fromkeys(tools))]

    def native_feature_gate_args(self, disabled_features) -> list[str]:
        """Bare deny rules remove tools from both the context and execution."""
        denied: set[str] = set()
        if "filesystem" in disabled_features:
            denied.update({"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS", "Bash"})
        if "search" in disabled_features:
            denied.update({"WebSearch", "WebFetch"})
        if "mcp" in disabled_features:
            denied.add("mcp__*")
        if disabled_features:
            denied.update({"Agent", "Task"})
        return ["--disallowedTools", *sorted(denied)] if denied else []

    def native_mcp_gate_args(self, blocked_servers) -> list[str] | None:
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", str(name)) for name in blocked_servers):
            return None
        # Remove matching native schemas too, including a provider's resumed
        # or host-configured copy of a server omitted from this turn's config.
        return ["--disallowedTools", *[f"mcp__{name}__*" for name in sorted(blocked_servers)]] if blocked_servers else []

    def extract_native_session_id(self, raw_output: str) -> Optional[str]:
        for event in self._iter_json_events(raw_output):
            event_type = str(event.get("type") or "")
            if event_type in {"system", "result", "session_started"}:
                for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
                    value = event.get(key)
                    if value:
                        return str(value).strip()
        return super().extract_native_session_id(raw_output)

    def validate_invocation_options(self, options: CLIInvocationOptions) -> None:
        super().validate_invocation_options(options)
        if options.sandbox is not None:
            raise ValueError("Claude Code CLI has no per-invocation sandbox flag")
        policy = options.effective_approval_policy
        if policy not in {
            "native_default",
            "standard",
            "accept_edits",
        }:
            raise ValueError(f"Claude Code CLI does not support approval policy: {policy}")

    def observe_effective_launch_config(self, raw_output: str, stderr: str) -> None:
        """Record structured init fields when Claude emits them."""

        for event in self._iter_json_events(str(raw_output or "")):
            if str(event.get("type") or "").lower() not in {"system", "init"}:
                continue
            init = event.get("init") if isinstance(event.get("init"), dict) else event
            self._record_observed_launch_fields(
                cwd=init.get("cwd"),
                model=init.get("model"),
                permission_mode=init.get("permission_mode"),
            )
            return

    def _observe_native_permission_denied_events(self, events: List[Dict[str, Any]]) -> None:
        """system/permission_deniedを承認待ち(permission_event_count)と区別して数える。"""
        count = 0
        for event in events:
            if str(event.get("type") or "").lower() != "system":
                continue
            if str(event.get("subtype") or event.get("event") or "").lower().replace("-", "_") == "permission_denied":
                count += 1
        if count:
            self._last_cli_diagnostics["native_permission_denied_event_count"] = count

    def validate_success_output(self, raw_output: str, stderr: str) -> Optional[str]:
        """Reject structured Claude failures even when the CLI exits zero."""

        if self.get_invocation_options() is None:
            return None
        events = list(self._iter_json_events(str(raw_output or "")))
        if not events:
            return None
        self._observe_native_permission_denied_events(events)
        result_seen = False
        for event in events:
            if str(event.get("type") or "").lower() != "result":
                continue
            result_seen = True
            result_payload = event.get("result") if isinstance(event.get("result"), dict) else {}
            denials = event.get("permission_denials") or result_payload.get("permission_denials")
            is_error = bool(event.get("is_error")) or bool(result_payload.get("is_error")) or str(event.get("subtype") or "").lower() in {
                "error",
                "failed",
                "failure",
            }
            if is_error or bool(denials):
                code = "claude_permission_denied" if denials else "claude_result_error"
                self._set_cli_diagnostic(status="failed", state="failed", error_code=code)
                if denials:
                    summary = _summarize_permission_denials(denials, str(self._last_invocation_config.get("observed_cwd") or ""))
                    self._last_cli_diagnostics["permission_denial_count"] = summary["count"]
                    self._last_cli_diagnostics["permission_denial_summary"] = summary["items"]
                    detail = f"denied_count={summary['count']} denied={json.dumps(summary['items'], ensure_ascii=False)}"
                    return f"Claude Code CLI reported {code}: {detail[:1000]}"
                detail = event.get("error") or result_payload.get("error") or event.get("result") or "Claude Code result reported an error"
                return f"Claude Code CLI reported {code}: {str(detail)[:1000]}"
        if not result_seen:
            self._set_cli_diagnostic(
                status="failed",
                state="failed",
                error_code="claude_missing_result",
            )
            return "Claude Code CLI did not return a final result event"
        return None

    def execute_prompt(self, *args: Any, **kwargs: Any):
        self._reset_stream_state()
        success, output = super().execute_prompt(*args, **kwargs)
        if (
            success
            or self.get_invocation_options() is not None
            or not _stream_json_supported
            or self._saw_stream_json_line
            or getattr(self, "_aoitalk_egress_transaction", False)
        ):
            return success, output

        # stream-json 行を一度も観測せずに失敗した場合は、
        # 旧 CLI が --output-format stream-json / --verbose を解さない可能性がある。
        # 旧形式（--output-format json）で1回だけ再試行する。
        logger.warning(
            "[%s] stream-json output was not observed; retrying with legacy json format",
            self.provider_name,
        )
        self._reset_stream_state()
        retry_success, retry_output = self._execute_prompt_with_legacy_format(
            *args, **kwargs
        )
        if not retry_success:
            # 旧形式でも失敗したなら stream-json が原因ではない。最初のエラーを返す。
            return success, output

        _set_stream_json_supported(False)
        logger.warning(
            "[%s] falling back to --output-format json for this process",
            self.provider_name,
        )
        return retry_success, retry_output

    def _execute_prompt_with_legacy_format(self, *args: Any, **kwargs: Any):
        previous = _stream_json_supported
        _set_stream_json_supported(False)
        try:
            return super().execute_prompt(*args, **kwargs)
        finally:
            _set_stream_json_supported(previous)

    @staticmethod
    def _extract_applied_launch_fields(
        cmd: List[str],
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        approval, permission, sandbox = CLIBackendBase._extract_applied_launch_fields(cmd)
        if "--dangerously-skip-permissions" in cmd:
            return "never", "bypassPermissions", sandbox
        return approval, permission, sandbox

    def get_cli_command(self, prompt: str) -> List[str]:
        """Build Claude Code CLI command

        Format: claude -p "prompt" --output-format stream-json --verbose [--model X]
        旧CLI向けフォールバック時は --output-format json のみを付ける。
        """
        bin_path = os.getenv("CLAUDE_BIN", "claude")
        cmd = [bin_path, "--dangerously-skip-permissions"]

        if prompt:
            cmd.extend(["-p", prompt])

        action = str(getattr(self, "_active_native_session_action", "stateless"))
        session_id = str(getattr(self, "_active_native_session_id", "") or "").strip()
        if action == "resume" and session_id:
            cmd.extend(["--resume", session_id])
        elif action == "start" and session_id:
            cmd.extend(["--session-id", session_id])

        if _stream_json_supported:
            # JSONL出力で途中経過（assistant text / thinking / tool_use）まで受け取る。
            # stream-json は print モードで --verbose が必須。
            cmd.extend(["--output-format", "stream-json", "--verbose"])
        else:
            cmd.extend(["--output-format", "json"])

        options = self.get_invocation_options()
        # モデル指定
        model = (
            str(options.model or "").strip()
            if options is not None
            else self._model or os.getenv("CLAUDE_MODEL")
        )
        if model:
            cmd.extend(["--model", model])

        reasoning_effort = (
            options.effective_effort
            if options is not None
            else (
                self._reasoning_effort
                if self._reasoning_effort is not None
                else os.getenv("CLAUDE_EFFORT")
            )
        )
        if reasoning_effort:
            cmd.extend(["--effort", reasoning_effort])

        # The CLI bypass flag is constant for all invocations. Do not append
        # --permission-mode because it could override the requested bypass.

        # ターン数制限
        max_turns = os.getenv("CLAUDE_MAX_TURNS")
        if max_turns:
            cmd.extend(["--max-turns", max_turns])

        # 許可するツール
        allowed_tools = os.getenv("CLAUDE_ALLOWED_TOOLS")
        agent_turn = False
        try:
            from ...services.turn_context import get_turn_context

            agent_turn = getattr(get_turn_context(), "agent_execution_binding", None) is not None
        except Exception:
            agent_turn = False
        if allowed_tools and not agent_turn:
            cmd.extend(["--allowedTools", allowed_tools])

        if options is not None and options.allowed_capabilities is not None:
            cmd.extend(self.capability_ceiling_args(options.allowed_capabilities))

        return cmd

    def capability_ceiling_args(self, capabilities) -> list[str]:
        """Expose only the tools the pinned ceiling grants (fail closed).

        ``--tools`` is the exposure allowlist, so unknown/new provider tools,
        web, delegation and every MCP tool stay absent.  The deny list and an
        empty strict MCP config also cover a resumed session's ambient tools.
        """

        selected = set(capabilities or ())
        native = set()
        if "workspace_read" in selected:
            native.add("read")
        if "workspace_write" in selected:
            native.add("edit")
        if "command_execute" in selected:
            native.add("shell")
        args = self.native_agent_tool_allowlist_args(native)
        denied = ["mcp__*", "Agent", "Task", "WebSearch", "WebFetch"]
        if "edit" not in native:
            denied += ["Write", "Edit", "MultiEdit", "NotebookEdit"]
        if "shell" not in native:
            denied += ["Bash", "BashOutput", "KillShell"]
        args += ["--disallowedTools", *denied]
        if "edit" not in native or "shell" not in native:
            args += ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        return args

    def get_provider_name(self) -> str:
        return "Claude Code"

    def get_scoped_provider_auth(self) -> Optional[Tuple[str, str]]:
        """Use the selected Windows Claude OAuth profile for scoped WSL runs."""

        workspace = None
        try:
            from ...services.agent_dialogue_workspace import (
                get_current_agent_dialogue_workspace,
            )

            workspace = get_current_agent_dialogue_workspace()
        except Exception:
            workspace = None
        if workspace is None or not bool(workspace.native_allowed):
            # A plain repository-scoped/tool-free compatibility call has no
            # native provider authority and therefore must not receive a
            # credential.  The issued interactive Agent workspace takes the
            # authenticated branch below.
            return None
        workspace.assert_provider_entry()
        if self.get_scoped_provider_env() and not self.formal_mcp_configuration_supported():
            raise RuntimeError("Claude strict MCP configuration was not preflight-confirmed")
        from ...security.claude_provider_credentials import (
            CLAUDE_OAUTH_ENV_NAME,
            resolve_claude_oauth_token,
        )

        return CLAUDE_OAUTH_ENV_NAME, resolve_claude_oauth_token()

    def get_scoped_provider_env(self) -> Dict[str, str]:
        """Return formal MCP values through the protected scoped channel."""

        if not self._formal_mcp_child_env:
            return {}
        try:
            from ...services.agent_run_service import get_current_agent_run_id

            current_run_id = str(get_current_agent_run_id() or "").strip()
        except Exception:
            return {}
        if not self._formal_mcp_context_key or current_run_id != self._formal_mcp_context_key[0]:
            return {}
        return dict(self._formal_mcp_child_env)

    def reset_scoped_provider_env(self) -> None:
        """Drop formal MCP values before the next launch projection."""

        self._formal_mcp_child_env = {}
        self._formal_mcp_context_key = None

    def get_scoped_guest_env(self) -> Dict[str, str]:
        """Expose only the server-derived Claude cache mount to the guest."""

        workspace = None
        try:
            from ...services.agent_dialogue_workspace import (
                get_current_agent_dialogue_workspace,
            )
            from ...security.agent_run_scope import get_current_run_scope

            workspace = get_current_agent_dialogue_workspace()
            scope = get_current_run_scope()
        except Exception:
            workspace = None
            scope = None
        if workspace is None or scope is None or not bool(workspace.native_allowed):
            return {}
        cache_root = workspace.working_directory / ".aoitalk-provider-cache"
        from ...security.wsl_bwrap_backend import scoped_scratch_guest_path

        guest_root = scoped_scratch_guest_path(scope, cache_root)
        return {
            "CLAUDE_CONFIG_DIR": f"{guest_root}/claude",
            # The OAuth token is intentionally placed in the provider process
            # only.  A tmpfs mounted at /proc prevents Claude Read and all
            # formal MCP descendants from inspecting environment aliases.
            "AOITALK_PROVIDER_HIDE_PROC": "1",
        }

    def get_subprocess_env(self) -> Dict[str, str]:
        """Keep Claude provider state inside the current Agent session root."""

        env = super().get_subprocess_env()
        # Claude launches run the owner's own commands (e.g. DB bootstrap),
        # which need python-dotenv to load the normal project .env.  The
        # generic builder's dotenv kill-switch is meant for MCP children only.
        env.pop("PYTHON_DOTENV_DISABLED", None)
        options = self.get_invocation_options()
        if options is not None and options.provider_state_root is not None:
            # The manual lane's explicit root is authoritative.  Do not let
            # an ambient AgentDialogue workspace silently replace it.
            return env
        try:
            from ...services.agent_dialogue_workspace import (
                get_current_agent_dialogue_workspace,
            )

            workspace = get_current_agent_dialogue_workspace()
            if workspace is not None and bool(workspace.native_allowed):
                cache_root = workspace.working_directory / ".aoitalk-provider-cache" / "claude"
                cache_root.mkdir(parents=True, exist_ok=True)
                env["CLAUDE_CONFIG_DIR"] = str(cache_root)
        except Exception:
            # The native gate performs the authoritative denial; this helper
            # must not invent a fallback provider home on an unbound turn.
            logger.debug("Claude Agent provider cache root unavailable", exc_info=True)
        return env

    @staticmethod
    def _iter_json_events(raw_output: str) -> List[Dict[str, Any]]:
        """JSONL 各行から dict イベントだけを取り出す。"""
        events: List[Dict[str, Any]] = []
        for line in str(raw_output or "").splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    @staticmethod
    def _assistant_text_blocks(event: Dict[str, Any]) -> List[str]:
        """assistant イベントの text ブロックだけを取り出す。"""
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return []
        texts: List[str] = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = _block_text(block.get("text"))
            if text.strip():
                texts.append(text)
        return texts

    def parse_output(self, raw_output: str) -> str:
        """Parse Claude Code output (stream-json JSONL / legacy single JSON)."""
        output = raw_output.strip()
        if not output:
            return output

        # 従来の --output-format json（単一JSON）にも後方互換で対応する。
        try:
            data = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict):
            self.set_last_usage(data.get("usage"))
            if "result" in data:
                return str(data.get("result") or "")

        result_text: Optional[str] = None
        assistant_texts: List[str] = []
        events = self._iter_json_events(output)
        for event in events:
            event_type = str(event.get("type") or "")
            if event_type == "assistant":
                assistant_texts.extend(self._assistant_text_blocks(event))
                continue
            if event_type != "result":
                continue
            self.set_last_usage(event.get("usage"))
            if "result" in event:
                result_text = str(event.get("result") or "")

        if result_text is not None:
            return result_text
        if not events:
            # JSONL ではない素のテキスト出力は従来どおりそのまま返す。
            return output

        self._saw_stream_json_line = True
        # result 行が無い場合でも、生の JSONL（stderr 混在を含む）は返さない。
        joined = "\n\n".join(text for text in assistant_texts if text.strip()).strip()
        if joined:
            return joined
        logger.warning(
            "[%s] no result line and no assistant text in CLI output",
            self.provider_name,
        )
        return CLAUDE_EMPTY_RESULT_MESSAGE

    def parse_error_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> Optional[str]:
        """失敗時も stream-json 行を観測済みとして記録し、要点だけを返す。"""
        events = self._iter_json_events(stdout)
        if not events:
            return None
        self._saw_stream_json_line = True
        details: List[str] = []
        for event in events:
            for key in ("error", "message", "result"):
                value = event.get(key)
                text = _block_text(value) if not isinstance(value, str) else value
                if text and text.strip() and text.strip() not in details:
                    details.append(text.strip())
        detail = "\n".join(details).strip()
        if detail:
            return f"Claude Code CLI failed (exit code {exit_code}): {detail[:2000]}"
        return f"Claude Code CLI failed (exit code {exit_code})"

    # ------------------------------------------------------------------
    # stream-json (JSONL) の逐次処理
    # ------------------------------------------------------------------

    def handle_stream_output_line(
        self,
        line: str,
        event_callback: CLIEventCallback,
    ) -> None:
        """Claude Code の stream-json 行を AoiTalk のストリームイベントへ変換する。"""
        stripped = line.strip()
        if not stripped.startswith("{"):
            return
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        # stream-json を実際に解釈できたので旧CLIフォールバックは不要。
        self._saw_stream_json_line = True

        event_type = str(event.get("type") or "")
        if event_type == "assistant":
            self._handle_assistant_line(event, event_callback)
            return
        if event_type == "user":
            self._handle_tool_result_line(event, event_callback)
            return
        if event_type == "result":
            self.set_last_usage(event.get("usage"))
            self._flush_pending_assistant_texts(
                event_callback,
                final_text=str(event.get("result") or ""),
            )
            return
        if event_type == "system" and str(event.get("subtype") or "") == "init":
            event_callback(
                "status_update",
                {
                    "status": "claude_cli_session_started",
                    "message": "Claude Code CLI session started",
                },
            )

    def _handle_assistant_line(
        self,
        event: Dict[str, Any],
        event_callback: CLIEventCallback,
    ) -> None:
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return

        # 新しいassistantメッセージが来た時点で、保留中テキストは最終回答ではない。
        self._flush_pending_assistant_texts(event_callback)

        round_index = self._stream_round
        texts: List[str] = []
        thoughts: List[str] = []
        tool_uses: List[Dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type == "text":
                text = _block_text(block.get("text"))
                if text.strip():
                    texts.append(text)
            elif block_type in {"thinking", "redacted_thinking"}:
                thought = _block_text(block.get("thinking")) or _block_text(
                    block.get("text")
                )
                if thought.strip():
                    thoughts.append(thought)
            elif block_type == "tool_use":
                tool_uses.append(block)

        for thought in thoughts:
            emit_thinking(
                event_callback,
                thought,
                round_index=round_index,
                kind="raw",
            )

        for text in texts:
            if tool_uses:
                # ツール呼び出しを伴うラウンドのテキストは確実に途中経過。
                emit_assistant_text(event_callback, text, round_index=round_index)
            else:
                # 最終回答の可能性があるため result 確定まで保留する。
                self._pending_assistant_texts.append((text, round_index))

        for block in tool_uses:
            tool_name = str(block.get("name") or "claude_cli_tool")
            tool_input = block.get("input")
            arguments = dict(tool_input) if isinstance(tool_input, dict) else {}
            operation_id = str(block.get("id") or "")
            if operation_id:
                self._active_tool_uses[operation_id] = (tool_name, arguments)
            emit_tool_start(
                event_callback,
                tool=tool_name,
                arguments=arguments,
                operation_id=operation_id,
                message=f"Claude Code CLI started {tool_name}",
            )

        self._stream_round += 1

    def _handle_tool_result_line(
        self,
        event: Dict[str, Any],
        event_callback: CLIEventCallback,
    ) -> None:
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            operation_id = str(block.get("tool_use_id") or "")
            tool_name, arguments = self._active_tool_uses.pop(
                operation_id,
                ("claude_cli_tool", {}),
            )
            output = _block_text(block.get("content"))
            is_error = bool(block.get("is_error"))
            emit_tool_end(
                event_callback,
                tool=tool_name,
                arguments=arguments,
                output="" if is_error else output,
                error=(output or "tool execution failed") if is_error else "",
                operation_id=operation_id,
                message=f"Claude Code CLI completed {tool_name}",
            )

    def _flush_pending_assistant_texts(
        self,
        event_callback: CLIEventCallback,
        final_text: Optional[str] = None,
    ) -> None:
        """保留中テキストを配信する。最終回答と同一のものは配信しない。"""
        pending = self._pending_assistant_texts
        self._pending_assistant_texts = []
        normalized_final = (final_text or "").strip()
        for text, round_index in pending:
            if normalized_final and text.strip() == normalized_final:
                continue
            emit_assistant_text(event_callback, text, round_index=round_index)

    def get_mcp_args(self, mcp_servers: Dict[str, Any]) -> List[str]:
        """Generate --mcp-config argument for Claude Code

        Claude Code supports MCP via:
            claude -p "prompt" --mcp-config '{"mcpServers": {...}}'

        Converts AoiTalk config.yaml MCP format to Claude Code's mcpServers format.
        Environment variable placeholders (${VAR}) are resolved to actual values.
        """
        if not mcp_servers:
            return []

        is_windows = platform.system() == "Windows"
        platform_key = "windows" if is_windows else "linux"

        claude_mcp_servers = {}
        self._formal_mcp_child_env = {}
        self._formal_mcp_context_key = None
        has_formal_context = False
        for name, server_config in mcp_servers.items():
            if not isinstance(server_config, dict):
                raise ValueError("Claude MCP server configuration is invalid")
            platform_config = server_config.get(platform_key, {})
            if not isinstance(platform_config, dict):
                raise ValueError("Claude MCP platform configuration is invalid")
            command = platform_config.get("command")
            args = platform_config.get("args", [])

            if not command:
                logger.warning(f"[Claude Code] MCP server '{name}': no {platform_key} command, skipping")
                continue
            if not isinstance(args, (list, tuple)):
                raise ValueError("Claude MCP server arguments are invalid")

            # 環境変数プレースホルダーを解決
            env_raw = server_config.get("env", platform_config.get("env", {}))
            if env_raw is None:
                env_raw = {}
            if not isinstance(env_raw, dict):
                raise ValueError("Claude MCP server environment is invalid")
            env_resolved = {}
            formal_context = server_config.get("_aoitalk_formal_agent_context")
            is_formal = isinstance(formal_context, dict)
            has_formal_context = has_formal_context or is_formal
            if is_formal:
                context_key = (
                    str(formal_context.get("run_id") or ""),
                    str(formal_context.get("session_id") or ""),
                    str(formal_context.get("binding_id") or ""),
                )
                if not all(context_key) or (
                    self._formal_mcp_context_key is not None
                    and self._formal_mcp_context_key != context_key
                ):
                    raise ValueError("formal Agent MCP context changed")
                self._formal_mcp_context_key = context_key
            for key, value in env_raw.items():
                env_key = str(key).strip()
                if not env_key or "\x00" in env_key or "\r" in env_key or "\n" in env_key:
                    raise ValueError("Claude MCP environment name is invalid")
                if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                    env_var = value[2:-1]
                    resolved = os.getenv(env_var, "")
                    if is_formal:
                        if env_key not in _FORMAL_AGENT_DATABASE_ENV_NAMES or env_var != env_key:
                            raise ValueError("formal Agent MCP environment is not approved")
                        if not resolved:
                            raise ValueError("formal Agent MCP database environment is unavailable")
                        self._set_formal_mcp_child_env(env_key, resolved)
                    elif (
                        env_key.upper() in _MCP_SAFE_INLINE_ENV_NAMES
                        and not _mcp_secret_env_name(env_var)
                        and resolved
                    ):
                        env_resolved[env_key] = resolved
                elif value is not None:
                    literal = str(value)
                    if "\x00" in literal or "\r" in literal or "\n" in literal:
                        raise ValueError("Claude MCP environment value is invalid")
                    if is_formal:
                        if env_key not in _FORMAL_AGENT_DATABASE_ENV_NAMES:
                            raise ValueError("formal Agent MCP environment is not approved")
                        self._set_formal_mcp_child_env(env_key, literal)
                    elif (
                        env_key.upper() in _MCP_SAFE_INLINE_ENV_NAMES
                        and not _mcp_secret_env_name(env_key)
                    ):
                        env_resolved[env_key] = literal

            if is_formal:
                # The MCP process must re-read Work/Revision/authority using
                # these exact values. Opaque lease and DB values are sent via
                # the protected scoped child channel, never in MCP argv.
                for key, value in (
                    ("AOITALK_AGENT_RUN_ID", formal_context.get("run_id")),
                    ("AOITALK_AGENT_WORK_ITEM_ID", formal_context.get("work_item_id")),
                    ("AOITALK_AGENT_SESSION_ID", formal_context.get("session_id")),
                    ("AOITALK_AGENT_ID", formal_context.get("agent_id")),
                    ("AOITALK_AGENT_REVISION_ID", formal_context.get("agent_revision_id")),
                    ("AOITALK_AGENT_BINDING_ID", formal_context.get("binding_id")),
                    ("AOITALK_AGENT_BINDING_SNAPSHOT_HASH", formal_context.get("binding_snapshot_hash")),
                    ("AOITALK_AGENT_CAPABILITY_MANIFEST_DIGEST", formal_context.get("capability_manifest_digest")),
                    ("AOITALK_AGENT_AUTHORITY_DIGEST", formal_context.get("authority_digest")),
                    ("AOITALK_AGENT_LEASE_OWNER", formal_context.get("lease_owner")),
                    ("AOITALK_AGENT_LEASE_TOKEN", formal_context.get("lease_token")),
                ):
                    if value not in (None, ""):
                        self._set_formal_mcp_child_env(str(key), str(value))

            claude_mcp_servers[name] = {
                "command": command,
                "args": args,
            }
            if env_resolved:
                claude_mcp_servers[name]["env"] = env_resolved

        if not claude_mcp_servers:
            return []

        mcp_config_json = json.dumps({"mcpServers": claude_mcp_servers}, ensure_ascii=False)
        logger.info(f"[Claude Code] MCP config: {len(claude_mcp_servers)} server(s) configured")
        args = ["--mcp-config", mcp_config_json]
        if has_formal_context:
            if not all(
                isinstance(server.get("_aoitalk_formal_agent_context"), dict)
                for server in mcp_servers.values()
            ):
                raise ValueError("formal Agent MCP cannot share a non-formal server list")
            args.append("--strict-mcp-config")
        return args

    def _set_formal_mcp_child_env(self, key: str, value: str) -> None:
        """Set one approved formal MCP value, rejecting conflicting snapshots."""

        if key not in _FORMAL_AGENT_CHILD_ENV_NAMES:
            raise ValueError("formal Agent MCP child environment is not approved")
        previous = self._formal_mcp_child_env.get(key)
        if previous is not None and previous != value:
            raise ValueError("formal Agent MCP child environment changed")
        self._formal_mcp_child_env[key] = value
