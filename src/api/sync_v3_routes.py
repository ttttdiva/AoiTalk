"""Mobile sync v3 の読み取りAPI（/api/sync/v3）。

各リクエストは1つの REPEATABLE READ READ ONLY トランザクションで処理する。
scope 計算・変更の読込・行本体・版・head はすべて同じスナップショットから読む。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..sync_v3 import contract
from ..sync_v3.contract import ChangeFeedReader, ChangeRef
from ..sync_v3.page_builder import build_bootstrap_page, build_changes_page
from ..sync_v3.rows import bootstrap_rows, count_bootstrap_rows, load_rows
from ..sync_v3.scopes import compute_scopes
from ..sync_v3.settings import sync_v3_enabled

_HEAD_COMMITTED_AT_SQL = text(
    "SELECT max(committed_at) FROM sync_change_log WHERE commit_seq = :head"
)


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _disabled() -> JSONResponse:
    return JSONResponse(status_code=503, content={"code": contract.SYNC_DISABLED_CODE})


def create_sync_v3_router(
    feed: ChangeFeedReader,
    *,
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
) -> APIRouter:
    """sync v3 router。認証は v2 ``create_sync_router`` と同じ依存を受け取る。"""
    router = APIRouter(prefix="/api/sync/v3", tags=["sync-v3"])

    async def _current_user(request: Request) -> UUID:
        user_info = await get_user_from_request(request)
        if not user_info or "id" not in user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return UUID(str(user_info["id"]))

    @asynccontextmanager
    async def _snapshot() -> AsyncIterator[AsyncSession]:
        session: AsyncSession = await get_db_manager().get_session()
        try:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            yield session
        finally:
            try:
                await session.rollback()
            finally:
                await session.close()

    @router.get("/capabilities", operation_id="sync_v3_capabilities")
    async def sync_v3_capabilities(request: Request, _auth=Depends(require_auth_dependency)):
        await _current_user(request)
        async with _snapshot() as session:
            enabled = await sync_v3_enabled(session)
        return {
            "protocol": contract.PROTOCOL_VERSION,
            "enabled": bool(enabled),
            "max_ops_per_request": contract.MAX_OPS_PER_REQUEST,
        }

    @router.get("/scopes", operation_id="sync_v3_scopes")
    async def sync_v3_scopes(request: Request, _auth=Depends(require_auth_dependency)):
        user_id = await _current_user(request)
        async with _snapshot() as session:
            if not await sync_v3_enabled(session):
                return _disabled()
            revision, scopes, _filter = await compute_scopes(session, user_id)
            head = await feed.read_head(session)
            return {
                "scope_set_revision": revision,
                "scopes": scopes,
                "head_cursor": contract.head_cursor(head),
            }

    @router.get("/changes", operation_id="sync_v3_changes")
    async def sync_v3_changes(
        request: Request,
        cursor: str = "",
        limit: int = Query(contract.CHANGES_PAGE_LIMIT, ge=1, le=contract.CHANGES_PAGE_LIMIT),
        _auth=Depends(require_auth_dependency),
    ):
        user_id = await _current_user(request)
        position: tuple[int, int] | None = None
        if cursor:
            try:
                position = contract.decode_cursor(cursor)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid cursor") from exc
        async with _snapshot() as session:
            if not await sync_v3_enabled(session):
                return _disabled()
            scopes = await compute_scopes(session, user_id)
            revision, _scope_list, scope_filter = scopes
            head = await feed.read_head(session)
            committed_at = (
                await session.execute(_HEAD_COMMITTED_AT_SQL, {"head": head})
            ).scalar_one_or_none()
            envelope: dict[str, Any] = {
                "scope_set_revision": revision,
                "server_time": _iso_utc(datetime.now(timezone.utc)),
                "head_committed_at": _iso_utc(committed_at),
            }
            if position is None:
                return {"changes": [], "cursor": contract.head_cursor(head), "has_more": False, **envelope}
            minimum = await feed.min_retained_commit_seq(session)
            if contract.cursor_expired(position, minimum):
                return JSONResponse(status_code=410, content={"code": contract.CURSOR_EXPIRED_CODE})
            raw: list[ChangeRef] = await feed.read_changes(
                session,
                scope=scope_filter,
                after_commit_seq=position[0],
                after_seq=position[1],
                limit=limit + 1,
            )
            if not raw:
                return {"changes": [], "cursor": contract.head_cursor(head), "has_more": False, **envelope}
            window = raw[:limit]
            ids_by_table: dict[str, list[str]] = {}
            for ref in window:
                ids_by_table.setdefault(ref.entity_table, []).append(ref.entity_id)
            loaded: dict[tuple[str, str], Any] = {}
            for table, ids in ids_by_table.items():
                for entity_id, result in (
                    await load_rows(session, user_id, table, ids, scopes=scopes)
                ).items():
                    loaded[(table, entity_id)] = result
            changes, last_covered, covered = build_changes_page(
                window, loaded, byte_budget=contract.PAGE_BYTE_BUDGET
            )
            if last_covered is None:  # pragma: no cover - build_changes_page は先頭を必ず含める
                raise HTTPException(status_code=500, detail="empty page")
            if covered < len(raw):
                next_cursor, has_more = contract.encode_cursor(*last_covered), True
            else:
                next_cursor, has_more = contract.head_cursor(head), False
            return {"changes": changes, "cursor": next_cursor, "has_more": has_more, **envelope}

    @router.get("/bootstrap", operation_id="sync_v3_bootstrap")
    async def sync_v3_bootstrap(
        request: Request,
        scope_key: str,
        table: str,
        after: str = "",
        limit: int = Query(contract.BOOTSTRAP_PAGE_LIMIT, ge=1, le=contract.BOOTSTRAP_PAGE_LIMIT),
        _auth=Depends(require_auth_dependency),
    ):
        user_id = await _current_user(request)
        async with _snapshot() as session:
            if not await sync_v3_enabled(session):
                return _disabled()
            scopes = await compute_scopes(session, user_id)
            scope = next((item for item in scopes[1] if item["scope_key"] == scope_key), None)
            if scope is None:
                raise HTTPException(status_code=404, detail="Scope not found")
            if table not in contract.SCOPE_TABLES[scope["kind"]]:
                raise HTTPException(status_code=400, detail="Table is not in scope")
            try:
                rows = await bootstrap_rows(
                    session, user_id, scope, table, after or None, limit + 1, scopes=scopes
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid after") from exc
            emitted, last_entity_id = build_bootstrap_page(
                rows[:limit], byte_budget=contract.PAGE_BYTE_BUDGET
            )
            next_after = last_entity_id if len(emitted) < len(rows) else None
            total_count = (
                None if after else await count_bootstrap_rows(session, user_id, scope, table, scopes=scopes)
            )
            return {"rows": emitted, "next_after": next_after, "total_count": total_count}

    return router
