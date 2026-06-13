"""
スキルシステム - スラッシュコマンドによる明示呼び出し

チャットのユーザーメッセージ先頭が `/skill名 入力` 形式のとき、
対象スキル(manual / both)を展開済みプロンプトへ変換する。
これにより LLM の自動判断(invoke_skill)に依存せず、ユーザーが
スキルを強制的に発火できる。
"""
from typing import Optional

from .models import SkillTriggerMode


def resolve_skill_slash_command(message: str) -> Optional[str]:
    """先頭の `/skill名` を検出し、対象スキルなら展開済みプロンプトを返す。

    Args:
        message: ユーザー入力メッセージ

    Returns:
        マッチしたスキルの展開済みプロンプト。
        スラッシュコマンドでない、または明示呼び出し対象スキルが
        無い場合は None(呼び出し元はメッセージをそのまま扱う)。
    """
    if not message:
        return None

    stripped = message.strip()
    if not stripped.startswith("/"):
        return None

    body = stripped[1:]
    # "/" 単体や "/ foo" のように直後が空白のものはコマンドとみなさない
    if not body or body[0].isspace():
        return None

    # 最初の空白(全角空白含む)でコマンドトークンと入力に分割
    parts = body.split(None, 1)
    token = parts[0]
    input_text = parts[1].strip() if len(parts) > 1 else ""

    from .registry import get_skill_registry

    registry = get_skill_registry()
    skill = registry.get_by_alias(token) or registry.get(token)
    if not skill:
        return None

    # AUTO は LLM 自動判断専用。スラッシュ明示呼び出しの対象外とする。
    if skill.trigger_mode == SkillTriggerMode.AUTO:
        return None

    rendered = skill.render_prompt(input_text)
    return f"[スキル: {skill.name}]\n{rendered}"
