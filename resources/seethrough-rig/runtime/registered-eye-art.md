# Registered per-key eyelash art (continuous eyes v2)

Version 2 is an explicit opt-in extension to [continuous eye geometry](continuous-eyes.md).
It keeps the exact semantic contour correspondence and `2*N+6` rectangular
annulus, and adds an individually registered texture for every contour key.
Existing recipes without `key_art` still produce version 1 with the original
single-texture path. Legacy recipes without continuous eyes are unchanged.

This is producer/runtime support, not art-quality approval or model adoption.
An isolated candidate must still be reviewed in real WebGL, and Native rendering
must be checked separately. Never overwrite source artwork or adopted output.

## Authoring contract

Add `key_art` to each opted-in `continuous_eyes.L` / `.R` specification:

```json
{
  "key_art": {
    "eyelash-l": {
      "half": {"image": "art/half-l.png", "origin": [100, 200], "density": 2},
      "closed": {"image": "art/closed-l.png", "origin": [100, 200], "density": 2},
      "jitome": {"image": "art/half-l.png", "origin": [100, 200], "density": 2},
      "happy": {"image": "art/happy-l.png", "origin": [100, 200], "density": 2}
    },
    "eyelash-l-overhair": {
      "half": {"transparent": true},
      "closed": {"transparent": true},
      "jitome": {"transparent": true},
      "happy": {"transparent": true}
    }
  }
}
```

This is a format example, not transferable art or coordinates. Mapping names
must cover exactly every moving lash layer on that side, including all active
`lift_over` rings. Each mapping must contain every contour key except `open`,
which may default to the current source layer after restoration/lift partition.
Optional `surprised`, `worried`, and `shy` contours require matching art too.
Unknown layers or keys and implicit non-open fallbacks are errors. If jitome
intentionally reuses half art, provide that same descriptor explicitly and author
matching contours. `{ "transparent": true }` alone explicitly marks an empty
key, such as a closed over-hair lash with no authored stroke above the hair.

An image descriptor requires a single 8-bit PNG, a two-coordinate source-space
`origin` for the raster's top-left edge, and a finite positive pixel `density`
(raster pixels per source pixel). Paths are relative to the recipe file, with
absolute paths also supported. No resampling, cropping, replacement outline,
alpha editing, RGB compositing, or normalization is performed on the decoded
source RGBA. Preserve the full authored patch, including long tails and feather.
Integer transparent padding is added on that raster's own grid. Inputs too large
for the normal atlas are rejected rather than resized.
The PNG IHDR bit depth is checked directly: 16-bit RGB/RGBA is rejected even
when an image decoder presents it as an 8-bit mode, avoiding silent precision loss.

### Separated lash versus complete eye patch

By default every key uses `aperture_mode: "strict"`. If alpha above 0.004 occurs
inside that key's aperture beyond the existing half-raster-pixel boundary
tolerance, the build fails. This prevents accidentally discarding isolated lash
ink. Version 1's coverage check is unchanged.

An explicitly authored full eye patch can instead declare:

```json
{
  "image": "art/happy-full-eye-l.png",
  "origin": [100, 200],
  "density": 2,
  "aperture_mode": "exclude-interior"
}
```

This declares that the contour interior is intentionally excluded by the annular
hole, for example white/iris pixels in a complete eye patch. The PNG remains
unchanged: this is geometry exclusion, not an image crop or alpha mask operation.
The author must trace that key's contour along the intended inner lash edge.
Pixels outside it, including feathered skin, are retained and require visual
review. This declaration applies only to that descriptor. It does not disable
coverage checks for any other key, open source layer, or version 1 model.

## Registration and geometry

For each ring, a common fixed face-local outer rectangle encloses every key's
complete raster rectangle and all target contours, plus `ring_padding`. Every
key texture is padded to cover it without shifting its original pixel grid.
The open contour supplies the neutral inner vertices. All contour keys use the
same ordered X columns for any supported even sample count from 8 through 512.

For each key, the inner UV source positions are that key's own float32 contour;
outer UV source positions are the fixed common outer vertices. At an authored
endpoint, texture source coordinates therefore coincide with evaluated geometry
(up to float32 UV representation). Open drawable atlas/UV fields are exactly the
registered open entry. The existing triangle, rigid-transform, attachment,
closed-mask and contour-correspondence guards remain in force.

The runtime evaluates one shared geometry and blends registered key samples in
premultiplied space. It does not stack separate endpoint drawables. White, iris,
and mask roles are unchanged. Endpoint matching and a valid topology do not
establish that authored contours follow the actual strokes: source endpoint and
intermediate-frame art comparisons are still required.

## Wire contract and compatibility

```text
rig.eyes[side].continuous.version = 2
ring.eyeRig.artKeys = {
  open: { atlas: integer, uvs: encoded-f32[vertexCount*2] },
  half: { atlas: integer, uvs: encoded-f32[vertexCount*2] },
  closed: { atlas: integer, uvs: encoded-f32[vertexCount*2] },
  jitome: { atlas: integer, uvs: encoded-f32[vertexCount*2] },
  happy: { atlas: integer, uvs: encoded-f32[vertexCount*2] },
  ...all other contour keys
}
```

Only version 2 rings may have `artKeys`. Each entry has exactly `atlas` and
`uvs`, references an existing atlas, and contains bounded finite UVs of exactly
the drawable's UV length. Open entry atlas and decoded float32 UVs must equal
the drawable's atlas and UVs. All contour keys are required, with no extras.
A registered ring cannot be referenced by any drawable's `clip`, including an
unrelated drawable: the art-blend shader is not an alpha-cut stencil shader.
Eye clipping uses the dedicated semantic mask-role drawable.

Data-only export determines the exact runtime declaration from the effective
model after settings:

- No continuous eyes: ABI 1, version `0.1.0`, no feature requirements
- Version 1 eyes only: ABI 1, version `0.2.0`, `requires: ["continuous-eyes-v1"]`
- Any version 2 eye, including mixed v1/v2 sides: ABI 1, version `0.3.0`,
  `requires: ["continuous-eyes-v2"]`

The current validator accepts only the matching pair. The published 0.2 validator
rejects a 0.3 package before rendering. Continuous models still cannot target the
historical pinned runtime.

## Producer provenance

A v2 build emits `eye-art-provenance.json` beside `model.json`. It records each
side/ring/key's descriptor, source PNG SHA256 when supplied, origin, density,
raster dimensions, transparent padding, and decoded pixel SHA256. The pixel
hash is of row-major little-endian float32 **straight RGBA**, before padding,
atlas quantization, or transparent-RGB bleed. A source-layer default hashes the
current separated raster; an intentional transparent key is recorded as such.

Keep this receipt with the source project. It is not an extension of the strict
model wire format, and the data-only package exporter does not distribute it.
Source-file and decoded-pixel integrity do not claim byte-identical atlas RGB
under transparent pixels: the existing atlas exporter quantizes and bleeds RGB.

### Export preview

The builder's `rest_composite.png` is a source-layer diagnostic. It does not
evaluate registered open overrides or the annular hole. Version 2 package export
therefore requires an explicit actual model rendering via `--preview`; it cannot
fall back to that source composite. This applies after settings are resolved.
Use a verified render from the canonical runtime. Providing a preview does not
itself establish artistic approval. Version 1 and legacy default previews remain
unchanged.

```sh
python -m pytest -q tests/test_registered_eye_art.py tests/test_continuous_eyes.py
```

## Optional v3 guide/coverage extension

[Guided strokes and bounded coverage](guided-eye-coverage.md) add an optional
moving outer stroke guide and alpha-supported under-lash coverage. They require
continuous version 3 and runtime 0.4.0; existing v2 recipes keep their exact
2*N+6 contract and 0.3.0 package requirement. A transparent front key is not an
automatically safe ownership transfer: inspect intermediate composite alpha and
hair depth order when artwork moves between separate rings.
