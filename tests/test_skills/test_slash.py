"""スラッシュコマンドによるスキル明示呼び出し (resolve_skill_slash_command) のテスト"""
import pytest

from src.skills.models import SkillDefinition, SkillTriggerMode
from src.skills.registry import get_skill_registry
from src.skills.slash import resolve_skill_slash_command


@pytest.fixture
def registered_skills():
    """グローバルレジストリへテスト用スキルを登録し、終了後に解除する"""
    registry = get_skill_registry()
    skills = [
        SkillDefinition(
            name="translate",
            description="翻訳",
            prompt_template="次を翻訳: {input}",
            trigger_mode=SkillTriggerMode.BOTH,
            aliases=["翻訳"],
        ),
        SkillDefinition(
            name="manualonly",
            description="手動専用",
            prompt_template="手動: {input}",
            trigger_mode=SkillTriggerMode.MANUAL,
        ),
        SkillDefinition(
            name="autoonly",
            description="自動専用",
            prompt_template="自動: {input}",
            trigger_mode=SkillTriggerMode.AUTO,
        ),
    ]
    for skill in skills:
        registry.register(skill)
    yield registry
    for skill in skills:
        registry.unregister(skill.name)


class TestResolveSkillSlashCommand:
    def test_both_mode_with_input(self, registered_skills):
        result = resolve_skill_slash_command("/translate hello world")
        assert result is not None
        assert "[スキル: translate]" in result
        assert "次を翻訳: hello world" in result

    def test_alias(self, registered_skills):
        result = resolve_skill_slash_command("/翻訳 こんにちは")
        assert result is not None
        assert "次を翻訳: こんにちは" in result

    def test_manual_mode_allowed(self, registered_skills):
        result = resolve_skill_slash_command("/manualonly foo")
        assert result is not None
        assert "手動: foo" in result

    def test_auto_mode_excluded(self, registered_skills):
        # AUTO は LLM 自動判断専用なのでスラッシュ明示呼び出し対象外
        assert resolve_skill_slash_command("/autoonly foo") is None

    def test_no_input_text(self, registered_skills):
        result = resolve_skill_slash_command("/translate")
        assert result is not None
        assert "次を翻訳:" in result

    def test_full_width_space_separator(self, registered_skills):
        result = resolve_skill_slash_command("/translate\u3000あいう")
        assert result is not None
        assert "次を翻訳: あいう" in result

    def test_unknown_command_passthrough(self, registered_skills):
        assert resolve_skill_slash_command("/unknown foo") is None

    def test_non_slash_message(self, registered_skills):
        assert resolve_skill_slash_command("translate this") is None

    def test_bare_slash(self, registered_skills):
        assert resolve_skill_slash_command("/") is None
        assert resolve_skill_slash_command("/ foo") is None

    def test_empty(self, registered_skills):
        assert resolve_skill_slash_command("") is None
        assert resolve_skill_slash_command("   ") is None

    def test_leading_whitespace(self, registered_skills):
        result = resolve_skill_slash_command("  /translate hi  ")
        assert result is not None
        assert "次を翻訳: hi" in result
