"""SkillLoader のテスト"""
import pytest
from pathlib import Path
from src.skills.loader import load_skill_from_yaml, load_all_skills, save_skill_to_yaml, delete_skill_yaml
from src.skills.models import SkillDefinition, SkillTriggerMode
from src.skills.registry import SkillRegistry


class TestLoadSkillFromYaml:
    def test_load_valid_yaml(self, sample_skill_yaml):
        skill = load_skill_from_yaml(sample_skill_yaml)
        assert skill is not None
        assert skill.name == "test_skill"
        assert skill.description == "テスト用スキル"
        assert skill.trigger_mode == SkillTriggerMode.BOTH
        assert "テスト" in skill.aliases
        assert "lang" in skill.parameters

    def test_load_nonexistent_file(self, tmp_path):
        skill = load_skill_from_yaml(tmp_path / "nonexistent.yaml")
        assert skill is None

    def test_load_invalid_yaml(self, tmp_path):
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text(": invalid: yaml: [", encoding="utf-8")
        skill = load_skill_from_yaml(bad_file)
        assert skill is None


class TestLoadAllSkills:
    def test_load_from_directory(self, sample_skills_dir, sample_skill_yaml):
        # 新規レジストリを使って重複を避ける
        skills = load_all_skills(sample_skills_dir)
        assert len(skills) >= 1
        assert any(s.name == "test_skill" for s in skills)

    def test_load_from_empty_directory(self, tmp_path):
        empty_dir = tmp_path / "empty_skills"
        empty_dir.mkdir()
        skills = load_all_skills(empty_dir)
        assert skills == []

    def test_creates_directory_if_missing(self, tmp_path):
        new_dir = tmp_path / "new_skills_dir"
        assert not new_dir.exists()
        skills = load_all_skills(new_dir)
        assert new_dir.exists()
        assert skills == []


class TestSaveSkillToYaml:
    def test_save_and_reload(self, sample_skills_dir):
        skill = SkillDefinition(
            name="saved_skill",
            description="保存テスト",
            prompt_template="テスト: {input}",
            aliases=["保存"],
        )
        result = save_skill_to_yaml(skill, sample_skills_dir)
        assert result is True

        saved_path = sample_skills_dir / "saved_skill.yaml"
        assert saved_path.exists()

        # リロード
        loaded = load_skill_from_yaml(saved_path)
        assert loaded.name == "saved_skill"
        assert loaded.description == "保存テスト"
        assert "保存" in loaded.aliases


class TestDeleteSkillYaml:
    def test_delete_existing(self, sample_skills_dir, sample_skill_yaml):
        assert sample_skill_yaml.exists()
        result = delete_skill_yaml("test_skill", sample_skills_dir)
        assert result is True
        assert not sample_skill_yaml.exists()

    def test_delete_nonexistent(self, sample_skills_dir):
        result = delete_skill_yaml("nonexistent", sample_skills_dir)
        assert result is False
