"""Shared delegation runners for specialist agents."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional, Sequence, Type

from agents import Runner
from openai import OpenAI

from ..config import Config
from ..services.project_context import (
    format_project_context_for_prompt,
    reset_runtime_project_context,
    set_runtime_project_context,
)
from ..services.model_sharing_service import (
    build_redacted_prompt,
    config_get,
    model_sharing_confirm_prompt,
    model_sharing_enabled,
    model_sharing_model,
    model_sharing_notify,
    model_sharing_provider,
)
from ..tools.adapters import CLIAdapter, OpenAIAPIAdapter
from ..tools.core import ToolDefinition, ToolParam
from ..tools.external import MCPPlugin, set_mcp_plugin
from ..tools.external_llm_permission import request_external_model_prompt
from ..tools.registry import ToolRegistry
from .agent_runtime import run_openai_tool_call_loop
from .json_tool_loop import (
    JsonToolCallRecord,
    JsonToolLoopResult,
    build_json_tool_loop_system_prompt,
    run_json_tool_loop,
)
from .tool_policy import project_management_required_mutation_tools

logger = logging.getLogger(__name__)

CLI_PROVIDER_NAMES = {"gemini-cli", "claude-cli", "codex-cli"}
AGENTS_SDK_MODEL_PREFIXES = ("openai/", "litellm/", "gpt-", "o1", "o3", "o4")
OLLAMA_PROVIDER_NAME = "ollama"
OPENAI_COMPATIBLE_PROVIDER_NAMES = {
    "openai_compatible_local",
    "sglang",
    "openrouter",
}


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        return config_get(config, key, default)
    if hasattr(config, "get"):
        return config.get(key, default)
    return default


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

        self.provider = self._select_provider()
        self.model = model or self._select_model()
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

    def _uses_model_sharing_target(self) -> bool:
        if not model_sharing_enabled(self.config):
            return False
        if self.domain_key == "search":
            from ..services.quick_search_service import (
                SEARCH_PROVIDER_LOCAL,
                get_search_provider,
            )

            return get_search_provider(self.config) != SEARCH_PROVIDER_LOCAL
        return False

    def _main_provider(self) -> str:
        provider = str(_config_get(self.config, "llm_provider", "openai")).strip().lower()
        return provider or "openai"

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
            "gemini-cli": ("gemini_cli.model",),
        }
        for key in provider_model_keys.get(provider, ()):
            value = str(_config_get(self.config, key, "") or "").strip()
            if value:
                return value
        return None

    def _select_provider(self) -> str:
        if self._uses_model_sharing_target():
            return model_sharing_provider(self.config)
        return self._main_provider()

    def _select_agents_sdk_model(self) -> str:
        configured_model = self.model or self._main_model_for_provider(self.provider)
        if configured_model and str(configured_model).strip().startswith(
            AGENTS_SDK_MODEL_PREFIXES
        ):
            return str(configured_model).strip()

        if _config_get(self.config, "openai_api_key"):
            return "gpt-4o-mini"

        llm_model = str(_config_get(self.config, "llm_model", "")).strip()
        if llm_model.startswith(AGENTS_SDK_MODEL_PREFIXES):
            return llm_model

        logger.warning(
            "[%sDelegationRunner] No Agents SDK compatible model configured; "
            "falling back to gpt-4o-mini",
            self.display_name,
        )
        return "gpt-4o-mini"

    def _select_model(self) -> Optional[str]:
        if self._uses_model_sharing_target():
            return model_sharing_model(self.config)

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

        return self._select_agents_sdk_model()

    def _create_cli_backend(self):
        if self.provider == "gemini-cli":
            from .cli_backends.gemini import GeminiCLIBackend

            return GeminiCLIBackend()

        if self.provider == "claude-cli":
            from .cli_backends.claude import ClaudeCLIBackend

            return ClaudeCLIBackend(model=self.model)

        if self.provider == "codex-cli":
            from .cli_backends.codex import CodexCLIBackend

            return CodexCLIBackend(model=self.model)

        raise ValueError(f"Unsupported specialist CLI provider: {self.provider}")

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
                model=self.model or self._select_agents_sdk_model()
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
        schema = getattr(agent_tool, "params_json_schema", {}) or {}
        properties = schema.get("properties", {}) or {}
        required = set(schema.get("required", []) or [])

        params = []
        for name, spec in properties.items():
            param_type = spec.get("type", "string")
            if isinstance(param_type, list):
                non_null_types = [value for value in param_type if value != "null"]
                param_type = non_null_types[0] if non_null_types else "string"

            params.append(
                ToolParam(
                    name=name,
                    type=param_type,
                    description=spec.get("description", ""),
                    required=name in required,
                    default=spec.get("default"),
                    enum=spec.get("enum"),
                )
            )

        def _invoke(**kwargs):
            payload = json.dumps(kwargs, ensure_ascii=False)
            return agent_tool.on_invoke_tool(None, payload)

        return ToolDefinition(
            name=getattr(agent_tool, "name", "tool"),
            description=getattr(agent_tool, "description", "")
            or getattr(agent_tool, "name", "tool"),
            function=_invoke,
            parameters=params,
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
                "You are running as a specialist sub-agent.",
                "Use the available tools when they are needed to complete the request.",
                "If a tool is needed, emit the exact [TOOL_CALL: tool_name(key=value)] format.",
                "After tool results are provided, continue and answer naturally.",
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
                "# Tool Execution Results",
                f"Original specialist request: {original_input}",
                "",
                "Initial response:",
                initial_response,
                "",
                tool_results_text,
                "",
                "# Your Task",
                "Use the tool results above to finish the user's request.",
                "Do not emit another tool call unless it is still required.",
                "Return the final user-facing answer.",
            ]
        )

    def _run_via_cli(self, request: str) -> str:
        if self.cli_backend is None:
            return f"{self.display_name} CLI backend is not configured"

        registry = self._build_tool_registry()
        system_context = self._build_cli_system_context()

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

        tool_calls = self.cli_backend.parse_tool_calls(cli_output)
        if not tool_calls:
            return cli_output

        logger.info(
            "[%sDelegationRunner] Executing %s CLI tool call(s)",
            self.display_name,
            len(tool_calls),
        )
        tool_results = CLIAdapter.execute_tool_calls(tool_calls, registry)
        results_text = CLIAdapter.format_tool_results(tool_results)

        follow_up = self._build_cli_follow_up_prompt(request, cli_output, results_text)
        success2, final_output = self.cli_backend.execute_prompt(
            follow_up,
            cwd=Path.cwd(),
        )
        if not success2:
            logger.error(
                "[%sDelegationRunner] CLI follow-up failed: %s",
                self.display_name,
                final_output,
            )
            return cli_output

        return final_output

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
                            "If the request asks to search, look up, verify, check current information, "
                            "or uses Japanese terms such as 検索, 調べて, 確認, 最新, use a relevant search tool first."
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
            response = client.chat.completions.create(
                model=model,
                messages=messages_payload,
                temperature=0,
                max_tokens=1024,
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content or ""

        required_tools = self._required_ollama_tool_names(request)
        result = run_json_tool_loop(
            create_completion=_create,
            initial_messages=initial_messages,
            registry=registry,
            original_request=request,
            required_tool_names=required_tools,
            required_tool_reason=self._required_ollama_tool_reason(request, required_tools),
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

        messages = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": request},
        ]
        api_kwargs: dict[str, Any] = {
            "model": self.model or "local-model",
            "messages": messages,
            "temperature": 0,
            "max_tokens": 1024,
        }
        if len(registry) > 0:
            api_kwargs["tools"] = OpenAIAPIAdapter.convert_all(registry.get_all())
            api_kwargs["tool_choice"] = "auto"

        response = self._create_openai_compatible_completion(client, api_kwargs)
        message = response.choices[0].message
        if getattr(message, "tool_calls", None):
            return run_openai_tool_call_loop(
                initial_messages=messages,
                assistant_message=message,
                api_kwargs=api_kwargs,
                registry=registry,
                create_completion=lambda kwargs: self._create_openai_compatible_completion(client, kwargs),
                log_prefix=f"{self.display_name}DelegationRunner",
                max_rounds=5,
            )
        return getattr(message, "content", None) or ""

    async def _approve_external_model_request(self, delegated_request: str) -> Optional[str]:
        if not self._uses_model_sharing_target():
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
            description=f"Review the {self.display_name} assistant prompt before sending it to {self.provider}/{self.model}.",
            confirm=model_sharing_confirm_prompt(self.config),
            notify=model_sharing_notify(self.config),
            request_kind=f"{self.domain_key}_assistant",
        )

    def _required_ollama_tool_names(self, request: str) -> set[str]:
        return set()

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
                model=self.model or self._select_agents_sdk_model()
            )
            result = await Runner.run(agent.agent, delegated_request)
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


class SearchDelegationRunner(SpecialistDelegationRunner):
    def __init__(self, config: Config, model: Optional[str] = None):
        from ..agents.search_agent import SearchAgent

        effective_model = model
        if effective_model is None and self._config_uses_direct_local_search(config):
            # Direct local search does not invoke a specialist LLM, but the base
            # runner still stores a model field. Provide a placeholder to avoid
            # misleading fallback warnings.
            effective_model = "gpt-4o-mini"

        super().__init__(
            config,
            domain_key="search",
            display_name="Search",
            agent_class=SearchAgent,
            model=effective_model,
        )

    @staticmethod
    def _config_uses_direct_local_search(config: Any) -> bool:
        from ..agents.search_agent import _knowledge_search_enabled, _x_search_enabled
        from ..services.quick_search_service import (
            SEARCH_PROVIDER_LOCAL,
            get_search_provider,
        )
        from .tool_policy import is_memory_search_enabled

        return (
            get_search_provider(config) == SEARCH_PROVIDER_LOCAL
            and not _x_search_enabled(config)
            and not _knowledge_search_enabled(config)
            and not is_memory_search_enabled(config)
        )

    def _should_use_direct_local_search(self) -> bool:
        return self._config_uses_direct_local_search(self.config)

    def run(
        self,
        request: str,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        if self._should_use_direct_local_search():
            from ..tools.basic.web_search import web_search_with_config
            from ..services.quick_search_service import normalize_local_search_query

            return web_search_with_config(
                normalize_local_search_query(request),
                config=self.config,
            )

        return super().run(request, project_context=project_context)

    async def run_async(
        self,
        request: str,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        if self._should_use_direct_local_search():
            from ..services.quick_search_service import normalize_local_search_query
            from ..tools.basic.web_search import web_search_with_config

            return await asyncio.to_thread(
                web_search_with_config,
                normalize_local_search_query(request),
                config=self.config,
            )

        return await super().run_async(
            request,
            project_context=project_context,
        )


class FilesystemDelegationRunner(SpecialistDelegationRunner):
    def __init__(self, config: Config, model: Optional[str] = None):
        from ..agents.filesystem_agent import FilesystemAgent

        super().__init__(
            config,
            domain_key="filesystem",
            display_name="Filesystem",
            agent_class=FilesystemAgent,
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


class SkillsDelegationRunner(SpecialistDelegationRunner):
    def __init__(self, config: Config, model: Optional[str] = None):
        from ..agents.skills_agent import SkillsAgent

        super().__init__(
            config,
            domain_key="skills",
            display_name="Skills",
            agent_class=SkillsAgent,
            model=model,
        )


class ProjectManagementDelegationRunner(SpecialistDelegationRunner):
    def __init__(self, config: Config, model: Optional[str] = None):
        from ..agents.project_management_agent import ProjectManagementAgent

        super().__init__(
            config,
            domain_key="project_management",
            display_name="ProjectManagement",
            agent_class=ProjectManagementAgent,
            model=model,
        )

    def _required_ollama_tool_names(self, request: str) -> set[str]:
        return project_management_required_mutation_tools(request)

    def _required_ollama_tool_reason(
        self,
        request: str,
        required_tools: set[str],
    ) -> str | None:
        if not required_tools:
            return None
        return (
            "The user requested a project/task mutation. The mutation must be "
            "performed by the project management tool before the assistant can "
            "claim it is complete."
        )

    def _validate_ollama_tool_loop_result(
        self,
        request: str,
        result: JsonToolLoopResult,
        required_tools: set[str],
    ) -> str:
        if not required_tools:
            return result.final_output

        successful_calls = [
            call
            for call in result.tool_calls
            if call.tool in required_tools and call.successful
        ]
        if successful_calls:
            return self._format_mutation_tool_result(successful_calls[-1])

        tools = ", ".join(f"`{name}`" for name in sorted(required_tools))
        attempted = ", ".join(
            f"{call.tool}: {call.result[:200]}"
            for call in result.tool_calls
        ) or "none"
        return "\n".join(
            [
                "ProjectManagement delegation error: requested mutation was not completed.",
                f"Required tool confirmation was missing. Expected one of: {tools}.",
                f"Executed tool calls: {attempted}",
                "Do not tell the user the task or schedule was added.",
            ]
        )

    def _format_mutation_tool_result(self, call: JsonToolCallRecord) -> str:
        try:
            payload = json.loads(call.result)
        except Exception:
            payload = None

        task = payload
        if isinstance(payload, dict):
            for key in ("task", "created_task", "updated_task", "result"):
                if isinstance(payload.get(key), dict):
                    task = payload[key]
                    break

        if isinstance(task, dict):
            lines = ["タスク操作を完了しました。", f"- tool: {call.tool}"]
            field_labels = {
                "id": "task_id",
                "title": "title",
                "project_id": "project_id",
                "status": "status",
                "priority": "priority",
                "start_at": "start_at",
                "end_at": "end_at",
                "due_at": "due_at",
            }
            for field, label in field_labels.items():
                value = task.get(field)
                if value is not None and str(value).strip():
                    lines.append(f"- {label}: {value}")
            if len(lines) > 2:
                return "\n".join(lines)

        return "\n".join(
            [
                "タスク操作を完了しました。",
                f"- tool: {call.tool}",
                f"- tool_result: {call.result}",
            ]
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
