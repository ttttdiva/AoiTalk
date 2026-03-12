"""SkillDefinition モデルのテスト"""
import pytest
from src.skills.models import SkillDefinition, SkillTriggerMode


class TestSkillTriggerMode:
    def test_values(self):
        assert SkillTriggerMode.MANUAL.value == "manual"
        assert SkillTriggerMode.AUTO.value == "auto"
        assert SkillTriggerMode.BOTH.value == "both"

    def test_from_string(self):
        assert SkillTriggerMode("manual") == SkillTriggerMode.MANUAL
        assert SkillTriggerMode("both") == SkillTriggerMode.BOTH


class TestSkillDefinition:
    def test_basic_creation(self):
        skill = SkillDefinition(
            name="test",
            description="テスト",
            prompt_template="入力: {input}",
        )
        assert skill.name == "test"
        assert skill.trigger_mode == SkillTriggerMode.BOTH
        assert skill.aliases == []
        assert skill.parameters == {}

    def test_render_prompt_simple(self):
        skill = SkillDefinition(
            name="test",
            description="テスト",
            prompt_template="翻訳: {input}",
        )
        result = skill.render_prompt("こんにちは")
        assert result == "翻訳: こんにちは"

    def test_render_prompt_with_parameters(self):
        skill = SkillDefinition(
            name="translate",
            description="翻訳",
            prompt_template="{target_lang}に翻訳: {input}",
            parameters={"target_lang": {"description": "言語", "default": "英語"}},
        )
        # デフォルト値使用
        result = skill.render_prompt("こんにちは")
        assert result == "英語に翻訳: こんにちは"

        # オーバーライド
        result = skill.render_prompt("こんにちは", target_lang="フランス語")
        assert result == "フランス語に翻訳: こんにちは"

    def test_render_prompt_missing_key_fallback(self):
        skill = SkillDefinition(
            name="test",
            description="テスト",
            prompt_template="{unknown_var}: {input}",
        )
        # 未定義変数はクラッシュしない
        result = skill.render_prompt("テスト入力")
        assert "テスト入力" in result

    def test_to_dict(self):
        skill = SkillDefinition(
            name="test",
            description="テスト",
            prompt_template="入力: {input}",
            trigger_mode=SkillTriggerMode.MANUAL,
            aliases=["テスト"],
            tags=["開発"],
        )
        d = skill.to_dict()
        assert d["name"] == "test"
        assert d["trigger_mode"] == "manual"
        assert d["aliases"] == ["テスト"]
        assert d["tags"] == ["開発"]
