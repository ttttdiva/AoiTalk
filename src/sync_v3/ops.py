"""Sync v3 push（/ops）の適用。受付記録と効果は同一トランザクションでcommitする。

既存の v2 適用関数（tasks / projects / time_entries / conversation_sessions / docs）は
渡した session 内で途中commitするため、受付記録と効果を1回でcommitできない。
そのため ``SUPPORTED_APPLIERS`` は空で、全tableを rejected/unsupported で返す。
途中commitしない適用関数ができたら ``TableApplier`` を実装して登録する。
値（patch/base/entity）はログ・例外文・detailに出さない。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import contract
from .merge import merge_fields, validate_field_names
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
_WHOLE_ENTITY_ACTIONS = ("delete", "restore", "reorder")


class OpRejected(Exception):
    """適用関数が返す拒否。code は contract.OpCode。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BodyInvalid(ValueError):
    """本文の形が不正（HTTP 400）。値を含めない。"""


class TableApplier(Protocol):
    async def load_current(
        self, session: AsyncSession, user_id: UUID, op: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    async def apply(
        self, session: AsyncSession, user_id: UUID, op: dict[str, Any], fields: dict[str, Any]
    ) -> None: ...


SUPPORTED_APPLIERS: dict[str, TableApplier] = {}


def _result(
    op_id: UUID,
    status: str,
    code: str | None,
    *,
    entity: dict[str, Any] | None = None,
    row_version: str | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    retry_after_ms: int | None = None,
) -> dict[str, Any]:
    return {
        "op_id": str(op_id),
        "status": status,
        "code": code,
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


def _code_for_exception(exc: BaseException) -> str | None:
    if isinstance(exc, OpRejected):
        return exc.code
    status = getattr(exc, "status_code", None)
    return {403: "forbidden", 404: "entity_deleted", 400: "invalid", 422: "invalid", 409: "field_conflict"}.get(status)


async def _merge_and_apply(
    session: AsyncSession,
    user_id: UUID,
    op: dict[str, Any],
    applier: TableApplier,
    mac: str,
) -> dict[str, Any]:
    current = await applier.load_current(session, user_id, op)
    action = op["action"]
    if current is None and action != "create":
        return build_record("rejected", "entity_deleted", [], [], mac)
    if action in _WHOLE_ENTITY_ACTIONS:
        fields, conflicts = dict(op["patch"]), []
    else:
        # 既存行へのcreateは全fieldのbaseを不明として扱う。
        base = op["base"] if action == "update" else {}
        fields, conflicts = merge_fields(current or {}, op["patch"], base)
    try:
        if fields or action in _WHOLE_ENTITY_ACTIONS or current is None:
            await applier.apply(session, user_id, op, fields)
    except Exception as exc:
        code = _code_for_exception(exc)
        if code is None:
            raise
        await session.rollback()
        return build_record("rejected", code, [], [], mac)
    if not conflicts:
        return build_record("applied", None, sorted(fields), [], mac)
    if fields:
        return build_record("applied_partial", "field_conflict", sorted(fields), conflicts, mac)
    return build_record("rejected", "field_conflict", [], conflicts, mac)


async def _commit_record(
    session_factory: SessionFactory,
    user_id: UUID,
    device_id: UUID,
    op: dict[str, Any],
    applier: TableApplier,
) -> dict[str, Any] | None:
    """1操作を1トランザクションで処理し、記録を返す。保存しない拒否は None。"""
    op_id: UUID = op["op_id"]
    mac = request_mac(user_id, device_id, op)
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
            return existing if existing["request_mac"] == mac else None
        if not validate_field_names(op["patch"], op["base"]):
            record = build_record("rejected", "invalid", [], [], mac)
        elif op["op_seq"] < await read_floor(session, user_id, device_id):
            await session.rollback()
            return None
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"{op['table']}:{op['entity_id']}"},
            )
            record = await _merge_and_apply(session, user_id, op, applier, mac)
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
        return record
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


async def _build_response(
    session_factory: SessionFactory,
    user_id: UUID,
    op: dict[str, Any],
    applier: TableApplier,
    record: dict[str, Any],
) -> dict[str, Any]:
    session = await session_factory()
    try:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        entity = await applier.load_current(session, user_id, op)
        row_version: str | None = None
        if entity is not None:
            versions = (
                await session.execute(
                    text(contract.ENTITY_VERSIONS_SQL),
                    {"table": op["table"], "ids": [op["entity_id"]]},
                )
            ).all()
            row_version = str(versions[0][1]) if versions else "0"
    finally:
        try:
            await session.rollback()
        finally:
            await session.close()
    conflicts = [
        {
            "field": name,
            "local": op["patch"].get(name),
            "base": op["base"].get(name),
            "server": entity.get(name) if entity is not None else None,
        }
        for name in record["conflict_fields"]
    ]
    return _result(
        op["op_id"], record["status"], record["code"],
        entity=entity, row_version=row_version, conflicts=conflicts,
    )


async def _apply_one(
    session_factory: SessionFactory,
    user_id: UUID,
    device_id: UUID,
    op: dict[str, Any],
    applier: TableApplier,
) -> dict[str, Any]:
    try:
        record = await _commit_record(session_factory, user_id, device_id, op, applier)
        if record is None:
            return _result(op["op_id"], "rejected", "invalid")
        return await _build_response(session_factory, user_id, op, applier, record)
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
    body: Any,
    *,
    appliers: dict[str, TableApplier] | None = None,
) -> list[dict[str, Any]]:
    """本文を検証し、各opの結果を返す。形が不正なら BodyInvalid。"""
    registry = SUPPORTED_APPLIERS if appliers is None else appliers
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
        applier = registry.get(op["table"])
        if applier is None:
            results.append(_result(op["op_id"], "rejected", "unsupported"))
        elif op["op_seq"] < floor:
            results.append(_result(op["op_id"], "rejected", "invalid"))
        else:
            results.append(await _apply_one(session_factory, user_id, device_id, op, applier))
    return results
