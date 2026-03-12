"""
pytest共通設定とフィクスチャ
"""
import sys
import os
from pathlib import Path

import pytest

# プロジェクトルートをPYTHONPATHに追加
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# CI環境ではGPU不要な設定にする
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")


@pytest.fixture
def sample_config_dict():
    """テスト用の設定辞書"""
    return {
        "default_character": "zundamon",
        "llm_model": "gemini-3-flash-preview",
        "llm_provider": "gemini",
        "mode": "terminal",
        "device_index": 0,
        "web_interface": {"port": 3000},
        "memory": {"enabled": True},
    }


@pytest.fixture
def tmp_yaml_config(tmp_path, sample_config_dict):
    """一時YAMLファイルに設定を書き出すフィクスチャ"""
    import yaml

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        yaml.dump(sample_config_dict, allow_unicode=True), encoding="utf-8"
    )
    return config_file
