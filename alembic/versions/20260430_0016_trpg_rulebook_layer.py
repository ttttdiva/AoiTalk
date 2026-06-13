"""Add TRPG ruleset profile and rulebook document layer.

Revision ID: 20260430_0016
Revises: 20260429_0015
Create Date: 2026-04-30
"""

from alembic import op


revision = "20260430_0016"
down_revision = "20260429_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS trpg_ruleset_profiles (
            key VARCHAR(50) PRIMARY KEY,
            display_name VARCHAR(120) NOT NULL,
            edition VARCHAR(50) NOT NULL DEFAULT '',
            system_type VARCHAR(50) NOT NULL DEFAULT 'generic',
            description TEXT NOT NULL DEFAULT '',
            gm_rules_brief TEXT NOT NULL DEFAULT '',
            character_sheet_schema JSON NOT NULL DEFAULT '{}',
            default_pc_state JSON NOT NULL DEFAULT '{}',
            resource_schema JSON NOT NULL DEFAULT '{}',
            dice_rule_schema JSON NOT NULL DEFAULT '{}',
            skill_resolver JSON NOT NULL DEFAULT '{}',
            profile_metadata JSON NOT NULL DEFAULT '{}',
            is_enabled BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS trpg_rulebook_documents (
            id UUID PRIMARY KEY,
            ruleset_key VARCHAR(50) NOT NULL REFERENCES trpg_ruleset_profiles(key) ON DELETE CASCADE,
            title VARCHAR(200) NOT NULL,
            source_label TEXT NOT NULL DEFAULT '',
            source_text TEXT NOT NULL DEFAULT '',
            structure JSON NOT NULL DEFAULT '{}',
            priority INTEGER NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_ruleset_profiles_system_type ON trpg_ruleset_profiles (system_type)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_ruleset_profiles_enabled ON trpg_ruleset_profiles (is_enabled)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_rulebook_documents_ruleset_key ON trpg_rulebook_documents (ruleset_key)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_rulebook_documents_active ON trpg_rulebook_documents (ruleset_key, is_active)"
    )

    bind.exec_driver_sql(
        """
        INSERT INTO trpg_ruleset_profiles
            (key, display_name, edition, system_type, description, gm_rules_brief,
             character_sheet_schema, resource_schema, dice_rule_schema, skill_resolver,
             profile_metadata, is_enabled, created_at, updated_at)
        VALUES
            (
                'generic',
                '汎用TRPG',
                '',
                'generic',
                '専用ルールブック未登録のTRPG向け。数値リソースと手動目標値で進行する。',
                '## 汎用TRPG進行ルール\n- ルールブック本文が登録されている場合はそれを優先する。\n- 未登録の判定はGMが成功条件を短く説明し、必要ならダイス式と目標値をREQUEST_ROLLで提示する。\n- 成否ログが無い判定結果を勝手に断定しない。',
                '{"sheet_format":"generic_pc_v1"}'::json,
                '{"resources":["hp","mp"],"conditions":true,"items":true}'::json,
                '{"default_expression":"2d6","success":"manual_or_lower_equal_target"}'::json,
                '{"mode":"generic_map","sections":["skills","stats"]}'::json,
                '{}'::json,
                true,
                now(),
                now()
            ),
            (
                'coc6',
                'クトゥルフ神話TRPG 6版',
                '6版',
                'coc',
                'CoC 6版/クラシック用。詳細な本文はユーザー投入のルールブック資料で補完する。',
                '## クトゥルフ神話TRPG 6版/クラシック向け進行ルール\n- 基本判定は 1d100 の下方判定。出目が技能値以下なら成功。\n- 判定は必要な場面だけ要求し、失敗しても物語が止まらないよう、時間経過、危険の接近、追加代償で進める。\n- クリティカル、ファンブル、スペシャル等の細部は卓の裁定として扱い、迷ったら簡潔に理由を示す。\n- SANチェックは「成功時損失/失敗時損失」を明示し、必要なら 0/1D3 のように追加ダイスを求める。\n- HP、MP、正気度、所持品、状態異常は参加者 pc_state を正本として扱う。\n- シナリオ原文が取り込まれている場合は、それを正本として扱い、部屋構成、NPC、真相、エンディングを勝手に改変しない。',
                '{"sheet_format":"coc_investigator_v1"}'::json,
                '{"resources":["hp","mp","sanity"],"conditions":true,"items":true}'::json,
                '{"default_expression":"1d100","success":"lower_equal_target","difficulty":["regular"]}'::json,
                '{"mode":"coc_sheet","sections":["skills","stats"]}'::json,
                '{}'::json,
                true,
                now(),
                now()
            ),
            (
                'coc7',
                'クトゥルフ神話TRPG 7版',
                '7版',
                'coc',
                'CoC 7版用。成功段階と難易度つき1d100判定を扱う。',
                '## クトゥルフ神話TRPG 7版向け進行ルール\n- 基本判定は 1d100 の下方判定。出目が技能値以下なら成功。\n- 成功段階は、通常成功、技能値の半分以下の困難成功、5分の1以下の極限成功として扱う。\n- 1 は決定的成功。ファンブルは技能値50未満なら96以上、50以上なら100を目安にする。\n- 判定は失敗して物語が止まる場面で乱発せず、失敗時は情報の遅延、代償、危険の接近で進行を維持する。\n- 失敗後の「プッシュ」は可能だが、再失敗時の具体的な悪化を先に示す。\n- SANチェックは「成功時損失/失敗時損失」を明示し、必要なら 0/1D3 のように追加ダイスを求める。\n- HP、MP、正気度、幸運、所持品、状態異常は参加者 pc_state を正本として扱う。\n- ルールブック本文や既存シナリオ本文を引用せず、この卓の現在状況に合わせて短く裁定する。',
                '{"sheet_format":"coc_investigator_v1"}'::json,
                '{"resources":["hp","mp","sanity","luck"],"conditions":true,"items":true}'::json,
                '{"default_expression":"1d100","success":"coc7_success_levels","difficulty":["regular","hard","extreme"]}'::json,
                '{"mode":"coc_sheet","sections":["skills","stats"]}'::json,
                '{}'::json,
                true,
                now(),
                now()
            ),
            (
                'shinobigami',
                'シノビガミ',
                '',
                'generic',
                'ルールブック本文投入待ち。現時点では汎用判定として扱う。',
                '',
                '{"sheet_format":"generic_pc_v1"}'::json,
                '{"resources":["hp"],"conditions":true,"items":true}'::json,
                '{"default_expression":"2d6","success":"manual_or_lower_equal_target"}'::json,
                '{"mode":"generic_map","sections":["skills","stats"]}'::json,
                '{"needs_rulebook_text":true}'::json,
                true,
                now(),
                now()
            ),
            (
                'swordworld2_5',
                'ソード・ワールド2.5',
                '2.5',
                'generic',
                'ルールブック本文投入待ち。現時点では汎用判定として扱う。',
                '',
                '{"sheet_format":"generic_pc_v1"}'::json,
                '{"resources":["hp","mp"],"conditions":true,"items":true}'::json,
                '{"default_expression":"2d6","success":"manual_or_lower_equal_target"}'::json,
                '{"mode":"generic_map","sections":["skills","stats"]}'::json,
                '{"needs_rulebook_text":true}'::json,
                true,
                now(),
                now()
            )
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_trpg_rulebook_documents_active", table_name="trpg_rulebook_documents")
    op.drop_index("ix_trpg_rulebook_documents_ruleset_key", table_name="trpg_rulebook_documents")
    op.drop_table("trpg_rulebook_documents")
    op.drop_index("ix_trpg_ruleset_profiles_enabled", table_name="trpg_ruleset_profiles")
    op.drop_index("ix_trpg_ruleset_profiles_system_type", table_name="trpg_ruleset_profiles")
    op.drop_table("trpg_ruleset_profiles")
