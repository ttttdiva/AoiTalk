"""AgentRun イベント整形・記録ユーティリティ

terminal_mode.py から挙動不変で切り出した AgentRun 関連ヘルパ群と、
`_process_user_message_web` 内のネスト関数（イベント payload 構築・安全送出）を
まとめた `AgentRunEventEmitter` を提供する。ロジック・例外処理・送出内容は
移設前と完全に同一である。
"""

import json
import re
import time
from typing import Any, Optional

from ...llm.agentic_completion import response_looks_like_unfinished_work
from ...llm.tool_policy import PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES
from ...services.agent_run_service import AgentRunService
from ...services.agent_team_service import (
    AGENT_TEAM_MEMBER_LABELS,
    agent_team_delegate_member,
    agent_team_member_for,
    config_get,
)


_SEARCH_TOOL_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'、。]+")
_DOCS_SEARCH_HIT_RE = re.compile(r"^\s*([0-9a-fA-F]{8,36})\s*\|", re.MULTILINE)
_SEARCH_URL_LIMIT = 20
_AGENT_RUN_DELEGATION_TOOL_MEMBERS = {
    "advanced_reasoning_assistant": "advanced_reasoning",
    "utility_assistant": "utility",
    "media_assistant": "media",
    "spotify_assistant": "spotify",
    "scenario_assistant": "scenario",
    "writing_assistant": "writing",
    "import_assistant": "import",
}
_PROVIDER_MODEL_KEYS = {
    "openai": ("openai.model",),
    "gemini": ("gemini.model",),
    "openai_compatible_local": (
        "openai_compatible_local.model",
        "openai_compatible_local_model",
    ),
    "sglang": ("sglang.model", "sglang_model"),
    "openrouter": ("openrouter.model", "openrouter_model"),
    "ollama": ("ollama.model", "ollama_model"),
    "codex-cli": ("codex_cli.model",),
    "claude-cli": ("claude_cli.model",),
    "antigravity-cli": ("antigravity_cli.model",),
    "grok-cli": ("grok_cli.model",),
}


def _extract_search_tool_urls(output_text: str) -> list[str]:
    urls: list[str] = []
    for match in _SEARCH_TOOL_URL_RE.finditer(str(output_text or "")):
        url = match.group(0).rstrip(".,;:!?")
        if url not in urls:
            urls.append(url)
        if len(urls) >= _SEARCH_URL_LIMIT:
            break
    return urls


def _config_text(config: Any, key: str, default: str = "") -> str:
    return str(config_get(config, key, default) or "").strip()


def _main_agent_run_provider(config: Any) -> str:
    provider = _config_text(config, "llm_provider", "openai").lower()
    return provider or "openai"


def _main_agent_run_model(config: Any, provider: str) -> str:
    selected = _config_text(config, "llm_model")
    if selected:
        return selected
    for key in _PROVIDER_MODEL_KEYS.get(provider, ()):
        value = _config_text(config, key)
        if value:
            return value
    return ""


def _agent_run_member_context(config: Any, member_key: str) -> dict[str, str]:
    provider = ""
    model = ""
    member = agent_team_member_for(config, member_key)
    if member:
        provider = str(member.get("provider") or "").strip()
        model = str(member.get("model") or "").strip()
    if not provider:
        provider = _main_agent_run_provider(config)
    if not model:
        model = _main_agent_run_model(config, provider)
    return {
        "actor_type": "agent_team",
        "actor_key": member_key,
        "actor_label": AGENT_TEAM_MEMBER_LABELS.get(member_key, member_key),
        "provider": provider,
        "model": model,
    }


def _agent_run_tool_context(config: Any, data: dict[str, Any]) -> dict[str, str]:
    tool_result = data.get("tool_result")
    tool_name = str(data.get("tool") or data.get("tool_name") or "").strip()
    if not tool_name and isinstance(tool_result, dict):
        tool_name = str(tool_result.get("tool") or tool_result.get("name") or "").strip()
    if not tool_name:
        return {}

    if tool_name == "agent_team_delegate":
        tool_args = (
            data.get("tool_args") if isinstance(data.get("tool_args"), dict) else {}
        )
        member = agent_team_delegate_member(
            config,
            str(tool_args.get("role") or ""),
            delegation_group_id=str(tool_args.get("group") or "") or None,
        )
        if not member:
            return {}
        member_key = str(member.get("member_key") or tool_args.get("role") or "").strip()
        provider = str(member.get("provider") or "").strip()
        provider = provider or _main_agent_run_provider(config)
        model = str(member.get("model") or "").strip()
        model = model or _main_agent_run_model(config, provider)
        label = str(
            member.get("label")
            or AGENT_TEAM_MEMBER_LABELS.get(member_key, member_key)
            or member_key
        )
        return {
            "actor_type": "agent_team",
            "actor_key": member_key,
            "actor_label": label,
            "provider": provider,
            "model": model,
            "mode": str(
                member.get("mode") or member.get("reasoning_effort") or ""
            ).strip(),
            "group_id": str(member.get("group_id") or "").strip(),
        }

    member_key = _AGENT_RUN_DELEGATION_TOOL_MEMBERS.get(tool_name)
    if member_key:
        return _agent_run_member_context(config, member_key)
    return {}


def _enrich_agent_run_event_payload(
    config: Any,
    data: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(data or {})
    context = _agent_run_tool_context(config, payload)
    if not context:
        return payload

    payload.setdefault("actor_type", context.get("actor_type"))
    payload.setdefault("actor_key", context.get("actor_key"))
    payload.setdefault("agent_member_key", context.get("actor_key"))
    payload.setdefault("actor_label", context.get("actor_label"))
    payload.setdefault("agent_label", context.get("actor_label"))
    payload.setdefault("provider", context.get("provider"))
    payload.setdefault("model", context.get("model"))
    payload.setdefault("mode", context.get("mode"))
    payload.setdefault("reasoning_effort", context.get("mode"))
    payload.setdefault("group_id", context.get("group_id"))

    tool_result = payload.get("tool_result")
    if isinstance(tool_result, dict):
        tool_result = dict(tool_result)
        tool_result.setdefault("actor_type", context.get("actor_type"))
        tool_result.setdefault("actor_key", context.get("actor_key"))
        tool_result.setdefault("actor_label", context.get("actor_label"))
        tool_result.setdefault("provider", context.get("provider"))
        tool_result.setdefault("model", context.get("model"))
        tool_result.setdefault("mode", context.get("mode"))
        tool_result.setdefault("reasoning_effort", context.get("mode"))
        tool_result.setdefault("group_id", context.get("group_id"))
        payload["tool_result"] = tool_result
    return payload


def _agent_run_tool_operation_signature(data: dict[str, Any]) -> str:
    tool_result = data.get("tool_result")
    tool_name = str(data.get("tool") or data.get("tool_name") or "").strip()
    if not tool_name and isinstance(tool_result, dict):
        tool_name = str(tool_result.get("tool") or tool_result.get("name") or "").strip()
    arguments = data.get("tool_args") or data.get("arguments") or data.get("args")
    if not isinstance(arguments, dict) and isinstance(tool_result, dict):
        arguments = tool_result.get("arguments") or tool_result.get("args")
    if not isinstance(arguments, dict):
        arguments = {}
    return f"{tool_name}\0{json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)}"


def _client_tool_calls(client) -> list[Any]:
    calls = getattr(client, "_last_tool_calls", None)
    if not calls:
        return []
    return list(calls)


def _agent_run_tool_call_payload(call: Any) -> dict[str, Any]:
    raw_arguments = getattr(call, "arguments", {}) or {}
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
    return {
        "tool": str(getattr(call, "tool", "") or getattr(call, "name", "")),
        "arguments": dict(arguments),
        "result": str(getattr(call, "result", "") or getattr(call, "output", "")),
        "successful": bool(getattr(call, "successful", True)),
    }


def _agent_run_completion_result(
    *,
    reply: Optional[str],
    search_tool_results: list[dict[str, Any]],
    tool_calls: list[Any],
) -> dict[str, Any]:
    result_payload: dict[str, Any] = {
        "assistant_response": reply or "",
        "tool_result_count": len(search_tool_results) + len(tool_calls),
    }
    if tool_calls:
        result_payload["tool_calls"] = [
            _agent_run_tool_call_payload(call) for call in tool_calls
        ]
    return result_payload


def _should_fail_agent_run_completion(
    *,
    user_input: str,
    reply: Optional[str],
    search_tool_result_count: int = 0,
) -> bool:
    if _looks_like_cli_execution_error(reply):
        return True
    if search_tool_result_count > 0 and str(reply or "").strip():
        return False
    return response_looks_like_unfinished_work(user_input, reply)


def _looks_like_cli_execution_error(reply: Optional[str]) -> bool:
    text = str(reply or "").strip()
    if not text:
        return False
    lowered = text.lower()
    cli_markers = (
        "cli error:",
        "cli execution failed",
        "cli returned no output",
        "returned no output from print mode",
        "codex cli error",
        "codex cli failed",
        "antigravity cli returned no output",
        "antigravity cli error",
        "antigravity cli failed",
        "gemini cli returned no output",
        "gemini cli error",
        "gemini cli failed",
    )
    if any(marker in lowered for marker in cli_markers):
        return True
    return lowered.startswith("エラーが発生しました:") and "cli" in lowered


def _agent_run_completion_failure_message(
    *,
    user_input: str,
    reply: Optional[str],
    search_tool_result_count: int = 0,
) -> Optional[str]:
    if _looks_like_cli_execution_error(reply):
        return "CLI execution failed"
    if search_tool_result_count > 0 and str(reply or "").strip():
        return None
    if response_looks_like_unfinished_work(user_input, reply):
        return "Assistant response did not complete the requested work"
    return None


class AgentRunEventEmitter:
    """`_process_user_message_web` の AgentRun イベント送出ネスト関数群を集約したクラス。

    移設前はメソッド内クロージャで参照していた変数（agent_run_service /
    agent_run_id / session 情報 / 共有 search_tool_results / user_input など）を
    コンストラクタで明示的に受け取る。`search_tool_results` は呼び出し側と同一の
    リスト参照を保持し、ストリーム中の append が complete() 時に反映される点も
    移設前と同一。`finished` フラグは旧 nonlocal `agent_run_finished` に相当する。
    """

    def __init__(
        self,
        *,
        agent_run_service: Optional[AgentRunService],
        agent_run_id: Optional[str],
        session_id: Optional[str],
        project_id: Optional[str],
        generation_profile: Any,
        include_project_context: Any,
        command_capabilities: Any,
        search_tool_results: list[dict[str, Any]],
        user_input: str,
        log_prefix: str = "TerminalMode",
    ):
        self._agent_run_service = agent_run_service
        self._agent_run_id = agent_run_id
        self._session_id = session_id
        self._project_id = project_id
        self._generation_profile = generation_profile
        self._include_project_context = include_project_context
        self._command_capabilities = command_capabilities
        self._search_tool_results = search_tool_results
        self._user_input = user_input
        self._log_prefix = log_prefix
        self.finished = False

    @staticmethod
    def _model_context(client) -> dict[str, Any]:
        provider = None
        backend = getattr(client, "cli_backend", None)
        if backend and hasattr(backend, "get_provider_name"):
            try:
                provider = backend.get_provider_name()
            except Exception:
                provider = None
        provider = provider or getattr(client, "provider", None)
        provider = provider or getattr(client, "provider_label", None)
        model = getattr(backend, "_model", None) if backend else None
        model = (
            model
            or getattr(client, "model_name", None)
            or getattr(client, "model", None)
        )
        if str(model or "").strip().lower() == "default":
            model = None
        return {
            "provider": str(provider) if provider else None,
            "model": str(model) if model else None,
        }

    @staticmethod
    def _event_payload(data: dict[str, Any]) -> dict[str, Any]:
        payload = dict(data or {})
        for key in ("content", "delta", "text", "output"):
            value = payload.get(key)
            if isinstance(value, str) and len(value) > 4000:
                payload[key] = value[:4000].rstrip() + "\n... (truncated)"
        tool_result = payload.get("tool_result")
        if isinstance(tool_result, dict):
            tool_result = dict(tool_result)
            for key in ("output", "result", "error", "stderr"):
                value = tool_result.get(key)
                if isinstance(value, str) and len(value) > 20000:
                    tool_result[key] = (
                        value[:20000].rstrip() + "\n... (truncated)"
                    )
            payload["tool_result"] = tool_result
        return payload

    async def record_event(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        status: str | None = None,
        message_text: str | None = None,
    ) -> None:
        if not self._agent_run_service:
            return
        try:
            await self._agent_run_service.record_event(
                self._agent_run_id,
                event_type,
                status=status,
                message=message_text,
                payload=self._event_payload(data or {}),
            )
        except Exception as exc:
            print(f"[{self._log_prefix}] AgentRun event record failed: {exc}")

    async def mark_running(self, client) -> None:
        if not self._agent_run_service:
            return
        model_context = self._model_context(client)
        try:
            await self._agent_run_service.mark_running(
                self._agent_run_id,
                message="Assistant generation started",
                metadata={
                    "session_id": self._session_id,
                    "project_id": self._project_id,
                    "generation_profile": self._generation_profile,
                    "include_project_context": self._include_project_context,
                    "command_capabilities": list(self._command_capabilities),
                },
                provider=model_context["provider"],
                model=model_context["model"],
            )
        except Exception as exc:
            print(f"[{self._log_prefix}] AgentRun start update failed: {exc}")

    async def complete(
        self,
        reply: Optional[str],
        client=None,
    ) -> None:
        if not self._agent_run_service or self.finished:
            return
        self.finished = True
        try:
            tool_calls = _client_tool_calls(client)
            for call in tool_calls:
                payload = _agent_run_tool_call_payload(call)
                if not payload["tool"]:
                    continue
                await self._agent_run_service.record_tool_call(
                    self._agent_run_id,
                    tool_name=payload["tool"],
                    arguments=payload["arguments"],
                    result=payload["result"],
                    success=payload["successful"],
                    mutation_confirmed=payload["tool"]
                    in PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES,
                )

            result_payload = _agent_run_completion_result(
                reply=reply,
                search_tool_results=self._search_tool_results,
                tool_calls=tool_calls,
            )
            failure_message = _agent_run_completion_failure_message(
                user_input=self._user_input,
                reply=reply,
                search_tool_result_count=len(self._search_tool_results),
            )
            if failure_message:
                await self._agent_run_service.fail_run(
                    self._agent_run_id,
                    failure_message,
                    result=result_payload,
                )
                return
            await self._agent_run_service.complete_run(
                self._agent_run_id,
                result=result_payload,
                message="Assistant generation completed",
            )
        except Exception as exc:
            print(f"[{self._log_prefix}] AgentRun completion update failed: {exc}")

    async def fail(
        self,
        error_text: str,
        reply: Optional[str] = None,
    ) -> None:
        if not self._agent_run_service or self.finished:
            return
        self.finished = True
        try:
            await self._agent_run_service.fail_run(
                self._agent_run_id,
                error_text,
                result={"assistant_response": reply or ""},
            )
        except Exception as exc:
            print(f"[{self._log_prefix}] AgentRun failure update failed: {exc}")
