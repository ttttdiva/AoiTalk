"""Knowledge Workspace tools for LLM function calling."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import logging
import uuid
from typing import Optional

from ..core import tool

logger = logging.getLogger(__name__)

_current_project_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "knowledge_current_project_id",
    default=None,
)


def set_current_project_context(project_id: Optional[str] = None) -> None:
    """Set the current project context for Knowledge Workspace operations."""
    _current_project_id.set(project_id)
    if project_id:
        logger.debug("Knowledge project context set: %s", project_id)
    else:
        logger.debug("Knowledge project context cleared")


def get_current_project_context() -> Optional[str]:
    """Return the current project context."""
    return _current_project_id.get()


def _run_async_in_thread(coro):
    # Tool calls execute in a short-lived worker thread.  ContextVars do not
    # implicitly cross that boundary, so capture the request context at the
    # call site before starting the event loop.
    context = contextvars.copy_context()

    def run_in_loop():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return context.run(loop.run_until_complete, coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(run_in_loop).result()


def _active_turn():
    # get_turn_context() supplies an anonymous default.  We must distinguish
    # that default from an explicitly installed anonymous turn: only the former
    # may use the legacy OS/project context. No public presence API exists yet.
    from ...services.turn_context import _current_turn_context

    return _current_turn_context.get(None)


def _user_context() -> tuple[Optional[uuid.UUID], Optional[bool]]:
    """Resolve identity before opening a session; None role requires DB lookup."""
    try:
        from ..os_operations.tools import get_current_user_context

        context = get_current_user_context()
    except Exception:
        context = {}
    turn = _active_turn()
    raw_user_id = turn.user_id if turn is not None else context.get("user_id")
    if not raw_user_id:
        # Explicit personal/unauthenticated compatibility context only. An
        # actorless server turn cannot inherit an earlier user's admin grant.
        if not context.get("user_id") and context.get("is_admin") is True:
            # Personal single-user dispatch explicitly grants this trusted
            # actorless authority. A tool-call ID may install an anonymous
            # TurnContext around it; that must not revoke the server grant.
            return None, True
        raise PermissionError("Authenticated Knowledge user context is required")
    actor_user_id = uuid.UUID(str(raw_user_id).strip())
    try:
        os_user_id = uuid.UUID(str(context.get("user_id") or "").strip())
    except ValueError:
        os_user_id = None
    is_admin = (
        context.get("is_admin") is True if os_user_id == actor_user_id else None
    )
    return actor_user_id, is_admin


async def _resolve_admin(session, actor_user_id, is_admin) -> bool:
    if is_admin is not None:
        return is_admin
    from ...memory.models import User

    actor = await session.get(User, actor_user_id, populate_existing=True)
    if (
        actor is None
        or actor.id != actor_user_id
        or actor.is_active is not True
    ):
        raise PermissionError("Authenticated Knowledge user is unavailable")
    return str(actor.role or "").strip().lower() == "admin"


def _project_scope(project_id: Optional[str] = None) -> Optional[uuid.UUID]:
    from ...services.turn_context import is_project_context_enabled

    turn = _active_turn()
    requested = str(project_id or "").strip()
    if turn is not None and turn.strict_project_scope:
        selected = str(turn.project_id or "").strip()
        if not selected or selected == "*":
            raise PermissionError("Strict Knowledge scope requires a project")
        selected_id = uuid.UUID(selected)
        if requested and (requested == "*" or uuid.UUID(requested) != selected_id):
            raise PermissionError("Knowledge project override exceeds strict scope")
        return selected_id
    if turn is not None:
        implicit = turn.project_id if is_project_context_enabled(turn) else None
    else:
        implicit = get_current_project_context()
    selected = requested or str(implicit or "").strip()
    # An explicit wildcard may widen only a non-strict turn. Source ACLs still
    # apply in the service. Empty overrides inherit, rather than widen, scope.
    return uuid.UUID(selected) if selected and selected != "*" else None


def _require_unrestricted_read_scope() -> None:
    turn = _active_turn()
    if turn is not None and turn.strict_project_scope:
        # These tools have no project discriminator. Match the exposure guard
        # even when a caller executes the public definition directly.
        raise PermissionError("Knowledge tool cannot enforce strict project scope")


async def _search_async(query: str, top_n: int) -> str:
    from ...knowledge.service import KnowledgeSearchFilters, KnowledgeService
    from ...memory.database import get_database_manager

    actor_user_id, is_admin = _user_context()
    project_id = _project_scope()
    if top_n < 1:
        raise ValueError("top_n must be at least 1")
    db = get_database_manager()
    session = await db.get_session()
    try:
        is_admin = await _resolve_admin(session, actor_user_id, is_admin)
        results = await KnowledgeService.search(
            session,
            query=query,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
            filters=KnowledgeSearchFilters(project_id=project_id),
            limit=top_n,
        )
        if not results:
            return "関連するナレッジ文書が見つかりませんでした。"
        lines = []
        for index, item in enumerate(results, start=1):
            document = item["document"]
            source = item["source"]
            chunk = item["chunk"]
            heading = " > ".join(chunk.get("heading_path") or [])
            citation = f"{source['name']} / {document['path']}"
            if heading:
                citation = f"{citation} / {heading}"
            lines.append(
                f"{index}. {citation}\n"
                f"score={item['score']:.2f}\n"
                f"{chunk['text']}"
            )
        return "**Knowledge検索結果:**\n\n" + "\n\n---\n\n".join(lines)
    finally:
        await session.close()


async def _query_async(
    operation: str,
    source_id: Optional[str],
    project_id: Optional[str],
    tags: Optional[list[str]],
    extension: Optional[str],
    path_prefix: Optional[str],
    status: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    order_by: str,
    order: str,
    group_by: Optional[str],
    limit: int,
    offset: int = 0,
) -> str:
    from ...knowledge.service import (
        KNOWLEDGE_QUERY_GROUP_FIELDS,
        KNOWLEDGE_QUERY_OPERATIONS,
        KNOWLEDGE_QUERY_ORDER_DIRECTIONS,
        KNOWLEDGE_QUERY_ORDER_FIELDS,
        KnowledgeQueryFilters,
        KnowledgeService,
    )
    from ...memory.database import get_database_manager

    actor_user_id, is_admin = _user_context()
    filters = KnowledgeQueryFilters(
        source_id=uuid.UUID(str(source_id).strip()) if source_id else None,
        project_id=_project_scope(project_id),
        tags=tuple(tags or ()),
        extension=extension,
        path_prefix=path_prefix,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    if limit < 1:
        raise ValueError("limit must be positive")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    operation = str(operation or "list").strip().lower()
    order_by = str(order_by or "date").strip().lower()
    order = str(order or "desc").strip().lower()
    if operation not in KNOWLEDGE_QUERY_OPERATIONS:
        raise ValueError("Invalid Knowledge query operation")
    if order_by not in KNOWLEDGE_QUERY_ORDER_FIELDS or order not in KNOWLEDGE_QUERY_ORDER_DIRECTIONS:
        raise ValueError("Invalid Knowledge query ordering")
    if group_by is not None:
        group_by = str(group_by).strip().lower()
        if group_by not in KNOWLEDGE_QUERY_GROUP_FIELDS:
            raise ValueError("Invalid Knowledge query group_by")
    if (operation == "group") != (group_by is not None):
        raise ValueError("group_by is required only for group operations")
    # Reuse the service's pure date/status/filter validation before acquiring
    # any session. The service also validates independently for other callers.
    filters, _, _, _ = KnowledgeService._normalize_query_filters(filters)
    db = get_database_manager()
    session = await db.get_session()
    try:
        is_admin = await _resolve_admin(session, actor_user_id, is_admin)
        result = await KnowledgeService.structured_query(
            session,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
            operation=operation,
            filters=filters,
            order_by=order_by,
            order=order,
            group_by=group_by,
            limit=limit,
            offset=offset,
        )
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    finally:
        await session.close()


async def _status_async() -> str:
    from ...knowledge.service import KnowledgeService
    from ...memory.database import get_database_manager

    actor_user_id, is_admin = _user_context()
    _require_unrestricted_read_scope()
    db = get_database_manager()
    session = await db.get_session()
    try:
        is_admin = await _resolve_admin(session, actor_user_id, is_admin)
        sources = await KnowledgeService.list_sources(
            session, actor_user_id=actor_user_id, is_admin=is_admin
        )
        if not sources:
            return "Knowledge Source はまだ登録されていません。"
        lines = [
            f"- {source.name}: {source.status} / documents={source.document_count or 0} / chunks={source.chunk_count or 0}"
            for source in sources
        ]
        return "**Knowledge Sources:**\n" + "\n".join(lines)
    finally:
        await session.close()


async def _read_async(document_id: str) -> str:
    from ...knowledge.service import KnowledgeService
    from ...memory.database import get_database_manager

    actor_user_id, is_admin = _user_context()
    _require_unrestricted_read_scope()
    document_uuid = uuid.UUID(str(document_id).strip())
    db = get_database_manager()
    session = await db.get_session()
    try:
        is_admin = await _resolve_admin(session, actor_user_id, is_admin)
        payload = await KnowledgeService.read_document(
            session,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
            document_id=document_uuid,
        )
        document = payload["document"]
        return f"**{document['path']}**\n\n{payload['content']}"
    finally:
        await session.close()


@tool
def knowledge_search(query: str, top_n: int = 5) -> str:
    """Knowledge Workspaceから関連文書を検索する。

    外部Markdownや案件フォルダなど、登録済みKnowledge Sourceの文書を
    ユーザー権限と現在のプロジェクト文脈に従って検索します。
    """
    try:
        return _run_async_in_thread(_search_async(query, int(top_n)))
    except Exception as exc:
        logger.exception("Knowledge search failed")
        return f"Knowledge検索でエラーが発生しました: {exc}"


@tool
def knowledge_query(
    operation: str = "list",
    source_id: Optional[str] = None,
    project_id: Optional[str] = None,
    tags: Optional[list[str]] = None,
    extension: Optional[str] = None,
    path_prefix: Optional[str] = None,
    status: Optional[str] = "active",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    order_by: str = "date",
    order: str = "desc",
    group_by: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Knowledge文書を条件付きで正確に集計・一覧・グループ化する。

    ``knowledge_search`` の本文関連検索とは別に、count/list/group の構造化
    問い合わせを実行します。列挙値はスキーマに加えサーバー側でも検証します。
    文書日付 document_date は frontmatter → ファイル名の日付 → GROWI更新日時
    → modified_at の順で決定します。日時はUTCに統一し、タイムゾーン省略も
    UTC扱いです。日付が不明な文書は日付条件から除外し、一覧では末尾になります。
    document_date_source（集計では date_source）がnull/不明なら由来を推測せず、
    sourceの再同期で再計算してください。既存文書のmodified_at補完は更新日時であり、
    発行日とは限りません。意味的な日付を反映するには再同期が必要です。
    tag/project_idのグループは複数値の分類（facet）です。同じ文書が複数グループに
    入るため件数は加算できません。文書総数にはtotal_matchesを使ってください。

    Args:
        operation: count、list、group のいずれか。
        source_id: Knowledge Source ID。
        project_id: 文書のプロジェクト ID。空欄は現在の文脈を継承、* は非厳格スコープで全件。
        tags: 文書がすべて含むタグ。
        extension: 拡張子（例: md または .md）。
        path_prefix: 文書相対パスの接頭辞。
        status: active（既定）、error、deleted、inactive、または全状態のall。
        date_from: 文書日付の下限を含む（ISO 8601、日付のみならUTCの当日0時）。
        date_to: 上限日時を含む（ISO 8601）。日付のみならUTCの翌日0時未満まで含む。
        order_by: date または同義のdocument_date。
        order: 一覧の日付順asc/desc。groupは件数降順、同数ならキー順。
        group_by: group時のみ必須。source_id、source、project_id、extension、tag、status、date_source。
        limit: listの文書数/groupのグループ数の上限（サーバー上限あり）。
        offset: listでは文書、groupではグループの開始位置（整数、0以上）。続きはnext_offsetを使用。
    """
    try:
        return _run_async_in_thread(
            _query_async(
                operation,
                source_id,
                project_id,
                tags,
                extension,
                path_prefix,
                status,
                date_from,
                date_to,
                order_by,
                order,
                group_by,
                int(limit),
                offset,
            )
        )
    except Exception as exc:
        logger.exception("Knowledge structured query failed")
        return f"Knowledge構造化問い合わせでエラーが発生しました: {exc}"


# Schema metadata only: keep runtime service validation authoritative. Literal
# annotations are not supported by the core decorator. Avoid importing the
# service here so schema construction never starts runtime dependencies.
_KNOWLEDGE_QUERY_ENUMS = {
    "operation": ["count", "list", "group"],
    "status": ["active", "error", "deleted", "inactive", "all"],
    "order": ["asc", "desc"],
    "order_by": ["date", "document_date"],
    "group_by": [
        "source_id", "source", "project_id", "extension", "tag", "status", "date_source",
    ],
}
for _query_parameter in knowledge_query.parameters:
    if _query_parameter.name in _KNOWLEDGE_QUERY_ENUMS:
        _query_parameter.enum = list(_KNOWLEDGE_QUERY_ENUMS[_query_parameter.name])


@tool
def knowledge_read(document_id: str) -> str:
    """Knowledge Document IDを指定して正本ファイルの現在内容を読む。"""
    try:
        return _run_async_in_thread(_read_async(document_id))
    except Exception as exc:
        logger.exception("Knowledge read failed")
        return f"Knowledge文書の読み取りでエラーが発生しました: {exc}"


@tool
def knowledge_status() -> str:
    """登録済みKnowledge Sourceと同期状態を確認する。"""
    try:
        return _run_async_in_thread(_status_async())
    except Exception as exc:
        logger.exception("Knowledge status failed")
        return f"Knowledge状態確認でエラーが発生しました: {exc}"
