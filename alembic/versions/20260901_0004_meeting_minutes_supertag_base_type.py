"""Allow the canonical Meeting Minutes Docs Supertag base type."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260901_0004"
down_revision = "20260901_0003"
branch_labels = None
depends_on = None


BASE_TYPES = (
    "note",
    "task",
    "decision",
    "risk",
    "question",
    "meeting",
    "person",
    "vendor",
    "device",
    "spec",
    "estimate",
    "evidence",
    "email",
    "url",
    "project",
    "project_information",
    "day",
    "record",
    "scenario",
    "scenario_episode",
    "scenario_scene",
    "scenario_canon",
    "scenario_branch",
    "meeting_minutes",
)
LEGACY_BASE_TYPES = BASE_TYPES[:-1]


def _base_type_check(values: tuple[str, ...]) -> str:
    return "base_type IN ({})".format(
        ", ".join(f"'{value}'" for value in values)
    )


def upgrade() -> None:
    op.drop_constraint(
        "ck_knowledge_supertags_base_type",
        "knowledge_supertags",
        type_="check",
    )
    op.create_check_constraint(
        "ck_knowledge_supertags_base_type",
        "knowledge_supertags",
        _base_type_check(BASE_TYPES),
    )


def downgrade() -> None:
    count = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM knowledge_supertags "
            "WHERE base_type = 'meeting_minutes'"
        )
    ).scalar_one()
    if count:
        raise RuntimeError(
            "Meeting Minutes Supertagが存在するためdowngradeできません。"
            "先に別base_typeへ明示的に移行してください。"
        )
    op.drop_constraint(
        "ck_knowledge_supertags_base_type",
        "knowledge_supertags",
        type_="check",
    )
    op.create_check_constraint(
        "ck_knowledge_supertags_base_type",
        "knowledge_supertags",
        _base_type_check(LEGACY_BASE_TYPES),
    )
