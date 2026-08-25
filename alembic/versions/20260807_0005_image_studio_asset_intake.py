"""Add Image Studio Asset intake and metadata provenance fields.

Revision ID: 20260807_0005
Revises: 20260807_0004_runtime_purge
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260807_0005"
down_revision = "20260807_0004_runtime_purge"
branch_labels = None
depends_on = None


def _normalize_and_audit_checksums() -> None:
    """Canonicalize valid checksums and fail closed on unsafe legacy rows."""

    op.execute(sa.text("LOCK TABLE image_studio_assets IN SHARE ROW EXCLUSIVE MODE"))
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1
                  FROM image_studio_assets
                 WHERE checksum_sha256 IS NOT NULL
                   AND checksum_sha256 !~* '^[0-9a-f]{64}$'
              ) THEN
                RAISE EXCEPTION
                  'image_studio_assets has malformed SHA-256 checksums; '
                  'audit the rows before retrying migration';
              END IF;
              IF EXISTS (
                SELECT 1
                  FROM image_studio_assets
                 WHERE storage_state = 'ready'
                   AND checksum_sha256 IS NULL
              ) THEN
                RAISE EXCEPTION
                  'image_studio_assets has ready rows without checksums; '
                  'audit the rows and files before retrying migration';
              END IF;
              IF EXISTS (
                SELECT 1
                  FROM image_studio_assets
                 WHERE storage_state = 'ready'
                 GROUP BY project_id, lower(checksum_sha256)
                HAVING count(*) > 1
              ) THEN
                RAISE EXCEPTION
                  'image_studio_assets has duplicate ready checksums inside a Project; '
                  'audit the rows and files before retrying migration';
              END IF;
            END
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE image_studio_assets
               SET checksum_sha256 = lower(checksum_sha256)
             WHERE checksum_sha256 IS NOT NULL
               AND checksum_sha256 <> lower(checksum_sha256)
            """
        )
    )


def _prepare_delivery_keys_for_downgrade() -> None:
    """Deterministically restore the all-state delivery-key uniqueness of 0004."""

    op.execute(sa.text("LOCK TABLE image_studio_assets IN SHARE ROW EXCLUSIVE MODE"))
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1
                  FROM image_studio_assets
                 WHERE delivery_key IS NOT NULL
                   AND storage_state = 'ready'
                 GROUP BY project_id, delivery_key
                HAVING count(*) > 1
              ) THEN
                RAISE EXCEPTION
                  'image_studio_assets has multiple ready rows for a delivery key; '
                  'audit the rows before retrying downgrade';
              END IF;
            END
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            WITH ranked_delivery_keys AS (
              SELECT id,
                     row_number() OVER (
                       PARTITION BY project_id, delivery_key
                       ORDER BY
                         CASE WHEN storage_state = 'ready' THEN 0 ELSE 1 END,
                         created_at ASC,
                         id ASC
                     ) AS keep_rank
                FROM image_studio_assets
               WHERE delivery_key IS NOT NULL
            )
            UPDATE image_studio_assets AS asset
               SET delivery_key = NULL
              FROM ranked_delivery_keys AS ranked
             WHERE asset.id = ranked.id
               AND ranked.keep_rank > 1
               AND asset.storage_state <> 'ready'
            """
        )
    )


def upgrade() -> None:
    _normalize_and_audit_checksums()
    op.add_column(
        "image_studio_assets",
        sa.Column(
            "source_type",
            sa.String(length=32),
            nullable=False,
            server_default="legacy",
        ),
    )
    op.add_column(
        "image_studio_assets",
        sa.Column(
            "metadata_provenance",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE image_studio_assets
               SET source_type = CASE
                     WHEN generation_id IS NOT NULL
                       OR metadata ->> 'source' = 'comfyui'
                       THEN 'comfyui'
                     ELSE 'legacy'
                   END,
                   metadata_provenance = jsonb_strip_nulls(jsonb_build_object(
                     'checksum_sha256', CASE WHEN checksum_sha256 IS NOT NULL THEN
                       jsonb_build_object(
                         'classification', 'parsed',
                         'source', 'migration:20260807_0005',
                         'confidence', 1.0,
                         'validated', false
                       ) END,
                     'mime_type', CASE WHEN mime_type IS NOT NULL THEN
                       jsonb_build_object(
                         'classification', 'parsed',
                         'source', 'migration:20260807_0005',
                         'confidence', 0.5,
                         'validated', false
                       ) END,
                     'byte_size', CASE WHEN byte_size IS NOT NULL THEN
                       jsonb_build_object(
                         'classification', 'exact',
                         'source', 'migration:20260807_0005',
                         'confidence', 1.0,
                         'validated', false
                       ) END,
                     'width', CASE WHEN width IS NOT NULL THEN
                       jsonb_build_object(
                         'classification', 'parsed',
                         'source', 'migration:20260807_0005',
                         'confidence', 0.5,
                         'validated', false
                       ) END,
                     'height', CASE WHEN height IS NOT NULL THEN
                       jsonb_build_object(
                         'classification', 'parsed',
                         'source', 'migration:20260807_0005',
                         'confidence', 0.5,
                         'validated', false
                       ) END
                   ))::json
            """
        )
    )
    op.create_check_constraint(
        "ck_image_studio_assets_source_type",
        "image_studio_assets",
        "source_type IN "
        "('upload','comfyui','browser_handoff','url_import',"
        "'external_editor','derived_control','legacy')",
    )
    op.create_index(
        "ix_image_studio_assets_source_type",
        "image_studio_assets",
        ["source_type"],
    )
    op.drop_index(
        "uq_image_studio_assets_project_delivery_key",
        table_name="image_studio_assets",
    )
    op.create_index(
        "uq_image_studio_assets_project_delivery_key",
        "image_studio_assets",
        ["project_id", "delivery_key"],
        unique=True,
        postgresql_where=sa.text(
            "delivery_key IS NOT NULL AND storage_state = 'ready'"
        ),
    )
    op.create_check_constraint(
        "ck_image_studio_assets_checksum_sha256_format",
        "image_studio_assets",
        "checksum_sha256 IS NULL OR checksum_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_image_studio_assets_ready_checksum_required",
        "image_studio_assets",
        "storage_state <> 'ready' OR checksum_sha256 IS NOT NULL",
    )
    op.create_index(
        "uq_image_studio_assets_project_ready_checksum",
        "image_studio_assets",
        ["project_id", "checksum_sha256"],
        unique=True,
        postgresql_where=sa.text(
            "checksum_sha256 IS NOT NULL AND storage_state = 'ready'"
        ),
    )


def downgrade() -> None:
    _prepare_delivery_keys_for_downgrade()
    op.drop_index(
        "uq_image_studio_assets_project_delivery_key",
        table_name="image_studio_assets",
    )
    op.create_index(
        "uq_image_studio_assets_project_delivery_key",
        "image_studio_assets",
        ["project_id", "delivery_key"],
        unique=True,
        postgresql_where=sa.text("delivery_key IS NOT NULL"),
    )
    op.drop_index(
        "uq_image_studio_assets_project_ready_checksum",
        table_name="image_studio_assets",
    )
    op.drop_constraint(
        "ck_image_studio_assets_ready_checksum_required",
        "image_studio_assets",
        type_="check",
    )
    op.drop_constraint(
        "ck_image_studio_assets_checksum_sha256_format",
        "image_studio_assets",
        type_="check",
    )
    op.drop_index("ix_image_studio_assets_source_type", table_name="image_studio_assets")
    op.drop_constraint(
        "ck_image_studio_assets_source_type",
        "image_studio_assets",
        type_="check",
    )
    op.drop_column("image_studio_assets", "metadata_provenance")
    op.drop_column("image_studio_assets", "source_type")
