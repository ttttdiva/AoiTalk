"""Shared delegation runners for specialist agents."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import logging
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional, Sequence, Type

from openai import OpenAI

from ..config import Config
from ..services.project_context import (
    format_project_context_for_prompt,
    get_runtime_project_context,
    reset_runtime_project_context,
    set_runtime_project_context,
)
from ..services.advanced_reasoning_service import (
    build_redacted_prompt,
)
from ..services.agent_team_service import (
    agent_team_confirm_prompt,
    agent_team_member_for,
    agent_team_member_mode,
    agent_team_member_requires_external_approval,
    agent_team_notify,
    apply_agent_team_member_mode,
    config_get,
)
from ..tools.adapters import CLIAdapter, OpenAIAPIAdapter
from ..tools.core import ToolDefinition, ToolParam, ensure_tool_definitions
from ..tools.external import MCPPlugin, set_mcp_plugin
from ..tools.external_llm_permission import request_external_model_prompt
from ..tools.registry import ToolRegistry
from .agent_runtime import (
    OpenAIToolCallLoopResult,
    OpenAIToolCallRecord,
    run_openai_tool_call_loop,
)
from .context_budget import (
    clip_text_preserve_tail,
    resolve_context_budget,
)
from .json_tool_loop import (
    JsonToolCallRecord,
    JsonToolLoopResult,
    build_json_tool_loop_system_prompt,
    run_json_tool_loop,
)
from .generation_policy import PermissionPolicy, get_current_generation_policy
from .native_runtime import (
    AgentDefinition as NativeAgentDefinition,
    NativeModelSettings,
    Reasoning,
    run_native_agent_once,
)
from .provider_mode_adapters import ollama_reasoning_effort_for_mode
from .unified_turn_runtime import run_cli_tool_call_loop

logger = logging.getLogger(__name__)

_runtime_specialist_provider: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("runtime_specialist_provider", default=None)
)


def set_runtime_specialist_provider(provider: Optional[str]) -> contextvars.Token:
    value = str(provider or "").strip().lower() or None
    return _runtime_specialist_provider.set(value)


def reset_runtime_specialist_provider(token: contextvars.Token) -> None:
    _runtime_specialist_provider.reset(token)

CLI_PROVIDER_NAMES = {"antigravity-cli", "claude-cli", "codex-cli"}
NATIVE_OPENAI_MODEL_PREFIXES = ("openai/", "litellm/", "gpt-", "o1", "o3", "o4")
OLLAMA_PROVIDER_NAME = "ollama"
OPENAI_COMPATIBLE_PROVIDER_NAMES = {
    "openai_compatible_local",
    "sglang",
    "openrouter",
}
CONSTRAINED_OPENAI_COMPATIBLE_CONTEXT_TOKENS = 16384

TEAM_ROLE_INSTRUCTIONS = {
    "architect": """
You are the Agent Team architect.

Analyze the existing context and produce a concrete implementation blueprint.
Make decisions, assign ownership boundaries, identify affected files/modules,
and call out risks that implementers and reviewers must handle.
Do not write marketing copy. Be specific and operational.
""".strip(),
    "explorer": """
You are an Agent Team explorer.

Investigate the requested topic with the available read-only tools. Trace entry
points, data flow, configuration, tests, and nearby conventions. Return concise
findings with file references and the minimum context needed for implementation.
Avoid duplicating other explorers' work when the request gives you a narrower
question.
""".strip(),
    "implementer": """
You are an Agent Team implementer.

Turn the assigned implementation scope into concrete steps and code-level
guidance. Respect file ownership from the coordinator, avoid unrelated
refactors, and explicitly note conflicts or missing prerequisites. When no
write-capable runner is attached, return an implementation patch plan with exact
files and functions rather than pretending to edit files.
""".strip(),
    "reviewer": """
You are an Agent Team reviewer.

Review the assigned scope for correctness, regressions, missing tests, security
or privacy risks, and maintainability. Report only issues that are actionable and
grounded in evidence. Start with findings, ordered by severity, with file
references whenever available.
""".strip(),
}

_COMPACT_TOOL_DESCRIPTIONS = {
    "find_workspace_items": "Find files or folders by name in the workspace.",
    "inspect_workspace_tree": "List a bounded workspace folder tree.",
    "read_workspace_file": "Read a workspace file preview.",
    "get_workspace_file_info": "Get workspace file metadata.",
    "list_workspace_files": "List direct workspace folder contents.",
    "get_project_context": "Get the active project context.",
    "list_project_information": "List saved project facts, documents, and tables.",
    "list_record_tables": "List project record tables.",
    "list_tasks": "List project tasks.",
    "create_task": "Create a project task.",
    "update_task": "Update a project task.",
    "delete_task": "Delete a project task.",
    "schedule_task": "Schedule a project task.",
    "get_project_progress": "Summarize project progress from goals, internal WBS, and tasks.",
    "get_upcoming_wbs_tasks": "List upcoming internal WBS.dbtable rows.",
    "get_project_issues": "List project issues.",
}


def _is_context_overflow_error(exc: Exception) -> bool:
    body = getattr(exc, "body", None)
    text = f"{exc} {body}".casefold()
    return any(
        term in text
        for term in (
            "context size",
            "context length",
            "context window",
            "maximum context",
            "max context",
            "n_ctx",
            "too many tokens",
            "prompt is too long",
            "requested tokens",
            "exceeds context",
            "exceeded context",
            "exceeds the context",
            "exceeded the context",
            "exceeds maximum context",
            "コンテキスト",
        )
    )


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        return config_get(config, key, default)
    if hasattr(config, "get"):
        return config.get(key, default)
    return default


def _compact_tool_definition(tool_def: ToolDefinition) -> ToolDefinition:
    return ToolDefinition(
        name=tool_def.name,
        description=_COMPACT_TOOL_DESCRIPTIONS.get(tool_def.name, tool_def.name),
        function=tool_def.function,
        parameters=[
            ToolParam(
                name=param.name,
                type=param.type,
                description="",
                required=param.required,
                default=param.default,
                enum=param.enum,
            )
            for param in tool_def.parameters
        ],
        is_async=tool_def.is_async,
    )


def _clone_registry_with_tools(
    registry: ToolRegistry,
    tool_names: Sequence[str],
    *,
    compact: bool = False,
) -> ToolRegistry:
    selected = ToolRegistry()
    seen: set[str] = set()
    for name in tool_names:
        if name in seen:
            continue
        seen.add(name)
        tool_def = registry.get(name)
        if tool_def is None:
            continue
        selected.register(_compact_tool_definition(tool_def) if compact else tool_def)
    return selected


def _is_constrained_openai_compatible_context(
    provider: str,
    context_window_tokens: int,
) -> bool:
    return (
        provider in {"openai_compatible_local", "sglang"}
        and context_window_tokens <= CONSTRAINED_OPENAI_COMPATIBLE_CONTEXT_TOKENS
    )


class SpecialistDelegationRunner:
    """Run a specialist agent from synchronous tool execution."""

    def __init__(
        self,
        config: Config,
        *,
        domain_key: str,
        display_name: str,
        agent_class: Type,
        mcp_server_names: Sequence[str] | None = None,
        model: Optional[str] = None,
    ):
        self.config = config
        self.domain_key = domain_key
        self.display_name = display_name
        self.agent_class = agent_class
        self.mcp_server_names = tuple(mcp_server_names or ())
        self._agent_team_member_config = agent_team_member_for(config, domain_key)

        self.provider = self._select_provider()
        self.model = model or self._select_model()
        self._mode_preset = ""
        if self._uses_agent_team_member_target():
            self._mode_preset = apply_agent_team_member_mode(
                self.config,
                member_key=self.domain_key,
                provider=self.provider,
                model=self.model or "",
            )
        self.cli_backend = (
            self._create_cli_backend() if self.provider in CLI_PROVIDER_NAMES else None
        )

        self._agent_definition = None
        self._tool_registry: ToolRegistry | None = None

    def _get_agent_configs(self) -> tuple[dict[str, Any], dict[str, Any]]:
        agents_config = _config_get(self.config, "agents", {}) or {}
        if not isinstance(agents_config, dict):
            return {}, {}

        domain_config = agents_config.get(self.domain_key, {}) or {}
        if not isinstance(domain_config, dict):
            domain_config = {}

        return agents_config, domain_config

    def _uses_agent_team_member_target(self) -> bool:
        return self._agent_team_member_config is not None

    def _main_provider(self) -> str:
        provider = str(_config_get(self.config, "llm_provider", "openai")).strip().lower()
        return provider or "openai"

    def _active_provider(self) -> str:
        return _runtime_specialist_provider.get() or self.provider

    def _main_model_for_provider(self, provider: str) -> Optional[str]:
        selected = str(_config_get(self.config, "llm_model", "") or "").strip()
        if selected:
            return selected

        provider_model_keys = {
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
        }
        for key in provider_model_keys.get(provider, ()):
            value = str(_config_get(self.config, key, "") or "").strip()
            if value:
                return value
        return None

    def _select_provider(self) -> str:
        if self._agent_team_member_config:
            return self._agent_team_member_config["provider"]
        return self._main_provider()

    def _select_native_openai_model(self) -> str:
        configured_model = self.model or self._main_model_for_provider(self.provider)
        if configured_model and str(configured_model).strip().startswith(
            NATIVE_OPENAI_MODEL_PREFIXES
        ):
            return str(configured_model).strip()

        if _config_get(self.config, "openai_api_key"):
            return "gpt-4o-mini"

        llm_model = str(_config_get(self.config, "llm_model", "")).strip()
        if llm_model.startswith(NATIVE_OPENAI_MODEL_PREFIXES):
            return llm_model

        logger.warning(
            "[%sDelegationRunner] No native OpenAI-compatible model configured; "
            "falling back to gpt-4o-mini",
            self.display_name,
        )
        return "gpt-4o-mini"

    def _select_model(self) -> Optional[str]:
        if self._agent_team_member_config:
            return self._agent_team_member_config["model"]

        configured_model = self._main_model_for_provider(self.provider)

        if self.provider in CLI_PROVIDER_NAMES:
            return (
                str(configured_model).strip()
                if configured_model
                else _config_get(
                    self.config,
                    "llm_model",
                )
            )

        if self.provider == OLLAMA_PROVIDER_NAME:
            ollama_config = _config_get(self.config, "ollama", {}) or {}
            return (
                str(configured_model).strip()
                if configured_model
                else (
                    _config_get(self.config, "ollama_model")
                    or _config_get(self.config, "llm_model")
                    or ollama_config.get("model")
                    or "gemma4:e4b"
                )
            )

        if self.provider in OPENAI_COMPATIBLE_PROVIDER_NAMES:
            return configured_model

        if self.provider == "openai":
            return configured_model or "gpt-4o-mini"

        if self.provider == "gemini":
            return configured_model or "gemini-3-flash-preview"

        return self._select_native_openai_model()

    def _create_cli_backend(self):
        if self.provider == "antigravity-cli":
            from .cli_backends.antigravity import AntigravityCLIBackend

            return AntigravityCLIBackend(model=self.model)

        if self.provider == "claude-cli":
            from .cli_backends.claude import ClaudeCLIBackend

            return ClaudeCLIBackend(
                model=self.model,
                reasoning_effort=(
                    agent_team_member_mode(self.config, self.domain_key)
                    if self._uses_agent_team_member_target()
                    else _config_get(self.config, "claude_cli.reasoning_effort")
                ),
            )

        if self.provider == "codex-cli":
            from .cli_backends.codex import CodexCLIBackend

            return CodexCLIBackend(
                model=self.model,
                reasoning_effort=(
                    agent_team_member_mode(self.config, self.domain_key)
                    if self._uses_agent_team_member_target()
                    else _config_get(self.config, "codex_cli.reasoning_effort")
                ),
            )

        raise ValueError(f"Unsupported specialist CLI provider: {self.provider}")

    def _mode_extra_body(self) -> dict[str, Any]:
        if self.provider not in {"openai_compatible_local", "sglang"}:
            return {}
        mode = str(self._mode_preset or "").strip().lower()
        if mode not in {"fast", "thinking"}:
            return {}
        return {
            "chat_template_kwargs": {
                "enable_thinking": mode == "thinking",
            }
        }

    def _with_native_mode_preset(self, agent: Any) -> Any:
        mode = str(self._mode_preset or "").strip()
        if self.provider != "openai" or not mode:
            return agent

        from ..services.llm_model_catalog import reasoning_effort_options_for_model

        if mode not in reasoning_effort_options_for_model("openai", getattr(agent, "model", "")):
            return agent

        current_settings = getattr(agent, "model_settings", None) or NativeModelSettings()
        return replace(
            agent,
            model_settings=replace(
                current_settings,
                reasoning=Reasoning(effort=mode),
            ),
        )

    def _configure_model_environment(self) -> None:
        openai_api_key = _config_get(self.config, "openai_api_key")
        gemini_api_key = _config_get(self.config, "gemini_api_key")

        if openai_api_key:
            os.environ["OPENAI_API_KEY"] = openai_api_key
        if gemini_api_key:
            os.environ["GOOGLE_API_KEY"] = gemini_api_key

    def _get_mcp_config(
        self,
        project_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.mcp_server_names or not _config_get(
            self.config, "mcp_enabled", False
        ):
            return {}

        configured_servers = _config_get(self.config, "mcp", {}).get("servers", {})
        selected = {
            name: configured_servers[name]
            for name in self.mcp_server_names
            if name in configured_servers
        }
        return {"servers": selected} if selected else {}

    def _augment_request_with_project_context(
        self,
        request: str,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        if not project_context:
            return request

        project_block = format_project_context_for_prompt(project_context)
        if not project_block:
            return request

        guidance = (
            "Use this project context as the default target when the user does not specify "
            "another project."
        )
        return f"{project_block}\n{guidance}\n\nUser request:\n{request}"

    def _get_agent_definition(self):
        if self._agent_definition is None:
            self._agent_definition = self._create_agent_instance(
                model=self.model or self._select_native_openai_model()
            ).agent
        return self._agent_definition

    def _create_agent_instance(self, model: Optional[str] = None):
        try:
            return self.agent_class(model=model, config=self.config)
        except TypeError:
            return self.agent_class(model=model)

    def _build_tool_registry(self) -> ToolRegistry:
        if self._tool_registry is not None:
            return self._tool_registry

        registry = ToolRegistry()
        for agent_tool in self._get_agent_definition().tools:
            registry.register(self._convert_agent_tool(agent_tool))

        self._tool_registry = registry
        return registry

    def _convert_agent_tool(self, agent_tool: Any) -> ToolDefinition:
        if isinstance(agent_tool, ToolDefinition):
            return agent_tool

        raise TypeError(
            "Specialist agent tools must be native ToolDefinition instances; "
            f"got {type(agent_tool)!r}"
        )

    def _build_cli_system_context(self) -> str:
        agent_definition = self._get_agent_definition()
        registry = self._build_tool_registry()

        parts = [str(getattr(agent_definition, "instructions", "")).strip()]

        if len(registry):
            parts.extend(
                [
                    "",
                    CLIAdapter.to_prompt_text(registry.get_all()),
                ]
            )

        parts.extend(
            [
                "",
                "あなたは専門アシスタントツールとして実行されています。",
                "依頼を完了するために必要な場合は、利用可能なツールを使ってください。",
                "ツールが必要な場合は [TOOL_CALL: tool_name(key=value)] 形式で出力してください。",
                "ツール結果が返された後は、その結果を根拠に自然に回答してください。",
            ]
        )

        return "\n".join(part for part in parts if part is not None)

    def _build_cli_follow_up_prompt(
        self,
        original_input: str,
        initial_response: str,
        tool_results_text: str,
    ) -> str:
        return "\n".join(
            [
                "# ツール実行結果",
                f"元の専門依頼: {original_input}",
                "",
                "直前の出力:",
                initial_response,
                "",
                tool_results_text,
                "",
                "# 最終回答",
                "上記のツール結果に基づいて、ユーザーの依頼を完了してください。",
                "追加ツールがまだ必要な場合だけ、次のツール呼び出しを出力してください。",
                "最終的にユーザーへ返す回答だけを出力してください。",
            ]
        )

    def _run_via_cli(self, request: str) -> str:
        if self.cli_backend is None:
            return f"{self.display_name} CLI backend is not configured"

        registry = self._build_tool_registry()
        system_context = self._build_cli_system_context()
        required_tools = self._required_native_openai_tool_names(request)
        if not required_tools:
            required_tools = self._required_ollama_tool_names(request)

        success, cli_output = self.cli_backend.execute_prompt(
            prompt=request,
            cwd=Path.cwd(),
            system_context=system_context,
        )
        if not success:
            logger.error(
                "[%sDelegationRunner] CLI execution failed: %s",
                self.display_name,
                cli_output,
            )
            return f"{self.display_name} delegation error: {cli_output}"

        turn_result = run_cli_tool_call_loop(
            original_input=request,
            initial_output=cli_output,
            registry=registry,
            parse_tool_calls=self.cli_backend.parse_tool_calls,
            execute_follow_up=lambda follow_up: self.cli_backend.execute_prompt(
                follow_up,
                cwd=Path.cwd(),
            ),
            build_follow_up_prompt=self._build_cli_follow_up_prompt,
            log_prefix=f"{self.display_name}DelegationRunner",
            config=self.config,
            user_input=request,
        )
        tool_call_records = [
            OpenAIToolCallRecord(
                tool=tool_result.call.tool,
                arguments=dict(tool_result.call.arguments),
                result=tool_result.model_output,
            )
            for tool_result in turn_result.tool_results
        ]

        if required_tools:
            return self._validate_openai_tool_loop_result(
                request,
                OpenAIToolCallLoopResult(
                    final_output=turn_result.final_output,
                    tool_calls=tool_call_records,
                ),
                required_tools,
            )
        return turn_result.final_output

    def _run_via_ollama_json_tool_loop(self, request: str) -> str:
        registry = self._build_tool_registry()
        agent_definition = self._get_agent_definition()
        instructions = str(getattr(agent_definition, "instructions", "")).strip()
        system_prompt = build_json_tool_loop_system_prompt(instructions, registry)

        ollama_config = _config_get(self.config, "ollama", {}) or {}
        base_url = (
            os.getenv("OLLAMA_BASE_URL")
            or _config_get(self.config, "ollama_base_url")
            or ollama_config.get("base_url")
            or "http://127.0.0.1:11434/v1"
        )
        base_url = str(base_url).rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"

        api_key = (
            os.getenv("OLLAMA_API_KEY")
            or _config_get(self.config, "ollama_api_key")
            or ollama_config.get("api_key")
            or "ollama"
        )
        client = OpenAI(base_url=base_url, api_key=api_key)
        model = self.model or "gemma4:e4b"

        initial_messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "\n".join(
                    [
                        "User request:",
                        request,
                        "",
                        "Decide whether a tool is needed.",
                        (
                            "If the request explicitly asks for web search or uses Japanese terms such "
                            "as 調べて or 調査して, use a relevant search tool first."
                        ),
                        (
                            "When calling a tool with a `request` parameter, copy the user's request exactly "
                            "unless a shorter accurate query is obvious."
                        ),
                    ]
                ),
            },
        ]

        def _create(messages_payload: list[dict[str, Any]]) -> str:
            api_kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages_payload,
                "temperature": 0,
                "max_tokens": 1024,
                "response_format": {"type": "json_object"},
            }
            reasoning_effort = ollama_reasoning_effort_for_mode(
                model,
                self._mode_preset,
            )
            if reasoning_effort:
                api_kwargs["reasoning_effort"] = reasoning_effort
            response = client.chat.completions.create(**api_kwargs)
            return response.choices[0].message.content or ""

        required_tools = self._required_ollama_tool_names(request)
        require_all_tools = self._require_all_required_tools(request, required_tools)
        result = run_json_tool_loop(
            create_completion=_create,
            initial_messages=initial_messages,
            registry=registry,
            original_request=request,
            required_tool_names=required_tools,
            required_tool_reason=self._required_ollama_tool_reason(request, required_tools),
            require_all_required_tools=require_all_tools,
            return_result=bool(required_tools),
        )
        if isinstance(result, JsonToolLoopResult):
            return self._validate_ollama_tool_loop_result(request, result, required_tools)
        return result

    def _openai_compatible_connection(self) -> tuple[str, str]:
        provider = self.provider
        if provider == "openai_compatible_local":
            base_url = (
                os.getenv("OPENAI_COMPATIBLE_LOCAL_BASE_URL")
                or _config_get(self.config, "openai_compatible_local.base_url")
                or "http://127.0.0.1:8080/v1"
            )
            api_key = (
                os.getenv("OPENAI_COMPATIBLE_LOCAL_API_KEY")
                or _config_get(self.config, "openai_compatible_local.api_key")
                or "dummy"
            )
        elif provider == "sglang":
            sglang_config = _config_get(self.config, "sglang", {}) or {}
            port = sglang_config.get("port", 30000) if isinstance(sglang_config, dict) else 30000
            base_url = (
                os.getenv("SGLANG_BASE_URL")
                or _config_get(self.config, "sglang_base_url")
                or _config_get(self.config, "sglang.base_url")
                or f"http://localhost:{port}/v1"
            )
            api_key = (
                os.getenv("SGLANG_API_KEY")
                or _config_get(self.config, "sglang_api_key")
                or "dummy"
            )
        elif provider == "openrouter":
            base_url = (
                os.getenv("OPENROUTER_BASE_URL")
                or _config_get(self.config, "openrouter.base_url")
                or "https://openrouter.ai/api/v1"
            )
            api_key = _config_get(self.config, "openrouter_api_key") or os.getenv("OPENROUTER_API_KEY")
        else:
            raise ValueError(f"Unsupported OpenAI-compatible specialist provider: {provider}")

        clean_base_url = str(base_url).rstrip("/")
        if provider in {"openai_compatible_local", "sglang"} and not clean_base_url.endswith("/v1"):
            clean_base_url = f"{clean_base_url}/v1"
        return clean_base_url, str(api_key or "dummy")

    def _create_openai_compatible_completion(self, client: OpenAI, api_kwargs: dict[str, Any]) -> Any:
        try:
            return client.chat.completions.create(**api_kwargs)
        except Exception as exc:
            if _is_context_overflow_error(exc):
                raise
            retry_kwargs = dict(api_kwargs)
            removed = []
            for key in ("tools", "tool_choice", "response_format", "extra_body"):
                if key in retry_kwargs:
                    retry_kwargs.pop(key, None)
                    removed.append(key)
            if not removed:
                raise
            logger.warning(
                "[%sDelegationRunner] Retrying without %s: %s",
                self.display_name,
                ", ".join(removed),
                exc,
            )
            return client.chat.completions.create(**retry_kwargs)

    def _run_via_openai_compatible_tool_loop(self, request: str) -> str:
        registry = self._build_tool_registry()
        agent_definition = self._get_agent_definition()
        instructions = str(getattr(agent_definition, "instructions", "")).strip()
        base_url, api_key = self._openai_compatible_connection()
        client = OpenAI(base_url=base_url, api_key=api_key)
        context_budget = resolve_context_budget(
            config=self.config,
            provider_key=self.provider,
            base_url=base_url,
            model_name=self.model,
            api_key=api_key,
        )
        required_tools = self._required_openai_compatible_tool_names(request)
        registry = self._openai_compatible_registry_for_budget(
            registry,
            request=request,
            context_window_tokens=context_budget.context_window_tokens,
            required_tools=required_tools,
        )

        messages = [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": clip_text_preserve_tail(
                    request,
                    context_budget.message_budget_chars,
                ),
            },
        ]
        api_kwargs: dict[str, Any] = {
            "model": self.model or "local-model",
            "messages": messages,
            "temperature": 0,
            "max_tokens": context_budget.response_tokens,
        }
        extra_body = self._mode_extra_body()
        if extra_body:
            api_kwargs["extra_body"] = extra_body
        if len(registry) > 0:
            api_kwargs["tools"] = OpenAIAPIAdapter.convert_all(registry.get_all())
            api_kwargs["tool_choice"] = "required" if required_tools else "auto"
        else:
            required_tools = set()

        response = self._create_openai_compatible_completion(client, api_kwargs)
        message = response.choices[0].message
        if getattr(message, "tool_calls", None):
            result = run_openai_tool_call_loop(
                initial_messages=messages,
                assistant_message=message,
                api_kwargs=api_kwargs,
                registry=registry,
                create_completion=lambda kwargs: self._create_openai_compatible_completion(client, kwargs),
                log_prefix=f"{self.display_name}DelegationRunner",
                max_rounds=5,
                return_result=bool(required_tools),
                max_tool_result_chars=context_budget.tool_result_chars,
                config=self.config,
                user_input=request,
            )
            if isinstance(result, OpenAIToolCallLoopResult):
                return self._validate_openai_tool_loop_result(
                    request,
                    result,
                    required_tools,
                )
            return result
        if required_tools:
            return self._validate_openai_tool_loop_result(
                request,
                OpenAIToolCallLoopResult(
                    final_output=getattr(message, "content", None) or "",
                    tool_calls=[],
                ),
                required_tools,
            )
        return getattr(message, "content", None) or ""

    def _openai_compatible_registry_for_budget(
        self,
        registry: ToolRegistry,
        *,
        request: str,
        context_window_tokens: int,
        required_tools: set[str],
    ) -> ToolRegistry:
        if not _is_constrained_openai_compatible_context(
            self.provider,
            context_window_tokens,
        ):
            return registry

        compact_names = self._compact_openai_compatible_tool_names(
            request,
            required_tools=required_tools,
        )
        if not compact_names:
            return registry

        compact_registry = _clone_registry_with_tools(
            registry,
            compact_names,
            compact=True,
        )
        return compact_registry if len(compact_registry) > 0 else registry

    def _compact_openai_compatible_tool_names(
        self,
        request: str,
        *,
        required_tools: set[str],
    ) -> list[str]:
        return sorted(required_tools)

    async def _approve_external_model_request(self, delegated_request: str) -> Optional[str]:
        if not self._uses_agent_team_member_target():
            return delegated_request
        if not agent_team_member_requires_external_approval(self._agent_team_member_config):
            return delegated_request

        redacted_prompt, redaction_findings = build_redacted_prompt(
            delegated_request,
            config=self.config,
        )
        return await request_external_model_prompt(
            delegated_request,
            redacted_prompt=redacted_prompt,
            redaction_findings=redaction_findings,
            provider=self.provider,
            model=self.model or "",
            description=(
                f"Review the {self.display_name} assistant prompt before "
                f"sending it to {self.provider}/{self.model}."
            ),
            confirm=self._external_route_prompt_confirmation_enabled(
                agent_team_confirm_prompt(self.config)
            ),
            notify=agent_team_notify(self.config),
            request_kind=f"{self.domain_key}_assistant",
        )

    def _external_route_prompt_confirmation_enabled(self, configured: bool) -> bool:
        if (
            get_current_generation_policy().permission_policy
            == PermissionPolicy.AUTO_APPROVE
        ):
            return False
        return bool(configured)

    def _required_ollama_tool_names(self, request: str) -> set[str]:
        return set()

    def _required_openai_compatible_tool_names(self, request: str) -> set[str]:
        return self._required_ollama_tool_names(request)

    def _require_all_required_tools(
        self,
        request: str,
        required_tools: set[str],
    ) -> bool:
        return False

    def _required_ollama_tool_reason(
        self,
        request: str,
        required_tools: set[str],
    ) -> str | None:
        return None

    def _validate_ollama_tool_loop_result(
        self,
        request: str,
        result: JsonToolLoopResult,
        required_tools: set[str],
    ) -> str:
        return result.final_output

    def _validate_openai_tool_loop_result(
        self,
        request: str,
        result: OpenAIToolCallLoopResult,
        required_tools: set[str],
    ) -> str:
        return result.final_output

    def _required_native_openai_tool_names(self, request: str) -> set[str]:
        return set()

    def _tool_loop_result_from_native_run_result(
        self,
        result: Any,
    ) -> OpenAIToolCallLoopResult:
        records: list[OpenAIToolCallRecord] = []
        pending_calls: list[tuple[str, dict[str, Any]]] = []

        for item in getattr(result, "new_items", []) or []:
            item_type = getattr(item, "type", "")
            raw_item = getattr(item, "raw_item", None)
            if item_type == "tool_call_item":
                name = str(getattr(raw_item, "name", "") or "")
                args_text = getattr(raw_item, "arguments", "{}") or "{}"
                try:
                    arguments = json.loads(args_text)
                except Exception:
                    arguments = {}
                if name:
                    pending_calls.append(
                        (name, arguments if isinstance(arguments, dict) else {})
                    )
                continue

            if item_type != "tool_call_output_item":
                continue

            output = getattr(item, "output", "")
            if pending_calls:
                name, arguments = pending_calls.pop(0)
            else:
                name = str(
                    getattr(raw_item, "name", "")
                    or getattr(raw_item, "tool_name", "")
                    or ""
                )
                arguments = {}
            if name:
                records.append(
                    OpenAIToolCallRecord(
                        tool=name,
                        arguments=arguments,
                        result=str(output or ""),
                    )
                )

        return OpenAIToolCallLoopResult(
            final_output=str(getattr(result, "final_output", "") or ""),
            tool_calls=records,
        )

    async def _run_async(
        self,
        request: str,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        if not request or not request.strip():
            return f"{self.display_name} request is empty"

        self._configure_model_environment()
        delegated_request = self._augment_request_with_project_context(
            request,
            project_context=project_context,
        )
        project_context_token = (
            set_runtime_project_context(project_context) if project_context else None
        )

        plugin: MCPPlugin | None = None
        mcp_config = self._get_mcp_config(project_context=project_context)

        try:
            if self.mcp_server_names:
                if not mcp_config:
                    return f"{self.display_name} MCP is not configured"

                plugin = MCPPlugin()
                success = await plugin.initialize(mcp_config)
                if not success:
                    return f"Failed to initialize {self.display_name} MCP"
                set_mcp_plugin(plugin)

            approved_request = await self._approve_external_model_request(delegated_request)
            if approved_request is None:
                return f"{self.display_name} delegation cancelled"
            delegated_request = approved_request

            if self.provider in CLI_PROVIDER_NAMES:
                return await asyncio.to_thread(self._run_via_cli, delegated_request)

            if self.provider == OLLAMA_PROVIDER_NAME:
                return await asyncio.to_thread(
                    self._run_via_ollama_json_tool_loop,
                    delegated_request,
                )

            if self.provider in OPENAI_COMPATIBLE_PROVIDER_NAMES:
                return await asyncio.to_thread(
                    self._run_via_openai_compatible_tool_loop,
                    delegated_request,
                )

            agent = self._create_agent_instance(
                model=self.model or self._select_native_openai_model()
            ).agent
            agent = self._with_native_mode_preset(agent)
            result = await run_native_agent_once(
                agent,
                delegated_request,
                config=self.config,
            )
            required_tools = self._required_native_openai_tool_names(delegated_request)
            if required_tools:
                return self._validate_openai_tool_loop_result(
                    delegated_request,
                    OpenAIToolCallLoopResult(
                        final_output=result.final_output,
                        tool_calls=[
                            OpenAIToolCallRecord(
                                tool=record.tool,
                                arguments=record.arguments,
                                result=record.result,
                            )
                            for record in result.tool_calls
                        ],
                    ),
                    required_tools,
                )
            return result.final_output
        except Exception as exc:
            logger.exception(
                "[%sDelegationRunner] Delegation failed", self.display_name
            )
            return f"{self.display_name} delegation error: {exc}"
        finally:
            if project_context_token is not None:
                reset_runtime_project_context(project_context_token)
            if plugin is not None:
                try:
                    await plugin.cleanup()
                except Exception:
                    logger.debug(
                        "[%sDelegationRunner] MCP cleanup failed",
                        self.display_name,
                        exc_info=True,
                    )
                finally:
                    set_mcp_plugin(None)

    def run(
        self,
        request: str,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._run_async(request, project_context=project_context)
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            context = contextvars.copy_context()
            future = pool.submit(
                lambda: context.run(
                    asyncio.run,
                    self._run_async(request, project_context=project_context),
                ),
            )
            return future.result()

    async def run_async(
        self,
        request: str,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        """Run delegation on the caller's event loop."""
        return await self._run_async(request, project_context=project_context)


class _AgentTeamRoleAgent:
    def __init__(
        self,
        *,
        model: str,
        role_key: str,
        label: str,
        tools: Sequence[ToolDefinition],
    ) -> None:
        self.model = model
        self.role_key = role_key
        self.label = label
        self.tools = list(tools)
        self._agent: NativeAgentDefinition | None = None

    @property
    def agent(self) -> NativeAgentDefinition:
        if self._agent is None:
            instructions = TEAM_ROLE_INSTRUCTIONS.get(
                self.role_key,
                f"You are the Agent Team {self.label} teammate.",
            )
            self._agent = NativeAgentDefinition(
                name=f"AgentTeam_{self.role_key}",
                model=self.model,
                instructions=instructions,
                model_settings=NativeModelSettings(tool_choice="auto"),
                tools=list(self.tools),
            )
        return self._agent


def _agent_team_read_tools() -> list[ToolDefinition]:
    from ..tools.file_explorer.file_explorer_tools import (
        find_workspace_items,
        get_workspace_file_info,
        inspect_workspace_tree,
        list_workspace_files,
        read_workspace_file,
    )
    from ..tools.repo_map.tools import get_repo_map

    return ensure_tool_definitions(
        [
            list_workspace_files,
            find_workspace_items,
            inspect_workspace_tree,
            read_workspace_file,
            get_workspace_file_info,
            get_repo_map,
        ]
    )


class AgentTeamRoleDelegationRunner(SpecialistDelegationRunner):
    def __init__(
        self,
        config: Config,
        *,
        member_key: str,
        display_name: str,
        model: Optional[str] = None,
    ):
        super().__init__(
            config,
            domain_key=member_key,
            display_name=display_name,
            agent_class=object,
            model=model,
        )

    def _create_agent_instance(self, model: Optional[str] = None):
        return _AgentTeamRoleAgent(
            model=model or self.model or self._select_native_openai_model(),
            role_key=self.domain_key,
            label=self.display_name,
            tools=_agent_team_read_tools(),
        )


class SpotifyDelegationRunner(SpecialistDelegationRunner):
    def __init__(self, config: Config, model: Optional[str] = None):
        from ..agents.spotify_agent import SpotifyAgent

        super().__init__(
            config,
            domain_key="spotify",
            display_name="Spotify",
            agent_class=SpotifyAgent,
            model=model,
        )


class UtilityDelegationRunner(SpecialistDelegationRunner):
    def __init__(self, config: Config, model: Optional[str] = None):
        from ..agents.utility_agent import UtilityAgent

        super().__init__(
            config,
            domain_key="utility",
            display_name="Utility",
            agent_class=UtilityAgent,
            model=model,
        )


class MediaDelegationRunner(SpecialistDelegationRunner):
    def __init__(self, config: Config, model: Optional[str] = None):
        from ..agents.media_agent import MediaAgent

        super().__init__(
            config,
            domain_key="media",
            display_name="Media",
            agent_class=MediaAgent,
            model=model,
        )


class ScenarioDelegationRunner(SpecialistDelegationRunner):
    def __init__(self, config: Config, model: Optional[str] = None):
        from ..agents.scenario_agent import ScenarioAgent

        super().__init__(
            config,
            domain_key="scenario",
            display_name="Scenario",
            agent_class=ScenarioAgent,
            model=model,
        )


class WritingDelegationRunner(SpecialistDelegationRunner):
    def __init__(self, config: Config, model: Optional[str] = None):
        from ..agents.writing_agent import WritingAgent

        super().__init__(
            config,
            domain_key="writing",
            display_name="Writing",
            agent_class=WritingAgent,
            model=model,
        )


class ImportDelegationRunner(SpecialistDelegationRunner):
    def __init__(self, config: Config, model: Optional[str] = None):
        from ..agents.import_agent import ImportAgent

        super().__init__(
            config,
            domain_key="import",
            display_name="Import",
            agent_class=ImportAgent,
            model=model,
        )
