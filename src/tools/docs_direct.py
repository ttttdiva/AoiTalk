"""Direct runtime tools for AoiTalk Docs.

Tool surface for chat agents, intentionally kept to a small, non-overlapping
set (Anthropic "writing effective tools for agents" guidance):

- Read: ``docs_search`` (find), ``docs_read`` (fetch one node in full),
  ``docs_query`` (structured tag/field query).
- Write: ``docs_create_nodes``, ``docs_update_node`` (title / description /
  fields / tags in one call), ``docs_move_node``, ``docs_archive_node``.

Field and tag mutations are folded into ``docs_update_node`` because tagging a
node and setting its fields is almost always a single logical edit.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from .core import tool
from ..services.docs_acl import apply_docs_visibility

logger = logging.getLogger(__name__)


DOCS_READ_TOOL_NAMES = {
    "inbox_search_items",
    "docs_search",
    "docs_read",
    "docs_query",
}

DOCS_MUTATION_TOOL_NAMES = {
    "docs_attach_workspace_file",
    "docs_place_workspace_file",
    "docs_ensure_inbox",
    "docs_create_nodes",
    "docs_update_node",
    "inbox_update_item",
    "docs_move_node",
    "docs_archive_node",
}


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    context = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(context.run, asyncio.run, coro).result()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _assert_generation_mutation_allowed() -> None:
    """Fence managed Docs writes after a parent steer/stop.

    A synchronous tool may continue in a worker thread after its asyncio task
    was cancelled.  The generation mutation gate is copied into that worker
    by ``asyncio.to_thread`` and remains blocked for the old attempt, so a
    late result cannot commit a stale write.
    """

    try:
        from ..llm.generation_cancellation import (
            raise_if_generation_mutation_blocked,
        )

        raise_if_generation_mutation_blocked()
    except ImportError:
        # Keep the direct tool usable in isolated legacy imports where the LLM
        # package is intentionally unavailable; production runtime always has
        # the guard module.
        return


def _record_generation_docs_resolution(
    node_ids: Any = (),
    *,
    pending_destination_parent_id: Any = None,
    pending_mutation_target_ids: Any = (),
    context: Any = (),
) -> None:
    """Persist canonical Docs identities/read facts into active continuation state.

    ``context`` is intentionally bounded and opaque.  It lets an interrupted
    Docs mutation resume with facts already read by the cancelled child,
    without asking the resumed model to search steer wording or retaining a
    full corpus payload in AgentRun metadata.
    """

    try:
        from ..services.agent_team_service import get_current_continuation_state

        state = get_current_continuation_state()
        if state is None:
            return
        resolved = list(state.resolved_node_ids or ())
        for value in node_ids if isinstance(node_ids, (list, tuple, set)) else (node_ids,):
            text = str(value or "").strip()
            if text and text not in resolved:
                resolved.append(text)
        state.resolved_node_ids = tuple(resolved)
        context_values = list(state.resolved_context or ())
        raw_context = context if isinstance(context, (list, tuple, set)) else (context,)
        for value in raw_context:
            text = str(value or "").strip()
            if not text:
                continue
            # Keep each snippet and the aggregate bounded.  Dedupe exact
            # snippets so repeated reads do not grow continuation metadata.
            text = text[:2000]
            if text not in context_values:
                context_values.append(text)
        state.resolved_context = tuple(context_values[-8:])
        targets = list(state.pending_mutation_target_ids or ())
        for value in (
            pending_mutation_target_ids
            if isinstance(pending_mutation_target_ids, (list, tuple, set))
            else (pending_mutation_target_ids,)
        ):
            text = str(value or "").strip()
            if text and text not in targets:
                targets.append(text)
        state.pending_mutation_target_ids = tuple(targets)
        parent = str(pending_destination_parent_id or "").strip()
        if parent:
            state.pending_destination_parent_id = parent
        if resolved and state.mutation_state not in {"completed", "cancelled", "interrupted"}:
            state.mutation_state = "resolved"
    except Exception:
        # Continuation evidence is advisory and must never break a Docs call.
        return


def _docs_error_envelope(exc: Exception) -> str:
    """Return a short, machine-readable Docs tool failure envelope.

    Raw database/traceback text is intentionally kept out of the LLM-facing
    result.  Agent Runtime can use ``error_code`` and ``retryable`` to avoid
    retry storms while technical diagnostics remain in the server logs.
    """

    message = str(exc or "").strip()
    lowered = message.casefold()
    if isinstance(exc, PermissionError) or "権限" in message or "denied" in lowered:
        code = "docs_access_denied"
        user_message = "Docsへのアクセス権限がありません。"
        retryable = False
    elif "ambiguous" in lowered or "曖昧" in message:
        code = "docs_ambiguous_target"
        user_message = "Docsの対象が複数候補に一致しました。対象を絞ってください。"
        retryable = False
    elif "not found" in lowered or "見つかりません" in message:
        code = "docs_not_found"
        user_message = "指定されたDocsノードが見つかりません。"
        retryable = False
    else:
        code = "docs_access_internal"
        user_message = "Docsの内部処理に失敗しました。"
        retryable = False
    return _json(
        {
            "success": False,
            "error": user_message,
            "error_code": code,
            "retryable": retryable,
        }
    )


def _parse_json_object(value: str, *, field_name: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    return parsed


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


_PROJECT_DOCS_ROOT_ERROR = (
    "A Project UUID is not a Docs parent and the selected Project's Docs root "
    "is not initialized. Call patch_project_information_doc first, then read "
    "the canonical Docs node and use its node id (or parent='project')."
)


def _parse_uuid_ref(value: Any) -> UUID | None:
    try:
        return UUID(str(value or "").strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _resolve_project_docs_parent_ref(
    parent_ref: str,
    project_obj: Any,
    *,
    default: str,
) -> str:
    """Resolve the selected Project alias without treating other project IDs as nodes."""
    ref = str(parent_ref or default).strip() or default
    if project_obj is None:
        return ref

    is_project_alias = ref.casefold() in {"project", "案件"}
    project_id = _parse_uuid_ref(getattr(project_obj, "id", None))
    is_project_uuid = project_id is not None and _parse_uuid_ref(ref) == project_id
    if not (is_project_alias or is_project_uuid):
        return ref

    knowledge_node_id = getattr(project_obj, "knowledge_node_id", None)
    if not knowledge_node_id:
        raise ValueError(_PROJECT_DOCS_ROOT_ERROR)
    return str(knowledge_node_id)


_WILDCARD_MUTATION_PROJECT_ERROR = (
    "Mutation tools require a specific authorized Project; project='*' is not allowed."
)


def _reject_wildcard_mutation_project_ref(project_ref: str = "") -> str:
    """Reject the read-only wildcard scope before any mutation can resolve nodes."""
    from ..services.turn_context import get_turn_context

    requested = str(project_ref or "").strip()
    turn_project = str(get_turn_context().project_id or "").strip()
    if requested == "*" or (not requested and turn_project == "*"):
        raise ValueError(_WILDCARD_MUTATION_PROJECT_ERROR)
    return requested


def _turn_project_scope() -> str:
    """Return the implicit Docs Project scope for the current turn.

    ``TurnContext.project_id`` always preserves the UI-selected Project for
    authorization and explicit project lookups.  It becomes an implicit Docs
    scope only while Project Context is enabled.  Legacy callers that omit the
    new flag keep their historical selected-project scope.
    """

    from ..services.turn_context import get_turn_context, is_project_context_enabled

    context = get_turn_context()
    if not is_project_context_enabled(context):
        return ""
    return str(context.project_id or "").strip()


def _node_library_scope(
    library: Any,
    ref: Any,
    *,
    project_obj: Any = None,
) -> UUID | None:
    """Resolve node UUIDs without forcing OFF turns into selected-library scope."""

    from ..services.turn_context import get_turn_context

    if (
        get_turn_context().include_project_context is False
        and project_obj is None
        and _parse_uuid_ref(ref) is not None
    ):
        return None
    return getattr(library, "id", None)


async def _preflight_project_workspace_placement(
    *,
    parent: str,
    dest: str,
    project: str,
) -> None:
    """Authorize project placement before a library copy can have side effects."""
    project_ref = _reject_wildcard_mutation_project_ref(project)
    turn_project = _turn_project_scope()
    project_ref = project_ref or turn_project

    from .file_explorer.file_explorer_tools import _authorized_workspace_path
    from .os_operations.tools import _get_user_files_root
    from ..memory.database import get_database_manager
    from ..services.docs_graph_service import DocsGraphService

    db = get_database_manager()
    session = await db.get_session()
    try:
        user_id = await _resolve_operator_user_id(session, require_context=True)
        service = DocsGraphService(session)
        project_obj = await _resolve_authorized_project_for_write(
            session, service, project_ref, user_id
        )
        library = await _resolve_docs_tool_workspace(
            session, service, user_id, project_obj, write=True
        )

        parent_ref = _resolve_project_docs_parent_ref(
            parent, project_obj, default="project"
        )
        parent_node = await _resolve_docs_tool_node(
            service,
            docs_library_id=_node_library_scope(
                library, parent_ref, project_obj=project_obj
            ),
            ref=parent_ref,
            project_id=project_obj.id if project_obj else None,
            user_id=user_id,
            required="write",
        )
        await _assert_generic_mutation_allowed(
            session, parent_node, "docs_attach_workspace_file"
        )
        if project_obj and parent_node.project_id not in {None, project_obj.id}:
            raise PermissionError("Docs parent does not belong to the selected Project")

        if project_obj:
            resolved_dest, path_error = _authorized_workspace_path(dest, "作成")
            if path_error or not resolved_dest:
                error = path_error or {
                    "success": False,
                    "error": "library destination could not be resolved",
                }
                raise ValueError(str(error.get("error") or error))
            workspace_root = _get_user_files_root().resolve()
            try:
                relative_dest = Path(resolved_dest).resolve().relative_to(workspace_root).as_posix()
            except ValueError as exc:
                raise PermissionError(
                    "library destination is outside the authorized root"
                ) from exc
            project_prefix = f"_projects/project_{project_obj.id}".casefold()
            if relative_dest.casefold() != project_prefix and not relative_dest.casefold().startswith(
                project_prefix + "/"
            ):
                raise PermissionError("library destination does not belong to the selected Project")
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def _assert_generic_mutation_allowed(session: Any, node: Any, tool_name: str) -> None:
    from ..services.managed_docs_policy import assert_managed_docs_tree_mutation_allowed

    try:
        await assert_managed_docs_tree_mutation_allowed(
            session, node, tool_name=tool_name
        )
    except PermissionError as exc:
        raise ValueError(str(exc)) from exc


def _assert_generic_update_allowed(system_key: str | None) -> None:
    """Backward-compatible unit boundary for the historical Inbox guard."""
    if str(system_key or "").startswith("project_inbox_item:"):
        raise ValueError(
            "Inbox項目はdocs_update_nodeでは更新できません。"
            "inbox_update_itemで更新してください。"
        )


async def _resolve_operator_user_id(session, *, require_context: bool = False) -> UUID:
    from ..memory.models import User
    from ..services.turn_context import get_turn_context

    turn_user_id = get_turn_context().user_id
    if turn_user_id:
        try:
            user = await session.get(User, UUID(turn_user_id))
        except (TypeError, ValueError):
            user = None
        if user is None:
            raise ValueError("Authenticated Docs user was not found.")
        return user.id
    if require_context:
        raise PermissionError("Authenticated Docs user context is required for mutation.")

    admin_result = await session.execute(select(User).where(User.role == "admin").limit(1))
    admin_user = admin_result.scalar_one_or_none()
    if admin_user:
        return admin_user.id
    first_user_result = await session.execute(select(User).limit(1))
    first_user = first_user_result.scalar_one_or_none()
    if first_user:
        return first_user.id
    raise ValueError("No local user exists to act as the Docs operator.")


async def _resolve_authorized_project(
    session,
    service,
    project_ref: str,
    user_id: UUID,
    *,
    required_permission: str = "read",
):
    from ..memory.project_repository import ProjectRepository
    ref = str(project_ref or "").strip() or _turn_project_scope()
    if ref == "*":
        return None
    if not ref:
        return None
    project = await service.resolve_project(ref)
    if project is None:
        raise ValueError(f"project not found: {ref}")
    try:
        allowed = await ProjectRepository.has_permission(
            session,
            project_id=project.id,
            user_id=user_id,
            permission="write" if required_permission == "write" else "read",
        )
    except AttributeError:
        # Lightweight fake sessions in legacy tool tests expose only ``get`` /
        # ``scalar``.  Keep that compatibility boundary without weakening real
        # AsyncSession ACL decisions (which always use the repository query).
        from ..memory.models import User

        actor = await session.get(User, user_id)
        allowed = str(getattr(actor, "role", "")).lower() == "admin"
    if not allowed:
        raise PermissionError("project access denied")
    return project


async def _resolve_authorized_project_for_write(
    session,
    service,
    project_ref: str,
    user_id: UUID,
):
    """Resolve a writable Project while keeping legacy test doubles usable.

    Older integrations monkeypatch ``_resolve_authorized_project`` with the
    original four-argument signature.  The fallback is limited to the
    signature mismatch; real ACL/DB ``TypeError`` exceptions still propagate.
    """

    try:
        return await _resolve_authorized_project(
            session,
            service,
            project_ref,
            user_id,
            required_permission="write",
        )
    except TypeError as exc:
        if "required_permission" not in str(exc):
            raise
        return await _resolve_authorized_project(session, service, project_ref, user_id)


async def _resolve_docs_tool_workspace(
    session,
    service,
    user_id: UUID,
    project_obj: Any = None,
    *,
    write: bool = False,
):
    """Resolve a Project Docs scope without mutating read-only tool calls."""

    async def _ensure_personal_library():
        # ``ensure_library`` is canonical after the Docs Library rename.  A
        # small fallback keeps legacy in-process tool doubles and pre-rename
        # integrations working without reintroducing a database alias.
        resolver = getattr(service, "ensure_library", None)
        if resolver is None:
            resolver = getattr(service, "ensure_workspace", None)
        if resolver is None:
            raise AttributeError("Docs service has no library resolver")
        return await resolver(user_id)

    if project_obj is not None:
        if write:
            resolver = getattr(service, "ensure_project_information_library", None)
            library = (
                await resolver(project_obj.id, user_id)
                if resolver is not None
                else await _ensure_personal_library()
            )
        else:
            resolver = getattr(service, "get_project_information_library", None)
            library = (
                await resolver(project_obj.id, user_id)
                if resolver is not None
                else await _ensure_personal_library()
            )
        if library is None:
            raise ValueError("Project Docs library not found")
        return library
    return await _ensure_personal_library()


async def _resolve_docs_tool_node(
    service,
    *,
    docs_library_id,
    ref: str,
    project_id=None,
    user_id: UUID | None = None,
    required: str = "read",
):
    """Resolve a node with ACL kwargs on the real graph service.

    Lightweight service doubles used by legacy tool-contract tests expose the
    original ``resolve_node(docs_library_id, ref, project_id)`` signature.  Keep
    that boundary while production ``DocsGraphService`` receives the actor and
    required permission for its transaction-time ACL checks.
    """

    kwargs: dict[str, Any] = {
        "docs_library_id": docs_library_id,
        "ref": ref,
        "project_id": project_id,
    }
    if hasattr(service, "_ensure_write_access"):
        kwargs["user_id"] = user_id
        kwargs["required"] = required
    return await service.resolve_node(**kwargs)


async def _email_node_allowed_in_turn(session, node) -> bool:
    """Keep generic cross-project Docs access while isolating archived email trees."""
    from ..memory.models import KnowledgeNode, KnowledgeNodeSupertag, KnowledgeSupertag
    from ..services.turn_context import get_turn_context

    turn_project_ref = str(get_turn_context().project_id or "").strip()
    if not turn_project_ref or node.project_id is None:
        return True
    try:
        turn_project_id = UUID(turn_project_ref)
    except (TypeError, ValueError):
        return True
    if node.project_id == turn_project_id:
        return True

    ancestor_ids = []
    current = node
    seen = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        ancestor_ids.append(current.id)
        if str(current.system_key or "").startswith("project_mail"):
            return False
        current = await session.get(KnowledgeNode, current.parent_id) if current.parent_id else None

    tagged = await session.scalar(
        select(KnowledgeNodeSupertag.node_id)
        .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id)
        .where(
            KnowledgeNodeSupertag.node_id.in_(ancestor_ids),
            KnowledgeSupertag.system_key == "email",
        )
        .limit(1)
    )
    return tagged is None


async def _semantic_docs_hits(
    docs_library_id,
    query: str,
    project_id,
    limit: int,
    *,
    session=None,
    user_id: UUID | None = None,
) -> tuple[list[UUID], Any]:
    """Return semantic node ids and lane telemetry, or ([], None) when unavailable."""
    if not str(query or "").strip():
        return [], None
    try:
        from ..rag.docs_index import search_docs_index_with_telemetry

        result = await search_docs_index_with_telemetry(
            docs_library_id=docs_library_id,
            query=query,
            project_id=project_id,
            limit=limit,
            session=session,
            user_id=user_id,
        )
        return result.node_ids, result.telemetry
    except Exception:
        return [], None


def _project_context_scope_requested(project_ref: str = "") -> bool:
    """Return whether an omitted project argument enters the scoped Docs lane.

    ``TurnContext.project_id`` remains the selected Project even when the UI
    turns Project Context off.  Only an explicitly enabled context and an
    omitted tool argument opt into the wider Project+Personal scope; explicit
    project references and the historical wildcard path stay on their legacy
    resolver below.
    """

    if str(project_ref or "").strip():
        return False
    from ..services.turn_context import get_turn_context, is_project_context_enabled

    context = get_turn_context()
    selected = str(context.project_id or "").strip()
    return bool(
        selected
        and selected != "*"
        and is_project_context_enabled(context)
    )


async def _resolve_context_docs_scope(
    session: Any,
    project_obj: Any,
    *,
    project_ref: str,
    user_id: UUID,
):
    """Resolve the identifier-only Project+Personal boundary for this turn.

    The helper is intentionally called *after* the existing Project resolver,
    so the normal Project ACL check remains the first authorization boundary.
    An empty scope is treated as denied rather than falling back to the broad
    legacy library search.
    """

    if not _project_context_scope_requested(project_ref):
        return None
    if project_obj is None or not getattr(project_obj, "id", None):
        raise PermissionError("project access denied")

    from ..services.docs_scope import DocsScopeMode, resolve_docs_scope

    scope = await resolve_docs_scope(
        session=session,
        actor_user_id=user_id,
        project_id=project_obj.id,
        mode=DocsScopeMode.PROJECT_PLUS_PERSONAL,
    )
    if not scope.project_ids:
        raise PermissionError(scope.reason or "project access denied")
    return scope


async def _search_docs_scope_with_tag(
    service: Any,
    *,
    docs_scope: Any,
    query: str,
    tag: str,
    limit: int,
    user_id: UUID,
) -> list[Any]:
    """Run the legacy tag-aware search inside an already resolved scope."""

    global_limit = min(int(limit or 0), 100)
    if global_limit <= 0:
        return []
    per_library_limit = min(global_limit, 20)
    candidates: list[Any] = []
    for library_id in getattr(docs_scope, "allowed_library_ids", ()):
        candidates.extend(
            await service.search(
                docs_library_id=library_id,
                query=query,
                tag=tag,
                limit=per_library_limit,
                user_id=user_id,
            )
        )
    merger = getattr(service, "_merge_scoped_nodes", None)
    if merger is None:
        # Real DocsGraphService always exposes the merger.  A missing method
        # on an integration double must fail closed instead of returning a
        # broad, unfiltered candidate list.
        return []
    return merger(
        candidates=candidates,
        docs_scope=docs_scope,
        limit=global_limit,
    )


def _scope_node_ids(scope: Any) -> set[UUID]:
    """Normalize the allowed canonical/related IDs from a DocsScope."""

    values: set[UUID] = set()
    for raw in (
        *getattr(scope, "canonical_node_ids", ()),
        *getattr(scope, "related_node_ids", ()),
    ):
        normalized = _parse_uuid_ref(raw)
        if normalized is not None:
            values.add(normalized)
    return values


def _scope_node_id_for_ref(ref: str, allowed_ids: set[UUID]) -> UUID | None:
    """Resolve a full UUID or short UUID prefix only within ``allowed_ids``."""

    text = str(ref or "").strip()
    parsed = _parse_uuid_ref(text)
    if parsed is not None:
        return parsed if parsed in allowed_ids else None
    normalized = text.replace("-", "").casefold()
    if len(normalized) < 8 or any(char not in "0123456789abcdef" for char in normalized):
        return None
    matches = [
        node_id
        for node_id in allowed_ids
        if str(node_id).replace("-", "").casefold().startswith(normalized)
    ]
    if len(matches) > 1:
        raise ValueError(f"node prefix is ambiguous: {text}")
    return matches[0] if matches else None


def build_docs_direct_tools() -> list:
    """Build root-level Docs tools used by chat agents."""

    @tool
    def inbox_search_items(query: str, limit: int = 5) -> str:
        """Find an existing Work Intake Inbox item across accessible real projects.

        Use for natural-language follow-ups such as "以前inboxに追加した～の件".
        The search excludes the default Inbox Space/Project and returns canonical
        full node/project UUIDs. A resolution token is returned only when one
        candidate is clearly unique; pass that token to `inbox_update_item` when
        appending new information. Never mutate an ambiguous result.
        """
        from ..memory.database import get_database_manager
        from ..services.inbox_item_resolution import (
            extract_inbox_query_terms,
            has_unique_inbox_match,
            issue_inbox_resolution_token,
        )
        from ..services.turn_context import get_turn_context
        from ..services.work_intake_docs_service import WorkIntakeDocsService

        async def _search_inbox_items():
            clean_query = str(query or "").strip()
            if not clean_query:
                raise ValueError("Inbox項目を検索する語句を入力してください。")
            requested_limit = max(1, min(int(limit or 5), 20))
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(
                    session,
                    require_context=True,
                )
                ranked = await WorkIntakeDocsService(session).search_items(
                    user_id=user_id,
                    query=clean_query,
                    # 一意判定の比較候補をlimit=1で隠せないよう最低2件取得する。
                    limit=max(2, requested_limit),
                )
                unique_match = has_unique_inbox_match(
                    ranked,
                    query=clean_query,
                )
                context = get_turn_context()
                message_ref = context.message_id or context.client_message_id
                matches: list[dict[str, Any]] = []
                visible_limit = (
                    requested_limit
                    if unique_match
                    else max(2, requested_limit)
                )
                for index, item in enumerate(ranked[:visible_limit]):
                    candidate = item.candidate
                    match = {
                        "node_id": str(candidate.node_id),
                        "project_id": str(candidate.project_id),
                        "project_name": candidate.project_name,
                        "title": candidate.title,
                        "canonical_path": (
                            "案件情報 / "
                            f"{candidate.project_name} / Inbox / {candidate.title}"
                        ),
                        "score": item.score,
                        "matched_terms": list(item.matched_terms),
                    }
                    if index == 0 and unique_match:
                        match["resolution_token"] = issue_inbox_resolution_token(
                            user_id=user_id,
                            item_id=candidate.node_id,
                            project_id=candidate.project_id,
                            session_id=context.session_id,
                            message_ref=message_ref,
                        )
                    matches.append(match)
                return {
                    "success": True,
                    "query_terms": list(extract_inbox_query_terms(clean_query)),
                    "unique_match": unique_match,
                    "matches": matches,
                    "not_found": not matches,
                    "requires_clarification": bool(matches) and not unique_match,
                }
            finally:
                await session.close()

        try:
            return _json(_run_async(_search_inbox_items()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_search(query: str, project: str = "", tag: str = "", limit: int = 20) -> str:
        """Search AoiTalk Docs (the internal outliner of notes, project info, and tasks).

        Hybrid keyword + semantic search over Docs nodes. Returns compact lines:
        `short_id | title | #tags | project | ⤷ parent`. Use `docs_read` to open a
        hit in full. This searches Docs only; use `search_past_chats` for past
        conversations.
        """
        from ..memory.database import get_database_manager
        from ..memory.models import KnowledgeNode
        from ..services.docs_graph_service import DocsGraphService

        async def _search():
            import time

            from ..rag.docs_search_telemetry import build_docs_search_telemetry
            from ..rag.docs_index import docs_rag_enabled

            started = time.perf_counter()
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(
                    session, require_context=True
                )
                service = DocsGraphService(session)
                project_obj = await _resolve_authorized_project(
                    session, service, project, user_id
                )
                docs_scope = await _resolve_context_docs_scope(
                    session,
                    project_obj,
                    project_ref=project,
                    user_id=user_id,
                )
                project_id = project_obj.id if project_obj else None
                library = None
                if docs_scope is None:
                    library = await _resolve_docs_tool_workspace(
                        session, service, user_id, project_obj
                    )
                if docs_scope is not None:
                    # The scoped graph wrapper applies the resolved node IDs
                    # after each library search.  Do not run the legacy
                    # semantic lane here: its index payload has no scope
                    # contract and could otherwise re-introduce an out-of-
                    # scope hit.  Telemetry records the intentional skip.
                    if str(tag or "").strip():
                        lexical = await _search_docs_scope_with_tag(
                            service,
                            docs_scope=docs_scope,
                            query=query,
                            tag=tag,
                            limit=limit,
                            user_id=user_id,
                        )
                    else:
                        lexical = await service.search_with_scope(
                            query=query,
                            docs_scope=docs_scope,
                            limit=limit,
                        )
                    semantic_ids, index_telemetry = [], None
                else:
                    lexical = await service.search(
                        docs_library_id=library.id,
                        query=query,
                        project_id=project_id,
                        tag=tag,
                        limit=limit,
                        user_id=user_id,
                    )
                    semantic_ids, index_telemetry = await _semantic_docs_hits(
                        library.id,
                        query,
                        project_id,
                        limit,
                        session=session,
                        user_id=user_id,
                    )
                merged = list(lexical)
                if semantic_ids and docs_scope is None:
                    seen = {node.id for node in merged}
                    extra_ids = [nid for nid in semantic_ids if nid not in seen]
                    if extra_ids:
                        extra_stmt = select(KnowledgeNode).where(
                            KnowledgeNode.id.in_(extra_ids),
                            KnowledgeNode.docs_library_id == library.id,
                            *(
                                [KnowledgeNode.project_id == project_id]
                                if project_id is not None
                                else []
                            ),
                            KnowledgeNode.archived_at.is_(None),
                        )
                        extra_stmt = apply_docs_visibility(
                            extra_stmt,
                            docs_library_id=library.id,
                            user_id=user_id,
                            library_owner_id=getattr(library, "owner_user_id", None),
                            node_model=KnowledgeNode,
                        )
                        extra_result = await session.execute(extra_stmt)
                        by_id = {n.id: n for n in extra_result.scalars().all()}
                        ordered_extra = [by_id[nid] for nid in extra_ids if nid in by_id]
                        merged = ordered_extra + merged
                merged = [
                    node
                    for node in merged
                    if await _email_node_allowed_in_turn(session, node)
                ]
                merged = merged[: max(1, min(int(limit or 20), 100))]
                _record_generation_docs_resolution([node.id for node in merged])
                telemetry = build_docs_search_telemetry(
                    query=query,
                    lexical_count=len(lexical),
                    semantic_count=len(semantic_ids),
                    merged_count=len(merged),
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    index=index_telemetry,
                    docs_rag_enabled=docs_rag_enabled(),
                )
                logger.debug("docs_search telemetry %s", telemetry.as_dict())
                return await service.format_search_results(merged, user_id=user_id)
            finally:
                await session.close()

        try:
            return _run_async(_search())
        except Exception as exc:
            return _docs_error_envelope(exc)

    @tool
    def docs_read(target: str, project: str = "", depth: int = 8) -> str:
        """Read a Docs node in full: header, description, fields, backlinks, and outline.

        `target` is a node id/prefix/title, 'today', or a project name. This is the
        detail view for a `docs_search` hit — it returns the node's description,
        current field values, nodes that reference it (backlinks), and its child
        outline down to `depth`.
        """
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _read():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(
                    session, require_context=True
                )
                service = DocsGraphService(session)
                project_obj = await _resolve_authorized_project(
                    session, service, project, user_id
                )
                docs_scope = await _resolve_context_docs_scope(
                    session,
                    project_obj,
                    project_ref=project,
                    user_id=user_id,
                )
                library = None
                if docs_scope is None:
                    library = await _resolve_docs_tool_workspace(
                        session, service, user_id, project_obj
                    )

                if docs_scope is not None:
                    allowed_ids = _scope_node_ids(docs_scope)
                    target_text = str(target or "").strip()
                    target_alias = bool(
                        project_obj
                        and target_text.casefold() in {
                            "",
                            "project",
                            "案件",
                            project_obj.name.casefold(),
                            (project_obj.slug or "").casefold(),
                        }
                    )
                    if target_alias:
                        if not project_obj.knowledge_node_id:
                            raise ValueError(
                                "project does not have a Docs information node yet"
                            )
                        scoped_node_id = _scope_node_id_for_ref(
                            str(project_obj.knowledge_node_id), allowed_ids
                        )
                    else:
                        scoped_node_id = _scope_node_id_for_ref(
                            target_text, allowed_ids
                        )
                        normalized_target = target_text.replace("-", "").casefold()
                        looks_like_id = bool(
                            len(normalized_target) >= 8
                            and all(
                                char in "0123456789abcdef"
                                for char in normalized_target
                            )
                        )
                        if scoped_node_id is None and target_text and not looks_like_id:
                            # Search titles only inside the already-resolved
                            # lanes; this supports a human title while keeping
                            # UUID/prefix resolution fail-closed.
                            title_matches = [
                                node
                                for node in await service.search_with_scope(
                                    query=target_text,
                                    docs_scope=docs_scope,
                                    limit=100,
                                )
                                if str(getattr(node, "title", "")).casefold()
                                == target_text.casefold()
                            ]
                            if len(title_matches) > 1:
                                raise ValueError(
                                    f"node title is ambiguous: {target_text}"
                                )
                            if title_matches:
                                scoped_node_id = _parse_uuid_ref(
                                    title_matches[0].id
                                )
                    if scoped_node_id is None or scoped_node_id not in allowed_ids:
                        raise PermissionError("Docs node is outside the active scope")
                    root = await _resolve_docs_tool_node(
                        service,
                        # A scoped UUID may live in the selected Project's
                        # library, a shared Personal library, or the actor's
                        # own Personal library.  The scope IDs are the
                        # discriminator; resolve_node still performs ACL.
                        docs_library_id=None,
                        ref=str(scoped_node_id),
                        project_id=None,
                        user_id=user_id,
                    )
                    if _parse_uuid_ref(getattr(root, "id", None)) not in allowed_ids:
                        raise PermissionError("Docs node is outside the active scope")
                elif project_obj and target.strip().casefold() in {
                    "",
                    "project",
                    "案件",
                    project_obj.name.casefold(),
                    (project_obj.slug or "").casefold(),
                }:
                    if not project_obj.knowledge_node_id:
                        raise ValueError("project does not have a Docs information node yet")
                    root = await _resolve_docs_tool_node(
                        service,
                        docs_library_id=library.id,
                        ref=str(project_obj.knowledge_node_id),
                        project_id=project_obj.id,
                        user_id=user_id,
                    )
                else:
                    root = await _resolve_docs_tool_node(
                        service,
                        docs_library_id=_node_library_scope(
                            library, target, project_obj=project_obj
                        ),
                        ref=target,
                        project_id=project_obj.id if project_obj else None,
                        user_id=user_id,
                    )

                if not await _email_node_allowed_in_turn(session, root):
                    raise PermissionError("project-bound Docs tools cannot access another project's email")

                if docs_scope is not None:
                    async def _scoped_node_allowed(node: Any) -> bool:
                        node_id = _parse_uuid_ref(getattr(node, "id", None))
                        return bool(
                            node_id in allowed_ids
                            and await _email_node_allowed_in_turn(session, node)
                        )

                    # Ancestor titles are path metadata and do not carry IDs
                    # that can be checked against the scope.  Omit them in
                    # the scoped lane instead of leaking an unrelated path.
                    ancestors = []
                    node_filter = _scoped_node_allowed
                else:
                    ancestors = await service.ancestor_titles(root, user_id=user_id)
                    node_filter = lambda node: _email_node_allowed_in_turn(session, node)
                fields = await service.get_node_field_values(root, user_id=user_id)
                backlinks = [
                    node
                    for node in await service.get_backlinks(root, user_id=user_id)
                    if await node_filter(node)
                ]
                outline = await service.outline_lines(
                    root=root,
                    depth=depth,
                    node_filter=node_filter,
                    user_id=user_id,
                )

                sections: list[str] = []
                header = f"# {root.title}  ({str(root.id)[:8]})"
                sections.append(header)
                if str(root.system_key or "").startswith("project_inbox_item:"):
                    from ..services.work_intake_docs_service import WorkIntakeDocsService

                    revision = await WorkIntakeDocsService(session).document_revision(root)
                else:
                    revision = root.updated_at.isoformat() if root.updated_at else ""
                if revision:
                    sections.append("revision: " + revision)
                if ancestors:
                    sections.append("path: " + " / ".join(t for t in ancestors if t))
                if root.project_id:
                    sections.append(f"project: {str(root.project_id)[:8]}")
                if root.description:
                    sections.append("\n## description\n" + str(root.description))
                if fields:
                    field_lines = "\n".join(f"- {name}: {value}" for name, value in fields.items())
                    sections.append("\n## fields\n" + field_lines)
                if backlinks:
                    link_lines = "\n".join(
                        f"- {str(node.id)[:8]} | {node.title}" for node in backlinks
                    )
                    sections.append("\n## backlinks\n" + link_lines)
                sections.append("\n## outline\n" + "\n".join(outline))
                result = "\n".join(sections)
                _record_generation_docs_resolution([root.id], context=[result])
                return result
            finally:
                await session.close()

        try:
            return _run_async(_read())
        except Exception as exc:
            return _docs_error_envelope(exc)

    @tool
    def docs_query(
        tags: str = "",
        text: str = "",
        project: str = "",
        fields_json: str = "",
        group_by: str = "",
        limit: int = 20,
    ) -> str:
        """Structured Docs query. AND over comma-separated `tags`, optional field equality.

        `fields_json` is a JSON object of field name -> expected value (e.g.
        `{"status": "done"}`). `group_by` is a field name that buckets the rows and
        adds per-group counts. Returns a count header then compact rows.
        """
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _query():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(
                    session, require_context=True
                )
                service = DocsGraphService(session)
                project_obj = await _resolve_authorized_project(
                    session, service, project, user_id
                )
                docs_scope = await _resolve_context_docs_scope(
                    session,
                    project_obj,
                    project_ref=project,
                    user_id=user_id,
                )
                library = None
                if docs_scope is None:
                    library = await _resolve_docs_tool_workspace(
                        session, service, user_id, project_obj
                    )
                field_filters = _parse_json_object(fields_json, field_name="fields_json")
                if docs_scope is not None:
                    nodes = await service.query_with_scope(
                        docs_scope=docs_scope,
                        tags=_parse_csv(tags),
                        text=text,
                        limit=limit,
                    )
                    # ``query_with_scope`` intentionally exposes only the
                    # scope lanes.  Field values are therefore filtered after
                    # the ACL-bounded result, never by a broad project-id
                    # predicate that could admit an unrelated node.
                    if field_filters:
                        filtered_nodes = []
                        expected = {
                            str(key): str(value)
                            for key, value in field_filters.items()
                        }
                        for node in nodes:
                            values = await service.get_node_field_values(
                                node, user_id=user_id
                            )
                            normalized_values = {
                                str(key).casefold(): str(value)
                                for key, value in values.items()
                            }
                            if all(
                                normalized_values.get(key.casefold(), "").casefold()
                                == value.casefold()
                                for key, value in expected.items()
                            ):
                                filtered_nodes.append(node)
                        nodes = filtered_nodes
                else:
                    nodes = await service.query_nodes(
                        docs_library_id=library.id,
                        tags=_parse_csv(tags),
                        text=text,
                        project_id=project_obj.id if project_obj else None,
                        field_filters={str(k): str(v) for k, v in field_filters.items()},
                        limit=limit,
                        user_id=user_id,
                    )
                nodes = [
                    node
                    for node in nodes
                    if await _email_node_allowed_in_turn(session, node)
                ]
                _record_generation_docs_resolution([node.id for node in nodes])
                header = f"count={len(nodes)}"
                if not group_by.strip():
                    return header + "\n" + await service.format_search_results(
                        nodes, user_id=user_id
                    )

                # Group rows by the requested field's current value.
                groups: dict[str, list] = {}
                for node in nodes:
                    values = await service.get_node_field_values(node, user_id=user_id)
                    key = values.get(group_by.strip()) or "(none)"
                    groups.setdefault(key, []).append(node)
                blocks = [f"{header} group_by={group_by.strip()}"]
                for key in sorted(groups):
                    members = groups[key]
                    blocks.append(f"\n[{key}] count={len(members)}")
                    blocks.append(
                        await service.format_search_results(members, user_id=user_id)
                    )
                return "\n".join(blocks)
            finally:
                await session.close()

        try:
            return _run_async(_query())
        except Exception as exc:
            return _docs_error_envelope(exc)

    @tool
    def docs_ensure_inbox() -> str:
        """Return the current user's root Docs Inbox, creating it when absent."""
        from ..memory.database import get_database_manager
        from ..memory.models import KnowledgeNode, DocsLibrary
        from ..services.docs_graph_service import DocsGraphService

        async def _ensure():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session, require_context=True)
                service = DocsGraphService(session)
                ensure_library = getattr(service, "ensure_library", None)
                if ensure_library is None:
                    ensure_library = getattr(service, "ensure_workspace", None)
                if ensure_library is None:
                    raise AttributeError("Docs service has no library resolver")
                library = await ensure_library(user_id)
                await session.execute(
                    select(DocsLibrary)
                    .where(DocsLibrary.id == library.id)
                    .with_for_update()
                )
                result = await session.execute(
                    select(KnowledgeNode).where(
                        KnowledgeNode.docs_library_id == library.id,
                        KnowledgeNode.parent_id.is_(None),
                        KnowledgeNode.project_id.is_(None),
                        KnowledgeNode.archived_at.is_(None),
                        KnowledgeNode.title.ilike("Inbox"),
                    ).limit(1)
                )
                node = result.scalar_one_or_none()
                created = False
                if node is None:
                    node = await service.create_node(
                        docs_library_id=library.id,
                        user_id=user_id,
                        title="Inbox",
                    )
                    created = True
                _assert_generation_mutation_allowed()
                await session.commit()
                return {
                    "success": True,
                    "created": created,
                    "id": str(node.id),
                    "short_id": str(node.id)[:8],
                    "title": node.title,
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_ensure()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_create_nodes(parent: str, outline_text: str, project: str = "") -> str:
        """Create a subtree from indented outline text.

        One node per non-empty line; leading tabs (or 4 spaces) set depth. Inline
        `#tags` are attached and `Field:: value` tokens set fields.

        Args:
            parent: Existing Docs KnowledgeNode UUID, short id, or title; `today` selects the default day node and `project`/`案件` selects the selected Project's canonical Docs page. A Project UUID is not a Docs node id and must not be passed as `parent`.
            outline_text: Indented outline text; one non-empty line becomes one
                node and indentation sets the depth.
            project: Authorized Project UUID, slug, or name. When omitted, an ON Project Context turn uses the selected Project; OFF uses general Docs scope. If the Project root is not initialized, call `patch_project_information_doc` first.
        """
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _create():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session, require_context=True)
                project_ref = _reject_wildcard_mutation_project_ref(project)
                service = DocsGraphService(session)
                project_obj = await _resolve_authorized_project_for_write(
                    session, service, project_ref, user_id
                )
                library = await _resolve_docs_tool_workspace(
                    session, service, user_id, project_obj, write=True
                )
                parent_ref = _resolve_project_docs_parent_ref(
                    parent, project_obj, default="today"
                )
                parent_node = await _resolve_docs_tool_node(
                    service,
                    docs_library_id=_node_library_scope(
                        library, parent_ref, project_obj=project_obj
                    ),
                    ref=parent_ref,
                    project_id=project_obj.id if project_obj else None,
                    user_id=user_id,
                    required="write",
                )
                await _assert_generic_mutation_allowed(
                    session, parent_node, "docs_create_nodes"
                )
                _record_generation_docs_resolution(
                    [parent_node.id],
                    pending_destination_parent_id=parent_node.id,
                )
                nodes = await service.create_nodes_from_outline(
                    docs_library_id=library.id,
                    user_id=user_id,
                    parent=parent_node,
                    outline_text=outline_text,
                    project_id=project_obj.id if project_obj else parent_node.project_id,
                )
                _assert_generation_mutation_allowed()
                await session.commit()
                return {
                    "success": True,
                    "created": [
                        {
                            "id": str(node.id),
                            "short_id": str(node.id)[:8],
                            "title": node.title,
                            "created_at": node.created_at,
                            "updated_at": node.updated_at,
                        }
                        for node in nodes
                    ],
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_create()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_attach_workspace_file(
        file_path: str,
        parent: str = "project",
        label: str = "",
        project: str = "",
    ) -> str:
        """Attach one clickable library-file reference under a Docs parent.

        The operation is idempotent for the same parent and normalized path.
        It verifies that the file exists in an authorized library and, when
        a Project is active, that the Docs parent and file belong to it.

        Args:
            file_path: Existing file path inside the authorized library.
            parent: Docs KnowledgeNode UUID, short id, or title; `project`/`案件` selects the selected Project's canonical Docs page. A Project UUID is not a Docs parent.
            label: Optional label shown in the clickable Docs reference.
            project: Authorized Project UUID, slug, or name. When omitted, an ON Project Context turn uses the selected Project; OFF uses general Docs scope. If its canonical Docs root is not initialized, call patch_project_information_doc first.
        """
        from .file_explorer.file_explorer_tools import _authorized_workspace_path
        from .os_operations.tools import _get_user_files_root
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        try:
            _reject_wildcard_mutation_project_ref(project)
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

        resolved, path_error = _authorized_workspace_path(file_path, "読み取り")
        if path_error or not resolved:
            return _json(path_error or {"success": False, "error": "file path could not be resolved"})
        target = Path(resolved)
        if not target.is_file():
            return _json({"success": False, "error": "library file was not found"})
        workspace_root = _get_user_files_root().resolve()
        try:
            relative_path = target.resolve().relative_to(workspace_root).as_posix()
        except ValueError:
            return _json({"success": False, "error": "library file path is outside the authorized root"})
        if any(character in relative_path for character in ("|", "]", "\r", "\n")):
            return _json(
                {
                    "success": False,
                    "error": "library file path contains unsupported Docs link characters",
                }
            )

        clean_label = (str(label or "").strip() or target.name).replace("]", "").replace("|", "")[:200]

        async def _attach():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session, require_context=True)
                project_ref = _reject_wildcard_mutation_project_ref(project)
                service = DocsGraphService(session)
                project_obj = await _resolve_authorized_project_for_write(
                    session,
                    service,
                    project_ref,
                    user_id,
                )
                library = await _resolve_docs_tool_workspace(
                    session, service, user_id, project_obj, write=True
                )
                parent_ref = _resolve_project_docs_parent_ref(
                    parent, project_obj, default="project"
                )
                parent_node = await _resolve_docs_tool_node(
                    service,
                    docs_library_id=_node_library_scope(
                        library, parent_ref, project_obj=project_obj
                    ),
                    ref=parent_ref,
                    project_id=project_obj.id if project_obj else None,
                    user_id=user_id,
                    required="write",
                )
                await _assert_generic_mutation_allowed(
                    session, parent_node, "docs_attach_workspace_file"
                )
                if project_obj:
                    project_prefix = f"_projects/project_{project_obj.id}/".casefold()
                    if not relative_path.casefold().startswith(project_prefix):
                        raise PermissionError("library file does not belong to the selected Project")
                    if parent_node.project_id not in {None, project_obj.id}:
                        raise PermissionError("Docs parent does not belong to the selected Project")

                stable_input = f"{parent_node.id}\0{relative_path.casefold()}"
                system_key = (
                    "workspace_file_reference:"
                    + hashlib.sha256(stable_input.encode("utf-8")).hexdigest()[:40]
                )
                node_title = f"[[file:{relative_path}|{clean_label}]]"[:500]
                node, created = await service.ensure_system_node(
                    docs_library_id=library.id,
                    user_id=user_id,
                    title=node_title,
                    parent=parent_node,
                    project_id=project_obj.id if project_obj else parent_node.project_id,
                    system_key=system_key,
                    body_json={
                        "format": "workspace_file_reference",
                        "file_path": relative_path,
                    },
                    source_refs=[{"type": "workspace_file", "path": relative_path}],
                )
                _assert_generation_mutation_allowed()
                await session.commit()
                return {
                    "success": True,
                    "created": created,
                    "node_id": str(node.id),
                    "parent_id": str(parent_node.id),
                    "title": node_title,
                    "file_path": relative_path,
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_attach()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_place_workspace_file(
        src: str,
        dest: str,
        parent: str = "project",
        label: str = "",
        project: str = "",
    ) -> str:
        """Copy one file and attach its Docs reference as one idempotent action.

        Use this after one ``list_workspace_tree`` call when the request asks to
        both place a library file and update Docs. ``dest`` is the destination
        directory. ``parent`` defaults to the selected Project's Docs root.
        Repeating the same call reuses both the identical file and Docs node.

        Args:
            src: Source library file path.
            dest: Destination library directory.
            parent: Docs KnowledgeNode UUID, short id, or title; `project`/`案件` selects the selected Project's canonical Docs page. A Project UUID is not a Docs parent.
            label: Optional label shown in the clickable Docs reference.
            project: Authorized Project UUID, slug, or name. When omitted, an ON Project Context turn uses the selected Project; OFF uses general Docs scope. If its canonical Docs root is not initialized, call patch_project_information_doc first.
        """
        from .file_explorer.file_explorer_tools import _copy_workspace_item_impl

        try:
            _run_async(
                _preflight_project_workspace_placement(
                    parent=parent,
                    dest=dest,
                    project=project,
                )
            )
        except Exception as exc:
            return _json(
                {
                    "success": False,
                    "stage": "authorization",
                    "error": str(exc),
                }
            )

        copy_result = _copy_workspace_item_impl(src=src, dest=dest)
        if not isinstance(copy_result, dict) or not copy_result.get("success"):
            return _json({"success": False, "stage": "copy", "copy": copy_result})
        placed_path = str(copy_result.get("new_path") or "").strip()
        if not placed_path:
            return _json(
                {
                    "success": False,
                    "stage": "copy",
                    "error": "copy result did not include new_path",
                    "copy": copy_result,
                }
            )
        attach_raw = docs_attach_workspace_file.function(
            parent=parent,
            file_path=placed_path,
            label=label,
            project=project,
        )
        try:
            attach_result = json.loads(attach_raw)
        except (TypeError, json.JSONDecodeError):
            attach_result = {"success": False, "error": str(attach_raw)}
        if not attach_result.get("success"):
            return _json(
                {
                    "success": False,
                    "partial_success": True,
                    "stage": "docs",
                    "file_path": placed_path,
                    "copy": copy_result,
                    "docs": attach_result,
                }
            )
        return _json(
            {
                "success": True,
                "created": bool(copy_result.get("created"))
                or bool(attach_result.get("created")),
                "file_path": placed_path,
                "copy": copy_result,
                "docs": attach_result,
            }
        )

    @tool
    def docs_update_node(
        node_id: str,
        title: str = "",
        description: str = "",
        fields_json: str = "",
        add_tags: str = "",
        remove_tags: str = "",
    ) -> str:
        """Update a Docs node in one call: title, description, fields, and tags.

        `fields_json` is a JSON object of field name -> value (fields must be
        defined by one of the node's tags; empty value clears). `add_tags` /
        `remove_tags` are comma-separated tag names; adding `#Task` binds a native
        task, removing it unlinks without deleting.

        Args:
            node_id: Existing Docs KnowledgeNode UUID, short id, or title. A Project UUID is not a Docs node id; read the canonical Docs page first and use the returned node id.
            title: Replacement title, or empty to leave the title unchanged.
            description: Replacement description, or empty to leave it unchanged.
            fields_json: JSON object mapping field names to values; an empty value
                clears a field.
            add_tags: Comma-separated tag names to add.
            remove_tags: Comma-separated tag names to remove.
        """
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _update():
            values = _parse_json_object(fields_json, field_name="fields_json") if fields_json.strip() else {}
            add_list = _parse_csv(add_tags)
            remove_list = _parse_csv(remove_tags)
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session, require_context=True)
                project_ref = _reject_wildcard_mutation_project_ref()
                service = DocsGraphService(session)
                project_obj = await _resolve_authorized_project_for_write(
                    session, service, project_ref, user_id
                )
                library = await _resolve_docs_tool_workspace(
                    session, service, user_id, project_obj, write=True
                )
                node_uuid = _parse_uuid_ref(node_id)
                selected_project_uuid = _parse_uuid_ref(
                    getattr(project_obj, "id", None)
                )
                if (
                    project_obj is not None
                    and node_uuid is not None
                    and node_uuid == selected_project_uuid
                ):
                    # Project and Docs nodes use separate UUID namespaces.  The
                    # selected Project was already authorization-checked above;
                    # do not probe arbitrary Projects and leak their existence.
                    raise ValueError(
                        "A Project UUID cannot be used as "
                        "docs_update_node.node_id; use the canonical "
                        "Docs node id instead."
                    )
                node = await _resolve_docs_tool_node(
                    service,
                    docs_library_id=_node_library_scope(
                        library, node_id, project_obj=project_obj
                    ),
                    ref=node_id,
                    project_id=project_obj.id if project_obj else None,
                    user_id=user_id,
                    required="write",
                )
                await _assert_generic_mutation_allowed(
                    session, node, "docs_update_node"
                )
                _record_generation_docs_resolution(
                    [node.id], pending_mutation_target_ids=[node.id]
                )
                node_library_id = getattr(node, "docs_library_id", None) or library.id
                changed: dict[str, Any] = {}

                if title.strip() or description.strip():
                    await service.update_node(
                        node=node,
                        user_id=user_id,
                        title=title if title.strip() else None,
                        description=description if description.strip() else None,
                    )
                    if title.strip():
                        changed["title"] = node.title
                    if description.strip():
                        changed["description"] = "updated"

                added: list[str] = []
                for tag_name in add_list:
                    supertag = await service.resolve_supertag(
                        docs_library_id=node_library_id, tag=tag_name, create=True
                    )
                    if await service.add_tag(node=node, tag=supertag, user_id=user_id):
                        added.append(supertag.name)
                if added:
                    changed["added_tags"] = added

                removed: list[str] = []
                for tag_name in remove_list:
                    supertag = await service.resolve_supertag(
                        docs_library_id=node_library_id, tag=tag_name, create=False
                    )
                    if await service.remove_tag(node=node, tag=supertag, user_id=user_id):
                        removed.append(supertag.name)
                if removed:
                    changed["removed_tags"] = removed

                if values:
                    updated = await service.set_fields(node=node, values=values, user_id=user_id)
                    changed["fields"] = updated

                _assert_generation_mutation_allowed()
                await session.commit()
                return {
                    "success": True,
                    "id": str(node.id),
                    "short_id": str(node.id)[:8],
                    "title": node.title,
                    "updated_at": node.updated_at,
                    "changed": changed,
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_update()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def inbox_update_item(
        node_id: str,
        update_text: str = "",
        document_json: str = "",
        expected_revision: str = "",
        status: str = "",
        task_status: str = "",
        ensure_task: bool = False,
        resolution_token: str = "",
    ) -> str:
        """Re-synthesize one existing Inbox item from additional information.

        Use when the user supplied the exact Inbox Docs UUID, or after
        `inbox_search_items` returned one unique result and a resolution token.
        First call `docs_read`, then combine its existing document with the new
        information and pass the full replacement document in `document_json`.
        Copy the `revision:` value returned by `docs_read` into
        `expected_revision`; stale revisions are rejected instead of overwriting
        a newer update. The JSON schema is
        `{"title":"...","overview":[{"text":"...","sources":["source-node-uuid"]}],`
        `"sections":[{"title":"content-dependent","items":[...]}]}`.
        Do not create fixed reference/update-history sections. Source UUIDs must
        be exact nodes in the selected project and stay local to the supported
        claim. The tool never creates a new Inbox item. Status/Task-only updates
        may omit `document_json`. Set `ensure_task` when the user asks to turn a
        saved-only Inbox item into work; `task_status` sets the linked Task status.
        """
        from ..memory.database import get_database_manager
        from ..memory.models import KnowledgeNode, Task
        from ..services.inbox_document_service import parse_inbox_document
        from ..services.task_management_service import TaskManagementService
        from ..services.turn_context import (
            get_turn_context,
            is_docs_reference_in_turn,
        )
        from ..services.work_intake_docs_service import (
            INBOX_STATUS_VALUES,
            WorkIntakeDocsService,
            inbox_display_id,
        )
        from ..services.inbox_item_resolution import verify_inbox_resolution_token

        async def _update_inbox():
            context = get_turn_context()
            item_id = UUID(str(node_id).strip())
            resolved = None
            if str(resolution_token or "").strip():
                if not context.user_id:
                    raise PermissionError(
                        "resolution tokenの検証には認証済みユーザーが必要です。"
                    )
                resolved = verify_inbox_resolution_token(
                    resolution_token,
                    user_id=UUID(str(context.user_id)),
                    session_id=context.session_id,
                    message_ref=context.message_id or context.client_message_id,
                )
                if resolved.item_id != item_id:
                    raise PermissionError(
                        "resolution tokenのInbox項目とnode_idが一致しません。"
                    )
            if resolved is not None:
                project_id = resolved.project_id
            elif context.project_id:
                project_id = UUID(context.project_id)
            else:
                raise PermissionError(
                    "Inbox項目の更新には選択中のプロジェクト、または"
                    "有効なresolution tokenが必要です。"
                )
            if resolved is None and not is_docs_reference_in_turn(str(item_id)):
                raise PermissionError(
                    "Inbox項目の更新には、現在のメッセージでコピーした"
                    "完全UUID参照、またはinbox_search_itemsの一意な"
                    "resolution tokenを明示してください。"
                )
            source_refs: list[dict[str, str]] = []
            if context.session_id:
                source_refs.append(
                    {"type": "conversation_session", "id": context.session_id}
                )
            if context.message_id:
                source_refs.append(
                    {"type": "conversation_message", "id": context.message_id}
                )
            elif context.client_message_id:
                source_refs.append(
                    {"type": "conversation_client_message", "id": context.client_message_id}
                )
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(
                    session, require_context=True
                )
                task_service = TaskManagementService()
                await task_service.require_project_permission(
                    session,
                    project_id=project_id,
                    user_id=user_id,
                    permission="write",
                )
                item = await session.get(KnowledgeNode, item_id)
                if (
                    item is None
                    or getattr(item, "project_id", project_id) != project_id
                    or not str(
                        getattr(
                            item,
                            "system_key",
                            f"project_inbox_item:{project_id}:verified",
                        )
                        or ""
                    ).startswith(
                        f"project_inbox_item:{project_id}:"
                    )
                ):
                    raise ValueError("指定したDocsノードはInbox項目ではありません。")
                if status.strip() and status.strip() not in INBOX_STATUS_VALUES:
                    raise ValueError("Inbox項目の対応状態が不正です。")
                if (
                    update_text.strip()
                    and not document_json.strip()
                ):
                    raise ValueError(
                        "追加情報は更新履歴へ追記できません。docs_readの既存内容と"
                        "統合した完全なdocument_jsonを指定してください。"
                    )
                result_title = str(getattr(item, "title", "") or "Inbox項目")
                document_replaced = False
                if document_json.strip():
                    if not expected_revision.strip():
                        raise ValueError(
                            "document_jsonで更新する場合は、直前のdocs_readが返した"
                            "expected_revisionを指定してください。"
                        )
                    payload = _parse_json_object(
                        document_json,
                        field_name="document_json",
                    )

                    def collect_source_keys(value: Any) -> list[str]:
                        found: list[str] = []
                        if isinstance(value, dict):
                            raw_sources = value.get("sources")
                            if isinstance(raw_sources, list):
                                found.extend(str(source) for source in raw_sources)
                            for child in value.values():
                                found.extend(collect_source_keys(child))
                        elif isinstance(value, list):
                            for child in value:
                                found.extend(collect_source_keys(child))
                        return found

                    source_keys = list(dict.fromkeys(collect_source_keys(payload)))
                    source_nodes: dict[str, UUID] = {}
                    for source_key in source_keys:
                        try:
                            source_nodes[source_key] = UUID(source_key)
                        except ValueError as exc:
                            raise ValueError(
                                "document_jsonのsourcesには完全なDocs UUIDだけを"
                                "指定してください。"
                            ) from exc
                    document = parse_inbox_document(
                        payload,
                        allowed_source_keys=source_keys,
                    )
                    replaced = await WorkIntakeDocsService(session).replace_document(
                        item_id=item_id,
                        project_id=project_id,
                        user_id=user_id,
                        document=document,
                        source_nodes=source_nodes,
                        status=status,
                        source_refs=source_refs,
                        expected_revision=expected_revision.strip(),
                    )
                    result_title = replaced.title
                    document_replaced = True
                else:
                    if status.strip():
                        await WorkIntakeDocsService(session).docs.set_fields(
                            node=item,
                            values={"inbox_status": status.strip()},
                            user_id=user_id,
                        )
                task = await session.scalar(
                    select(Task).where(
                        Task.knowledge_node_id == item_id,
                        Task.project_id == project_id,
                        Task.deleted_at.is_(None),
                        Task.archived_at.is_(None),
                    )
                )
                created_task: dict[str, Any] | None = None
                if ensure_task and task is None:
                    created_task = await task_service.create_task(
                        session,
                        user_id=user_id,
                        project_id=project_id,
                        knowledge_node_id=item_id,
                        title=result_title,
                        description=(
                            f"Inbox項目 {inbox_display_id(item_id)} から作成されたタスクです。"
                        ),
                        status=task_status.strip() or "in_progress",
                        source="work_intake",
                        task_metadata={
                            "work_intake": {
                                "inbox_item_id": str(item_id),
                                "promoted_from_inbox": True,
                            }
                        },
                        commit=False,
                    )
                    await WorkIntakeDocsService(session).bind_task(
                        item_id=item_id,
                        task_id=UUID(str(created_task["id"])),
                        user_id=user_id,
                    )
                    task = await session.get(Task, UUID(str(created_task["id"])))
                elif task_status.strip():
                    if task is None:
                        raise ValueError(
                            "このInbox項目に紐付くタスクが見つかりません。"
                            "新しくタスク化する場合はensure_taskを指定してください。"
                        )
                    await task_service.update_task(
                        session,
                        user_id=user_id,
                        task_id=task.id,
                        updates={"status": task_status.strip()},
                        commit=False,
                    )
                _assert_generation_mutation_allowed()
                await session.commit()
                if created_task is not None:
                    await task_service._broadcast("task_created", created_task)
                return {
                    "success": True,
                    "id": str(item_id),
                    "inbox_id": inbox_display_id(item_id),
                    "title": result_title,
                    "document_replaced": document_replaced,
                    "task_created": created_task is not None,
                    "task_updated": bool(task_status.strip()) and task is not None,
                    "task_id": str(task.id) if task is not None else None,
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_update_inbox()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_move_node(node_id: str, new_parent: str, leave_reference: bool = False) -> str:
        """Move a Docs node under another node, optionally leaving a placement reference at the old parent."""
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _move():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session, require_context=True)
                project_ref = _reject_wildcard_mutation_project_ref()
                service = DocsGraphService(session)
                project_obj = await _resolve_authorized_project_for_write(
                    session, service, project_ref, user_id
                )
                library = await _resolve_docs_tool_workspace(
                    session, service, user_id, project_obj, write=True
                )
                project_id = project_obj.id if project_obj else None
                node = await _resolve_docs_tool_node(
                    service,
                    docs_library_id=_node_library_scope(
                        library, node_id, project_obj=project_obj
                    ),
                    ref=node_id,
                    project_id=project_id,
                    user_id=user_id,
                    required="write",
                )
                parent = await _resolve_docs_tool_node(
                    service,
                    docs_library_id=_node_library_scope(
                        library, new_parent, project_obj=project_obj
                    ),
                    ref=new_parent,
                    project_id=project_id,
                    user_id=user_id,
                    required="write",
                )
                await _assert_generic_mutation_allowed(
                    session, node, "docs_move_node"
                )
                await _assert_generic_mutation_allowed(
                    session, parent, "docs_move_node"
                )
                await service.move_node(
                    node=node,
                    new_parent=parent,
                    user_id=user_id,
                    leave_reference=leave_reference,
                )
                _assert_generation_mutation_allowed()
                await session.commit()
                return {
                    "success": True,
                    "id": str(node.id),
                    "parent_id": str(parent.id),
                    "title": node.title,
                    "updated_at": node.updated_at,
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_move()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_archive_node(node_id: str) -> str:
        """Archive a Docs node and its subtree without hard-deleting it."""
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _archive():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session, require_context=True)
                project_ref = _reject_wildcard_mutation_project_ref()
                service = DocsGraphService(session)
                project_obj = await _resolve_authorized_project_for_write(
                    session, service, project_ref, user_id
                )
                library = await _resolve_docs_tool_workspace(
                    session, service, user_id, project_obj, write=True
                )
                node = await _resolve_docs_tool_node(
                    service,
                    docs_library_id=_node_library_scope(
                        library, node_id, project_obj=project_obj
                    ),
                    ref=node_id,
                    project_id=project_obj.id if project_obj else None,
                    user_id=user_id,
                    required="write",
                )
                await _assert_generic_mutation_allowed(
                    session, node, "docs_archive_node"
                )
                # node だけ archive すると、outline から消えたのに検索へ残る孤児ができる。
                await service.archive_subtree(root=node, user_id=user_id)
                _assert_generation_mutation_allowed()
                await session.commit()
                return {
                    "success": True,
                    "id": str(node.id),
                    "title": node.title,
                    "archived_at": node.archived_at,
                    "updated_at": node.updated_at,
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_archive()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    return [
        inbox_search_items,
        docs_search,
        docs_read,
        docs_query,
        docs_ensure_inbox,
        docs_create_nodes,
        docs_attach_workspace_file,
        docs_place_workspace_file,
        docs_update_node,
        inbox_update_item,
        docs_move_node,
        docs_archive_node,
    ]
