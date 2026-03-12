"""SkillRegistry のテスト"""
import pytest
from src.skills.models import SkillDefinition, SkillTriggerMode
from src.skills.registry import SkillRegistry


@pytest.fixture
def registry():
    """テスト用の新規レジストリ"""
    return SkillRegistry()


@pytest.fixture
def sample_skill():
    return SkillDefinition(
        name="translate",
        description="翻訳スキル",
        prompt_template="翻訳: {input}",
        trigger_mode=SkillTriggerMode.BOTH,
        aliases=["翻訳", "訳して"],
    )


class TestSkillRegistry:
    def test_register_and_get(self, registry, sample_skill):
        registry.register(sample_skill)
        assert registry.get("translate") is sample_skill
        assert len(registry) == 1

    def test_get_by_alias(self, registry, sample_skill):
        registry.register(sample_skill)
        # 名前自体でも取得可能
        assert registry.get_by_alias("translate") is sample_skill
        # エイリアスで取得
        assert registry.get_by_alias("翻訳") is sample_skill
        assert registry.get_by_alias("訳して") is sample_skill
        # 大文字小文字を無視
        assert registry.get_by_alias("TRANSLATE") is sample_skill

    def test_get_by_alias_not_found(self, registry):
        assert registry.get_by_alias("存在しない") is None

    def test_unregister(self, registry, sample_skill):
        registry.register(sample_skill)
        assert registry.unregister("translate") is True
        assert registry.get("translate") is None
        assert registry.get_by_alias("翻訳") is None
        assert len(registry) == 0

    def test_unregister_not_found(self, registry):
        assert registry.unregister("存在しない") is False

    def test_get_all(self, registry, sample_skill):
        registry.register(sample_skill)
        other = SkillDefinition(name="other", description="他", prompt_template="{input}")
        registry.register(other)
        assert len(registry.get_all()) == 2

    def test_get_names(self, registry, sample_skill):
        registry.register(sample_skill)
        assert "translate" in registry.get_names()

    def test_get_auto_skills(self, registry):
        auto_skill = SkillDefinition(
            name="auto", description="自動", prompt_template="{input}",
            trigger_mode=SkillTriggerMode.AUTO,
        )
        manual_skill = SkillDefinition(
            name="manual", description="手動", prompt_template="{input}",
            trigger_mode=SkillTriggerMode.MANUAL,
        )
        both_skill = SkillDefinition(
            name="both", description="両方", prompt_template="{input}",
            trigger_mode=SkillTriggerMode.BOTH,
        )
        registry.register(auto_skill)
        registry.register(manual_skill)
        registry.register(both_skill)

        auto_skills = registry.get_auto_skills()
        names = [s.name for s in auto_skills]
        assert "auto" in names
        assert "both" in names
        assert "manual" not in names

    def test_contains(self, registry, sample_skill):
        registry.register(sample_skill)
        assert "translate" in registry
        assert "nonexistent" not in registry
