import subprocess
from pathlib import Path

from src.llm.cli_backends.base import CLIBackendBase, _decode_cli_output
from src.llm.cli_backends.gemini import GeminiCLIBackend


class FakeBackend(CLIBackendBase):
    def get_cli_command(self, prompt: str):
        return ["fake-cli", prompt] if prompt else ["fake-cli"]

    def get_provider_name(self) -> str:
        return "Fake CLI"


def test_decode_cli_output_falls_back_from_utf8_to_cp932():
    assert _decode_cli_output("こんにちは".encode("cp932")) == "こんにちは"


def test_execute_prompt_decodes_subprocess_bytes_without_text_mode(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="応答です".encode("cp932"),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    success, output = FakeBackend().execute_prompt(
        "ユーザー入力",
        cwd=Path("."),
        system_context="会話履歴",
    )

    assert success is True
    assert output == "応答です"
    assert calls[0]["input"] == "会話履歴".encode("utf-8")
    assert "text" not in calls[0]
    assert "encoding" not in calls[0]


def test_gemini_backend_initializes_base_provider_name():
    backend = GeminiCLIBackend(model="gemini-2.5-flash")

    assert backend.provider_name == "Gemini CLI"
