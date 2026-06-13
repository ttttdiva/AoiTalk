# Irodori Character Voice integration memo

Updated: 2026-05-15

## Conclusion

Treat image-to-voice as a separate experimental feature from Irodori v3. The
current public Character Voice project is based on Irodori-TTS v2 variants, not
v3, and it conditions speech on character images through image-derived features.

Use a feature flag and local-service-first design. Do not send character images
to an external API unless the user explicitly configures that endpoint.

## Source findings

- `p1atdev/Irodori-Character-Voice` generates speech conditioned on character
  images, unlike standard Irodori-TTS reference-audio or VoiceDesign caption
  conditioning.
- The public variants are `v2-Tagger` and `v2-SigLIP`, both based on
  `Irodori-TTS-500M-v2`.
- The project provides a Gradio app and CLI. The Gradio app runs on port `7862`
  in the documented example, and CLI inference accepts `--character-image`.
- Generated wav files are saved under `gradio_outputs_character/` in the demo.

References:

- https://character-voice-control.p1atdev.workers.dev/ja/
- https://github.com/p1atdev/Irodori-Character-Voice
- https://huggingface.co/p1atdev/Irodori-TTS-500M-v2-Character-Voice-Tagger
- https://huggingface.co/p1atdev/Irodori-TTS-500M-v2-Character-Voice-SigLIP

## Proposed AoiTalk flow

Feature flag: `experimental_character_voice`.

UI placement:

- Character settings or voice preset settings.
- Label: `画像から声候補を生成`.
- Inputs: character image, preview text, model variant (`Tagger` or `SigLIP`).
- Optional later inputs: seed, steps, guidance settings.

Backend/provider shape:

- provider: `irodori_character_voice`
- execution mode: local service or CLI wrapper
- default checkpoint: `p1atdev/Irodori-TTS-500M-v2-Character-Voice-Tagger`
- optional checkpoint: `p1atdev/Irodori-TTS-500M-v2-Character-Voice-SigLIP`

Persisted result:

- generated wav path
- source image file id/path
- model variant
- checkpoint
- seed when available
- preview text
- created_at

Save the result as a reusable voice preset candidate, not as a task attachment.

## Failure handling

Generation failures should call the automatic failure recorder with:

- source: `backend`
- operation: `irodori_character_voice_generate`
- project_id when available
- input_summary containing model variant, checkpoint, image file name, and
  preview text length only

## Next execution unit

Add a disabled experimental provider skeleton and settings UI copy, then wire it
to a local service/CLI only after a reproducible local smoke test exists.
