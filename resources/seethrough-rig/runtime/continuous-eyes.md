# Opt-in continuous eyelids (current runtime)

This is an opt-in builder/runtime extension. Existing recipes, adopted models,
expression patches, and the pinned historical comparison core remain unchanged.
Do not point an experimental build at an adopted model directory. A successful
build or geometry test is not an art-quality approval, WebGL review, or Native
rendering test. Conversion of additional characters needs their own art review.

## Recipe

A recipe with separate `eyewhite-l`, `irides-l`, `eyelash-l` layers can enable one
side without changing the other. Use `rig.head.mode: "rigid"` and
`rig.body.rigid: true`. Keyforms, per-eye strands and other local deformers are
not supported together with this version. An iris gaze deformer is supported.

```json
{
  "continuous_eyes": {
    "L": {
      "samples": 48,
      "half_open": 0.5,
      "brow_jitome_drop": 2,
      "ring_padding": 2,
      "contours": {
        "open": {
          "upper": [[100,100],[105,95],[110,93],[115,95],[120,100]],
          "lower": [[100,100],[105,105],[110,107],[115,105],[120,100]]
        },
        "closed": {
          "upper": [[100,100],[105,101],[110,102],[115,101],[120,100]],
          "lower": [[100,100],[105,101],[110,102],[115,101],[120,100]]
        }
      }
    }
  }
}
```

This example illustrates the format, not transferable coordinates or reviewed
art. Coordinates are source-image pixels, with positive Y downward. Both
`upper` and `lower` run left to right and contain their shared corner points.
Each authored pair may have its own sampling density; the builder resamples
piecewise-linearly at the same fixed X columns for every key. All keys must have
the same endpoint X coordinates. It rejects crossing input even before
resampling. For a neutral contour authored with nonuniform columns, choose
sufficient samples to retain its features: output X columns are uniform.

A contour can alternatively be a canonical even-length `[x,y]` loop: upper left
corner through upper right corner at index N/2, followed by lower-right-to-left
interior points. The corners are stored once. `samples` is the output contour
count, an even integer from 8 to 512 (48 by default), not a hardcoded runtime
count. `half_open` is inside 0..1. Padding is in source pixels and at least 0.25.
Setting a side to `false`/`null` disables an inherited opt-in side specification.

### Authored keys and explicit fallback labels

The output always has `open`, `half`, `closed`, `jitome`, and `happy`. Authoring
all five from reviewed eye shapes is preferred. `surprised`, `worried`, and `shy`
are optional only when the recipe does not use those eye expressions. An opted-in
side with active legacy eye-expression opacity gates or nonzero presets must
provide the matching contours. Partial conversion is rejected with an actionable
error: retaining the old open expression patch while removing its shared
half/closed patches would make that eye disappear during a blink.

Missing shapes are mechanical fallbacks, recorded in
`rig.eyes[side].continuous.sources` and the build log:

- `open`: thresholded white alpha sampled by column (`derived:white-alpha`)
- `closed`: the open upper/lower midline (`derived:open-midline`)
- `half`: closed-to-open interpolation at `half_open`
- `jitome`: a copy of half (`derived:half-placeholder`)
- `happy`: a copy of closed (`derived:closed-placeholder`)

A copied half/closed key is not an authored jitome/happy expression. The builder
does not invent an artist-authored smiling or half-lidded contour from a raster.
A white aperture often extends underneath the eyelashes, so the white-only
fallback can correctly fail the lash-art coverage check. Trace the aperture
along the lash's inner edge instead. The first regular-art Akane pilot exposed
this overlap; automatic white tracing alone must not be treated as adopted art.

## Geometry and runtime contract

```text
rig.eyes[side].continuous = {
  version: 1,
  contours: {open, half, closed, jitome, happy, ...},
  halfOpen: 0.5,
  browJitomeDrop: sourcePixels,
  sources: {key: "authored" | "derived:..."}
}
drawable.eyeRig = {
  side: "L" | "R",
  role: "ring" | "mask" | "iris" | "white",
  indices: [controlIndexPerVertex],
  weights: [0..1PerVertex]
}
```

Only ring and mask have `indices`/`weights`. The ring's inner vertices match the
neutral shared contour, with weight 1. Its outer top/bottom rows and rectangular
end caps stay fixed in face-local coordinates, with weight 0. Version 1 requires
the builder's exact `2*N+6` rectangular-annulus topology, semantic inner vertices,
fixed rows/caps and full triangle set, without an additional ring clipping mask; fractional weights or intermediate rings
are not part of this version. This rectangular
annulus is deliberate: radial rings can fold near the eye corners as a curved
closed line moves. All ring triangle areas remain positive at every key.

The ring covers the layer's complete raster extent and all target contours,
adding transparent bounds as necessary. UVs stay at their neutral source-space
positions, including density and origin. Original art is not replaced by a
synthetic outline. If alpha greater than 0.004 lies inside the proposed aperture
beyond a half-raster-pixel boundary tolerance, building fails rather than
silently removing the lash pixels. Boundary antialiasing still needs visual
review. Long lash tails are enclosed rather than cropped. Extremely large art
may exceed the existing atlas size limit; the builder does not resize it away.

The mask (`eyeL-continuous-mask` or `eyeR-continuous-mask`) has `isMask:true`, one
vertex per control, all weights 1, and an aperture triangle strip. It samples a
solid opaque atlas texel so deformation cannot accidentally sample transparent
white-art pixels. White and iris are rigid artwork clipped to this exact mask;
only iris gaze translation is applied. The mask collapses entirely when closed,
and the renderer gates closed aperture rendering. There is no half/closed
texture switch on the opted-in side.

Normal openness uses piecewise `closed -> half -> open`. Expression targets
interpolate from closed according to openness, then combine with the normal path
using expression weights normalized when their sum exceeds 1. Thus a blink
closes every expression, including mixed expressions. Eye open inputs above 1
clamp to 1. `browJitomeDrop` moves the corresponding brow downward with the
jitome weight; default 0 introduces no extra brow movement.

Control coordinates are quantized to the same float32 representation as the mesh
before validation. Raw JSON float arrays are decoded at the same float32 precision
as binary arrays and the JS player. Head weights are exactly 1 and all breath
weights within one eye are exactly equal. Fully bound inner/mask vertices use the evaluated control
position directly. This avoids residual closed-eye slivers from rounding.
Every key has exactly the same strictly ordered X coordinates; approximate X
correspondence is rejected. Upper Y never crosses
lower Y, and closed upper/lower Y coincide. Vertex movement changes only Y, so
signed triangle area is affine in convex shape weights. Validating all endpoints
certifies every intermediate blink/expression mix, rather than only a finite
set of animation frames. Regression tests additionally sample mixed shapes.

## Existing expressions and lashes over hair

Matching half/closed/happy/jitome and all active surprised/worried/shy eye
contours replace texture patches on the opted-in side. Their corresponding
opacity terms are removed, including terms within `oneMinusSum`. Active legacy
eye expressions without corresponding provided contours cause an error before
output. Mixing their old open-only patches with a continuous blink would hide
the eye below the old opening threshold; version 1 does not permit that path.
The other eye, brow variants, mouth shapes and vowel patches are preserved.
For nonstandard expression names or pre-extracted expression-over-hair artwork,
use explicit per-side `replace_variants` (raw keys from `expressions.eyes`, e.g.
`["eye_local_eye_half", "eye_local_eye_closed", "eye_local_happy"]`) and
`replace_layers` (existing IDs, e.g. `["eyeL-eye_local_happy-overhair"]`). Names
must be unique and known. Layer IDs must belong to that side (`eyeL-*`, `eyelash-l-*`, `eyewhite-l-*`,
or `irides-l-*`, respectively R/r), and cannot remove required eye art or its active
lifted rings. Unhandled eye-expression names and pre-extracted eye overlay layers
are rejected until listed explicitly, preventing doubled eyelashes. Inputs stay untouched; only opted-in output drawables are omitted.
Explicit replacement names do not synthesize missing expression contours.
Provide corresponding contours and inspect all affected expression presets when
using custom names.

The existing `lift_over` pass still runs before restoration and continuous mesh
construction. Each lifted eyelash-over-hair layer gets its own ring bound to the
same contour, preserving its draw order, alpha and `no_restore` protection.
A `lift_over` entry combining both eyes is rejected; split it per side. This
prevents restored hair from retaining the old lash when the eyelid moves, but
cannot repair eye remnants that were already baked into the input face/hair art.
Inspect separated layers and the actual closing animation.

An optional `keep_mask` on a `lift_over` entry partitions an over-broad lift
without editing the original assets. It references a single 8-bit `L`-mode PNG
the exact source-canvas width/height, relative to the recipe (absolute paths are
also supported). White moves with the lash; black returns to the existing hair;
gray values split softly. The source mask is bilinearly sampled at native lifted
pixel centers, edge-clamped only at the source canvas's outer half-pixels. The
lift must be an in-bounds, equal-density, integer-grid-aligned crop of its hair:
no artwork is resampled. After normal restoration, moving RGB stays unchanged
and moving alpha is `A*K`. Hair RGB remains unchanged while its bundle cuts are
traced. After splitting, residual alpha `(A-A*K)/(1-A*K)` (zero for a fully opaque
retained pixel) is folded into only the frontmost covering hair drawable at each
pixel, using premultiplied over-composition and then unpremultiplication. Only
the original occluder and its actual generated bundle pieces can carry residual;
all remain below the lifted art. This associative composition handles soft hair
edges and overlapping bundles without applying residual alpha twice. Existing
`no_restore` protection remains intact;
there is no extra face-fixed overlay or runtime/schema change. The residual uses
the hair's existing bundle and strand bindings. Missing `keep_mask` preserves
the old path; an all-white mask is a true no-op.

This repair intentionally fails instead of changing composition order: lift and
hair identities must stay intact, overlapping lifts on one occluder and
intervening layers over returned material are rejected, with conservative
bilinear-fringe support checks. Receiving hair cannot have opacity/clip
modifiers; inherited lash opacity must be exactly one at neutral open eyes
(known blink/expression gates are supported), and lifted clipping is unsupported.
`lift_parts` cannot subsequently repaint either partitioned layer. A returned
pixel without any covering hair material after splitting is rejected explicitly.
For faint edges removed by the existing bundle alpha cleanup, temporary build-only
metadata retains the pre-prune material footprint inside each existing carrier.
It assigns the lifted residual without restoring the discarded hair alpha or
inventing a new carrier. Neutral-identity tests compare with the old **post-pruned**
hair stack plus the original lift, not with an unpruned source image.
These checks and pixelwise neutral tests do not replace the final art comparison:
fractional renderer sampling and the animated material assignment still need review.
Keep this opt-in in an isolated candidate recipe until that review passes.

## Validation and rollout

The current runtime and standard-library-only package validator reject malformed
contours, binding lengths/weights, wrong-side clips, incomplete masks, non-strip
mask triangulations, incomplete ring topology, inner ring offsets, partial
weights, visible art marked as a mask, mismatched head/breath attachment,
incompatible local deformation, and triangle flips/collapses. Continuous packages cannot target the historical
pinned core. New geometry exports declare runtime version `0.2.0`, ABI 1, and
`requires: ["continuous-eyes-v1"]`; legacy model exports keep runtime `0.1.0`.
The required version/feature pair is checked against the settings-applied model.
Unknown features, missing declarations, and a 0.1.0 continuous package are
rejected. A model binding the new `EyeJitome` control in opacity, drawable/chain keyforms, packaged motion tracks, or a
nonzero expression preset also requires continuous geometry; a legacy model
cannot announce it under version 0.1.0. A neutral zero-value preset is harmless. This lets old hosts reject new geometry instead of silently rendering
it incorrectly. AoiTalk (41) must update its bundled runtime and import validator
before importing these candidates. This engine change does not activate a model,
replace its installed runtime, or adopt the pilot. Legacy data-only packages keep their existing validation path.

```sh
python -m pytest -q tests/test_continuous_eyes.py tests/test_pipeline.py tests/test_integration_package.py
node --test tests/js/continuous-eyes.test.mjs
```

For a character pilot, build to an isolated output; inspect the open contour and
lifted art first, author half/closed/expression keys, then inspect full-open,
half-open, nearly closed, closed, asymmetric winks, gaze, mixed expressions,
head/body motion and repeated blinks in real WebGL. Evaluate Native separately.
Pixel-art adaptation and nearest-neighbor texture sampling are not automatically
enabled by this builder; preserve original pixel art and review that separately.

## Optional v3 source fixes

See [guided strokes and bounded under-lash coverage](guided-eye-coverage.md) for
the explicit version-3 extension. It retains the shared semantic aperture and
rigid iris while optionally registering both stroke boundaries and allowing a
small alpha-supported under-lash overlap. Existing v1/v2 models remain on their
original topology, clipping behavior and required runtime version.
