"""Add versioned Persona policy and lifecycle fields.

The original Persona core intentionally started with a small profile.  This
non-destructive revision adds the operational policy surface needed by MediaOps
while keeping every field optional so incomplete drafts remain saveable.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260901_0014"
down_revision = "20260901_0013"
branch_labels = None
depends_on = None


def _is_sqlite() -> bool:
    bind = op.get_bind()
    return str(getattr(getattr(bind, "dialect", None), "name", "")).lower() == "sqlite"


def _add_columns(table: str, columns: list[sa.Column]) -> None:
    if _is_sqlite():
        with op.batch_alter_table(table, recreate="always") as batch:
            for column in columns:
                batch.add_column(column)
    else:
        for column in columns:
            op.add_column(table, column)


def _drop_columns(table: str, names: list[str]) -> None:
    if _is_sqlite():
        with op.batch_alter_table(table, recreate="always") as batch:
            for name in reversed(names):
                batch.drop_column(name)
    else:
        for name in reversed(names):
            op.drop_column(table, name)


_RESOURCE_KIND_CHECK = (
    "resource_kind IN ("
    "'profile', 'reference', 'asset', 'persona_bible', 'character_bible', "
    "'world_bible', 'visual_style_reference', 'reference_image', 'posting_rule', "
    "'platform_rule', 'sensitive_rule', 'forbidden_content_rule', 'ip_rights_rule', "
    "'monetization_rule', 'kpi_definition', 'experiment_policy', 'topic_source', "
    "'idea_bank', 'high_performing_content', 'supporting_document')"
)


def _expand_resource_kind_check() -> None:
    if _is_sqlite():
        with op.batch_alter_table("media_persona_resources", recreate="always") as batch:
            batch.drop_constraint("ck_media_persona_resources_kind", type_="check")
            batch.create_check_constraint("ck_media_persona_resources_kind", _RESOURCE_KIND_CHECK)
    else:
        op.drop_constraint("ck_media_persona_resources_kind", "media_persona_resources", type_="check")
        op.create_check_constraint(
            "ck_media_persona_resources_kind",
            "media_persona_resources",
            _RESOURCE_KIND_CHECK,
        )


def upgrade() -> None:
    _expand_resource_kind_check()
    _add_columns(
        "media_personas",
        [
            sa.Column("state", sa.String(length=16), nullable=False, server_default="draft"),
            sa.Column("parent_brand_ref", sa.String(length=164), nullable=True),
        ],
    )
    op.create_index("ix_media_personas_state", "media_personas", ["state"])

    _add_columns(
        "media_persona_revisions",
        [
            sa.Column("niche", sa.Text(), nullable=True),
            sa.Column("positioning", sa.Text(), nullable=True),
            sa.Column("visual_identity", sa.JSON(), nullable=True),
            sa.Column("creative_direction", sa.Text(), nullable=True),
            sa.Column("allowed_subjects", sa.JSON(), nullable=True),
            sa.Column("prohibited_subjects", sa.JSON(), nullable=True),
            sa.Column("adult_policy", sa.String(length=32), nullable=True),
            sa.Column("sensitive_policy", sa.String(length=32), nullable=True),
            sa.Column("ip_policy", sa.String(length=32), nullable=True),
            sa.Column("disclosure_policy", sa.String(length=32), nullable=True),
            sa.Column("monetization_policy", sa.JSON(), nullable=True),
            sa.Column("kpi_objectives", sa.JSON(), nullable=True),
            sa.Column("default_language", sa.String(length=16), nullable=True),
            sa.Column("locale", sa.String(length=64), nullable=True),
            sa.Column("timezone", sa.String(length=64), nullable=True),
            sa.Column("research_policy", sa.JSON(), nullable=True),
            sa.Column("image_production_policy", sa.JSON(), nullable=True),
            sa.Column("video_production_policy", sa.JSON(), nullable=True),
            sa.Column("public_aliases", sa.JSON(), nullable=True),
        ],
    )


def downgrade() -> None:
    _drop_columns(
        "media_persona_revisions",
        [
            "niche",
            "positioning",
            "visual_identity",
            "creative_direction",
            "allowed_subjects",
            "prohibited_subjects",
            "adult_policy",
            "sensitive_policy",
            "ip_policy",
            "disclosure_policy",
            "monetization_policy",
            "kpi_objectives",
            "default_language",
            "locale",
            "timezone",
            "research_policy",
            "image_production_policy",
            "video_production_policy",
            "public_aliases",
        ],
    )
    op.drop_index("ix_media_personas_state", table_name="media_personas")
    _drop_columns("media_personas", ["state", "parent_brand_ref"])
