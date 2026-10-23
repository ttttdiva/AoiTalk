"""Sync v3 push（/ops）の適用。v2 push と同じ ``_dispatch_sync_operation`` へ委譲する。

- receipt と DB の効果は同一 transaction で commit する。どちらかだけが残ることはない。
- App README（README.md と Git checkpoint）は commit 前の外部効果であり、内容が同一なら再実行しない（冪等）。
  DB が失敗した場合は残り、同じ op_id の再送で前に進めて揃える（巻き戻しはしない）。
- commit 後に行う副作用（会話の registry 解放、Project ファイル削除）は after_commit で、commit 成功後にだけ実行する。

値（patch/base/entity）はログ・例外文・detailに出さない。
"""
from __future__ import annotations

import json
import logging
from contextlib import AbstractAsyncContextManager
from typing import Any, Awaitable, Callable, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import contract
from .receipts import (
    build_record,
    decrypt_record,
    encrypt_record,
    raise_floor,
    read_floor,
    request_mac,
)

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Awaitable[AsyncSession]]
RETRY_AFTER_MS = 5000


class OpRejected(Exception):
    """適用関数が返す拒否。code は contract.OpCode。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BodyInvalid(ValueError):
    """本文の形が不正（HTTP 400）。値を含めない。"""


class TableApplier(Protocol):
    def outer_lock(self, op: dict[str, Any]) -> AbstractAsyncContextManager[dict[str, Any]]: ...

    async def apply(
        self,
        session: AsyncSession,
        user_id: UUID,
        user_info: dict[str, Any],
        op: dict[str, Any],
        held: dict[str, Any],
        after_commit: list[Callable[[], None]],
    ) -> tuple[str | None, dict[str, Any]]: ...

    async def load_current(
        self, session: AsyncSession, user_id: UUID, table: str, key: str
    ) -> dict[str, Any] | None: ...


def _result(
    op_id: UUID,
    status: str,
    code: str | None,
    *,
    entity: dict[str, Any] | None = None,
    row_version: str | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    retry_after_ms: int | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    return {
        "op_id": str(op_id),
        "status": status,
        "code": code,
        "reason_code": reason_code,
        "entity": entity,
        "row_version": row_version,
        "conflicts": conflicts or [],
        "retry_after_ms": retry_after_ms,
    }


def _int_field(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BodyInvalid(name)
    return value


def _uuid_field(value: Any, name: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise BodyInvalid(name) from exc


def parse_body(body: Any) -> tuple[UUID, int, list[dict[str, Any]]]:
    if not isinstance(body, dict) or not isinstance(body.get("ops"), list):
        raise BodyInvalid("body")
    device_id = _uuid_field(body.get("device_id"), "device_id")
    floor = _int_field(body.get("retirement_floor"), "retirement_floor")
    ops: list[dict[str, Any]] = []
    for raw in body["ops"]:
        if not isinstance(raw, dict):
            raise BodyInvalid("op")
        op = {
            "op_id": _uuid_field(raw.get("op_id"), "op_id"),
            "table": raw.get("table"),
            "action": raw.get("action"),
            "entity_id": raw.get("entity_id"),
            "scope_key": raw.get("scope_key"),
            "patch": raw.get("patch", {}),
            "base": raw.get("base", {}),
            "op_seq": _int_field(raw.get("op_seq"), "op_seq"),
        }
        if (
            not isinstance(op["table"], str)
            or op["action"] not in ("create", "update", "delete", "restore", "reorder")
            or not isinstance(op["entity_id"], str)
            or not isinstance(op["patch"], dict)
            or not isinstance(op["base"], dict)
        ):
            raise BodyInvalid("op")
        ops.append(op)
    return device_id, floor, ops


_STATUS_CODES = {
    400: "invalid",
    403: "forbidden",
    404: "entity_deleted",
    409: "field_conflict",
    410: "entity_deleted",
    422: "invalid",
}
_ARCHIVE_CODES = {403: "forbidden", 404: "entity_deleted", 409: "field_conflict"}


def _code_for_exception(exc: BaseException) -> str | None:
    if isinstance(exc, OpRejected):
        return exc.code
    if isinstance(exc, PermissionError):
        return "forbidden"
    return _STATUS_CODES.get(getattr(exc, "status_code", None))


def _reason_code_for_exception(exc: BaseException) -> str | None:
    """v2 push の reason_code と同じ規則。ただし detail.code も allowlist 内の値だけにする。"""
    from ..api.sync_routes import _SYNC_REASON_CODE_ALLOWLIST  # 循環 import を避ける

    reason_code = getattr(exc, "reason_code", None)
    if isinstance(reason_code, str) and reason_code in _SYNC_REASON_CODE_ALLOWLIST:
        return reason_code
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        code = detail.get("code")
        if isinstance(code, str) and code in _SYNC_REASON_CODE_ALLOWLIST:
            return code
    return None


def _base_valid(base: dict[str, Any]) -> bool:
    if not set(base) <= {"updated_at"}:
        return False
    value = base.get("updated_at")
    return value is None or isinstance(value, str)


async def _commit_record(
    session_factory: SessionFactory,
    user_id: UUID,
    user_info: dict[str, Any],
    device_id: UUID,
    op: dict[str, Any],
    applier: TableApplier,
    held: dict[str, Any],
) -> tuple[dict[str, Any], str | None] | None:
    """1操作を1トランザクションで処理し、(記録, 実キー) を返す。保存しない拒否は None。"""
    op_id: UUID = op["op_id"]
    mac = request_mac(user_id, device_id, op)
    after_commit: list[Callable[[], None]] = []
    key: str | None = None
    session = await session_factory()
    try:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{user_id}:{op_id}"},
        )
        stored = (
            await session.execute(
                text("SELECT result FROM sync_op_receipts WHERE user_id = :u AND op_id = :o"),
                {"u": user_id, "o": op_id},
            )
        ).scalar_one_or_none()
        if stored is not None:
            existing = decrypt_record(user_id, op_id, stored)
            await session.rollback()
            return (existing, None) if existing["request_mac"] == mac else None
        if op["op_seq"] < await read_floor(session, user_id, device_id):
            await session.rollback()
            return None
        if not _base_valid(op["base"]):
            record = build_record("rejected", "invalid", [], [], mac)
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"{op['table']}:{op['entity_id']}"},
            )
            try:
                # savepoint だけを戻す。rollback すると advisory lock を手放してしまう。
                async with session.begin_nested():
                    key, outcome = await applier.apply(
                        session, user_id, user_info, op, held, after_commit
                    )
                archive_status = outcome.get("archive_rejected_status")
                if archive_status is None:
                    record = build_record("applied", None, [], [], mac)
                else:
                    record = build_record(
                        "applied_partial",
                        _ARCHIVE_CODES.get(archive_status, "invalid"),
                        [], [], mac,
                        # v2 push（sync_routes.py）と同じく 409 のときだけ。
                        "docs_business_conflict" if archive_status == 409 else None,
                    )
            except Exception as exc:
                code = _code_for_exception(exc)
                if code is None:
                    raise
                after_commit.clear()
                key = None
                record = build_record(
                    "rejected", code, [], [], mac, _reason_code_for_exception(exc)
                )
        await session.execute(
            text(
                "INSERT INTO sync_op_receipts (user_id, op_id, device_id, op_seq, result) "
                "VALUES (:u, :o, :d, :s, CAST(:r AS jsonb))"
            ),
            {
                "u": user_id,
                "o": op_id,
                "d": device_id,
                "s": op["op_seq"],
                "r": json.dumps(encrypt_record(user_id, op_id, record)),
            },
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()
    for callback in after_commit:
        try:
            callback()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sync_v3 after_commit failed op_id=%s type=%s", op_id, type(exc).__name__
            )
    return record, key


def _is_physical_key(key: str) -> bool:
    """複合キーの各要素が UUID のときだけ実在し得る（"<node>:new" 等は物理キーでない）。"""
    if ":" not in key:
        return True
    for part in key.split(":"):
        try:
            UUID(part)
        except ValueError:
            return False
    return True


async def _build_response(
    session_factory: SessionFactory,
    user_id: UUID,
    op: dict[str, Any],
    applier: TableApplier,
    record: dict[str, Any],
    key: str | None,
) -> dict[str, Any]:
    read_key = key if key is not None else op["entity_id"]
    entity: dict[str, Any] | None = None
    row_version: str | None = None
    if _is_physical_key(read_key):
        session = await session_factory()
        try:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            load_for_op = getattr(applier, "load_current_for_op", None)
            if callable(load_for_op):
                entity = await load_for_op(session, user_id, op["table"], read_key, op, record)
            else:
                entity = await applier.load_current(session, user_id, op["table"], read_key)
            if entity is not None:
                versions = (
                    await session.execute(
                        text(contract.ENTITY_VERSIONS_SQL),
                        {"table": op["table"], "ids": [read_key]},
                    )
                ).all()
                row_version = str(versions[0][1]) if versions else "0"
        finally:
            try:
                await session.rollback()
            finally:
                await session.close()
    return _result(
        op["op_id"], record["status"], record["code"],
        entity=entity, row_version=row_version, reason_code=record["reason_code"],
    )


async def _apply_one(
    session_factory: SessionFactory,
    user_id: UUID,
    user_info: dict[str, Any],
    device_id: UUID,
    op: dict[str, Any],
    applier: TableApplier,
) -> dict[str, Any]:
    try:
        async with applier.outer_lock(op) as held:
            committed = await _commit_record(
                session_factory, user_id, user_info, device_id, op, applier, held
            )
        if committed is None:
            return _result(op["op_id"], "rejected", "invalid")
        record, key = committed
        return await _build_response(session_factory, user_id, op, applier, record, key)
    except Exception as exc:
        # 例外文には値が入り得るため、型名だけを記録する。
        logger.warning(
            "sync_v3 op failed op_id=%s table=%s action=%s code=server_error type=%s",
            op["op_id"], op["table"], op["action"], type(exc).__name__,
        )
        return _result(op["op_id"], "retry", "server_error", retry_after_ms=RETRY_AFTER_MS)


async def apply_ops(
    session_factory: SessionFactory,
    user_id: UUID,
    user_info: dict[str, Any],
    body: Any,
    *,
    appliers: dict[str, TableApplier],
) -> list[dict[str, Any]]:
    """本文を検証し、各opの結果を返す。形が不正なら BodyInvalid。"""
    device_id, floor, ops = parse_body(body)
    session = await session_factory()
    try:
        await raise_floor(session, user_id, device_id, floor)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()
    results: list[dict[str, Any]] = []
    for op in ops:
        applier = appliers.get(op["table"])
        if applier is None:
            results.append(_result(op["op_id"], "rejected", "unsupported"))
        elif op["op_seq"] < floor:
            results.append(_result(op["op_id"], "rejected", "invalid"))
        else:
            results.append(
                await _apply_one(session_factory, user_id, user_info, device_id, op, applier)
            )
    return results
