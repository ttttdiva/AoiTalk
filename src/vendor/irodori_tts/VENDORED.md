# Vendored Irodori-TTS Runtime

This package contains the Irodori-TTS inference runtime used by AoiTalk.

- Upstream: https://github.com/Aratako/Irodori-TTS
- Base commit: `89f9d8fbd4d51ea019867ee1197725ede1df13c5`
- Included runtime features: unified v4/v4.1 text/reference/caption conditioning,
  Irodori-TTS-v4-Large T5Gemma2 conditioning, `flow_parameterization`
  (`rf_velocity` Rectified Flow and `meanflow` MeanFlow), ordered
  multi-reference waveforms/latents, checkpoint metadata (including reference
  limits), duration prediction, variable-length sampling, Sway/linear
  schedules, quantized checkpoints, LoRA, Speaker Inversion, and SilentCipher
  watermarking.  The upstream runtime remains backward-compatible with released
  v2/v3 checkpoints.
- AoiTalk compatibility patches (kept separate from the upstream API):
  `tokenizer.py` preserves the local bundled-tokenizer fallback used by older
  v4 snapshots. Runtime acquisition is serialized (`_RUNTIME_EXECUTION_LOCK` /
  `acquire_cached_runtime`) so one concurrent request cannot unload another
  request's model. Optional torchaudio imports retain a soundfile fallback when
  that backend is unavailable.
- Hugging Face cache: standard environment variables (`HF_HOME`,
  `HF_HUB_CACHE`, and related Hugging Face defaults). AoiTalk call sites do not
  pass `cache_dir`. Vendored loaders still accept an optional `cache_dir` for
  upstream compatibility; it stays unused in the AoiTalk path.
- Local integration: AoiTalk supplies the configured model repository (v4.1
  Small by default, with explicit v4 Large / v4.1 Anime / v3 / local
  checkpoints preserved) and request parameters through
  `src/tts/engines/irodori_tts_engine.py`. MeanFlow checkpoints receive the
  runtime default NFE (4) unless the caller sets `num_steps` to a value other
  than the historical RF default of 40.
- License: MIT, see `LICENSE`

AoiTalk imports this package directly and does not load a separate local
`D:/tool/Irodori-TTS` checkout.
