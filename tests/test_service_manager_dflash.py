import src.service_manager as service_manager
from src.service_manager import (
    _openai_compatible_local_model,
    _should_start_luce_dflash,
    _should_start_qwopus_llama_server,
    ensure_openai_compatible_local_server,
)


class DummyConfig:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        value = self.values
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


def test_starts_luce_dflash_for_openai_compatible_dflash_model():
    config = DummyConfig(
        {
            "llm_provider": "openai_compatible_local",
            "llm_model": "qwen3.6-27b-dflash",
            "openai_compatible_local": {
                "model": "qwen3.6-27b-dflash",
                "base_url": "http://127.0.0.1:8080/v1",
            },
        }
    )

    assert _should_start_luce_dflash(config) is True


def test_does_not_start_luce_dflash_for_generic_local_model_on_same_port():
    config = DummyConfig(
        {
            "llm_provider": "openai_compatible_local",
            "llm_model": "local-model",
            "openai_compatible_local": {
                "model": "local-model",
                "base_url": "http://127.0.0.1:8080/v1",
            },
        }
    )

    assert _should_start_luce_dflash(config) is False


def test_starts_qwopus_llama_server_for_openai_compatible_qwopus_model():
    config = DummyConfig(
        {
            "llm_provider": "openai_compatible_local",
            "llm_model": "qwopus3.6-35b-a3b",
            "openai_compatible_local": {
                "model": "qwopus3.6-35b-a3b",
                "base_url": "http://127.0.0.1:8080/v1",
            },
        }
    )

    assert _should_start_qwopus_llama_server(config) is True
    assert _should_start_luce_dflash(config) is False


def test_does_not_start_qwopus_llama_server_when_explicitly_disabled():
    config = DummyConfig(
        {
            "llm_provider": "openai_compatible_local",
            "llm_model": "qwopus3.6-35b-a3b",
            "openai_compatible_local": {
                "model": "qwopus3.6-35b-a3b",
                "base_url": "http://127.0.0.1:8080/v1",
                "qwopus": {"auto_start": False},
            },
        }
    )

    assert _should_start_qwopus_llama_server(config) is False


def test_does_not_start_luce_dflash_for_other_provider():
    config = DummyConfig(
        {
            "llm_provider": "ollama",
            "llm_model": "qwen3.6-27b-dflash",
            "openai_compatible_local": {"model": "qwen3.6-27b-dflash"},
        }
    )

    assert _should_start_luce_dflash(config) is False


def test_does_not_start_qwopus_llama_server_for_ollama_provider():
    config = DummyConfig(
        {
            "llm_provider": "ollama",
            "llm_model": "qwopus3.6-35b-a3b",
            "openai_compatible_local": {"model": "qwopus3.6-35b-a3b"},
        }
    )

    assert _should_start_qwopus_llama_server(config) is False


def test_openai_compatible_local_model_falls_back_to_llm_model():
    config = DummyConfig(
        {
            "llm_provider": "openai_compatible_local",
            "llm_model": "qwopus3.6-35b-a3b",
            "openai_compatible_local": {},
        }
    )

    assert _openai_compatible_local_model(config) == "qwopus3.6-35b-a3b"


def test_ensure_starts_qwopus_for_hot_switch(monkeypatch):
    config = DummyConfig(
        {
            "llm_provider": "openai_compatible_local",
            "llm_model": "qwopus3.6-35b-a3b",
            "openai_compatible_local": {"model": "qwopus3.6-35b-a3b"},
        }
    )
    calls = []

    monkeypatch.setattr(service_manager, "_local_openai_model_ids", lambda: set())
    monkeypatch.setattr(
        service_manager,
        "_start_qwopus_llama_server",
        lambda project_root: calls.append(("qwopus", project_root)),
    )
    monkeypatch.setattr(
        service_manager,
        "_start_luce_dflash",
        lambda project_root: calls.append(("dflash", project_root)),
    )

    assert ensure_openai_compatible_local_server(config) is True
    assert [name for name, _ in calls] == ["qwopus"]


def test_ensure_continues_when_qwopus_launch_fails(monkeypatch):
    config = DummyConfig(
        {
            "llm_provider": "openai_compatible_local",
            "llm_model": "qwopus3.6-35b-a3b",
            "openai_compatible_local": {"model": "qwopus3.6-35b-a3b"},
        }
    )

    monkeypatch.setattr(service_manager, "_local_openai_model_ids", lambda: set())
    monkeypatch.setattr(
        service_manager,
        "_start_qwopus_llama_server",
        lambda project_root: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert ensure_openai_compatible_local_server(config) is False


def test_ensure_starts_dflash_for_hot_switch(monkeypatch):
    config = DummyConfig(
        {
            "llm_provider": "openai_compatible_local",
            "llm_model": "qwen3.6-27b-dflash",
            "openai_compatible_local": {"model": "qwen3.6-27b-dflash"},
        }
    )
    calls = []

    monkeypatch.setattr(
        service_manager,
        "_local_openai_model_ids",
        lambda: {"qwopus3.6-35b-a3b"},
    )
    monkeypatch.setattr(
        service_manager,
        "_start_qwopus_llama_server",
        lambda project_root: calls.append(("qwopus", project_root)),
    )
    monkeypatch.setattr(
        service_manager,
        "_start_luce_dflash",
        lambda project_root: calls.append(("dflash", project_root)),
    )

    assert ensure_openai_compatible_local_server(config) is True
    assert [name for name, _ in calls] == ["dflash"]


def test_ensure_skips_already_running_local_server(monkeypatch):
    config = DummyConfig(
        {
            "llm_provider": "openai_compatible_local",
            "llm_model": "qwopus3.6-35b-a3b",
            "openai_compatible_local": {"model": "qwopus3.6-35b-a3b"},
        }
    )

    monkeypatch.setattr(
        service_manager,
        "_local_openai_model_ids",
        lambda: {"qwopus3.6-35b-a3b"},
    )
    monkeypatch.setattr(
        service_manager,
        "_start_qwopus_llama_server",
        lambda project_root: (_ for _ in ()).throw(AssertionError("unexpected start")),
    )

    assert ensure_openai_compatible_local_server(config) is False


def test_qwopus_launcher_does_not_wait_for_model_readiness(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 1234

    monkeypatch.setattr(service_manager, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        service_manager.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        service_manager,
        "_wait_for_port",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("waited")),
    )

    service_manager._child_processes.clear()
    service_manager._start_qwopus_llama_server(service_manager.Path.cwd())
    calls.extend(proc.pid for proc in service_manager._child_processes)

    assert calls == [1234]
    service_manager._child_processes.clear()


def test_ensure_does_not_start_for_custom_local_server(monkeypatch):
    config = DummyConfig(
        {
            "llm_provider": "openai_compatible_local",
            "llm_model": "local-model",
            "openai_compatible_local": {"model": "local-model"},
        }
    )

    monkeypatch.setattr(
        service_manager,
        "_start_qwopus_llama_server",
        lambda project_root: (_ for _ in ()).throw(AssertionError("unexpected start")),
    )
    monkeypatch.setattr(
        service_manager,
        "_start_luce_dflash",
        lambda project_root: (_ for _ in ()).throw(AssertionError("unexpected start")),
    )

    assert ensure_openai_compatible_local_server(config) is False
