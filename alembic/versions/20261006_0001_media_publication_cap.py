"""Add the nullable cumulative EditorialProgram publication ceiling."""
from alembic import op
import sqlalchemy as sa

revision = "20261006_0001"
down_revision = "20261005_0001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("media_editorial_program_revisions") as batch:
        batch.add_column(sa.Column("max_publications", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_media_editorial_program_revisions_publication_cap",
            "max_publications IS NULL OR (max_publications >= 0 AND max_publications <= 10000)",
        )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM media_editorial_program_revisions WHERE max_publications IS NOT NULL")):
        raise RuntimeError("Cannot remove configured publication ceilings; retain the publication-cap schema")
    with op.batch_alter_table("media_editorial_program_revisions") as batch:
        batch.drop_constraint("ck_media_editorial_program_revisions_publication_cap", type_="check")
        batch.drop_column("max_publications")
