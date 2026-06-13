import inspect
from pathlib import Path

from src.tts.engines.irodori_tts_engine import IrodoriTTSEngine


def test_irodori_engine_uses_vendored_runtime_by_default():
    signature = inspect.signature(IrodoriTTSEngine)

    assert "repo_path" not in signature.parameters

    engine = IrodoriTTSEngine(use_gpu=False)

    assert not hasattr(engine, "repo_path")
    assert engine.num_steps == 6
    assert engine.t_schedule_mode == "sway"
    assert engine.sway_coeff == -1.0


def test_irodori_adapter_no_longer_references_external_checkout():
    root = Path(__file__).resolve().parents[1]
    source = (root / "src" / "tts" / "engines" / "irodori_tts_engine.py").read_text(
        encoding="utf-8"
    )

    assert "D:/tool/Irodori-TTS" not in source
    assert "sys.path" not in source
    assert "src.vendor.irodori_tts.inference_runtime" in source


def test_vendored_irodori_runtime_contains_sway_sampling_patch():
    root = Path(__file__).resolve().parents[1]
    runtime_source = (
        root / "src" / "vendor" / "irodori_tts" / "inference_runtime.py"
    ).read_text(encoding="utf-8")
    rf_source = (root / "src" / "vendor" / "irodori_tts" / "rf.py").read_text(
        encoding="utf-8"
    )

    assert 't_schedule_mode: str = "linear"' in runtime_source
    assert "sway_coeff: float = -1.0" in runtime_source
    assert 't_schedule_mode_norm == "sway"' in rf_source
    assert "torch.cos(0.5 * math.pi * u)" in rf_source
