import { resolvePlaybackSettings } from '../runtime/playback-settings.js';
// Procedural motion layers that write into a parameter set every frame:
// auto blink, breathing, idle sway, saccades, look-at, lip sync, expressions
// and keyframed timelines. All randomness is seeded so recordings are
// reproducible.

export function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const lerp = (a, b, t) => a + (b - a) * t;

// Under-damped spring: follows its target with a little lag, overshoot and
// settle - the "follow-through" that makes rigid motion feel alive.
export class Spring {
  constructor(v = 0, freq = 2.2, zeta = 0.55) {
    this.v = v;
    this.vel = 0;
    this.freq = freq;
    this.zeta = zeta;
    this.init = false;
  }
  update(target, dt) {
    if (!this.init) {
      this.v = target;
      this.init = true;
      return this.v;
    }
    const w = 2 * Math.PI * this.freq;
    const steps = Math.max(1, Math.ceil(dt / (1 / 240)));
    const h = dt / steps;
    for (let i = 0; i < steps; i++) {
      const a = w * w * (target - this.v) - 2 * this.zeta * w * this.vel;
      this.vel += a * h;
      this.v += this.vel * h;
    }
    return this.v;
  }
}

// Critically damped spring towards a target (smooth, no overshoot).
export class Smooth {
  constructor(v = 0, halfLife = 0.12) {
    this.v = v;
    this.vel = 0;
    this.halfLife = halfLife;
  }
  update(target, dt) {
    const omega = 1.386 / Math.max(1e-3, this.halfLife); // ln(4)/halfLife
    const x = omega * dt;
    const exp = 1 / (1 + x + 0.48 * x * x + 0.235 * x * x * x);
    const change = this.v - target;
    const temp = (this.vel + omega * change) * dt;
    this.vel = (this.vel - omega * temp) * exp;
    this.v = target + (change + temp) * exp;
    return this.v;
  }
}

// Smooth value noise for idle motion.
export class Noise1D {
  constructor(seed) {
    const r = mulberry32(seed);
    this.v = Array.from({ length: 256 }, () => r() * 2 - 1);
  }
  at(t) {
    const i = Math.floor(t);
    const f = t - i;
    const a = this.v[i & 255];
    const b = this.v[(i + 1) & 255];
    const s = f * f * (3 - 2 * f);
    return a + (b - a) * s;
  }
  fbm(t) {
    return this.at(t) * 0.65 + this.at(t * 2.1 + 17) * 0.25 + this.at(t * 4.3 + 41) * 0.1;
  }
}

export class AutoBlink {
  constructor(seed = 1) {
    this.rand = mulberry32(seed);
    this.next = 1.5;
    this.phase = -1;
    this.double = false;
    this.enabled = true;
  }
  configure(settings, t = 0) {
    const previous = this.settings;
    this.settings = { ...settings };
    if (!previous) {
      // Preserve the familiar first blink for the unmodified natural profile.
      this.next = settings.intervalMode === 'random' && settings.intervalMin === 2.2 && settings.intervalMax === 6 ? 1.5 : t + this._interval();
    } else if (JSON.stringify(previous) !== JSON.stringify(settings)) {
      this.phase = -1; this.double = false;
      this.next = t + this._interval();
    }
  }
  _interval() {
    const s = this.settings;
    return s.intervalMode === 'fixed' ? s.intervalMin : s.intervalMin + this.rand() * (s.intervalMax - s.intervalMin);
  }
  _begin(t, hold) {
    const s = this.settings;
    const jitter = () => s.durationRandomness ? 1 + (this.rand() * 2 - 1) * s.durationRandomness : 1;
    this.phase = 0; this.start = t;
    this.durations = {close: s.closeDuration * jitter(), hold: hold ?? s.holdDuration * jitter(), open: s.openDuration * jitter()};
  }
  get closedAt() { return this.settings && this.phase >= 0 ? this.start + this.durations.close : undefined; }
  // start a blink now (used to hide eye-shape swaps between expressions)
  force(t, hold = 0.035) {
    if (this.settings) {
      if (this.phase < 0 || t >= this.start + this.durations.close + this.durations.hold) this._begin(t, hold);
      else this.durations.hold = Math.max(this.durations.hold, t - this.start - this.durations.close + hold);
      return;
    }
    if (this.phase < 0) this.next = t;
    this.hold = hold;
  }
  // returns eye openness multiplier 0..1
  update(t) {
    if (!this.settings) return this._legacyUpdate(t);
    if (!this.enabled || !this.settings.enabled) return 1;
    // Advance events at their scheduled times, not at render frame boundaries.
    // Randomness is sampled only on blink/interval events, never every frame.
    for (let events = 0; events < 10000; events++) {
      if (this.phase < 0) {
        if (t < this.next) return 1;
        this._begin(this.next);
      }
      const {close, hold, open} = this.durations, e = t - this.start;
      if (e < close + hold + open) {
        const v = e < close ? 1 - e / close : e < close + hold ? 0 : (e - close - hold) / open;
        return v * v * (3 - 2 * v);
      }
      const end = this.start + close + hold + open;
      this.phase = -1;
      if (!this.double && this.rand() < this.settings.doubleProbability) {
        this.double = true; this.next = end + this.settings.doubleGap;
      } else {
        this.double = false; this.next = end + this._interval();
      }
    }
    throw new RangeError('Blink event budget exceeded');
  }
  _legacyUpdate(t) {
    if (!this.enabled) return 1;
    if (this.phase < 0 && t >= this.next) {
      this.phase = 0;
      this.start = t;
    }
    if (this.phase >= 0) {
      const e = t - this.start;
      const close = 0.075, hold = this.hold ?? 0.035, open = 0.13;
      let v;
      if (e < close) v = 1 - e / close;
      else if (e < close + hold) v = 0;
      else if (e < close + hold + open) v = (e - close - hold) / open;
      else {
        v = 1;
        this.phase = -1;
        this.hold = 0.035;
        if (!this.double && this.rand() < 0.18) {
          this.double = true;
          this.next = t + 0.12;
        } else {
          this.double = false;
          this.next = t + 2.2 + this.rand() * 3.8;
        }
      }
      // ease: eyelids accelerate when closing
      return v * v * (3 - 2 * v);
    }
    return 1;
  }
}

export class LipSync {
  constructor() {
    this.level = new Smooth(0, 0.05);
    this.analyser = null;
    this.buf = null;
    this.enabled = false;
    this.fake = null;
  }
  async useMic() {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const ctx = new AudioContext();
    const src = ctx.createMediaStreamSource(stream);
    this.attach(ctx, src);
  }
  useAudioElement(el) {
    const ctx = new AudioContext();
    const src = ctx.createMediaElementSource(el);
    src.connect(ctx.destination);
    this.attach(ctx, src);
  }
  attach(ctx, src) {
    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = 1024;
    src.connect(this.analyser);
    this.buf = new Float32Array(this.analyser.fftSize);
    this.enabled = true;
  }
  // Procedural "talking" for demos: syllable-like pulses.
  startFake(seed = 7, duration = 4) {
    const r = mulberry32(seed);
    const sy = [];
    let t = 0;
    while (t < duration) {
      const len = 0.09 + r() * 0.14;
      sy.push([t, len, 0.45 + r() * 0.55, r() < 0.25 ? 1 : 0]);
      t += len + (r() < 0.15 ? 0.18 + r() * 0.2 : 0.02);
    }
    this.fake = { sy, start: null, duration };
  }
  sample(t, dt) {
    let target = 0;
    let form = 0;
    if (this.analyser) {
      this.analyser.getFloatTimeDomainData(this.buf);
      let s = 0;
      for (const v of this.buf) s += v * v;
      const rms = Math.sqrt(s / this.buf.length);
      target = clamp((rms - 0.01) * 9, 0, 1);
    } else if (this.fake) {
      if (this.fake.start === null) this.fake.start = t;
      const e = t - this.fake.start;
      if (e > this.fake.duration) this.fake = null;
      else {
        for (const [st, len, amp, round] of this.fake.sy) {
          if (e >= st && e < st + len) {
            const u = (e - st) / len;
            target = Math.sin(Math.PI * u) * amp;
            form = round ? -0.3 : 0.3;
          }
        }
      }
    }
    return { open: this.level.update(target, dt), form };
  }
}

// Keyframed parameter tracks (a tiny motion3-like format):
// { duration, loop, tracks: { ParamId: [[t, value, ease?], ...] }, events: [[t, 'expression', id]] }
export class Timeline {
  constructor(data) {
    this.data = data;
    this.time = 0;
  }
  sample(t) {
    const out = {};
    for (const [id, keys] of Object.entries(this.data.tracks)) out[id] = sampleTrack(keys, t);
    return out;
  }
}

const EASE = {
  linear: (t) => t,
  in: (t) => t * t,
  out: (t) => 1 - (1 - t) * (1 - t),
  inout: (t) => t * t * (3 - 2 * t),
  sine: (t) => 0.5 - 0.5 * Math.cos(Math.PI * t),
};

export function sampleTrack(keys, t) {
  if (t <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) {
    if (t <= keys[i][0]) {
      const [t0, v0] = keys[i - 1];
      const [t1, v1, ease = 'sine'] = keys[i];
      const u = (t - t0) / (t1 - t0 || 1);
      return lerp(v0, v1, (EASE[ease] || EASE.sine)(u));
    }
  }
  return keys[keys.length - 1][1];
}

// The "character brain": combines all procedural layers into parameters.
export class Animator {
  constructor(params, model, seed = 3) {
    this.params = params;
    this.model = model;
    this.blink = new AutoBlink(seed);
    this.lip = new LipSync();
    this.noise = [0, 1, 2, 3, 4, 5, 6].map((i) => new Noise1D(seed * 31 + i));
    this.rand = mulberry32(seed * 7 + 1);
    // follow-through springs (used when the head moves as one rigid plate)
    this.springs = { AngleZ: new Spring(0, 1.8, 0.65), AngleY: new Spring(0, 2.6, 0.6), BodyAngleZ: new Spring(0, 1.4, 0.7),
                     BodyAngleX: new Spring(0, 1.2, 0.72), BodyAngleY: new Spring(0, 1.0, 0.75) };
    this.talkNod = new Smooth(0, 0.08);
    this.talkLean = new Smooth(0, 0.6);
    this.options = { idle: true, breath: true, blink: true, saccade: true, lookAt: false, follow: 0.6 };
    this.look = { x: 0, y: 0 };
    this.lookS = { ax: new Smooth(0, 0.22), ay: new Smooth(0, 0.22), az: new Smooth(0, 0.3), bx: new Smooth(0, 0.45), ex: new Smooth(0, 0.07), ey: new Smooth(0, 0.07) };
    this.saccade = { x: 0, y: 0, next: 0.8 };
    this.exprWeights = {};
    this.exprTarget = null;
    this.exprSmooth = new Map();
    this.manual = {}; // slider values from the editor (base pose)
    this.timeline = null;
    this.timelineStart = 0;
  }

  setPlaybackSettings(settings, t = 0) {
    const previous = this.playbackSettings;
    this.playbackSettings = resolvePlaybackSettings(settings);
    const s = this.playbackSettings;
    if ((!previous && (s.gaze.intervalMin !== 0.6 || s.gaze.intervalMax !== 3)) || previous && (s.gaze.enabled !== previous.gaze.enabled || s.gaze.intervalMin !== previous.gaze.intervalMin || s.gaze.intervalMax !== previous.gaze.intervalMax)) {
      this.saccade.next = t + s.gaze.intervalMin + this.rand() * (s.gaze.intervalMax - s.gaze.intervalMin);
    }
    Object.assign(this.options, {idle:s.idle.enabled, breath:s.breath.enabled, blink:s.blink.enabled, saccade:s.gaze.enabled, lookAt:s.gaze.enabled && s.gaze.tracking, follow:s.gaze.follow});
    this.blink.configure(s.blink, t);
    this.manual.Wind = s.wind.enabled ? s.wind.wind : 0; this.manual.WindBlow = s.wind.enabled ? s.wind.windBlow : 0; this.manual.PhysicsGain = s.physics.gain;
  }

  setExpression(id) {
    if (id === this.exprTarget && !this.pendingExpr) return;
    // blink first, swap the expression once the lids are (almost) shut
    this.blinkOnSwap = true;
    this.pendingExpr = { id, delay: this.options.blink ? 0.075 : 0 };
  }

  playTimeline(tl, t) {
    this.timeline = tl;
    this.timelineStart = t;
  }

  update(t, dt) {
    const P = this.params;
    const o = this.options;
    const settings = this.playbackSettings;
    P.reset();
    for (const [k, v] of Object.entries(this.manual)) P.set(k, v);

    let tlv = null;
    if (this.timeline) {
      const lt = t - this.timelineStart;
      tlv = this.timeline.sample(lt);
      for (const e of this.timeline.data.events || []) {
        if (!e.fired && lt >= e[0]) {
          e.fired = true;
          if (e[1] === 'expression') this.setExpression(e[2]);
          if (e[1] === 'talk') this.lip.startFake(e[3] ?? 7, e[2]);
          if (e[1] === 'lookAt') this.options.lookAt = !!e[2];
        }
      }
      if (lt > this.timeline.data.duration) {
        if (this.timeline.data.loop) {
          this.timelineStart = t;
          for (const e of this.timeline.data.events || []) e.fired = false;
        } else this.timeline = null;
      }
    }

    // --- idle sway (subtle, always on)
    if (o.idle) {
      const n = this.noise;
      const speed = settings?.idle.speed ?? 1, head = settings?.idle.headAmplitude ?? 1, body = settings?.idle.bodyAmplitude ?? 1;
      const s = t * 0.18 * speed;
      P.add('AngleX', n[0].fbm(s) * 6 * head);
      P.add('AngleY', n[1].fbm(s + 5) * 4 * head);
      P.add('AngleZ', n[2].fbm(s + 9) * 4 * head);
      P.add('BodyAngleX', n[3].fbm(s * 0.8 + 3) * 2.2 * body);
      P.add('BodyAngleZ', n[4].fbm(s * 0.7 + 7) * 1.5 * body);
      if (this.model.rig?.body?.rigid) {
        // the whole figure drifts a little closer / further and shifts its
        // weight; leaning in comes with a small nod and a lean to that side
        const s2 = t * 0.13 * speed;
        const depth = n[5].fbm(s2 + 11) * 6;
        const sway = n[6].fbm(s2 * 0.9 + 23) * 4;
        P.add('BodyAngleY', depth * body);
        P.add('BodyAngleX', sway * body);
        P.add('AngleY', -depth * 0.5 * head);
        P.add('BodyAngleZ', -sway * 0.4 * body);
      }
    }
    if (o.breath) {
      const b = 0.5 - 0.5 * Math.cos((t * 2 * Math.PI) / (settings?.breath.period ?? 3.6));
      const amplitude = settings?.breath.amplitude ?? 1;
      P.add('Breath', b * amplitude);
      P.add('AngleY', (b - 0.5) * 1.2 * amplitude);
    }

    // --- look at pointer
    if (o.lookAt) {
      const L = this.lookS;
      const lx = clamp(this.look.x, -1, 1) * (settings?.gaze.range ?? 1);
      const ly = clamp(this.look.y, -1, 1) * (settings?.gaze.range ?? 1);
      P.add('AngleX', L.ax.update(lx * 28 * o.follow, dt));
      // a head that cannot turn tilts a little towards where it looks
      if (this.model.rig?.head?.mode === 'rigid') P.add('AngleZ', L.az.update(-lx * 10 * o.follow, dt) + lx * ly * 8 * o.follow);
      P.add('AngleY', L.ay.update(ly * 24 * o.follow, dt));
      P.add('AngleZ', L.az.update(-lx * ly * 8 * o.follow, dt));
      P.add('BodyAngleX', L.bx.update(lx * 7 * o.follow, dt));
      P.set('EyeBallX', L.ex.update(lx * 0.9, dt));
      P.set('EyeBallY', L.ey.update(ly * 0.9, dt));
    } else if (o.saccade) {
      const S = this.saccade;
      let gazeEvents = 0;
      while (t > S.next) {
        if (++gazeEvents > 10000) throw new RangeError('Gaze event budget exceeded');
        const r = this.rand;
        const big = r() < 0.25;
        S.x = (r() * 2 - 1) * (big ? 0.7 : 0.25);
        S.y = (r() * 2 - 1) * (big ? 0.4 : 0.15);
        if (r() < 0.4) S.x = S.y = 0;
        S.next = settings ? S.next + settings.gaze.intervalMin + r() * (settings.gaze.intervalMax - settings.gaze.intervalMin) : t + 0.6 + r() * 2.4;
        if (!settings) break;
      }
      P.add('EyeBallX', this.lookS.ex.update(S.x * (settings?.gaze.range ?? 1), dt));
      P.add('EyeBallY', this.lookS.ey.update(S.y * (settings?.gaze.range ?? 1), dt));
    }

    // --- timeline tracks (added on top of idle)
    if (tlv) {
      for (const [k, v] of Object.entries(tlv)) {
        if (k.startsWith('=')) P.set(k.slice(1), v);
        else P.add(k, v);
      }
    }

    if (settings && this.blinkOnSwap) {
      this.blinkOnSwap = false;
      if (o.blink) this.blink.force(t, Math.max(0.2, settings.blink.holdDuration));
      if (this.pendingExpr) this.pendingExpr.at = o.blink ? this.blink.closedAt : t;
    }
    if (this.pendingExpr && (settings && !o.blink || this.pendingExpr.at !== undefined && t >= this.pendingExpr.at)) {
      this.exprTarget = this.pendingExpr.id;
      this.pendingExpr = null;
    }

    // --- expressions: one transition from the previous preset to the current
    //     one; the two weights always sum to 1, so exactly one set of face art
    //     is shown at any moment (art swaps are hard steps at 0.5)
    const presets = this.model.expressions || [];
    const cur = this.exprTarget ?? null;
    if (cur !== this._exprTo) {
      this._exprFrom = this._exprTo ?? null;
      this._exprTo = cur;
      this._exprT = new Smooth(0, 0.06);
    }
    const wT = this._exprT ? this._exprT.update(1, dt) : 1;
    for (const pr of presets) {
      const w = (pr.id === this._exprTo ? wT : 0) + (pr.id === this._exprFrom ? 1 - wT : 0);
      if (w < 1e-3) continue;
      for (const [k, v] of Object.entries(pr.params)) P.add(k, v * w);
    }

    // --- blink (multiplies manual eye openness); expression swaps blink so the
    //     eye art changes while the lids are shut instead of visibly cross-fading
    if (this.blinkOnSwap) {
      this.blinkOnSwap = false;
      if (o.blink) this.blink.force(t, 0.2);
      if (this.pendingExpr) this.pendingExpr.at = t + this.pendingExpr.delay;
    }
    const bl = o.blink ? this.blink.update(t) : 1;
    P.set('EyeLOpen', P.get('EyeLOpen') * bl);
    P.set('EyeROpen', P.get('EyeROpen') * bl);

    // --- lip sync
    const ls = this.lip.sample(t, dt);
    if (ls.open > 0.001) {
      P.add('MouthOpenY', ls.open);
      P.add('MouthForm', ls.form * ls.open);
    }
    const rigid = this.model.rig?.head?.mode === 'rigid';
    if (rigid) {
      // small nods that follow the syllables while talking, and a slight lean in
      P.add('AngleY', -this.talkNod.update(ls.open, dt) * 7);
      P.add('BodyAngleY', this.talkLean.update(this.lip.fake || ls.open > 0.05 ? 1 : 0, dt) * 3);
      // the head briefly lags behind a quick body lean (counter-motion), then catches up
      const lean = P.get('BodyAngleZ');
      const leanVel = dt > 0 ? (lean - (this._prevLean ?? lean)) / dt : 0;
      this._prevLean = lean;
      P.add('AngleZ', -leanVel * 0.12);
      // lag, overshoot and settle on head tilt / nod and body lean
      for (const [k, sp] of Object.entries(this.springs)) P.set(k, sp.update(P.get(k), dt));
    }
    // The shared wind switch gates all authored/manual/timeline wind inputs,
    // without overwriting the stored strengths used when it is enabled again.
    if (settings && !settings.wind.enabled) { P.set('Wind', 0); P.set('WindBlow', 0); }
    return P;
  }
}
