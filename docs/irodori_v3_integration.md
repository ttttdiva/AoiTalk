# Irodori v3 integration memo

Updated: 2026-05-15

## Conclusion

Do not embed Irodori v3 inference directly in AoiTalk yet. Treat
`Aratako/Irodori-TTS-Server` as the first integration target because it exposes
an OpenAI-compatible `POST /v1/audio/speech` endpoint and targets the
`Aratako/Irodori-TTS-500M-v3` base model.

Recommended provider shape:

- provider: `irodori_v3`
- base URL: `http://localhost:8088/v1`
- model: `irodori-tts`
- voice: reference file stem, for example `sample`
- response_format: `wav`
- auth: optional bearer token

## Source findings

- `Aratako/Irodori-TTS-500M-v3` is a Japanese TTS model based on RF-DiT with
  zero-shot voice cloning, emoji-based style control, and a 500M parameter
  model card.
- v3 changed from fixed-length to variable-length training, added a duration
  predictor, expanded training data, and integrates SilentCipher for invisible
  audio watermarking.
- The v3 model card explicitly lists limitations: Japanese-only input, variable
  emoji-control reliability, voice/style quality variance, and weaker kanji
  reading accuracy than comparable models.
- `Aratako/Irodori-TTS-Server` is an OpenAI Text-to-Speech compatible server,
  supports reference voices from files / `voices.json` / upload, supports
  `wav`, `mp3`, `flac`, `opus`, `aac`, `pcm`, and does not stream internally.

References:

- https://huggingface.co/Aratako/Irodori-TTS-500M-v3
- https://github.com/Aratako/Irodori-TTS
- https://github.com/Aratako/Irodori-TTS-Server

## Watermark handling

The model card describes SilentCipher as an invisible watermark. This is
acceptable for AoiTalk if generated samples do not contain audible phrases such
as "sample voice" or similar.

Required before enabling by default:

1. Start the local server.
2. Generate at least three samples with `voice=sample` and `response_format=wav`.
3. Listen to the result and confirm there is no audible injected disclaimer.
4. Keep SilentCipher as allowed metadata/watermark; do not strip it.

If an audible disclaimer is present, keep the provider disabled and leave this
document as the investigation result.

## Runtime notes

Server startup path:

```bash
git clone https://github.com/Aratako/Irodori-TTS-Server
cd Irodori-TTS-Server
cp .env.example .env
docker compose -f compose.yaml -f compose.gpu.yaml up --build --force-recreate
```

Smoke request:

```bash
curl http://localhost:8088/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"irodori-tts","input":"こんにちは。AoiTalkのテストです。","voice":"sample","response_format":"wav"}' \
  --output speech.wav
```

## AoiTalk change range

1. Add an OpenAI-compatible custom TTS endpoint setting if the existing TTS
   settings do not already allow arbitrary `base_url`, `model`, `voice`, and
   `response_format`.
2. Add a preset named `irodori_v3`.
3. Keep advanced controls such as `seed`, `num_steps`, `cfg_scale`, and LoRA
   adapter selection out of the first implementation unless the server endpoint
   contract is stable in AoiTalk settings.
4. Save generated audio through the existing voice/TTS output path.

## Next execution unit

Implement a custom OpenAI-compatible TTS provider preset, then run the local
server smoke test and audible-watermark check before enabling it in UI.
