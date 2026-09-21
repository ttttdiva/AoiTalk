"""Harden Media credential state hashes before they gate publication.

Revision ``0018`` introduced the vault and its first state-hash envelope.  A
later service revision added provider evidence and the embedded encryption
key id to that envelope.  This repair migration deliberately treats existing
rows as untrusted input:

* every row must match the legacy hash before it is changed;
* the key id embedded in ciphertext must match the metadata column;
* previously ``verified`` rows are demoted to ``verification_pending`` with
  unknown capabilities and an explicit ``provider_response_unknown`` code;
* a downgrade refuses to process a verified row (there is no safe way to
  recreate the lost verification evidence).

Validation is performed for the complete table before any UPDATE is issued so
an integrity failure fails closed and leaves the migration transaction
untouched.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Mapping

from alembic import op
import sqlalchemy as sa


revision = "20260902_0021"
down_revision = "20260902_0020"
branch_labels = None
depends_on = None


_TABLE = "media_platform_credentials"
_UNKNOWN_CAPABILITIES = {
    "identity": "unknown",
    "publish": "unknown",
    "media": "unknown",
    "analytics": "unknown",
}
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CIPHERTEXT_PREFIX = ("enc", "v1", "aes256gcm")


def _table_exists(bind: sa.Connection) -> bool:
    try:
        return _TABLE in sa.inspect(bind).get_table_names()
    except (sa.exc.NoInspectionAvailable, sa.exc.NoSuchTableError):
        return False


def _normalise_capabilities(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = {}
    return dict(value) if isinstance(value, Mapping) else {}


def _state_hash(
    row: Mapping[str, Any],
    *,
    include_verification_code: bool,
    include_encryption_key_id: bool,
) -> str:
    """Compute either the legacy or current state-hash envelope."""

    payload: dict[str, Any] = {
        "revision": int(row.get("revision") or 0),
        "status": row.get("status"),
        "connection_type": row.get("connection_type"),
        "payload_digest": row.get("payload_digest"),
        "capabilities": _normalise_capabilities(row.get("capabilities")),
    }
    if include_verification_code:
        payload["verification_code"] = row.get("verification_code")
    if include_encryption_key_id:
        payload["encryption_key_id"] = row.get("encryption_key_id")
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _legacy_state_hash(row: Mapping[str, Any]) -> str:
    return _state_hash(
        row,
        include_verification_code=False,
        include_encryption_key_id=False,
    )


def _current_state_hash(row: Mapping[str, Any]) -> str:
    return _state_hash(
        row,
        include_verification_code=True,
        include_encryption_key_id=True,
    )


def _embedded_key_id(ciphertext: Any) -> str | None:
    """Parse only non-secret ciphertext metadata.

    We intentionally do not decode nonce/ciphertext bytes here.  The
    migration's concern is that the recorded key selector cannot be swapped
    independently of the ciphertext.  Any malformed prefix is an integrity
    failure and is never included in an exception message.
    """

    if not isinstance(ciphertext, str):
        return None
    parts = ciphertext.split(":", 5)
    if len(parts) != 6 or tuple(parts[:3]) != _CIPHERTEXT_PREFIX:
        return None
    key_id = parts[3]
    return key_id if _KEY_ID_RE.fullmatch(key_id) else None


def _load_rows(bind: sa.Connection) -> list[Mapping[str, Any]]:
    rows = bind.execute(
        sa.text(
            "SELECT id, revision, status, connection_type, payload_digest, "
            "capabilities, verification_code, encryption_key_id, "
            "encrypted_payload, verification_started_at, "
            "verification_completed_at, last_verified_at, state_hash FROM "
            + _TABLE
        )
    ).mappings().all()
    return list(rows)


def _validate_legacy_row(row: Mapping[str, Any]) -> None:
    expected_hash = _legacy_state_hash(row)
    actual_hash = str(row.get("state_hash") or "")
    # A current-envelope hash is accepted for idempotent/manual reruns.  It is
    # still subjected to key metadata validation and verified-row demotion.
    if not hmac.compare_digest(actual_hash, expected_hash) and not hmac.compare_digest(
        actual_hash,
        _current_state_hash(row),
    ):
        raise RuntimeError(
            "media credential state integrity check failed before migration"
        )
    embedded = _embedded_key_id(row.get("encrypted_payload"))
    metadata_key = str(row.get("encryption_key_id") or "")
    if embedded is None or not hmac.compare_digest(embedded, metadata_key):
        raise RuntimeError(
            "media credential ciphertext key metadata mismatch before migration"
        )


def _demote_verified(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return the safe replacement fields for an existing verified row."""

    if str(row.get("status") or "").strip().lower() != "verified":
        return {
            "status": row.get("status"),
            "capabilities": _normalise_capabilities(row.get("capabilities")),
            "verification_code": row.get("verification_code"),
            "verification_started_at": row.get("verification_started_at"),
            "verification_completed_at": row.get("verification_completed_at"),
            "last_verified_at": row.get("last_verified_at"),
        }
    return {
        "status": "verification_pending",
        "capabilities": dict(_UNKNOWN_CAPABILITIES),
        "verification_code": "provider_response_unknown",
        "verification_started_at": None,
        "verification_completed_at": None,
        "last_verified_at": None,
    }


def _upgrade_plan(rows: list[Mapping[str, Any]]) -> list[tuple[Any, dict[str, Any]]]:
    plan: list[tuple[Any, dict[str, Any]]] = []
    # Validate every row first.  Do not mutate earlier rows if a later row is
    # corrupt; Alembic transaction behaviour differs by dialect for DDL and
    # operator-triggered reruns must remain fail-closed.
    for row in rows:
        _validate_legacy_row(row)
        fields = _demote_verified(row)
        projected = dict(row)
        projected.update(fields)
        fields["state_hash"] = _current_state_hash(projected)
        plan.append((row.get("id"), fields))
    return plan


def _apply_update(bind: sa.Connection, row_id: Any, fields: Mapping[str, Any]) -> None:
    statement = sa.text(
        "UPDATE "
        + _TABLE
        + " SET status = :status, capabilities = :capabilities, "
        "verification_code = :verification_code, "
        "verification_started_at = :verification_started_at, "
        "verification_completed_at = :verification_completed_at, "
        "last_verified_at = :last_verified_at, state_hash = :state_hash "
        "WHERE id = :id"
    ).bindparams(sa.bindparam("capabilities", type_=sa.JSON))
    bind.execute(
        statement,
        {
            "id": row_id,
            "status": fields["status"],
            "capabilities": fields["capabilities"],
            "verification_code": fields["verification_code"],
            "verification_started_at": fields["verification_started_at"],
            "verification_completed_at": fields["verification_completed_at"],
            "last_verified_at": fields["last_verified_at"],
            "state_hash": fields["state_hash"],
        },
    )


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind):
        return
    for row_id, fields in _upgrade_plan(_load_rows(bind)):
        _apply_update(bind, row_id, fields)


def _downgrade_plan(rows: list[Mapping[str, Any]]) -> list[tuple[Any, str]]:
    plan: list[tuple[Any, str]] = []
    for row in rows:
        # A verified row has provider evidence that cannot be represented by
        # the pre-0021 envelope.  Refuse the inverse rather than silently
        # restoring a state that publication would trust incorrectly.
        if str(row.get("status") or "").strip().lower() == "verified":
            raise RuntimeError(
                "cannot downgrade media credentials while verified rows exist"
            )
        expected = _current_state_hash(row)
        if not hmac.compare_digest(str(row.get("state_hash") or ""), expected):
            raise RuntimeError(
                "media credential state integrity check failed during downgrade"
            )
        embedded = _embedded_key_id(row.get("encrypted_payload"))
        metadata_key = str(row.get("encryption_key_id") or "")
        if embedded is None or not hmac.compare_digest(embedded, metadata_key):
            raise RuntimeError(
                "media credential ciphertext key metadata mismatch during downgrade"
            )
        plan.append((row.get("id"), _legacy_state_hash(row)))
    return plan


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind):
        return
    for row_id, state_hash in _downgrade_plan(_load_rows(bind)):
        bind.execute(
            sa.text("UPDATE " + _TABLE + " SET state_hash = :state_hash WHERE id = :id"),
            {"id": row_id, "state_hash": state_hash},
        )
