"""Purge legacy Image Studio rows before strict V2 audits run.

Revision ID: 20260807_0004_runtime_purge
Revises: 20260807_0004_runtime

Image Studio is being removed from AoiTalk.  Existing rows are therefore
discarded deliberately, before the asset/identity/evaluation V2 migrations
add strict uniqueness and lineage constraints.  Tables are checked at runtime
so this bridge also works for old databases that only have the legacy subset.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260807_0004_runtime_purge"
down_revision = "20260807_0004_runtime"
branch_labels = None
depends_on = None


# Children first.  The list covers both the legacy 14 tables and the V2
# runtime/domain tables that may already exist when upgrading an old runtime
# revision.  Missing tables are harmless; the bridge is intentionally
# idempotent.
IMAGE_STUDIO_TABLES_REVERSE_FK = (
    "image_studio_repair_stages",
    "image_studio_structured_commands",
    "image_studio_project_event_cursors",
    "image_studio_output_evaluations",
    "image_studio_reroll_requests",
    "image_studio_reference_bindings",
    "image_studio_calibrations",
    "image_studio_pattern_set_items",
    "image_studio_pattern_set_versions",
    "image_studio_pattern_sets",
    "image_studio_pattern_versions",
    "image_studio_patterns",
    "image_studio_identity_asset_bindings",
    "image_studio_identity_pack_versions",
    "image_studio_identity_packs",
    "image_studio_run_events",
    "image_studio_external_sessions",
    "image_studio_cases",
    "image_studio_evaluations",
    "image_studio_outputs",
    "image_studio_references",
    "image_studio_assets",
    "image_studio_runs",
    "image_studio_execution_manifests",
    "image_studio_batches",
    "image_studio_shot_revisions",
    "image_studio_shot_branches",
    "image_studio_shots",
    "image_studio_presets",
    "image_studio_workflows",
    "image_studio_concepts",
    "image_studio_characters",
    "image_studio_generations",
)


def _delete_if_present(table: str) -> None:
    # A DO block avoids compiling a DELETE against a table that is absent in a
    # partially upgraded/legacy database.  Identifiers are fixed literals in
    # this migration, never user input.
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
              IF to_regclass('{table}') IS NOT NULL THEN
                EXECUTE 'DELETE FROM "{table}"';
              END IF;
            END
            $$
            """
        )
    )


def upgrade() -> None:
    purge_existing_rows()


def purge_existing_rows() -> None:
    """Delete all currently known Studio rows without touching generic data.

    Later V2 revisions call this idempotent helper at their own upgrade
    boundary as well.  That closes the gap for databases whose current
    revision is already 0005--0009 (the bridge itself has run, but new or
    malformed Studio rows may have been inserted since then).
    """
    for table in IMAGE_STUDIO_TABLES_REVERSE_FK:
        _delete_if_present(table)


def downgrade() -> None:
    raise RuntimeError(
        "Image Studio purge is irreversible: deleted rows cannot be restored"
    )
