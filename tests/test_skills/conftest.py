"""Skills テスト用 conftest"""
import pytest
from pathlib import Path


@pytest.fixture
def sample_skills_dir(tmp_path):
    """一時スキルディレクトリを作成"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    return skills_dir


@pytest.fixture
def sample_skill_yaml(sample_skills_dir):
    """サンプルスキルYAMLファイルを作成"""
    yaml_content = """
name: test_skill
description: テスト用スキル
prompt_template: |
  テスト指示: {input}
  言語: {lang}
trigger_mode: both
aliases:
  - テスト
  - test
bound_tools: []
examples:
  - "/test_skill hello"
tags:
  - テスト
parameters:
  lang:
    description: 言語
    default: 日本語
"""
    path = sample_skills_dir / "test_skill.yaml"
    path.write_text(yaml_content, encoding="utf-8")
    return path
