# Canonical See-through Rig runtime (ABI 1 / 0.4.0)

The package is @aoitalk/seethrough-rig-runtime, ESM JavaScript plus index.d.ts, without npm dependencies. See web/js/runtime/index.d.ts for the exact interface.

Generate a fresh package directory from the repository root:

```powershell
.venv/Scripts/python.exe scripts/package_runtime.py --output .local/seethrough-integration-20261001/runtime-package-final
```

The output must be empty or absent. Package generation reads current core/adapter source from disk and the baseline engine directly from Git commit 46c0037779fb5e59715030925d66abdc6c09ca12. It never rewinds the worktree, never copies artwork, models, videos, Python or inference code, and never modifies AoiTalk. runtime-provenance.json records SHA-256, size and source commit per payload file; working-tree edits have null source_commit. The baseline engine contains exact historical motion/deform/physics/params; current core is a separate engine.

The repository has no LICENSE. First-party upstream license is unspecified. Existing source notices are retained; package metadata grants no MIT or third-party license.

```js
import { RigRuntime } from '@aoitalk/seethrough-rig-runtime';
const rig = await RigRuntime.create(canvas, modelUrl, {
  signal, manifest, fetch: authenticatedFetch,
  // Supply settingsUrl only when declared by the verified data manifest.
  settingsUrl: manifest.settings ? assetUrl(manifest.settings) : undefined,
  profile: manifest.profile,
});
rig.setConversation('speaking');
rig.setMouth({open: audioEnvelope, form: 0, speaking: true});
rig.step(dt); // host schedules RAF and pauses when hidden
rig.setMouth({open: 0, speaking: false});
rig.dispose();
```

For native, inject options.gl (WebGL2-equivalent VAO/GLSL 300 es context), options.loadImage(src,signal) returning the texture source supported by that context (e.g. Expo {localUri}), and a canvas host {width,height}. No window/document/Image is needed on this path. Renderer calls endFrameEXP when supplied. RigRuntime.fromData(canvas, decodedModelWithImages, options) and the constructor are synchronous; input images remain caller-owned. Async create owns its loaded images and closes/disposes them on failure/abort/dispose, including late completion from a loader that ignores abort. A loader must expose close()/dispose() on temporary owned resources requiring cleanup; localUri file management remains host-owned unless the returned object provides dispose(). GPU context itself remains host-owned.

Use options.manifest.runtime.core_ref equal to the full baseline commit for the approved historical model. provenance.core_ref is a compatibility alias; conflicts reject. If no explicit ref exists, provenance.commit equal to baseline selects baseline; all other manifests default canonical. Unknown ABI/version/core refs reject before fetch/allocation. A profile never implies engine provenance. Low-level standalone Puppet uses canonical source and the same RuntimeHost; RigRuntime adds pinned engine selection without cyclic imports. Standalone editor fields remain available via puppet.

frame(t,fps=60) quantizes to floor(t*fps) and advances genuine physics in fixed steps. Backward seek, FPS change or control edits reconstruct seeded motion, springs and physics from zero. It replays the current controls, not a history of live speech/gaze changes. Repeated frame requests are idempotent. pause gates step/frame; resize works while paused. step rejects negative/nonfinite dt and subdivides intervals over 0.1 seconds instead of silently discarding physics time. Supported FPS 1..240 and seek budget one million frames.

Expressions keep engine blink-before-swap, transition and spring ordering. External speaking filters expression/manual Mouth* values before the engine animation update and supplies the real mouth envelope to syllable nod/springs. Speaking state alone creates no artificial pulses and no AudioContext. A stopped external mouth stays at MouthOpenY=0, while expression face art and form can resume. Controls and capability lists describe authored geometry/opacity behavior; differential deformation probes expose rigid AngleX as unsupported. Unbound physics does not advertise physics. Unknown expression/view/parameter IDs reject.

Tests: node --test --test-isolation=none tests/js/runtime*.mjs (the isolation option avoids child-process restrictions in managed Windows sessions). These exercise real baseline/current Animator/Deformer/Physics with a tracked mock GL, asynchronous abort/failure, actual baseline-model replay, shader/allocation/upload failures, deterministic seeks, lip-expression suppression, and standalone/package module parity. Real WebGL/native visual QA belongs to the supervisor and is not claimed by these tests.

## Release builds use immutable Git source

Use `scripts/package_runtime.py --output <fresh-directory> --source-ref <full-commit-SHA>` for distribution. This reads canonical code and documentation directly from Git, so a clean Windows checkout with CRLF conversions does not become an uncommitted-source dependency. Omit `--source-ref` only for local working-tree QA. The provenance records source mode, source commit, generator commit and individual file hashes. Never edit the generated AoiTalk copy.

## Repeated expression input

`setExpression` is idempotent for an unchanged requested ID, including while the engine is waiting for blink-before-swap. Hosts resolve the effective expression (preview or reply cue) once per frame instead of alternating a default and a cue in the same frame. Historical motion/deformation/physics files remain byte-identical to the approved baseline.

## Breeze and wind in the playback profile

A `conversation-v1` profile may set `wind` (the always-on breeze, 0..1) and `windBlow` (0..1: hair bundles, ribbon cords, chest cords and skirt blowing in the wind). `windBlow` exists only in the current core; a baseline core rejects it. `comparison-v1` stays fixed at `wind: 0` without `windBlow`. Export a wind variant with `python -m seethrough_rig.integration export ... --profile conversation-v1 --wind 0.35 --wind-blow 0.7`; a host switches wind by using a different exported package, or live with `setPlaybackSettings({wind: {windBlow: 0.7}})`.

## Shared, model-bound playback settings (version 1)

`web/js/runtime/playback-settings.schema.json` is the canonical definition of groups, Japanese labels, field types, ranges, steps, defaults and constraints. `scripts/generate_playback_schema.py` creates its JS data mirror for browser/Native compatibility without JSON import syntax. Both files ship in the data-free runtime distribution; parity tests detect drift. Python package/project validation reads the same JSON rather than duplicating ranges. No animation implementation belongs in a host UI or backend.

The versioned settings object has these groups:

- `wind`: breeze and blowing-wind strength
- `blink`: enabled, fixed/random interval, minimum/maximum interval, close/hold/open duration, duration randomness, double-blink probability and gap
- `breath`: enabled, period and amplitude
- `idle`: enabled, speed, head amplitude and body amplitude
- `gaze`: enabled, interval range, movement range, pointer tracking and follow strength
- `physics`: enabled and gain

Persisted blocks require `version: 1`. `model.json.playbackSettings` and `settings.json.playbackSettings` may contain partial groups; new package manifests contain the complete resolved `playbackSettings`. Old packages without this field retain the built-in/profile defaults. Resolution order is built-ins → legacy profile → model → tuning settings → manifest → `options.playbackSettings`. Explicit export CLI wind overrides are written to manifest defaults, without rewriting model or tuning bytes. The whole manifest is bound to the model asset identity by its host; source files retain their inventory hashes. Normal project export uses `conversation-v1` and includes profile/defaults in its content revision.

```js
const defaults = rig.getPlaybackSettings(); // detached complete snapshot
rig.setPlaybackSettings({blink: {intervalMode: 'fixed', intervalMin: 4, holdDuration: 0.08}});
rig.setPlaybackSettings(defaults); // cancel unsaved preview
```

`setPlaybackSettings` validates before mutation, merges partial live values, updates the existing animator, and invalidates deterministic replay. `capabilities.playbackSettings` contains editable `group.field` paths derived from actual behavior probes. Unsupported changed fields reject; unchanged fields in full snapshots are allowed. Historical pinned cores and comparison playback retain their original animation and expose no editable paths. Valid settings blocks remain loadable there but do not alter fixed playback. Saving defaults or overrides never rebuilds meshes or artwork.

For Web, use the same framework-neutral `createPlaybackSettingsPanel(container, {settings, capabilities, onChange, disabled?, groups?})` as the standalone editor. It returns `update({settings?, capabilities?, disabled?, groups?})` and `dispose()`. It renders canonical groups, toggles, enum choices, sliders plus bounded numeric inputs, with disabled unsupported fields. Omit `groups` to show all groups with wind and blink expanded; supply a group-ID array to show only those groups, initially expanded. An empty array renders no fields. Filtering changes only the displayed controls, never the full settings snapshot. The editor places `['wind', 'blink', 'breath']` left of its center preview and `['idle', 'gaze', 'physics']` on the right, and sends every accepted complete snapshot to both panels with `update({settings})`. Numeric labels sit above their slider/value controls so narrow panes remain readable. Invalid/empty numeric typing does not emit a setting and restores the last accepted value on blur. `onChange` receives a complete validated snapshot; the host owns preview, save, cancel, reset, model switching and persistence.

Native renders `playbackSettingsFields(capabilities)` metadata with its own controls and uses `updatePlaybackSetting(settings, group, field, value)` for the same ordered-interval editing behavior. Direct setters remain strict: an ordered range must be valid. The helper moves the opposite endpoint when necessary. Fixed blink mode uses `intervalMin` and disables the ignored `intervalMax` control.

Pointer handlers should send live `setGaze` only when effective `gaze.enabled` and `gaze.tracking` are true. Hosts should use their actual frame runtime's capabilities and keep async responses keyed to the immutable asset/manifest identity; changing a character, revision or runtime must not apply a stale edit. Neither panel metadata nor persistence replaces actual engine behavior.

Additional checks: `node --test tests/js/playback*.test.mjs` and `python -m pytest tests/test_playback_settings.py`. DOM contract/mocked-GL tests, actual WebGL rendering, and actual Native rendering are distinct verification stages.


### Optional five-vowel artwork input (canonical engine)

`setMouth({ open: 1, speaking: true, vowel: 'a' })` selects a registered vowel
image. Accepted vowels are `a`, `i`, `u`, `e`, `o`; omitted/null restores the
existing `MouthOpenY` half/open artwork. `speaking: false` clears the vowel and
closes the mouth. `open < 0.18` uses the existing closed artwork. `form` retains
its old meaning; it is not a vowel classifier. No audio/phoneme inference is
performed. The host must supply the vowel.

Check `runtime.capabilities.mouthVowels` first. The list is recomputed by
render-engine opacity evaluation: a vowel must select exactly one mouth image
and disappear when closed. Missing artwork and historical pinned cores reject
vowel input before changing controls. Legacy mouth inputs remain supported.
`MouthVowel` is a discrete parameter (0 = legacy, 1..5 = a/i/u/e/o), rounded by
the canonical parameter store; it is also usable in manual controls/motions.
Do not add it to smoothly blended expression presets.

### Authoring eye and mouth keys

The existing `aoi.json` recipe already registers independently controlled left
and right open/half/closed eye artwork, including blink-safe expression gates.
Preserve these keys and their source-space image alignment. New vowel images
must use exactly the existing `expressions.box` crop and image dimensions, with
only the mouth changed. Do not use the differently framed head-angle keyform
reference. Dimensions alone cannot prove alignment or artwork quality.

After reviewing all five images, create a separate candidate recipe:

```sh
python scripts/register_face_keys.py --recipe recipes/aoi_rig.json \
  --images assets/aoi/vowels --output recipes/aoi_vowels.json
strig build recipes/aoi_vowels.json --out web/models/aoi_vowels
```

The image directory must contain `mouth_a.png`, `mouth_i.png`, `mouth_u.png`,
`mouth_e.png`, `mouth_o.png`. Registration requires every original expression
asset as well, rejects mismatched dimensions and never overwrites a recipe.
It preserves the legacy mouth and all eye/rig settings. The normal build still
requires the recipe's decomposition work directory. Export the resulting
candidate through the existing production export protocol; the normal data
package carries vowel opacity bindings and atlas art without inference code.

Before adoption, inspect left/right wink, half blink, every vowel, return to
closed/legacy, facial expressions, head roll and repeat/seek in real WebGL.
Synthetic tests and a generated ZIP are not evidence of visual or native-device
quality. Keep the approved model unchanged until this review passes.

#### 2026-10-03 implementation checkpoint

The canonical input/registration code and synthetic regressions are implemented.
The original model's left/right eye keys were rebuilt from the actual source
materials. A separate five-vowel candidate was built and passed data validation,
canonical capability probes, and 245 numeric expression/mouth/eye poses; automatic
blink traversed 0 to 1. Its existing 82 drawables match the ordinary rebuild in
all fields except the two intended half/open-mouth opacity gates.

Validation at this checkpoint: 94 JS tests passed (historical Git-object test
excluded); 167 Python tests passed, 6 skipped, one Git-history packaging test
excluded. The source snapshot has no Git object database for those historical
checks. The real-model probes exercise deformation and opacity, not GPU pixels.
Real WebGL visual review and native-device QA remain pending. Generated artwork,
candidate model adoption and a committed-source production package are separate
from this code checkpoint; the approved `models/aoi` is unchanged.

#### 2026-10-03 local adoption

The candidate has now passed parent-agent image review and is adopted as the
standalone default `models/aoi`; `models/aoi_vowels` retains the separately
addressable candidate. See `docs/face-keys-release.md` for source hashes,
restoration and normal UI instructions. With a vowel selected, the deformer
switches mouth opacity at open=0.18 instead of blending closed/open art inside
the legacy narrow ramp. The continuous speech envelope and vowel=0 behavior
are retained. Generate distributions from the actual committed adoption HEAD.
## Native texture alpha compatibility

The renderer requests `UNPACK_PREMULTIPLY_ALPHA_WEBGL` and reads back the
resulting state before texture upload. Browser image uploads honor that flag;
Expo GL 16.0.10's `localUri` PNG loader returns straight-alpha RGBA and reports
the flag as false. Native uploads are normalized once, at load time, by an
exact-size GPU pass (`texelFetch`, highp coordinates), then copied back into
the original texture before mipmap generation. Ordinary draw calls always
receive premultiplied texture samples. Browser uploads are not multiplied twice.

Multiplying RGB after texture filtering is insufficient: PNG conversion can
clear hidden RGB in transparent texels, and mipmapping straight RGBA then
multiplying the sample darkens the hair bundle and clothing seams. Normalizing
before filtering retains both clean feathered facial edges and clean welds.
The pass uses a separate RGBA target, never samples its framebuffer attachment,
and releases temporary texture/FBO/program/VAO allocations on success or failure.
It restores viewport, program, texture unit, color mask and enable flags, plus
the renderer-owned default canvas framebuffer and unbound VAO. Expo does not
support querying framebuffer/VAO/texture bindings; these are not shared-context
state. No per-frame normalization or readback is added.

The model, atlas, masks, meshes, iris clipping, eye keys, and vowel keys remain
unchanged. `tests/js/renderer-alpha.test.mjs` covers both upload modes, ordering,
state restoration, invalid dimensions, failure cleanup and reload. The optional
`tests/test_renderer_alpha_gpu.py` runs the canonical GLSL and copy-back using
Mesa/EGL (install `moderngl` in the development environment); it checks exact
texels/orientation and minification against a premultiplied reference. These
are regression checks, not Native visual acceptance. Inspect the packaged
runtime on the actual Native GL view with open/half/closed eyes, both winks,
five vowels, and the minified hair/clothing before release.

## Continuous eyes (runtime 0.2.0)

This release is experimental and opt-in. The first real-art pilot is not visually approved. It does not convert, select, activate, or replace any existing model or original artwork. Numerical validation and compatibility tests establish engine behavior, not visual acceptance; production conversion remains blocked until source-specific neutral and blink checks pass.

The current player keeps ABI 1 and accepts legacy 0.1.0 model packages unchanged. Opt-in continuous-eye packages declare `runtime.version: "0.2.0"` and `runtime.requires: ["continuous-eyes-v1"]`. Previous players reject the new version before loading instead of displaying a static or incorrectly clipped eye. Unknown required features and model/manifest mismatches are rejected. The pinned baseline engine remains unchanged and cannot render continuous-eye packages.

Update AoiTalk's generated runtime distribution and strict importer together before importing a new continuous-eye candidate. Do not hand-edit the embedded runtime. Export candidates only after source-specific contour/material checks; numerical tests, software diagnostic images, real WebGL and real Native acceptance are separate. A model import does not authorize its activation.

See [continuous-eye authoring and validation](continuous-eyes.md) for semantic contours, exact annular topology, source-preserving materials and current limitations. The runtime exports `SUPPORTED_MODEL_VERSIONS` and `SUPPORTED_MODEL_FEATURES` for host compatibility checks. Standalone unversioned `fromData` remains available for authoring; distributable packages must carry the explicit feature/version declaration.

## Registered key artwork (runtime 0.3.0)

Version 0.3.0 also accepts all existing 0.1.0 and 0.2.0 packages. A model with any
registered-art eye uses `continuous.version: 2`, `runtime.version: "0.3.0"` and
exactly `runtime.requires: ["continuous-eyes-v2"]`. The older players reject this
package before loading. Models with only v1 continuous eyes keep the 0.2.0/v1
pair; models without continuous eyes keep 0.1.0.

Each v2 lash ring carries source-registered UVs and an atlas reference for every
contour key. The renderer evaluates one common geometry, then blends the active
premultiplied samples in one draw. It does not overlay separately positioned
lash meshes. Identical atlas/UV mappings are treated as the same image during
capability probing. All key assets share at most eight fixed sampler slots;
the context must provide at least eight fragment texture units and nine vertex
attributes. High-precision UV varyings and blending retain registration on large
atlases. The historical alpha-normalization path remains unchanged.

The builder requires an explicit rendered preview for v2 exports: its ordinary
source-layer `rest_composite.png` is not evidence of the registered-art result.
See [registered artwork](registered-eye-art.md) for the authoring contract and
source/pixel receipts. This feature remains opt-in: no original art, adopted
model, default selection, or host runtime is changed by building a candidate.
CPU contact sheets and tests are separate from actual WebGL/Native approval.


## Guided strokes and bounded under-lash coverage (runtime 0.4.0)

Version 0.4.0 accepts all earlier model versions unchanged. An eye with
`continuous.version: 3` opts into an authored moving stroke guide and/or bounded
alpha-supported coverage. Its package declares exactly `runtime.version: "0.4.0"`
and `runtime.requires: ["continuous-eyes-v3"]`. The highest eye version determines
the package requirement; v2-only packages remain 0.3.0, v1-only remain 0.2.0, and
legacy packages remain 0.1.0. Earlier players reject the new package before load.

See [guided stroke and coverage authoring](guided-eye-coverage.md) for the strict
source-unit, topology and material-support contract. These remain opt-in authoring
features, not automatic source conversion, real-GPU acceptance or model activation.
Generated AoiTalk runtime and importer must be updated together before import.
