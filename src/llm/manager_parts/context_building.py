"""AgentLLMClient のプロンプト/コンテキスト構築 Mixin。

manager.py から責務分割したもの。メソッド本体のロジックは一切変更していない。
"""

import copy
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


# ContextBundle is intentionally shared by several providers, while provider
# clients may outlive a schema rollout that adds another project/work layer.
# Keep the OFF-boundary projection in one place and discover fields at runtime
# so an older worker can consume a bundle produced by a newer worker (and vice
# versa) without passing unknown keyword arguments to ``dataclasses.replace``.
_PROJECT_CONTEXT_LAYER_MARKERS = (
    "project_context",
    "project_knowledge",
    "accessible_knowledge",
    "project_information",
    "active_task_context",
    "task_context",
    "task_information",
    "task_knowledge",
    "task_block",
    "work_intelligence",
)


def _is_project_context_layer_field(name: str) -> bool:
    """Return whether a ContextBundle field belongs to the Project scope.

    The ContextBundle schema is deliberately extensible.  New fields should
    include one of the stable layer markers above (for example
    ``work_intelligence_block``); matching by marker lets old providers strip
    them safely when Project Context is OFF instead of leaking a stale bundle.
    Scope identity fields (``project_id``/``task_id``) are not matched here so
    they remain available to authorization/audit code and are never rendered
    by this projection.
    """

    normalized = str(name or "").strip().casefold()
    return bool(normalized) and any(
        marker in normalized for marker in _PROJECT_CONTEXT_LAYER_MARKERS
    )


def strip_project_context_bundle(bundle: Any) -> Any:
    """Return a copy of ``bundle`` with all Project-scoped layers removed.

    User/session memory fields are intentionally preserved.  The helper is
    defensive for migration compatibility: it accepts ``None``, non-dataclass
    test doubles, and ContextBundle instances with fields unknown to this
    worker.  Unknown fields are left untouched unless their name advertises a
    Project/Task/Work-Intelligence layer via ``_is_project_context_layer_field``.
    """

    if bundle is None:
        return bundle

    if not dataclasses.is_dataclass(bundle):
        # A few embedding/test integrations still hand providers a simple
        # namespace instead of ContextBundle.  Copy those objects defensively
        # when possible; never mutate a bundle retained by another turn.
        try:
            projected = copy.copy(bundle)
        except Exception:
            return bundle
        changed = False
        for name in dir(bundle):
            if name.startswith("_") or not _is_project_context_layer_field(name):
                continue
            try:
                current = getattr(bundle, name)
                if isinstance(current, str):
                    value = ""
                elif isinstance(current, tuple):
                    value = ()
                elif isinstance(current, list):
                    value = []
                elif isinstance(current, set):
                    value = set()
                else:
                    value = None
                setattr(projected, name, value)
                changed = True
            except Exception:
                continue
        return projected if changed else bundle

    try:
        bundle_fields = {field.name for field in dataclasses.fields(bundle)}
    except (TypeError, ValueError):
        return bundle

    changes: dict[str, Any] = {}
    for name in bundle_fields:
        if name == "debug" or not _is_project_context_layer_field(name):
            continue
        try:
            current = getattr(bundle, name)
        except Exception:
            continue
        # Blocks are rendered as text; indexes/structured projections should
        # be absent rather than rendered as an empty container.  For an
        # extension field with another shape, retain its container kind while
        # clearing its contents so ``render_with_trace`` cannot see stale data.
        if isinstance(current, str):
            changes[name] = ""
        elif isinstance(current, tuple):
            changes[name] = ()
        elif isinstance(current, list):
            changes[name] = []
        elif isinstance(current, set):
            changes[name] = set()
        elif isinstance(current, dict):
            changes[name] = None
        else:
            changes[name] = None

    # ``render_with_trace`` consults the debug layer map for empty blocks.  A
    # stale map must not report old Project/Task layers as active/deferred, and
    # should not retain provider-visible error payloads for those layers.
    if "debug" in bundle_fields:
        try:
            debug = getattr(bundle, "debug")
        except Exception:
            debug = None
        if isinstance(debug, dict):
            cleaned_debug = dict(debug)
            layers = cleaned_debug.get("layers")
            if isinstance(layers, dict):
                cleaned_debug["layers"] = {
                    key: value
                    for key, value in layers.items()
                    if not _is_project_context_layer_field(str(key))
                }
            errors = cleaned_debug.get("errors")
            if isinstance(errors, dict):
                cleaned_debug["errors"] = {
                    key: value
                    for key, value in errors.items()
                    if not _is_project_context_layer_field(str(key))
                }
            # Selection metadata is useful only for the layers it describes;
            # leave user/session diagnostics untouched while avoiding stale
            # project/task claims in provider traces.
            for key in (
                "project_information_selection",
                "task_context_selection",
                "project_context_mode",
                "project_scope_authorized",
                "task_scope_authorized",
                "task_project_id",
                "work_intelligence_selection",
                "work_intelligence_gate",
                "work_intelligence_compiled",
                "work_intelligence_omissions",
                "work_intelligence_manifest_provenance",
            ):
                cleaned_debug.pop(key, None)
            changes["debug"] = cleaned_debug

    if not changes:
        return bundle
    try:
        return dataclasses.replace(bundle, **changes)
    except (TypeError, ValueError):
        # A custom dataclass implementation may reject one extension field;
        # fall back to replacing only fields accepted by its constructor.
        accepted = {
            name: value
            for name, value in changes.items()
            if name in bundle_fields
        }
        try:
            return dataclasses.replace(bundle, **accepted)
        except (TypeError, ValueError):
            return bundle


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
        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        # Controller turns such as AoiTalk Help carry their own verified
        # snapshot in ``user_input``.  Treat every optional context argument
        # as untrusted here as well: this method is also called directly by
        # provider compatibility paths that may still hold stale state.
        self._current_memory_recall_context = (
            "" if suppress_automatic_context else str(memory_recall or "")
        )
        self._current_tool_hint_context = (
            "" if suppress_automatic_context else str(tool_hint_context or "")
        )
        history = (
            []
            if suppress_automatic_context
            else self.history_manager.get_model_messages()
        )
        state_mode_before = self._provider_state_mode
        if (
            history
            and history[-1].get("role") == "user"
            and str(history[-1].get("content") or "") == user_input
        ):
            history = history[:-1]

        include_project_context = (
            False
            if suppress_automatic_context
            else self._project_context_enabled_for_turn()
        )
        dynamic: list[tuple[str, str]] = []
        if (
            not suppress_automatic_context
            and self._current_context_bundle
            and not self._get_story_chat_context_sync()
        ):
            bundle = self._context_bundle_for_turn(include_project_context)
            if self.history_manager.summary and getattr(bundle, "session_context_block", ""):
                bundle = dataclasses.replace(bundle, session_context_block="")
            dynamic.append(
                ("Current ContextBundle", bundle.render_for_prompt())
            )
        if (
            not suppress_automatic_context
            and include_project_context
            and project_context
            and not self._current_context_bundle
        ):
            dynamic.append(
                (
                    "Current Project Context",
                    format_project_context_for_chat_prompt(project_context),
                )
            )
        if not suppress_automatic_context and memory_recall:
            dynamic.append(("Current memory search results", memory_recall))
        if not suppress_automatic_context and tool_hint_context:
            dynamic.append(("Current tool hints", tool_hint_context))
        self._current_prompt_dynamic_context = list(dynamic)
        return build_prompt_messages(
            history,
            summary=("" if suppress_automatic_context else self.history_manager.summary),
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
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            # Help is a one-turn, Guide-only controller flow.  Returning an
            # empty block prevents legacy worldbook/history fallbacks from
            # widening the provider prompt when this helper is called outside
            # the normal generation path.
            return ""
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
        # Project's identity, Docs, or tasks into this turn through a
        # stale/direct-client ContextBundle fallback.
        return strip_project_context_bundle(bundle)

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
        # Trusted controller turns such as AoiTalk Help are deliberately
        # independent of the active conversation. Do not resolve the
        # StoryWritingSession here: the resolver performs a session-scoped DB
        # read and would widen the Guide-only data boundary.
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return None
        if not self.current_session_id:
            return None
        return run_story_chat_context_sync(self._run_sync, self.current_session_id)

    async def _build_context_bundle_for_prompt(
        self, user_input: str, project_context: Optional[dict[str, Any]]
    ) -> Optional[ContextBundle]:
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            # A controller turn such as AoiTalk Help already carries its
            # server-verified Guide snapshot.  Avoid Story/Project/Docs
            # resolution and memory reads entirely on this path.
            return None
        if self._get_story_chat_context_sync():
            return None
        # Project Context is controlled by the immutable turn flag (or the
        # provider compatibility flag), not by natural-language search words.
        include_project_context = self._project_context_enabled_for_turn()
        turn_task_id = get_turn_context().task_id
        try:
            try:
                context_builder = ContextBuilder(
                    manifest_config=getattr(self, "config", None)
                )
            except TypeError:
                context_builder = ContextBuilder()
            return await context_builder.build_context(
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
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            # Controller turns such as AoiTalk Help are already grounded by a
            # server-owned snapshot.  Do not resolve the selected session or
            # Project here: even a read-only ACL lookup would widen the Help
            # data scope and could expose private metadata to the provider.
            return None
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
