from src.config import Config


def test_config_loads():
    config = Config()
    assert config.get("llm_model")
    assert config.get("default_character")
    assert config.get("web_interface.port") == 3000


def test_available_characters_non_empty():
    config = Config()
    assert config.get_available_characters()
