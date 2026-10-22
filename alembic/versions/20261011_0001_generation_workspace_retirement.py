"""生成接続を無効化しても計画・Run・出力の参照を保持する。"""

from alembic import op
import sqlalchemy as sa

revision = "20261011_0001"
down_revision = "20261010_0002"
branch_labels = None
depends_on = None


def _indexes(active_only):
    for suffix, columns, scope in [
        ("personal", ["owner_user_id", "external_workspace_id"], "project_id IS NULL"),
        ("project", ["project_id", "external_workspace_id"], "project_id IS NOT NULL"),
    ]:
        name = f"uq_media_generation_workspaces_{suffix}_identity"
        op.drop_index(name, table_name="media_generation_workspaces")
        predicate = sa.text(scope + (" AND status <> 'unavailable'" if active_only else ""))
        op.create_index(name, "media_generation_workspaces", columns, unique=True,
                        postgresql_where=predicate, sqlite_where=predicate)


def upgrade():
    _indexes(True)


def downgrade():
    # Refuse a narrowing downgrade that would require deleting saved history.
    connection = op.get_bind()
    for columns, scope in [("owner_user_id, external_workspace_id", "project_id IS NULL"),
                           ("project_id, external_workspace_id", "project_id IS NOT NULL")]:
        if connection.execute(sa.text(f"SELECT 1 FROM media_generation_workspaces WHERE {scope} GROUP BY {columns} HAVING count(*) > 1 LIMIT 1")).first():
            raise RuntimeError("生成接続の保存履歴があるため旧一意制約へ戻せません。履歴を削除せず接続を確認してください。")
    _indexes(False)
