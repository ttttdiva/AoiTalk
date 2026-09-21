"""Re-encrypt Media Operations credentials with the configured active key.

The command never prints plaintext or ciphertext.  It is intentionally an
explicit operator action; normal application requests do not rekey rows.  A
rekey is a row-locked state transition with an immutable audit event so
concurrent rotations cannot be overwritten silently.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import os
from pathlib import Path
import sys
from datetime import datetime
from uuid import uuid4


# Running ``python scripts/rekey_media_platform_credentials.py`` sets
# ``sys.path[0]`` to ``scripts/``.  Put the checkout root first so the command
# always imports this repository's ``src`` package rather than an unrelated
# installed distribution named ``src``.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.memory.models import (
    MediaPlatformCredential,
    MediaPlatformCredentialAuditEvent,
    PlatformAccount,
    media_credential_state_hash,
)
from src.security.media_credential_crypto import (
    MediaCredentialCryptoError,
    _key_id,
    canonical_payload,
    decrypt_media_credential,
    encrypt_media_credential,
    media_credential_ciphertext_key_id,
)
from src.services.media_credential_vault_service import sha256_json
from src.services.media_credential_vault_service import MediaCredentialVaultService


def _scope(owner_user_id: object, project_id: object) -> str:
    return f"{owner_user_id}:{project_id or 'personal'}"


async def _append_rekey_audit(
    session: AsyncSession,
    row: MediaPlatformCredential,
    *,
    old_key_id: str,
    new_key_id: str,
) -> None:
    previous = (
        await session.execute(
            select(MediaPlatformCredentialAuditEvent)
            .where(MediaPlatformCredentialAuditEvent.credential_id == row.id)
            .order_by(MediaPlatformCredentialAuditEvent.sequence.desc(), MediaPlatformCredentialAuditEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    sequence = int(previous.sequence) + 1 if previous is not None else 1
    event_id = uuid4()
    created_at = datetime.utcnow()
    request_hash = sha256_json(
        {
            "event": "rekey",
            "credential_id": str(row.id),
            "from_key_id": old_key_id,
            "to_key_id": new_key_id,
            "revision": int(row.revision),
        }
    )
    idempotency_scope = _scope(row.owner_user_id, row.project_id)
    # Include the source key and post-rekey revision so a deliberate
    # k1→k2→k1 cycle cannot collide with an earlier immutable audit event.
    # Store only a bounded digest in the VARCHAR(255) idempotency key: key
    # identifiers themselves may be up to 128 characters each.
    cycle_digest = sha256_json(
        {
            "from_key_id": old_key_id,
            "to_key_id": new_key_id,
            "revision": int(row.revision),
        }
    )[:32]
    idempotency_key = f"rekey:{row.id}:{cycle_digest}"
    snapshot = {
        "status": row.status,
        "revision": int(row.revision),
        "provider_code": "rekeyed",
    }
    event_hash = sha256_json(
        {
            "id": str(event_id),
            "credential_id": str(row.id),
            "event_type": "rekey",
            "sequence": sequence,
            "request_hash": request_hash,
            "idempotency_scope": idempotency_scope,
            "idempotency_key": idempotency_key,
            "prev_event_hash": previous.event_hash if previous is not None else None,
            "snapshot": snapshot,
            "created_at": created_at.isoformat(),
        }
    )
    session.add(
        MediaPlatformCredentialAuditEvent(
            id=event_id,
            credential_id=row.id,
            platform_account_id=row.platform_account_id,
            owner_user_id=row.owner_user_id,
            project_id=row.project_id,
            event_type="rekey",
            actor_id=None,
            actor_type="unknown",
            snapshot_json=snapshot,
            request_hash=request_hash,
            sequence=sequence,
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            prev_event_hash=previous.event_hash if previous is not None else None,
            event_hash=event_hash,
            created_at=created_at,
        )
    )


async def rekey_credentials(
    session: AsyncSession,
    *,
    dry_run: bool = False,
    batch_size: int = 100,
) -> int:
    active_key_id = _key_id()
    # Key ids permit ``_`` and ``-``.  Escape SQL LIKE wildcards so the
    # candidate query cannot mistake a different embedded key id for the
    # active one (the exact comparison below remains authoritative).
    escaped_active_key_id = (
        active_key_id.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    limit = max(1, min(int(batch_size), 1000))
    vault = MediaCredentialVaultService()
    statement = (
        select(
            MediaPlatformCredential.id,
            MediaPlatformCredential.platform_account_id,
        )
        .where(
            or_(
                MediaPlatformCredential.encryption_key_id != active_key_id,
                ~MediaPlatformCredential.encrypted_payload.like(
                    f"enc:v1:aes256gcm:{escaped_active_key_id}:%",
                    escape="\\",
                ),
            )
        )
        .order_by(MediaPlatformCredential.id)
        .limit(limit)
    )
    candidates = list((await session.execute(statement)).all())
    changed = 0
    for credential_id, platform_account_id in candidates:
        # Match the application lock order (account -> credential ->
        # connection).  Selecting candidate ids above is intentionally
        # unlocked; each row is re-read after the account lock so a concurrent
        # rotation can win cleanly instead of deadlocking the rekey worker.
        account_statement = select(PlatformAccount).where(PlatformAccount.id == platform_account_id).limit(1)
        if not dry_run:
            account_statement = account_statement.with_for_update()
        account = (await session.execute(account_statement)).scalar_one_or_none()
        if account is None:
            raise RuntimeError("media credential platform account is missing")
        credential_statement = select(MediaPlatformCredential).where(MediaPlatformCredential.id == credential_id).limit(1)
        if not dry_run:
            credential_statement = credential_statement.with_for_update()
        row = (await session.execute(credential_statement)).scalar_one_or_none()
        if row is None:
            continue
        old_key_id = str(row.encryption_key_id)
        try:
            embedded_key_id = media_credential_ciphertext_key_id(row.encrypted_payload)
        except MediaCredentialCryptoError:
            raise RuntimeError("media credential ciphertext metadata is invalid") from None
        if old_key_id == active_key_id and embedded_key_id == active_key_id:
            continue
        # Rekey is an operator mutation, but it must not become an integrity
        # repair tool.  Validate the account/connection ownership, credential
        # reference and state hash before decrypting or writing anything.
        await vault._assert_credential_binding(session, account, row)
        payload = decrypt_media_credential(
            row.encrypted_payload,
            credential_id=row.id,
            platform_account_id=row.platform_account_id,
        )
        digest = hashlib.sha256(canonical_payload(payload)).hexdigest()
        if not hmac.compare_digest(digest, str(row.payload_digest or "")):
            raise RuntimeError("media credential payload integrity check failed")
        encrypted = encrypt_media_credential(
            payload,
            credential_id=row.id,
            platform_account_id=row.platform_account_id,
        )
        if encrypted.key_id == old_key_id:
            continue
        changed += 1
        if dry_run:
            continue

        row.revision = int(row.revision) + 1
        row.encrypted_payload = encrypted.ciphertext
        row.encryption_key_id = encrypted.key_id
        row.payload_digest = encrypted.payload_digest
        row.updated_at = datetime.utcnow()
        row.state_hash = media_credential_state_hash(
            revision=int(row.revision),
            status=row.status,
            connection_type=row.connection_type,
            payload_digest=row.payload_digest,
            capabilities=row.capabilities,
            verification_code=row.verification_code,
            encryption_key_id=row.encryption_key_id,
        )
        await _append_rekey_audit(
            session,
            row,
            old_key_id=old_key_id,
            new_key_id=encrypted.key_id,
        )
    if not dry_run:
        await session.commit()
    return changed


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Re-encrypt MediaOps credential vault rows")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    database_url = os.getenv("AOITALK_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("AOITALK_DATABASE_URL or DATABASE_URL is required")
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+asyncpg://" + database_url[len("postgresql://") :]
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            batch_limit = max(1, min(int(args.batch_size), 1000))
            # A dry run does not mutate candidate rows, so repeatedly asking
            # for the first batch would never make progress.  Keep it to one
            # bounded inspection; mutating runs drain batches until no work
            # remains.
            incomplete = False
            if args.dry_run:
                total_changed = await rekey_credentials(
                    session,
                    dry_run=True,
                    batch_size=batch_limit,
                )
                # The dry-run API intentionally inspects only one bounded
                # batch.  Signal that additional candidates may remain
                # instead of looping forever over the unchanged first page.
                incomplete = total_changed >= batch_limit
            else:
                total_changed = 0
                while True:
                    changed = await rekey_credentials(
                        session,
                        dry_run=False,
                        batch_size=batch_limit,
                    )
                    total_changed += changed
                    if changed < batch_limit:
                        break
        print(f"rekeyed={total_changed} incomplete={'true' if incomplete else 'false'}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
