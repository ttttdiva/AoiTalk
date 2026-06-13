"""Replace Qwen3-TTS character voice engine with Irodori-TTS.

Revision ID: 20260502_0023
Revises: 20260502_0022
Create Date: 2026-05-02
"""

from __future__ import annotations

from alembic import op

revision = "20260502_0023"
down_revision = "20260502_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE characters
        SET
            voice_engine = 'irodori_tts',
            voice_parameters = (
                COALESCE(voice_parameters::jsonb, '{}'::jsonb)
                - 'top_k'
                - 'top_p'
                - 'temperature'
                - 'repetition_penalty'
                || jsonb_build_object('migrated_from_qwen3tts', true)
            )::json
        WHERE voice_engine = 'qwen3tts'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE characters
        SET
            voice_engine = 'qwen3tts',
            voice_parameters = (
                COALESCE(voice_parameters::jsonb, '{}'::jsonb)
                - 'migrated_from_qwen3tts'
            )::json
        WHERE
            voice_engine = 'irodori_tts'
            AND COALESCE(voice_parameters::jsonb, '{}'::jsonb)
                ? 'migrated_from_qwen3tts'
        """
    )
