"""
スキルシステム - LLM呼び出し用ツール

LLMが自動的にスキルを呼び出すための invoke_skill ツールを提供する。
ToolRegistry に登録され、function calling 経由で使用される。
"""
from ..tools.core import tool


@tool
def invoke_skill(skill_name: str, input_text: str) -> str:
    """スキルを呼び出してプロンプトテンプレートを展開する

    Args:
        skill_name: スキル名またはエイリアス（例: translate, 翻訳）
        input_text: スキルに渡すテキスト入力

    Returns:
        展開されたスキルプロンプト
    """
    from .registry import get_skill_registry
    from .loader import load_project_skills
    from ..services.project_context import get_runtime_project_context

    registry = get_skill_registry()
    context = get_runtime_project_context() or {}
    project_id = str(context.get("id") or "") or None
    if project_id:
        load_project_skills(project_id)
    skill = registry.get_by_alias(skill_name, project_id) or registry.get(skill_name, project_id)

    if not skill:
        available = ", ".join(registry.get_names(project_id))
        return f"スキル '{skill_name}' が見つかりません。利用可能なスキル: {available}"

    rendered = skill.render_prompt(input_text)
    return f"[スキル: {skill.name}]\n{rendered}"
