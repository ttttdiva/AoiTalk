/** Chat-only host input. The shared runtime still owns face deformation,
 * blinking, speech, and physics; this controller never drives its own RAF. */
export type Point = { x: number; y: number };
export type TouchMood = 'shy' | 'happy';
const clamp = (value: number, limit = 1) => Math.max(-limit, Math.min(limit, value));

export function pointerGaze(x: number, y: number, bounds: Pick<DOMRect, 'left' | 'top' | 'width' | 'height'>): Point {
  if (![x, y, bounds.left, bounds.top, bounds.width, bounds.height].every(Number.isFinite) || bounds.width <= 0 || bounds.height <= 0) return { x: 0, y: 0 };
  return { x: clamp((x - bounds.left - bounds.width / 2) / (bounds.width / 2), .85), y: clamp((bounds.top + bounds.height / 2 - y) / (bounds.height / 2), .75) };
}

export class AvatarInteraction {
  gaze: Point = { x: 0, y: 0 };
  target: Point = { x: 0, y: 0 };
  pointer: { id: number; start: Point; last: Point; dragged: boolean } | null = null;
  mood: TouchMood = 'shy';
  remaining = 0;
  strength = 0;
  bounce = 0;
  velocity = 0;
  sway = 0;
  reducedMotion = false;

  reset() {
    this.gaze = { x: 0, y: 0 }; this.target = { x: 0, y: 0 };
    this.pointer = null; this.remaining = 0; this.strength = 0;
    this.bounce = 0; this.velocity = 0; this.sway = 0;
  }
  down(id: number, x: number, y: number) {
    if (this.pointer) return false;
    this.pointer = { id, start: { x, y }, last: { x, y }, dragged: false };
    return true;
  }
  move(id: number, x: number, y: number) {
    const p = this.pointer;
    if (!p || p.id !== id) return;
    const distance = Math.hypot(x - p.last.x, y - p.last.y);
    p.dragged ||= Math.hypot(x - p.start.x, y - p.start.y) >= 7;
    if (p.dragged && distance > .5) {
      this.mood = 'happy'; this.remaining = .65;
      this.sway = this.reducedMotion ? 0 : clamp((x - p.last.x) / 18) * 1.4;
    }
    p.last = { x, y };
  }
  up(id: number) {
    if (!this.pointer || this.pointer.id !== id) return;
    const dragged = this.pointer.dragged;
    this.pointer = null;
    if (!dragged) this.touch();
  }
  touch() {
    this.mood = 'shy'; this.remaining = 1.15;
    if (!this.reducedMotion) this.velocity = -32;
  }
  step(delta: number) {
    // Clamp tab stalls and use small spring steps, so repeated input cannot
    // accumulate an unstable impulse or jump after a long background pause.
    const dt = Number.isFinite(delta) ? Math.max(0, Math.min(delta, .1)) : 0;
    this.remaining = Math.max(0, this.remaining - dt);
    const blend = 1 - Math.exp(-dt * 12);
    for (const key of ['x', 'y'] as const) {
      this.gaze[key] += (this.target[key] - this.gaze[key]) * blend;
      if (Math.abs(this.gaze[key] - this.target[key]) < .0001) this.gaze[key] = this.target[key];
    }
    this.strength += ((this.remaining > 0 ? 1 : 0) - this.strength) * (1 - Math.exp(-dt * (this.remaining > 0 ? 12 : 7)));
    if (this.strength < .001) this.strength = 0;
    if (this.reducedMotion) { this.bounce = 0; this.velocity = 0; this.sway = 0; }
    else {
      for (let left = dt; left > 0;) {
        const step = Math.min(left, 1 / 120); left -= step;
        this.velocity += (-190 * this.bounce - 15 * this.velocity) * step;
        this.bounce = clamp(this.bounce + this.velocity * step, 3);
      }
      if (Math.abs(this.bounce) + Math.abs(this.velocity) < .001) { this.bounce = 0; this.velocity = 0; }
      this.sway *= Math.exp(-dt * 7);
      if (Math.abs(this.sway) < .001) this.sway = 0;
    }
    return { gaze: this.reducedMotion ? { x: 0, y: 0 } : this.gaze, strength: this.strength, mood: this.mood,
      transform: this.reducedMotion || (!this.bounce && !this.sway) ? '' : `translateY(${this.bounce}px) rotate(${this.sway}deg) scale(${1 - this.bounce * .004}, ${1 + this.bounce * .006})` };
  }
}

/** Only supported controls are sent. Openness is a base for runtime blink
 * multiplication, not a replacement blink animation. MouthOpenY is deliberately
 * left to real audio; the silent smile uses shape/art controls instead. */
export function touchFace(parameters: readonly string[], mood: TouchMood, strength: number) {
  const supported = new Set(parameters);
  const result: Record<string, number> = {};
  const amount = clamp(strength);
  for (const id of ['EyeLOpen', 'EyeROpen']) if (supported.has(id)) result[id] = 1 - amount * .22;
  for (const part of ['Eye', 'Brow', 'Mouth']) {
    for (const name of ['Shy', 'Happy']) {
      const id = `${part}${name}`;
      if (supported.has(id)) result[id] = name.toLowerCase() === mood ? amount * .7 : 0;
    }
  }
  if (supported.has('MouthForm')) result.MouthForm = amount * .45;
  return result;
}

export function bindAvatarInteraction(canvas: HTMLCanvasElement, active: () => boolean, onReset: () => void) {
  const state = new AvatarInteraction();
  const media = window.matchMedia('(prefers-reduced-motion: reduce)');
  state.reducedMotion = media.matches;
  const release = () => {
    const id = state.pointer?.id;
    state.pointer = null;
    if (id !== undefined && canvas.hasPointerCapture(id)) canvas.releasePointerCapture(id);
  };
  const reset = () => { release(); state.reset(); onReset(); };
  const preference = () => { reset(); state.reducedMotion = media.matches; };
  const move = (event: PointerEvent) => {
    if (!active()) return;
    if (event.pointerType !== 'touch' || state.pointer?.id === event.pointerId) {
      state.target = pointerGaze(event.clientX, event.clientY, canvas.getBoundingClientRect());
    }
    state.move(event.pointerId, event.clientX, event.clientY);
  };
  const down = (event: PointerEvent) => {
    if (!active() || event.button !== 0 || event.isPrimary === false) return;
    if (!state.down(event.pointerId, event.clientX, event.clientY)) return;
    canvas.setPointerCapture(event.pointerId);
    if (event.pointerType === 'mouse') event.preventDefault();
  };
  const up = (event: PointerEvent) => {
    if (state.pointer?.id !== event.pointerId) return;
    state.up(event.pointerId);
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    if (event.pointerType === 'touch') state.target = { x: 0, y: 0 };
  };
  const cancel = (event: PointerEvent) => { if (state.pointer?.id === event.pointerId) reset(); };
  const leaveWindow = (event: PointerEvent) => { if (!event.relatedTarget) reset(); };
  const key = (event: KeyboardEvent) => {
    if (!active() || event.repeat || !['Enter', ' '].includes(event.key)) return;
    event.preventDefault(); state.touch();
  };
  // Passive observation outside the avatar never captures selection/scroll.
  window.addEventListener('pointermove', move, { passive: true });
  window.addEventListener('pointerup', up, { passive: true });
  window.addEventListener('pointerout', leaveWindow, { passive: true });
  window.addEventListener('blur', reset);
  canvas.addEventListener('pointerdown', down);
  canvas.addEventListener('pointercancel', cancel);
  canvas.addEventListener('lostpointercapture', cancel);
  canvas.addEventListener('keydown', key);
  media.addEventListener('change', preference);
  return { state, reset, dispose: () => {
    window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up);
    window.removeEventListener('pointerout', leaveWindow); window.removeEventListener('blur', reset);
    canvas.removeEventListener('pointerdown', down); canvas.removeEventListener('pointercancel', cancel);
    canvas.removeEventListener('lostpointercapture', cancel); canvas.removeEventListener('keydown', key);
    media.removeEventListener('change', preference); reset();
  } };
}
