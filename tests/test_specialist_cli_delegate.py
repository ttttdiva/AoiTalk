from __future__ import annotations

from pathlib import Path

from src.llm.cli_backends.claude import ClaudeCLIBackend
from src.llm.cli_backends.gemini import GeminiCLIBackend
from src.llm.cli_backends.codex import CodexCLIBackend
from src.llm.specialist_delegate import UtilityDelegationRunner
from src.tools.core import tool
from src.tools.registry import ToolRegistry


class FakeCLIBackend:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def execute_prompt(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: int = 300,
        extra_args=None,
        system_context=None,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": cwd,
                "system_context": system_context,
            }
        )
        return True, self._responses.pop(0)

    def parse_tool_calls(self, cli_output: str):
        from src.tools.adapters import CLIAdapter

        return CLIAdapter.parse_tool_calls(cli_output)


def test_codex_backend_uses_model_from_config(monkeypatch):
    monkeypatch.setenv("CODEX_AUTO_APPROVE", "true")

    backend = CodexCLIBackend(model="gpt-5-codex")

    assert backend.get_cli_command("hello") == [
        "codex",
        "exec",
        "--json",
        "--color",
        "never",
        "--ephemeral",
        "--ignore-rules",
        "--model",
        "gpt-5-codex",
        "--sandbox",
        "read-only",
        "hello",
    ]


def test_codex_backend_uses_reasoning_effort_from_config(monkeypatch):
    monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)

    backend = CodexCLIBackend(model="gpt-5-codex", reasoning_effort="high")

    assert backend.get_cli_command("hello") == [
        "codex",
        "exec",
        "--json",
        "--color",
        "never",
        "--ephemeral",
        "--ignore-rules",
        "--model",
        "gpt-5-codex",
        "-c",
        'model_reasoning_effort="high"',
        "--sandbox",
        "read-only",
        "hello",
    ]


def test_claude_backend_uses_reasoning_effort_from_config(monkeypatch):
    monkeypatch.delenv("CLAUDE_EFFORT", raising=False)

    backend = ClaudeCLIBackend(model="sonnet", reasoning_effort="xhigh")

    assert backend.get_cli_command("hello") == [
        "claude",
        "-p",
        "hello",
        "--output-format",
        "json",
        "--model",
        "sonnet",
        "--effort",
        "xhigh",
    ]


def test_codex_backend_extracts_jsonl_agent_message():
    backend = CodexCLIBackend(model="gpt-5-codex")

    output = backend.parse_output(
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"t"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"最終応答"}}',
                '{"type":"turn.completed","usage":{"input_tokens":1}}',
            ]
        )
    )

    assert output == "最終応答"


def test_codex_backend_extracts_jsonl_usage_limit_error():
    backend = CodexCLIBackend(model="gpt-5.3-codex-spark")

    output = backend.parse_error_output(
        stdout="\n".join(
            [
                '{"type":"thread.started","thread_id":"t"}',
                '{"type":"turn.started"}',
                '{"type":"error","message":"You\'ve hit your usage limit for GPT-5.3-Codex-Spark. Switch to another model now, or try again at 5:46 PM."}',
                '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit for GPT-5.3-Codex-Spark. Switch to another model now, or try again at 5:46 PM."}}',
            ]
        ),
        stderr='Reading prompt from stdin...\nエラー: プロセス "14824" が見つかりませんでした。',
        exit_code=1,
    )

    assert output == (
        "Codex CLI の利用上限に達しました（GPT-5.3-Codex-Spark）。"
        "別の Codex モデルへ切り替えるか、5:46 PM 以降に再試行してください。"
    )
    assert "thread.started" not in output
    assert "プロセス" not in output


def test_codex_backend_sends_combined_prompt_via_stdin(monkeypatch):
    calls = []

    class RecordingCodex(CodexCLIBackend):
        def get_cli_command(self, prompt: str):
            calls.append({"command_prompt": prompt})
            return super().get_cli_command(prompt)

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return __import__("subprocess").CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=(
                '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n'
            ).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    success, output = RecordingCodex(model="gpt-5-codex").execute_prompt(
        prompt="今の質問",
        system_context="会話履歴",
    )

    assert success is True
    assert output == "OK"
    assert calls[0]["command_prompt"] == ""
    stdin_text = calls[1]["input"].decode("utf-8")
    assert "plain text-generation backend inside the AoiTalk chat app" in stdin_text
    assert stdin_text.endswith("会話履歴\n\nUser request:\n今の質問")
    assert "今の質問" not in calls[1]["cmd"]


def test_gemini_backend_uses_model_from_config(monkeypatch):
    monkeypatch.setenv("GEMINI_AUTO_APPROVE", "true")

    backend = GeminiCLIBackend(model="gemini-2.5-flash")

    assert backend.get_cli_command("hello") == [
        "gemini",
        "--yolo",
        "-m",
        "gemini-2.5-flash",
        "-p",
        "hello",
    ]


def test_specialist_runner_uses_codex_cli_when_configured(monkeypatch):
    config = {
        "llm_provider": "codex-cli",
        "llm_model": "gpt-5-codex",
        "agents": {
            "provider": "codex-cli",
            "model": "gpt-5-codex",
        },
    }

    runner = UtilityDelegationRunner(config)
    calls = []
    registry = ToolRegistry()

    @tool
    def fake_tool(value: str) -> str:
        """Record delegated tool execution."""
        calls.append(value)
        return f"tool:{value}"

    runner._tool_registry = registry
    registry.register(fake_tool)
    runner.cli_backend = FakeCLIBackend(
        [
            '[TOOL_CALL: fake_tool(value="delegated")]',
            "final response",
        ]
    )

    result = runner.run("handle this with the utility specialist")

    assert runner.provider == "codex-cli"
    assert runner.model == "gpt-5-codex"
    assert result == "final response"
    assert calls == ["delegated"]
