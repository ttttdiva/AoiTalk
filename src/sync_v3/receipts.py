"""Sync v3 の受付記録（値を保存せず、所有者とop_idに結び付けて暗号化する）。"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..security.field_crypto import FieldCryptoError, decrypt_json_value_if_needed, encrypt_json_value, get_data_key

FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

RECEIPT_RETIREMENT_DDL = """
ALTER TABLE sync_op_receipts ADD COLUMN IF NOT EXISTS device_id uuid NOT NULL;
ALTER TABLE sync_op_receipts ADD COLUMN IF NOT EXISTS op_seq bigint NOT NULL;
CREATE INDEX IF NOT EXISTS ix_sync_op_receipts_device_seq ON sync_op_receipts (user_id, device_id, op_seq);
CREATE TABLE IF NOT EXISTS sync_device_floors (
  user_id uuid NOT NULL,
  device_id uuid NOT NULL,
  floor bigint NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, device_id)
);
"""

RECORD_KEYS = frozenset(
    {"v", "status", "code", "reason_code", "applied_fields", "conflict_fields", "request_mac"}
)
_MAC_INFO = b"aoitalk:sync_op_receipts:request:v1"
_ENCRYPTED_PREFIX = "enc:v1:"


def request_mac(user_id: UUID, device_id: UUID, op: dict[str, Any]) -> str:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_MAC_INFO).derive(
        get_data_key()
    )
    msg = json.dumps(
        {name: op.get(name) for name in (
            "table", "action", "entity_id", "scope_key", "patch", "base", "op_seq"
        )},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(
        key, f"{user_id}\n{device_id}\n".encode() + msg, hashlib.sha256
    ).hexdigest()


def _field_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and FIELD_NAME_RE.match(item) for item in value
    ):
        raise FieldCryptoError("receipt record has invalid field names")
    return list(value)


def _reason_code(value: Any) -> str | None:
    if value is None:
        return None
    if not (isinstance(value, str) and FIELD_NAME_RE.match(value)):
        raise FieldCryptoError("receipt record has invalid reason_code")
    return value


def build_record(
    status: str,
    code: str | None,
    applied_fields: list[str],
    conflict_fields: list[str],
    mac: str,
    reason_code: str | None = None,
) -> dict[str, Any]:
    return {
        "v": 1,
        "status": status,
        "code": code,
        "reason_code": _reason_code(reason_code),
        "applied_fields": _field_list(list(applied_fields)),
        "conflict_fields": _field_list(list(conflict_fields)),
        "request_mac": mac,
    }


def _aad(user_id: UUID, op_id: UUID) -> str:
    return f"sync_op_receipts.result:{user_id}:{op_id}"


def encrypt_record(user_id: UUID, op_id: UUID, record: dict[str, Any]) -> Any:
    stored = encrypt_json_value(record, aad=_aad(user_id, op_id))
    if not (isinstance(stored, str) and stored.startswith(_ENCRYPTED_PREFIX)):
        raise FieldCryptoError("receipt record was not encrypted")
    return stored


def decrypt_record(user_id: UUID, op_id: UUID, stored: Any) -> dict[str, Any]:
    if not (isinstance(stored, str) and stored.startswith(_ENCRYPTED_PREFIX)):
        raise FieldCryptoError("receipt record is not encrypted")
    record = decrypt_json_value_if_needed(stored, aad=_aad(user_id, op_id))
    if not isinstance(record, dict) or set(record) != RECORD_KEYS:
        raise FieldCryptoError("receipt record has unexpected keys")
    _field_list(record["applied_fields"])
    _field_list(record["conflict_fields"])
    _reason_code(record["reason_code"])
    return record


async def raise_floor(
    session: AsyncSession, user_id: UUID, device_id: UUID, floor: int
) -> None:
    await session.execute(
        text(
            "INSERT INTO sync_device_floors (user_id, device_id, floor) "
            "VALUES (:user_id, :device_id, :floor) "
            "ON CONFLICT (user_id, device_id) DO UPDATE SET "
            "floor = greatest(sync_device_floors.floor, EXCLUDED.floor), updated_at = now()"
        ),
        {"user_id": user_id, "device_id": device_id, "floor": floor},
    )


async def read_floor(session: AsyncSession, user_id: UUID, device_id: UUID) -> int:
    value = (
        await session.execute(
            text("SELECT floor FROM sync_device_floors WHERE user_id = :u AND device_id = :d"),
            {"u": user_id, "d": device_id},
        )
    ).scalar_one_or_none()
    return int(value or 0)


async def purge_retired_receipts(session: AsyncSession, batch: int = 5000) -> int:
    total = 0
    while True:
        result = await session.execute(
            text(
                "DELETE FROM sync_op_receipts WHERE ctid IN ("
                "SELECT r.ctid FROM sync_op_receipts r "
                "JOIN sync_device_floors f ON r.user_id = f.user_id AND r.device_id = f.device_id "
                "WHERE r.op_seq < f.floor LIMIT :batch)"
            ),
            {"batch": batch},
        )
        deleted = result.rowcount or 0
        total += deleted
        if deleted < batch:
            return total
