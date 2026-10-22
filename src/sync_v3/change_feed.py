"""Sync v3 変更フィードのPostgreSQL実装。"""
from __future__ import annotations

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from . import contract
from .contract import ChangeRef, ScopeFilter

# purge は commit 順の付与と競合しないよう、commit lock とは別のキーで同時実行だけを排他する。
PURGE_LOCK_KEY = contract.SYNC_COMMIT_LOCK_KEY + 1

_UUID_ARRAY = ARRAY(PG_UUID(as_uuid=True))

_READ_CHANGES_SQL = text(
    """
    SELECT entity_table, entity_id, op, commit_seq, seq
      FROM sync_change_log
     WHERE commit_seq IS NOT NULL
       AND (commit_seq, seq) > (:after_commit_seq, :after_seq)
       AND (owner_user_id = :user_id
            OR project_id = ANY(:project_ids)
            OR docs_library_id = ANY(:library_ids)
            OR space_id = ANY(:space_ids))
     ORDER BY commit_seq, seq
     LIMIT :limit
    """
).bindparams(
    bindparam("project_ids", type_=_UUID_ARRAY),
    bindparam("library_ids", type_=_UUID_ARRAY),
    bindparam("space_ids", type_=_UUID_ARRAY),
    bindparam("user_id", type_=PG_UUID(as_uuid=True)),
)

_PURGE_SQL = text(
    """
    WITH boundary AS (
      SELECT max(commit_seq) AS p FROM sync_change_log
       WHERE commit_seq IS NOT NULL
         AND committed_at < now() - make_interval(days => :days)
    ), removed AS (
      DELETE FROM sync_change_log
       WHERE commit_seq IS NOT NULL AND commit_seq <= (SELECT p FROM boundary)
       RETURNING 1
    ), advanced AS (
      UPDATE sync_change_log_state
         SET min_retained_commit_seq = greatest(min_retained_commit_seq, (SELECT p FROM boundary))
       WHERE id = 1 AND (SELECT p FROM boundary) IS NOT NULL
       RETURNING min_retained_commit_seq
    )
    SELECT (SELECT p FROM boundary) AS boundary, (SELECT count(*) FROM removed) AS removed
    """
)


class PgChangeFeed:
    async def read_head(self, session: AsyncSession) -> int:
        return int((await session.execute(text(contract.HEAD_SQL))).scalar_one())

    async def min_retained_commit_seq(self, session: AsyncSession) -> int:
        result = await session.execute(
            text("SELECT min_retained_commit_seq FROM sync_change_log_state WHERE id = 1")
        )
        return int(result.scalar_one())

    async def read_changes(
        self,
        session: AsyncSession,
        *,
        scope: ScopeFilter,
        after_commit_seq: int,
        after_seq: int,
        limit: int,
    ) -> list[ChangeRef]:
        result = await session.execute(
            _READ_CHANGES_SQL,
            {
                "after_commit_seq": after_commit_seq,
                "after_seq": after_seq,
                "user_id": scope.user_id,
                "project_ids": list(scope.project_ids),
                "library_ids": list(scope.docs_library_ids),
                "space_ids": list(scope.space_ids),
                "limit": limit,
            },
        )
        return [
            ChangeRef(
                entity_table=row.entity_table,
                entity_id=row.entity_id,
                op=row.op,
                commit_seq=int(row.commit_seq),
                seq=int(row.seq),
            )
            for row in result
        ]

    async def purge_expired(
        self,
        session: AsyncSession,
        older_than_days: int = contract.CHANGE_LOG_RETENTION_DAYS,
    ) -> int:
        """期限切れの先頭区間を削除して削除件数を返す。呼び出し側がcommitする。"""
        locked = (
            await session.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": PURGE_LOCK_KEY}
            )
        ).scalar_one()
        if not locked:
            return 0
        row = (await session.execute(_PURGE_SQL, {"days": older_than_days})).one()
        return int(row.removed) if row.boundary is not None else 0
