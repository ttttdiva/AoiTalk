"""Durable protocol for paged ``docs_read(view='edit')`` reads.

The ordinary document/record reads intentionally remain stateless.  Edit reads
are different: a write token is only safe after every page in one immutable,
authorized projection has been delivered.  This service keeps the checkpoint
on the server, so a caller can submit only the exact opaque next token and
cannot forge an offset from the public read fingerprint.

Only protocol metadata is persisted.  Page content is rebuilt from canonical
Docs rows for each delivery and replay.  A lost response is therefore safe to
retry without storing a canonical body or a plaintext model response.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, or_, select, text

from ..memory.models import DocsEditReadSession, DocsReadLease
from .docs_consistency import (
    DocsConflict,
    is_postgres,
    issue_edit_lease,
    revision,
)
from .docs_read_projection import (
    MAX_DEPTH,
    _cursor,
    build_docs_read_projection,
)


SESSION_TTL = timedelta(minutes=30)
TOKEN_BYTES = 32
EDIT_READ_LOCK_NAMESPACE = 1847300451


def _edit_read_lock_key(*, actor_id: UUID, root_id: UUID, scope_binding: str) -> int:
    """Map one initial-session identity to a signed PostgreSQL advisory key."""

    digest = hashlib.sha256(
        f"{actor_id}:{root_id}:{scope_binding}".encode("utf-8")
    ).digest()
    # pg_advisory_xact_lock(int,int) accepts signed 32-bit values.  Keep the
    # value non-negative and reserve a distinct namespace from Docs writer and
    # index locks, so this is never the global writer boundary.
    return int.from_bytes(digest[:4], "big", signed=False) & 0x7FFFFFFF


async def lock_edit_read_session(
    session,
    *,
    actor_id: UUID,
    root_id: UUID,
    scope_binding: str,
) -> None:
    """Serialize identical initial-session creation for one actor/scope.

    PostgreSQL advisory locks are transaction-scoped and therefore remain held
    until the tool commits the session row and (for a one-page read) its final
    lease.  Non-PostgreSQL sessions cannot satisfy the Docs protocol contract
    and deliberately skip the database lock for lightweight unit doubles.
    """

    if not is_postgres(session):
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
        {
            "namespace": EDIT_READ_LOCK_NAMESPACE,
            "key": _edit_read_lock_key(
                actor_id=actor_id,
                root_id=root_id,
                scope_binding=scope_binding,
            ),
        },
    )


def edit_read_scope_binding(
    *,
    actor_id: UUID,
    root_id: UUID,
    library_id: UUID,
    depth: int,
    page_chars: int,
    turn_project_id: UUID | None,
    allowed_node_ids: set[UUID] | None,
    context_binding: str,
) -> str:
    """Hash every input that can change the edit projection's scope.

    ``read_fingerprint`` remains a public diagnostic/read-continuation value;
    this separate binding is the authorization/session identity and is never
    accepted by the mutation path.
    """

    payload = [
        str(actor_id),
        str(root_id),
        str(library_id),
        int(depth),
        int(page_chars),
        str(turn_project_id or ""),
        str(context_binding or ""),
        sorted(str(value) for value in allowed_node_ids)
        if allowed_node_ids is not None
        else None,
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _new_token() -> str:
    """Return an unguessable, database-backed continuation token."""

    return secrets.token_urlsafe(TOKEN_BYTES)


class DocsEditReadSessionService:
    """Read and advance one durable edit-read session."""

    def __init__(self, session):
        self.session = session

    async def _cleanup_expired(self, now: datetime) -> None:
        await self.session.execute(
            delete(DocsEditReadSession).where(DocsEditReadSession.expires_at < now)
        )

    async def _find_by_cursor(
        self,
        *,
        actor_id: UUID,
        root_id: UUID,
        cursor: str,
    ) -> DocsEditReadSession | None:
        """Find the session whose current or replay token equals ``cursor``."""

        if not isinstance(cursor, str) or len(cursor) > 128:
            return None
        result = await self.session.execute(
            select(DocsEditReadSession)
            .where(
                DocsEditReadSession.actor_id == actor_id,
                DocsEditReadSession.root_id == root_id,
                or_(
                    DocsEditReadSession.next_cursor == cursor,
                    DocsEditReadSession.last_cursor == cursor,
                ),
            )
            .order_by(DocsEditReadSession.created_at.desc())
            .with_for_update()
        )
        return result.scalars().first()

    async def _find_initial_session(
        self,
        *,
        actor_id: UUID,
        root_id: UUID,
        scope_binding: str,
        now: datetime,
    ) -> DocsEditReadSession | None:
        """Find a session that can answer an initial-page retry.

        A one-page edit read has an empty request cursor even on its terminal
        response, so completed rows must remain eligible here for final
        lost-response replay.  A completed multi-page row has a non-empty
        terminal request cursor and is ignored by the caller for a fresh empty
        request.
        """

        result = await self.session.execute(
            select(DocsEditReadSession)
            .where(
                DocsEditReadSession.actor_id == actor_id,
                DocsEditReadSession.root_id == root_id,
                DocsEditReadSession.scope_binding == scope_binding,
                DocsEditReadSession.expires_at > now,
            )
            .order_by(DocsEditReadSession.created_at.desc())
            .with_for_update()
        )
        return result.scalars().first()

    async def _render(
        self,
        *,
        graph,
        root,
        actor_id: UUID,
        allowed_node_ids: set[UUID] | None,
        turn_project_id: UUID | None,
        depth: int,
        page_chars: int,
        start: int,
        fingerprint: str | None,
    ) -> dict:
        """Rebuild one page without ever trusting a client-provided offset."""

        if start < 0:
            raise DocsConflict("Docs edit continuation state is invalid; restart the edit read")
        internal_cursor = "" if start == 0 else _cursor(fingerprint, start)
        try:
            payload = await build_docs_read_projection(
                graph,
                root,
                actor_id,
                allowed_node_ids=allowed_node_ids,
                turn_project_id=turn_project_id,
                depth=depth,
                cursor=internal_cursor,
                page_chars=page_chars,
                include_manifest=True,
                record_only=False,
            )
        except (ValueError, PermissionError) as exc:
            # A changed projection produces a public cursor error in the
            # stateless path.  For edit reads it is a durable session conflict.
            raise DocsConflict(
                "Docs changed or the edit read scope is no longer valid; restart the edit read"
            ) from exc
        if fingerprint is not None and payload.get("read_fingerprint") != fingerprint:
            raise DocsConflict(
                "Docs changed or the edit read scope is no longer valid; restart the edit read"
            )
        if payload.get("page_start") != start:
            raise DocsConflict("Docs edit continuation state is invalid; restart the edit read")
        return payload

    @staticmethod
    def _decorate_intermediate(payload: dict) -> None:
        payload.update(
            {
                "success": False,
                "error_code": "docs_edit_read_incomplete",
                "retryable": True,
                "write_token_status": "withheld",
                "error": (
                    "Edit lease withheld until all pages are read; "
                    "continue with the exact next_cursor before mutating."
                ),
                "next_action": (
                    "Call docs_read(view='edit') with the exact next_cursor and the "
                    "same target/scope arguments."
                ),
            }
        )
        payload.pop("write_token", None)
        payload.pop("write_revision", None)

    @staticmethod
    def _decorate_incomplete_scope(payload: dict) -> None:
        payload.update(
            {
                "success": False,
                "error_code": "docs_edit_scope_incomplete",
                "retryable": False,
                "write_token_status": "withheld",
                "error": (
                    "Edit lease withheld because the bounded edit scope is incomplete; "
                    "narrow the target or address coverage_reasons before mutating."
                ),
                "next_action": (
                    "Read a smaller or deeper-safe section and resolve coverage_reasons; "
                    "docs_mutate is unavailable for this incomplete projection."
                ),
            }
        )
        payload.pop("write_token", None)
        payload.pop("write_revision", None)

    async def _replay(
        self,
        *,
        row: DocsEditReadSession,
        graph,
        root,
        actor_id: UUID,
        allowed_node_ids: set[UUID] | None,
        turn_project_id: UUID | None,
        current_revision: tuple[int, int],
        request_cursor: str,
    ) -> dict:
        """Replay the exact prior page after a lost response."""

        if row.revision != current_revision[0] or row.policy_revision != current_revision[1]:
            raise DocsConflict("Docs or permissions changed during the edit read; restart the edit read")
        if row.last_page_start is None or row.last_page_end is None:
            raise DocsConflict("Docs edit continuation state is invalid; restart the edit read")
        payload = await self._render(
            graph=graph,
            root=root,
            actor_id=actor_id,
            allowed_node_ids=allowed_node_ids,
            turn_project_id=turn_project_id,
            depth=row.depth,
            page_chars=row.page_chars,
            start=row.last_page_start,
            fingerprint=row.read_fingerprint,
        )
        if await revision(self.session, row.library_id) != current_revision:
            raise DocsConflict("Docs changed during page replay; restart the edit read")
        if payload.get("page_end") != row.last_page_end:
            raise DocsConflict("Docs edit page boundaries changed; restart the edit read")
        payload["request_cursor"] = request_cursor
        payload["next_cursor"] = row.next_cursor
        payload["view"] = "edit"
        if row.has_more:
            self._decorate_intermediate(payload)
            return payload
        if not payload.get("coverage_complete"):
            self._decorate_incomplete_scope(payload)
            return payload
        if row.lease_id is None:
            raise DocsConflict("Docs edit lease is unavailable; restart the edit read")
        lease = await self.session.get(DocsReadLease, row.lease_id)
        if lease is None or lease.expires_at <= datetime.utcnow():
            raise DocsConflict("Docs edit lease expired; restart the edit read")
        payload["success"] = True
        payload["write_token"] = str(lease.id)
        payload["write_revision"] = lease.revision
        payload["write_precondition"] = "library_and_policy_revision"
        return payload

    async def read_page(
        self,
        *,
        graph,
        root,
        actor_id: UUID,
        allowed_node_ids: set[UUID] | None,
        turn_project_id: UUID | None,
        depth: int,
        page_chars: int,
        cursor: str,
        scope_binding: str,
        mutation_binding: str | None = None,
    ) -> dict:
        """Deliver/advance or safely replay one edit-read page.

        ``cursor`` is either empty for the first page or the exact opaque token
        previously returned as ``next_cursor``.  Any old, forged, skipped, or
        cross-scope token is rejected.  State is advanced before returning, so
        a lost response can replay the page without advancing twice.
        """

        # Keep the stateless projection's public argument contract: malformed
        # caller values are validation errors, while ValueError from the
        # internal cursor below means the durable source projection changed.
        if type(depth) is not int or not 0 <= depth <= MAX_DEPTH:
            raise ValueError(f"depth must be between 0 and {MAX_DEPTH}")
        if type(page_chars) is not int or not 4096 <= page_chars <= 32000:
            raise ValueError("page_chars must be between 4096 and 32000")
        if not isinstance(cursor, str):
            raise ValueError("cursor must be a string")
        now = datetime.utcnow()
        if not cursor:
            # The row-level FOR UPDATE in the continuation path cannot lock a
            # row that does not exist yet.  Serialize the initial lookup and
            # creation with a per actor/root/scope transaction advisory lock;
            # this is intentionally not ``lock_docs_writes``.
            await lock_edit_read_session(
                self.session,
                actor_id=actor_id,
                root_id=root.id,
                scope_binding=scope_binding,
            )
        await self._cleanup_expired(now)
        current_revision = await revision(self.session, root.docs_library_id)

        row: DocsEditReadSession | None
        replay = False
        if cursor:
            row = await self._find_by_cursor(
                actor_id=actor_id,
                root_id=root.id,
                cursor=cursor,
            )
            if row is None:
                raise DocsConflict(
                    "Docs edit continuation token is invalid, stale, or out of order; "
                    "restart the edit read"
                )
            if row.scope_binding != scope_binding:
                raise DocsConflict("Docs edit continuation token does not match this scope")
            if row.expires_at <= now:
                raise DocsConflict("Docs edit continuation token expired; restart the edit read")
            if row.last_cursor == cursor:
                replay = True
            elif row.next_cursor != cursor or row.finished:
                raise DocsConflict(
                    "Docs edit continuation token is stale or out of order; "
                    "retry the exact prior request or restart the edit read"
                )
        else:
            row = await self._find_initial_session(
                actor_id=actor_id,
                root_id=root.id,
                scope_binding=scope_binding,
                now=now,
            )
            if row is not None:
                # An empty cursor is the first request and is replayable only
                # while that first page remains the most recent response.  A
                # completed one-page session is retained for final-response
                # replay; if its revision is already stale, it can be safely
                # retired and replaced by a fresh session.
                if row.revision != current_revision[0] or row.policy_revision != current_revision[1]:
                    await self.session.delete(row)
                    row = None
                elif row.last_cursor != "":
                    if row.finished:
                        row = None
                    else:
                        raise DocsConflict(
                            "Docs edit read requires the exact next_cursor; restart the edit read"
                        )
                elif row.finished:
                    lease = (
                        await self.session.get(DocsReadLease, row.lease_id)
                        if row.lease_id is not None
                        else None
                    )
                    if row.lease_id is not None and (
                        lease is None or lease.expires_at <= now
                    ):
                        await self.session.delete(row)
                        row = None
                    else:
                        replay = True
                else:
                    replay = True

        if row is not None and replay:
            return await self._replay(
                row=row,
                graph=graph,
                root=root,
                actor_id=actor_id,
                allowed_node_ids=allowed_node_ids,
                turn_project_id=turn_project_id,
                current_revision=current_revision,
                request_cursor=cursor,
            )

        # No row exists: build the first page before inserting protocol state,
        # so a failed source read cannot leave an unusable session behind.
        if row is None:
            payload = await self._render(
                graph=graph,
                root=root,
                actor_id=actor_id,
                allowed_node_ids=allowed_node_ids,
                turn_project_id=turn_project_id,
                depth=depth,
                page_chars=page_chars,
                start=0,
                fingerprint=None,
            )
            row = DocsEditReadSession(
                actor_id=actor_id,
                root_id=root.id,
                library_id=root.docs_library_id,
                revision=current_revision[0],
                policy_revision=current_revision[1],
                scope_binding=scope_binding,
                read_fingerprint=payload["read_fingerprint"],
                depth=depth,
                page_chars=page_chars,
                turn_project_id=turn_project_id,
                expires_at=now + SESSION_TTL,
            )
            self.session.add(row)
            await self.session.flush()
            start = 0
        else:
            # A non-empty exact ``next_cursor`` advances one page only.
            if row.revision != current_revision[0] or row.policy_revision != current_revision[1]:
                raise DocsConflict("Docs or permissions changed during the edit read; restart the edit read")
            start = row.next_offset
            payload = await self._render(
                graph=graph,
                root=root,
                actor_id=actor_id,
                allowed_node_ids=allowed_node_ids,
                turn_project_id=turn_project_id,
                depth=row.depth,
                page_chars=row.page_chars,
                start=start,
                fingerprint=row.read_fingerprint,
            )

        # A writer may commit while the projection is being rebuilt.  Do not
        # persist a checkpoint for a page that straddled that revision.
        if await revision(self.session, row.library_id) != current_revision:
            raise DocsConflict("Docs changed during page delivery; restart the edit read")

        # Projection identity is established by the first page and must remain
        # stable for every later page, including the final lease page.
        if row.read_fingerprint != payload.get("read_fingerprint"):
            raise DocsConflict("Docs edit projection changed; restart the edit read")
        page_end = int(payload.get("page_end", start))
        has_more = bool(payload.get("has_more"))
        next_token = _new_token() if has_more else None
        row.last_cursor = cursor
        row.last_page_start = start
        row.last_page_end = page_end
        row.next_offset = page_end
        row.next_cursor = next_token
        row.has_more = has_more
        row.expires_at = now + SESSION_TTL
        payload["request_cursor"] = cursor
        payload["view"] = "edit"
        payload["next_cursor"] = next_token

        if has_more:
            self._decorate_intermediate(payload)
            await self.session.flush()
            return payload

        row.finished = True
        if not payload.get("coverage_complete"):
            row.terminal_status = "scope_incomplete"
            self._decorate_incomplete_scope(payload)
            await self.session.flush()
            return payload

        # The write lease is created exactly once, at the terminal page.  On a
        # lost final response ``_replay`` reads this stored ID and never issues
        # a second lease.
        lease_projection = dict(payload)
        lease = await issue_edit_lease(
            self.session,
            root=root,
            actor_id=actor_id,
            before_revision=(row.revision, row.policy_revision),
            projection=lease_projection,
            # DocsMutationService deliberately continues to consume the
            # existing turn/project binding contract.  The stronger full
            # projection binding above is session authority only.
            binding=mutation_binding or scope_binding,
        )
        row.lease_id = lease.id
        row.terminal_status = "lease_issued"
        payload["success"] = True
        payload["write_token"] = str(lease.id)
        payload["write_revision"] = lease.revision
        payload["write_precondition"] = "library_and_policy_revision"
        await self.session.flush()
        return payload


__all__ = [
    "DocsEditReadSessionService",
    "edit_read_scope_binding",
    "lock_edit_read_session",
]
