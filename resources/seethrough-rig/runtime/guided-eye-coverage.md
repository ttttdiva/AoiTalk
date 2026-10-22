# Guided strokes and bounded under-lash coverage (continuous eyes v3)

This opt-in extension addresses two source-proven authoring problems: a single
moving inner edge does not register the thickness of different painted lash
keys, and a hard aperture can remove iris/white color beneath translucent lash
fringes. It does not automatically repair source artwork or approve a model.

Packages containing any v3 eye require runtime **0.4.0**, ABI 1, and exactly
`["continuous-eyes-v3"]`. The player continues to accept 0.1.0/0.2.0/0.3.0
packages. Older players reject a v3 package rather than silently omit its effect.
At least one of `strokeGuides` and `underLashCoverage` is required for v3.
Both require the registered per-key artwork contract; v1/v2 reject these fields.

## Source authoring

Within a `continuous_eyes.L` or `.R` recipe, use optional
`under_lash_coverage: {"margin_px": 2}` and/or `stroke_guides` alongside existing
`contours` and `key_art`. `stroke_guides` is an explicit mapping from every
contour key to the ordered points described below. The builder does not infer
guides from arbitrary dark pixels. Author and review the source stroke's inner
and outer edges independently of hair classification, then freeze those guides.
The corresponding wire fields are `underLashCoverage` and `strokeGuides`.

Count is not a quality target. A common topology resampled from meaningful
source features is preferable to an unreviewed dense raster staircase. A lash
tip is an authored feature; a detached hair pixel is not a reason to deform the
aperture. Keep original sources and rejected experiments intact.

## Three-boundary ring

Let N be the even inner-contour count. Each `strokeGuides` key has N+6 `[x,y]`
points, ordered as:

1. Upper stroke row, left to right, N/2+1 points
2. Lower stroke row, left to right, N/2+1 points
3. Left-top, right-top, right-bottom, left-bottom wing corners

Row X coordinates match the corresponding inner upper-row X values. Every
guide X remains identical across keys. Wing X values lie outside the aperture
endpoints, with equal X for the two corners on each side. The moving upper row
stays above the inner upper edge and the lower row below the inner lower edge.
The fixed outer rectangle encloses every authored key. Complete topology,
attachment, finite-coordinate and noncollapse checks remain mandatory.

A guided ring contains inner N vertices, moving-guide N+6 vertices and fixed
outer N+6 vertices: **3N+12** total. Its binding indices are
`[0..N-1, N..2N+5, N..2N+5]`; the first 2N+6 weights are one and the rest zero.
The first shell uses the existing canonical annulus triangles. The second shell
connects corresponding guide/fixed perimeter edges with two triangles each.
Unguided coverage-only v3 rings retain the existing 2N+6 layout.

The inner and outer stroke guides use the same convex key weights. A key's UVs
register its own inner and stroke geometry, while the far outer boundary stays
face-local and fixed. This registers the painted stroke band before deformation
transitions through transparent padding. Fixed X keeps signed triangle area
affine in the key coordinates. Validators check canonical oriented endpoint
triangles and require an analytic clearance greater than the conservative
Float32 rounding-area error over the entire source-space convex key hull. This
is stronger than testing sampled poses. It is a source-space guarantee, not a
universal guarantee after arbitrary head/body transforms and their final
Float32 rounding: extremely thin transformed geometry can still collapse.
Actual transformed poses and GPU behavior require separate tests. Do not add
independent moving-X keys to this contract.

The mask remains the same N-point semantic aperture. Iris and white geometry
stay rigid. Outer stroke guides must not become a second independent eye mask.

## Alpha-supported coverage

`underLashCoverage` contains exactly `marginPx`, a finite number greater than
zero and at most **2**. Its unit is one model-canvas/source-coordinate pixel,
not an atlas texel or a screen/DPR pixel. Atlas density, camera zoom and output
resolution do not change that model-space distance.

For normalized horizontal position t and local aperture gap g, the diagnostic
and runtime envelope use `marginPx * sqrt(sin(pi*t)) * min(1, g/4)`. Shared
corners receive exactly zero. The upper edge expands upward and the lower edge
downward; the canonical contour and its serialized rest positions do not move.
The expanded mesh receives the same head/body/breath transforms as the mask.

The final clip is the union of the canonical aperture and the part of this
bounded envelope supported by a visible registered lash ring's current sampled
alpha times effective opacity, strictly greater than 0.004. Each qualifying
ring contributes independently; sub-threshold alpha from several rings is not
accumulated into new support. Hidden/zero-opacity rings do not create coverage.
Plain geometric dilation is deliberately unsupported: it can expose iris/white
outside the authored stroke. Fully closed apertures, closed happy keys and
closed wink sides create **no** coverage or iris/white rendering.

The GPU implementation requires two stencil bits only for opted-in coverage,
and retains the registered-art limit of eight fragment samplers and nine vertex
attributes. Internal alpha-support draws write stencil, not color. Visible
artwork still uses one color draw per ring. Registered ring IDs remain forbidden
as arbitrary model clip targets. Native premultiplied-alpha normalization is
unchanged.

## Material ownership and acceptance

Guides do not fix contaminated artwork. Remove source-supported non-lash skin
or hair fragments from moving eye alpha; keep hair material on its existing
physics carrier. A stationary clean-carrier repair may make a small, reviewed
neutral shading change. Record its sources, masks, reproducible command and
output hashes rather than conceal the repair in an unexplained atlas edit.

Do not use a fading stationary compensation patch to claim faithful motion, or
move blue hair pigment with a lash merely to obtain exact neutral pixels.
Likewise, transferring a complete stroke from a front ring to a behind-hair
ring across keys can thin its intermediate alpha. Inspect the actual composited
result; mathematical rest identity alone does not prove filtered or moving
identity. Long tips and tiny overhangs still need source-specific review.

Review source/old/new neutral, half, quarter, closed, happy and jitome; isolate
each active key at intermediate geometry on a blank background. Check native
pixel centers and fractional sampling, stroke separation/width, under-lash
coverage and leakage, outside-eye stability, and retained hair motion. Preserve
exact endpoint artwork where practical, but judge visible source fidelity rather
than accepting a large double line or rejecting a harmless numeric difference.

Require an explicit rendered preview for export. CPU diagnostics, geometry tests,
mock GL, actual WebGL and actual Native rendering are separate evidence. A
candidate package or a source-code release does not activate a character or
authorize an automatic batch conversion.
