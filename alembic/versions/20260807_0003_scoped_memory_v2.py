"""Add Scoped Memory v2 lineage, evidence, audit, and job state.

Revision ID: 20260807_0003
Revises: 20260807_0002
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from src.security.field_crypto import decrypt_text_if_needed


revision = "20260807_0003"
down_revision = "20260807_0002"
branch_labels = None
depends_on = None


def _normalized_text(value: Any) -> str:
    """Keep the migration key byte-for-byte compatible with runtime memory keys."""
    text = " ".join(str(value or "").strip().casefold().split())
    return re.sub(r"[^0-9a-zぁ-んァ-ン一-龥]+", "", text)


def _runtime_dedupe_key(memory_type: Any, content: Any) -> str:
    material = f"{str(memory_type or '').casefold()}:{_normalized_text(content)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _projection_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _legacy_duplicate_key(*, canonical_id: Any, duplicate_id: Any, dedupe_key: str) -> str:
    suffix = hashlib.sha256(
        f"{canonical_id}:{duplicate_id}:{dedupe_key}".encode("utf-8")
    ).hexdigest()
    return f"legacy_duplicate:{suffix}"


def _build_legacy_dedupe_backfill(
    rows: Iterable[Mapping[str, Any]],
    *,
    decryptor: Callable[..., str | None] = decrypt_text_if_needed,
) -> list[dict[str, Any]]:
    """Plan a lossless backfill while selecting one canonical active row per key."""
    prepared: list[dict[str, Any]] = []
    for row in rows:
        plaintext = decryptor(
            row.get("content"),
            aad="context_memories.content",
        )
        prepared.append(
            {
                "id": row["id"],
                "user_id": row.get("user_id"),
                "scope_type": row.get("scope_type"),
                "scope_id": row.get("scope_id"),
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "dedupe_key": _runtime_dedupe_key(row.get("memory_type"), plaintext),
                "created_by_actor": str(row.get("user_id") or "legacy"),
                "projection_metadata": _projection_dict(row.get("projection_metadata")),
            }
        )

    active_groups: dict[tuple[Any, Any, Any, str], list[dict[str, Any]]] = {}
    for item in prepared:
        if item["status"] != "active":
            continue
        group = (
            item["user_id"],
            item["scope_type"],
            item["scope_id"],
            item["dedupe_key"],
        )
        active_groups.setdefault(group, []).append(item)

    for group in active_groups.values():
        group.sort(
            key=lambda item: (
                item["created_at"] is None,
                str(item["created_at"] or ""),
                str(item["id"]),
            )
        )
        canonical = group[0]
        for duplicate in group[1:]:
            canonical_key = duplicate["dedupe_key"]
            duplicate["dedupe_key"] = _legacy_duplicate_key(
                canonical_id=canonical["id"],
                duplicate_id=duplicate["id"],
                dedupe_key=canonical_key,
            )
            duplicate["projection_metadata"].update(
                {
                    "legacy_dedupe_duplicate_of": str(canonical["id"]),
                    "legacy_dedupe_canonical_key": canonical_key,
                    "migration_attention": "duplicate_active_memory_preserved",
                }
            )

    return [
        {
            "id": item["id"],
            "dedupe_key": item["dedupe_key"],
            "created_by_actor": item["created_by_actor"],
            "projection_metadata": json.dumps(
                item["projection_metadata"],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        for item in prepared
    ]


def _backfill_context_memory_dedupe(bind: Any) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT id, user_id, scope_type, scope_id, memory_type, content,
                   status, created_at, projection_metadata
              FROM context_memories
             ORDER BY created_at NULLS LAST, id
               FOR UPDATE
            """
        )
    ).mappings().all()
    plan = _build_legacy_dedupe_backfill(rows)
    if not plan:
        return
    bind.execute(
        sa.text(
            """
            UPDATE context_memories
               SET dedupe_key = :dedupe_key,
                   created_by_actor = coalesce(created_by_actor, :created_by_actor),
                   projection_metadata = CAST(:projection_metadata AS JSON)
             WHERE id = :id
            """
        ),
        plan,
    )


def upgrade() -> None:
    op.add_column(
        "context_memories",
        sa.Column("trust_level", sa.String(length=32), nullable=False, server_default="inferred"),
    )
    op.add_column(
        "context_memories",
        sa.Column("sensitivity", sa.String(length=32), nullable=False, server_default="normal"),
    )
    op.add_column(
        "context_memories",
        sa.Column("evidence_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
    )
    op.add_column(
        "context_memories",
        sa.Column("evidence_span", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column("context_memories", sa.Column("dedupe_key", sa.String(length=128), nullable=True))
    op.add_column(
        "context_memories",
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "context_memories",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("context_memories", sa.Column("created_by_actor", sa.String(length=120), nullable=True))
    op.add_column("context_memories", sa.Column("rejection_reason", sa.Text(), nullable=True))
    op.add_column(
        "context_memories",
        sa.Column("projection_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column("context_memories", sa.Column("migration_id", sa.String(length=120), nullable=True))
    op.create_foreign_key(
        "fk_context_memories_supersedes_id",
        "context_memories",
        "context_memories",
        ["supersedes_id"],
        ["id"],
        ondelete="SET NULL",
    )
    if op.get_context().as_sql:
        # Encrypted legacy content must be decrypted with the configured field
        # key before hashing. Render a fail-safe guard instead of emitting an
        # offline SQL file that would silently create incompatible keys.
        op.execute(
            sa.text(
                """
                DO $$
                BEGIN
                  RAISE EXCEPTION 'Scoped Memory dedupe backfill requires online Alembic execution';
                END
                $$
                """
            )
        )
    else:
        _backfill_context_memory_dedupe(op.get_bind())
    op.create_index("ix_context_memories_dedupe_key", "context_memories", ["dedupe_key"])
    op.create_index("ix_context_memories_supersedes_id", "context_memories", ["supersedes_id"])
    op.create_index("ix_context_memories_migration_id", "context_memories", ["migration_id"])
    op.create_index(
        "uq_context_memories_active_scope_dedupe",
        "context_memories",
        ["user_id", "scope_type", "scope_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "context_memory_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", sa.String(length=100), nullable=True),
        sa.Column("operation", sa.String(length=48), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("turn_context", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("before_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("after_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["memory_id"], ["context_memories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_context_memory_audits_memory_id", "context_memory_audits", ["memory_id"])
    op.create_index("ix_context_memory_audits_user_id", "context_memory_audits", ["user_id"])
    op.create_index("ix_context_memory_audits_operation", "context_memory_audits", ["operation"])
    op.create_index("ix_context_memory_audits_created_at", "context_memory_audits", ["created_at"])

    op.create_table(
        "scoped_memory_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.String(length=100), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_key", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["conversation_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "session_id", "message_key", name="uq_scoped_memory_jobs_turn"),
    )
    op.create_index("ix_scoped_memory_jobs_user_id", "scoped_memory_jobs", ["user_id"])
    op.create_index("ix_scoped_memory_jobs_session_id", "scoped_memory_jobs", ["session_id"])
    op.create_index("ix_scoped_memory_jobs_project_id", "scoped_memory_jobs", ["project_id"])
    op.create_index("ix_scoped_memory_jobs_status", "scoped_memory_jobs", ["status"])
    op.create_index("ix_scoped_memory_jobs_next_retry_at", "scoped_memory_jobs", ["next_retry_at"])


def downgrade() -> None:
    op.drop_index("ix_scoped_memory_jobs_next_retry_at", table_name="scoped_memory_jobs")
    op.drop_index("ix_scoped_memory_jobs_status", table_name="scoped_memory_jobs")
    op.drop_index("ix_scoped_memory_jobs_project_id", table_name="scoped_memory_jobs")
    op.drop_index("ix_scoped_memory_jobs_session_id", table_name="scoped_memory_jobs")
    op.drop_index("ix_scoped_memory_jobs_user_id", table_name="scoped_memory_jobs")
    op.drop_table("scoped_memory_jobs")
    op.drop_index("ix_context_memory_audits_created_at", table_name="context_memory_audits")
    op.drop_index("ix_context_memory_audits_operation", table_name="context_memory_audits")
    op.drop_index("ix_context_memory_audits_user_id", table_name="context_memory_audits")
    op.drop_index("ix_context_memory_audits_memory_id", table_name="context_memory_audits")
    op.drop_table("context_memory_audits")
    op.drop_index("uq_context_memories_active_scope_dedupe", table_name="context_memories")
    op.drop_index("ix_context_memories_migration_id", table_name="context_memories")
    op.drop_index("ix_context_memories_supersedes_id", table_name="context_memories")
    op.drop_index("ix_context_memories_dedupe_key", table_name="context_memories")
    op.drop_constraint("fk_context_memories_supersedes_id", "context_memories", type_="foreignkey")
    for column in (
        "migration_id",
        "projection_metadata",
        "rejection_reason",
        "created_by_actor",
        "version",
        "supersedes_id",
        "dedupe_key",
        "evidence_span",
        "evidence_refs",
        "sensitivity",
        "trust_level",
    ):
        op.drop_column("context_memories", column)
