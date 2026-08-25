"""Allow record supertag base type.

Revision ID: 20260727_0001
Revises: 20260726_0001
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260727_0001"
down_revision: Union[str, None] = "20260726_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BASE_TYPES_WITH_RECORD = (
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
)

BASE_TYPES_WITHOUT_RECORD = tuple(
    value for value in BASE_TYPES_WITH_RECORD if value != "record"
)


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _check_constraint_exists(table_name: str, constraint_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        item.get("name") == constraint_name
        for item in inspector.get_check_constraints(table_name)
    )


def _base_type_check(values: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"base_type IN ({allowed})"


def upgrade() -> None:
    if not _table_exists("knowledge_supertags"):
        return
    if _check_constraint_exists(
        "knowledge_supertags", "ck_knowledge_supertags_base_type"
    ):
        op.drop_constraint(
            "ck_knowledge_supertags_base_type",
            "knowledge_supertags",
            type_="check",
        )
    op.create_check_constraint(
        "ck_knowledge_supertags_base_type",
        "knowledge_supertags",
        _base_type_check(BASE_TYPES_WITH_RECORD),
    )


def downgrade() -> None:
    if not _table_exists("knowledge_supertags"):
        return
    record_count = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM knowledge_supertags "
            "WHERE base_type = 'record'"
        )
    ).scalar_one()
    if record_count:
        raise RuntimeError(
            "record Supertagが存在するためdowngradeできません。"
            "削除または別base_typeへの明示的な移行を先に実施してください。"
        )
    if _check_constraint_exists(
        "knowledge_supertags", "ck_knowledge_supertags_base_type"
    ):
        op.drop_constraint(
            "ck_knowledge_supertags_base_type",
            "knowledge_supertags",
            type_="check",
        )
    op.create_check_constraint(
        "ck_knowledge_supertags_base_type",
        "knowledge_supertags",
        _base_type_check(BASE_TYPES_WITHOUT_RECORD),
    )
