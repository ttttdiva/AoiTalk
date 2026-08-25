"""AgentLLMClient のキャラクター/エージェント構築・モード管理・ツール解決 Mixin。

manager.py から責務分割したもの。メソッド本体のロジックは一切変更していない。
"""

import logging
from typing import Any, Dict, List, Optional

from ..native_runtime import (
    AgentDefinition as Agent,
    NativeModelSettings as ModelSettings,
    Reasoning,
)
from ..generation_policy import GenerationProfile, get_client_generation_policy
from ..prompts import build_unified_instructions
from ..tool_exposure import (
    REVIEW_TOOL_ALLOWLIST,
    apply_story_pack_auto_load,
    filter_tools_for_client,
    tool_name,
)
from ...services.project_context import (
    get_runtime_project_context,
    project_context_enabled_for_client,
)
from ...services.story_chat_context import is_story_workflow_tool_allowed
from ...services.user_settings_service import get_user_custom_instructions_sync

logger = logging.getLogger(__name__)

class AgentSetupMixin:
    """キャラクターエージェントの生成、システムプロンプト、LLM モード、ツール解決。"""

    def _build_instructions(self) -> str:
        """統一的なシステムプロンプトを生成（共通関数を使用）"""
        # セッションに紐づくRPステアリング設定を取得
        rp_settings = self._get_current_rp_settings()
        project_agents_instructions = None
        project_context = get_runtime_project_context() or {}
        project_id = project_context.get("id")
        # ``runtime_project_context`` is also used by authorization and tool
        # setup.  It must not implicitly publish Project-local AGENTS.md when
        # the current turn explicitly has Project Context OFF.
        # Explicit turn-local state wins; direct provider callers that only
        # set the legacy client flag must also be respected.
        project_context_visible = project_context_enabled_for_client(self)
        if project_id and project_context_visible:
            try:
                from ...services.workspace_agents import load_project_agents_instructions
                project_agents_instructions = load_project_agents_instructions(str(project_id))
            except Exception as exc:
                logger.warning("project AGENTS.md の読み込みに失敗: %s", exc)
        return build_unified_instructions(
            character_name=self.character_name,
            config=self.config,
            include_static_tool_reference=False,
            # AgentLLMClient sends declared function tools through the native
            # provider API.  Never give that path the legacy textual
            # ``[TOOL_CALL: ...]`` contract, even when a route temporarily
            # disables all tools (for example a tool-free free-team model).
            tool_protocol="native",
            rp_settings=rp_settings,
            custom_instructions=get_user_custom_instructions_sync(
                self._get_session_user_id()
            ),
            project_agents_instructions=project_agents_instructions,
        )

    def _get_current_rp_settings(self) -> Optional[dict]:
        """現在のセッションのRPステアリング設定を取得する。"""
        if not self.current_session_id:
            return None
        try:
            from ...memory.conversation_repository import ConversationRepository

            repo = ConversationRepository()
            session = self._run_sync(repo.get_session_by_id(self.current_session_id))
            if session and hasattr(session, "rp_settings"):
                return session.rp_settings or None
        except Exception as e:
            logger.warning(f"RPステアリング設定の取得に失敗: {e}")
        return None

    def _build_effective_instructions(self, story_chat_context=None) -> str:
        override = str(getattr(self, "_system_prompt_override", "") or "").strip()
        if override:
            return override
        if story_chat_context:
            return story_chat_context.prompt
        return self._build_instructions()

    def _create_character_agent(self) -> Agent:
        """Create character agent with tools from the unified runtime registry."""
        base_tools = (
            self._tool_registry.get_all()
            if getattr(self, "_native_tools_enabled", True)
            else []
        )

        # キャラクター名を決定
        if self.config:
            character_config = self.config.get_character_config(self.character_name)
            character_name = character_config.get("name", self.character_name)
        else:
            character_name = "MainAssistant"

        agent_kwargs: Dict[str, Any] = {}
        reasoning_effort = self._get_reasoning_effort()
        if reasoning_effort:
            agent_kwargs["model_settings"] = ModelSettings(
                reasoning=Reasoning(effort=reasoning_effort)
            )

        return Agent(
            name=character_name,
            instructions=self._build_effective_instructions(None),
            tools=base_tools,
            model=self.model_name,
            **agent_kwargs,
        )

    def _get_reasoning_effort(self) -> Optional[str]:
        if self.provider_label not in {"openai", "deepseek", "deepinfra"}:
            return None
        from ...services.llm_model_catalog import reasoning_effort_options_for_model

        config_key = {
            "deepseek": "deepseek.reasoning_effort",
            "deepinfra": "deepinfra.reasoning_effort",
        }.get(self.provider_label, "openai.reasoning_effort")
        model_leaf = str(self.model_name or "").strip().lower().rsplit("/", 1)[-1]
        default_effort = "high" if self.provider_label in {"deepseek", "deepinfra"} else ""
        is_luna_openai = (
            self.provider_label == "openai"
            and model_leaf.startswith("gpt-5.6-luna")
        )
        if is_luna_openai:
            # Luna is the standard OpenAI model.  Treat a missing legacy
            # effort setting as the required max preset instead of silently
            # omitting reasoning from the native Responses request.
            default_effort = "max"
        if not self.config and not is_luna_openai:
            return None
        if self.config and hasattr(self.config, "get"):
            configured_effort = self.config.get(config_key, default_effort)
        elif isinstance(self.config, dict):
            configured_effort = self.config.get(config_key, default_effort)
        else:
            configured_effort = default_effort
        effort = str(configured_effort or default_effort or "").strip()
        if not effort:
            return None
        if effort not in reasoning_effort_options_for_model(
            self.provider_label, self.model_name
        ):
            return None
        return effort

    def _get_effective_tools_for_current_session(self, story_chat_context=None):
        if not getattr(self, "_native_tools_enabled", True):
            return []
        story_chat_context = (
            story_chat_context or self._get_story_chat_context_sync()
        )
        apply_story_pack_auto_load(self, story_chat_context)
        tools = list(self.agent.tools)
        if story_chat_context:
            tools = [
                tool
                for tool in tools
                if is_story_workflow_tool_allowed(
                    self._tool_name(tool),
                    story_chat_context,
                )
            ]
        return self._filter_tools_for_generation_purpose(tools)

    @staticmethod
    def _tool_name(tool: Any) -> str:
        return tool_name(tool)

    def _filter_tools_for_generation_purpose(self, tools):
        """REVIEW では明示済みの読み取り専用ツールだけをモデルへ公開する。"""
        return filter_tools_for_client(self, tools)

    def set_character(self, character_name: str):
        """Set character and recreate main agent

        Args:
            character_name: Name of the character
        """
        previous_character_name = self.character_name
        previous_system_prompt_override = getattr(
            self, "_system_prompt_override", ""
        )
        previous_agent = self.agent
        self.character_name = character_name
        # BaseAssistant は起動時に system prompt override を設定する。
        # これを残したまま agent だけ再生成すると、選択後も旧キャラの
        # prompt が優先され、character_name の切替が実生成へ届かない。
        self._system_prompt_override = ""
        try:
            self.agent = self._create_character_agent()
        except Exception:
            self.character_name = previous_character_name
            self._system_prompt_override = previous_system_prompt_override
            self.agent = previous_agent
            raise

    def update_character(self, yaml_filename: str):
        """Update character from YAML file

        Args:
            yaml_filename: YAML filename (without extension)
        """
        # Load character configuration from YAML
        if self.config:
            new_config = self.config.get_character_config(yaml_filename)
            if new_config:
                previous_character_name = self.character_name
                previous_system_prompt_override = getattr(
                    self, "_system_prompt_override", ""
                )
                previous_agent = self.agent
                self.character_name = new_config.get("name", yaml_filename)
                self._system_prompt_override = ""
                try:
                    new_agent = self._create_character_agent()
                except Exception:
                    self.character_name = previous_character_name
                    self._system_prompt_override = previous_system_prompt_override
                    self.agent = previous_agent
                    raise
                # Clear conversation history when switching characters
                self.clear_history()
                self.agent = new_agent
                print(
                    f"[AgentLLMClient] キャラクター更新: {self.character_name} (会話履歴クリア済み)"
                )
            else:
                print(
                    f"[AgentLLMClient] キャラクター設定が見つかりません: {yaml_filename}"
                )
        else:
            print("[AgentLLMClient] 設定オブジェクトがありません")

    def set_system_prompt(self, prompt: str):
        """Set system prompt by recreating agent with new instructions

        Args:
            prompt: System prompt
        """
        self._system_prompt_override = str(prompt or "").strip()
        self.agent = self._create_character_agent()

    def set_llm_mode(self, mode: str):
        """Set LLM response mode

        Args:
            mode: 'fast' for quick responses, 'thinking' for deeper reasoning

        Note: This is used for response-mode providers such as SGLang/Qwen3,
              and for OpenAI reasoning effort when the active model supports it.
        """
        from ...services.llm_model_catalog import reasoning_effort_options_for_model

        if mode in reasoning_effort_options_for_model("openai", self.model_name):
            self._current_llm_mode = mode
            if self.config:
                try:
                    self.config.set("openai.reasoning_effort", mode)
                except Exception:
                    pass
            self.agent = self._create_character_agent()
            print(f"[AgentLLMClient] Reasoning effort set to: {mode}")
            return

        self._current_llm_mode = mode
        print(f"[AgentLLMClient] LLM mode set to: {mode}")

    def get_llm_mode(self) -> str:
        """Get current LLM response mode

        Returns:
            Current mode ('fast' or 'thinking')
        """
        return getattr(self, "_current_llm_mode", "fast")

    def _get_available_tools(self) -> List[str]:
        """Get list of available tool names

        Returns:
            List of tool names
        """
        if not getattr(self, "_native_tools_enabled", True):
            return []
        story_chat_context = self._get_story_chat_context_sync()
        apply_story_pack_auto_load(self, story_chat_context)
        names = self._tool_registry.get_names()
        if story_chat_context:
            names = [
                name
                for name in names
                if is_story_workflow_tool_allowed(name, story_chat_context)
            ]
        return self._filter_tools_for_generation_purpose(names)
