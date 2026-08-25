"""Converge the conversation dispatch idempotency schema with the models.

20260727_0002 を適用済みとして記録している環境の一部が、旧設計
(session_id + client_message_id を鍵にする版)のままになっている。
alembic は同じ revision を再実行しないため、`agent_runs.client_message_key`
`agent_runs.request_fingerprint`、outbox の `user_id` などが存在せず、
HTTP dispatch(メッセージ再実行・編集)が UndefinedColumnError で必ず 500 になる。

この migration は現状を調べて足りない部分だけを埋め、モデル定義の形へ収束させる。
既に 20260727_0002 の最終形が入っている環境では何も変更しない。

Revision ID: 20260727_0004
Revises: 20260727_0003
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op


revision = "20260727_0004"
down_revision = "20260727_0003"
branch_labels = None
depends_on = None


def _client_message_key(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def _has_column(bind, table: str, column: str) -> bool:
    return bind.execute(
        sa.text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = :table
              AND column_name = :column
            """
        ),
        {"table": table, "column": column},
    ).first() is not None


def _has_constraint(bind, table: str, constraint: str) -> bool:
    return bind.execute(
        sa.text(
            """
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = to_regclass(:table)
              AND conname = :constraint
            """
        ),
        {"table": table, "constraint": constraint},
    ).first() is not None


def _converge_agent_runs(bind) -> None:
    op.execute(
        "ALTER TABLE agent_runs "
        "ADD COLUMN IF NOT EXISTS client_message_id VARCHAR(512)"
    )
    op.execute(
        "ALTER TABLE agent_runs "
        "ADD COLUMN IF NOT EXISTS client_message_key VARCHAR(64)"
    )
    op.execute(
        "ALTER TABLE agent_runs "
        "ADD COLUMN IF NOT EXISTS request_fingerprint VARCHAR(64)"
    )

    # client_message_id を持つ既存runへ鍵を補完する。旧unique制約は
    # (session_id, client_message_id) だったため重複は起きない想定だが、
    # 制約作成が落ちないよう最初の1件だけを採用する。
    pending = bind.execute(
        sa.text(
            """
            SELECT id, session_id, user_id, client_message_id
            FROM agent_runs
            WHERE client_message_id IS NOT NULL
              AND client_message_key IS NULL
            ORDER BY created_at, id
            """
        )
    )
    update_key = sa.text(
        """
        UPDATE agent_runs
        SET client_message_key = :client_message_key
        WHERE id = :run_id
        """
    )
    seen: set[tuple[str, str, str]] = set()
    for row in pending:
        client_message_id = str(row.client_message_id or "").strip()
        identity = (
            str(row.session_id),
            str(row.user_id or ""),
            client_message_id,
        )
        if not client_message_id or len(client_message_id) > 512 or identity in seen:
            continue
        seen.add(identity)
        bind.execute(
            update_key,
            {
                "run_id": row.id,
                "client_message_key": _client_message_key(client_message_id),
            },
        )

    op.execute(
        "ALTER TABLE agent_runs "
        "DROP CONSTRAINT IF EXISTS uq_agent_runs_session_client_message"
    )
    if not _has_constraint(
        bind, "agent_runs", "uq_agent_runs_session_user_client_message_key"
    ):
        op.create_unique_constraint(
            "uq_agent_runs_session_user_client_message_key",
            "agent_runs",
            ["session_id", "user_id", "client_message_key"],
        )


def _create_dispatch_outbox() -> None:
    op.create_table(
        "conversation_dispatch_outbox",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.String(length=200), nullable=False),
        sa.Column("client_message_id", sa.String(length=512), nullable=False),
        sa.Column("client_message_key", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["conversation_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint(
            "session_id",
            "user_id",
            "client_message_key",
            name="uq_conversation_dispatch_outbox_session_user_client_key",
        ),
    )
    op.create_index(
        "ix_conversation_dispatch_outbox_session_id",
        "conversation_dispatch_outbox",
        ["session_id"],
    )
    op.create_index(
        "ix_conversation_dispatch_outbox_status",
        "conversation_dispatch_outbox",
        ["status"],
    )
    op.create_index(
        "ix_conversation_dispatch_outbox_lease_expires_at",
        "conversation_dispatch_outbox",
        ["lease_expires_at"],
    )


def upgrade() -> None:
    bind = op.get_bind()
    _converge_agent_runs(bind)
    outbox_exists = (
        bind.execute(
            sa.text("SELECT to_regclass('conversation_dispatch_outbox')")
        ).scalar()
        is not None
    )
    if not outbox_exists:
        _create_dispatch_outbox()
        return
    if not _has_column(bind, "conversation_dispatch_outbox", "client_message_key"):
        # outbox は配送待ちの一時キュー。旧設計の行からは user_id と
        # request_fingerprint を復元できないため、旧形のときだけ作り直す。
        op.drop_table("conversation_dispatch_outbox")
        _create_dispatch_outbox()


def downgrade() -> None:
    # 20260727_0002 の downgrade が目標形からの巻き戻しを担当する。
    # ここでの巻き戻しは不整合な旧形を再現するだけなので何もしない。
    pass
