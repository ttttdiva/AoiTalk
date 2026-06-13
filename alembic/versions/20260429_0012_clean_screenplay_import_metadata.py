"""Clean screenplay import metadata from scenario content.

Revision ID: 20260429_0012
Revises: 20260428_0011
Create Date: 2026-04-29 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260429_0012"
down_revision = "20260428_0011"
branch_labels = None
depends_on = None


SCENARIO_IDS = (
    "1b08cbef-1434-5c9c-8bd9-4508d74e0d44",
    "fada1570-6b09-5ff5-a002-7404886b1416",
    "3f2a3aeb-ef89-54c5-94c7-2ca36626f900",
)


def upgrade() -> None:
    ids_sql = "ARRAY[" + ",".join(f"'{item}'::uuid" for item in SCENARIO_IDS) + "]"

    op.execute(
        f"""
        UPDATE scenarios
        SET
            description = CASE id
                WHEN '1b08cbef-1434-5c9c-8bd9-4508d74e0d44'::uuid THEN '現代日本で悪魔の侵入が進む中、無気力と怒りを抱えた琴葉茜が、黒魔術を背負う東北きりたんと出会い、ギルドと悪魔界の脅威に巻き込まれていくダークファンタジー本編。'
                WHEN 'fada1570-6b09-5ff5-a002-7404886b1416'::uuid THEN '秩父盆地を舞台に、鳴花ヒメの種子が生む静域によって生体反応が減速し、交通、通信、医療、避難秩序が崩壊していく災害サバイバル編。'
                WHEN '3f2a3aeb-ef89-54c5-94c7-2ca36626f900'::uuid THEN '琴葉茜、東北きりたん、音街ウナを中心に、依存、罪悪感、保護欲、黒魔術による死者の復活が絡む暗いキャラクター劇を、単発エピソード形式で扱うシリーズ。'
                ELSE description
            END,
            setting = '',
            gm_instructions = '',
            voice_tone = CASE id
                WHEN '3f2a3aeb-ef89-54c5-94c7-2ca36626f900'::uuid
                    THEN '暗い日常劇として、単発エピソードごとの温度感を保ち、人物の口調を設定資料に合わせる。'
                ELSE '日本語のダークファンタジー脚本として、会話は淡々とした地の文と感情のにじむ台詞を組み合わせる。'
            END,
            tags = COALESCE(
                (
                    SELECT json_agg(tag)
                    FROM json_array_elements_text(COALESCE(tags, '[]'::json)) AS tag
                    WHERE tag <> 'legacy-import'
                ),
                '[]'::json
            ),
            updated_at = now()
        WHERE id = ANY({ids_sql})
        """
    )

    op.execute(
        f"""
        DELETE FROM scenario_canon_entries
        WHERE scenario_id = ANY({ids_sql})
          AND (
            category IN ('管理', '移行', 'RAG')
            OR fact LIKE '%D:\\Screenplay%'
            OR fact LIKE '%旧D:%'
            OR fact LIKE '%旧F01%'
            OR fact LIKE '%旧F02%'
            OR fact LIKE '%旧資料%'
            OR fact LIKE '%旧outputs%'
            OR fact LIKE '%移行%'
            OR fact LIKE '%AoiTalkのシナリオDB%'
            OR fact LIKE '%RAGコレクション%'
          )
        """
    )

    op.execute(
        f"""
        DELETE FROM scenario_scenes
        WHERE scenario_id = ANY({ids_sql})
          AND (
            state_snapshot ->> 'source_path' = 'CLAUDE.md'
            OR state_snapshot ->> 'source_path' = 'AGENTS.md'
            OR state_snapshot ->> 'source_path' LIKE 'memory-bank/%'
          )
        """
    )

    op.execute(
        f"""
        UPDATE scenario_scenes
        SET description = ''
        WHERE scenario_id = ANY({ids_sql})
          AND (
            description IN (
                '参考資料として移行。',
                'キャラクター設定資料として分解移行。',
                '単発エピソード本文として移行。'
            )
            OR description LIKE '%移行%'
            OR description LIKE '%旧D:%'
            OR description LIKE '%D:\\Screenplay%'
            OR description LIKE '%旧資料%'
            OR description LIKE '%旧outputs%'
          )
        """
    )

    op.execute(
        f"""
        UPDATE scenario_scenes
        SET state_snapshot = '{{}}'::json
        WHERE scenario_id = ANY({ids_sql})
          AND (
            state_snapshot::jsonb ? 'migration_source'
            OR state_snapshot::jsonb ? 'managed_by'
            OR state_snapshot::jsonb ? 'source_path'
          )
        """
    )

    op.execute(
        f"""
        UPDATE scenario_episodes
        SET
            synopsis_sentence = 'キャラクター情報と参考資料。',
            synopsis_paragraph = 'キャラクター情報と参考資料。',
            synopsis_full = 'キャラクター情報と参考資料。'
        WHERE scenario_id = '3f2a3aeb-ef89-54c5-94c7-2ca36626f900'::uuid
          AND title = '参考資料'
          AND (
            synopsis_sentence LIKE '%移行%'
            OR synopsis_paragraph LIKE '%移行%'
            OR synopsis_full LIKE '%移行%'
          )
        """
    )


def downgrade() -> None:
    # Removed management prose is intentionally not restored into scenario content.
    pass
