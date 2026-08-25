"""App 系テーブルの越境参照と Grant 重複を DB レベルで禁止する。

背景:
    AppTarget / AppRelease への参照はこれまで単独 FK（``target_id`` や
    ``release_id`` だけ）で、参照先が同じ App に属するかは API 検証にしか
    依存していなかった。そのため直接 SQL や将来の別クライアントからは
    「別 App の Target を指す AppJob」「別 App の Release を install 済みに
    した ProjectApp」のような越境参照を作れてしまう。

このMigrationで追加するもの:
    1. app_targets / app_releases に (app_id, id) の複合一意キーを追加。
       複合 FK の参照先として使う。
    2. 参照側を単独 FK から複合 FK へ置き換える。
       - project_apps (app_id, installed_release_id) -> app_releases
       - app_jobs (app_id, target_id) -> app_targets
       - app_jobs (app_id, release_id) -> app_releases
       - task_app_links (app_id, target_id) -> app_targets
       - app_artifacts (app_id, release_id) -> app_releases
       - app_artifacts (app_id, target_id) -> app_targets
       ON DELETE は既存挙動を変えないよう、PostgreSQL 15 以降の列指定付き
       ``SET NULL (列名)`` を使って app_id を NULL 化しない。
    3. app_artifacts に非正規化列 app_id を追加。省略時は BEFORE INSERT
       トリガーが release_id から補完するので、既存の登録コードは変更不要。
    4. app_grants の一意キーを permission 込みから主体単位へ強化する。
       Grant API は (app_id, user_id, project_id) で既存行を引いて permission を
       上書きする実装なので、permission を含む従来の一意キーでは同じ主体に
       別 permission の行を並行 INSERT できてしまう。

既存データの扱い:
    unique / FK を追加する前に違反行を数え、1 件でもあれば RuntimeError で
    停止する。データを黙って削除・NULL 化することはしない。停止した場合は
    エラーメッセージの SQL で対象行を確認し、業務判断で解消してから再実行する。

同一 App 内の一意性（AppTarget.target_key / AppRelease.version /
ProjectApp の binding）は 20260801_0001 で作成済みのため追加しない。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260803_0002"
down_revision = "20260803_0001"
branch_labels = None
depends_on = None


def _fail_if_rows(bind, *, count_sql: str, label: str, detail_sql: str) -> None:
    """違反行があれば、消さずに RuntimeError で停止する。"""

    count = bind.execute(sa.text(count_sql)).scalar() or 0
    if count:
        raise RuntimeError(
            f"{label}: {count} 件の違反行があるため制約を追加できません。"
            f" 次のSQLで対象を確認し、解消してから再実行してください: {detail_sql}"
        )


def _preflight(bind) -> None:
    _fail_if_rows(
        bind,
        label="app_grants に同一 User への重複 Grant があります",
        count_sql=(
            "SELECT count(*) FROM ("
            " SELECT app_id, user_id FROM app_grants WHERE user_id IS NOT NULL"
            " GROUP BY app_id, user_id HAVING count(*) > 1) AS duplicated"
        ),
        detail_sql=(
            "SELECT app_id, user_id, count(*) FROM app_grants WHERE user_id IS NOT NULL"
            " GROUP BY app_id, user_id HAVING count(*) > 1"
        ),
    )
    _fail_if_rows(
        bind,
        label="app_grants に同一 Project への重複 Grant があります",
        count_sql=(
            "SELECT count(*) FROM ("
            " SELECT app_id, project_id FROM app_grants WHERE project_id IS NOT NULL"
            " GROUP BY app_id, project_id HAVING count(*) > 1) AS duplicated"
        ),
        detail_sql=(
            "SELECT app_id, project_id, count(*) FROM app_grants WHERE project_id IS NOT NULL"
            " GROUP BY app_id, project_id HAVING count(*) > 1"
        ),
    )
    _fail_if_rows(
        bind,
        label="project_apps が別 App の Release を install 済みにしています",
        count_sql=(
            "SELECT count(*) FROM project_apps AS binding"
            " JOIN app_releases AS release ON release.id = binding.installed_release_id"
            " WHERE release.app_id <> binding.app_id"
        ),
        detail_sql=(
            "SELECT binding.project_id, binding.app_id, binding.installed_release_id"
            " FROM project_apps AS binding"
            " JOIN app_releases AS release ON release.id = binding.installed_release_id"
            " WHERE release.app_id <> binding.app_id"
        ),
    )
    _fail_if_rows(
        bind,
        label="app_jobs が別 App の Target を参照しています",
        count_sql=(
            "SELECT count(*) FROM app_jobs AS job"
            " JOIN app_targets AS target ON target.id = job.target_id"
            " WHERE target.app_id <> job.app_id"
        ),
        detail_sql=(
            "SELECT job.id, job.app_id, job.target_id FROM app_jobs AS job"
            " JOIN app_targets AS target ON target.id = job.target_id"
            " WHERE target.app_id <> job.app_id"
        ),
    )
    _fail_if_rows(
        bind,
        label="app_jobs が別 App の Release を参照しています",
        count_sql=(
            "SELECT count(*) FROM app_jobs AS job"
            " JOIN app_releases AS release ON release.id = job.release_id"
            " WHERE release.app_id <> job.app_id"
        ),
        detail_sql=(
            "SELECT job.id, job.app_id, job.release_id FROM app_jobs AS job"
            " JOIN app_releases AS release ON release.id = job.release_id"
            " WHERE release.app_id <> job.app_id"
        ),
    )
    _fail_if_rows(
        bind,
        label="task_app_links が別 App の Target を参照しています",
        count_sql=(
            "SELECT count(*) FROM task_app_links AS link"
            " JOIN app_targets AS target ON target.id = link.target_id"
            " WHERE target.app_id <> link.app_id"
        ),
        detail_sql=(
            "SELECT link.id, link.app_id, link.target_id FROM task_app_links AS link"
            " JOIN app_targets AS target ON target.id = link.target_id"
            " WHERE target.app_id <> link.app_id"
        ),
    )
    _fail_if_rows(
        bind,
        label="app_artifacts の Release と Target が別 App です",
        count_sql=(
            "SELECT count(*) FROM app_artifacts AS artifact"
            " JOIN app_releases AS release ON release.id = artifact.release_id"
            " JOIN app_targets AS target ON target.id = artifact.target_id"
            " WHERE release.app_id <> target.app_id"
        ),
        detail_sql=(
            "SELECT artifact.id, release.app_id AS release_app_id, target.app_id AS target_app_id"
            " FROM app_artifacts AS artifact"
            " JOIN app_releases AS release ON release.id = artifact.release_id"
            " JOIN app_targets AS target ON target.id = artifact.target_id"
            " WHERE release.app_id <> target.app_id"
        ),
    )


def upgrade() -> None:
    bind = op.get_bind()
    _preflight(bind)

    # 1. 複合 FK の参照先となる一意キー。id が主キーなので重複は発生し得ない。
    op.create_unique_constraint(
        "uq_app_targets_app_id_id", "app_targets", ["app_id", "id"]
    )
    op.create_unique_constraint(
        "uq_app_releases_app_id_id", "app_releases", ["app_id", "id"]
    )

    # 2. app_artifacts に App を持たせ、Release と Target の App 一致を保証する。
    op.add_column(
        "app_artifacts",
        sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE app_artifacts AS artifact
            SET app_id = release.app_id
            FROM app_releases AS release
            WHERE release.id = artifact.release_id
              AND artifact.app_id IS DISTINCT FROM release.app_id
            """
        )
    )
    _fail_if_rows(
        bind,
        label="app_artifacts の app_id を Release から補完できませんでした",
        count_sql="SELECT count(*) FROM app_artifacts WHERE app_id IS NULL",
        detail_sql="SELECT id, release_id, target_id FROM app_artifacts WHERE app_id IS NULL",
    )
    op.alter_column(
        "app_artifacts",
        "app_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    # 既存の登録コードは app_id を指定しないため、release_id から補完する。
    # 明示指定された値が Release / Target と食い違う場合は複合 FK が弾く。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_artifacts_set_app_id() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.app_id IS NULL THEN
                SELECT app_id INTO NEW.app_id FROM app_releases WHERE id = NEW.release_id;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    # Temporary PostgreSQL schemas do not participate in unqualified routine
    # lookup even though the function is created in the same ``pg_temp_*``
    # namespace as the table.  Resolve the table namespace explicitly so the
    # trigger works both for a normal schema and for disposable pg_temp
    # migration clones.
    op.execute(
        """
        DO $$
        DECLARE
            table_schema text;
        BEGIN
            SELECT namespace.nspname
              INTO table_schema
              FROM pg_class AS relation
              JOIN pg_namespace AS namespace
                ON namespace.oid = relation.relnamespace
             WHERE relation.oid = 'app_artifacts'::regclass;

            EXECUTE format(
                'CREATE TRIGGER trg_app_artifacts_set_app_id '
                'BEFORE INSERT ON %I.app_artifacts '
                'FOR EACH ROW EXECUTE FUNCTION %I.app_artifacts_set_app_id()',
                table_schema,
                table_schema
            );
        END;
        $$
        """
    )
    op.execute('ALTER TABLE app_artifacts DROP CONSTRAINT IF EXISTS "app_artifacts_release_id_fkey"')
    op.execute('ALTER TABLE app_artifacts DROP CONSTRAINT IF EXISTS "app_artifacts_target_id_fkey"')
    op.execute(
        """
        ALTER TABLE app_artifacts
        ADD CONSTRAINT fk_app_artifacts_release_app
        FOREIGN KEY (app_id, release_id) REFERENCES app_releases (app_id, id)
        ON DELETE CASCADE
        """
    )
    op.execute(
        """
        ALTER TABLE app_artifacts
        ADD CONSTRAINT fk_app_artifacts_target_app
        FOREIGN KEY (app_id, target_id) REFERENCES app_targets (app_id, id)
        ON DELETE RESTRICT
        """
    )

    # 3. 残りの参照側を複合 FK へ置き換える。
    #    ON DELETE SET NULL は列指定付き（PostgreSQL 15+）にして app_id を守る。
    op.execute(
        'ALTER TABLE project_apps DROP CONSTRAINT IF EXISTS "project_apps_installed_release_id_fkey"'
    )
    op.execute(
        """
        ALTER TABLE project_apps
        ADD CONSTRAINT fk_project_apps_installed_release_app
        FOREIGN KEY (app_id, installed_release_id) REFERENCES app_releases (app_id, id)
        ON DELETE SET NULL (installed_release_id)
        """
    )
    op.execute('ALTER TABLE app_jobs DROP CONSTRAINT IF EXISTS "app_jobs_target_id_fkey"')
    op.execute(
        """
        ALTER TABLE app_jobs
        ADD CONSTRAINT fk_app_jobs_target_app
        FOREIGN KEY (app_id, target_id) REFERENCES app_targets (app_id, id)
        ON DELETE SET NULL (target_id)
        """
    )
    op.execute('ALTER TABLE app_jobs DROP CONSTRAINT IF EXISTS "app_jobs_release_id_fkey"')
    op.execute(
        """
        ALTER TABLE app_jobs
        ADD CONSTRAINT fk_app_jobs_release_app
        FOREIGN KEY (app_id, release_id) REFERENCES app_releases (app_id, id)
        ON DELETE SET NULL (release_id)
        """
    )
    op.execute('ALTER TABLE task_app_links DROP CONSTRAINT IF EXISTS "task_app_links_target_id_fkey"')
    op.execute(
        """
        ALTER TABLE task_app_links
        ADD CONSTRAINT fk_task_app_links_target_app
        FOREIGN KEY (app_id, target_id) REFERENCES app_targets (app_id, id)
        ON DELETE SET NULL (target_id)
        """
    )

    # 4. Grant は 1 App × 1 主体につき 1 行にする。UNIQUE は NULL を相異なる値と
    #    して扱うため、主体側が NOT NULL の行だけを対象にした部分一意にする。
    op.execute('ALTER TABLE app_grants DROP CONSTRAINT IF EXISTS "uq_app_grants_user_permission"')
    op.execute('ALTER TABLE app_grants DROP CONSTRAINT IF EXISTS "uq_app_grants_project_permission"')
    op.create_index(
        "uq_app_grants_app_user",
        "app_grants",
        ["app_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
        sqlite_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_app_grants_app_project",
        "app_grants",
        ["app_id", "project_id"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
        sqlite_where=sa.text("project_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_app_grants_app_project", table_name="app_grants")
    op.drop_index("uq_app_grants_app_user", table_name="app_grants")
    op.create_unique_constraint(
        "uq_app_grants_user_permission", "app_grants", ["app_id", "user_id", "permission"]
    )
    op.create_unique_constraint(
        "uq_app_grants_project_permission",
        "app_grants",
        ["app_id", "project_id", "permission"],
    )

    op.execute('ALTER TABLE task_app_links DROP CONSTRAINT IF EXISTS "fk_task_app_links_target_app"')
    op.create_foreign_key(
        "task_app_links_target_id_fkey",
        "task_app_links",
        "app_targets",
        ["target_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute('ALTER TABLE app_jobs DROP CONSTRAINT IF EXISTS "fk_app_jobs_release_app"')
    op.create_foreign_key(
        "app_jobs_release_id_fkey",
        "app_jobs",
        "app_releases",
        ["release_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute('ALTER TABLE app_jobs DROP CONSTRAINT IF EXISTS "fk_app_jobs_target_app"')
    op.create_foreign_key(
        "app_jobs_target_id_fkey",
        "app_jobs",
        "app_targets",
        ["target_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        'ALTER TABLE project_apps DROP CONSTRAINT IF EXISTS "fk_project_apps_installed_release_app"'
    )
    op.create_foreign_key(
        "project_apps_installed_release_id_fkey",
        "project_apps",
        "app_releases",
        ["installed_release_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.execute('ALTER TABLE app_artifacts DROP CONSTRAINT IF EXISTS "fk_app_artifacts_target_app"')
    op.execute('ALTER TABLE app_artifacts DROP CONSTRAINT IF EXISTS "fk_app_artifacts_release_app"')
    op.execute("DROP TRIGGER IF EXISTS trg_app_artifacts_set_app_id ON app_artifacts")
    op.execute("DROP FUNCTION IF EXISTS app_artifacts_set_app_id()")
    op.create_foreign_key(
        "app_artifacts_release_id_fkey",
        "app_artifacts",
        "app_releases",
        ["release_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "app_artifacts_target_id_fkey",
        "app_artifacts",
        "app_targets",
        ["target_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_column("app_artifacts", "app_id")

    op.drop_constraint("uq_app_releases_app_id_id", "app_releases", type_="unique")
    op.drop_constraint("uq_app_targets_app_id_id", "app_targets", type_="unique")
