"""
Unified system prompts for LLM clients
"""
import logging
from typing import Optional, Dict, List
from ..config import Config

logger = logging.getLogger(__name__)


def _build_skills_section() -> str:
    """スキルカタログセクションを構築（auto/both のスキルのみ）"""
    try:
        from ..skills.registry import get_skill_registry
        registry = get_skill_registry()
        auto_skills = registry.get_auto_skills()

        if not auto_skills:
            return ""

        lines = [
            "利用可能なスキル（ユーザーの意図に合致する場合、invoke_skillツールで呼び出してください）:"
        ]
        for skill in auto_skills:
            aliases_str = ", ".join(skill.aliases[:3]) if skill.aliases else ""
            alias_hint = f" ({aliases_str})" if aliases_str else ""
            lines.append(f"  - {skill.name}{alias_hint}: {skill.description}")

        return "\n".join(lines)
    except Exception:
        return ""


def build_unified_instructions(character_name: str, config: Optional[Config] = None,
                             include_mcp_info: bool = False,
                             available_mcp_servers: Optional[Dict] = None) -> str:
    """統一的なシステムプロンプトを生成

    Args:
        character_name: キャラクター名
        config: アプリケーション設定
        include_mcp_info: MCP情報を含むかどうか
        available_mcp_servers: 利用可能なMCPサーバー情報

    Returns:
        システムプロンプト文字列
    """
    # キャラクター情報を取得
    if config:
        character_config = config.get_character_config(character_name)
        personality = character_config.get('personality', {})
        character_display_name = character_config.get('name', character_name)
        character_intro = f"あなたは{character_display_name}です。{personality.get('details', '')}"
    else:
        character_intro = "あなたは親切なAIアシスタントです。"

    # MCP情報を取得
    mcp_info = ""
    if include_mcp_info and available_mcp_servers:
        clickup_tools = available_mcp_servers.get('clickup', [])
        if clickup_tools:
            tool_names = [t['name'] for t in clickup_tools] if clickup_tools else []
            mcp_info = f"\n\nClickUpタスク管理がMCPサーバー経由で利用可能です({len(clickup_tools)}個のツール: {', '.join(tool_names)})。use_mcp_toolでserver_name='clickup'と適切なtool_nameを指定して呼び出してください。"

    # スキル情報
    skills_section = _build_skills_section()
    skills_block = f"\n\n{skills_section}" if skills_section else ""

    # 統一プロンプト
    instructions = f"""
{character_intro}

最も重要なツールとして、知らない情報はWeb Searchを使って調べてください。

以下は他ツールです。
音楽・曲のリクエストは必ずspotify_assistantツールを使用してください。
YouTube動画の再生リクエストには必ずsearch_and_play_youtubeツールを使用してください。動画タイトルや検索キーワードを指定された場合はsearch_and_play_youtubeを使い、URLが指定された場合のみplay_youtube_audioを使用してください。
ニコニコ動画の再生リクエストには必ずsearch_and_play_niconicoツールを使用してください。動画タイトルや検索キーワードを指定された場合はsearch_and_play_niconicoを使い、URLが指定された場合のみplay_niconico_audioを使用してください。
過去の会話履歴から検索する場合はsearch_memoryを使用してください。
ドキュメントや資料から検索する場合はsearch_ragを使用してください。検索時は具体的なキーワードや製品名を使用し、「私の」などの代名詞は避けてください。
タスク管理やTODOに関する操作はuse_mcp_tool（server_name='clickup'）を使用してください。主なツール: create_task(タスク作成), get_tasks(一覧取得), search_tasks(検索), update_task(更新), get_task_detail(詳細), add_comment(コメント追加)。{mcp_info}{skills_block}

"""
    return instructions.strip()