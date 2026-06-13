"""unify legacy user memories into context memories

Revision ID: 20260606_0001
Revises: 20260526_0001
Create Date: 2026-06-06
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260606_0001"
down_revision: Union[str, None] = "20260526_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALLOWED_MEMORY_TYPES = {
    "fact",
    "preference",
    "constraint",
    "project",
    "workflow",
    "relationship",
    "instruction",
}


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _normalize_memory_type(value: object) -> str:
    memory_type = str(value or "").strip().lower()
    return memory_type if memory_type in _ALLOWED_MEMORY_TYPES else "fact"


def upgrade() -> None:
    if not _has_table("user_memories"):
        return

    if _has_table("context_memories"):
        bind = op.get_bind()
        rows = bind.execute(
            sa.text(
                """
                SELECT id, user_id, content, source, category, is_active,
                       memory_metadata, created_at, updated_at
                FROM user_memories
                """
            )
        ).mappings()

        insert_stmt = sa.text(
            """
            INSERT INTO context_memories (
                id, user_id, scope_type, scope_id, memory_type, title, content,
                structured_data, source_type, source_ref, confidence, importance,
                status, is_pinned, created_at, updated_at, last_used_at, expires_at
            )
            VALUES (
                :id, :user_id, 'user', :user_id, :memory_type, NULL, :content,
                CAST(:structured_data AS JSON), :source_type, :source_ref,
                :confidence, :importance, :status, :is_pinned,
                :created_at, :updated_at, NULL, NULL
            )
            ON CONFLICT (id) DO NOTHING
            """
        )

        for row in rows:
            source = row.get("source") or "auto"
            category = row.get("category") or "general"
            metadata = row.get("memory_metadata") or {}
            structured_data = {
                "legacy_source": "user_memories",
                "legacy_category": category,
                "legacy_metadata": metadata,
            }
            bind.execute(
                insert_stmt,
                {
                    "id": row["id"],
                    "user_id": row["user_id"],
                    "memory_type": _normalize_memory_type(category),
                    "content": row["content"],
                    "structured_data": json.dumps(structured_data, ensure_ascii=False),
                    "source_type": "manual" if source == "manual" else "legacy_auto",
                    "source_ref": f"legacy:user_memories:{row['id']}",
                    "confidence": 1.0 if source == "manual" else 0.7,
                    "importance": 7 if source == "manual" else 5,
                    "status": "active" if row.get("is_active") else "archived",
                    "is_pinned": source == "manual",
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                },
            )

    op.drop_table("user_memories")


def downgrade() -> None:
    if not _has_table("user_memories"):
        op.create_table(
            "user_memories",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", sa.String(length=100), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("source", sa.String(length=50), nullable=True),
            sa.Column("category", sa.String(length=50), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=True),
            sa.Column("memory_metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_user_memories_user_active",
            "user_memories",
            ["user_id", "is_active"],
        )

    if not _has_table("context_memories"):
        return

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT id, user_id, content, memory_type, structured_data, source_type,
                   status, created_at, updated_at
            FROM context_memories
            WHERE scope_type = 'user'
              AND source_ref LIKE 'legacy:user_memories:%'
            """
        )
    ).mappings()
    insert_stmt = sa.text(
        """
        INSERT INTO user_memories (
            id, user_id, content, source, category, is_active,
            memory_metadata, created_at, updated_at
        )
        VALUES (
            :id, :user_id, :content, :source, :category, :is_active,
            CAST(:memory_metadata AS JSON), :created_at, :updated_at
        )
        ON CONFLICT (id) DO NOTHING
        """
    )
    for row in rows:
        structured_data = row.get("structured_data") or {}
        bind.execute(
            insert_stmt,
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "content": row["content"],
                "source": "manual" if row.get("source_type") == "manual" else "auto",
                "category": row.get("memory_type") or "general",
                "is_active": row.get("status") == "active",
                "memory_metadata": json.dumps(
                    structured_data.get("legacy_metadata") or {},
                    ensure_ascii=False,
                ),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            },
        )
