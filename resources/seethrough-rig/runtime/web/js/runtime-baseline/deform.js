// Vertex deformation: the heart of the rig.
//
// Every vertex goes through the same pipeline, loosely mirroring a Live2D
// deformer hierarchy:
//
//   rest position
//     -> local part deformers (blink, iris, brow, mouth)       [face space]
//     -> head rotation (2.5D: ellipsoid depth z, yaw/pitch/roll) weighted by `head`
//     -> body rotation (cylinder depth zb, twist/bend/lean/breath) weighted per vertex
//     -> physics offsets from pendulum chains (hair, ribbon, skirt)
//
// Rotations are weighted by *angle* (not by blending positions) so hair and
// the neck bend smoothly instead of shrinking.

const DEG = Math.PI / 180;

function clamp(v, lo, hi) {
  return v < lo ? lo : v > hi ? hi : v;
}

function smoothstep(a, b, x) {
  const t = clamp((x - a) / (b - a), 0, 1);
  return t * t * (3 - 2 * t);
}

export function evalKeys(keys, v) {
  if (v <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) {
    if (v <= keys[i][0]) {
      const [x0, y0] = keys[i - 1];
      const [x1, y1] = keys[i];
      return y0 + ((y1 - y0) * (v - x0)) / (x1 - x0 || 1);
    }
  }
  return keys[keys.length - 1][1];
}

export function evalOpacity(spec, P) {
  if (!spec) return 1;
  let o = 1;
  for (const term of spec) {
    if (term.keys) o *= evalKeys(term.keys, P[term.param] ?? 0);
    else if (term.oneMinusSum) {
      let s = 0;
      for (const p of term.oneMinusSum) s += P[p] ?? 0;
      o *= 1 - smoothstep(term.lo ?? 0.85, term.hi ?? 1.0, s);
    } else if (term.sum) {
      let s = 0;
      for (const p of term.sum) s += P[p] ?? 0;
      o *= clamp(s, 0, 1);
    }
  }
  return clamp(o, 0, 1);
}

function sampleCurve(eye, x) {
  // Returns [upper, lower, taper] of the eye opening at column x.
  const n = eye.up.length;
  const f = (x - eye.x0) / eye.step;
  const i = clamp(Math.floor(f), 0, n - 2);
  const t = clamp(f - i, 0, 1);
  const up = eye.up[i] + (eye.up[i + 1] - eye.up[i]) * t;
  const low = eye.low[i] + (eye.low[i + 1] - eye.low[i]) * t;
  const xEnd = eye.x0 + eye.step * (n - 1);
  const m = eye.margin;
  const taper = x < eye.x0 ? smoothstep(eye.x0 - m, eye.x0, x) : x > xEnd ? 1 - smoothstep(xEnd, xEnd + m, x) : 1;
  return [up, low, taper];
}

// ---------------------------------------------------------------- head / body

export class Transformer {
  constructor(rig) {
    this.rig = rig;
  }

  prepare(P) {
    const h = this.rig.head;
    const b = this.rig.body;
    this.yaw = (P.AngleX ?? 0) * h.yawScale * DEG;
    this.pitch = (P.AngleY ?? 0) * h.pitchScale * DEG;
    this.roll = (P.AngleZ ?? 0) * h.rollScale * DEG;
    this.bob = (-(P.AngleY ?? 0) / 30) * (h.bob ?? 4);
    this.shiftX = ((P.AngleX ?? 0) / 30) * h.shift[0] + ((P.AngleZ ?? 0) / 30) * (h.shift[2] ?? 0);
    this.shiftY = (-(P.AngleY ?? 0) / 30) * h.shift[1];
    this.twist = (P.BodyAngleX ?? 0) * b.twistScale * DEG;
    this.bend = (P.BodyAngleY ?? 0) * b.pitchScale * DEG;
    this.lean = -(P.BodyAngleZ ?? 0) * b.leanScale * DEG;
    this.breath = P.Breath ?? 0;
    this.bodyShift = ((P.BodyAngleX ?? 0) / 10) * (b.shift ?? 0);
  }

  // Parallax mode: the part moves rigidly. Its offset is the yaw/pitch motion
  // of its own centre at its own depth; the whole part then rotates (AngleZ)
  // around the neck. Nothing inside a part is stretched or bent.
  prepareParts(model) {
    this.partT = new Map();
    const h = this.rig.head;
    const save = this.roll;
    this.roll = 0;
    const o = [0, 0];
    // foreshortening: a surface patch facing angle th (from the part's position
    // on the head ellipse) projects cos(th + yaw) / cos(th) as wide after the turn
    const fs = h.foreshorten;
    this.partS = new Map();
    const yaw = this.yaw, pitch = this.pitch;
    const items = [];
    for (const d of model.drawables) {
      if (!d.pc) continue;
      items.push([d.id, d.pc, d.pz, d.fs]);
      for (const sb of d.sub || []) items.push([sb.id, sb.c, sb.z, d.fs]);
    }
    for (const [id, pc, pz, dfs] of items) {
      const [cx, cy] = pc;
      this.head(cx, cy, pz, 1, o);
      this.partT.set(id, [o[0] - cx, o[1] - cy]);
      if (fs && dfs) {
        const u = Math.max(-0.9, Math.min(0.9, (cx - h.center[0]) / h.radius[0]));
        const v = Math.max(-0.9, Math.min(0.9, (cy - h.center[1]) / h.radius[1]));
        const th = Math.asin(u), ph = Math.asin(v);
        const k = fs.k ?? 1;
        const sx = Math.cos(th + yaw * k) / Math.cos(th);
        const sy = Math.cos(ph - pitch * k) / Math.cos(ph);
        const cl = (x) => Math.max(fs.min ?? 0.6, Math.min(fs.max ?? 1.15, x));
        this.partS.set(id, [cl(sx), cl(sy), o[0], o[1]]);
      }
    }
    this.roll = save;
    this.cr = Math.cos(this.roll);
    this.sr = Math.sin(this.roll);
  }

  // position of (x, y) under part `id`'s own rigid transform (translation +
  // uniform per-part scale), before the roll
  partMove(id, x, y, wt) {
    const t = this.partT.get(id);
    let px = x + t[0] * wt;
    let py = y + t[1] * wt;
    const S = this.partS?.get(id);
    if (S) {
      px = S[2] + (px - S[2]) * (1 + (S[0] - 1) * wt);
      py = S[3] + (py - S[3]) * (1 + (S[1] - 1) * wt);
    }
    return [px, py];
  }

  // wt: how much of the head translation the vertex follows (hanging hair
  // keeps its length by following fully); w: how much of the rotation.
  // w2/id2: blend towards another part's transform (the crown of the back hair
  // moves with the bangs, the top of the neck with the face)
  partHead(id, x, y, w, out, w2 = 0, id2 = null, wt = w, subs = null, sw = null) {
    const h = this.rig.head;
    let [px, py] = this.partMove(id, x, y, wt);
    if (subs) {
      // sub-pieces of one layer (bangs / side locks) blended by weight
      let rest = 1, ax = 0, ay = 0;
      for (let k = 0; k < subs.length; k++) {
        const wk = sw[k];
        if (wk <= 0) continue;
        const [qx, qy] = this.partMove(subs[k].id, x, y, wt);
        ax += qx * wk; ay += qy * wk; rest -= wk;
      }
      px = px * rest + ax;
      py = py * rest + ay;
    }
    if (id2 && w2 > 0 && this.partT.has(id2)) {
      const [qx, qy] = this.partMove(id2, x, y, wt);
      px += (qx - px) * w2;
      py += (qy - py) * w2;
    }
    if (this.roll !== 0 && w > 0) {
      const a = this.roll * w;
      const c = Math.cos(a), s = Math.sin(a);
      const rx = px - h.pivot[0], ry = py - h.pivot[1];
      px = h.pivot[0] + rx * c - ry * s;
      py = h.pivot[1] + rx * s + ry * c;
    }
    out[0] = px;
    out[1] = py;
  }

  // Head rotation in rest space. Writes into out[0], out[1].
  head(x, y, z, w, out) {
    const h = this.rig.head;
    if (w <= 0) {
      out[0] = x;
      out[1] = y;
      return;
    }
    if (h.mode === 'rigid') {
      // the whole head is one rigid plate: tilt around the neck base and a
      // small vertical bob. Nothing inside the head moves relative to anything else.
      const [pvx, pvy] = h.rigidPivot ?? h.pivot;
      const a = this.roll * w;
      const c = Math.cos(a), s = Math.sin(a);
      const rx = x - pvx, ry = y - pvy + this.bob * w;
      out[0] = pvx + rx * c - ry * s;
      out[1] = pvy + rx * s + ry * c;
      return;
    }
    z *= h.depthScale ?? 1;
    const yaw = this.yaw * w;
    const pitch = this.pitch * w;
    const roll = this.roll * w;
    const dx = x - h.center[0];
    const dy = y - h.center[1];
    const cy = Math.cos(yaw), sy = Math.sin(yaw);
    const x1 = dx * cy + z * sy;
    const z1 = -dx * sy + z * cy;
    const cp = Math.cos(pitch), sp = Math.sin(pitch);
    const y2 = dy * cp - z1 * sp;
    const z2 = dy * sp + z1 * cp;
    // mild perspective, normalised so that the rest pose is unchanged
    const f = h.focal;
    const k = (f - z) / (f - z2);
    let px = h.center[0] + x1 * k + this.shiftX * w;
    let py = h.center[1] + y2 * k + this.shiftY * w;
    // roll around the neck pivot
    const cr = Math.cos(roll), sr = Math.sin(roll);
    const rx = px - h.pivot[0];
    const ry = py - h.pivot[1];
    out[0] = h.pivot[0] + rx * cr - ry * sr;
    out[1] = h.pivot[1] + rx * sr + ry * cr;
  }

  // Body rotation. wt: twist/bend weight, wl: lean weight, wb: breath weight.
  body(x, y, zb, wt, wl, wb, out) {
    const b = this.rig.body;
    if (b.rigid) {
      // no twist / bend (they would warp the drawing); the lean rotates the
      // whole figure rigidly around its bottom
      wt = 0;
      wl = 1;
    }
    // twist around the vertical body axis
    const a = this.twist * wt;
    const dx = x - b.axisX;
    let px = b.axisX + dx * Math.cos(a) + zb * Math.sin(a) + this.bodyShift * wt;
    const zt = -dx * Math.sin(a) + zb * Math.cos(a);
    // bend forward/back around the hip line
    const p = this.bend * wt;
    const dy = y - b.pivot[1];
    let py = b.pivot[1] + dy * Math.cos(p) - zt * Math.sin(p) * 0.5;
    // breathing: chest rises and widens slightly
    if (wb > 0) {
      py -= this.breath * b.breathLift * wb;
      px = b.axisX + (px - b.axisX) * (1 + this.breath * b.breathWiden * wb);
    }
    // lean sideways around the hip pivot
    const l = this.lean * wl;
    if (l !== 0) {
      const cl = Math.cos(l), sl = Math.sin(l);
      const rx = px - b.pivot[0];
      const ry = py - b.pivot[1];
      px = b.pivot[0] + rx * cl - ry * sl;
      py = b.pivot[1] + rx * sl + ry * cl;
    }
    out[0] = px;
    out[1] = py;
  }

  // Full rigid transform of a point with attributes (used for chain targets).
  point(x, y, a, i, out) {
    if (this.rig.head.mode === 'parallax' && a.pid && this.partT && this.partT.has(a.pid)) {
      const pd = this.pfollow?.[a.pid];
      this.partHead(a.pid, x, y, a.head[i], out, pd ? pd(y) : 0, pd ? pd.id : null, a.ht ? a.ht[i] : a.head[i]);
    }
    else this.head(x, y, a.z[i], a.head[i], out);
    this.body(out[0], out[1], a.zb[i], a.twist[i], a.lean[i], a.breath[i], out);
  }
}

// ---------------------------------------------------------------- strands
//
// A hair bundle (or ribbon tie) is a chain of small rotations along its own
// centre line ("spine"), like a Live2D rotation-deformer chain: the root turns
// with the head, the angle eases back towards the body's angle further down
// ("droop by angle Z"), and physics only changes these angles. Every row of the
// bundle is carried by the spine point at its height and rotated by the angle
// there, so the bundle bends but is never sheared or stretched.

// Static target angles for a strand (no physics).
export function strandTargets(def, tf) {
  const thB = tf.roll + tf.lean;       // head plate angle (head roll + body lean)
  const thL = tf.lean;                 // body angle
  const drop = def.droop ?? 0.5;
  const g = (s) => s * s * (3 - 2 * s);
  const at = (s) => thL + (thB - thL) * (1 - drop * g(s));
  return { root: thB, mid: at(0.5), tip: at(1) };
}

export function strandRoot(def, tf, out) {
  const [x, y] = def.pts[0];
  const r = def.root;
  tf.head(x, y, 0, 1, out);
  tf.body(out[0], out[1], r.zb, r.twist, r.lean, r.breath, out);
}

// Spine angle profile through (0, root), (0.5, mid), (1, tip).
function angleAt(A, s) {
  const m = 2 * A.mid - 0.5 * (A.root + A.tip);
  const u = 1 - s;
  return A.root * u * u + 2 * m * s * u + A.tip * s * s;
}

export function evalStrand(def, tf, A) {
  const n = def.pts.length;
  const out = { x: new Float64Array(n), y: new Float64Array(n), a: new Float64Array(n) };
  const o = [0, 0];
  strandRoot(def, tf, o);
  out.x[0] = o[0];
  out.y[0] = o[1];
  for (let k = 0; k < n; k++) out.a[k] = angleAt(A, k / (n - 1));
  for (let k = 0; k < n - 1; k++) {
    const am = 0.5 * (out.a[k] + out.a[k + 1]);
    const c = Math.cos(am), sn = Math.sin(am);
    const dx = def.pts[k + 1][0] - def.pts[k][0], dy = def.pts[k + 1][1] - def.pts[k][1];
    out.x[k + 1] = out.x[k] + c * dx - sn * dy;
    out.y[k + 1] = out.y[k] + sn * dx + c * dy;
  }
  return out;
}

// Position of a rest point (x, y) carried by the strand at parameter s.
export function strandPoint(def, rt, x, y, s, out) {
  const n = def.pts.length;
  const f = Math.max(0, Math.min(1, s)) * (n - 1);
  const k = Math.min(n - 2, Math.floor(f));
  const u = f - k;
  const sx = def.pts[k][0] * (1 - u) + def.pts[k + 1][0] * u;
  const sy = def.pts[k][1] * (1 - u) + def.pts[k + 1][1] * u;
  const px = rt.x[k] * (1 - u) + rt.x[k + 1] * u;
  const py = rt.y[k] * (1 - u) + rt.y[k + 1] * u;
  const a = rt.a[k] * (1 - u) + rt.a[k + 1] * u;
  const c = Math.cos(a), sn = Math.sin(a);
  const ox = x - sx, oy = y - sy;
  out[0] = px + c * ox - sn * oy;
  out[1] = py + sn * ox + c * oy;
}

// ---------------------------------------------------------------- local deformers

function applyLocal(df, rig, P, x, y, out) {
  switch (df.type) {
    case 'blink': {
      const eye = rig.eyes[df.eye];
      const open = df.eye === 'L' ? P.EyeLOpen : P.EyeROpen;
      const [up, low, taper] = sampleCurve(eye, x);
      const h = low - up;
      if (h > 0.5) {
        const f = clamp((low - y) / h, 0, 1);
        // the lid travels further than the opening so the lash sits on the lower lid
        y += (1 - open) * h * f * taper * (df.travel ?? 1);
      }
      // squeeze slightly towards the eye centre while closing (cheeks push up)
      y += (1 - Math.min(open, 1)) * (df.lift ?? 0) * taper;
      break;
    }
    case 'iris': {
      const eye = rig.eyes[df.eye];
      const bx = P.EyeBallX ?? 0;
      const by = P.EyeBallY ?? 0;
      x = x + bx * eye.irisRange[0];
      y = y - by * eye.irisRange[1];
      break;
    }
    case 'brow': {
      const br = rig.brows[df.side];
      const by = df.side === 'L' ? P.BrowLY : P.BrowRY;
      const ba = df.side === 'L' ? P.BrowLAngle : P.BrowRAngle;
      const open = df.side === 'L' ? P.EyeLOpen : P.EyeROpen;
      // inner end (towards the nose) goes up for positive angle
      const dir = df.side === 'L' ? -1 : 1; // L brow is on screen right; inner end is to its left
      const ang = ba * br.maxAngle * DEG * dir;
      const dx = x - br.center[0];
      const dy = y - br.center[1];
      x = br.center[0] + dx * Math.cos(ang) - dy * Math.sin(ang);
      y = br.center[1] + dx * Math.sin(ang) + dy * Math.cos(ang);
      y += -by * br.range + (1 - Math.min(open, 1)) * br.blinkDrop;
      break;
    }
    case 'mouthOpen': {
      const open = clamp(P.MouthOpenY ?? 0, 0, 1);
      const s = df.minScale + (1 - df.minScale) * open;
      y = df.anchor[1] + (y - df.anchor[1]) * s;
      x = df.anchor[0] + (x - df.anchor[0]) * (0.85 + 0.15 * open);
      break;
    }
    case 'mouthForm': {
      // a little smile/frown bend on the closed mouth line
      const form = P.MouthForm ?? 0;
      const dx = (x - df.anchor[0]) / df.halfWidth;
      y += -form * df.amount * dx * dx;
      break;
    }
    default:
      break;
  }
  out[0] = x;
  out[1] = y;
}

// ---------------------------------------------------------------- driver

export class Deformer {
  constructor(model) {
    this.model = model;
    this.tf = new Transformer(model.rig);
    this.out = new Map();
    for (const d of model.drawables) this.out.set(d.id, new Float32Array(d.positions.length));
    this.opacity = new Map();
  }

  update(P, physics) {
    const rig = this.model.rig;
    if (!this._strandsLinked) {
      const byId = Object.fromEntries((this.model.strands || []).map((sd) => [sd.id, sd]));
      for (const d of this.model.drawables) if (d.strandIds) d.strands = d.strandIds.map((id) => byId[id]);
      this._strandsLinked = true;
    }
    const tf = this.tf;
    tf.prepare(P);
    if (rig.head.mode === 'parallax') tf.prepareParts(this.model);
    const tmp = [0, 0], tmp2 = [0, 0];
    // strands: angles from physics when running, static targets otherwise
    const srt = new Map();
    for (const sd of this.model.strands || []) {
      const A = physics && physics.strandAngles ? physics.strandAngles(sd.id) : null;
      srt.set(sd.id, evalStrand(sd, tf, A || strandTargets(sd, tf)));
    }
    for (const d of this.model.drawables) {
      const op = evalOpacity(d.opacity, P) * (d.baseOpacity ?? 1);
      this.opacity.set(d.id, op);
      if (op <= 0.001 && !d.isMask) continue;
      const src = d.positions;
      const dst = this.out.get(d.id);
      const a = d.attrs;
      const local = (d.deformers || []).filter((df) => df.type !== 'chain');
      const chains = (d.deformers || []).filter((df) => df.type === 'chain');
      const n = src.length / 2;
      const parallax = rig.head.mode === 'parallax';
      for (let i = 0; i < n; i++) {
        let x = src[2 * i];
        let y = src[2 * i + 1];
        for (const df of local) {
          applyLocal(df, rig, P, x, y, tmp);
          x = tmp[0];
          y = tmp[1];
        }
        if (parallax && d.pc) {
          let sw = null;
          if (d.sub) { const K = d.sub.length; sw = a.sw.subarray(i * K, i * K + K); }
          tf.partHead(d.id, x, y, a.head[i], tmp, a.pf ? a.pf[i] : 0, d.pfollow, a.ht ? a.ht[i] : a.head[i], d.sub || null, sw);
        }
        else tf.head(x, y, a.z[i], a.head[i], tmp);
        tf.body(tmp[0], tmp[1], a.zb[i], a.twist[i], a.lean[i], a.breath[i], tmp);
        if (d.strands) {
          // blend the rigid base with the strand(s) this vertex hangs on
          const K = d.strands.length;
          let wsum = 0, bx = 0, by = 0;
          for (let k = 0; k < K; k++) {
            const w = a.stw[i * K + k];
            if (w <= 0) continue;
            const sd = d.strands[k];
            strandPoint(sd, srt.get(sd.id), x, y, a.sts[i * K + k], tmp2);
            bx += w * tmp2[0];
            by += w * tmp2[1];
            wsum += w;
          }
          if (wsum > 0) {
            tmp[0] = tmp[0] * (1 - wsum) + bx;
            tmp[1] = tmp[1] * (1 - wsum) + by;
          }
        }
        x = tmp[0];
        y = tmp[1];
        if (physics) {
          for (const c of chains) {
            const w = c.w[i];
            if (w > 0) {
              physics.offsetAt(c.chain, c.t[i], tmp);
              x += tmp[0] * w;
              y += tmp[1] * w;
            }
          }
        }
        dst[2 * i] = x;
        dst[2 * i + 1] = y;
      }
    }
  }
}
