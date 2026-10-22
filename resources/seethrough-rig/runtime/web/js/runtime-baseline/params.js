// Parameter store. Names follow the Live2D standard parameter set where one
// exists (ParamAngleX -> AngleX etc.) so motions are easy to reason about.

export const PARAM_DEFS = [
  // group, id, min, max, default, label
  ['head', 'AngleX', -30, 30, 0, '顔 左右'],
  ['head', 'AngleY', -30, 30, 0, '顔 上下'],
  ['head', 'AngleZ', -30, 30, 0, '顔 傾き'],
  ['body', 'BodyAngleX', -10, 10, 0, '体 ひねり'],
  ['body', 'BodyAngleY', -10, 10, 0, '体 前後'],
  ['body', 'BodyAngleZ', -10, 10, 0, '体 傾き'],
  ['body', 'Breath', 0, 1, 0, '呼吸'],
  ['eye', 'EyeLOpen', 0, 1.2, 1, '左目 開閉'],
  ['eye', 'EyeROpen', 0, 1.2, 1, '右目 開閉'],
  ['eye', 'EyeBallX', -1, 1, 0, '目玉 X'],
  ['eye', 'EyeBallY', -1, 1, 0, '目玉 Y'],
  ['brow', 'BrowLY', -1, 1, 0, '左眉 上下'],
  ['brow', 'BrowRY', -1, 1, 0, '右眉 上下'],
  ['brow', 'BrowLAngle', -1, 1, 0, '左眉 角度'],
  ['brow', 'BrowRAngle', -1, 1, 0, '右眉 角度'],
  ['mouth', 'MouthOpenY', 0, 1, 0, '口 開閉'],
  ['mouth', 'MouthForm', -1, 1, 0, '口 変形'],
  ['expr', 'EyeHappy', 0, 1, 0, '目: 笑顔'],
  ['expr', 'EyeSurprised', 0, 1, 0, '目: 驚き'],
  ['expr', 'EyeWorried', 0, 1, 0, '目: 困り'],
  ['expr', 'EyeShy', 0, 1, 0, '目: 照れ'],
  ['expr', 'BrowHappy', 0, 1, 0, '眉: 笑顔'],
  ['expr', 'BrowSurprised', 0, 1, 0, '眉: 驚き'],
  ['expr', 'BrowWorried', 0, 1, 0, '眉: 困り'],
  ['expr', 'BrowShy', 0, 1, 0, '眉: 照れ'],
  ['expr', 'MouthSurprised', 0, 1, 0, '口: 驚き'],
  ['expr', 'MouthWorried', 0, 1, 0, '口: 困り'],
  ['expr', 'MouthHappy', 0, 1, 0, '口: 笑顔'],
  ['expr', 'MouthShy', 0, 1, 0, '口: 照れ'],
  ['physics', 'Wind', 0, 1, 0.35, '風（揺れの常時量）'],
  ['physics', 'PhysicsGain', 0, 2, 1, '揺れの強さ'],
];

export class Params {
  constructor(defs = PARAM_DEFS) {
    this.defs = defs.map(([group, id, min, max, def, label]) => ({ group, id, min, max, default: def, label }));
    this.byId = Object.fromEntries(this.defs.map((d) => [d.id, d]));
    this.values = {};
    this.reset();
  }

  reset() {
    for (const d of this.defs) this.values[d.id] = d.default;
  }

  get(id) {
    return this.values[id] ?? 0;
  }

  set(id, v) {
    const d = this.byId[id];
    if (!d) return;
    this.values[id] = Math.min(d.max, Math.max(d.min, v));
  }

  add(id, v) {
    this.set(id, this.get(id) + v);
  }

  snapshot() {
    return { ...this.values };
  }
}
