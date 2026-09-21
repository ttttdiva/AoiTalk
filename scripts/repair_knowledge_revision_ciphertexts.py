"""Repair knowledge revision ciphertexts copied with the node AAD.

The normal operation is a read-only dry-run.  ``--apply`` performs one
transaction after every candidate has been validated, then decrypts the
updated values again before committing.  No plaintext, ciphertext, or row id
is emitted by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from sqlalchemy import JSON, Text, bindparam, text

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.memory.database import DatabaseManager  # noqa: E402
from src.security.field_crypto import (  # noqa: E402
    FieldCryptoError,
    decrypt_text,
    encrypt_text,
    is_encrypted_value,
)


REVISION_TEXT_AAD = "knowledge_revisions.body_text"
NODE_TEXT_AAD = "knowledge_nodes.body_text"
REVISION_JSON_AAD = "knowledge_revisions.body_json"
NODE_JSON_AAD = "knowledge_nodes.body_json"


class RepairError(RuntimeError):
    """Raised when a revision cannot be proven safe to repair."""


@dataclass(frozen=True)
class RepairCandidate:
    """One ciphertext replacement, with only a digest of the plaintext."""

    revision_id: Any
    column: str
    old_value: str
    new_value: str
    plaintext_digest: str


def _canonical_json(value: Any) -> str:
    """Match the frontend's JSON.stringify-compatible compact representation."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _plaintext_digest(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _row_value(row: Any, key: str) -> Any:
    """Read SQLAlchemy rows and the small mapping fakes used by tests alike."""

    if isinstance(row, Mapping):
        return row.get(key)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping[key]
    return getattr(row, key)


def _is_authentication_failure(error: BaseException) -> bool:
    """Only authentication failures permit trying the legacy node AAD."""

    if isinstance(error, FieldCryptoError):
        return str(error) == "encrypted field authentication failed"
    # Keep compatibility with callers/tests that exercise the pre-sanitized
    # cryptography exception directly.  Import lazily so this module's public
    # behavior remains governed by field_crypto's sanitized error contract.
    try:
        from cryptography.exceptions import InvalidTag
    except ImportError:  # pragma: no cover - cryptography is a runtime dep
        return False
    return isinstance(error, InvalidTag)


def _candidate(
    revision_id: Any,
    column: str,
    ciphertext: Any,
    revision_aad: str,
    node_aad: str,
    is_json: bool,
) -> RepairCandidate | None:
    """Return a repair only for the proven node-AAD-only corruption case.

    A value that already decrypts with the revision AAD is intentionally a
    no-op.  A malformed value, provider failure, or node-AAD failure fails
    closed instead of guessing at a replacement.
    """

    if not is_encrypted_value(ciphertext):
        raise RepairError("knowledge revision ciphertext is not encrypted")

    try:
        decrypt_text(ciphertext, aad=revision_aad)
    except Exception as revision_error:
        if not _is_authentication_failure(revision_error):
            raise RepairError("knowledge revision authentication validation failed") from None
    else:
        return None

    # A node-AAD fallback is a cryptographic proof only when the source value
    # is itself an encrypted envelope.  Plain legacy values must not be
    # silently wrapped as if they were the known corruption.
    # Validate the historical revision ciphertext itself with the node AAD.
    # The current node body may have changed since this revision was created;
    # using it as the source would overwrite history with today's content.
    source_ciphertext = ciphertext
    if not isinstance(source_ciphertext, str) or not is_encrypted_value(source_ciphertext):
        raise RepairError("knowledge revision ciphertext is invalid")
    try:
        plaintext = decrypt_text(source_ciphertext, aad=node_aad)
    except Exception:
        raise RepairError("knowledge revision node authentication validation failed") from None
    if plaintext is None:
        raise RepairError("knowledge revision plaintext is invalid")

    if is_json:
        try:
            parsed = json.loads(plaintext)
            canonical = _canonical_json(parsed)
        except (TypeError, ValueError):
            raise RepairError("knowledge revision JSON payload is invalid") from None
        repaired_plaintext = canonical
    else:
        repaired_plaintext = plaintext

    try:
        repaired_ciphertext = encrypt_text(repaired_plaintext, aad=revision_aad)
    except Exception:
        raise RepairError("knowledge revision re-encryption failed") from None
    if not isinstance(repaired_ciphertext, str) or not is_encrypted_value(repaired_ciphertext):
        raise RepairError("knowledge revision re-encryption failed")
    return RepairCandidate(
        revision_id=revision_id,
        column=column,
        old_value=ciphertext,
        new_value=repaired_ciphertext,
        plaintext_digest=_plaintext_digest(repaired_plaintext),
    )


def _select_rows_statement():
    # Keep the scan as one explicit JOIN so a revision can only be repaired
    # when its owning node provides the matching legacy AAD ciphertext.
    return text(
        """
        SELECT
            r.id AS revision_id,
            r.body_text AS revision_body_text,
            r.body_json AS revision_body_json,
            n.body_text AS node_body_text,
            n.body_json AS node_body_json
        FROM knowledge_revisions AS r
        INNER JOIN knowledge_nodes AS n ON n.id = r.node_id
        ORDER BY r.id
        """
    )


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _rollback_safely(session: Any) -> None:
    """Best-effort rollback that cannot replace a sanitized primary error."""

    try:
        await _await_if_needed(session.rollback())
    except Exception:
        pass


async def _open_existing_schema_session(manager: Any) -> Any:
    """Open a session without invoking DatabaseManager.initialize().

    ``DatabaseManager.initialize`` runs Alembic migrations.  A repair dry-run
    must be strictly read-only, so use the already-configured session factory
    directly for both dry-run and apply.  The get_session fallback is retained
    only for lightweight test doubles that do not expose the factories.
    """

    # Prefer the async factory because ``run`` is an asyncio command and a
    # synchronous psycopg2 session would block the event loop while scanning
    # or updating a large revision table.  Both factories bypass initialize()
    # and therefore never run Alembic; SyncSessionLocal remains a compatibility
    # fallback for lightweight test doubles/older managers.
    for factory_name in ("SessionLocal", "SyncSessionLocal"):
        factory = getattr(manager, factory_name, None)
        if callable(factory):
            return await _await_if_needed(factory())
    get_session = getattr(manager, "get_session", None)
    if callable(get_session):
        return await _await_if_needed(get_session())
    raise RepairError("database session factory unavailable")


async def _prevalidate(session: Any) -> list[RepairCandidate]:
    """Scan and validate every encrypted revision before any update occurs."""

    result = await _await_if_needed(session.execute(_select_rows_statement()))
    rows = result.fetchall() if hasattr(result, "fetchall") else result
    rows = await _await_if_needed(rows)
    candidates: list[RepairCandidate] = []
    for row in rows or ():
        revision_id = _row_value(row, "revision_id")
        fields = (
            (
                "body_text",
                _row_value(row, "revision_body_text"),
                _row_value(row, "node_body_text"),
                REVISION_TEXT_AAD,
                NODE_TEXT_AAD,
                False,
            ),
            (
                "body_json",
                _row_value(row, "revision_body_json"),
                _row_value(row, "node_body_json"),
                REVISION_JSON_AAD,
                NODE_JSON_AAD,
                True,
            ),
        )
        for column, revision_value, _node_value, revision_aad, node_aad, is_json in fields:
            if not is_encrypted_value(revision_value):
                continue
            candidate = _candidate(
                revision_id,
                column,
                revision_value,
                revision_aad,
                node_aad,
                is_json,
            )
            # A revision that already authenticates is a no-op and should not
            # be represented in the update list.
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def _update_statement(column: str):
    if column == "body_text":
        return text(
            """
            UPDATE knowledge_revisions
            SET body_text = :new_value
            WHERE id = :revision_id AND body_text = :old_value
            """
        ).bindparams(
            bindparam("revision_id"),
            bindparam("old_value", type_=Text()),
            bindparam("new_value", type_=Text()),
        )
    if column == "body_json":
        # JSON/JSONB columns must receive a JSON string value (not raw SQL
        # text).  SQLAlchemy's JSON bind processor preserves the encrypted
        # envelope as a JSON string while retaining the column's type.
        return text(
            """
            UPDATE knowledge_revisions
            SET body_json = :new_value
            -- PostgreSQL's json type has no equality operator.  Compare the
            -- encrypted JSON string's canonical textual representation while
            -- retaining JSON-typed bind parameters for SET and old_value.
            WHERE id = :revision_id
              AND body_json::text = CAST(:old_value AS JSON)::text
            """
        ).bindparams(
            bindparam("revision_id"),
            bindparam("old_value", type_=JSON()),
            bindparam("new_value", type_=JSON()),
        )
    raise RepairError("unsupported knowledge revision column")


async def _apply(session: Any, candidates: Iterable[RepairCandidate]) -> int:
    """Apply all replacements with compare-and-swap guards."""

    applied = 0
    for candidate in candidates:
        if candidate.column not in {"body_text", "body_json"}:
            raise RepairError("unsupported knowledge revision column")
        try:
            result = await _await_if_needed(
                session.execute(
                    _update_statement(candidate.column),
                    {
                        "revision_id": candidate.revision_id,
                        "old_value": candidate.old_value,
                        "new_value": candidate.new_value,
                    },
                )
            )
        except Exception:
            raise RepairError("knowledge revision update failed") from None
        if getattr(result, "rowcount", None) != 1:
            raise RepairError("knowledge revision update precondition failed")
        applied += 1
    return applied


def _verify_plaintext_digest(plaintext: str, candidate: RepairCandidate) -> bool:
    if candidate.column == "body_json":
        try:
            plaintext = _canonical_json(json.loads(plaintext))
        except (TypeError, ValueError):
            return False
    return _plaintext_digest(plaintext) == candidate.plaintext_digest


async def _reverify(session: Any, candidates: Iterable[RepairCandidate]) -> int:
    """Re-read, authenticate, and digest-check every updated row."""

    verified = 0
    for candidate in candidates:
        selected = text(
            f"SELECT {candidate.column} AS value FROM knowledge_revisions "
            "WHERE id = :revision_id"
        )
        try:
            result = await _await_if_needed(
                session.execute(selected, {"revision_id": candidate.revision_id})
            )
        except Exception:
            raise RepairError("knowledge revision verification query failed") from None
        row = result.first() if hasattr(result, "first") else next(iter(result), None)
        row = await _await_if_needed(row)
        if row is None:
            raise RepairError("knowledge revision disappeared during verification")
        value = _row_value(row, "value")
        if value != candidate.new_value:
            raise RepairError("knowledge revision value changed during verification")
        aad = REVISION_JSON_AAD if candidate.column == "body_json" else REVISION_TEXT_AAD
        try:
            plaintext = decrypt_text(value, aad=aad)
        except Exception:
            raise RepairError("knowledge revision authentication re-verification failed") from None
        if plaintext is None or not _verify_plaintext_digest(plaintext, candidate):
            raise RepairError("knowledge revision digest re-verification failed")
        verified += 1
    return verified


async def run(*, apply: bool = False, db_manager: DatabaseManager | None = None) -> int:
    """Run a dry-run (default) or transactional repair."""

    manager = db_manager
    owns_manager = manager is None
    session = None
    try:
        if manager is None:
            manager = DatabaseManager()
        session = await _open_existing_schema_session(manager)
        candidates = await _prevalidate(session)
        print(f"knowledge revision repair candidates: {len(candidates)}")
        if not apply:
            await _rollback_safely(session)
            print("dry-run: no database changes committed")
            return 0
        applied = await _apply(session, candidates)
        verified = await _reverify(session, candidates)
        if verified != applied:
            raise RepairError("knowledge revision verification count mismatch")
        await _await_if_needed(session.commit())
        print(f"knowledge revision repair applied: {applied}")
        return 0
    except RepairError:
        if session is not None:
            await _rollback_safely(session)
        raise
    except asyncio.CancelledError:
        # Cancellation must remain observable to the caller/scheduler while
        # still rolling back any transaction opened by the scan or updates.
        if session is not None:
            await _rollback_safely(session)
        raise
    except BaseException:
        if session is not None:
            await _rollback_safely(session)
        raise RepairError("knowledge revision repair failed") from None
    finally:
        if session is not None:
            try:
                await _await_if_needed(session.close())
            except Exception:
                pass
        if owns_manager:
            close = getattr(manager, "close", None)
            if close is not None:
                try:
                    await _await_if_needed(close())
                except Exception:
                    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="knowledge_revisions の node-AAD ciphertext を安全に再暗号化する"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="全候補を事前検証した後、DB更新とcommitを行う（既定はdry-run）",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(apply=args.apply))
    except RepairError as exc:
        # The exception text is deliberately fixed and contains no row IDs,
        # plaintext, ciphertext, or provider diagnostics.
        print(f"knowledge revision repair failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
