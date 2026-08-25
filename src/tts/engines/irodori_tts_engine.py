"""
Irodori-TTS engine adapter.

The implementation keeps the Irodori model lazy: engine initialization only
checks that the vendored Irodori-TTS runtime can be imported. The first
synthesis downloads the configured Hugging Face checkpoint and codec, then
caches the loaded runtime for later requests.
"""

import asyncio
import io
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Optional

from ..irodori_config import IRODORI_TTS_CHECKPOINT, resolve_irodori_checkpoint


class IrodoriTTSEngine:
    """Irodori-TTS adapter returning WAV bytes for AoiTalk playback."""

    DEFAULT_CHECKPOINT = IRODORI_TTS_CHECKPOINT
    DEFAULT_CODEC_REPO = "Aratako/Semantic-DACVAE-Japanese-32dim"

    def __init__(
        self,
        hf_checkpoint: Optional[str] = None,
        codec_repo: Optional[str] = None,
        refs_dir: Optional[str] = None,
        model_device: str = "cuda",
        codec_device: str = "cuda",
        model_precision: str = "fp32",
        codec_precision: str = "fp32",
        use_gpu: bool = True,
        num_steps: int = 40,
        t_schedule_mode: str = "linear",
        sway_coeff: float = -1.0,
        seconds: Optional[float] = None,
        duration_scale: float = 1.0,
        max_ref_seconds: Optional[float] = None,
        ref_normalize_db: Optional[float] = -16.0,
        ref_ensure_max: bool = True,
        cfg_scale_text: float = 3.0,
        cfg_scale_caption: float = 3.0,
        cfg_scale_speaker: float = 5.0,
        irodori_model: Optional[str] = None,
        config: Optional[Any] = None,
    ):
        root = Path(__file__).resolve().parents[3]
        self.repo_root = root
        # Keep explicit local/Hugging Face checkpoints intact.  In particular,
        # callers may still select the v3 VoiceDesign checkpoint while the
        # AoiTalk default points at the unified v4.1-Small release.
        # Keep explicit local/Hugging Face checkpoints intact while allowing a
        # character-facing selector to resolve to the canonical v3/v4 repo.
        self.hf_checkpoint = resolve_irodori_checkpoint(
            {
                "hf_checkpoint": hf_checkpoint,
                "irodori_model": irodori_model,
            }
        )
        self.codec_repo = str(codec_repo or self.DEFAULT_CODEC_REPO).strip()
        self.refs_dir = self._resolve_path(refs_dir or "config/irodori_refs")
        self.use_gpu = bool(use_gpu)
        self.model_device = self._normalize_device(model_device)
        self.codec_device = self._normalize_device(codec_device)
        self.model_precision = self._normalize_precision(model_precision, self.model_device)
        self.codec_precision = self._normalize_precision(codec_precision, self.codec_device)
        self.num_steps = int(num_steps)
        self.t_schedule_mode = str(t_schedule_mode)
        self.sway_coeff = float(sway_coeff)
        self.seconds = None if seconds is None else float(seconds)
        self.duration_scale = float(duration_scale)
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

            from huggingface_hub import hf_hub_download
            from src.vendor.irodori_tts.inference_runtime import (
                RuntimeKey,
                SamplingRequest,
                acquire_cached_runtime,
                get_cached_runtime,
                download_hf_checkpoint,
            )

            self._runtime_symbols = {
                "hf_hub_download": hf_hub_download,
                "RuntimeKey": RuntimeKey,
                "SamplingRequest": SamplingRequest,
                "acquire_cached_runtime": acquire_cached_runtime,
                "get_cached_runtime": get_cached_runtime,
                "download_hf_checkpoint": download_hf_checkpoint,
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
            try:
                # v4 checkpoints carry the tokenizer beside model.safetensors;
                # snapshot_download preserves that layout so the runtime can
                # reuse bundled assets without an additional network request.
                downloader = symbols.get("download_hf_checkpoint")
                if callable(downloader):
                    resolved = downloader(checkpoint_key)
                else:
                    resolved = symbols["hf_hub_download"](
                        repo_id=checkpoint_key,
                        filename="model.safetensors",
                    )
            except Exception as exc:
                raise RuntimeError(
                    "Irodori-TTS checkpoint download failed: "
                    f"repo={checkpoint_key}, file=model.safetensors, "
                    f"cause={exc}"
                ) from exc
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
        ref_wavs: Optional[list[str]] = None,
        ref_latent: Optional[str] = None,
        ref_latents: Optional[list[str]] = None,
        caption: Optional[str] = None,
        no_ref: Optional[bool] = None,
        **kwargs,
    ) -> Optional[bytes]:
        if not text or not text.strip():
            return None

        await self._ensure_runtime_symbols()
        symbols = self._runtime_symbols
        if symbols is None:
            return None

        checkpoint_path = self._resolve_checkpoint(self.hf_checkpoint)
        runtime_key = self._build_runtime_key(checkpoint_path)

        # An explicit list takes precedence over the legacy single-reference
        # fields.  Paths remain ordered so v4 can concatenate clips exactly as
        # supplied by the character voice configuration.
        if ref_wavs is not None and not isinstance(ref_wavs, (list, tuple)):
            raise TypeError("ref_wavs must be a list or tuple of paths")
        if ref_latents is not None and not isinstance(ref_latents, (list, tuple)):
            raise TypeError("ref_latents must be a list or tuple of paths")
        requested_ref_wavs = [str(item) for item in (ref_wavs or []) if str(item).strip()]
        requested_ref_latents = [
            str(item) for item in (ref_latents or []) if str(item).strip()
        ]
        if bool(no_ref) and (ref_wav or requested_ref_wavs or ref_latent or requested_ref_latents):
            raise ValueError(
                "no_ref cannot be combined with ref_wav/ref_wavs or ref_latent/ref_latents"
            )

        resolved_ref_wav = None
        resolved_ref_wavs: Optional[list[str]] = None
        if not bool(no_ref):
            if requested_ref_wavs:
                resolved_ref_wavs = [str(self._resolve_path(path)) for path in requested_ref_wavs]
            elif ref_wav or (not ref_latent and not requested_ref_latents):
                # Preserve the v2/v3 compatibility path: when callers provide
                # only a voice/character name, discover ``<name>.wav`` under
                # refs_dir even though ref_wav itself is omitted.  An explicit
                # latent source still wins and must not be made conflicting by
                # this legacy waveform lookup.
                resolved_ref_wav = self._find_reference_wav(
                    ref_wav, voice_name, character_name
                )
        resolved_ref_latent = str(self._resolve_path(ref_latent)) if ref_latent else None
        resolved_ref_latents = (
            [str(self._resolve_path(path)) for path in requested_ref_latents]
            if requested_ref_latents
            else None
        )
        if resolved_ref_wav or resolved_ref_wavs:
            if resolved_ref_latent or resolved_ref_latents:
                raise ValueError(
                    "ref_wav/ref_wavs and ref_latent/ref_latents are mutually exclusive"
                )
        if resolved_ref_latent and resolved_ref_latents:
            raise ValueError("ref_latent and ref_latents are mutually exclusive")
        should_use_no_ref = bool(no_ref)
        if not (
            resolved_ref_wav
            or resolved_ref_wavs
            or resolved_ref_latent
            or resolved_ref_latents
        ):
            should_use_no_ref = True

        SamplingRequest = symbols["SamplingRequest"]
        requested_seconds = kwargs.get("seconds", self.seconds)
        requested_duration_scale = kwargs.get("duration_scale", self.duration_scale)

        # The vendored cache is process-global even when live and preview use
        # different engine instances.  Newer runtimes expose a lease context
        # that keeps model switching/unload serialized with inference.  Keep a
        # compatibility fallback for test doubles and older vendored adapters.
        acquire = symbols.get("acquire_cached_runtime")
        if callable(acquire):
            runtime_scope = acquire(runtime_key)
        else:
            runtime, reloaded = symbols["get_cached_runtime"](runtime_key)
            runtime_scope = nullcontext((runtime, reloaded))

        with runtime_scope as runtime_info:
            runtime, reloaded = runtime_info
            if reloaded:
                print("[Irodori-TTS] Loaded runtime")
            result = runtime.synthesize(
                SamplingRequest(
                    text=str(text),
                    caption=None if caption is None else str(caption),
                    ref_wav=resolved_ref_wav,
                    ref_wavs=resolved_ref_wavs,
                    ref_latent=resolved_ref_latent,
                    ref_latents=resolved_ref_latents,
                    no_ref=should_use_no_ref,
                    num_steps=int(kwargs.get("num_steps", self.num_steps)),
                    t_schedule_mode=str(
                        kwargs.get("t_schedule_mode", self.t_schedule_mode)
                    ),
                    sway_coeff=float(kwargs.get("sway_coeff", self.sway_coeff)),
                    seconds=(
                        None if requested_seconds is None else float(requested_seconds)
                    ),
                    duration_scale=float(requested_duration_scale),
                    max_ref_seconds=kwargs.get("max_ref_seconds", self.max_ref_seconds),
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
                    min_seconds=float(kwargs.get("min_seconds", 0.5)),
                    max_seconds=float(
                        30.0
                        if kwargs.get("max_seconds") is None
                        else kwargs.get("max_seconds", 30.0)
                    ),
                    num_candidates=int(kwargs.get("num_candidates", 1)),
                    decode_mode=str(kwargs.get("decode_mode", "sequential")),
                    cfg_guidance_mode=str(kwargs.get("cfg_guidance_mode", "independent")),
                    cfg_scale=kwargs.get("cfg_scale"),
                    max_text_len=kwargs.get("max_text_len"),
                    max_caption_len=kwargs.get("max_caption_len"),
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
