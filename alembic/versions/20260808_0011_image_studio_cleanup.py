"""Remove the Image Studio schema from the AoiTalk database.

Revision ID: 20260808_0011
Revises: 20260807_0004, 20260808_0010

The Image Studio tables are product-owned data and are intentionally not
restored by a downgrade.  They are dropped in reverse foreign-key order with
``RESTRICT`` so an unexpected dependency (including a non-Studio table) stops
the migration instead of being removed implicitly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op
import sqlalchemy as sa


revision = "20260808_0011"
down_revision = ("20260807_0004", "20260808_0010")
branch_labels = None
depends_on = None


# Keep this list auditable against the Image Studio migrations/models.  The
# order is the reverse dependency order: children and join tables first,
# foundations last.  In particular, run dependants (external sessions, cases,
# and outputs) precede ``runs`` even though those tables were introduced by
# later migrations.  Every identifier is a fixed source-controlled literal.
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


_DEDUPE_LEGACY_SLUG = "project_management_assistant"
_LEGACY_RUNTIME_AMBIGUITY_FLAG = "_aoi_image_studio_legacy_runtime_ambiguous"


def _current_schema(bind: sa.engine.Connection) -> str | None:
    try:
        return bind.execute(sa.text("SELECT current_schema()")).scalar()
    except Exception:
        return None


def _column_metadata(
    bind: sa.engine.Connection,
    table: str,
    column: str,
) -> tuple[bool, str | None]:
    schema = _current_schema(bind)
    if schema is None:
        return False, None
    try:
        row = bind.execute(
            sa.text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = :table "
                "AND column_name = :column"
            ),
            {"schema": schema, "table": table, "column": column},
        ).first()
    except Exception:
        return False, None
    return row is not None, (str(row[0]) if row is not None else None)


def _dedupe_signature_needs_reconcile(bind: sa.engine.Connection) -> bool:
    """Audit generic character references before invoking the old merge.

    A clean canonical row (or no character rows) is the normal/dual-head
    signature and returns ``False``.  Any remaining legacy slug/reference is
    the ambiguous old-runtime signature and requests the idempotent merge.
    """

    has_characters, _ = _column_metadata(bind, "characters", "slug")
    if not has_characters:
        return False
    markers: list[tuple[str, str, str]] = [
        ("characters", "slug", "slug = :legacy"),
        ("conversation_sessions", "character_name", "character_name = :legacy"),
        ("conversation_archives", "character_name", "character_name = :legacy"),
        ("conversation_history", "character_name", "character_name = :legacy"),
        ("conversation_messages", "sender_id", "sender_id = :legacy"),
        ("users", "preferred_character", "preferred_character = :legacy"),
        ("spotify_activity_logs", "character_name", "character_name = :legacy"),
        ("spotify_session_summaries", "character_name", "character_name = :legacy"),
        (
            "conversation_participants",
            "participant_id",
            "participant_type = 'character' AND participant_id = :legacy",
        ),
    ]
    for table, column, predicate in markers:
        has_table_column, _ = _column_metadata(bind, table, column)
        if not has_table_column:
            continue
        try:
            if bind.execute(
                sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {predicate})"),
                {"legacy": _DEDUPE_LEGACY_SLUG},
            ).scalar():
                return True
        except Exception:
            # An unexpected generic-schema shape is not a reason to silently
            # leave a legacy marker behind; let the audited merge fail closed.
            return True

    has_metadata, _ = _column_metadata(bind, "conversation_messages", "message_metadata")
    if has_metadata:
        try:
            if bind.execute(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM conversation_messages "
                    "WHERE message_metadata::jsonb ->> 'character_name' = :legacy)"
                ),
                {"legacy": _DEDUPE_LEGACY_SLUG},
            ).scalar():
                return True
        except Exception:
            return True

    has_group_names, _ = _column_metadata(bind, "conversation_sessions", "group_character_names")
    if has_group_names:
        try:
            if bind.execute(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM conversation_sessions "
                    "WHERE jsonb_typeof(group_character_names::jsonb) = 'array' "
                    "AND EXISTS (SELECT 1 FROM jsonb_array_elements_text "
                    "(group_character_names::jsonb) item(value) "
                    "WHERE item.value = :legacy))"
                ),
                {"legacy": _DEDUPE_LEGACY_SLUG},
            ).scalar():
                return True
        except Exception:
            return True
    return False


def _reconcile_ambiguous_character_dedupe() -> None:
    bind = op.get_bind()
    if bind is None or not hasattr(bind, "execute"):
        return
    # Only the historical runtime-only 0004 path is allowed to reconcile the
    # generic character dedupe here.  Normal and dual-head upgrades have a
    # canonical 0004 row and must remain generic-data no-ops.  The runtime
    # migration sets this connection-local marker only when it detects a
    # complete pre-existing runtime footprint under the ambiguous revision.
    try:
        ambiguous_runtime = bool(bind.info.pop(_LEGACY_RUNTIME_AMBIGUITY_FLAG, False))
        if not ambiguous_runtime:
            return
    except Exception:
        return
    if not _dedupe_signature_needs_reconcile(bind):
        return

    path = Path(__file__).with_name("20260807_0004_dedupe_project_manager_character.py")
    spec = importlib.util.spec_from_file_location("_aoi_project_manager_dedupe", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load canonical character dedupe migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.reconcile_project_manager_character(bind)


def _purge_dead_image_studio_navigation_tab() -> None:
    """Remove only ``navigation_tabs.image_studio`` from generic user JSON."""

    bind = op.get_bind()
    if bind is None or not hasattr(bind, "execute"):
        return
    present, udt_name = _column_metadata(bind, "users", "user_settings")
    if not present or udt_name not in {"json", "jsonb"}:
        return
    cast_suffix = "::json" if udt_name == "json" else ""
    bind.execute(
        sa.text(
            f"""
            UPDATE users
               SET user_settings =
                   (user_settings::jsonb #- '{{navigation_tabs,image_studio}}'){cast_suffix}
             WHERE user_settings IS NOT NULL
               AND jsonb_typeof(user_settings::jsonb) = 'object'
               AND jsonb_typeof(user_settings::jsonb -> 'navigation_tabs') = 'object'
               AND user_settings::jsonb -> 'navigation_tabs' ? 'image_studio'
            """
        )
    )


def upgrade() -> None:
    _reconcile_ambiguous_character_dedupe()
    _purge_dead_image_studio_navigation_tab()
    for table in IMAGE_STUDIO_TABLES_REVERSE_FK:
        op.execute(sa.text(f'DROP TABLE IF EXISTS "{table}" RESTRICT'))


def downgrade() -> None:
    raise RuntimeError(
        "Image Studio cleanup is irreversible: dropped tables and their data "
        "cannot be restored by Alembic downgrade"
    )
