"""Remove the retired isolated Browser Agent settings from persisted JSON."""

from alembic import op

revision = "20260921_0001"
down_revision = "20260917_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        UPDATE app_config_settings
        SET value = (value::jsonb #- '{browser_agent,allowed_origins}')::json
        WHERE jsonb_typeof(value::jsonb -> 'browser_agent') = 'object'
    """)


def downgrade():
    # Configuration deletion cannot recover a user's former site list.
    pass
