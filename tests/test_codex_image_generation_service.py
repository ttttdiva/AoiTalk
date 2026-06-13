import subprocess

import pytest

from src.services import codex_image_generation_service as service


@pytest.mark.asyncio
async def test_generate_codex_image_uses_gpt55_medium_and_returns_metadata(tmp_path, monkeypatch):
    calls = {}

    def fake_run_codex(**kwargs):
        calls.update(kwargs)
        kwargs["output_path"].write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 1024)
        kwargs["response_path"].write_text(
            '{"prompt":"misty ruins, torchlight","engine":"codex-cli"}',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args=["codex"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(service, "_run_codex", fake_run_codex)

    result = await service.generate_codex_image(
        visual_request="現在の廃墟を描く",
        scene_context="探索者たちは霧の濃い遺跡にいる。",
        output_dir=tmp_path,
        model="gpt-5.5",
        reasoning_effort="medium",
        timeout_seconds=1,
    )

    assert calls["model"] == "gpt-5.5"
    assert calls["reasoning_effort"] == "medium"
    assert "Use Codex CLI model gpt-5.5 with reasoning effort medium" in calls["prompt"]
    assert result["engine"] == "codex-cli"
    assert result["model"] == "gpt-5.5"
    assert result["reasoning_effort"] == "medium"
    assert result["prompt"] == "misty ruins, torchlight"
    assert result["filename"].startswith("codex_trpg_")


@pytest.mark.asyncio
async def test_generate_codex_image_rejects_missing_image(tmp_path, monkeypatch):
    def fake_run_codex(**kwargs):
        kwargs["response_path"].write_text("text only", encoding="utf-8")
        return subprocess.CompletedProcess(args=["codex"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(service, "_run_codex", fake_run_codex)

    with pytest.raises(service.CodexImageGenerationError):
        await service.generate_codex_image(
            visual_request="現在の状況",
            scene_context="ログ",
            output_dir=tmp_path,
            timeout_seconds=1,
        )
