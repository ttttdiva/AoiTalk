"""Generate a MioTTS preset embedding from reference audio."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.tts.engines.miotts_engine import MioTTSEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True, help="Reference audio path")
    parser.add_argument("--preset-id", required=True, help="Preset id to create")
    parser.add_argument("--model-id", default=None, help="MioTTS model id")
    parser.add_argument("--codec-model-id", default=None, help="MioCodec model id")
    parser.add_argument("--presets-dir", default=None, help="Output presets directory")
    parser.add_argument("--cache-dir", default=None, help="Model cache directory")
    parser.add_argument("--device", default="auto", help="auto, cuda, or cpu")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    engine = MioTTSEngine(
        model_id=args.model_id,
        codec_model_id=args.codec_model_id,
        presets_dir=args.presets_dir,
        cache_dir=args.cache_dir,
        device=args.device,
    )
    if not await engine.initialize():
        return 1
    output = await engine.generate_preset(args.audio, args.preset_id)
    await engine.cleanup()
    if output is None:
        return 1
    print(Path(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
