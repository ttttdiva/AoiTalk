"""Add FANZA and TikTok to the closed MediaOps platform contract.

Existing revisions remain immutable and both new platform flags default false.
Downgrade refuses to discard records that use the new platforms.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261005_0001"
down_revision = "20261004_0001"
branch_labels = None
depends_on = None

REVISION_TABLES = ("media_persona_revisions", "media_research_routine_revisions")
DRAFT_TABLE = "media_persona_bulk_draft_slots"
CHECKS = [('media_persona_resources', 'ck_media_persona_resources_platform', "platform IS NULL OR platform IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')", "platform IS NULL OR platform IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram', 'fanza', 'tiktok')"), ('media_content_variants', 'ck_media_content_variants_platform', "platform IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')", "platform IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram', 'fanza', 'tiktok')"), ('media_content_variant_revisions', 'ck_media_content_variant_revisions_platform', "platform IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')", "platform IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram', 'fanza', 'tiktok')"), ('media_metric_ingestion_runs', 'ck_media_metric_ingestion_runs_provider', "provider IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')", "provider IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram', 'fanza', 'tiktok')"), ('media_persona_bulk_draft_slots', 'ck_media_persona_bulk_draft_platforms_fact', "(platforms_state = 'unknown' AND platform_x IS NULL AND platform_pixiv IS NULL AND platform_dlsite IS NULL AND platform_patreon IS NULL AND platform_youtube IS NULL AND platform_instagram IS NULL AND platforms_evidence IS NULL) OR (platforms_state = 'explicit' AND platform_x IS NOT NULL AND platform_pixiv IS NOT NULL AND platform_dlsite IS NOT NULL AND platform_patreon IS NOT NULL AND platform_youtube IS NOT NULL AND platform_instagram IS NOT NULL) OR (platforms_state = 'inferred' AND platform_x IS NOT NULL AND platform_pixiv IS NOT NULL AND platform_dlsite IS NOT NULL AND platform_patreon IS NOT NULL AND platform_youtube IS NOT NULL AND platform_instagram IS NOT NULL AND platforms_evidence IS NOT NULL)", "(platforms_state = 'unknown' AND platform_x IS NULL AND platform_pixiv IS NULL AND platform_dlsite IS NULL AND platform_patreon IS NULL AND platform_youtube IS NULL AND platform_instagram IS NULL AND platform_fanza IS NULL AND platform_tiktok IS NULL AND platforms_evidence IS NULL) OR (platforms_state = 'explicit' AND platform_x IS NOT NULL AND platform_pixiv IS NOT NULL AND platform_dlsite IS NOT NULL AND platform_patreon IS NOT NULL AND platform_youtube IS NOT NULL AND platform_instagram IS NOT NULL AND platform_fanza IS NOT NULL AND platform_tiktok IS NOT NULL) OR (platforms_state = 'inferred' AND platform_x IS NOT NULL AND platform_pixiv IS NOT NULL AND platform_dlsite IS NOT NULL AND platform_patreon IS NOT NULL AND platform_youtube IS NOT NULL AND platform_instagram IS NOT NULL AND platform_fanza IS NOT NULL AND platform_tiktok IS NOT NULL AND platforms_evidence IS NOT NULL)"), ('media_platform_accounts', 'ck_media_platform_accounts_platform', "platform IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')", "platform IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram', 'fanza', 'tiktok')")]
CHECKS.append(('media_provider_capability_snapshots', 'ck_media_provider_capability_snapshots_provider', "provider IN ('x', 'pixiv', 'patreon', 'youtube', 'instagram', 'dlsite')", "provider IN ('x', 'pixiv', 'patreon', 'youtube', 'instagram', 'dlsite', 'fanza', 'tiktok')"))


def upgrade():
    for table in REVISION_TABLES:
        for platform in ("fanza", "tiktok"):
            op.add_column(table, sa.Column("platform_" + platform, sa.Boolean(), nullable=False, server_default=sa.false()))
    for platform in ("fanza", "tiktok"):
        op.add_column(DRAFT_TABLE, sa.Column("platform_" + platform, sa.Boolean(), nullable=True))
    # Known draft platform facts must remain complete; unknown stays NULL.
    op.execute(sa.text("UPDATE media_persona_bulk_draft_slots SET platform_fanza = false, platform_tiktok = false WHERE platforms_state <> 'unknown'"))
    for table, name, _old, new in CHECKS:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="check")
            batch.create_check_constraint(name, new)


def downgrade():
    connection = op.get_bind()
    for table in (*REVISION_TABLES, DRAFT_TABLE):
        used = connection.execute(sa.text(f"SELECT 1 FROM {table} WHERE platform_fanza = true OR platform_tiktok = true LIMIT 1")).first()
        if used:
            raise RuntimeError("Cannot downgrade: FANZA/TikTok platform facts exist")
    for table, _name, old, _new in CHECKS:
        if table == DRAFT_TABLE:
            continue
        column = "provider" if table in {"media_metric_ingestion_runs", "media_provider_capability_snapshots"} else "platform"
        used = connection.execute(sa.text(f"SELECT 1 FROM {table} WHERE {column} IN ('fanza', 'tiktok') LIMIT 1")).first()
        if used:
            raise RuntimeError("Cannot downgrade: FANZA/TikTok records exist")
    for table, name, old, _new in reversed(CHECKS):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="check")
            batch.create_check_constraint(name, old)
    for table in (DRAFT_TABLE, *REVISION_TABLES):
        for platform in ("tiktok", "fanza"):
            op.drop_column(table, "platform_" + platform)
