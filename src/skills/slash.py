"""
スキルシステム - スラッシュコマンドによる明示呼び出し

チャットのユーザーメッセージ先頭が `/skill名 入力` 形式のとき、
対象スキル(manual / both)を展開済みプロンプトへ変換する。
"""

from dataclasses import dataclass
from typing import Any, Optional

from .models import SkillDefinition, SkillTriggerMode


@dataclass(frozen=True)
class SkillSlashResolution:
    """One slash invocation bound to the exact canonical snapshot rendered."""

    rendered: str
    skill: SkillDefinition
    input_text: str
    rendered_snapshot: Any | None = None


def resolve_skill_slash_invocation(
    message: str,
    *,
    project_id: Optional[str] = None,
) -> Optional[SkillSlashResolution]:
    if not message:
        return None
    stripped = message.strip()
    if not stripped.startswith("/"):
        return None
    body = stripped[1:]
    if not body or body[0].isspace():
        return None
    parts = body.split(None, 1)
    token = parts[0]
    input_text = parts[1].strip() if len(parts) > 1 else ""

    # ``/masking`` is a server-owned literal operation.  It is represented by
    # a canonical Skill YAML for discovery/receipt identity, but it must never
    # be rendered into a prompt by the generic Skill executor.  The request
    # boundary dispatches this token to the local one-way masking service;
    # returning ``None`` here is a defense for older/direct callers that reach
    # the generic resolver without that boundary.
    if token.casefold() == "masking":
        return None

    from .registry import get_skill_registry
    from .loader import load_project_skills
    from .executor import render_skill_snapshot, skill_from_snapshot
    from ..services.skill_learning_service import (
        SkillLearningError,
        SkillLearningService,
    )

    registry = get_skill_registry()
    authorized_project_id = str(project_id or "").strip() or None
    if authorized_project_id:
        load_project_skills(authorized_project_id)
    skill = (
        registry.get_by_alias(token, authorized_project_id)
        or registry.get(token, authorized_project_id)
    )
    if not skill:
        return None

    # Registry state is discovery-only.  A registry object may be older than
    # the canonical file after an atomic writer or crash recovery.  Freeze the
    # canonical target exactly once, then resolve trigger/alias/rendering from
    # that snapshot only.
    try:
        snapshot = SkillLearningService().prepare_usage_snapshot(
            skill,
            project_id=authorized_project_id,
        )
        canonical_skill = skill_from_snapshot(snapshot)
    except (SkillLearningError, OSError, UnicodeError, ValueError, TypeError):
        # Missing targets, invalid/pending transition journals, parse failures,
        # malformed UTF-8/canonical payloads, and other storage failures must
        # never fall back to stale registry prompt text.
        return None

    token_key = token.casefold()
    canonical_tokens = {
        canonical_skill.name.casefold(),
        *(
            str(alias).casefold()
            for alias in canonical_skill.aliases
            if str(alias).strip()
        ),
    }
    if token_key not in canonical_tokens:
        # An alias removed by the canonical version must not remain executable
        # merely because an older registry object still advertises it.
        return None
    if canonical_skill.trigger_mode == SkillTriggerMode.AUTO:
        return None

    try:
        rendered = render_skill_snapshot(
            snapshot,
            input_text,
            include_header=True,
        )
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        # A canonical Skill can parse successfully while still containing an
        # invalid Python format template. Slash is fail-closed: do not return
        # stale registry content and do not allow a receipt for a prompt that
        # was never successfully rendered from this exact snapshot.
        return None

    return SkillSlashResolution(
        rendered=rendered,
        skill=canonical_skill,
        input_text=input_text,
        rendered_snapshot=snapshot,
    )


def resolve_skill_slash_command(
    message: str,
    *,
    project_id: Optional[str] = None,
) -> Optional[str]:
    """Backward-compatible prompt-only resolver."""
    resolved = resolve_skill_slash_invocation(message, project_id=project_id)
    return resolved.rendered if resolved is not None else None
