"""Build compact runtime context blocks for LLM prompts."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from sqlalchemy import select

from ..memory.database import get_db_session
from ..memory.models import ConversationSession, Task
from .context_memory_service import ContextMemoryService
from .scoped_memory_flags import (
    legacy_agent_memory_read_enabled,
    scoped_memory_v2_enabled,
)
from .scoped_memory_service import ScopedMemoryService
from .project_context import (
    ProjectContextResolver,
    format_minimal_project_context_for_chat_prompt,
    format_project_context_for_chat_prompt,
)
from .project_context_pack_service import ProjectContextPackService
from .docs_acl import can_read_node, docs_readable_node_predicate
from .docs_workspace import get_project_docs_library
from .turn_context import get_turn_context

logger = logging.getLogger(__name__)

# Accessible Knowledge is an additive convenience layer.  It must never hold
# the chat request open indefinitely when Docs/ACL storage is slow.
ACCESSIBLE_KNOWLEDGE_TIMEOUT_SECONDS = 3.0

_DETAILED_PROJECT_CONTEXT_RE = re.compile(
    r"(?:"
    r"architecture|spec(?:ification)?|requirement|design|decision|history|"
    r"implementation|deadline|due\s+date|owner|assignee|customer|client|"
    r"database|\bdb\b|schedule|milestone|status|progress|risk|issue|"
    r"project\s+(?:detail|context|memory)|"
    r"アーキテクチャ|仕様|要件|設計|判断|経緯|実装|案件情報|"
    r"プロジェクト(?:情報|詳細|記憶)|納期|期限|担当|顧客|お客様|"
    r"利用DB|データベース|進捗|課題|リスク|"
    r"マイルストーン|契約"
    r")",
    re.IGNORECASE,
)
_PROJECT_REFERENCE_RE = re.compile(
    r"(?:project|client|task|案件|プロジェクト|顧客|タスク|WBS|仕様|実装)",
    re.IGNORECASE,
)
_WEAK_DETAIL_RE = re.compile(
    r"(?:status|schedule|environment|target|scope|detail|"
    r"状況|予定|日程|スケジュール|環境|構成|対象|範囲|詳しく|詳細)",
    re.IGNORECASE,
)

_PROJECT_INFORMATION_EXCLUDED_PREFIXES = (
    "agent_memory",
    "project_inbox",
    "project_mail",
    "workspace_file_reference:",
)
_PROJECT_INFORMATION_EXCLUDED_DOMAINS = {
    "legacy_agent_memory",
    "project_inbox",
    "project_mail",
    "workspace_file_reference",
}


def _needs_detailed_project_context(message: str) -> bool:
    text = str(message or "")
    return bool(
        _DETAILED_PROJECT_CONTEXT_RE.search(text)
        or (
            _PROJECT_REFERENCE_RE.search(text)
            and _WEAK_DETAIL_RE.search(text)
        )
    )


def _coerce_uuid(value: Optional[str]) -> Optional[uuid.UUID]:
    if not value:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _clip_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else ""


def _heading_outline(text: str, *, limit: int = 20) -> list[str]:
    headings: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        title = stripped.lstrip("#").strip()
        if title:
            headings.append(title)
        if len(headings) >= limit:
            break
    return headings


def _field_value_text(value: Any) -> str:
    if getattr(value, "target_node_id", None):
        return f"@docs:{value.target_node_id}"
    if getattr(value, "value_text", None):
        return str(value.value_text)
    if getattr(value, "value_number", None) is not None:
        return str(value.value_number)
    if getattr(value, "value_datetime", None) is not None:
        return _iso(value.value_datetime)
    raw = getattr(value, "value_json", None)
    if raw is None:
        return ""
    if isinstance(raw, dict):
        for key in ("value", "text", "label", "id"):
            if raw.get(key) not in (None, ""):
                return str(raw[key])
    return str(raw)


def _render_project_knowledge_index(value: Any) -> str:
    """Render only safe KnowledgeNode identity metadata for the prompt index."""

    if not isinstance(value, dict):
        return ""

    def render_nodes(items: Any, *, include_priority: bool) -> list[str]:
        lines: list[str] = []
        if not isinstance(items, list):
            return lines
        for item in items:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("id") or item.get("node_id") or "").strip()
            title = str(item.get("title") or "(untitled)").strip()
            if not node_id:
                continue
            details = [f"@docs:{node_id}", title]
            if include_priority and item.get("priority") is not None:
                details.append(f"priority={item.get('priority')}")
            lines.append("- " + " | ".join(details))
        return lines

    canonical = render_nodes(value.get("canonical_nodes"), include_priority=False)
    related = render_nodes(value.get("related_nodes"), include_priority=True)
    if not canonical and not related:
        return ""
    lines = ["## Active Project Knowledge"]
    if canonical:
        lines.append("### Canonical")
        lines.extend(canonical)
    if related:
        lines.append("### Related")
        lines.extend(related)
    return "\n".join(lines)


def _render_accessible_knowledge_index(value: Any) -> str:
    """Render ACL-filtered KnowledgeNode identity metadata only.

    ``accessible_knowledge_index`` is deliberately a title/id index.  The
    resolver and its bounded projection are responsible for deciding which
    nodes are visible; this renderer never accepts or emits node body,
    description, field, or table data.
    """

    if not isinstance(value, dict):
        return ""

    def render_nodes(items: Any, *, include_relation: bool) -> list[str]:
        lines: list[str] = []
        if not isinstance(items, list):
            return lines
        for item in items[:24]:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("id") or item.get("node_id") or "").strip()
            title = str(item.get("title") or "(untitled)")
            # A title is identity metadata, but remove line breaks so a
            # malformed/malicious title cannot smuggle an arbitrary prompt
            # block into the compact index.
            title = _clip_text(" ".join(title.split()), 180)
            if not node_id:
                continue
            details = [f"@docs:{node_id}", title]
            if include_relation and item.get("relation"):
                details.append(str(item["relation"]))
            lines.append("- " + " | ".join(details))
        return lines

    project = render_nodes(value.get("project"), include_relation=True)
    personal = render_nodes(value.get("personal"), include_relation=False)
    if not project and not personal:
        return ""
    lines = ["## Accessible Knowledge"]
    if project:
        lines.append("### Project")
        lines.extend(project)
    if personal:
        lines.append("### Personal")
        lines.extend(personal)
    return "\n".join(lines)


def _project_information_node_allowed(
    node: Any,
    *,
    canonical_subtree_ids: set[uuid.UUID],
    tags: list[dict[str, str]],
) -> bool:
    system_key = str(getattr(node, "system_key", None) or "")
    props = node.display_props if isinstance(getattr(node, "display_props", None), dict) else {}
    managed_domain = str(props.get("managed_domain") or "").strip()
    if system_key.startswith(_PROJECT_INFORMATION_EXCLUDED_PREFIXES):
        return False
    if managed_domain in _PROJECT_INFORMATION_EXCLUDED_DOMAINS:
        return False
    if node.id in canonical_subtree_ids:
        return True
    if props.get("include_in_project_information_context") is True:
        return True
    if str(props.get("context_domain") or "") == "project_information":
        return True
    return any(tag.get("base_type") == "project_information" for tag in tags)


@dataclass
class ContextBundle:
    memory_context_block: str = ""
    project_context_block: str = ""
    project_information_block: str = ""
    project_knowledge_index: Optional[Dict[str, Any]] = None
    accessible_knowledge_index: Optional[Dict[str, Any]] = None
    agent_memory_block: str = ""
    project_pack_block: str = ""
    task_context_block: str = ""
    session_context_block: str = ""
    debug: Dict[str, Any] = field(default_factory=dict)
    max_chars: int = 12000

    def render_with_trace(self, max_chars: Optional[int] = None) -> tuple[str, list[dict[str, Any]]]:
        limit = max_chars or self.max_chars
        project_knowledge_index_block = _render_project_knowledge_index(
            self.project_knowledge_index
        )
        accessible_knowledge_index_block = _render_accessible_knowledge_index(
            self.accessible_knowledge_index
        )
        blocks = [
            ("project_context", "Project context", "ContextBundle.project_context_block", self.project_context_block, 85),
            ("session_summary", "Session summary", "ContextBundle.session_context_block", self.session_context_block, 90),
            ("context_memory", "Context memory", "ContextBundle.memory_context_block", self.memory_context_block, 100),
            ("project_knowledge_index", "Active Project Knowledge", "ContextBundle.project_knowledge_index", project_knowledge_index_block, 84),
            ("accessible_knowledge_index", "Accessible Knowledge", "ContextBundle.accessible_knowledge_index", accessible_knowledge_index_block, 82),
            ("project_information", "Project information / Docs", "ContextBundle.project_information_block", self.project_information_block, 80),
            ("agent_memory", "Agent Memory", "ContextBundle.agent_memory_block", self.agent_memory_block, 70),
            ("project_context_pack", "Project context pack", "ContextBundle.project_pack_block", self.project_pack_block, 60),
            ("active_task_context", "Active task context", "ContextBundle.task_context_block", self.task_context_block, 95),
        ]
        seen: set[str] = set()
        candidates: list[tuple[int, str, str, str, str, int]] = []
        trace: list[dict[str, Any]] = []
        duplicate_categories: set[str] = set()
        for order, (category, label, source, block, priority) in enumerate(blocks):
            layer = (self.debug.get("layers") or {}).get(category, {})
            block = (block or "").strip()
            if not block:
                if layer:
                    trace.append(
                        {
                            "category": category,
                            "label": label,
                            "source": source,
                            "text": "",
                            "status": layer.get("status", "deferred"),
                            "preview": "このターンではモデルへ未送信",
                            "selection_reason": layer.get(
                                "selection_reason", "no_context_selected"
                            ),
                            "duration_ms": layer.get("duration_ms"),
                            "retrieved_chars": layer.get("retrieved_chars", 0),
                            "selected_chars": 0,
                        }
                    )
                continue
            if block in seen:
                duplicate_categories.add(category)
                continue
            seen.add(block)
            candidates.append((order, category, label, source, block, priority))

        selected: dict[str, str] = {}
        used = 0
        # Allocate the shared budget by relevance/importance first. Rendering is
        # still performed in the stable historical order below so prompts remain
        # predictable while low-priority trailing/leading blocks no longer win
        # merely because of their position.
        for _order, category, _label, _source, block, priority in sorted(
            candidates, key=lambda item: (-item[5], item[0])
        ):
            separator = 2 if selected else 0
            remaining = max(0, limit - used - separator)
            if len(block) <= remaining:
                selected[category] = block
                used += separator + len(block)
            elif remaining > 80:
                clipped = _clip_text(block, remaining)
                selected[category] = clipped
                used += separator + len(clipped)

        rendered: list[str] = []
        candidate_by_category = {item[1]: item for item in candidates}
        for category, label, source, raw_block, _priority in blocks:
            layer = (self.debug.get("layers") or {}).get(category, {})
            block = (raw_block or "").strip()
            if not block:
                continue
            if category in duplicate_categories:
                trace.append({"category": category, "label": label, "source": source, "text": "", "status": "deferred", "preview": "重複のため未送信", "selection_reason": "duplicate_context", "duration_ms": layer.get("duration_ms"), "retrieved_chars": layer.get("retrieved_chars", len(block)), "selected_chars": 0})
                continue
            if category not in candidate_by_category or category not in selected:
                trace.append({"category": category, "label": label, "source": source, "text": "", "status": "deferred", "preview": "重要度・関連度を考慮したコンテキスト予算超過のため未送信", "selection_reason": "context_budget_exceeded", "duration_ms": layer.get("duration_ms"), "retrieved_chars": layer.get("retrieved_chars", len(block)), "selected_chars": 0})
                continue
            chosen = selected[category]
            rendered.append(chosen)
            clipped = len(chosen) < len(block)
            trace.append({"category": category, "label": label, "source": source, "text": chosen, "status": "active", "preview": "上限に合わせて切り詰めて送信" if clipped else "モデルへ送信済み", "selection_reason": layer.get("selection_reason", "selected_with_budget_truncation" if clipped else "selected_for_current_turn"), "duration_ms": layer.get("duration_ms"), "retrieved_chars": layer.get("retrieved_chars", len(block)), "selected_chars": len(chosen)})
        return "\n\n".join(rendered), trace

    def render_for_prompt(self, max_chars: Optional[int] = None) -> str:
        return self.render_with_trace(max_chars)[0]


class ContextBuilder:
    """Collect existing and scoped context into one prompt block."""

    def __init__(
        self,
        *,
        context_memory_service: Optional[Any] = None,
        project_context_pack_service: Optional[ProjectContextPackService] = None,
        project_context_resolver: Optional[ProjectContextResolver] = None,
    ):
        self.context_memory_service = context_memory_service or (
            ScopedMemoryService()
            if scoped_memory_v2_enabled()
            else ContextMemoryService()
        )
        self.project_context_pack_service = (
            project_context_pack_service or ProjectContextPackService()
        )
        self.project_context_resolver = project_context_resolver or ProjectContextResolver()

    async def build_context(
        self,
        *,
        user_id: str,
        message: str,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        max_chars: int = 12000,
        project_context: Optional[dict[str, Any]] = None,
        include_project_context: bool = True,
        include_project_information: Optional[bool] = None,
        include_agent_memory: Optional[bool] = None,
        include_project_pack: Optional[bool] = None,
        include_task_context: Optional[bool] = None,
        project_context_mode: str = "auto",
    ) -> ContextBundle:
        # Provider bridges historically omitted ``task_id`` because ordinary
        # chat turns do not have one.  A request that entered through a
        # specific, server-authorized Task boundary carries it in the
        # task-local TurnContext; inherit that value without inventing a scope
        # for normal conversations.
        if task_id is None:
            try:
                task_id = get_turn_context().task_id
            except Exception:
                task_id = None
        debug: Dict[str, Any] = {
            "user_id": user_id,
            "project_id": project_id,
            "task_id": task_id,
            "session_id": session_id,
            "errors": {},
            "layers": {},
        }
        bundle = ContextBundle(max_chars=max_chars, debug=debug)

        def record_layer(
            category: str,
            started: float,
            block: str,
            selection_reason: str,
        ) -> None:
            error_keys = {
                "project_context": "project_context",
                "project_knowledge_index": "project_knowledge",
                "accessible_knowledge_index": "accessible_knowledge",
                "project_information": "project_information",
                "project_context_pack": "project_context_pack",
                "agent_memory": "agent_memory",
                "active_task_context": "task_context",
                "session_summary": "session_context",
                "context_memory": "context_memories",
            }
            error_key = error_keys.get(category)
            # ``project_pack`` is a lifecycle warning (stale/failed) while
            # ``project_context_pack`` is the historical retrieval error key.
            # Both must keep the layer trace honest without changing the
            # externally visible warning contract.
            lifecycle_error = debug["errors"].get("project_pack")
            failed = bool(
                error_key in debug["errors"]
                or (
                    category == "project_context_pack"
                    and lifecycle_error
                    and lifecycle_error != "stale_context_pack"
                )
            )
            debug["layers"][category] = {
                "selection_reason": (
                    "retrieval_failed" if failed else selection_reason
                ),
                "status": "failed" if failed else (
                    "active" if block else "deferred"
                ),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "retrieved_chars": len(str(block or "")),
            }
        detailed_context_needed = _needs_detailed_project_context(message)
        if include_project_pack is None:
            include_project_pack = detailed_context_needed
        if include_task_context is None:
            include_task_context = bool(task_id) or detailed_context_needed
        resolved_project_context_mode = (
            "full"
            if project_context_mode == "auto" and detailed_context_needed
            else "minimal"
            if project_context_mode == "auto"
            else project_context_mode
        )
        if include_project_information is None:
            include_project_information = detailed_context_needed
        if include_agent_memory is None:
            include_agent_memory = detailed_context_needed
        debug["project_information_selection"] = (
            "explicit_or_current_message_requires_details"
            if include_project_information
            else "deferred_until_relevant_turn"
        )
        debug["agent_memory_selection"] = (
            "explicit_or_current_message_requires_details"
            if include_agent_memory
            else "deferred_until_relevant_turn"
        )
        debug["project_pack_selection"] = (
            "explicit_or_current_message_requires_details"
            if include_project_pack
            else "deferred_until_relevant_turn"
        )
        debug["task_context_selection"] = (
            "explicit_task_or_current_message_requires_details"
            if include_task_context
            else "deferred_until_relevant_turn"
        )
        debug["project_context_mode"] = resolved_project_context_mode

        if session_id:
            try:
                resolved_user_id = await self._resolve_session_user_id(session_id)
            except Exception as exc:
                logger.warning("[ContextBuilder] session user lookup failed: %s", exc)
                debug["errors"]["session_user"] = str(exc)
            else:
                if resolved_user_id and (not user_id or user_id == "default_user"):
                    user_id = resolved_user_id
                    debug["user_id"] = user_id
                    debug["user_id_source"] = "conversation_session"
                elif resolved_user_id != user_id:
                    debug["session_scope_authorized"] = False
                    debug["errors"]["session_scope"] = "session access denied"
                    session_id = None
                    debug["session_id"] = None

        if task_id:
            try:
                task_project_id = await self._resolve_task_project_id(task_id)
            except Exception as exc:
                logger.warning("[ContextBuilder] task scope lookup failed: %s", exc)
                task_project_id = None
            if task_project_id is None or (
                project_id is not None and str(project_id) != str(task_project_id)
            ):
                debug["task_scope_authorized"] = False
                debug["errors"]["task_scope"] = "task access denied"
                task_id = None
                debug["task_id"] = None
            elif project_id is None:
                project_id = task_project_id
                debug["project_id"] = project_id

        resolved_project_context = project_context if include_project_context else None
        project_context_started = time.perf_counter()
        if include_project_context:
            if resolved_project_context is None and (project_id or session_id):
                try:
                    resolved_project_context = await self.project_context_resolver.resolve_context(
                        project_id=project_id,
                        session_id=session_id,
                        user_id=user_id,
                    )
                except Exception as exc:
                    logger.warning("[ContextBuilder] project context failed: %s", exc)
                    debug["errors"]["project_context"] = str(exc)

            if resolved_project_context:
                if resolved_project_context_mode == "minimal":
                    bundle.project_context_block = (
                        format_minimal_project_context_for_chat_prompt(
                            resolved_project_context
                        )
                    )
                else:
                    bundle.project_context_block = format_project_context_for_chat_prompt(
                        resolved_project_context
                    )
                if not project_id and resolved_project_context.get("id"):
                    project_id = str(resolved_project_context["id"])
                    debug["project_id"] = project_id
            if project_id and (
                not resolved_project_context
                or str(resolved_project_context.get("id") or "") != str(project_id)
            ):
                debug["project_scope_authorized"] = False
                debug["errors"]["project_scope"] = "project access denied"
                project_id = None
                task_id = None
                debug["project_id"] = None
                debug["task_id"] = None
            elif project_id:
                debug["project_scope_authorized"] = True
        record_layer(
            "project_context",
            project_context_started,
            bundle.project_context_block,
            (
                "selected_project_context"
                if bundle.project_context_block
                else "project_context_disabled_or_unavailable"
            ),
        )

        if include_project_context and project_id:
            project_knowledge_started = time.perf_counter()
            try:
                from .project_knowledge_service import resolve_project_knowledge

                bundle.project_knowledge_index = await resolve_project_knowledge(
                    project_id=project_id,
                    actor_user_id=user_id,
                )
            except Exception as exc:
                logger.warning("[ContextBuilder] project knowledge index failed: %s", exc)
                debug["errors"]["project_knowledge"] = str(exc)
            record_layer(
                "project_knowledge_index",
                project_knowledge_started,
                _render_project_knowledge_index(bundle.project_knowledge_index),
                "selected_project_knowledge_index",
            )

            accessible_knowledge_started = time.perf_counter()
            accessible_knowledge_timeout = max(
                0.01,
                float(ACCESSIBLE_KNOWLEDGE_TIMEOUT_SECONDS),
            )
            try:
                bundle.accessible_knowledge_index = (
                    await asyncio.wait_for(
                        self._build_accessible_knowledge_index(
                            project_id=project_id,
                            user_id=user_id,
                            max_nodes=24,
                        ),
                        timeout=accessible_knowledge_timeout,
                    )
                )
            except asyncio.TimeoutError:
                # This layer is optional.  Keep canonical Project Knowledge
                # and normal chat generation alive even when Personal Docs
                # retrieval cannot finish inside its bounded budget.
                error = (
                    "accessible knowledge retrieval timed out after "
                    f"{accessible_knowledge_timeout:g}s"
                )
                logger.warning("[ContextBuilder] %s", error)
                debug["errors"]["accessible_knowledge"] = error
            except Exception as exc:
                # Personal Docs are an additive index layer.  A scope/DB
                # failure must not suppress the canonical Project Knowledge,
                # Project Information, or Project Context Pack layers.
                logger.warning(
                    "[ContextBuilder] accessible knowledge index failed: %s",
                    exc,
                )
                debug["errors"]["accessible_knowledge"] = str(exc)
            record_layer(
                "accessible_knowledge_index",
                accessible_knowledge_started,
                _render_accessible_knowledge_index(
                    bundle.accessible_knowledge_index
                ),
                "selected_accessible_knowledge_index",
            )

            project_information_started = time.perf_counter()
            if include_project_information:
                try:
                    bundle.project_information_block = await self._build_project_information_block(
                        project_id=project_id,
                        user_id=user_id,
                    )
                    debug["project_information_context"] = bool(
                        bundle.project_information_block
                    )
                except Exception as exc:
                    logger.warning("[ContextBuilder] project information failed: %s", exc)
                    debug["errors"]["project_information"] = str(exc)
            record_layer(
                "project_information",
                project_information_started,
                bundle.project_information_block,
                debug["project_information_selection"],
            )

            project_pack_started = time.perf_counter()
            if include_project_pack:
                try:
                    project_pack_status = None
                    actor_renderer = getattr(
                        self.project_context_pack_service,
                        "render_project_context_pack_for_actor",
                        None,
                    )
                    if callable(actor_renderer):
                        lifecycle_result = await actor_renderer(
                            project_id,
                            actor_user_id=user_id,
                        )
                        if isinstance(lifecycle_result, tuple):
                            (
                                bundle.project_pack_block,
                                project_pack_status,
                            ) = lifecycle_result
                        elif isinstance(lifecycle_result, dict):
                            bundle.project_pack_block = str(
                                lifecycle_result.get("rendered")
                                or lifecycle_result.get("block")
                                or ""
                            )
                            project_pack_status = lifecycle_result.get("status")
                        else:
                            bundle.project_pack_block = str(lifecycle_result or "")
                            project_pack_status = None
                    else:
                        # Keep lifecycle-aware test doubles and older
                        # integrations compatible while actor-scoped readers
                        # roll out.
                        render_with_status = getattr(
                            self.project_context_pack_service,
                            "render_project_context_pack_for_prompt_with_status",
                            None,
                        )
                        if callable(render_with_status):
                            lifecycle_result = await render_with_status(project_id)
                            if isinstance(lifecycle_result, tuple):
                                (
                                    bundle.project_pack_block,
                                    project_pack_status,
                                ) = lifecycle_result
                            elif isinstance(lifecycle_result, dict):
                                bundle.project_pack_block = str(
                                    lifecycle_result.get("rendered")
                                    or lifecycle_result.get("block")
                                    or ""
                                )
                                project_pack_status = lifecycle_result.get("status")
                            else:
                                bundle.project_pack_block = str(lifecycle_result or "")
                                project_pack_status = None
                        else:
                            # Keep lightweight test doubles and older
                            # integrations compatible while the lifecycle-aware
                            # reader rolls out.
                            legacy_pack_reader = getattr(
                                self.project_context_pack_service,
                                "get_project_context_pack",
                                None,
                            )
                            legacy_pack = (
                                await legacy_pack_reader(project_id)
                                if callable(legacy_pack_reader)
                                else None
                            )
                            legacy_status = str(
                                (legacy_pack or {}).get("status") or "fresh"
                            ).casefold()
                            if legacy_status == "stale":
                                debug["errors"]["project_pack"] = (
                                    "stale_context_pack"
                                )
                                bundle.project_pack_block = (
                                    await self.project_context_pack_service.render_project_context_pack_for_prompt(
                                        project_id
                                    )
                                )
                            elif legacy_status == "failed":
                                debug["errors"]["project_pack"] = (
                                    "failed_context_pack"
                                )
                                bundle.project_pack_block = ""
                            else:
                                bundle.project_pack_block = (
                                    await self.project_context_pack_service.render_project_context_pack_for_prompt(
                                        project_id
                                    )
                                )

                    project_pack_status = str(
                        project_pack_status or ""
                    ).casefold()
                    if project_pack_status == "stale":
                        debug["errors"]["project_pack"] = (
                            "stale_context_pack"
                        )
                    elif project_pack_status == "failed":
                        debug["errors"]["project_pack"] = (
                            "failed_context_pack"
                        )
                        bundle.project_pack_block = ""
                except Exception as exc:
                    logger.warning("[ContextBuilder] project context pack failed: %s", exc)
                    debug["errors"]["project_context_pack"] = str(exc)
            record_layer(
                "project_context_pack",
                project_pack_started,
                bundle.project_pack_block,
                debug["project_pack_selection"],
            )

        agent_memory_started = time.perf_counter()
        # Project Context OFF must not turn the retained Selected Project ID
        # into an implicit Agent Memory scope.  The ID remains available to
        # runtime authorization/get_project_context(), while rich project
        # memory is strictly model-visible Project Context.
        if include_agent_memory and include_project_context and project_id:
            try:
                bundle.agent_memory_block = await self._build_agent_memory_block(
                    project_id=project_id,
                    user_id=user_id,
                )
                debug["agent_memory_context"] = bool(bundle.agent_memory_block)
            except Exception as exc:
                logger.warning("[ContextBuilder] agent memory failed: %s", exc)
                debug["errors"]["agent_memory"] = str(exc)
        record_layer(
            "agent_memory",
            agent_memory_started,
            bundle.agent_memory_block,
            debug["agent_memory_selection"],
        )

        task_context_started = time.perf_counter()
        if include_task_context:
            try:
                bundle.task_context_block = await self._build_task_context_block(
                    project_id=project_id if include_project_context else None,
                    task_id=task_id,
                )
            except Exception as exc:
                logger.warning("[ContextBuilder] task context failed: %s", exc)
                debug["errors"]["task_context"] = str(exc)
        record_layer(
            "active_task_context",
            task_context_started,
            bundle.task_context_block,
            (
                debug["task_context_selection"]
            ),
        )

        session_context_started = time.perf_counter()
        try:
            bundle.session_context_block = await self._build_session_context_block(
                session_id
            )
        except Exception as exc:
            logger.warning("[ContextBuilder] session context failed: %s", exc)
            debug["errors"]["session_context"] = str(exc)
        record_layer(
            "session_summary",
            session_context_started,
            bundle.session_context_block,
            "current_session_summary",
        )

        context_memory_started = time.perf_counter()
        try:
            if hasattr(self.context_memory_service, "retrieve_for_context"):
                memories, retrieval_trace = (
                    await self.context_memory_service.retrieve_for_context(
                        actor_id=user_id,
                        project_id=project_id if include_project_context else None,
                        task_id=task_id,
                        session_id=session_id,
                        query=message,
                        limit=8,
                        max_chars=5000,
                    )
                )
                debug["context_memory_retrieval_trace"] = retrieval_trace
            else:
                memories = await self.context_memory_service.get_memories_for_context(
                    user_id=user_id,
                    project_id=project_id if include_project_context else None,
                    task_id=task_id,
                    session_id=session_id,
                    message=message,
                    limit=8,
                )
            bundle.memory_context_block = (
                self.context_memory_service.render_memories_for_prompt(memories)
            )
            debug["context_memory_count"] = len(memories)
        except Exception as exc:
            logger.warning("[ContextBuilder] context memories failed: %s", exc)
            debug["errors"]["context_memories"] = str(exc)
        record_layer(
            "context_memory",
            context_memory_started,
            bundle.memory_context_block,
            "scoped_relevance_search",
        )

        return bundle

    async def _build_accessible_knowledge_index(
        self,
        *,
        project_id: str,
        user_id: str,
        max_nodes: int = 24,
    ) -> Optional[Dict[str, Any]]:
        """Resolve and render a bounded ACL-visible Docs identity index.

        ``resolve_docs_scope`` is the single authorization boundary.  It
        returns identifiers only; this method performs one bounded projection
        for ``id/title/project_id/archived_at`` and deliberately never selects
        body, description, field, table, or other content columns.
        """

        project_uuid = _coerce_uuid(project_id)
        actor_uuid = _coerce_uuid(user_id)
        if project_uuid is None or actor_uuid is None:
            return None

        from ..memory.models import KnowledgeNode
        from .docs_scope import DocsScopeMode, resolve_docs_scope

        limit = max(1, min(int(max_nodes or 24), 24))
        async with await get_db_session() as session:
            scope = await resolve_docs_scope(
                session=session,
                actor_user_id=actor_uuid,
                project_id=project_uuid,
                mode=DocsScopeMode.PROJECT_PLUS_PERSONAL,
                max_personal_nodes=limit,
            )

            # Fail closed if the scope resolver did not authorize this
            # project.  ``reason`` is intentionally not exposed to the model;
            # the layer trace records only the compact retrieval outcome.
            scope_project_id = _coerce_uuid(
                getattr(scope, "project_id", project_uuid)
            )
            if (
                scope is None
                or scope_project_id != project_uuid
                or getattr(scope, "reason", None)
            ):
                return None

            canonical_ids: list[uuid.UUID] = []
            related_ids: list[uuid.UUID] = []
            seen: set[uuid.UUID] = set()

            def append_ids(target: list[uuid.UUID], values: Any) -> None:
                if not values:
                    return
                for raw in values:
                    node_uuid = _coerce_uuid(raw)
                    if node_uuid is None or node_uuid in seen:
                        continue
                    if len(canonical_ids) + len(related_ids) >= limit:
                        return
                    seen.add(node_uuid)
                    target.append(node_uuid)

            # Canonical nodes always win the bounded budget.  Personal IDs
            # are mixed into ``related_node_ids`` by DocsScope and are later
            # classified from the server-side project_id projection.
            append_ids(canonical_ids, getattr(scope, "canonical_node_ids", ()))
            append_ids(related_ids, getattr(scope, "related_node_ids", ()))
            ordered_ids = [*canonical_ids, *related_ids]
            if not ordered_ids:
                return {"project": [], "personal": []}

            result = await session.execute(
                select(
                    KnowledgeNode.id,
                    KnowledgeNode.title,
                    KnowledgeNode.project_id,
                    KnowledgeNode.archived_at,
                )
                .where(KnowledgeNode.id.in_(ordered_ids))
                .where(KnowledgeNode.archived_at.is_(None))
                .limit(limit)
            )
            rows = result.all() if hasattr(result, "all") else []

        # Reorder by the scope resolver's deterministic ID order rather than
        # relying on database IN-list ordering.
        by_id: dict[uuid.UUID, tuple[str, Optional[uuid.UUID]]] = {}
        for row in rows:
            try:
                node_id, title, node_project_id, archived_at = row
            except (TypeError, ValueError):
                mapping = getattr(row, "_mapping", {})
                node_id = mapping.get("id")
                title = mapping.get("title")
                node_project_id = mapping.get("project_id")
                archived_at = mapping.get("archived_at")
            node_uuid = _coerce_uuid(node_id)
            if node_uuid is None or archived_at is not None:
                continue
            node_project_uuid = _coerce_uuid(node_project_id)
            safe_title = _clip_text(
                " ".join(str(title or "(untitled)").split()),
                180,
            )
            by_id[node_uuid] = (safe_title, node_project_uuid)

        canonical_set = set(canonical_ids)
        project_entries: list[dict[str, str]] = []
        personal_entries: list[dict[str, str]] = []
        for node_uuid in ordered_ids:
            row = by_id.get(node_uuid)
            if row is None:
                continue
            title, node_project_uuid = row
            if node_uuid not in canonical_set and node_project_uuid is None:
                personal_entries.append(
                    {"id": str(node_uuid), "title": title}
                )
            else:
                project_entries.append(
                    {
                        "id": str(node_uuid),
                        "title": title,
                        "relation": (
                            "canonical"
                            if node_uuid in canonical_set
                            else "related"
                        ),
                    }
                )

        if not project_entries and not personal_entries:
            return None
        return {"project": project_entries, "personal": personal_entries}

    async def _resolve_session_user_id(self, session_id: Optional[str]) -> Optional[str]:
        session_uuid = _coerce_uuid(session_id)
        if not session_uuid:
            return None
        async with await get_db_session() as session:
            conversation = await session.get(ConversationSession, session_uuid)
            if not conversation:
                return None
            return str(conversation.user_id) if conversation.user_id else None

    async def _resolve_task_project_id(self, task_id: str) -> Optional[str]:
        task_uuid = _coerce_uuid(task_id)
        if not task_uuid:
            return None
        async with await get_db_session() as session:
            task = await session.get(Task, task_uuid)
            if not task or task.deleted_at is not None:
                return None
            return str(task.project_id) if task.project_id else None

    async def _build_session_context_block(self, session_id: Optional[str]) -> str:
        session_uuid = _coerce_uuid(session_id)
        if not session_uuid:
            return ""
        async with await get_db_session() as session:
            conversation = await session.get(ConversationSession, session_uuid)
            if not conversation or not conversation.current_summary:
                return ""
            return "## Session Summary\n" + conversation.current_summary.strip()

    async def _build_task_context_block(
        self,
        *,
        project_id: Optional[str],
        task_id: Optional[str],
    ) -> str:
        task_uuid = _coerce_uuid(task_id)
        project_uuid = _coerce_uuid(project_id)
        async with await get_db_session() as session:
            tasks: list[Task] = []
            if task_uuid:
                task = await session.get(Task, task_uuid)
                if task and task.deleted_at is None and task.archived_at is None:
                    tasks.append(task)
            elif project_uuid:
                result = await session.execute(
                    select(Task)
                    .where(Task.project_id == project_uuid)
                    .where(Task.deleted_at.is_(None))
                    .where(Task.archived_at.is_(None))
                    .where(Task.status.notin_(["closed", "done", "completed"]))
                    .order_by(Task.priority.desc(), Task.updated_at.desc())
                    .limit(8)
                )
                tasks = list(result.scalars().all())

        if not tasks:
            return ""

        lines = ["## Active Task Context"]
        for task in tasks:
            title = task.title or "(untitled)"
            details = [f"status={task.status}"]
            if task.priority:
                details.append(f"priority={task.priority}")
            if task.end_at:
                details.append(f"end_at={task.end_at.isoformat()}")
            lines.append(f"- {title} ({', '.join(details)})")
            if task.description:
                lines.append(f"  {task.description.strip()[:500]}")
        return "\n".join(lines)

    async def _build_agent_memory_block(
        self,
        *,
        project_id: str,
        user_id: str | None = None,
        agent_memory_chars: int = 4000,
    ) -> str:
        """プロジェクト毎のエージェントメモリ索引ノードのアウトラインを注入する。

        索引ノード（system_key="agent_memory:<project_id>"）を ensure し、
        その直下の子ノード群（1エントリ=1子ノード）を浅いアウトラインで描画する。
        DB接続やensureに失敗してもチャットを壊さず空ブロックへ落とす。
        """
        project_uuid = _coerce_uuid(project_id)
        user_uuid = _coerce_uuid(user_id)
        if not project_uuid or not legacy_agent_memory_read_enabled():
            return ""

        from .agent_memory_docs import (
            AGENT_MEMORY_AI_INSTRUCTIONS,
            get_agent_memory_doc,
        )

        async with await get_db_session() as session:
            node = (
                await get_agent_memory_doc(session, project_uuid, user_uuid)
                if user_uuid is not None
                else await get_agent_memory_doc(session, project_uuid)
            )
            if node is None:
                return ""

            node_id = node.id
            node_title = (node.title or "(untitled)").strip()
            # 索引ノードは project_id を持つため DocsGraphService.outline_lines は
            # scope が project_id 一致となり、案件情報等プロジェクト全ノード(LIMIT 500)を
            # 引いてしまう。500超のプロジェクトではメモリの子が取得対象から漏れて静かに
            # 消え得るため、索引ノードの子孫だけを parent_id で辿って直接構築する。
            outline_lines = (
                await self._agent_memory_outline_lines(
                    session, node, depth=2, user_id=user_uuid
                )
                if user_uuid is not None
                else await self._agent_memory_outline_lines(session, node, depth=2)
            )

        outline_text = "\n".join(outline_lines).strip()
        truncated = False
        if len(outline_text) > max(1, agent_memory_chars):
            outline_text = _clip_text(outline_text, max(1, agent_memory_chars)).rstrip()
            truncated = True

        lines = [
            "## Agent Memory (project-scoped, agent-maintained)",
            (
                "プロジェクト毎の恒久メモリ（Claude CodeのMEMORY.md相当）。"
                "訂正・導出不能な知見・作業上の嗜好のみをここへ保存する。"
            ),
            f"- Memory Index Node: {node_title} (ref=@docs:{node_id})",
            (
                "- 書込単位: 索引ノード直下に「1エントリ=1子ノード」を docs_create_nodes で追加し、"
                "既存エントリの修正は docs_update_node で行う"
                "（索引ノード本文はタイトルミラー固定のため本文へは書き込まない）。"
            ),
            "- 詳細は各エントリの子ノードにある。必要なら docs_read で該当ノードを読む。",
            "- 上限接近時は古い項目を統合・圧縮する。秘密情報（パスワード/トークン）は保存禁止。",
        ]
        if AGENT_MEMORY_AI_INSTRUCTIONS.strip():
            lines.append(
                "- 保存基準: " + _clip_text(AGENT_MEMORY_AI_INSTRUCTIONS.strip(), 420)
            )
        lines.append("### Memory Entries Outline")
        if outline_text:
            lines.append(outline_text)
            if truncated:
                lines.append("...(truncated; 全量は docs_read で索引ノードを読む)")
        else:
            lines.append("(まだ記憶はありません)")

        return "\n".join(lines)

    async def _agent_memory_outline_lines(
        self,
        session: Any,
        root: Any,
        *,
        depth: int = 2,
        user_id: uuid.UUID | None = None,
    ) -> list[str]:
        """索引ノードのサブツリーだけを浅いアウトラインとして構築する。

        ``DocsGraphService.outline_lines`` はプロジェクト全ノードを引くため、
        ここでは ``parent_id`` を階層ごとに辿って索引ノードの子孫だけを取得し、
        同一フォーマット（``短縮ID タイトル #タグ`` + タブインデント、短縮IDは
        UUID 先頭8hex）で組み立てる。``docs_update_node`` / ``docs_read`` の
        ``resolve_node`` が 8-12hex プレフィックスで解決できる表記を保つ。
        """
        from ..memory.models import (
            DocsLibrary,
            KnowledgeNode,
            KnowledgeNodeSupertag,
            KnowledgeSupertag,
        )

        max_depth = max(0, min(int(depth or 2), 8))
        visibility = None
        if user_id is not None:
            library = await session.get(DocsLibrary, root.docs_library_id)
            visibility = docs_readable_node_predicate(
                KnowledgeNode,
                docs_library_id=root.docs_library_id,
                user_id=user_id,
                library_owner_id=getattr(library, "owner_user_id", None),
            )
            root_visible = await session.scalar(
                select(KnowledgeNode.id).where(
                    KnowledgeNode.id == root.id,
                    visibility,
                )
            )
            if root_visible is None:
                return []
        nodes: list[Any] = [root]
        children_map: dict[Any, list[Any]] = {}
        frontier: list[Any] = [root.id]
        current_depth = 0
        while current_depth < max_depth and frontier:
            level_result = await session.execute(
                select(KnowledgeNode)
                .where(
                    KnowledgeNode.docs_library_id == root.docs_library_id,
                    KnowledgeNode.parent_id.in_(frontier),
                    KnowledgeNode.archived_at.is_(None),
                    visibility if visibility is not None else True,
                )
                .order_by(KnowledgeNode.sort_order, KnowledgeNode.created_at)
                .limit(500)
            )
            level_nodes = list(level_result.scalars().unique().all())
            for child in level_nodes:
                children_map.setdefault(child.parent_id, []).append(child)
            nodes.extend(level_nodes)
            frontier = [child.id for child in level_nodes]
            current_depth += 1

        tags_by_node: dict[Any, list[str]] = {}
        if nodes:
            tag_rows = await session.execute(
                select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.name)
                .join(
                    KnowledgeSupertag,
                    KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id,
                )
                .where(
                    KnowledgeNodeSupertag.node_id.in_([node.id for node in nodes])
                )
            )
            for node_id, tag_name in tag_rows.all():
                tags_by_node.setdefault(node_id, []).append(tag_name)

        lines: list[str] = []

        def visit(node: Any, node_depth: int) -> None:
            if node_depth > max_depth:
                return
            tags = " ".join(f"#{name}" for name in tags_by_node.get(node.id, []))
            suffix = f" {tags}" if tags else ""
            indent = "\t" * node_depth
            lines.append(f"{indent}{str(node.id)[:8]} {node.title}{suffix}")
            for child in children_map.get(node.id, []):
                visit(child, node_depth + 1)

        visit(root, 0)
        return lines

    async def _build_project_information_block(
        self,
        *,
        project_id: str,
        user_id: str,
        max_record_tables: int = 6,
        max_related_nodes: int = 24,
        max_qas: int = 10,
        docs_node_chars: int = 8000,
    ) -> str:
        project_uuid = _coerce_uuid(project_id)
        if not project_uuid:
            return ""

        from ..memory.models import (
            KnowledgeEdge,
            KnowledgeField,
            KnowledgeFieldValue,
            KnowledgeNode,
            KnowledgeNodeSupertag,
            KnowledgeRevision,
            KnowledgeSupertag,
            DocsLibrary,
            Project,
            ProjectQaEntry,
            RecordTable,
        )

        async with await get_db_session() as session:
            project = await session.get(Project, project_uuid)
            user_uuid = _coerce_uuid(user_id)
            docs_library_id = None
            visibility = None
            if project and user_uuid:
                try:
                    project_workspace = await get_project_docs_library(
                        session,
                        project_id=project_uuid,
                        actor_user_id=user_uuid,
                    )
                except (PermissionError, ValueError):
                    # Project ACL is authoritative; return no project Docs
                    # context rather than falling back to a creator's private
                    # library or leaking a node title.
                    return ""
                if project_workspace is None:
                    return ""
                docs_library_id = project_workspace.id
                visibility = docs_readable_node_predicate(
                    KnowledgeNode,
                    docs_library_id=docs_library_id,
                    user_id=user_uuid,
                    library_owner_id=getattr(project_workspace, "owner_user_id", None),
                )
            tables_result = await session.execute(
                select(RecordTable)
                .where(
                    RecordTable.project_id == project_uuid,
                    RecordTable.deleted_at.is_(None),
                )
                .order_by(RecordTable.sort_order, RecordTable.created_at)
                .limit(max(1, max_record_tables))
            )
            qa_result = await session.execute(
                select(ProjectQaEntry)
                .where(
                    ProjectQaEntry.project_id == project_uuid,
                    ProjectQaEntry.deleted_at.is_(None),
                    ProjectQaEntry.status == "unanswered",
                    ProjectQaEntry.review_state == "accepted",
                )
                .order_by(ProjectQaEntry.asked_count.desc(), ProjectQaEntry.updated_at.desc())
                .limit(max(1, max_qas))
            )

            tables = list(tables_result.scalars().all())
            qa_entries = list(qa_result.scalars().all())

            canonical_node: Any = None
            if project and project.knowledge_node_id and docs_library_id:
                candidate = await session.get(KnowledgeNode, project.knowledge_node_id)
                if (
                    candidate
                    and candidate.docs_library_id == docs_library_id
                    and candidate.project_id == project_uuid
                    and not candidate.archived_at
                    and await can_read_node(
                        session,
                        candidate,
                        user_uuid,
                    )
                ):
                    canonical_node = candidate

            if canonical_node is None and docs_library_id:
                canonical_result = await session.execute(
                    select(KnowledgeNode)
                    .join(
                        KnowledgeNodeSupertag,
                        KnowledgeNodeSupertag.node_id == KnowledgeNode.id,
                    )
                    .join(
                        KnowledgeSupertag,
                        KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id,
                    )
                    .where(
                        KnowledgeNode.docs_library_id == docs_library_id,
                        KnowledgeNode.project_id == project_uuid,
                        KnowledgeNode.archived_at.is_(None),
                        visibility if visibility is not None else True,
                        KnowledgeSupertag.base_type == "project_information",
                    )
                    .order_by(KnowledgeNode.updated_at.desc())
                    .limit(1)
                )
                canonical_node = canonical_result.scalar_one_or_none()

            docs_nodes: list[Any] = []
            if docs_library_id:
                related_result = await session.execute(
                    select(KnowledgeNode)
                    .where(
                        KnowledgeNode.docs_library_id == docs_library_id,
                        KnowledgeNode.project_id == project_uuid,
                        KnowledgeNode.archived_at.is_(None),
                        visibility if visibility is not None else True,
                    )
                    .order_by(KnowledgeNode.updated_at.desc())
                    .limit(max(1, max_related_nodes + 1))
                )
                docs_nodes = list(related_result.scalars().all())
            if canonical_node and all(node.id != canonical_node.id for node in docs_nodes):
                docs_nodes.insert(0, canonical_node)

            child_nodes: list[Any] = []
            if canonical_node:
                child_result = await session.execute(
                    select(KnowledgeNode)
                    .where(
                        KnowledgeNode.docs_library_id == docs_library_id,
                        KnowledgeNode.project_id == project_uuid,
                        KnowledgeNode.archived_at.is_(None),
                        visibility if visibility is not None else True,
                    )
                    .order_by(KnowledgeNode.sort_order, KnowledgeNode.created_at)
                    .limit(500)
                )
                child_nodes = list(child_result.scalars().all())

            all_context_nodes = docs_nodes + [
                node for node in child_nodes if all(existing.id != node.id for existing in docs_nodes)
            ]
            docs_tags_by_node: dict[uuid.UUID, list[dict[str, str]]] = {}
            ai_instructions: dict[str, tuple[str, str, str]] = {}
            fields_by_tag: dict[uuid.UUID, list[KnowledgeField]] = {}
            field_values_by_node: dict[uuid.UUID, list[tuple[str, str]]] = {}
            edges: list[Any] = []
            revisions: list[Any] = []
            canonical_outline_lines: list[str] = []
            if docs_nodes:
                candidate_node_ids = [node.id for node in all_context_nodes]
                tag_rows = await session.execute(
                    select(
                        KnowledgeNodeSupertag.node_id,
                        KnowledgeSupertag.id,
                        KnowledgeSupertag.name,
                        KnowledgeSupertag.base_type,
                        KnowledgeSupertag.ai_instructions,
                    )
                    .join(
                        KnowledgeSupertag,
                        KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id,
                    )
                    .where(KnowledgeNodeSupertag.node_id.in_(candidate_node_ids))
                )
                tag_records: list[
                    tuple[uuid.UUID, uuid.UUID, str, str | None, str | None]
                ] = []
                for node_id, tag_id, tag_name, base_type, instructions in tag_rows.all():
                    tag_records.append(
                        (node_id, tag_id, tag_name, base_type, instructions)
                    )
                    docs_tags_by_node.setdefault(node_id, []).append(
                        {
                            "id": str(tag_id),
                            "name": tag_name,
                            "base_type": base_type or "note",
                        }
                    )
                canonical_subtree_ids: set[uuid.UUID] = set()
                if canonical_node:
                    canonical_subtree_ids.add(canonical_node.id)
                    pending = list(all_context_nodes)
                    changed = True
                    while changed:
                        changed = False
                        for candidate in pending:
                            if (
                                candidate.id not in canonical_subtree_ids
                                and candidate.parent_id in canonical_subtree_ids
                            ):
                                canonical_subtree_ids.add(candidate.id)
                                changed = True

                all_context_nodes = [
                    node
                    for node in all_context_nodes
                    if _project_information_node_allowed(
                        node,
                        canonical_subtree_ids=canonical_subtree_ids,
                        tags=docs_tags_by_node.get(node.id, []),
                    )
                ][: max(1, max_related_nodes + 1)]
                docs_nodes = list(all_context_nodes)
                node_ids = [node.id for node in all_context_nodes]
                allowed_node_ids = set(node_ids)
                tag_ids: set[uuid.UUID] = set()
                for node_id, tag_id, tag_name, base_type, instructions in tag_records:
                    if node_id not in allowed_node_ids:
                        continue
                    tag_ids.add(tag_id)
                    if instructions:
                        ai_instructions[tag_name] = (
                            base_type or "note",
                            str(tag_id),
                            instructions.strip(),
                        )

                if tag_ids and node_ids:
                    fields_result = await session.execute(
                        select(KnowledgeField)
                        .where(KnowledgeField.supertag_id.in_(tag_ids))
                        .order_by(KnowledgeField.sort_order, KnowledgeField.created_at)
                    )
                    for field in fields_result.scalars().all():
                        fields_by_tag.setdefault(field.supertag_id, []).append(field)

                values_result = await session.execute(
                    select(KnowledgeFieldValue, KnowledgeField)
                    .join(KnowledgeField, KnowledgeFieldValue.field_id == KnowledgeField.id)
                    .where(KnowledgeFieldValue.node_id.in_(node_ids))
                    .order_by(KnowledgeField.sort_order, KnowledgeField.created_at)
                )
                for value, field in values_result.all():
                    rendered = _field_value_text(value)
                    if rendered:
                        field_values_by_node.setdefault(value.node_id, []).append(
                            (field.name, rendered)
                        )

                edge_result = await session.execute(
                    select(KnowledgeEdge).where(
                        KnowledgeEdge.source_node_id.in_(node_ids),
                        KnowledgeEdge.target_node_id.in_(node_ids),
                    ).limit(60)
                )
                edges = list(edge_result.scalars().all())

                if canonical_node:
                    try:
                        from .docs_graph_service import DocsGraphService

                        async def allowed_outline_node(node: KnowledgeNode) -> bool:
                            return node.id in allowed_node_ids

                        canonical_outline_lines = await DocsGraphService(
                            session
                        ).outline_lines(
                            root=canonical_node,
                            depth=4,
                            node_filter=allowed_outline_node,
                            user_id=user_uuid,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[ContextBuilder] project Docs outline failed: %s",
                            exc,
                        )

                    revision_result = await session.execute(
                        select(KnowledgeRevision)
                        .where(KnowledgeRevision.node_id == canonical_node.id)
                        .order_by(KnowledgeRevision.created_at.desc())
                        .limit(5)
                    )
                    revisions = list(revision_result.scalars().all())

        if not tables and not docs_nodes and not qa_entries:
            return ""

        node_title_by_id = {
            node.id: (node.title or "(untitled)").strip()
            for node in all_context_nodes
        }
        lines = [
            "## Project Information Docs Source of Truth",
            (
                "Use this canonical project Docs context as grounded evidence. Read it before "
                "writing; preserve headings/blocks; update with revision change_summary and "
                "source_refs; route unsupported claims to 要確認 or candidate Q&A."
            ),
        ]
        if project:
            lines.append(
                f"- Project: {project.name} (id={project.id}, slug={project.slug})"
            )

        if canonical_node:
            tags = docs_tags_by_node.get(canonical_node.id, [])
            tag_text = ", ".join(f"#{tag['name']}:{tag['base_type']}" for tag in tags)
            lines.append(
                f"- Canonical Page: {canonical_node.title} (ref=@docs:{canonical_node.id}, updated={_iso(canonical_node.updated_at)})"
            )
            if tag_text:
                lines.append(f"- Canonical Tags: {tag_text}")
            headings = _heading_outline(canonical_node.body_text or "")
            if headings:
                lines.append("- Section Outline: " + " > ".join(headings))
            canonical_fields = field_values_by_node.get(canonical_node.id, [])
            if canonical_fields:
                lines.append("- Canonical Fields:")
                for name, rendered in canonical_fields[:20]:
                    lines.append(f"  - {name}: {_clip_text(rendered, 240)}")
            if canonical_outline_lines:
                lines.append("### Canonical Outline")
                lines.append(_clip_text("\n".join(canonical_outline_lines), docs_node_chars))
            if (canonical_node.body_text or "").strip():
                lines.append("### Canonical Body")
                lines.append(_clip_text((canonical_node.body_text or "").strip(), docs_node_chars))

        typed_nodes = [
            node
            for node in all_context_nodes
            if not canonical_node or node.id != canonical_node.id
        ][:max_related_nodes]
        if typed_nodes:
            lines.append("### Related Typed Docs Nodes")
            for node in typed_nodes:
                tags = docs_tags_by_node.get(node.id, [])
                tag_text = ", ".join(f"#{tag['name']}:{tag['base_type']}" for tag in tags[:5])
                meta = [f"ref=@docs:{node.id}"]
                if tag_text:
                    meta.append(f"tags={tag_text}")
                lines.append(f"- {node.title or '(untitled)'} ({', '.join(meta)})")
                fields = field_values_by_node.get(node.id, [])
                if fields:
                    field_text = "; ".join(
                        f"{name}={_clip_text(rendered, 80)}"
                        for name, rendered in fields[:8]
                    )
                    lines.append(f"  fields: {field_text}")
                body = _clip_text((node.body_text or "").strip(), 320)
                if body:
                    lines.append(f"  body: {body}")

        if ai_instructions:
            lines.append("### Supertag AI Instructions")
            for tag_name, (base_type, tag_id, instructions) in sorted(ai_instructions.items()):
                field_names = [
                    field.name
                    for field in fields_by_tag.get(uuid.UUID(tag_id), [])[:8]
                ]
                suffix = f" fields={', '.join(field_names)}" if field_names else ""
                lines.append(
                    f"- #{tag_name} ({base_type},{suffix}): {_clip_text(instructions, 420)}"
                )

        if qa_entries:
            lines.append("### Accepted Project Q&A")
            for entry in qa_entries:
                question = _clip_text((entry.question or "").strip(), 180)
                answer = _clip_text((entry.answer or "").strip(), 220)
                meta = f"status={entry.status}, review={entry.review_state}"
                line = f"- Q: {question} ({meta}, asked={entry.asked_count})"
                if answer:
                    line += f" / A: {answer}"
                lines.append(line)

        if edges:
            lines.append("### Docs References")
            for edge in edges[:24]:
                source = node_title_by_id.get(edge.source_node_id, str(edge.source_node_id))
                target = node_title_by_id.get(edge.target_node_id, str(edge.target_node_id))
                lines.append(
                    f"- {source} -> {target} ({edge.relation_type}, confidence={edge.confidence})"
                )

        if revisions:
            lines.append("### Canonical Revision Meta")
            for revision in revisions:
                refs = revision.source_refs_json or []
                ref_note = f", source_refs={len(refs)}" if refs else ""
                lines.append(
                    f"- {revision.created_at.isoformat() if revision.created_at else ''}: "
                    f"{_clip_text(revision.change_summary or '', 180)}{ref_note}"
                )

        if tables:
            lines.append("### Record Tables")
            for table in tables:
                description = _clip_text((table.description or "").strip(), 140)
                line = f"- {table.name}.dbtable"
                if description:
                    line += f": {description}"
                lines.append(line)

        return "\n".join(lines)
