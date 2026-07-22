# Vendored Irodori-TTS Runtime

This package contains the Irodori-TTS inference runtime used by AoiTalk.

- Upstream: https://github.com/Aratako/Irodori-TTS
- Base commit: `eaf74d6a19138f743acb5b71a445fd25a57db987`
- Included runtime features: v3 checkpoint metadata, joint text/reference/caption
  conditioning, duration prediction, variable-length sampling, Sway Sampling,
  and SilentCipher watermarking.
- Local integration: AoiTalk supplies the fixed model repository, cache paths,
  and request parameters through `src/tts/engines/irodori_tts_engine.py`.
- License: MIT, see `LICENSE`

AoiTalk imports this package directly and does not load a separate local
`D:/tool/Irodori-TTS` checkout.
