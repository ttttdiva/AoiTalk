from src.llm.prompts import build_unified_instructions


def test_unified_instructions_route_non_search_work_to_specialists():
    instructions = build_unified_instructions(character_name="test", config=None)

    assert "project_management_assistant" in instructions
    assert "spotify_assistant" in instructions
    assert "filesystem_assistant" in instructions
    assert "utility_assistant" in instructions
    assert "media_assistant" in instructions
    assert "invoke_skill" in instructions
    assert "skills_assistant" not in instructions


def test_unified_instructions_keep_search_direct_only():
    instructions = build_unified_instructions(character_name="test", config=None)

    assert "検索は `search_assistant` に委譲する" in instructions
    assert "X検索とKnowledge検索は設定で有効な場合だけ使う" in instructions
    assert "search_assistant" in instructions
    assert "knowledge_search" not in instructions
    assert "search_spotify_music" not in instructions
    assert "use_mcp_tool" not in instructions


def test_unified_instructions_marks_memory_search_unavailable_when_disabled():
    class _Config:
        def get(self, key, default=None):
            if key == "memory":
                return {"enabled": True, "enable_search": False}
            return default

        def get_character_config(self, _character_name):
            return {"name": "test", "personality": {}}

    instructions = build_unified_instructions(character_name="test", config=_Config())

    assert "セマンティックメモリ検索は無効" in instructions


def test_unified_instructions_append_custom_instructions():
    instructions = build_unified_instructions(
        character_name="test",
        config=None,
        custom_instructions="Always answer in concise bullet points.",
    )

    assert "ユーザー別の追加指示:" in instructions
    assert "Always answer in concise bullet points." in instructions


def test_unified_instructions_do_not_turn_general_questions_into_project_work():
    instructions = build_unified_instructions(character_name="test", config=None)

    assert "Projectが選択されていても、勝手に案件管理やWBS確認へ変換しない" in instructions
    assert "ユーザーが作業を依頼した場合だけ" in instructions
    assert "対象が既に分かるなら聞き返さない" in instructions
    assert "専門ツールは、ユーザー入力がその作業を明示している場合だけ使う" in instructions


def test_unified_instructions_omit_detailed_skill_catalog():
    instructions = build_unified_instructions(character_name="test", config=None)

    assert "利用可能なスキル" not in instructions
    assert "next_actions" not in instructions
    assert "wbs_sync" not in instructions
    assert "weekly_report" not in instructions
