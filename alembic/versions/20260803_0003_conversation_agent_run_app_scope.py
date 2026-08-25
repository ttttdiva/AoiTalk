"""conversation_sessions / agent_runs の App スコープ越境参照を DB レベルで禁止する。

背景:
    20260803_0002 で App 系テーブル（project_apps / app_jobs / task_app_links /
    app_artifacts）の越境参照は複合 FK で塞いだが、参照先の複合一意キー
    ``uq_app_targets_app_id_id`` を利用していない参照側が 2 つ残っていた。

        - conversation_sessions (app_id, app_target_id)
        - agent_runs (app_id, app_target_id)

    どちらも ``app_target_id`` が単独 FK（``app_targets(id)``）だったため、
    「App A のチャットが App B の Target を指す」越境参照を直接 SQL から作れた。
    防波堤は conversation_routes の ``_validate_app_scope`` などの API 検証だけ
    だった。参照先の複合一意キーは既にあるので FK の張り替えだけで塞げる。

このMigrationで追加するもの:
    1. 単独 FK ``fk_*_app_target_id`` を落とし、複合 FK
       ``(app_id, app_target_id) -> app_targets (app_id, id)`` へ置き換える。
       ON DELETE は既存挙動（Target 削除時に app_target_id だけ NULL 化）を
       変えないよう、PostgreSQL 15 以降の列指定付き ``SET NULL (app_target_id)``
       を使い app_id を巻き込まない。
    2. CHECK ``ck_*_app_target_requires_app``。
       複合 FK は MATCH SIMPLE なので、app_id が NULL なら app_target_id が
       どんな値でも検査されない。単独 FK を外した後にこの穴を塞がないと、
       「app_id は NULL なのに app_target_id が実在しない UUID」という行を
       作れてしまう。API 側の「app_target_id には app_id が必要です」
       （conversation_routes._validate_app_scope）と同じ規則を DB にも置く。
       なお MATCH FULL は「app_id だけ指定して Target 未指定」という正常な
       App 開発チャットまで弾いてしまうため使えない。
    3. BEFORE UPDATE トリガー ``trg_*_clear_app_target``。
       apps 行を削除すると (a) app_targets の ON DELETE CASCADE 経由で
       複合 FK が app_target_id を NULL 化し、(b) fk_*_app_id の
       ON DELETE SET NULL が app_id を NULL 化する。この 2 つの RI トリガーの
       発火順は保証されないため、(b) が先に走ると
       「app_id=NULL / app_target_id=非NULL」という中間状態が生まれ、
       即時評価される CHECK に引っかかって App 削除自体が失敗する。
       app_id が NULL になる更新では app_target_id も同時に落とすことで、
       発火順に依存せず App 削除が通るようにする。
       INSERT には掛けないので、不正な INSERT は CHECK が明示的に弾く。

既存データの扱い:
    制約を張る前に違反行を数え、1 件でもあれば RuntimeError で停止する。
    データを黙って削除・NULL 化することはしない。停止した場合はエラー
    メッセージの SQL で対象行を確認し、業務判断で解消してから再実行する。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260803_0003"
down_revision = "20260803_0002"
branch_labels = None
depends_on = None


# (テーブル名, 複合FK名, CHECK名, トリガー名, 旧単独FK名)
_TABLES = (
    (
        "conversation_sessions",
        "fk_conversation_sessions_app_target_app",
        "ck_conversation_sessions_app_target_requires_app",
        "trg_conversation_sessions_clear_app_target",
        "fk_conversation_sessions_app_target_id",
    ),
    (
        "agent_runs",
        "fk_agent_runs_app_target_app",
        "ck_agent_runs_app_target_requires_app",
        "trg_agent_runs_clear_app_target",
        "fk_agent_runs_app_target_id",
    ),
)

_CLEAR_TARGET_FUNCTION = "app_scope_clear_app_target"


def _fail_if_rows(bind, *, count_sql: str, label: str, detail_sql: str) -> None:
    """違反行があれば、消さずに RuntimeError で停止する。"""

    count = bind.execute(sa.text(count_sql)).scalar() or 0
    if count:
        raise RuntimeError(
            f"{label}: {count} 件の違反行があるため制約を追加できません。"
            f" 次のSQLで対象を確認し、解消してから再実行してください: {detail_sql}"
        )


def _preflight(bind) -> None:
    for table, _fk, _ck, _trg, _legacy_fk in _TABLES:
        _fail_if_rows(
            bind,
            label=f"{table} が別 App の Target を参照しています",
            count_sql=(
                f"SELECT count(*) FROM {table} AS scoped"
                " JOIN app_targets AS target ON target.id = scoped.app_target_id"
                " WHERE target.app_id IS DISTINCT FROM scoped.app_id"
            ),
            detail_sql=(
                f"SELECT scoped.id, scoped.app_id, scoped.app_target_id, target.app_id AS target_app_id"
                f" FROM {table} AS scoped"
                " JOIN app_targets AS target ON target.id = scoped.app_target_id"
                " WHERE target.app_id IS DISTINCT FROM scoped.app_id"
            ),
        )
        _fail_if_rows(
            bind,
            label=f"{table} に app_id が NULL のまま Target だけ指す行があります",
            count_sql=(
                f"SELECT count(*) FROM {table}"
                " WHERE app_target_id IS NOT NULL AND app_id IS NULL"
            ),
            detail_sql=(
                f"SELECT id, app_id, app_target_id FROM {table}"
                " WHERE app_target_id IS NOT NULL AND app_id IS NULL"
            ),
        )


def upgrade() -> None:
    bind = op.get_bind()
    _preflight(bind)

    # App 削除時の RI 発火順に依存せず CHECK を満たせるようにする補正トリガー。
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_CLEAR_TARGET_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.app_target_id := NULL;
            RETURN NEW;
        END;
        $$
        """
    )

    for table, fk_name, check_name, trigger_name, legacy_fk in _TABLES:
        # 単独 FK -> 複合 FK。Target 削除時の挙動（app_target_id だけ NULL 化）は変えない。
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{legacy_fk}"')
        op.execute(
            f"""
            ALTER TABLE {table}
            ADD CONSTRAINT {fk_name}
            FOREIGN KEY (app_id, app_target_id) REFERENCES app_targets (app_id, id)
            ON DELETE SET NULL (app_target_id)
            """
        )
        # PostgreSQL's ``pg_temp`` alias is used for temporary relations but
        # is not searched for unqualified routines.  Resolve the table's
        # namespace before attaching the trigger so fresh disposable schemas
        # behave exactly like a normal schema.
        op.execute(
            f"""
            DO $$
            DECLARE
                table_schema text;
            BEGIN
                SELECT namespace.nspname
                  INTO table_schema
                  FROM pg_class AS relation
                  JOIN pg_namespace AS namespace
                    ON namespace.oid = relation.relnamespace
                 WHERE relation.oid = '{table}'::regclass;

                EXECUTE format(
                    'CREATE TRIGGER {trigger_name} '
                    'BEFORE UPDATE ON %I.{table} '
                    'FOR EACH ROW '
                    'WHEN (NEW.app_id IS NULL AND NEW.app_target_id IS NOT NULL) '
                    'EXECUTE FUNCTION %I.{_CLEAR_TARGET_FUNCTION}()',
                    table_schema,
                    table_schema
                );
            END;
            $$
            """
        )
        # 複合 FK は MATCH SIMPLE のため app_id が NULL だと検査されない。
        # その穴（実在しない Target を指す行）を CHECK で塞ぐ。
        op.execute(
            f"""
            ALTER TABLE {table}
            ADD CONSTRAINT {check_name}
            CHECK (app_target_id IS NULL OR app_id IS NOT NULL)
            """
        )


def downgrade() -> None:
    for table, fk_name, check_name, trigger_name, legacy_fk in _TABLES:
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{check_name}"')
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table}")
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{fk_name}"')
        op.create_foreign_key(
            legacy_fk,
            table,
            "app_targets",
            ["app_target_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.execute(f"DROP FUNCTION IF EXISTS {_CLEAR_TARGET_FUNCTION}()")
