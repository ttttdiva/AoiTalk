# Vendored Irodori-TTS Runtime

This package contains the Irodori-TTS inference runtime used by AoiTalk.

- Upstream: https://github.com/Aratako/Irodori-TTS
- Base commit: `8224dafb46d0aba89209a8f905f1cb7e3299d9c1`
- Included runtime features: unified v4.1 text/reference/caption conditioning,
  ordered multi-reference waveforms/latents, checkpoint metadata (including the
  v4 120-second reference limit), duration prediction, variable-length sampling,
  Sway/linear schedules, quantized checkpoints, LoRA, Speaker Inversion, and
  SilentCipher watermarking.  The upstream runtime remains backward-compatible
  with released v2/v3 checkpoints.  A small ModernBERT rotary-configuration
  shim maps the v5 checkpoint metadata to transformers 4.57's equivalent field
  names so AoiTalk can coexist with MioTTS/yomi installations that still pin
  transformers below 5.
- AoiTalk compatibility patches (kept separate from the upstream API):
  `tokenizer.py` detects a bundled v4 tokenizer whose config advertises the
  transformers-5-only `TokenizersBackend` class and loads its local
  `tokenizer.json` through `PreTrainedTokenizerFast` on transformers 4.x.  The
  cached snapshot is never rewritten, and remote/v3 tokenizer loading remains
  on the normal `AutoTokenizer` path.  Vendored loaders accept an optional
  `cache_dir` for upstream compatibility, but AoiTalk's normal integration path
  leaves it unset so Hugging Face standard cache resolution applies.  Optional torchaudio/soundfile codec
  imports have lightweight fallbacks for installations where those backends
  are unavailable; normal upstream backends are preferred when installed.
- Local integration: AoiTalk supplies the configured model repository (v4.1 by
  default, with explicit v3/local checkpoints preserved) and request parameters
  through `src/tts/engines/irodori_tts_engine.py`.
- License: MIT, see `LICENSE`

AoiTalk imports this package directly and does not load a separate local
`D:/tool/Irodori-TTS` checkout.
