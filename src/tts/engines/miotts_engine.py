"""
Embedded MioTTS engine.

AoiTalk loads the MioTTS language model and MioCodec in this process. No
separate MioTTS-Inference HTTP server is required.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator, Optional


TOKEN_PATTERN = re.compile(r"<\|s_(\d+)\|>")
AUDIO_EXTENSIONS = (".wav", ".flac", ".ogg", ".mp3", ".m4a")


@dataclass(frozen=True)
class MioTTSReference:
    kind: str
    value: str


class MioTTSEngine:
    """MioTTS adapter returning WAV bytes for AoiTalk playback."""

    DEFAULT_MODEL_ID = "Aratako/MioTTS-0.6B"
    DEFAULT_CODEC_MODEL_ID = "Aratako/MioCodec-25Hz-44.1kHz-v2"

    def __init__(
        self,
        model_id: Optional[str] = None,
        codec_model_id: Optional[str] = None,
        refs_dir: Optional[str] = None,
        presets_dir: Optional[str] = None,
        cache_dir: Optional[str] = None,
        device: str = "auto",
        dtype: str = "auto",
        default_preset_id: Optional[str] = None,
        trust_remote_code: bool = False,
        max_text_length: int = 300,
        max_reference_mb: int = 20,
        max_reference_seconds: float = 20.0,
        temperature: float = 0.8,
        top_p: float = 1.0,
        max_tokens: int = 700,
        repetition_penalty: float = 1.0,
        presence_penalty: float = 0.0,
        frequency_penalty: float = 0.0,
        best_of_n_enabled: bool = False,
        best_of_n_n: int = 1,
        runtime: Optional[Any] = None,
    ):
        self.repo_root = Path(__file__).resolve().parents[3]
        self.model_id = model_id or self.DEFAULT_MODEL_ID
        self.codec_model_id = codec_model_id or self.DEFAULT_CODEC_MODEL_ID
        self.refs_dir = self._resolve_path(refs_dir or "config/miotts_refs")
        self.presets_dir = self._resolve_path(presets_dir or "config/miotts_presets")
        self.cache_dir = self._resolve_path(cache_dir or "cache/miotts")
        self.device = str(device or "auto")
        self.dtype = str(dtype or "auto")
        self.default_preset_id = default_preset_id or None
        self.trust_remote_code = bool(trust_remote_code)
        self.max_text_length = int(max_text_length or 300)
        self.max_reference_mb = int(max_reference_mb or 20)
        self.max_reference_seconds = float(max_reference_seconds or 20.0)
        self.temperature = float(temperature)
        self.top_p = float(top_p)
        self.max_tokens = int(max_tokens or 700)
        self.repetition_penalty = float(repetition_penalty)
        self.presence_penalty = float(presence_penalty)
        self.frequency_penalty = float(frequency_penalty)
        self.best_of_n_enabled = bool(best_of_n_enabled)
        self.best_of_n_n = int(best_of_n_n or 1)
        self.presets: list[str] = []

        self._runtime = runtime
        self._runtime_lock = asyncio.Lock()

    def _resolve_path(self, raw: str | Path) -> Path:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = self.repo_root / path
        return path

    async def initialize(self) -> bool:
        """Prepare directories and validate dependencies without loading weights."""
        try:
            self.refs_dir.mkdir(parents=True, exist_ok=True)
            self.presets_dir.mkdir(parents=True, exist_ok=True)
            self.cache_dir.mkdir(parents=True, exist_ok=True)

            if self._runtime is None:
                missing = self._missing_dependencies()
                if missing:
                    joined = ", ".join(missing)
                    print(
                        "[MioTTS] Missing dependencies: "
                        f"{joined}. Install with: pip install -e \".[audio,miotts]\""
                    )
                    return False

            self.presets = self._list_presets()
            print("[MioTTS] Engine initialized; model loads on first synthesis")
            return True
        except Exception as exc:
            print(f"[MioTTS] Initialization error: {type(exc).__name__}: {exc}")
            return False

    @staticmethod
    def _missing_dependencies() -> list[str]:
        required = ("torch", "transformers", "miocodec", "soundfile")
        return [name for name in required if importlib.util.find_spec(name) is None]

    def _list_presets(self) -> list[str]:
        if not self.presets_dir.exists():
            return []
        return sorted(
            {
                path.stem
                for path in self.presets_dir.iterdir()
                if path.is_file() and path.suffix.lower() in {".pt", ".npz"}
            }
        )

    async def _ensure_runtime(self, load: bool = True) -> Any:
        async with self._runtime_lock:
            if self._runtime is None:
                runtime = _EmbeddedMioTTSRuntime(
                    model_id=self.model_id,
                    codec_model_id=self.codec_model_id,
                    presets_dir=self.presets_dir,
                    cache_dir=self.cache_dir,
                    device=self.device,
                    dtype=self.dtype,
                    trust_remote_code=self.trust_remote_code,
                    max_reference_mb=self.max_reference_mb,
                    max_reference_seconds=self.max_reference_seconds,
                )
                self._runtime = runtime
            if load and hasattr(self._runtime, "load"):
                await asyncio.to_thread(self._runtime.load)
            return self._runtime

    async def synthesize(self, text: str, **kwargs) -> Optional[bytes]:
        """Synthesize text with an embedded MioTTS runtime and return WAV bytes."""
        if not text or not text.strip():
            return None
        if len(text) > self.max_text_length:
            print(
                f"[MioTTS] Text is too long: {len(text)} chars "
                f"(max {self.max_text_length})"
            )
            return None

        reference = self._resolve_reference(kwargs)
        if reference is None:
            print(
                "[MioTTS] No reference audio or preset configured. "
                f"Put wav files in {self.refs_dir} or presets in {self.presets_dir}."
            )
            return None

        params = self._build_generation_params(kwargs)
        try:
            runtime = await self._ensure_runtime()
            audio = await asyncio.to_thread(
                runtime.synthesize,
                str(text),
                reference,
                params,
            )
            return audio
        except Exception as exc:
            print(f"[MioTTS] Synthesis error: {type(exc).__name__}: {exc}")
            return None

    async def generate_preset(self, audio_path: str, preset_id: str) -> Optional[Path]:
        """Create a reusable MioCodec preset from a reference audio file."""
        if not preset_id or not preset_id.strip():
            print("[MioTTS] preset_id is required")
            return None
        source = self._resolve_path(audio_path)
        if not source.exists():
            print(f"[MioTTS] Reference audio not found: {source}")
            return None
        output_path = self.presets_dir / f"{_sanitize_preset_id(preset_id)}.pt"
        try:
            runtime = await self._ensure_runtime(load=False)
            await asyncio.to_thread(runtime.generate_preset, source, output_path)
            self.presets = self._list_presets()
            return output_path
        except Exception as exc:
            print(f"[MioTTS] Preset generation error: {type(exc).__name__}: {exc}")
            return None

    def _resolve_reference(self, params: dict[str, Any]) -> Optional[MioTTSReference]:
        reference_data = (
            params.get("reference_data")
            or params.get("reference_base64")
            or params.get("reference_audio_base64")
        )
        if reference_data:
            return MioTTSReference(kind="base64", value=str(reference_data))

        reference_audio_path = params.get("reference_audio_path") or params.get("ref_wav")
        if reference_audio_path:
            path = self._resolve_path(reference_audio_path)
            if path.exists() and path.is_file():
                return MioTTSReference(kind="path", value=str(path))
            print(f"[MioTTS] Reference audio not found: {path}")
            return None

        explicit_preset = (
            params.get("preset_id")
            or params.get("voice_id")
            or self.default_preset_id
        )
        if explicit_preset and self._preset_exists(str(explicit_preset)):
            return MioTTSReference(kind="preset", value=str(explicit_preset))

        for candidate in (
            params.get("voice_name"),
            params.get("character_name"),
            params.get("voice_id"),
            explicit_preset,
        ):
            path = self._find_reference_audio(candidate)
            if path is not None:
                return MioTTSReference(kind="path", value=str(path))

        return None

    def _find_reference_audio(self, raw: Any) -> Optional[Path]:
        if not raw:
            return None
        name = str(raw).strip()
        if not name:
            return None

        direct = self._resolve_path(name)
        if direct.exists() and direct.is_file():
            return direct

        path_name = Path(name)
        candidates = [self.refs_dir / path_name.name]
        if not path_name.suffix:
            candidates.extend(self.refs_dir / f"{name}{ext}" for ext in AUDIO_EXTENSIONS)
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    def _preset_exists(self, preset_id: str) -> bool:
        try:
            normalized = _sanitize_preset_id(preset_id)
        except ValueError as exc:
            print(f"[MioTTS] Invalid preset id: {exc}")
            return False
        base = self.presets_dir.resolve()
        return any((base / f"{normalized}{ext}").exists() for ext in (".pt", ".npz"))

    def _build_generation_params(self, params: dict[str, Any]) -> dict[str, Any]:
        max_new_tokens = params.get("max_new_tokens", params.get("max_tokens", self.max_tokens))
        temperature = float(params.get("temperature", self.temperature))
        top_p = float(params.get("top_p", self.top_p))
        repetition_penalty = float(
            params.get("repetition_penalty", self.repetition_penalty)
        )
        presence_penalty = float(params.get("presence_penalty", self.presence_penalty))
        frequency_penalty = float(params.get("frequency_penalty", self.frequency_penalty))

        best_of_n_n = int(params.get("best_of_n_n", self.best_of_n_n) or 1)
        best_of_n_enabled = params.get("best_of_n_enabled", self.best_of_n_enabled)
        if "best_of_n_enabled" not in params and best_of_n_n > 1:
            best_of_n_enabled = True
        num_candidates = best_of_n_n if bool(best_of_n_enabled) and best_of_n_n > 1 else 1

        return {
            "temperature": temperature,
            "top_p": top_p,
            "max_new_tokens": int(max_new_tokens or self.max_tokens),
            "repetition_penalty": repetition_penalty,
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
            "num_candidates": max(1, num_candidates),
        }

    async def cleanup(self):
        runtime = self._runtime
        self._runtime = None
        if runtime is not None and hasattr(runtime, "cleanup"):
            try:
                result = runtime.cleanup()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                print(f"[MioTTS] Cleanup error: {exc}")


class _EmbeddedMioTTSRuntime:
    def __init__(
        self,
        model_id: str,
        codec_model_id: str,
        presets_dir: Path,
        cache_dir: Path,
        device: str,
        dtype: str,
        trust_remote_code: bool,
        max_reference_mb: int,
        max_reference_seconds: float,
    ):
        self.model_id = model_id
        self.codec_model_id = codec_model_id
        self.presets_dir = presets_dir
        self.cache_dir = cache_dir
        self.requested_device = device
        self.requested_dtype = dtype
        self.trust_remote_code = trust_remote_code
        self.max_reference_mb = max_reference_mb
        self.max_reference_seconds = max_reference_seconds

        self._loaded = False
        self._torch = None
        self._tokenizer = None
        self._model = None
        self._codec = None
        self._load_audio = None

    def load(self) -> None:
        if self._loaded:
            return

        hf_cache = self._prepare_cache()

        import torch
        import transformers
        from transformers import AutoTokenizer

        device = self._resolve_device(torch)
        dtype = self._resolve_dtype(torch, device)

        if self._tokenizer is None or self._model is None:
            print(f"[MioTTS] Loading model: {self.model_id} ({device}, {self.requested_dtype})")
            tokenizer = AutoTokenizer.from_pretrained(
                self.model_id,
                cache_dir=str(hf_cache),
                trust_remote_code=self.trust_remote_code,
            )
            if tokenizer.pad_token_id is None and tokenizer.eos_token_id is not None:
                tokenizer.pad_token = tokenizer.eos_token

            model = self._load_model(transformers, hf_cache, dtype)
            self._tokenizer = tokenizer
            self._model = model.eval().to(device)

        self._torch = torch
        self._load_codec_only()
        self._loaded = True

    def _prepare_cache(self) -> Path:
        hf_home = self.cache_dir / "hf_home"
        hf_cache = self.cache_dir / "hf"
        hf_home.mkdir(parents=True, exist_ok=True)
        hf_cache.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HOME", str(hf_home))
        os.environ.setdefault("HF_HUB_CACHE", str(hf_cache))
        return hf_cache

    def _load_codec_only(self) -> None:
        if self._codec is not None and self._load_audio is not None and self._torch is not None:
            return

        self._prepare_cache()
        import torch
        from miocodec import MioCodecModel
        from miocodec.util import load_audio

        device = self._resolve_device(torch)
        print(f"[MioTTS] Loading codec: {self.codec_model_id}")
        codec = MioCodecModel.from_pretrained(self.codec_model_id)

        self._torch = torch
        self._codec = codec.eval().to(device)
        self._load_audio = load_audio

    def _load_model(self, transformers: Any, hf_cache: Path, dtype: Any) -> Any:
        candidates = []
        causal_cls = getattr(transformers, "AutoModelForCausalLM", None)
        multimodal_cls = getattr(transformers, "AutoModelForMultimodalLM", None)
        if causal_cls is not None:
            candidates.append(causal_cls)
        if multimodal_cls is not None and multimodal_cls not in candidates:
            candidates.append(multimodal_cls)

        last_exc: Exception | None = None
        for model_cls in candidates:
            try:
                return model_cls.from_pretrained(
                    self.model_id,
                    cache_dir=str(hf_cache),
                    dtype=dtype,
                    low_cpu_mem_usage=True,
                    trust_remote_code=self.trust_remote_code,
                )
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No compatible Transformers auto model class is available.")

    def _resolve_device(self, torch: Any) -> str:
        requested = str(self.requested_device or "auto").lower()
        if requested == "auto":
            return "cuda" if torch.cuda.is_available() else "cpu"
        if requested == "cuda" and not torch.cuda.is_available():
            return "cpu"
        return requested

    def _resolve_dtype(self, torch: Any, device: str) -> Any:
        requested = str(self.requested_dtype or "auto").lower()
        if device == "cpu":
            return torch.float32
        if requested == "auto":
            if torch.cuda.is_available() and torch.cuda.is_bf16_supported():
                return torch.bfloat16
            return torch.float16
        if requested in {"bf16", "bfloat16"}:
            return torch.bfloat16
        if requested in {"fp16", "float16"}:
            return torch.float16
        if requested in {"fp32", "float32"}:
            return torch.float32
        return torch.float32

    @property
    def sample_rate(self) -> int:
        self._load_codec_only()
        return int(self._codec.config.sample_rate)

    def synthesize(
        self,
        text: str,
        reference: MioTTSReference,
        generation_params: dict[str, Any],
    ) -> bytes:
        self.load()
        normalized = _normalize_text(text)
        token_candidates = self._generate_token_candidates(normalized, generation_params)
        if not token_candidates:
            raise ValueError("No speech tokens found in generated text.")

        reference_waveform = None
        global_embedding = None
        if reference.kind == "preset":
            global_embedding = self._load_preset_embedding(reference.value)
        elif reference.kind == "path":
            reference_waveform = self._load_reference_audio_path(Path(reference.value))
        elif reference.kind == "base64":
            reference_waveform = self._load_reference_audio_base64(reference.value)
        else:
            raise ValueError(f"Unsupported reference kind: {reference.kind}")

        audio = self._decode_audio(token_candidates[0], reference_waveform, global_embedding)
        return self._write_wav_bytes(audio, self.sample_rate)

    def generate_preset(self, audio_path: Path, output_path: Path) -> None:
        self._load_codec_only()
        waveform = self._load_reference_audio_path(audio_path)
        torch = self._torch
        device = self._codec_device()
        with torch.inference_mode():
            features = self._codec.encode(
                waveform.to(device=device, dtype=torch.float32),
                return_content=False,
                return_global=True,
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {"global_embedding": features.global_embedding.squeeze().detach().cpu()},
            output_path,
        )

    def _generate_token_candidates(
        self,
        normalized_text: str,
        generation_params: dict[str, Any],
    ) -> list[list[int]]:
        torch = self._torch
        encoded = self._encode_prompt(normalized_text)
        input_ids = encoded["input_ids"].to(self._model.device)
        attention_mask = encoded.get("attention_mask")
        if attention_mask is not None:
            attention_mask = attention_mask.to(self._model.device)

        temperature = float(generation_params.get("temperature", 0.8))
        num_candidates = int(generation_params.get("num_candidates", 1))
        do_sample = temperature > 0 or num_candidates > 1

        generate_kwargs = {
            "max_new_tokens": int(generation_params.get("max_new_tokens", 700)),
            "repetition_penalty": float(generation_params.get("repetition_penalty", 1.0)),
            "do_sample": do_sample,
            "pad_token_id": self._tokenizer.pad_token_id or self._tokenizer.eos_token_id,
            "eos_token_id": self._tokenizer.eos_token_id,
        }
        if do_sample:
            generate_kwargs["temperature"] = max(temperature, 1e-5)
            generate_kwargs["top_p"] = float(generation_params.get("top_p", 1.0))
            generate_kwargs["num_return_sequences"] = max(1, num_candidates)

        with torch.inference_mode():
            generated = self._model.generate(
                input_ids=input_ids,
                attention_mask=attention_mask,
                **generate_kwargs,
            )

        prompt_len = input_ids.shape[-1]
        candidates: list[list[int]] = []
        for sequence in generated:
            decoded = self._tokenizer.decode(
                sequence[prompt_len:],
                skip_special_tokens=False,
            )
            try:
                candidates.append(_parse_speech_tokens(decoded))
                continue
            except ValueError:
                pass
            full_decoded = self._tokenizer.decode(sequence, skip_special_tokens=False)
            try:
                candidates.append(_parse_speech_tokens(full_decoded))
            except ValueError:
                continue
        return candidates

    def _encode_prompt(self, text: str) -> dict[str, Any]:
        messages = [{"role": "user", "content": text}]
        chat_template = getattr(self._tokenizer, "chat_template", None)
        if chat_template:
            try:
                encoded = self._tokenizer.apply_chat_template(
                    messages,
                    add_generation_prompt=True,
                    tokenize=True,
                    return_tensors="pt",
                    return_dict=True,
                )
                if isinstance(encoded, dict):
                    return encoded
            except TypeError:
                encoded = self._tokenizer.apply_chat_template(
                    messages,
                    add_generation_prompt=True,
                    tokenize=True,
                    return_tensors="pt",
                )
                return {"input_ids": encoded}
        return self._tokenizer(text, return_tensors="pt")

    def _load_reference_audio_path(self, path: Path) -> Any:
        waveform = self._load_audio(str(path), sample_rate=self.sample_rate)
        return self._trim_reference(waveform)

    def _load_reference_audio_base64(self, data: str) -> Any:
        payload = data.split("base64,", 1)[1] if "base64," in data else data
        payload = "".join(payload.split())
        max_bytes = self.max_reference_mb * 1024 * 1024
        estimated_size = max(0, (len(payload) * 3) // 4 - len(payload.rstrip("=")))
        if estimated_size > max_bytes:
            raise ValueError(f"Reference audio too large (max {self.max_reference_mb} MB).")
        raw = base64.b64decode(payload, validate=True)
        if len(raw) > max_bytes:
            raise ValueError(f"Reference audio too large (max {self.max_reference_mb} MB).")
        with _temp_audio_file(raw) as path:
            return self._load_reference_audio_path(path)

    def _trim_reference(self, waveform: Any) -> Any:
        if self.max_reference_seconds <= 0:
            return waveform
        max_samples = int(self.sample_rate * self.max_reference_seconds)
        if waveform.numel() > max_samples:
            return waveform[:max_samples]
        return waveform

    def _load_preset_embedding(self, preset_id: str) -> Any:
        torch = self._torch
        normalized = _sanitize_preset_id(preset_id)
        base = self.presets_dir.resolve()
        for path in (base / f"{normalized}.pt", base / f"{normalized}.npz"):
            if not path.exists():
                continue
            if path.suffix.lower() == ".pt":
                value = torch.load(path, map_location="cpu", weights_only=True)
            else:
                import numpy as np

                data = np.load(path)
                if "global_embedding" in data:
                    value = data["global_embedding"]
                elif "embedding" in data:
                    value = data["embedding"]
                else:
                    value = data[list(data.keys())[0]]
            return self._prepare_embedding(value, self._codec_device())
        raise FileNotFoundError(f"Preset '{preset_id}' not found in {base}.")

    def _decode_audio(
        self,
        tokens: list[int],
        reference_waveform: Any,
        global_embedding: Any,
    ) -> Any:
        torch = self._torch
        device = self._codec_device()
        with torch.inference_mode():
            if reference_waveform is not None:
                features = self._codec.encode(
                    reference_waveform.to(device=device, dtype=torch.float32),
                    return_content=False,
                    return_global=True,
                )
                global_embedding = features.global_embedding
            if global_embedding is None:
                raise ValueError("Either reference audio or preset embedding is required.")
            global_embedding = self._prepare_embedding(global_embedding, device)
            content_tokens = torch.tensor(tokens, dtype=torch.long, device=device)
            return self._codec.decode(
                global_embedding=global_embedding,
                content_token_indices=content_tokens,
            )

    def _prepare_embedding(self, embedding: Any, device: Any) -> Any:
        torch = self._torch
        if isinstance(embedding, dict):
            embedding = embedding.get("global_embedding", embedding.get("embedding", embedding))
        try:
            import numpy as np

            if isinstance(embedding, np.ndarray):
                embedding = torch.from_numpy(embedding)
        except Exception:
            pass
        if not isinstance(embedding, torch.Tensor):
            embedding = torch.tensor(embedding)
        embedding = embedding.squeeze()
        if embedding.dim() != 1:
            embedding = embedding.flatten()
        return embedding.to(device)

    def _codec_device(self) -> Any:
        return next(self._codec.parameters()).device

    def _write_wav_bytes(self, audio: Any, sample_rate: int) -> bytes:
        import soundfile as sf

        if audio.dim() == 2 and audio.shape[0] == 1:
            audio = audio.squeeze(0)
        elif audio.dim() != 1:
            audio = audio.flatten()
        if audio.dtype not in (self._torch.float32, self._torch.float64):
            audio = audio.float()
        buffer = io.BytesIO()
        sf.write(buffer, audio.detach().cpu().numpy(), int(sample_rate), format="WAV")
        return buffer.getvalue()

    def cleanup(self) -> None:
        try:
            self._tokenizer = None
            self._model = None
            self._codec = None
            if self._torch is not None and self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()
        finally:
            self._loaded = False


def _parse_speech_tokens(text: str) -> list[int]:
    tokens = [int(value) for value in TOKEN_PATTERN.findall(text)]
    if not tokens:
        raise ValueError("No speech tokens found in LLM output.")
    return tokens


def _sanitize_preset_id(preset_id: str) -> str:
    normalized = str(preset_id).strip()
    if not normalized:
        raise ValueError("empty preset id")
    if normalized in {".", ".."}:
        raise ValueError("invalid preset id")
    if any(sep in normalized for sep in ("/", "\\", "\x00")):
        raise ValueError("preset id must not contain path separators")
    return normalized


@contextmanager
def _temp_audio_file(data: bytes) -> Generator[Path, None, None]:
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(data)
            tmp.flush()
            path = Path(tmp.name)
        yield path
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def _normalize_text(text: str) -> str:
    replacements = {
        r"\t": "",
        r"\[n\]": "",
        r" ": "",
        r"　": "",
        r"[;▼♀♂《》≪≫①②③④⑤⑥]": "",
        r"[\u02d7\u2010-\u2015\u2043\u2212\u23af\u23e4\u2500\u2501\u2e3a\u2e3b]": "",
        r"[\uff5e\u301C]": "ー",
        r"？": "?",
        r"！": "!",
        r"[●◯〇]": "○",
        r"♥": "♡",
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text)

    text = text.translate(
        str.maketrans(
            {
                chr(full): chr(half)
                for full, half in zip(
                    list(range(0xFF21, 0xFF3B)) + list(range(0xFF41, 0xFF5B)),
                    list(range(0x41, 0x5B)) + list(range(0x61, 0x7B)),
                    strict=True,
                )
            }
        )
    )
    text = text.translate(
        str.maketrans(
            {chr(full): chr(half) for full, half in zip(range(0xFF10, 0xFF1A), range(0x30, 0x3A), strict=True)}
        )
    )
    text = re.sub(r"…{3,}", "……", text)
    for opener, closer in (("「", "」"), ("『", "』"), ("（", "）"), ("【", "】"), ("(", ")")):
        if text.startswith(opener) and text.endswith(closer):
            text = text[1:-1]
    return text.rstrip("。、")
