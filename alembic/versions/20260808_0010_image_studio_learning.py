"""Add immutable Image Studio learning lineage.

Revision ID: 20260808_0010
Revises: 20260808_0009
"""

import importlib.util
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260808_0010"
down_revision = "20260808_0009"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def _purge_existing_image_studio_rows() -> None:
    """Re-purge Studio data before adding strict learning lineage fields."""
    path = Path(__file__).with_name("20260807_0004_runtime_purge.py")
    spec = importlib.util.spec_from_file_location("_aoi_image_studio_runtime_purge", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Image Studio purge helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.purge_existing_rows()


def upgrade() -> None:
    _purge_existing_image_studio_rows()
    uuid_type = _uuid()
    evaluations = "image_studio_evaluations"
    cases = "image_studio_cases"

    op.alter_column(evaluations, "generation_id", nullable=True)
    op.add_column(evaluations, sa.Column("output_record_id", uuid_type, nullable=True))
    op.add_column(evaluations, sa.Column("dimensions", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.add_column(evaluations, sa.Column("source_kind", sa.String(32), nullable=False, server_default="legacy_unverified"))
    op.add_column(evaluations, sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(evaluations, sa.Column("request_key_hash", sa.String(64), nullable=True))
    op.add_column(evaluations, sa.Column("request_fingerprint", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_is_evaluations_output_record_id",
        evaluations,
        "image_studio_outputs",
        ["output_record_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    for column in ("output_record_id", "source_kind"):
        op.create_index(f"ix_image_studio_evaluations_{column}", evaluations, [column])
    op.create_unique_constraint(
        "uq_image_studio_evaluations_project_request_key",
        evaluations,
        ["project_id", "request_key_hash"],
    )
    op.create_check_constraint(
        "ck_image_studio_evaluations_source_kind",
        evaluations,
        "source_kind IN ('legacy_unverified','output_evaluation')",
    )
    op.create_check_constraint(
        "ck_image_studio_evaluations_state_version",
        evaluations,
        "state_version >= 0",
    )
    op.create_check_constraint(
        "ck_image_studio_evaluations_v2_lineage",
        evaluations,
        "source_kind = 'legacy_unverified' OR "
        "(output_record_id IS NOT NULL AND asset_id IS NOT NULL AND "
        "request_key_hash IS NOT NULL AND request_fingerprint IS NOT NULL)",
    )

    for name, table in (
        ("output_record_id", "image_studio_outputs"),
        ("run_id", "image_studio_runs"),
        ("manifest_id", "image_studio_execution_manifests"),
    ):
        op.add_column(cases, sa.Column(name, uuid_type, nullable=True))
        op.create_foreign_key(
            f"fk_is_cases_{name}", cases, table, [name], ["id"], ondelete="RESTRICT"
        )
        op.create_index(f"ix_image_studio_cases_{name}", cases, [name])
    op.add_column(cases, sa.Column("snapshot_schema", sa.String(80), nullable=True))
    op.add_column(cases, sa.Column("snapshot_hash", sa.String(64), nullable=True))
    op.add_column(cases, sa.Column("source_kind", sa.String(32), nullable=False, server_default="legacy_unverified"))
    op.add_column(cases, sa.Column("source_key_hash", sa.String(64), nullable=True))
    op.add_column(cases, sa.Column("source_fingerprint", sa.String(64), nullable=True))
    op.add_column(cases, sa.Column("evaluation_version", sa.Integer(), nullable=True))
    op.add_column(cases, sa.Column("workflow_key", sa.String(160), nullable=True))
    op.add_column(cases, sa.Column("workflow_version", sa.String(80), nullable=True))
    op.add_column(cases, sa.Column("workflow_hash", sa.String(64), nullable=True))
    op.add_column(cases, sa.Column("model_key", sa.String(256), nullable=True))
    op.add_column(cases, sa.Column("seed", sa.BigInteger(), nullable=True))
    op.add_column(cases, sa.Column("parameter_projection", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.add_column(cases, sa.Column("bundle_provenance", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.create_index("ix_image_studio_cases_source_kind", cases, ["source_kind"])
    op.create_unique_constraint(
        "uq_image_studio_cases_evaluation_version",
        cases,
        ["evaluation_id", "evaluation_version"],
    )
    op.create_unique_constraint(
        "uq_image_studio_cases_project_source_key",
        cases,
        ["project_id", "source_key_hash"],
    )
    op.create_check_constraint(
        "ck_image_studio_cases_source_kind",
        cases,
        "source_kind IN ('legacy_unverified','output_evaluation')",
    )
    op.create_check_constraint(
        "ck_image_studio_cases_evaluation_version",
        cases,
        "evaluation_version IS NULL OR evaluation_version >= 0",
    )
    op.create_check_constraint(
        "ck_image_studio_cases_v2_lineage",
        cases,
        "source_kind = 'legacy_unverified' OR "
        "(evaluation_id IS NOT NULL AND output_record_id IS NOT NULL AND "
        "run_id IS NOT NULL AND manifest_id IS NOT NULL AND "
        "snapshot_schema IS NOT NULL AND snapshot_hash IS NOT NULL AND "
        "source_key_hash IS NOT NULL AND source_fingerprint IS NOT NULL AND "
        "evaluation_version IS NOT NULL)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    v2_evaluations = bind.execute(
        sa.text("SELECT count(*) FROM image_studio_evaluations WHERE source_kind <> 'legacy_unverified'")
    ).scalar_one()
    v2_cases = bind.execute(
        sa.text("SELECT count(*) FROM image_studio_cases WHERE source_kind <> 'legacy_unverified'")
    ).scalar_one()
    null_generations = bind.execute(
        sa.text("SELECT count(*) FROM image_studio_evaluations WHERE generation_id IS NULL")
    ).scalar_one()
    if v2_evaluations or v2_cases or null_generations:
        raise RuntimeError(
            "cannot downgrade Image Studio learning schema while V2 learning rows exist"
        )

    cases = "image_studio_cases"
    for constraint in (
        "ck_image_studio_cases_v2_lineage",
        "ck_image_studio_cases_evaluation_version",
        "ck_image_studio_cases_source_kind",
        "uq_image_studio_cases_project_source_key",
        "uq_image_studio_cases_evaluation_version",
    ):
        op.drop_constraint(constraint, cases, type_="unique" if constraint.startswith("uq_") else "check")
    op.drop_index("ix_image_studio_cases_source_kind", table_name=cases)
    for name in ("manifest_id", "run_id", "output_record_id"):
        op.drop_index(f"ix_image_studio_cases_{name}", table_name=cases)
        op.drop_constraint(f"fk_is_cases_{name}", cases, type_="foreignkey")
    for name in (
        "bundle_provenance", "parameter_projection", "seed", "model_key", "workflow_hash",
        "workflow_version", "workflow_key", "evaluation_version", "source_fingerprint",
        "source_key_hash", "source_kind", "snapshot_hash", "snapshot_schema", "manifest_id",
        "run_id", "output_record_id",
    ):
        op.drop_column(cases, name)

    evaluations = "image_studio_evaluations"
    for constraint in (
        "ck_image_studio_evaluations_v2_lineage",
        "ck_image_studio_evaluations_state_version",
        "ck_image_studio_evaluations_source_kind",
        "uq_image_studio_evaluations_project_request_key",
    ):
        op.drop_constraint(
            constraint,
            evaluations,
            type_="unique" if constraint.startswith("uq_") else "check",
        )
    for name in ("source_kind", "output_record_id"):
        op.drop_index(f"ix_image_studio_evaluations_{name}", table_name=evaluations)
    op.drop_constraint("fk_is_evaluations_output_record_id", evaluations, type_="foreignkey")
    for name in (
        "request_fingerprint", "request_key_hash", "state_version", "source_kind",
        "dimensions", "output_record_id",
    ):
        op.drop_column(evaluations, name)
    op.alter_column(evaluations, "generation_id", nullable=False)
