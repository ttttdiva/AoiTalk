# Vendored Irodori-TTS Runtime

This package contains the Irodori-TTS inference runtime used by AoiTalk.

- Upstream: https://github.com/Aratako/Irodori-TTS
- Base commit: `2708d3c` (`Add VoiceDesign caption-conditioned model support`)
- Local patch: Sway Sampling support from upstream PR #10 (`bd49f98`)
- License: MIT, see `LICENSE`

AoiTalk imports this package directly and does not load a separate local
`D:/tool/Irodori-TTS` checkout.
