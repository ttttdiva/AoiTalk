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
import uuid
from typing import List, Dict, Any, Optional, Tuple
from .base import CLIBackendBase, CLIEventCallback, CLISessionCapabilities
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


class ClaudeCLIBackend(CLIBackendBase):
    """Claude Code CLI backend implementation"""

    scoped_execution_delegate = True

    _native_sessions_available: Optional[bool] = None

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

    def create_native_session_id(self) -> Optional[str]:
        return str(uuid.uuid4())

    def extract_native_session_id(self, raw_output: str) -> Optional[str]:
        for event in self._iter_json_events(raw_output):
            event_type = str(event.get("type") or "")
            if event_type in {"system", "result", "session_started"}:
                for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
                    value = event.get(key)
                    if value:
                        return str(value).strip()
        return super().extract_native_session_id(raw_output)

    def execute_prompt(self, *args: Any, **kwargs: Any):
        self._reset_stream_state()
        success, output = super().execute_prompt(*args, **kwargs)
        if success or not _stream_json_supported or self._saw_stream_json_line:
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

    def get_cli_command(self, prompt: str) -> List[str]:
        """Build Claude Code CLI command

        Format: claude -p "prompt" --output-format stream-json --verbose [--model X]
        旧CLI向けフォールバック時は --output-format json のみを付ける。
        """
        bin_path = os.getenv("CLAUDE_BIN", "claude")
        cmd = [bin_path]

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

        # モデル指定
        model = self._model or os.getenv("CLAUDE_MODEL")
        if model:
            cmd.extend(["--model", model])

        reasoning_effort = (
            self._reasoning_effort
            if self._reasoning_effort is not None
            else os.getenv("CLAUDE_EFFORT")
        )
        if reasoning_effort:
            cmd.extend(["--effort", reasoning_effort])

        # ターン数制限
        max_turns = os.getenv("CLAUDE_MAX_TURNS")
        if max_turns:
            cmd.extend(["--max-turns", max_turns])

        # 許可するツール
        allowed_tools = os.getenv("CLAUDE_ALLOWED_TOOLS")
        if allowed_tools:
            cmd.extend(["--allowedTools", allowed_tools])

        return cmd

    def get_provider_name(self) -> str:
        return "Claude Code"

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
        for name, server_config in mcp_servers.items():
            platform_config = server_config.get(platform_key, {})
            command = platform_config.get("command")
            args = platform_config.get("args", [])

            if not command:
                logger.warning(f"[Claude Code] MCP server '{name}': no {platform_key} command, skipping")
                continue

            # 環境変数プレースホルダーを解決
            env_raw = server_config.get("env", {})
            env_resolved = {}
            for key, value in env_raw.items():
                if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                    env_var = value[2:-1]
                    resolved = os.getenv(env_var, "")
                    if resolved:
                        env_resolved[key] = resolved
                    else:
                        logger.debug(f"[Claude Code] MCP '{name}': env var {env_var} not set, skipping")
                elif value is not None:
                    env_resolved[key] = str(value)

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
        return ["--mcp-config", mcp_config_json]
