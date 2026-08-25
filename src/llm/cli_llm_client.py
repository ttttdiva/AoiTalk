"""
CLI-based LLM Client (supports Antigravity CLI, Claude Code, Codex CLI, Grok Build CLI)

CLI（Antigravity/Claude/Codex/Grok Build）をLLMバックエンドとして使用するクライアント実装。
AgentLLMClientと互換のインターフェースを提供する。
"""

import asyncio
import inspect
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Optional, List, Dict, Any, Union, Generator

from ..config import Config
from ..features import Features
from ..services.project_context import (
    ProjectContextResolver,
    format_minimal_project_context_for_chat_prompt,
    get_runtime_project_context,
    project_context_enabled_for_client,
    reset_runtime_project_context,
    sanitize_project_context_for_chat,
    set_runtime_project_context,
)
from ..services.context_builder import ContextBuilder, ContextBundle
from ..services.turn_context import get_turn_context
from ..services.outbound_privacy_service import (
    current_effective_privacy_mode,
    effective_privacy_mode,
)
from .context_snapshot import (
    component,
    context_bundle_components,
    reconcile_snapshot,
    sanitized_snapshot_series,
    snapshot,
)
from ..services.story_chat_context import (
    build_story_chat_context,
    run_story_chat_context_sync,
    is_story_workflow_tool_allowed,
)
from .cli_backends.base import CLIBackendBase, CLISessionCapabilities
from .cli_sessions import (
    CLINativeSessionScope,
    CLISessionBusyError,
    CLISessionLease,
    CLISessionStore,
    fingerprint_settings,
    mask_native_session_id,
    resolve_branch_key,
)
from .prompts import build_unified_instructions
from .agent_runtime import (
    OpenAIToolCallRecord,
    build_tool_hint_context_sync,
    combined_final_response_check,
    compose_tool_hint_user_message,
    guard_tool_execution_claims,
    project_context_required_read_tool_names,
)
from .cli_tool_context import build_cli_tool_context
from .context_budget import clip_text
from .unified_turn_runtime import (
    CLI_TOOL_LOOP_FAILURE_MESSAGE,
    run_cli_tool_call_loop,
)

# Memory management
from ..memory.manager import ConversationMemoryManager
from ..memory.config import MemoryConfig
from ..memory.history import HistoryManager

# Reasoning support
from ..reasoning import ReasoningManager

# Unified tool registry and adapters
from ..tools.adapters import CLIAdapter
from .generation_policy import (
    DEFAULT_GENERATION_POLICY,
    GenerationProfile,
    get_client_generation_policy,
    reset_current_generation_policy,
    set_current_generation_policy,
)
from .generation_cancellation import (
    GenerationCancelled,
    GenerationInterrupted,
    get_current_generation_cancellation,
    raise_if_generation_interrupted,
)
from .runtime_tool_registry import (
    build_runtime_tool_registry,
    build_runtime_tool_registry_for_client,
)
from .tool_packs import ensure_load_tool_pack_tool
from .tool_exposure import (
    apply_story_pack_auto_load,
    effective_tool_pack_session,
    filter_tools_for_client,
    filtered_registry_for_client,
    is_review_generation,
)
from .tool_policy import (
    looks_like_managed_workspace_request,
    reset_current_user_input,
    set_current_user_input,
)
from ..services.user_settings_service import get_user_custom_instructions_sync

logger = logging.getLogger(__name__)

DEFAULT_CLI_TOOL_RESULT_MAX_CHARS = 8000
MAX_CLI_TOOL_RESULT_MAX_CHARS = 16000
# CLI-native tool loops need a bounded budget, but work profiles must be able
# to inspect and update more than the short chat budget.  This is intentionally
# far below the API/native runtime's 120-round work ceiling because every CLI
# round starts another provider process/request.
DEFAULT_CLI_TOOL_ROUND_LIMIT = 5
DEFAULT_CLI_WORK_TOOL_ROUND_LIMIT = 12
MAX_CLI_TOOL_ROUND_LIMIT = 12
CLI_TOOL_LOOP_EXHAUSTED_MESSAGE = (
    "ツール実行回数の上限に達したため、処理を完了できませんでした。"
    "未完了の操作は実行していないため、内容を確認してから再実行してください。"
)
CLI_TOOL_LOOP_SUCCESS_REASONS = frozenset(
    {
        "final",
        "redundant_tool_call_suppressed",
        "redundant_tool_call_repeated",
    }
)
CLI_REVIEW_ISOLATION_UNAVAILABLE = (
    "REVIEW用途では、選択中のCLI providerが内蔵ツールとMCPを"
    "陽性リストへ確実に限定できないため、モデル実行を停止しました。"
    "REVIEW対応のAPIまたはローカルproviderを選択してください。"
)

_PLAIN_TEXT_MODEL_OVERRIDE_UNSET = object()


def _is_codex_model_unavailable(value: Any) -> bool:
    """Return whether Codex rejected the configured model for this account."""
    lowered = str(value or "").lower()
    if "usage limit" in lowered or "rate limit" in lowered:
        return False
    if "not supported when using codex with a chatgpt account" in lowered:
        return True
    if "model" not in lowered:
        return False
    return any(
        phrase in lowered
        for phrase in (
            "not supported",
            "unsupported",
            "does not support",
            "unknown model",
            "invalid model",
        )
    )


def _truncate_cli_output_to_token_cap(value: str, max_tokens: Optional[int]) -> str:
    """Tokenizer非依存でtoken上限を超えないUTF-8 byte上限を適用する。"""

    if not max_tokens:
        return value
    encoded = str(value or "").encode("utf-8")
    limit = max(1, int(max_tokens))
    if len(encoded) <= limit:
        return str(value or "")
    return encoded[:limit].decode("utf-8", errors="ignore")


class CLILLMClient:
    """
    CLI-based LLM client (supports Antigravity/Claude/Codex/Grok Build)

    AgentLLMClientと互換性のあるインターフェースを提供し、
    外部CLIツールを通じて推論・応答生成を行う。
    """

    def __init__(self, config: Optional[Config] = None, cli_backend: Optional[CLIBackendBase] = None):
        if config is None:
            raise ValueError("Config is required for CLILLMClient")

        self.config = config
        self.character_name = config.default_character
        self.model_name = config.get('llm_model', 'cli')

        # CLI backend (Antigravity/Claude/Codex/Grok Build)
        if cli_backend is None:
            from .cli_backends.antigravity import AntigravityCLIBackend
            self.cli_backend = AntigravityCLIBackend()
        else:
            self.cli_backend = cli_backend

        # Serialize plain-text jobs on this client so backend stream/usage
        # state cannot race with another plain-text request.
        self._plain_text_lock = threading.Lock()
        # The backend keeps per-invocation stream/usage fields, so all CLI
        # invocations on a shared runtime client must be serialized.
        self._cli_execution_lock = threading.RLock()

        logger.info(f"[CLILLMClient] Using {self.cli_backend.get_provider_name()}")

        # Character configuration
        self.character_config = config.get_character_config(self.character_name)

        # Session context
        self.session_user_id: str = "default_user"
        self.session_metadata: Dict[str, Any] = {}
        self._privacy_session_context: Dict[str, Any] = {}
        self._privacy_project_metadata: Dict[str, Any] = {}
        self.current_session_id: Optional[str] = None
        self.current_project_id: Optional[str] = None
        self.generation_policy = DEFAULT_GENERATION_POLICY
        self.current_edit_message_id: Optional[str] = None
        self._current_context_bundle: Optional[ContextBundle] = None
        self._last_context_snapshots: list[dict[str, Any]] = []
        self._last_cli_usage: dict[str, Any] = {}
        self._agent_run_usage: dict[str, int] = {}
        self._last_turn_tool_rounds_exhausted = False
        self._last_turn_tool_loop_failed = False
        self._cli_native_session_reset_requested = False
        self._cli_native_session_info: dict[str, Any] = {
            "mode": "stateless",
            "recreated": False,
            "native_session_id_masked": None,
        }

        # History manager (HistoryManager を使用、独自リストではなく)
        self.history_manager = HistoryManager()
        self._summarize_batch_size = 10
        self._summarize_threshold = 20

        # Custom system prompt
        self.custom_system_prompt: Optional[str] = None

        # LLM mode
        self._current_llm_mode = 'fast'

        # Config helper
        def get_config_value(key, default=None):
            if hasattr(config, 'get'):
                return config.get(key, default)
            return default

        # Memory manager (ConversationMemoryManager)
        self.memory_manager = None
        self._memory_enabled = get_config_value('memory', {}).get('enabled', True)

        if self._memory_enabled:
            try:
                memory_config = MemoryConfig()
                memory_config.llm_provider = get_config_value(
                    'llm_provider', memory_config.llm_provider
                )
                memory_config.llm_model = get_config_value(
                    'llm_model', memory_config.llm_model
                )
                memory_settings = get_config_value('memory', {})
                memory_config.embedding_model = memory_settings.get(
                    'embedding_model', memory_config.embedding_model
                )
                memory_config.preload_embedding_model = memory_settings.get(
                    'preload_embedding_model', memory_config.preload_embedding_model
                )
                memory_config.enable_search = memory_settings.get(
                    'enable_search', memory_config.enable_search
                )
                self.memory_manager = ConversationMemoryManager(memory_config, app_config=config)
                logger.info("[CLILLMClient] ConversationMemoryManager初期化完了")
            except Exception as e:
                logger.warning(f"[CLILLMClient] Memory manager initialization failed: {e}")

        # MCP: CLIネイティブ委譲（MCPPluginは使用しない）
        # Claude Code: --mcp-config で実行時渡し
        # Antigravity CLI / Codex CLI: 各CLIの設定ファイルで事前設定が必要
        self._mcp_servers: dict = {}
        if not Features.is_enterprise() and config.get('mcp_enabled', False):
            self._mcp_servers = config.get('mcp', {}).get('servers', {})
            if self._mcp_servers:
                mcp_args = self.cli_backend.get_mcp_args(self._mcp_servers)
                if mcp_args:
                    logger.info(
                        f"[CLILLMClient] MCP: {len(self._mcp_servers)} server(s) will be "
                        f"passed via CLI args ({self.cli_backend.get_provider_name()})"
                    )
                else:
                    logger.info(
                        f"[CLILLMClient] MCP: {len(self._mcp_servers)} server(s) in config, "
                        f"but {self.cli_backend.get_provider_name()} requires native settings file"
                    )

        # Reasoning manager
        self.reasoning_manager = None
        reasoning_config = get_config_value('reasoning', {})
        if reasoning_config.get('enabled', False):
            self.reasoning_manager = ReasoningManager(self, reasoning_config)
            logger.info(
                f"[CLILLMClient] 推論モード初期化完了 "
                f"(閾値: {reasoning_config.get('complexity_threshold', 0.6)})"
            )

        # Tool registry
        self._tool_registry = build_runtime_tool_registry_for_client(
            build_runtime_tool_registry,
            self.config,
            client=self,
        )
        ensure_load_tool_pack_tool(self._tool_registry, self)
        logger.info(
            f"[CLILLMClient] Initialized: character={self.character_name}, "
            f"backend={self.cli_backend.get_provider_name()}, "
            f"tools={len(self._tool_registry)}"
        )

    # ------------------------------------------------------------------
    # Session / Character management
    # ------------------------------------------------------------------

    def set_session_context(
        self, user_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None
    ):
        if user_id:
            self.session_user_id = str(user_id)
        if metadata:
            sanitized = {k: str(v) for k, v in metadata.items() if v is not None}
            self.session_metadata = {**self.session_metadata, **sanitized}
            if "privacy_mode" in metadata:
                self._privacy_session_context["privacy_mode"] = str(
                    metadata.get("privacy_mode") or ""
                )

    def _get_session_user_id(self) -> str:
        return self.session_user_id or "default_user"

    def set_character(self, character_name: str):
        self.character_name = character_name
        self.character_config = self.config.get_character_config(character_name)
        # キャラクター変更後に起動時の古いプロンプトを再利用しない。
        # _build_system_context は custom_system_prompt を最優先するため、
        # ここを残すとヘッダーで選択したキャラクターが実生成へ反映されない。
        self.custom_system_prompt = None
        self._cli_native_session_reset_requested = True
        logger.info(f"[CLILLMClient] Character changed to: {character_name}")

    def update_character(self, yaml_filename: str):
        if self.config:
            new_config = self.config.get_character_config(yaml_filename)
            if new_config:
                self.character_name = new_config.get('name', yaml_filename)
                self.character_config = new_config
                # update_character はヘッダー切替からも呼ばれるため、
                # set_character と同じく旧キャラクターのプロンプトを破棄する。
                self.custom_system_prompt = None
                self.clear_history()
                self._cli_native_session_reset_requested = True
                logger.info(f"[CLILLMClient] キャラクター更新: {self.character_name} (会話履歴クリア済み)")
            else:
                logger.warning(f"[CLILLMClient] キャラクター設定が見つかりません: {yaml_filename}")

    def set_system_prompt(self, prompt: str):
        self.custom_system_prompt = prompt
        self._cli_native_session_reset_requested = True
        logger.info(f"[CLILLMClient] Custom system prompt set: {prompt[:50]}...")

    def set_llm_mode(self, mode: str):
        if mode not in ['fast', 'thinking']:
            logger.warning(f"[CLILLMClient] Invalid mode '{mode}', defaulting to 'fast'")
            mode = 'fast'
        self._current_llm_mode = mode
        logger.info(f"[CLILLMClient] LLM mode set to: {mode}")

    def get_llm_mode(self) -> str:
        return self._current_llm_mode

    # ------------------------------------------------------------------
    # History management
    # ------------------------------------------------------------------

    def clear_history(self):
        self.history_manager.clear()
        self._cli_native_session_reset_requested = True
        logger.info("[CLILLMClient] Conversation history cleared")

    def _get_cli_session_capabilities(self) -> CLISessionCapabilities:
        if not isinstance(self.cli_backend, CLIBackendBase):
            return CLISessionCapabilities()
        try:
            from ..security.agent_run_scope import get_current_run_scope

            if get_current_run_scope() is not None:
                # Provider capability probes invoke the host CLI's ``--help``
                # command.  A trusted repository run must stay entirely on
                # the WSL+bwrap lane, so scoped turns use stateless mode.
                return CLISessionCapabilities()
        except Exception:
            pass
        try:
            capabilities = self.cli_backend.get_session_capabilities()
        except Exception:
            logger.warning("[CLILLMClient] CLI session capability probe failed", exc_info=True)
            return CLISessionCapabilities()
        return capabilities if isinstance(capabilities, CLISessionCapabilities) else CLISessionCapabilities()

    def _native_chat_session_allowed(self) -> bool:
        capabilities = self._get_cli_session_capabilities()
        if not (
            capabilities.native_sessions
            and capabilities.supports_resume
            and capabilities.supports_follow_up
        ):
            return False
        if not capabilities.fallback_to_stateless:
            return False
        if not self.current_session_id:
            return False
        if get_client_generation_policy(self).profile != GenerationProfile.CHAT:
            return False
        if is_review_generation(self):
            return False
        settings = self.config.get("cli_native_sessions", {})
        if isinstance(settings, dict) and settings.get("enabled") is False:
            return False
        return True

    def _build_cli_session_scope(self, cwd: Path) -> CLINativeSessionScope:
        provider = self.cli_backend.get_provider_name().lower().replace(" ", "-")
        model = str(
            getattr(self, "current_response_model", None)
            or getattr(self.cli_backend, "_model", None)
            or self.model_name
            or "cli"
        )
        try:
            user_custom_instructions = get_user_custom_instructions_sync(
                self._get_session_user_id()
            )
        except Exception:
            user_custom_instructions = None
        policy = get_client_generation_policy(self)
        fingerprint = fingerprint_settings(
            {
                "provider": provider,
                "model": model,
                "character_name": self.character_name,
                "character_config": self.character_config or {},
                "custom_system_prompt": self.custom_system_prompt or "",
                "user_custom_instructions": user_custom_instructions or "",
                "generation_profile": str(policy.profile),
                "permission_policy": str(policy.permission_policy),
                "sandbox": {
                    key: os.getenv(key, "")
                    for key in (
                        "CODEX_SANDBOX",
                        "CODEX_APPROVAL_POLICY",
                        "AGY_SANDBOX",
                        "AGY_AUTO_APPROVE",
                        "CLAUDE_ALLOWED_TOOLS",
                    )
                },
                "mcp_servers": sorted(str(key) for key in self._mcp_servers),
                "include_project_context": self.current_include_project_context,
            }
        )
        project_key = str(self.current_project_id or "none")
        return CLINativeSessionScope(
            chat_session_id=str(self.current_session_id),
            branch_key="pending",
            provider=provider,
            model=model,
            project_key=project_key,
            working_directory=str(cwd.resolve()),
            fingerprint=fingerprint,
        )

    def _build_cli_continuation_context(
        self,
        *,
        project_context_read_block: str,
        tool_hint_context: str,
        max_tokens: Optional[int],
    ) -> str:
        """Build only per-turn context for an already resumed CLI session."""
        parts = ["# AoiTalk dynamic context for this turn"]
        if project_context_read_block:
            parts.extend(["", "Project context update:", project_context_read_block])
        # ``compose_tool_hint_user_message`` places the current hint in the
        # user prompt. Keep one copy only; static tool definitions remain in
        # the provider-managed native session.
        if max_tokens:
            parts.extend(
                [
                    "",
                    f"Keep the final response within {max(1, int(max_tokens))} tokens.",
                ]
            )
        return "\n".join(parts) if len(parts) > 1 else ""

    def _build_native_follow_up_prompt(
        self,
        original_input: str,
        initial_response: str,
        tool_results_text: str,
    ) -> str:
        """Return only new tool results for a provider-managed conversation."""
        story_chat_context = self._get_story_chat_context_sync()
        task_instruction = (
            "シナリオ進行の応答を続けてください。"
            if story_chat_context
            else "ツール結果に基づいて、ユーザーへ自然な最終回答を返してください。"
        )
        return "\n".join(
            [
                "# AoiTalk tool result",
                "ツール結果を受け取った後は、要求が満たされたか判定してください。",
                "満たされている場合は追加のツール呼び出しをせず、直ちに最終回答してください。",
                "追加のツール呼び出しは、エラー・不完全な結果・未完了の別タスク、または変更後検証が必要な場合だけ許可されます。",
                "",
                tool_results_text,
                "",
                "# Final response",
                task_instruction,
                "\nAssistant:",
            ]
        )

    @staticmethod
    def _sanitize_cli_session_error(value: Any, session_ids: list[Any]) -> str:
        """Prevent provider continuation handles from reaching chat errors."""
        text = str(value or "")
        for session_id in session_ids:
            candidate = str(session_id or "").strip()
            if candidate:
                text = text.replace(candidate, "[CLI session]")
        return text
    def get_history(self) -> List[Dict[str, Any]]:
        return self.history_manager.get_all()

    def _get_cli_execution_lock(self) -> threading.RLock:
        lock = getattr(self, "_cli_execution_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._cli_execution_lock = lock
        return lock

    def _ensure_cli_privacy_direct(self) -> None:
        """Fail closed for every direct CLI backend invocation.

        ``generate_response`` performs the same check at its turn boundary,
        but plain-text helpers and internal tracked calls are also callable by
        docs/tools integrations.  Keeping the guard at the central execution
        seam prevents those paths from bypassing the privacy policy.
        """

        provider_name = str(
            getattr(getattr(self, "cli_backend", None), "get_provider_name", lambda: "cli")()
            or "cli"
        ).strip().lower()
        session_context = getattr(self, "_privacy_session_context", {})
        project_metadata = getattr(self, "_privacy_project_metadata", {})
        if (
            effective_privacy_mode(
                getattr(self, "config", None),
                session_context=session_context or None,
                project_metadata=project_metadata or None,
            )
            != "direct"
            or current_effective_privacy_mode(getattr(self, "config", None))
            != "direct"
        ):
            raise RuntimeError(
                f"外部CLI provider ({provider_name}) は保護/ローカル限定モードでは使用できません。"
            )

    def _execute_prompt_tracked(self, *args: Any, **kwargs: Any):
        """Serialize access to the mutable CLI backend/runtime state."""
        with self._get_cli_execution_lock():
            return self._execute_prompt_tracked_locked(*args, **kwargs)

    def _execute_prompt_tracked_locked(self, *args: Any, **kwargs: Any):
        """CLI実行と、その構造化usageの永続化を一体で行う。"""
        self._ensure_cli_privacy_direct()
        # A trusted coding run must not let a lightweight/custom backend skip
        # the CLIBackendBase WSL+bwrap seam.  Ordinary (unscoped) chat keeps
        # the historical backend compatibility path below.
        try:
            from ..security.agent_run_scope import get_current_run_scope

            active_scope = get_current_run_scope()
        except Exception:
            active_scope = None
        if active_scope is not None:
            if not isinstance(self.cli_backend, CLIBackendBase):
                raise RuntimeError(
                    "active AgentRunScope requires a scope-capable CLI backend"
                )
            if not bool(getattr(self.cli_backend, "supports_scoped_run", False)):
                raise RuntimeError(
                    "CLI backend does not support the active AgentRunScope"
                )
            if (
                type(self.cli_backend).execute_prompt is not CLIBackendBase.execute_prompt
                and not bool(
                    getattr(self.cli_backend, "scoped_execution_delegate", False)
                )
            ):
                raise RuntimeError(
                    "custom CLI override must delegate to the verified WSL2/bwrap seam"
                )
        started = time.monotonic()
        if not hasattr(self, "_last_context_snapshots"):
            self._last_context_snapshots = []
        snapshot_components = list(kwargs.pop("_snapshot_components", []) or [])
        native_mode = str(kwargs.pop("_native_session_mode", "stateless") or "stateless")
        native_recreated = bool(kwargs.pop("_native_session_recreated", False))
        native_id_for_snapshot = kwargs.pop("_native_session_id_for_snapshot", None)
        snapshot_model = kwargs.pop("_snapshot_model", None)
        prompt = kwargs.get("prompt", args[0] if args else "")
        system_context = kwargs.get("system_context", "")
        mcp_args = kwargs.get("extra_args") or []
        parts = []
        rendered_bundle, bundle_parts = context_bundle_components(getattr(self, "_current_context_bundle", None))
        reduced_system_context = str(system_context)
        if rendered_bundle:
            reduced_system_context = reduced_system_context.replace(rendered_bundle, "", 1)
        for traced in snapshot_components:
            traced_text = str(traced.pop("_text", "") or "")
            if traced_text:
                reduced_system_context = reduced_system_context.replace(traced_text, "", 1)
        if reduced_system_context.strip():
            parts.append(component("system_instructions", "System instructions", reduced_system_context, source="CLI system_context"))
        # A resumed native session receives only the delta context. Do not
        # claim that the full ContextBundle was transmitted in its snapshot.
        if rendered_bundle and rendered_bundle in str(system_context):
            parts.extend(bundle_parts)
        parts.extend(snapshot_components)
        if prompt:
            parts.append(component("current_user_message", "Current user message", prompt, source="CLI prompt/stdin"))
        if mcp_args:
            parts.append(component("cli_tool_descriptions", "CLI tool descriptions", source="CLI MCP arguments", measurement="unavailable", preview=f"MCP設定を渡しました（{len(self._mcp_servers)} server）"))
        parts.append(
            component(
                "provider_managed",
                "Provider-managed context",
                source=self.cli_backend.get_provider_name(),
                measurement="unavailable",
                preview=(
                    "CLI内部の管理対象コンテキスト（AoiTalkから今回送信した履歴には含めない）"
                    if native_mode in {"new", "resumed"}
                    else "CLI内部のsystem prompt・組み込みtoolは詳細取得不能"
                ),
            )
        )
        parts.append(
            component(
                "cli_native_session",
                "CLI native session",
                source=self.cli_backend.get_provider_name(),
                measurement="unavailable",
                preview=(
                    f"mode={native_mode}; session={mask_native_session_id(native_id_for_snapshot) or 'none'}; "
                    f"recreated={str(native_recreated).lower()}"
                ),
            )
        )
        self._cli_native_session_info = {
            "provider": self.cli_backend.get_provider_name(),
            "mode": native_mode,
            "recreated": native_recreated,
            "native_session_id_masked": mask_native_session_id(native_id_for_snapshot),
        }
        request_snapshot = snapshot(
            provider=self.cli_backend.get_provider_name().lower().replace(" ", "-"),
            model=str(
                snapshot_model
                if snapshot_model is not None
                else getattr(self.cli_backend, "_model", None)
                or getattr(self, "model_name", "cli")
            ),
            components=parts,
            request_index=len(self._last_context_snapshots),
            request_kind="cli.execute_prompt",
        )
        self._last_context_snapshots.append(request_snapshot)
        self._last_context_snapshots = self._last_context_snapshots[-8:]
        consume_usage = getattr(self.cli_backend, "consume_last_usage", None)
        success = False
        output = ""
        usage: Optional[dict[str, Any]] = None
        try:
            success, output = self.cli_backend.execute_prompt(*args, **kwargs)
        finally:
            try:
                usage = consume_usage() if callable(consume_usage) else None
            except Exception:
                logger.warning(
                    "[CLILLMClient] CLI usageの取得に失敗しました",
                    exc_info=True,
                )
                usage = None

            self._last_cli_usage = dict(usage or {})
            if usage:
                if not self._agent_run_usage:
                    self._agent_run_usage = {
                        key: int(value)
                        for key, value in usage.items()
                        if key in {
                            "input_tokens",
                            "output_tokens",
                            "cached_tokens",
                            "total_tokens",
                        }
                    }
                else:
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cached_tokens",
                        "total_tokens",
                    ):
                        self._agent_run_usage[key] = int(
                            self._agent_run_usage.get(key, 0)
                        ) + int(usage.get(key, 0))
                self._last_context_snapshots[-1] = reconcile_snapshot(
                    request_snapshot, usage.get("input_tokens")
                )

            if success and usage:
                try:
                    from ..services.token_tracking_service import get_token_tracking_service

                    def _uuid_or_none(value: Any):
                        try:
                            return uuid.UUID(str(value)) if value else None
                        except (TypeError, ValueError, AttributeError):
                            return None

                    cli_model = str(
                        snapshot_model
                        if snapshot_model is not None
                        else getattr(self.cli_backend, "_model", None) or self.model_name
                    )

                    async def _record() -> None:
                        await get_token_tracking_service().record_usage(
                            provider=self.cli_backend.get_provider_name().lower().replace(" ", "-"),
                            model=cli_model,
                            requested_model=cli_model,
                            input_tokens=usage["input_tokens"],
                            output_tokens=usage["output_tokens"],
                            cached_tokens=usage.get("cached_tokens", 0),
                            cache_read_tokens=usage.get(
                                "cache_read_tokens", usage.get("cached_tokens", 0)
                            ),
                            cache_write_tokens=usage.get("cache_write_tokens", 0),
                            reasoning_tokens=usage.get("reasoning_tokens", 0),
                            prompt_eval_tokens=usage.get("prompt_eval_tokens", 0),
                            prompt_eval_ms=usage.get("prompt_eval_ms", 0),
                            session_id=_uuid_or_none(self.current_session_id),
                            user_id=self._get_session_user_id(),
                            project_id=_uuid_or_none(self.current_project_id),
                            agent_name=self.character_name,
                            request_type="cli",
                            latency_ms=int((time.monotonic() - started) * 1000),
                        )

                    self._run_async_in_new_loop(_record())
                except Exception:
                    logger.warning(
                        "[CLILLMClient] CLI usageの保存に失敗しました",
                        exc_info=True,
                    )
        return success, output

    def get_generation_metadata(self) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        if self._last_context_snapshots:
            bounded_snapshot = sanitized_snapshot_series(
                self._last_context_snapshots
            )
            if bounded_snapshot:
                metadata["context_snapshot"] = bounded_snapshot
        if self._last_cli_usage:
            metadata["cache_usage"] = dict(self._last_cli_usage)
        if self._agent_run_usage:
            metadata["agent_run_usage"] = dict(self._agent_run_usage)
        if self._cli_native_session_info:
            metadata["cli_native_session"] = dict(self._cli_native_session_info)
        return metadata

    def check_and_summarize_history(self, history_manager=None) -> None:
        hm = history_manager or self.history_manager
        if len(hm.history) > self._summarize_threshold:
            hm.pop_oldest(self._summarize_batch_size)
            logger.info(f"[CLILLMClient] History truncated to {len(hm.history)} messages")

    # ------------------------------------------------------------------
    # Response generation (sync)
    # ------------------------------------------------------------------

    def generate_response(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        image_data: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Union[str, Generator[str, None, None]]:
        logger.info(f"[CLILLMClient] Generating response for: {user_input[:50]}...")
        stream_callback = kwargs.get("stream_callback")

        try:
            response = self._generate_sync(
                user_input,
                image_data=image_data,
                stream_callback=stream_callback,
                max_tokens=max_tokens,
            )
        except GenerationCancelled:
            raise
        except GenerationInterrupted:
            # ResponseHandler owns the same-run retry contract.  Do not turn
            # a steering interrupt into a fallback response here.
            raise
        except Exception as e:
            logger.error(f"[CLILLMClient] Error: {e}", exc_info=True)
            if self.config.get("free_team.propagate_errors", False):
                raise
            personality = self.character_config.get('personality', {}) if self.character_config else {}
            response = personality.get('fallbackReply', 'エラーが発生しました')
            self._emit_stream_event_sync(
                stream_callback,
                "stream_end",
                {"content": response},
            )

        response = _truncate_cli_output_to_token_cap(response, max_tokens)

        if stream:
            def response_generator():
                yield response
            return response_generator()
        return response

    def _generate_sync(
        self,
        user_input: str,
        image_data: Optional[Dict[str, Any]] = None,
        stream_callback: Any = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Serialize a complete CLI turn, including native-session bookkeeping."""
        with self._get_cli_execution_lock():
            return self._generate_sync_locked(
                user_input,
                image_data=image_data,
                stream_callback=stream_callback,
                max_tokens=max_tokens,
            )

    def _generate_sync_locked(
        self,
        user_input: str,
        image_data: Optional[Dict[str, Any]] = None,
        stream_callback: Any = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Core synchronous generation logic"""
        self._ensure_cli_privacy_direct()
        provider_name = str(self.cli_backend.get_provider_name() or "").strip().lower()
        self._last_context_snapshots = []
        self._last_cli_usage = {}
        self._agent_run_usage = {}
        # A shared CLI client can serve consecutive sessions.  Never carry a
        # previous turn's exhaustion state into a successful new turn.
        self._last_turn_tool_rounds_exhausted = False
        self._last_turn_tool_loop_failed = False
        if is_review_generation(self):
            logger.warning(
                "[CLILLMClient] REVIEW generation blocked because native CLI "
                "tool isolation cannot be guaranteed"
            )
            return CLI_REVIEW_ISOLATION_UNAVAILABLE
        project_token = None
        tool_policy_token = set_current_user_input(user_input)
        generation_policy_token = set_current_generation_policy(
            get_client_generation_policy(self)
        )
        event_callback = self._make_stream_event_callback(stream_callback)
        cancellation_handle = get_current_generation_cancellation()
        image_cleanup = None
        native_store: Optional[CLISessionStore] = None
        native_lease: Optional[CLISessionLease] = None
        native_scope: Optional[CLINativeSessionScope] = None
        native_active_session_id: Optional[str] = None
        native_usable = False
        native_recreated = False

        try:
            project_context = self._resolve_project_context_sync()
            project_token = set_runtime_project_context(project_context)
            self._privacy_project_metadata = (
                dict((project_context or {}).get("metadata") or {})
                if isinstance(project_context, dict)
                and isinstance((project_context or {}).get("metadata"), dict)
                else {}
            )
            if (
                effective_privacy_mode(
                    self.config,
                    session_context=getattr(self, "_privacy_session_context", {}) or None,
                    project_metadata=self._privacy_project_metadata or None,
                )
                != "direct"
                or current_effective_privacy_mode(self.config) != "direct"
            ):
                raise RuntimeError(
                    f"外部CLI provider ({provider_name}) は保護/ローカル限定モードでは使用できません。"
                )
            self._current_context_bundle = self._build_context_bundle_sync(
                user_input, project_context
            )

            # Reasoning mode check
            if self.reasoning_manager:
                try:
                    if self.reasoning_manager.is_reasoning_required(
                        user_input, self._get_available_tools()
                    ):
                        logger.info("[CLILLMClient] Using reasoning mode")
                        return self._run_reasoning(user_input)
                except GenerationCancelled:
                    raise
                except Exception as e:
                    logger.warning(f"[CLILLMClient] Reasoning check failed: {e}")

            # Attach image if provided and backend supports it
            prompt = user_input
            if image_data:
                attachment = self.cli_backend.prepare_image_attachment(image_data, cwd=Path.cwd())
                if attachment:
                    suffix, image_cleanup = attachment
                    prompt = user_input + suffix
                    logger.info(f"[CLILLMClient] 画像添付あり: {image_data.get('name', 'unknown')}")
                else:
                    logger.warning(
                        f"[CLILLMClient] {self.cli_backend.get_provider_name()} は画像入力未対応"
                    )

            cli_cwd = self._cli_execution_cwd(user_input)
            if cli_cwd is None:
                cli_cwd = Path.cwd()

            # Native session acquisition is deliberately centralized here. A
            # provider is resumed only when the branch/settings fingerprint
            # matches and the conversation row lease is available.
            if self._native_chat_session_allowed():
                try:
                    branch_key = self._run_async_in_new_loop(
                        resolve_branch_key(
                            str(self.current_session_id),
                            current_edit_message_id=self.current_edit_message_id,
                        )
                    )
                    native_scope = replace(
                        self._build_cli_session_scope(cli_cwd),
                        branch_key=branch_key,
                    )
                    session_settings = self.config.get("cli_native_sessions", {})
                    lease_seconds = (
                        session_settings.get("lease_seconds", 45 * 60)
                        if isinstance(session_settings, dict)
                        else 45 * 60
                    )
                    native_store = CLISessionStore(lease_seconds=int(lease_seconds))
                    native_lease = self._run_async_in_new_loop(
                        native_store.acquire(
                            native_scope,
                            force_new=bool(self._cli_native_session_reset_requested),
                        )
                    )
                    self._cli_native_session_reset_requested = False
                    native_active_session_id = native_lease.native_session_id
                    native_usable = True
                    native_recreated = native_lease.recreated
                    logger.info(
                        "[CLILLMClient] CLI native session %s (%s)",
                        "resumed" if native_lease.action == "resume" else "started",
                        mask_native_session_id(native_active_session_id) or "pending",
                    )
                except CLISessionBusyError:
                    raise
                except Exception:
                    # Persistence outages must not take down the existing CLI
                    # chat path. The provider then receives the full context.
                    logger.warning(
                        "[CLILLMClient] Native CLI session state unavailable; using stateless mode",
                        exc_info=True,
                    )
                    native_store = None
                    native_lease = None
                    native_scope = None

            # Bootstrap context contains the complete AoiTalk-side contract.
            # It is sent only for a new/recreated native session or a stateless
            # provider; resumed calls use the delta context below.
            bootstrap_system_context = self._build_system_context(user_input=user_input)
            if max_tokens:
                bootstrap_system_context = (
                    f"{bootstrap_system_context}\n\n"
                    f"Keep the final response within {max(1, int(max_tokens))} tokens."
                )
            project_context_read_block = self._run_project_context_read_before_cli(
                user_input,
                event_callback=event_callback,
            )
            if project_context_read_block:
                bootstrap_system_context = f"{bootstrap_system_context}\n\n{project_context_read_block}"
            tool_hint_context = build_tool_hint_context_sync(
                user_input=user_input,
                registry=filtered_registry_for_client(
                    self,
                    self._tool_registry,
                ),
                policy=get_client_generation_policy(self),
                log_prefix="CLILLMClient",
            )
            prompt = compose_tool_hint_user_message(
                prompt,
                tool_hint_context,
            )
            continuation_system_context = self._build_cli_continuation_context(
                project_context_read_block=project_context_read_block or "",
                tool_hint_context=tool_hint_context or "",
                max_tokens=max_tokens,
            )
            system_context = (
                continuation_system_context
                if native_lease is not None and native_lease.action == "resume"
                else bootstrap_system_context
            )
            snapshot_components = []
            get_all_history = getattr(self.history_manager, "get_all", None)
            history = get_all_history() if callable(get_all_history) else list(getattr(self.history_manager, "history", []) or [])
            if history and not (
                native_lease is not None and native_lease.action == "resume"
            ):
                history_text = "\n".join(str(item.get("content") or "") for item in history)
                snapshot_components.append({**component("conversation_history", "Conversation history", history_text, source="CLI history manager"), "_text": history_text})
            if project_context_read_block:
                snapshot_components.append({**component("project_context", "Project context", project_context_read_block, source="CLI project pre-read"), "_text": project_context_read_block})
            if tool_hint_context and not (
                native_lease is not None and native_lease.action == "resume"
            ):
                snapshot_components.append({**component("tool_hints", "Tool hints", tool_hint_context, source="CLI tool hint context"), "_text": tool_hint_context})
            if image_data:
                snapshot_components.append(component("attachments", "添付ファイル・画像由来の入力", source="CLI image attachment", measurement="unavailable", preview="画像入力（バイナリ・パスは保存しません）"))

            # MCP args (CLI-native delegation)
            mcp_args = (
                self.cli_backend.get_mcp_args(self._mcp_servers)
                if self._mcp_servers and not is_review_generation(self)
                else None
            )

            self._emit_stream_event_sync(
                stream_callback,
                "stream_start",
                {"message": "CLI generation started"},
            )
            self._emit_stream_event_sync(
                stream_callback,
                "status_update",
                {
                    "status": "cli_backend_started",
                    "message": f"{self.cli_backend.get_provider_name()} is running",
                },
            )

            # Execute via CLI: system_context → stdin, prompt → -p
            if max_tokens:
                system_context = (
                    f"{system_context}\n\n# Strict final response budget\n"
                    f"The final response must not exceed {max(1, int(max_tokens))} "
                    "UTF-8 bytes. Keep the answer concise and stop before this limit."
                )
            def _record_native_result() -> None:
                nonlocal native_active_session_id, native_usable
                if native_lease is None or native_store is None or not native_usable:
                    return
                consume_session_id = getattr(
                    self.cli_backend,
                    "consume_last_native_session_id",
                    None,
                )
                observed_id = consume_session_id() if callable(consume_session_id) else None
                if observed_id:
                    native_active_session_id = str(observed_id)
                    try:
                        self._run_async_in_new_loop(
                            native_store.record_native_session(
                                native_lease,
                                native_active_session_id,
                            )
                        )
                    except Exception:
                        logger.warning(
                            "[CLILLMClient] CLI native session ID could not be persisted",
                            exc_info=True,
                        )
                    return
                if not native_active_session_id:
                    # A provider advertised native support but did not return
                    # a stable handle. Never persist a dummy ID.
                    try:
                        self._run_async_in_new_loop(
                            native_store.record_native_session(native_lease, None)
                        )
                    except Exception:
                        logger.warning(
                            "[CLILLMClient] CLI native session unsupported result could not be persisted",
                            exc_info=True,
                        )
                    native_usable = False

            def _execute_cli_invocation(
                invocation_prompt: str,
                invocation_system_context: str,
                *,
                invocation_snapshot_components: Optional[list[dict[str, Any]]] = None,
                initial_invocation: bool = False,
            ) -> tuple[bool, str]:
                nonlocal native_lease, native_scope, native_active_session_id
                nonlocal native_usable, native_recreated
                native_action = "stateless"
                native_mode = "stateless"
                native_kwargs: dict[str, Any] = {}
                if native_lease is not None and native_usable:
                    native_action = "resume" if native_active_session_id else "start"
                    native_mode = "resumed" if native_action == "resume" else "new"
                    native_kwargs = {
                        "native_session_action": native_action,
                        "native_session_id": native_active_session_id,
                        "ephemeral": False,
                    }
                success, output = self._execute_prompt_tracked(
                    prompt=invocation_prompt,
                    cwd=cli_cwd,
                    timeout=self._cli_execution_timeout(user_input),
                    extra_args=mcp_args,
                    system_context=invocation_system_context,
                    event_callback=event_callback,
                    _snapshot_components=invocation_snapshot_components or [],
                    _native_session_mode=native_mode,
                    _native_session_recreated=native_recreated,
                    _native_session_id_for_snapshot=native_active_session_id,
                    **native_kwargs,
                )
                _record_native_result()
                self._cli_native_session_info = {
                    "provider": self.cli_backend.get_provider_name(),
                    "mode": native_mode,
                    "recreated": native_recreated,
                    "native_session_id_masked": mask_native_session_id(
                        native_active_session_id
                    ),
                }
                resume_failed = bool(
                    native_action == "resume"
                    and getattr(
                        self.cli_backend,
                        "was_native_session_resume_failure",
                        lambda: False,
                    )()
                )
                if success or not resume_failed or native_store is None or native_scope is None:
                    return success, output

                # The provider rejected only the old continuation handle. It
                # is safe to invalidate that association and retry the same
                # request once with a complete AoiTalk bootstrap.
                try:
                    self._run_async_in_new_loop(
                        native_store.invalidate(native_lease, reason="native_session_resume_failed")
                    )
                    native_lease = self._run_async_in_new_loop(
                        native_store.acquire(native_scope, force_new=True)
                    )
                    native_active_session_id = None
                    native_recreated = True
                    native_usable = True
                except Exception:
                    logger.warning(
                        "[CLILLMClient] Native session recreation failed; falling back to stateless execution",
                        exc_info=True,
                    )
                    native_lease = None
                    native_scope = None
                    native_usable = False

                retry_prompt = invocation_prompt
                if not initial_invocation:
                    retry_prompt = f"元のユーザー発話:\n{user_input}\n\n{invocation_prompt}"
                retry_mode = "new" if native_usable else "stateless"
                retry_kwargs: dict[str, Any] = {}
                if native_usable:
                    retry_kwargs = {
                        "native_session_action": "start",
                        "native_session_id": None,
                        "ephemeral": False,
                    }
                retry_success, retry_output = self._execute_prompt_tracked(
                    prompt=retry_prompt,
                    cwd=cli_cwd,
                    timeout=self._cli_execution_timeout(user_input),
                    extra_args=mcp_args,
                    system_context=bootstrap_system_context,
                    event_callback=event_callback,
                    _snapshot_components=(
                        invocation_snapshot_components if initial_invocation else []
                    ),
                    _native_session_mode=retry_mode,
                    _native_session_recreated=True,
                    _native_session_id_for_snapshot=None,
                    **retry_kwargs,
                )
                _record_native_result()
                self._cli_native_session_info = {
                    "provider": self.cli_backend.get_provider_name(),
                    "mode": retry_mode,
                    "recreated": True,
                    "native_session_id_masked": mask_native_session_id(
                        native_active_session_id
                    ),
                }
                return retry_success, retry_output

            success, cli_output = _execute_cli_invocation(
                prompt,
                system_context,
                invocation_snapshot_components=snapshot_components,
                initial_invocation=True,
            )

            if cancellation_handle and cancellation_handle.cancel_requested.is_set():
                raise GenerationCancelled("CLI generation cancelled")
            if not success:
                safe_cli_output = self._sanitize_cli_session_error(
                    cli_output,
                    [
                        native_active_session_id,
                        getattr(self.cli_backend, "_active_native_session_id", None),
                    ],
                )
                logger.error(f"[CLILLMClient] CLI execution failed: {safe_cli_output}")
                self._emit_stream_event_sync(
                    stream_callback,
                    "stream_end",
                    {
                        "content": f"CLI error: {safe_cli_output}",
                        "status": "failed",
                        "message": "CLI execution failed",
                        "error": safe_cli_output,
                    },
                )
                if self.config.get("free_team.propagate_errors", False):
                    raise RuntimeError(safe_cli_output or "CLI execution failed")
                return f"エラーが発生しました: {safe_cli_output}"

            turn_result = run_cli_tool_call_loop(
                original_input=user_input,
                initial_output=cli_output,
                registry=filtered_registry_for_client(self, self._tool_registry),
                parse_tool_calls=self.cli_backend.parse_tool_calls,
                execute_follow_up=lambda follow_up: _execute_cli_invocation(
                    follow_up,
                    (
                        continuation_system_context
                        if native_usable
                        else system_context
                    ),
                    invocation_snapshot_components=[],
                    initial_invocation=False,
                ),
                build_follow_up_prompt=(
                    self._build_native_follow_up_prompt
                    if native_usable
                    else self._build_follow_up_prompt
                ),
                log_prefix="CLILLMClient",
                max_rounds=self._cli_tool_round_limit(user_input),
                max_tool_result_chars=self._cli_tool_result_max_chars(),
                config=self.config,
                user_input=user_input,
                event_callback=event_callback,
                final_response_check=combined_final_response_check(
                    user_input=user_input,
                    require_project_context_read=(
                        self._requires_cli_project_context_read(user_input)
                        and not bool(project_context_read_block)
                    ),
                ),
                should_cancel=(
                    cancellation_handle.cancel_requested.is_set
                    if cancellation_handle
                    else None
                ),
            )
            if turn_result.stopped_reason == "cancelled":
                raise GenerationCancelled("CLI generation cancelled")
            tool_loop_stop_reason = turn_result.stopped_reason
            tool_rounds_exhausted = tool_loop_stop_reason == "max_rounds"
            tool_loop_failed = (
                tool_loop_stop_reason not in CLI_TOOL_LOOP_SUCCESS_REASONS
            )
            self._last_turn_tool_rounds_exhausted = tool_rounds_exhausted
            self._last_turn_tool_loop_failed = tool_loop_failed
            audit_results = (
                turn_result.audit_tool_results
                or turn_result.tool_results
            )
            tool_call_records: list[OpenAIToolCallRecord] = [
                OpenAIToolCallRecord(
                    tool=tool_result.call.tool,
                    arguments=dict(tool_result.call.arguments),
                    result=tool_result.model_output,
                )
                for tool_result in audit_results
            ]
            if tool_loop_failed:
                # A failed loop can carry a provider marker from the last
                # attempted round.  Never persist/display that marker: it may
                # describe a tool call which was not executed.
                logger.warning(
                    "[CLILLMClient] CLI tool loop stopped (%s) after %s rounds",
                    tool_loop_stop_reason,
                    turn_result.rounds,
                )
                response = (
                    CLI_TOOL_LOOP_EXHAUSTED_MESSAGE
                    if tool_rounds_exhausted
                    else CLI_TOOL_LOOP_FAILURE_MESSAGE
                )
            else:
                response = guard_tool_execution_claims(
                    turn_result.final_output,
                    tool_call_records,
                )
            if event_callback:
                event_callback(
                    "agentic_review",
                    {
                        "round": 0,
                        "status": "failed" if tool_loop_failed else "done",
                        "reason": (
                            (
                                "CLI tool loop stopped before a final answer; "
                                "the pending tool marker was not executed."
                                f" reason={tool_loop_stop_reason}"
                            )
                            if tool_loop_failed
                            else (
                                "CLI completion was decided from the primary tool loop; "
                                "a second tool-capable CLI verifier was not started."
                            )
                        ),
                        "review_response": "",
                    },
                )
            self._emit_stream_event_sync(
                stream_callback,
                "stream_end",
                {
                    "content": response,
                    **(
                        {
                            "status": "failed",
                            "message": response,
                            "error": (
                                "CLI tool loop exceeded max rounds"
                                if tool_rounds_exhausted
                                else f"CLI tool loop stopped: {tool_loop_stop_reason}"
                            ),
                        }
                        if tool_loop_failed
                        else {}
                    ),
                },
            )

            # Update history
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", response)
            self.check_and_summarize_history()

            # Save to memory (async operations run in a new loop)
            self._save_to_memory(user_input, response)

            logger.info(f"[CLILLMClient] Response generated: {len(response)} chars")
            return response
        finally:
            if native_store is not None and native_lease is not None:
                try:
                    self._run_async_in_new_loop(native_store.release(native_lease))
                except Exception:
                    logger.warning(
                        "[CLILLMClient] CLI native session lease release failed",
                        exc_info=True,
                    )
            reset_current_generation_policy(generation_policy_token)
            reset_current_user_input(tool_policy_token)
            if project_token is not None:
                reset_runtime_project_context(project_token)
            self._current_context_bundle = None
            if image_cleanup is not None:
                image_cleanup()

    def _run_streamed_with_callback(self, *args, **kwargs):
        """Capability marker: CLI clients emit progress through stream_callback."""
        raise NotImplementedError(
            "CLILLMClient uses generate_response(..., stream_callback=...)"
        )

    def _run_agentic_review_once(
        self,
        prompt: str,
        *,
        system_context: str,
        event_callback: Any,
        user_input: str,
    ) -> str:
        success, cli_output = self._execute_prompt_tracked(
            prompt=prompt,
            cwd=self._cli_execution_cwd(user_input),
            timeout=self._cli_execution_timeout(user_input),
            system_context=system_context,
            event_callback=event_callback,
        )
        if not success:
            logger.error(f"[CLILLMClient] agentic review CLI execution failed: {cli_output}")
            return f"エラーが発生しました: {cli_output}"

        turn_result = run_cli_tool_call_loop(
            original_input=user_input,
            initial_output=cli_output,
            registry=filtered_registry_for_client(self, self._tool_registry),
            parse_tool_calls=self.cli_backend.parse_tool_calls,
            execute_follow_up=lambda follow_up: self._execute_prompt_tracked(
                follow_up,
                cwd=self._cli_execution_cwd(user_input),
                timeout=self._cli_execution_timeout(user_input),
                system_context=system_context,
                event_callback=event_callback,
            ),
            build_follow_up_prompt=self._build_follow_up_prompt,
            log_prefix="CLILLMClient",
            max_rounds=self._cli_tool_round_limit(user_input),
            max_tool_result_chars=self._cli_tool_result_max_chars(),
            config=self.config,
            user_input=user_input,
            event_callback=event_callback,
            final_response_check=combined_final_response_check(
                user_input=user_input,
                require_project_context_read=False,
            ),
            should_cancel=(
                get_current_generation_cancellation().cancel_requested.is_set
                if get_current_generation_cancellation()
                else None
            ),
        )
        if turn_result.stopped_reason == "cancelled":
            raise GenerationCancelled("CLI generation cancelled")
        tool_loop_stop_reason = turn_result.stopped_reason
        tool_rounds_exhausted = tool_loop_stop_reason == "max_rounds"
        tool_loop_failed = (
            tool_loop_stop_reason not in CLI_TOOL_LOOP_SUCCESS_REASONS
        )
        self._last_turn_tool_rounds_exhausted = tool_rounds_exhausted
        self._last_turn_tool_loop_failed = tool_loop_failed
        audit_results = (
            turn_result.audit_tool_results
            or turn_result.tool_results
        )
        tool_call_records: list[OpenAIToolCallRecord] = [
            OpenAIToolCallRecord(
                tool=tool_result.call.tool,
                arguments=dict(tool_result.call.arguments),
                result=tool_result.model_output,
            )
            for tool_result in audit_results
        ]
        if tool_loop_failed:
            response = (
                CLI_TOOL_LOOP_EXHAUSTED_MESSAGE
                if tool_rounds_exhausted
                else CLI_TOOL_LOOP_FAILURE_MESSAGE
            )
            if event_callback:
                event_callback(
                    "agentic_review",
                    {
                        "round": 0,
                        "status": "failed",
                        "reason": (
                            "CLI tool loop stopped before a review answer; "
                            "the pending tool marker was not executed."
                            f" reason={tool_loop_stop_reason}"
                        ),
                        "review_response": "",
                    },
                )
            return response
        return guard_tool_execution_claims(turn_result.final_output, tool_call_records)

    def _cli_tool_result_max_chars(self) -> int:
        configured = None
        if hasattr(self.config, "get"):
            configured = self.config.get("cli.tool_result_max_chars", None)
            if configured is None:
                configured = self.config.get("llm_cli.tool_result_max_chars", None)
        try:
            value = int(configured) if configured is not None else DEFAULT_CLI_TOOL_RESULT_MAX_CHARS
        except (TypeError, ValueError):
            value = DEFAULT_CLI_TOOL_RESULT_MAX_CHARS
        return max(1000, min(value, MAX_CLI_TOOL_RESULT_MAX_CHARS))

    @staticmethod
    def _cli_execution_cwd(user_input: str | None) -> Path:
        """Keep trusted workspace-operation turns outside the source repo."""

        # An active AgentRunScope is the authoritative repository boundary.
        # Do not replace it with the historical neutral/temp cwd: the CLI
        # backend must receive the selected root so its WSL+bwrap lane can
        # mount exactly that checkout.
        try:
            from ..security.agent_run_scope import get_current_run_scope

            scoped = get_current_run_scope()
        except Exception:
            scoped = None
        if scoped is not None:
            return Path(scoped.canonical_root)

        if not looks_like_managed_workspace_request(user_input or ""):
            return Path.cwd()
        neutral = Path(tempfile.gettempdir()) / "aoitalk-cli-managed-runtime"
        neutral.mkdir(parents=True, exist_ok=True)
        return neutral

    @staticmethod
    def _cli_plain_text_cwd() -> Path:
        """Use a neutral directory for tool-free provider generations."""

        try:
            from ..security.agent_run_scope import get_current_run_scope

            scoped = get_current_run_scope()
        except Exception:
            scoped = None
        if scoped is not None:
            return Path(scoped.canonical_root)

        neutral = Path(tempfile.gettempdir()) / "aoitalk-cli-plain-runtime"
        neutral.mkdir(parents=True, exist_ok=True)
        return neutral

    def _cli_plain_text_timeout(self) -> int:
        """Bound provider calls so a hung CLI cannot hold ingest forever."""
        configured = None
        config = getattr(self, "config", None)
        if hasattr(config, "get"):
            configured = config.get("llm_cli.plain_text_timeout_seconds", None)
        try:
            value = int(configured) if configured is not None else 120
        except (TypeError, ValueError):
            value = 120
        return max(15, min(value, 600))

    def _cli_execution_timeout(self, user_input: str | None) -> int | None:
        if not looks_like_managed_workspace_request(user_input or ""):
            return None
        try:
            value = int(self.config.get("llm_cli.managed_workspace_timeout_seconds", 120))
        except (TypeError, ValueError):
            value = 120
        return max(15, min(value, 600))

    def _cli_tool_round_limit(self, user_input: str | None) -> int:
        """Resolve a bounded CLI tool budget for the active generation profile.

        ``agentic_completion.*_max_rounds`` is the API/native runtime budget
        and may be 120.  Applying that value directly to a CLI backend would
        start an unbounded sequence of provider processes, so CLI keeps a
        separate, smaller profile budget.  The legacy managed-workspace
        setting remains the fallback only when a trusted workspace capability
        or verified attachment metadata is active.
        """

        policy = get_client_generation_policy(self)
        profile = getattr(policy, "profile", GenerationProfile.CHAT)
        profile_name = getattr(profile, "value", str(profile)).strip().lower()
        if profile_name == GenerationProfile.REVIEW.value:
            default = 2
        elif profile_name in {
            GenerationProfile.ASSISTED_WORK.value,
            GenerationProfile.AUTONOMOUS_WORK.value,
        }:
            default = DEFAULT_CLI_WORK_TOOL_ROUND_LIMIT
        else:
            default = DEFAULT_CLI_TOOL_ROUND_LIMIT

        configured = None
        config = getattr(self, "config", None)
        if hasattr(config, "get"):
            configured = config.get(
                f"llm_cli.{profile_name}_max_tool_rounds",
                None,
            )
            # Reuse the existing generation-profile setting when no CLI-only
            # override is present, but still apply the strict CLI cap below.
            # This keeps a customized profile budget effective without ever
            # importing the native 120-round ceiling verbatim.
            if configured is None and profile_name in {
                GenerationProfile.ASSISTED_WORK.value,
                GenerationProfile.AUTONOMOUS_WORK.value,
                GenerationProfile.REVIEW.value,
            }:
                configured = config.get(
                    f"agentic_completion.{profile_name}_max_rounds",
                    None,
                )
            # Keep the old chat/workspace knob compatible for existing users;
            # it is consulted only for trusted workspace-operation turns.
            if (
                configured is None
                and profile_name == GenerationProfile.CHAT.value
                and looks_like_managed_workspace_request(user_input or "")
            ):
                configured = config.get(
                    "llm_cli.managed_workspace_max_tool_rounds",
                    None,
                )
            if configured is None:
                configured = config.get("llm_cli.max_tool_rounds", None)
        try:
            value = int(configured) if configured is not None else default
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, MAX_CLI_TOOL_ROUND_LIMIT))

    def _should_include_cli_project_context(self, user_input: str | None) -> bool:
        return project_context_enabled_for_client(self, default=False)

    def _requires_cli_project_context_read(self, user_input: str | None) -> bool:
        return (
            project_context_enabled_for_client(self, default=False)
            and bool(self.current_project_id or self.current_session_id)
        )

    def _run_project_context_read_before_cli(
        self,
        user_input: str | None,
        *,
        event_callback: Any,
    ) -> str:
        if not self._requires_cli_project_context_read(user_input):
            return ""

        for tool_name in project_context_required_read_tool_names(self._tool_registry):
            args: dict[str, Any] = {}
            if tool_name == "list_project_information" and self.current_project_id:
                args["project_id"] = str(self.current_project_id)

            self._emit_cli_tool_event(
                event_callback,
                "tool_start",
                tool_name=tool_name,
                args=args,
                message=f"Running {tool_name}",
            )
            try:
                result = self._tool_registry.execute(tool_name, **args)
            except Exception as exc:
                self._emit_cli_tool_event(
                    event_callback,
                    "tool_end",
                    tool_name=tool_name,
                    args=args,
                    message=f"Failed {tool_name}",
                    output="",
                    error=str(exc),
                )
                logger.warning(
                    "[CLILLMClient] project context pre-read failed: %s: %s",
                    tool_name,
                    exc,
                )
                continue

            output = str(result)
            self._emit_cli_tool_event(
                event_callback,
                "tool_end",
                tool_name=tool_name,
                args=args,
                message=f"Completed {tool_name}",
                output=output,
                error="",
            )
            clipped = clip_text(output, self._cli_tool_result_max_chars())
            return (
                "## Project Context DB Read Result\n"
                f"- `{tool_name}` was executed before the model response because "
                "Project context is enabled.\n"
                "- Treat this tool result as grounding evidence. Decide normally "
                "whether additional reads or project DB mutation tools are needed.\n\n"
                f"```text\n{clipped}\n```"
            )

        return ""

    def _emit_cli_tool_event(
        self,
        event_callback: Any,
        event_type: str,
        *,
        tool_name: str,
        args: dict[str, Any],
        message: str,
        output: str | None = None,
        error: str = "",
    ) -> None:
        if not event_callback:
            return
        payload: dict[str, Any] = {
            "tool": tool_name,
            "tool_args": dict(args),
            "message": message,
        }
        if event_type == "tool_end":
            payload["tool_result"] = {
                "tool": tool_name,
                "arguments": dict(args),
                "output": output or "",
                "error": error,
            }
        try:
            event_callback(event_type, payload)
        except GenerationInterrupted:
            raise
        except Exception:
            logger.debug(
                "[CLILLMClient] project context pre-read event callback failed",
                exc_info=True,
            )

    def _run_reasoning(self, user_input: str) -> str:
        """Run reasoning mode synchronously"""
        cancellation_handle = get_current_generation_cancellation()
        if cancellation_handle and cancellation_handle.cancel_requested.is_set():
            raise GenerationCancelled("CLI generation cancelled")
        context_bundle = self._context_bundle_for_turn()
        context = {
            'available_tools': self._get_available_tools(),
            'conversation_history': self.history_manager.get_all(),
            'character_name': self.character_name,
            'project_context': sanitize_project_context_for_chat(
                get_runtime_project_context()
            ) if self._should_include_cli_project_context(user_input) else None,
            'runtime_context': (
                context_bundle.render_for_prompt()
                if context_bundle
                else ""
            ),
        }
        return self._run_async_in_new_loop(
            self.reasoning_manager.execute_reasoning_mode(
                user_input=user_input,
                context=context,
            )
        )

    def _context_bundle_for_turn(self) -> ContextBundle | None:
        """Hide stale selected-Project layers when Project Context is OFF."""

        bundle = getattr(self, "_current_context_bundle", None)
        if bundle is None or project_context_enabled_for_client(self):
            return bundle
        return replace(
            bundle,
            project_context_block="",
            project_information_block="",
            agent_memory_block="",
            project_pack_block="",
            task_context_block="",
        )

    def _run_async_in_new_loop(self, coro):
        """Run a coroutine in a new event loop (thread-safe)"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def cancellation_aware_result():
            task = asyncio.create_task(coro)
            while not task.done():
                await asyncio.wait({task}, timeout=0.1)
                cancellation_handle = get_current_generation_cancellation()
                if (
                    cancellation_handle
                    and cancellation_handle.cancel_requested.is_set()
                    and not task.done()
                ):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise GenerationCancelled("CLI generation cancelled")
                if (
                    cancellation_handle
                    and cancellation_handle.interrupt_requested.is_set()
                    and not task.done()
                ):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise_if_generation_interrupted()
            return await task

        try:
            return loop.run_until_complete(cancellation_aware_result())
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    def _make_stream_event_callback(self, stream_callback: Any):
        if stream_callback is None:
            return None

        def _callback(event_type: str, data: dict[str, Any]) -> None:
            if event_type == "assistant_text":
                assistant_text = str(
                    data.get("text")
                    or data.get("content")
                    or data.get("message")
                    or ""
                )
                if "[tool_call:" in assistant_text.casefold():
                    # Codex/other CLI stream handlers may expose an
                    # intermediate agent message before the provider's tool
                    # item is executed.  That marker is an instruction, not
                    # user-facing text, so never forward it as assistant_text.
                    logger.warning(
                        "[CLILLMClient] Suppressed pending CLI tool marker from assistant_text"
                    )
                    return
            cancellation_handle = get_current_generation_cancellation()
            if cancellation_handle and cancellation_handle.cancel_requested.is_set():
                return
            raise_if_generation_interrupted()
            self._emit_stream_event_sync(stream_callback, event_type, data)

        return _callback

    def _emit_stream_event_sync(
        self,
        stream_callback: Any,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        if stream_callback is None:
            return
        try:
            result = stream_callback(event_type, data)
            if inspect.isawaitable(result):
                try:
                    running_loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(result)
                else:
                    running_loop.create_task(result)
        except GenerationInterrupted:
            raise
        except Exception:
            logger.debug(
                "[CLILLMClient] Stream callback failed for %s",
                event_type,
                exc_info=True,
            )

    def _save_to_memory(self, user_input: str, response: str):
        """Save conversation to memory manager (runs async in background)"""
        if getattr(self, "external_persistence_enabled", False):
            return
        if not self.memory_manager:
            return

        try:
            user_id = self.session_user_id
            character_name = self.character_name

            async def _save():
                if not self.memory_manager.is_initialized():
                    await self.memory_manager.initialize()
                if self.current_session_id:
                    await self.memory_manager.add_message_to_session(
                        session_id=self.current_session_id,
                        role="user",
                        content=user_input,
                        metadata=self.session_metadata.copy(),
                        branch_from_message_id=self.current_edit_message_id,
                    )
                    await self.memory_manager.add_message_to_session(
                        session_id=self.current_session_id,
                        role="assistant",
                        content=response,
                    )
                else:
                    await self.memory_manager.add_message(
                        user_id=user_id,
                        character_name=character_name,
                        role="user",
                        content=user_input,
                        metadata=self.session_metadata.copy(),
                        llm_client=self,
                    )
                    await self.memory_manager.add_message(
                        user_id=user_id,
                        character_name=character_name,
                        role="assistant",
                        content=response,
                        llm_client=self,
                    )

            self._run_async_in_new_loop(_save())
        except Exception as e:
            logger.warning(f"[CLILLMClient] Failed to save memory: {e}")

    # ------------------------------------------------------------------
    # Response generation (async)
    # ------------------------------------------------------------------

    async def generate_response_async(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> str:
        from ..services.session_llm_generation import run_session_aware_generation

        return await run_session_aware_generation(
            self,
            # Lightweight/internal clients (and cancellation tests) may be
            # constructed without the full config object.  The session-aware
            # dispatcher already treats a missing config as no overrides;
            # avoid turning that valid direct-generation path into an
            # AttributeError before the provider is invoked.
            getattr(self, "config", None),
            user_input,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    async def _generate_response_async_impl(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> str:
        cancellation_handle = get_current_generation_cancellation()
        if cancellation_handle is not None:
            cancellation_handle.mark_worker_started()
        worker = asyncio.create_task(
            asyncio.to_thread(
                self.generate_response,
                user_input,
                temperature,
                max_tokens,
                stream=False,
                **kwargs,
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            if cancellation_handle is not None:
                cancellation_handle.cancel_requested.set()
            try:
                await worker
            except GenerationCancelled:
                pass
            except Exception:
                logger.debug(
                    "[CLILLMClient] Worker failed while cancellation was draining",
                    exc_info=True,
                )
            raise
        finally:
            if cancellation_handle is not None:
                cancellation_handle.mark_worker_completed()

    async def generate_plain_text_async(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate one tool-free CLI response without the chat/tool loop.

        Docs clip planning expects a machine-readable JSON response.  Sending
        that prompt through ``generate_response_async`` adds chat history,
        tool contracts, and the follow-up tool loop, which can wrap or replace
        the JSON.  This path still uses the normal tracked CLI invocation so
        usage/snapshots are preserved, but it supplies only a strict system
        instruction and never runs tool-call parsing.
        """
        system = system_prompt or (
            "You are a machine-readable AoiTalk workflow helper. Follow the "
            "user instruction exactly. Do not call tools, do not add an "
            "explanation or Markdown fences, and return only the requested "
            "JSON object."
        )
        # Check before creating a worker so a protected/local-only call cannot
        # even enter the CLI execution queue.  The tracked seam repeats this
        # guard for direct/internal callers.
        self._ensure_cli_privacy_direct()
        prompt_text = str(prompt or "")
        provider_name = str(self.cli_backend.get_provider_name() or "").lower()
        is_codex = "codex" in provider_name

        def invoke(model_override: object = _PLAIN_TEXT_MODEL_OVERRIDE_UNSET):
            user_input_token = set_current_user_input(prompt_text)
            generation_policy_token = set_current_generation_policy(
                get_client_generation_policy(self)
            )
            try:
                kwargs: dict[str, Any] = {
                    "prompt": prompt_text,
                    "cwd": self._cli_plain_text_cwd(),
                    "timeout": self._cli_plain_text_timeout(),
                    # Plain generation must not inherit configured MCP servers.
                    "extra_args": [],
                    "system_context": system,
                    "event_callback": None,
                    "_snapshot_components": [],
                    "_native_session_mode": "stateless",
                }
                if model_override is not _PLAIN_TEXT_MODEL_OVERRIDE_UNSET:
                    # Only CodexCLIBackend implements this optional keyword.
                    kwargs["model_override"] = model_override
                    if model_override is None:
                        # The account default is intentionally different from
                        # the configured model; avoid recording a false model
                        # name in the request snapshot/usage record.
                        kwargs["_snapshot_model"] = "codex-default"
                if is_codex:
                    kwargs["disable_native_tools"] = True
                return self._execute_prompt_tracked(**kwargs)
            finally:
                reset_current_generation_policy(generation_policy_token)
                reset_current_user_input(user_input_token)

        def _run_locked() -> str:
            self._last_context_snapshots = []
            self._last_cli_usage = {}
            self._agent_run_usage = {}
            success, output = invoke()
            if not success and is_codex and _is_codex_model_unavailable(output):
                logger.warning(
                    "[CLILLMClient] Codex model is unavailable for the current "
                    "account; retrying plain generation with the CLI default"
                )
                success, output = invoke(None)
            if not success:
                raise RuntimeError(str(output or "CLI plain-text generation failed"))
            result = str(output or "").strip()
            if not result:
                raise RuntimeError("CLI plain-text generation returned no output")
            return result

        def run() -> str:
            deadline = time.monotonic() + self._cli_plain_text_timeout()
            if not self._plain_text_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise RuntimeError(
                    "CLI plain-text generation timed out waiting for another request"
                )
            execution_lock = self._get_cli_execution_lock()
            try:
                if not execution_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                    raise RuntimeError(
                        "CLI plain-text generation timed out waiting for another request"
                    )
                try:
                    return _run_locked()
                finally:
                    execution_lock.release()
            finally:
                self._plain_text_lock.release()

        cancellation_handle = get_current_generation_cancellation()
        if cancellation_handle is not None:
            cancellation_handle.mark_worker_started()
        worker = asyncio.create_task(asyncio.to_thread(run))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            if cancellation_handle is not None:
                cancellation_handle.cancel_requested.set()
            try:
                await worker
            except GenerationCancelled:
                pass
            except Exception:
                logger.debug(
                    "[CLILLMClient] Plain-text worker failed while cancellation was draining",
                    exc_info=True,
                )
            raise
        finally:
            if cancellation_handle is not None:
                cancellation_handle.mark_worker_completed()

    def generate(self, prompt: str) -> str:
        """Generate a synchronous response for reasoning workflows."""
        return self.generate_response(prompt, stream=False)

    async def generate_async(self, prompt: str) -> str:
        """Generate an async response for reasoning workflows."""
        return await self.generate_response_async(prompt)

    # ------------------------------------------------------------------
    # MCP (CLIネイティブ委譲)
    # ------------------------------------------------------------------

    def is_mcp_available(self) -> bool:
        """MCP availability check

        CLI backends delegate MCP to the CLI tool itself.
        Returns True if MCP servers are configured and the backend supports
        runtime MCP args (currently only Claude Code).
        """
        return bool(self._mcp_servers) and (
            bool(self.cli_backend.get_mcp_args(self._mcp_servers))
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup(self):
        if self.memory_manager:
            try:
                await self.memory_manager.cleanup()
                logger.info("[CLILLMClient] Memory manager cleaned up")
            except Exception as e:
                logger.warning(f"[CLILLMClient] Memory cleanup error: {e}")

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def _build_system_context(self, user_input: str | None = None) -> str:
        """Build system context for stdin (instructions + history + tools)

        システムプロンプト・会話履歴・ツール情報をまとめて返す。
        各CLI backendが対応する入力形式へ変換して渡す。
        """
        parts = []
        story_chat_context = self._get_story_chat_context_sync()

        # System instructions (セクションヘッダなし — Geminiが誤解しないように)
        if story_chat_context:
            parts.append(story_chat_context.prompt)
        elif self.custom_system_prompt:
            parts.append(self.custom_system_prompt)
        else:
            instructions = build_unified_instructions(
                character_name=self.character_name,
                config=self.config,
                include_mcp_info=False,
                include_static_tool_reference=False,
                custom_instructions=get_user_custom_instructions_sync(
                    self.session_user_id
                ),
            )
            parts.append(instructions)

        context_bundle = self._context_bundle_for_turn()
        context_builder_block = (
            context_bundle.render_for_prompt()
            if not story_chat_context and context_bundle
            else ""
        )
        if context_builder_block:
            parts.append(f"\n{context_builder_block}")

        include_project_context = self._should_include_cli_project_context(user_input)
        project_context = (
            None
            if story_chat_context or not include_project_context
            else get_runtime_project_context()
        )
        if project_context and not context_builder_block:
            parts.append(
                f"\n{format_minimal_project_context_for_chat_prompt(project_context)}"
            )

        # Conversation history
        context_text = self.history_manager.get_context_as_text()
        if context_text:
            parts.append(f"\n会話履歴:\n{context_text}")

        if story_chat_context:
            # シナリオ許可ツールは全て deferred pack 所属なので、
            # 絞り込み前に pack を自動ロードしないとツール0件になる。
            apply_story_pack_auto_load(self, story_chat_context)
            story_tools = [
                tool
                for tool in filter_tools_for_client(
                    self,
                    self._tool_registry.get_all(),
                )
                if is_story_workflow_tool_allowed(
                    tool.name,
                    story_chat_context,
                )
            ]
            if story_tools:
                tool_prompt = CLIAdapter.to_prompt_text(story_tools)
                parts.append(f"\nAvailable scenario tools:\n{tool_prompt}")
            parts.append(
                "\n---\n"
                "Respond according to the dedicated scenario workflow instructions above."
            )
            return "\n".join(parts)

        # Tool information
        tool_prompt = build_cli_tool_context(
            user_input=user_input,
            registry=filtered_registry_for_client(self, self._tool_registry),
            force_project_tools=include_project_context,
            loaded_pack_ids=effective_tool_pack_session(self).loaded,
        )
        if tool_prompt:
            parts.append(f"\n利用可能なツール:\n{tool_prompt}")

        # Final response instruction for the current user utterance.
        parts.append(
            "\n---\n"
            "以下のユーザー発話に、キャラクターとして直接答えてください。"
        )

        parts.append(self._build_tool_execution_contract_prompt())
        return "\n".join(parts)

    def _build_tool_execution_contract_prompt(self) -> str:
        return "\n".join(
            [
                "\nツール実行形式:",
                "- ツールが必要な場合は通常回答ではなく `[TOOL_CALL: tool_name(key=value)]` 形式で出力してください。",
                "- 引数が不要な場合は `[TOOL_CALL: tool_name()]` 形式で出力してください。",
                "- 外部状態を確認・変更した事実は、ツール結果だけを根拠にしてください。",
                "- 追加確認が必要な場合は、最終回答に進まず次のツール呼び出しを出力してください。",
            ]
        )

    def _build_prompt_with_tools(self, user_input: str) -> str:
        """Build combined prompt (fallback for non-stdin backends)"""
        system_context = self._build_system_context(user_input=user_input)
        return f"{system_context}\n\nUser: {user_input}\nAssistant:"

    def _build_follow_up_prompt(
        self, original_input: str, initial_response: str, tool_results_text: str
    ) -> str:
        parts = [
            "# ツール実行結果",
        ]
        story_chat_context = self._get_story_chat_context_sync()
        if story_chat_context:
            parts.extend([story_chat_context.prompt, ""])
        task_instruction = (
            "上記のツール結果に基づいて、シナリオ進行の応答を続けてください。"
            if story_chat_context
            else "上記のツール結果に基づいて、ユーザーに自然に回答してください。"
        )
        final_reminder = (
            "専用シナリオ進行指示に従ってください。"
            if story_chat_context
            else f"キャラクターとしての応答を維持してください: {self.character_name}"
        )
        parts.extend([
            "ツール結果を受け取った後は、まず元のユーザー要求がすでに満たされたかを判定してください。",
            "満たされている場合は、追加の `[TOOL_CALL: ...]` を出さず、直ちに最終回答してください。",
            "追加の `[TOOL_CALL: tool_name(key=value)]` が許されるのは、直前の結果がエラー、空、不完全、不整合、古い、元の要求に未完了の別サブタスクが残っている、直前の結果で新しい確認対象が発生した、または変更系作業で変更後の検証が未完了の場合だけです。",
            "「念のため」「もう一度確認」だけを理由に、成功したツール結果を再確認しないでください。",
            "同じツールの再実行は、変更後検証、変更されたファイルの確認、エラー解消、未完了サブタスクの処理など、具体的な新しい理由がある場合だけ許可されます。",
            "",
            "元のユーザー発話:",
            original_input,
            "",
            "直前の出力:",
            initial_response,
            "",
            "ツール結果:",
            tool_results_text,
            "",
            "# 最終回答",
            task_instruction,
            final_reminder,
            "\nAssistant:",
        ])
        return "\n".join(parts)

    def _build_context_bundle_sync(
        self, user_input: str, project_context: Optional[dict[str, Any]]
    ) -> Optional[ContextBundle]:
        if self._get_story_chat_context_sync():
            return None
        include_project_context = self._should_include_cli_project_context(user_input)
        try:
            return self._run_async_in_new_loop(
                ContextBuilder().build_context(
                    user_id=self._get_session_user_id(),
                    message=user_input,
                    project_id=self.current_project_id if include_project_context else None,
                    task_id=get_turn_context().task_id,
                    session_id=self.current_session_id,
                    project_context=project_context if include_project_context else None,
                    include_project_context=include_project_context,
                    include_project_information=False,
                    include_project_pack=False,
                    include_task_context=False,
                    project_context_mode="minimal",
                )
            )
        except Exception as e:
            logger.warning(
                f"[CLILLMClient] ContextBuilder failed; fallback to basic context: {e}"
            )
            return None

    def _resolve_project_context_sync(self) -> Optional[dict[str, Any]]:
        if not self.current_project_id and not self.current_session_id:
            return None

        if self._get_story_chat_context_sync():
            return None

        resolver = ProjectContextResolver()
        try:
            return self._run_async_in_new_loop(
                resolver.resolve_context(
                    project_id=self.current_project_id,
                    session_id=self.current_session_id,
                    user_id=self._get_session_user_id(),
                )
            )
        except Exception as e:
            logger.warning(f"[CLILLMClient] Failed to resolve project context: {e}")
            return None

    def _get_story_chat_context_sync(self):
        if not self.current_session_id:
            return None
        return run_story_chat_context_sync(
            self._run_async_in_new_loop,
            self.current_session_id,
        )

    def _get_available_tools(self) -> List[str]:
        story_chat_context = self._get_story_chat_context_sync()
        if not story_chat_context:
            return filter_tools_for_client(self, self._tool_registry.get_names())
        apply_story_pack_auto_load(self, story_chat_context)
        return [
            name
            for name in filter_tools_for_client(
                self,
                self._tool_registry.get_names(),
            )
            if is_story_workflow_tool_allowed(name, story_chat_context)
        ]
