"""
Irodori-TTS engine adapter.

The implementation keeps the Irodori model lazy: engine initialization only
checks that the vendored Irodori-TTS runtime can be imported. The first
synthesis downloads the configured Hugging Face checkpoint and codec, then
caches the loaded runtime for later requests.
"""

import asyncio
import io
import os
from pathlib import Path
from typing import Any, Optional


class IrodoriTTSEngine:
    """Irodori-TTS adapter returning WAV bytes for AoiTalk playback."""

    DEFAULT_CHECKPOINT = "Aratako/Irodori-TTS-500M-v2"
    DEFAULT_VOICE_DESIGN_CHECKPOINT = "Aratako/Irodori-TTS-500M-v2-VoiceDesign"
    DEFAULT_CODEC_REPO = "Aratako/Semantic-DACVAE-Japanese-32dim"

    def __init__(
        self,
        hf_checkpoint: Optional[str] = None,
        voice_design_checkpoint: Optional[str] = None,
        codec_repo: Optional[str] = None,
        refs_dir: Optional[str] = None,
        cache_dir: Optional[str] = None,
        model_device: str = "cuda",
        codec_device: str = "cuda",
        model_precision: str = "fp32",
        codec_precision: str = "fp32",
        use_gpu: bool = True,
        num_steps: int = 6,
        t_schedule_mode: str = "sway",
        sway_coeff: float = -1.0,
        seconds: float = 30.0,
        max_ref_seconds: Optional[float] = 30.0,
        ref_normalize_db: Optional[float] = None,
        ref_ensure_max: bool = True,
        cfg_scale_text: float = 3.0,
        cfg_scale_caption: float = 3.0,
        cfg_scale_speaker: float = 5.0,
        config: Optional[Any] = None,
    ):
        root = Path(__file__).resolve().parents[3]
        self.repo_root = root
        self.hf_checkpoint = hf_checkpoint or self.DEFAULT_CHECKPOINT
        self.voice_design_checkpoint = (
            voice_design_checkpoint or self.DEFAULT_VOICE_DESIGN_CHECKPOINT
        )
        self.codec_repo = codec_repo or self.DEFAULT_CODEC_REPO
        self.refs_dir = self._resolve_path(refs_dir or "config/irodori_refs")
        self.cache_dir = self._resolve_path(cache_dir or "cache/irodori_tts")
        self.use_gpu = bool(use_gpu)
        self.model_device = self._normalize_device(model_device)
        self.codec_device = self._normalize_device(codec_device)
        self.model_precision = self._normalize_precision(model_precision, self.model_device)
        self.codec_precision = self._normalize_precision(codec_precision, self.codec_device)
        self.num_steps = int(num_steps)
        self.t_schedule_mode = str(t_schedule_mode)
        self.sway_coeff = float(sway_coeff)
        self.seconds = float(seconds)
        self.max_ref_seconds = max_ref_seconds
        self.ref_normalize_db = ref_normalize_db
        self.ref_ensure_max = bool(ref_ensure_max)
        self.cfg_scale_text = float(cfg_scale_text)
        self.cfg_scale_caption = float(cfg_scale_caption)
        self.cfg_scale_speaker = float(cfg_scale_speaker)
        self.config = config

        self._runtime_symbols: Optional[dict[str, Any]] = None
        self._checkpoint_paths: dict[str, str] = {}
        self._init_lock = asyncio.Lock()

    def _resolve_path(self, raw: str) -> Path:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = self.repo_root / path
        return path

    def _normalize_device(self, requested: str) -> str:
        device = str(requested or "cpu").strip().lower()
        if not self.use_gpu and device != "cpu":
            return "cpu"
        if device == "cuda":
            try:
                import torch

                if not torch.cuda.is_available():
                    return "cpu"
            except Exception:
                return "cpu"
        return device

    @staticmethod
    def _normalize_precision(requested: str, device: str) -> str:
        precision = str(requested or "fp32").strip().lower()
        if device == "cpu" and precision == "bf16":
            return "fp32"
        return precision

    async def initialize(self) -> bool:
        """Prepare imports and directories without loading model weights."""
        try:
            self.refs_dir.mkdir(parents=True, exist_ok=True)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            await self._ensure_runtime_symbols()
            print("[Irodori-TTS] Engine initialized; model loads on first synthesis")
            return True
        except Exception as exc:
            print(f"[Irodori-TTS] Initialization error: {exc}")
            return False

    async def _ensure_runtime_symbols(self) -> dict[str, Any]:
        async with self._init_lock:
            if self._runtime_symbols is not None:
                return self._runtime_symbols

            hf_home = self.cache_dir / "hf_home"
            hf_hub_cache = self.cache_dir / "hf"
            hf_home.mkdir(parents=True, exist_ok=True)
            hf_hub_cache.mkdir(parents=True, exist_ok=True)
            os.environ.setdefault("HF_HOME", str(hf_home))
            os.environ.setdefault("HF_HUB_CACHE", str(hf_hub_cache))

            from huggingface_hub import hf_hub_download
            from src.vendor.irodori_tts.inference_runtime import (
                RuntimeKey,
                SamplingRequest,
                get_cached_runtime,
            )

            self._runtime_symbols = {
                "hf_hub_download": hf_hub_download,
                "RuntimeKey": RuntimeKey,
                "SamplingRequest": SamplingRequest,
                "get_cached_runtime": get_cached_runtime,
            }
            return self._runtime_symbols

    def _resolve_checkpoint(self, checkpoint: str) -> str:
        checkpoint_key = str(checkpoint).strip()
        if checkpoint_key in self._checkpoint_paths:
            return self._checkpoint_paths[checkpoint_key]

        path = Path(checkpoint_key).expanduser()
        if not path.is_absolute():
            candidate = self.repo_root / path
            if candidate.exists():
                path = candidate
        if path.exists() and path.is_file():
            resolved = str(path)
        else:
            symbols = self._runtime_symbols
            if symbols is None:
                raise RuntimeError("Irodori runtime symbols are not loaded")
            resolved = symbols["hf_hub_download"](
                repo_id=checkpoint_key,
                filename="model.safetensors",
                cache_dir=str(self.cache_dir / "hf"),
            )
            print(f"[Irodori-TTS] checkpoint: hf://{checkpoint_key} -> {resolved}")

        self._checkpoint_paths[checkpoint_key] = resolved
        return resolved

    def _build_runtime_key(self, checkpoint_path: str):
        symbols = self._runtime_symbols
        if symbols is None:
            raise RuntimeError("Irodori runtime symbols are not loaded")
        RuntimeKey = symbols["RuntimeKey"]
        return RuntimeKey(
            checkpoint=checkpoint_path,
            model_device=self.model_device,
            codec_repo=self.codec_repo,
            model_precision=self.model_precision,
            codec_device=self.codec_device,
            codec_precision=self.codec_precision,
        )

    def _find_reference_wav(
        self,
        ref_wav: Optional[str],
        voice_name: Optional[str],
        character_name: Optional[str],
    ) -> Optional[str]:
        if ref_wav:
            path = self._resolve_path(ref_wav)
            return str(path)

        candidates = [voice_name, character_name]
        extensions = [".wav", ".mp3", ".flac", ".ogg", ".m4a"]
        for candidate in candidates:
            if not candidate:
                continue
            raw = str(candidate).strip()
            direct = self._resolve_path(raw)
            if direct.exists() and direct.is_file():
                return str(direct)
            for ext in extensions:
                path = self.refs_dir / f"{raw}{ext}"
                if path.exists() and path.is_file():
                    return str(path)
        return None

    @staticmethod
    def _result_to_wav_bytes(audio: Any, sample_rate: int) -> bytes:
        import soundfile as sf

        tensor = audio.detach().cpu()
        data = tensor.numpy()
        if data.ndim == 2:
            data = data.T
        buffer = io.BytesIO()
        sf.write(buffer, data, int(sample_rate), format="WAV", subtype="PCM_16")
        buffer.seek(0)
        return buffer.read()

    async def synthesize(
        self,
        text: str,
        voice_name: Optional[str] = None,
        character_name: Optional[str] = None,
        ref_wav: Optional[str] = None,
        ref_latent: Optional[str] = None,
        caption: Optional[str] = None,
        no_ref: Optional[bool] = None,
        voice_design: bool = False,
        **kwargs,
    ) -> Optional[bytes]:
        if not text or not text.strip():
            return None

        await self._ensure_runtime_symbols()
        symbols = self._runtime_symbols
        if symbols is None:
            return None

        use_voice_design = bool(voice_design)
        checkpoint = (
            self.voice_design_checkpoint if use_voice_design else self.hf_checkpoint
        )
        checkpoint_path = self._resolve_checkpoint(checkpoint)
        runtime_key = self._build_runtime_key(checkpoint_path)
        runtime, reloaded = symbols["get_cached_runtime"](runtime_key)
        if reloaded:
            print("[Irodori-TTS] Loaded runtime")

        resolved_ref_wav = self._find_reference_wav(ref_wav, voice_name, character_name)
        resolved_ref_latent = str(self._resolve_path(ref_latent)) if ref_latent else None
        should_use_no_ref = bool(no_ref)
        if not resolved_ref_wav and not resolved_ref_latent:
            should_use_no_ref = True

        SamplingRequest = symbols["SamplingRequest"]
        result = runtime.synthesize(
            SamplingRequest(
                text=str(text),
                caption=None if caption is None else str(caption),
                ref_wav=resolved_ref_wav,
                ref_latent=resolved_ref_latent,
                no_ref=should_use_no_ref,
                num_steps=int(kwargs.get("num_steps", self.num_steps)),
                t_schedule_mode=str(
                    kwargs.get("t_schedule_mode", self.t_schedule_mode)
                ),
                sway_coeff=float(kwargs.get("sway_coeff", self.sway_coeff)),
                seconds=float(kwargs.get("seconds", self.seconds)),
                max_ref_seconds=self.max_ref_seconds,
                ref_normalize_db=kwargs.get(
                    "ref_normalize_db", self.ref_normalize_db
                ),
                ref_ensure_max=bool(kwargs.get("ref_ensure_max", self.ref_ensure_max)),
                cfg_scale_text=float(kwargs.get("cfg_scale_text", self.cfg_scale_text)),
                cfg_scale_caption=float(
                    kwargs.get("cfg_scale_caption", self.cfg_scale_caption)
                ),
                cfg_scale_speaker=float(
                    kwargs.get("cfg_scale_speaker", self.cfg_scale_speaker)
                ),
                seed=kwargs.get("seed"),
            ),
            log_fn=None,
        )
        wav_bytes = self._result_to_wav_bytes(result.audio, result.sample_rate)
        print(
            f"[Irodori-TTS] Generated {len(wav_bytes)} bytes at {result.sample_rate}Hz"
        )
        return wav_bytes

    async def cleanup(self):
        try:
            if self._runtime_symbols is not None:
                from src.vendor.irodori_tts.inference_runtime import clear_cached_runtime

                clear_cached_runtime()
            self._checkpoint_paths.clear()
            print("[Irodori-TTS] Cleanup complete")
        except Exception as exc:
            print(f"[Irodori-TTS] Cleanup error: {exc}")
