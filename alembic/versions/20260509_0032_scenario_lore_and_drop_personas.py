"""シナリオロアブック化とユーザーペルソナ削除

Revision ID: 20260509_0032
Revises: 20260509_0031
Create Date: 2026-05-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260509_0032"
down_revision = "20260509_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "world_books",
        sa.Column("scenario_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_world_books_scenario_id",
        "world_books",
        "scenarios",
        ["scenario_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_world_books_scenario_id", "world_books", ["scenario_id"])

    op.execute("DROP TABLE IF EXISTS automation_logs")
    op.execute("DROP TABLE IF EXISTS automation_rules")

    op.drop_index("ix_user_personas_user_id", table_name="user_personas")
    op.drop_table("user_personas")


def downgrade() -> None:
    op.create_table(
        "user_personas",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_index("ix_user_personas_user_id", "user_personas", ["user_id"])

    op.create_table(
        "automation_rules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("trigger_type", sa.String(50), nullable=False),
        sa.Column("trigger_config", sa.JSON(), server_default=sa.text("'{}'::json")),
        sa.Column("conditions", sa.JSON(), server_default=sa.text("'[]'::json")),
        sa.Column("action_type", sa.String(50), nullable=False),
        sa.Column("action_config", sa.JSON(), server_default=sa.text("'{}'::json")),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("project_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column("run_count", sa.Integer(), server_default="0"),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_table(
        "automation_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "rule_id",
            UUID(as_uuid=True),
            sa.ForeignKey("automation_rules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("trigger_data", sa.JSON(), server_default=sa.text("'{}'::json")),
        sa.Column("action_result", sa.JSON(), server_default=sa.text("'{}'::json")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )

    op.drop_index("ix_world_books_scenario_id", table_name="world_books")
    op.drop_constraint("fk_world_books_scenario_id", "world_books", type_="foreignkey")
    op.drop_column("world_books", "scenario_id")
