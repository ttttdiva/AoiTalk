"""AgentLLMClient のプロンプト/コンテキスト構築 Mixin。

manager.py から責務分割したもの。メソッド本体のロジックは一切変更していない。
"""

import dataclasses
from typing import Any, Optional

from ..conversation_context import PromptMessages, build_prompt_messages
from ..multimodal import openai_content_parts
from ...services.context_builder import ContextBuilder, ContextBundle
from ...services.project_context import (
    ProjectContextResolver,
    format_project_context_for_chat_prompt,
    get_runtime_project_context,
)
from ...services.story_chat_context import (
    build_story_chat_context,
    resolve_story_chat_context_for_chat,
    run_story_chat_context_sync,
)
from ...services.turn_context import get_turn_context


class ContextBuildingMixin:
    """モデル入力メッセージ・会話コンテキスト・プロジェクトコンテキストの構築。"""

    def _project_context_enabled_for_turn(self) -> bool:
        """Prefer the immutable turn-local Project Context flag.

        Provider clients retain ``current_include_project_context`` for legacy
        and direct CLI/voice callers, but a request-scoped ContextVar must win
        whenever the Web/REST boundary supplied an explicit ON/OFF value.
        """

        turn = get_turn_context()
        if turn.include_project_context is not None:
            return bool(turn.include_project_context)
        return bool(getattr(self, "current_include_project_context", True))

    def _build_model_prompt_messages(
        self,
        user_input: str,
        *,
        tool_hint_context: str = "",
        memory_recall: str = "",
        project_context: Optional[dict[str, Any]] = None,
    ) -> PromptMessages:
        """Build canonical role messages and append turn-local context."""
        self._current_memory_recall_context = str(memory_recall or "")
        self._current_tool_hint_context = str(tool_hint_context or "")
        history = self.history_manager.get_model_messages()
        state_mode_before = self._provider_state_mode
        if (
            history
            and history[-1].get("role") == "user"
            and str(history[-1].get("content") or "") == user_input
        ):
            history = history[:-1]

        include_project_context = self._project_context_enabled_for_turn()
        dynamic: list[tuple[str, str]] = []
        if self._current_context_bundle and not self._get_story_chat_context_sync():
            bundle = self._context_bundle_for_turn(include_project_context)
            if self.history_manager.summary and getattr(bundle, "session_context_block", ""):
                bundle = dataclasses.replace(bundle, session_context_block="")
            dynamic.append(
                ("Current ContextBundle", bundle.render_for_prompt())
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
        self._current_prompt_dynamic_context = list(dynamic)
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
        story_chat_context = self._get_story_chat_context_sync()
        include_project_context = self._project_context_enabled_for_turn()
        current_bundle = self._context_bundle_for_turn(include_project_context)
        context_builder_block = (
            current_bundle.render_for_prompt()
            if not story_chat_context and current_bundle
            else ""
        )
        project_context = (
            None
            if story_chat_context or not include_project_context
            else get_runtime_project_context()
        )
        project_block = (
            format_project_context_for_chat_prompt(project_context)
            if project_context and not context_builder_block
            else ""
        )

        # Story workflow sessions use story_chat_context.prompt as agent
        # instructions, not as ordinary conversation context. TRPG play state
        # is intentionally no longer loaded here (§11.8).
        story_block = ""

        # ワールドブック情報の取得
        worldbook_block = ""
        if self.character_name and not story_chat_context:
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
                    story_block,
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
                    story_block,
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
                story_block,
                worldbook_block,
                context,
            ]
            if p
        ]
        return "\n\n".join(parts)

    def _context_bundle_for_turn(self, include_project_context: bool) -> ContextBundle:
        """Strip selected-Project layers when Project Context is explicitly OFF."""

        bundle = self._current_context_bundle
        if bundle is None or include_project_context:
            return bundle or ContextBundle()
        # Keep user/session-scoped context, but never leak a retained selected
        # Project's identity, Docs, pack, tasks, or Agent Memory into this
        # turn through a stale/direct-client ContextBundle fallback.
        return dataclasses.replace(
            bundle,
            project_context_block="",
            project_knowledge_index=None,
            project_information_block="",
            agent_memory_block="",
            project_pack_block="",
            task_context_block="",
        )

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

    def _get_story_chat_context_sync(self):
        if not self.current_session_id:
            return None
        return run_story_chat_context_sync(self._run_sync, self.current_session_id)

    async def _build_context_bundle_for_prompt(
        self, user_input: str, project_context: Optional[dict[str, Any]]
    ) -> Optional[ContextBundle]:
        if self._get_story_chat_context_sync():
            return None
        # Project Context is controlled by the immutable turn flag (or the
        # provider compatibility flag), not by natural-language search words.
        include_project_context = self._project_context_enabled_for_turn()
        turn_task_id = get_turn_context().task_id
        try:
            return await ContextBuilder().build_context(
                user_id=self._get_session_user_id(),
                message=user_input,
                project_id=self.current_project_id if include_project_context else None,
                task_id=turn_task_id,
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
                resolution = await resolve_story_chat_context_for_chat(
                    self.current_session_id
                )
                if resolution.has_writing_session:
                    return None
            except Exception as e:
                print(f"[AgentLLMClient] Failed to resolve scenario chat context: {e}")

        resolver = ProjectContextResolver()
        try:
            context = await resolver.resolve_context(
                project_id=self.current_project_id,
                session_id=self.current_session_id,
                user_id=self._get_session_user_id(),
            )
            if context is not None:
                # Tool/service authorization uses this server-resolved identity;
                # it is not exposed by sanitize_project_context_for_chat().
                context["user_id"] = self._get_session_user_id()
            return context
        except Exception as e:
            print(f"[AgentLLMClient] Failed to resolve project context: {e}")
            return None
