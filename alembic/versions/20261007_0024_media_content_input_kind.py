"""Distinguish original user briefs from attributed research inputs."""

from alembic import op
import sqlalchemy as sa

revision = "20261007_0024"
down_revision = "20261007_0023"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("media_content_items") as batch:
        batch.add_column(sa.Column(
            "input_kind", sa.String(16), nullable=False, server_default="research",
        ))
        batch.create_check_constraint(
            "ck_media_content_items_input_kind",
            "input_kind IN ('research', 'user_authored')",
        )


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM media_content_items WHERE input_kind = 'user_authored' LIMIT 1"
    )).first():
        raise RuntimeError("User-authored brief provenance must be retained")
    with op.batch_alter_table("media_content_items") as batch:
        batch.drop_constraint("ck_media_content_items_input_kind", type_="check")
        batch.drop_column("input_kind")
