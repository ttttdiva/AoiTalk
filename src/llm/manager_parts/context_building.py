"""AgentLLMClient のプロンプト/コンテキスト構築 Mixin。

manager.py から責務分割したもの。メソッド本体のロジックは一切変更していない。
"""

import dataclasses
from typing import Any, Optional

from ..conversation_context import PromptMessages, build_prompt_messages
from ..multimodal import openai_content_parts
from ..tool_policy import looks_like_bare_search_followup_request
from ...services.context_builder import ContextBuilder, ContextBundle
from ...services.project_context import (
    ProjectContextResolver,
    format_project_context_for_chat_prompt,
    get_runtime_project_context,
)
from ...services.scenario_chat_context import build_scenario_chat_context


class ContextBuildingMixin:
    """モデル入力メッセージ・会話コンテキスト・プロジェクトコンテキストの構築。"""

    def _build_model_prompt_messages(
        self,
        user_input: str,
        *,
        tool_hint_context: str = "",
        memory_recall: str = "",
        project_context: Optional[dict[str, Any]] = None,
    ) -> PromptMessages:
        """Build canonical role messages and append turn-local context."""
        history = self.history_manager.get_model_messages()
        state_mode_before = self._provider_state_mode
        if (
            history
            and history[-1].get("role") == "user"
            and str(history[-1].get("content") or "") == user_input
        ):
            history = history[:-1]

        dynamic: list[tuple[str, str]] = []
        if self._current_context_bundle and not self._get_scenario_chat_context_sync():
            bundle = self._current_context_bundle
            if self.history_manager.summary and getattr(bundle, "session_context_block", ""):
                bundle = dataclasses.replace(bundle, session_context_block="")
            dynamic.append(
                ("Current ContextBundle", bundle.render_for_prompt())
            )
        include_project_context = bool(
            getattr(self, "current_include_project_context", True)
        )
        if include_project_context and project_context and not self._current_context_bundle:
            dynamic.append(
                (
                    "Current Project Context",
                    format_project_context_for_chat_prompt(project_context),
                )
            )
        if memory_recall:
            dynamic.append(("Current memory search results", memory_recall))
        if tool_hint_context:
            dynamic.append(("Current tool hints", tool_hint_context))
        return build_prompt_messages(
            history,
            summary=self.history_manager.summary,
            current_user_input=user_input,
            dynamic_context=dynamic,
        )

    def _add_image_to_prompt_messages(
        self,
        messages: PromptMessages,
        image_data: Optional[dict[str, Any]],
    ) -> PromptMessages:
        if not image_data or not messages:
            return messages
        updated = PromptMessages(dict(item) for item in messages)
        current = updated[-1]
        current["content"] = openai_content_parts(
            str(current.get("content") or ""), image_data
        )
        return updated

    def _build_conversation_context(self) -> str:
        """Build conversation context from history"""
        history = self.history_manager.get_all()
        scenario_chat_context = self._get_scenario_chat_context_sync()
        context_builder_block = (
            self._current_context_bundle.render_for_prompt()
            if not scenario_chat_context and self._current_context_bundle
            else ""
        )
        include_project_context = bool(
            getattr(self, "current_include_project_context", True)
        )
        project_context = (
            None
            if scenario_chat_context or not include_project_context
            else get_runtime_project_context()
        )
        project_block = (
            format_project_context_for_chat_prompt(project_context)
            if project_context and not context_builder_block
            else ""
        )

        # Scenario workflow sessions use scenario_chat_context.prompt as agent
        # instructions, not as ordinary conversation context.
        scenario_block = ""
        if self.current_session_id:
            try:
                from ...services.scenario_service import (
                    get_play_session_by_conversation_id,
                )

                if not scenario_chat_context:
                    play_session = self._run_sync(
                        get_play_session_by_conversation_id(self.current_session_id)
                    )
                else:
                    play_session = None
                if play_session:
                    import json

                    scenario_data = {
                        "scenario_title": play_session.get("scenario", {}).get("title"),
                        "current_scene": play_session.get("current_scene", {}).get(
                            "title"
                        ),
                        "player_state": play_session.get("player_state", {}),
                        "status": play_session.get("status"),
                    }
                    scenario_block = f"## Active TRPG Scenario State:\n{json.dumps(scenario_data, ensure_ascii=False, indent=2)}\n"
            except Exception as e:
                print(f"[AgentLLMClient] Failed to get scenario state: {e}")

        # ワールドブック情報の取得
        worldbook_block = ""
        if self.character_name and not scenario_chat_context:
            try:
                from ...services.worldbook_service import get_matching_entries

                recent_text = (
                    " ".join(msg["content"] for msg in history[-5:]) if history else ""
                )
                entries = self._run_sync(
                    get_matching_entries(self.character_name, recent_text)
                )
                if entries:
                    lines = [
                        (
                            f"### {e['name']}\n{e['content']}"
                            if e.get("name")
                            else e["content"]
                        )
                        for e in entries
                    ]
                    worldbook_block = "## 世界情報:\n" + "\n\n".join(lines)
            except Exception as e:
                print(f"[AgentLLMClient] Failed to get worldbook: {e}")

        if not history:
            parts = [
                p
                for p in [
                    context_builder_block,
                    project_block,
                    scenario_block,
                    worldbook_block,
                ]
                if p
            ]
            return "\n\n".join(parts) if parts else ""

        current_input = history[-1]["content"]

        if len(history) == 1:
            parts = [
                p
                for p in [
                    context_builder_block,
                    project_block,
                    scenario_block,
                    worldbook_block,
                    current_input,
                ]
                if p
            ]
            return "\n\n".join(parts)

        # Get context window size from manager
        context_window = self.history_manager.context_window_size

        # Original logic: history[-11:-1] -> up to 10 items before the last one
        relevant_history = history[-(context_window + 1) : -1]

        context_parts = []
        for msg in relevant_history:
            if msg["role"] == "user":
                context_parts.append(f"ユーザー: {msg['content']}")
            else:
                context_parts.append(f"アシスタント: {msg['content']}")

        if context_parts:
            context = (
                f"過去の会話:\n"
                + "\n".join(context_parts)
                + f"\n\n現在の質問: {current_input}"
            )
        else:
            context = f"現在の質問: {current_input}"

        parts = [
            p
            for p in [
                context_builder_block,
                project_block,
                scenario_block,
                worldbook_block,
                context,
            ]
            if p
        ]
        return "\n\n".join(parts)

    def _run_sync(self, coro):
        """async コルーチンを同期的に実行するヘルパー。"""
        import asyncio
        import concurrent.futures

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()

    def _get_scenario_chat_context_sync(self):
        if not self.current_session_id:
            return None
        try:
            return self._run_sync(build_scenario_chat_context(self.current_session_id))
        except Exception as e:
            print(f"[AgentLLMClient] Failed to resolve scenario chat context: {e}")
            return None

    async def _build_context_bundle_for_prompt(
        self, user_input: str, project_context: Optional[dict[str, Any]]
    ) -> Optional[ContextBundle]:
        if self._get_scenario_chat_context_sync():
            return None
        include_project_context = bool(
            getattr(self, "current_include_project_context", True)
        ) and not looks_like_bare_search_followup_request(user_input)
        try:
            return await ContextBuilder().build_context(
                user_id=self._get_session_user_id(),
                message=user_input,
                project_id=self.current_project_id if include_project_context else None,
                session_id=self.current_session_id,
                project_context=project_context if include_project_context else None,
                include_project_context=include_project_context,
            )
        except Exception as e:
            print(f"[AgentLLMClient] ContextBuilder failed; no memory context injected: {e}")
            return None

    async def _resolve_project_context(self) -> Optional[dict[str, Any]]:
        if not self.current_project_id and not self.current_session_id:
            return None

        if self.current_session_id:
            try:
                if await build_scenario_chat_context(self.current_session_id):
                    return None
            except Exception as e:
                print(f"[AgentLLMClient] Failed to resolve scenario chat context: {e}")

        resolver = ProjectContextResolver()
        try:
            return await resolver.resolve_context(
                project_id=self.current_project_id,
                session_id=self.current_session_id,
            )
        except Exception as e:
            print(f"[AgentLLMClient] Failed to resolve project context: {e}")
            return None
