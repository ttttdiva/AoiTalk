"""Docs正本のシナリオ執筆基盤と会話フォークを追加する。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260801_0002"
down_revision = "20260801_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_sessions",
        sa.Column("parent_session_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "conversation_sessions",
        sa.Column("forked_from_message_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversation_sessions_parent_session_id",
        "conversation_sessions",
        "conversation_sessions",
        ["parent_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_conversation_sessions_forked_from_message_id",
        "conversation_sessions",
        "conversation_messages",
        ["forked_from_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_conversation_sessions_parent_session_id",
        "conversation_sessions",
        ["parent_session_id"],
    )

    for table_name in (
        "scenarios",
        "scenario_episodes",
        "scenario_scenes",
        "scenario_canon_entries",
        "trpg_scenario_documents",
    ):
        op.add_column(
            table_name,
            sa.Column("knowledge_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"fk_{table_name}_knowledge_node_id",
            table_name,
            "knowledge_nodes",
            ["knowledge_node_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            f"ix_{table_name}_knowledge_node_id",
            table_name,
            ["knowledge_node_id"],
        )

    op.create_table(
        "scenario_authoring_branches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scenario_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("root_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("base_branch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fork_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fork_offset", sa.Integer(), nullable=True),
        sa.Column("fork_etag", sa.String(length=64), nullable=True),
        sa.Column(
            "fork_manifest",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["scenario_id"], ["scenarios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["root_node_id"], ["knowledge_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["base_branch_id"], ["scenario_authoring_branches.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["fork_node_id"], ["knowledge_nodes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["source_message_id"], ["conversation_messages.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "root_node_id", name="uq_scenario_authoring_branches_root_node"
        ),
    )
    op.create_index(
        "ix_scenario_authoring_branches_scenario_id",
        "scenario_authoring_branches",
        ["scenario_id"],
    )
    op.create_index(
        "ix_scenario_authoring_branches_base_branch_id",
        "scenario_authoring_branches",
        ["base_branch_id"],
    )

    op.add_column(
        "scenario_writing_sessions",
        sa.Column("target_node_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "scenario_writing_sessions",
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "scenario_writing_sessions",
        sa.Column(
            "context_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.create_foreign_key(
        "fk_scenario_writing_sessions_target_node_id",
        "scenario_writing_sessions",
        "knowledge_nodes",
        ["target_node_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_scenario_writing_sessions_branch_id",
        "scenario_writing_sessions",
        "scenario_authoring_branches",
        ["branch_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_scenario_writing_sessions_target_node_id",
        "scenario_writing_sessions",
        ["target_node_id"],
    )

    op.add_column(
        "scenario_play_sessions",
        sa.Column("source_scenario_node_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column("source_snapshot_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column(
            "source_revision_manifest",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column(
            "source_snapshot",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.create_foreign_key(
        "fk_scenario_play_sessions_source_scenario_node_id",
        "scenario_play_sessions",
        "knowledge_nodes",
        ["source_scenario_node_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_scenario_play_sessions_source_scenario_node_id",
        "scenario_play_sessions",
        ["source_scenario_node_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scenario_play_sessions_source_scenario_node_id",
        table_name="scenario_play_sessions",
    )
    op.drop_constraint(
        "fk_scenario_play_sessions_source_scenario_node_id",
        "scenario_play_sessions",
        type_="foreignkey",
    )
    for column_name in (
        "source_snapshot",
        "source_revision_manifest",
        "source_snapshot_hash",
        "source_scenario_node_id",
    ):
        op.drop_column("scenario_play_sessions", column_name)

    op.drop_index(
        "ix_scenario_writing_sessions_target_node_id",
        table_name="scenario_writing_sessions",
    )
    op.drop_constraint(
        "fk_scenario_writing_sessions_branch_id",
        "scenario_writing_sessions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_scenario_writing_sessions_target_node_id",
        "scenario_writing_sessions",
        type_="foreignkey",
    )
    for column_name in ("context_json", "branch_id", "target_node_id"):
        op.drop_column("scenario_writing_sessions", column_name)

    op.drop_index(
        "ix_scenario_authoring_branches_base_branch_id",
        table_name="scenario_authoring_branches",
    )
    op.drop_index(
        "ix_scenario_authoring_branches_scenario_id",
        table_name="scenario_authoring_branches",
    )
    op.drop_table("scenario_authoring_branches")

    for table_name in reversed(
        (
            "scenarios",
            "scenario_episodes",
            "scenario_scenes",
            "scenario_canon_entries",
            "trpg_scenario_documents",
        )
    ):
        op.drop_index(f"ix_{table_name}_knowledge_node_id", table_name=table_name)
        op.drop_constraint(
            f"fk_{table_name}_knowledge_node_id",
            table_name,
            type_="foreignkey",
        )
        op.drop_column(table_name, "knowledge_node_id")

    op.drop_index(
        "ix_conversation_sessions_parent_session_id",
        table_name="conversation_sessions",
    )
    op.drop_constraint(
        "fk_conversation_sessions_forked_from_message_id",
        "conversation_sessions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_conversation_sessions_parent_session_id",
        "conversation_sessions",
        type_="foreignkey",
    )
    op.drop_column("conversation_sessions", "forked_from_message_id")
    op.drop_column("conversation_sessions", "parent_session_id")
