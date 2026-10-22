import { strandTargets, strandRoot } from './deform.js';
// Pendulum-chain physics (the Live2D "multi-stage pendulum" idea).
//
// Each chain is a list of points hanging from a root. Every frame the rig gives
// us where the points *would* be if the hair were rigid ("rigid targets").
// The simulated points chase their targets with a damped spring, inherit the
// offset of their parent (so a swing travels down the strand), and keep their
// segment lengths. The difference simulated - rigid is the sway offset that
// the deformer adds to the vertices bound to the chain.

export class Physics {
  constructor(model, tf) {
    this.tf = tf;
    this.initStrands(model);
    this.chains = new Map();
    for (const def of model.chains || []) {
      const n = def.points.length;
      this.chains.set(def.id, {
        def,
        n,
        pos: new Float32Array(n * 2),
        vel: new Float32Array(n * 2),
        prev: new Float32Array(n * 2),
        rigid: new Float32Array(n * 2),
        prevRigid: new Float32Array(n * 2),
        off: new Float32Array(n * 2),
        restLen: new Float32Array(n),
        phase: hash(def.id) * Math.PI * 2,
        initialised: false,
      });
    }
    this.time = 0;
    this.acc = 0;
    this.step = 1 / 120;
  }

  // Strand sway: two under-damped angle springs (middle, tip) chase the static
  // droop targets; head/body motion and root acceleration make them lag,
  // overshoot and settle. Output = angles only (the strand bends, never shears).
  initStrands(model) {
    this.strands = new Map();
    // "static" strands only droop (no sway): wide pieces that would visibly bend
    for (const sd of model.strands || []) if (!(sd.physics && sd.physics.static)) this.strands.set(sd.id, { sd, init: false });
  }

  strandAngles(id) {
    const st = this.strands?.get(id);
    if (!st || !st.init) return null;
    return { root: st.root, mid: st.m, tip: st.t };
  }

  updateStrands(P, dt) {
    if (!this.strands) return;
    const tf = this.tf;
    const o = [0, 0];
    const gain = P.PhysicsGain ?? 1;
    const wind = P.Wind ?? 0;
    for (const st of this.strands.values()) {
      const sd = st.sd, ph = sd.physics || {};
      const T = strandTargets(sd, tf);
      strandRoot(sd, tf, o);
      if (!st.init) Object.assign(st, { m: T.mid, t: T.tip, mv: 0, tv: 0, px: o[0], vx: 0, init: true, time: 0 });
      const vx = dt > 0 ? (o[0] - st.px) / dt : 0;
      const ax = dt > 0 ? (vx - st.vx) / dt : 0;
      st.px = o[0];
      st.vx = vx;
      st.root = T.root;
      const steps = Math.max(1, Math.ceil(dt / (1 / 240)));
      const h = dt / steps;
      const L = ph.length ?? 400;
      for (let i = 0; i < steps; i++) {
        st.time += h;
        const wnd = wind * (ph.wind ?? 0.02) * (Math.sin(st.time * 1.3 + (sd.phase ?? 0)) + 0.5 * Math.sin(st.time * 2.7 + (sd.phase ?? 0) * 1.9));
        // root acceleration to the right swings the hanging part to the left
        const kick = -gain * Math.max(-4000, Math.min(4000, ax)) / L;
        const specs = [['m', 'mv', T.mid, ph.freqMid ?? 1.6, 0.5], ['t', 'tv', T.tip, ph.freqTip ?? 1.1, 1.0]];
        for (const [key, vkey, tgt, f, share] of specs) {
          const w = 2 * Math.PI * f, z = ph.zeta ?? 0.3;
          const acc = w * w * (tgt + wnd * share - st[key]) - 2 * z * w * st[vkey] + kick * share;
          st[vkey] += acc * h;
          st[key] += st[vkey] * h;
        }
      }
      const lim = ph.maxAngle ?? 0.35;
      st.m = T.mid + Math.max(-lim, Math.min(lim, st.m - T.mid));
      st.t = T.tip + Math.max(-lim, Math.min(lim, st.t - T.tip));
    }
  }

  reset() {
    if (this.strands) for (const st of this.strands.values()) st.init = false;
    for (const c of this.chains.values()) c.initialised = false;
  }

  computeRigid(P) {
    const tmp = [0, 0];
    for (const c of this.chains.values()) {
      const { def } = c;
      c.prevRigid.set(c.rigid);
      for (let i = 0; i < c.n; i++) {
        this.tf.point(def.points[i][0], def.points[i][1], def.attrs, i, tmp, def.keyforms);
        c.rigid[2 * i] = tmp[0];
        c.rigid[2 * i + 1] = tmp[1];
      }
      if (!c.initialised) {
        c.pos.set(c.rigid);
        c.prevRigid.set(c.rigid);
        c.vel.set(c.rigid);
        c.prev.set(c.rigid);
        for (let i = 1; i < c.n; i++) {
          c.restLen[i] = Math.hypot(def.points[i][0] - def.points[i - 1][0], def.points[i][1] - def.points[i - 1][1]);
        }
        c.initialised = true;
      }
    }
  }

  // Advance the simulation by dt seconds. P: parameter values (Wind, PhysicsGain).
  update(P, dt) {
    this.tf.prepare(P);
    this.computeRigid(P);
    dt = Math.min(dt, 0.1);
    this.updateStrands(P, dt);
    this.acc += dt;
    const gain = P.PhysicsGain ?? 1;
    const wind = P.Wind ?? 0;
    // targets are linearly interpolated across substeps for smooth input
    const steps = Math.floor(this.acc / this.step);
    this.acc -= steps * this.step;
    for (const c of this.chains.values()) {
      const { def, n, pos, vel, rigid, prevRigid } = c;
      const k0 = def.stiffness ?? 120;
      const k1 = def.tipStiffness ?? k0 * 0.35;
      const damp = def.damping ?? 6;
      const follow = def.follow ?? 0.9;
      const inertia = (def.inertia ?? 1) * gain;
      const h = this.step;
      const drag = Math.min(0.5, damp * h);
      const g = def.gravity ?? 1400;
      for (let s = 0; s < steps; s++) {
        const a = (s + 1) / steps;
        const t = this.time + (s + 1) * h;
        const windX = wind * (def.wind ?? 30) * (Math.sin(t * 1.3 + c.phase) + 0.6 * Math.sin(t * 2.9 + c.phase * 1.7) + 0.3 * Math.sin(t * 0.5 + c.phase * 0.3));
        // root is pinned to its rigid target
        pos[0] = prevRigid[0] + (rigid[0] - prevRigid[0]) * a;
        pos[1] = prevRigid[1] + (rigid[1] - prevRigid[1]) * a;
        vel[0] = pos[0];
        vel[1] = pos[1];
        // 1) integrate: gravity + a spring back to the drawn (rigid) shape + wind
        for (let i = 1; i < n; i++) {
          const u = i / (n - 1);
          const k = (k0 + (k1 - k0) * u) / Math.max(0.2, inertia);
          const rx = prevRigid[2 * i] + (rigid[2 * i] - prevRigid[2 * i]) * a;
          const ry = prevRigid[2 * i + 1] + (rigid[2 * i + 1] - prevRigid[2 * i + 1]) * a;
          const px = pos[2 * i], py = pos[2 * i + 1];
          // the drawn shape is carried along by the parent's sway (from the
          // previous substep, which keeps the coupling explicit and stable)
          const prx = prevRigid[2 * i - 2] + (rigid[2 * i - 2] - prevRigid[2 * i - 2]) * a;
          const pry = prevRigid[2 * i - 1] + (rigid[2 * i - 1] - prevRigid[2 * i - 1]) * a;
          const tx = rx + (c.prev[2 * i - 2] - prx) * follow;
          const ty = ry + (c.prev[2 * i - 1] - pry) * follow;
          const ax = k * (tx - px) + windX * u;
          const ay = k * (ty - py);
          vel[2 * i] = px;
          vel[2 * i + 1] = py;
          pos[2 * i] = px + (px - c.prev[2 * i]) * (1 - drag) + ax * h * h;
          pos[2 * i + 1] = py + (py - c.prev[2 * i + 1]) * (1 - drag) + (ay + g * 0) * h * h;
        }
        // 2) inextensible strand: follow-the-leader projection from the root
        for (let i = 1; i < n; i++) {
          const dx = pos[2 * i] - pos[2 * i - 2];
          const dy = pos[2 * i + 1] - pos[2 * i - 1];
          const L = Math.hypot(dx, dy) || 1;
          // the segment keeps the length it has in the current drawn pose
          // (keyforms may lengthen or shorten a strand); a fixed rest length
          // would leave a permanent offset
          const rl = Math.hypot(rigid[2 * i] - rigid[2 * i - 2], rigid[2 * i + 1] - rigid[2 * i - 1]) || c.restLen[i];
          const f = rl / L;
          pos[2 * i] = pos[2 * i - 2] + dx * f;
          pos[2 * i + 1] = pos[2 * i - 1] + dy * f;
        }
        // 3) bending damping: damp the velocity *difference* between neighbours
        //    and smooth kinks so high-frequency zig-zag modes die out quickly
        const beta = def.bendDamping ?? 0.03;
        for (let i = n - 1; i >= 1; i--) {
          const vx = pos[2 * i] - vel[2 * i], vy = pos[2 * i + 1] - vel[2 * i + 1];
          const pvx = pos[2 * i - 2] - vel[2 * i - 2], pvy = pos[2 * i - 1] - vel[2 * i - 1];
          vel[2 * i] += (vx - pvx) * beta;
          vel[2 * i + 1] += (vy - pvy) * beta;
        }
        c.prev.set(vel);
      }
      const maxOff = def.maxOffset ?? 1e9;
      for (let i = 0; i < n; i++) {
        let ox = (pos[2 * i] - rigid[2 * i]) * gain;
        let oy = (pos[2 * i + 1] - rigid[2 * i + 1]) * gain;
        const m = Math.hypot(ox, oy);
        if (m > maxOff) {
          ox *= maxOff / m;
          oy *= maxOff / m;
        }
        c.off[2 * i] = ox;
        c.off[2 * i + 1] = oy;
      }
    }
    this.time += steps * this.step;
  }

  offsetAt(id, t, out) {
    const c = this.chains.get(id);
    if (!c) {
      out[0] = out[1] = 0;
      return;
    }
    const f = Math.min(Math.max(t, 0), 1) * (c.n - 1);
    const i = Math.min(Math.floor(f), c.n - 2);
    const u = f - i;
    out[0] = c.off[2 * i] * (1 - u) + c.off[2 * i + 2] * u;
    out[1] = c.off[2 * i + 1] * (1 - u) + c.off[2 * i + 3] * u;
  }

  debugPoints() {
    const res = [];
    for (const c of this.chains.values()) res.push({ id: c.def.id, pos: Array.from(c.pos), rigid: Array.from(c.rigid) });
    return res;
  }
}

function hash(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return ((h >>> 0) % 10000) / 10000;
}
