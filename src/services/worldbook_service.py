"""ワールドブック管理サービス

ワールドブックのCRUD + キーワードマッチングによるプロンプト注入を提供する。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.database import get_db_session
from ..models.ecc_models import WorldBook, WorldBookEntry, CharacterWorldBook, Character
from ..utils.uuid_utils import parse_uuid, parse_uuid_strict

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────
# ユーティリティ
# ────────────────────────────────────────────




def _run_sync(coro):
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()


# ────────────────────────────────────────────
# 例外
# ────────────────────────────────────────────


class WorldBookError(Exception):
    """ワールドブック操作のドメインエラー"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class WorldBookNotFoundError(WorldBookError):
    """指定されたワールドブックが見つからない"""

    def __init__(self, identifier: str):
        super().__init__(
            f"ワールドブックが見つかりません: {identifier}",
            status_code=404,
        )


class EntryNotFoundError(WorldBookError):
    """指定されたエントリが見つからない"""

    def __init__(self, identifier: str):
        super().__init__(
            f"エントリが見つかりません: {identifier}",
            status_code=404,
        )


# ────────────────────────────────────────────
# ワールドブック CRUD
# ────────────────────────────────────────────


async def create_worldbook(data: dict) -> dict:
    """ワールドブックを新規作成する。"""
    if not data.get("name"):
        raise WorldBookError("名前は必須です")

    async with await get_db_session() as session:
        wb = WorldBook(
            id=uuid.uuid4(),
            scenario_id=parse_uuid(data.get("scenario_id")),
            name=data["name"],
            description=data.get("description", ""),
            is_enabled=data.get("is_enabled", True),
        )
        session.add(wb)
        await session.commit()
        await session.refresh(wb)

        logger.info("ワールドブックを作成しました: %s (%s)", wb.name, wb.id)
        return wb.to_dict()


async def update_worldbook(worldbook_id: str, data: dict) -> dict:
    """ワールドブックを更新する。"""
    uid = parse_uuid_strict(worldbook_id, lambda v: WorldBookError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        wb = await session.get(WorldBook, uid)
        if wb is None:
            raise WorldBookNotFoundError(worldbook_id)

        for key in ("name", "description", "is_enabled"):
            if key in data:
                setattr(wb, key, data[key])
        if "scenario_id" in data:
            wb.scenario_id = parse_uuid(data.get("scenario_id"))

        wb.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(wb)

        logger.info("ワールドブックを更新しました: %s (%s)", wb.name, wb.id)
        return wb.to_dict()


async def delete_worldbook(worldbook_id: str) -> bool:
    """ワールドブックを削除する。"""
    uid = parse_uuid_strict(worldbook_id, lambda v: WorldBookError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        wb = await session.get(WorldBook, uid)
        if wb is None:
            raise WorldBookNotFoundError(worldbook_id)

        wb_name = wb.name
        await session.execute(sa_delete(WorldBook).where(WorldBook.id == uid))
        await session.commit()

        logger.info("ワールドブックを削除しました: %s (%s)", wb_name, uid)
        return True


async def list_worldbooks(scenario_id: Optional[str] = None) -> list:
    """ワールドブック一覧を取得する。"""
    async with await get_db_session() as session:
        scenario_uid = parse_uuid(scenario_id)
        stmt = (
            select(WorldBook)
            .options(selectinload(WorldBook.entries))
            .order_by(WorldBook.name)
        )
        if scenario_id is not None:
            stmt = stmt.where(WorldBook.scenario_id == scenario_uid)
        result = await session.execute(stmt)
        books = result.scalars().all()
        return [b.to_dict() for b in books]


async def get_worldbook(worldbook_id: str) -> dict:
    """ワールドブックをエントリ込みで取得する。"""
    uid = parse_uuid_strict(worldbook_id, lambda v: WorldBookError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        stmt = (
            select(WorldBook)
            .options(
                selectinload(WorldBook.entries),
                selectinload(WorldBook.character_links),
            )
            .where(WorldBook.id == uid)
        )
        result = await session.execute(stmt)
        wb = result.scalar_one_or_none()

        if wb is None:
            raise WorldBookNotFoundError(worldbook_id)

        data = wb.to_dict()
        data["entries"] = [e.to_dict() for e in wb.entries]
        data["linked_character_ids"] = [
            str(link.character_id) for link in wb.character_links
        ]
        return data


# ────────────────────────────────────────────
# エントリ CRUD
# ────────────────────────────────────────────

_ENTRY_UPDATABLE_FIELDS = {
    "name",
    "keywords",
    "secondary_keywords",
    "content",
    "is_enabled",
    "priority",
    "case_sensitive",
    "constant",
    "insertion_position",
}


async def create_entry(worldbook_id: str, data: dict) -> dict:
    """ワールドブックにエントリを追加する。"""
    wb_uid = parse_uuid_strict(worldbook_id, lambda v: WorldBookError(f"無効なUUID形式です: {v}"))

    if not data.get("content"):
        raise WorldBookError("エントリのcontentは必須です")

    async with await get_db_session() as session:
        # ワールドブックの存在確認
        wb = await session.get(WorldBook, wb_uid)
        if wb is None:
            raise WorldBookNotFoundError(worldbook_id)

        entry = WorldBookEntry(
            id=uuid.uuid4(),
            world_book_id=wb_uid,
        )
        for key in _ENTRY_UPDATABLE_FIELDS:
            if key in data:
                setattr(entry, key, data[key])

        session.add(entry)
        await session.commit()
        await session.refresh(entry)

        logger.info("エントリを作成しました: %s (worldbook=%s)", entry.name, wb.name)
        return entry.to_dict()


async def update_entry(entry_id: str, data: dict) -> dict:
    """エントリを更新する。"""
    uid = parse_uuid_strict(entry_id, lambda v: WorldBookError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        entry = await session.get(WorldBookEntry, uid)
        if entry is None:
            raise EntryNotFoundError(entry_id)

        for key in _ENTRY_UPDATABLE_FIELDS:
            if key in data:
                setattr(entry, key, data[key])

        entry.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(entry)

        logger.info("エントリを更新しました: %s (%s)", entry.name, entry.id)
        return entry.to_dict()


async def delete_entry(entry_id: str) -> bool:
    """エントリを削除する。"""
    uid = parse_uuid_strict(entry_id, lambda v: WorldBookError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        entry = await session.get(WorldBookEntry, uid)
        if entry is None:
            raise EntryNotFoundError(entry_id)

        await session.execute(sa_delete(WorldBookEntry).where(WorldBookEntry.id == uid))
        await session.commit()

        logger.info("エントリを削除しました: %s", uid)
        return True


# ────────────────────────────────────────────
# キャラクターリンク
# ────────────────────────────────────────────


async def link_character(worldbook_id: str, character_id: str) -> dict:
    """キャラクターとワールドブックを紐づける。"""
    wb_uid = parse_uuid_strict(worldbook_id, lambda v: WorldBookError(f"無効なUUID形式です: {v}"))
    char_uid = parse_uuid_strict(character_id, lambda v: WorldBookError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        # 存在確認
        wb = await session.get(WorldBook, wb_uid)
        if wb is None:
            raise WorldBookNotFoundError(worldbook_id)

        char = await session.get(Character, char_uid)
        if char is None:
            raise WorldBookError(
                f"キャラクターが見つかりません: {character_id}", status_code=404
            )

        # 重複チェック
        existing = (
            await session.execute(
                select(CharacterWorldBook).where(
                    CharacterWorldBook.character_id == char_uid,
                    CharacterWorldBook.world_book_id == wb_uid,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.to_dict()

        link = CharacterWorldBook(
            id=uuid.uuid4(),
            character_id=char_uid,
            world_book_id=wb_uid,
        )
        session.add(link)
        await session.commit()
        await session.refresh(link)

        logger.info(
            "キャラクターをワールドブックに紐づけました: %s <-> %s",
            char.name,
            wb.name,
        )
        return link.to_dict()


async def unlink_character(worldbook_id: str, character_id: str) -> bool:
    """キャラクターとワールドブックの紐づけを解除する。"""
    wb_uid = parse_uuid_strict(worldbook_id, lambda v: WorldBookError(f"無効なUUID形式です: {v}"))
    char_uid = parse_uuid_strict(character_id, lambda v: WorldBookError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        result = await session.execute(
            sa_delete(CharacterWorldBook).where(
                CharacterWorldBook.character_id == char_uid,
                CharacterWorldBook.world_book_id == wb_uid,
            )
        )
        await session.commit()

        if result.rowcount == 0:
            raise WorldBookError("紐づけが見つかりません", status_code=404)

        logger.info(
            "キャラクターとワールドブックの紐づけを解除しました: char=%s wb=%s",
            character_id,
            worldbook_id,
        )
        return True


async def get_linked_characters(worldbook_id: str) -> list:
    """ワールドブックに紐づいたキャラクター一覧を取得する。"""
    wb_uid = parse_uuid_strict(worldbook_id, lambda v: WorldBookError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        stmt = (
            select(Character)
            .join(
                CharacterWorldBook,
                CharacterWorldBook.character_id == Character.id,
            )
            .where(CharacterWorldBook.world_book_id == wb_uid)
        )
        result = await session.execute(stmt)
        chars = result.scalars().all()
        return [c.to_dict() for c in chars]


# ────────────────────────────────────────────
# プロンプト注入用: キーワードマッチング
# ────────────────────────────────────────────


async def get_matching_entries(
    character_slug: str, recent_text: str, scenario_id: Optional[str] = None
) -> list:
    """キャラクターに紐づくワールドブックから、マッチするエントリを取得する。

    1. character_world_books から character_id を slug で取得
    2. 紐づく world_book の is_enabled=True なエントリを取得
    3. constant=True は無条件で含める
    4. keywords が recent_text 内に存在するかチェック (case_sensitive 考慮)
    5. priority 降順でソート
    """
    async with await get_db_session() as session:
        # slug からキャラクターを取得
        char_result = await session.execute(
            select(Character).where(Character.slug == character_slug)
        )
        char = char_result.scalar_one_or_none()

        if char is None:
            # recognition_aliases で検索
            all_chars = await session.execute(
                select(Character).where(Character.is_enabled.is_(True))
            )
            for c in all_chars.scalars().all():
                aliases = c.recognition_aliases or []
                if character_slug in aliases or c.name == character_slug:
                    char = c
                    break

        wb_id_set = set()

        if scenario_id:
            scenario_uid = parse_uuid(scenario_id)
            if scenario_uid:
                scenario_books = await session.execute(
                    select(WorldBook.id).where(
                        WorldBook.scenario_id == scenario_uid,
                        WorldBook.is_enabled.is_(True),
                    )
                )
                wb_id_set.update(row[0] for row in scenario_books.all())

        # キャラクターに紐づくワールドブックIDを取得
        if char is not None:
            links_result = await session.execute(
                select(CharacterWorldBook.world_book_id).where(
                    CharacterWorldBook.character_id == char.id
                )
            )
            wb_id_set.update(row[0] for row in links_result.all())
        wb_ids = list(wb_id_set)
        if not wb_ids:
            return []

        # 有効なワールドブックの有効なエントリを取得
        stmt = (
            select(WorldBookEntry)
            .join(WorldBook, WorldBookEntry.world_book_id == WorldBook.id)
            .where(
                WorldBookEntry.world_book_id.in_(wb_ids),
                WorldBook.is_enabled.is_(True),
                WorldBookEntry.is_enabled.is_(True),
            )
        )
        result = await session.execute(stmt)
        entries = result.scalars().all()

        matched = []
        for entry in entries:
            # constant=True は無条件で含める
            if entry.constant:
                matched.append(entry.to_dict())
                continue

            # キーワードマッチング
            keywords = entry.keywords or []
            if not keywords:
                continue

            text_to_check = recent_text
            if not entry.case_sensitive:
                text_to_check = recent_text.lower()

            for keyword in keywords:
                kw = keyword if entry.case_sensitive else keyword.lower()
                if kw and kw in text_to_check:
                    matched.append(entry.to_dict())
                    break

        # priority 降順でソート
        matched.sort(key=lambda e: e.get("priority", 0), reverse=True)
        return matched
