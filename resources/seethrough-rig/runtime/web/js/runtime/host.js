import { resolvePlaybackSettings, validatePlaybackSettings, playbackSettingsFromProfile, playbackSettingPaths, PLAYBACK_SETTINGS_DEFINITION } from './playback-settings.js';
// Shared lifecycle adapter. Engines own all motion, deformation and physics.
import { loadModel, decodeModel, applySettings, assertSafeData, abortable, checkAbort, releaseImages } from '../core/model.js';
import { Renderer } from '../core/renderer.js';
import { EYE_ART_KEYS } from '../core/continuous-eyes.js';
export const VERSION = '0.4.0';
export const ABI = 1;
export const SUPPORTED_MODEL_VERSIONS = Object.freeze(['0.1.0', '0.2.0', '0.3.0', '0.4.0']);
export const SUPPORTED_MODEL_FEATURES = Object.freeze(['continuous-eyes-v1', 'continuous-eyes-v2', 'continuous-eyes-v3']);
export const BASELINE_CORE_REF = '46c0037779fb5e59715030925d66abdc6c09ca12';
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const number = (v, name) => { if (!Number.isFinite(v)) throw new TypeError(name + ' must be finite'); return v; };
function profileOptions(options) {
  const raw = options.profile ?? options.manifest?.profile ?? 'conversation-v1';
  const p = typeof raw === 'string' ? { id: raw } : raw;
  assertSafeData(p);
  if (p.id && !['comparison-v1','conversation-v1'].includes(p.id)) throw new Error('Unknown runtime profile');
  const out = { seed: 3, wind: 0, windBlow: 0, idle: true, breath: true, blink: true, saccade: true, lookAt: false, follow: 0.6, ...p };
  if (options.seed !== undefined) out.seed = options.seed;
  if (!Number.isInteger(out.seed) || out.seed < 0 || out.seed > 4294967295) throw new Error('Invalid seed');
  for (const k of ['idle','breath','blink','saccade','lookAt']) if (typeof out[k] !== 'boolean') throw new Error('Invalid profile flag: ' + k);
  number(out.wind, 'wind'); number(out.windBlow, 'windBlow'); number(out.follow, 'follow');
  if (out.wind < 0 || out.wind > 1 || out.windBlow < 0 || out.windBlow > 1 || out.follow < 0 || out.follow > 1) throw new Error('Profile value out of bounds');
  // the approved comparison profile is fixed: no breeze, no wind
  if (out.id === 'comparison-v1' && (out.wind !== 0 || out.windBlow !== 0)) throw new Error('comparison-v1 profile has no wind');
  return out;
}
export function resolveCoreRef(options) {
  const m = options.manifest;
  if (m && (m.runtime?.abi !== ABI || !SUPPORTED_MODEL_VERSIONS.includes(m.runtime?.version))) throw new Error('Unsupported seethrough runtime ABI/version');
  if (m) {
    const required = m.runtime.requires ?? [];
    if (!Array.isArray(required) || new Set(required).size !== required.length || required.some(feature => !SUPPORTED_MODEL_FEATURES.includes(feature))) throw new Error('Unsupported required model feature');
    const expected={'0.1.0':[],'0.2.0':['continuous-eyes-v1'],'0.3.0':['continuous-eyes-v2'],'0.4.0':['continuous-eyes-v3']}[m.runtime.version];
    if(required.join(',')!==expected.join(','))throw new Error('Model features require matching runtime version 0.2.0, 0.3.0 or 0.4.0');
  }
  const refs = [options.coreRef, m?.runtime?.core_ref, m?.provenance?.core_ref].filter(x => x != null);
  if (new Set(refs).size > 1) throw new Error('Conflicting runtime core refs');
  return refs[0] ?? (m?.provenance?.commit === BASELINE_CORE_REF ? BASELINE_CORE_REF : 'canonical');
}
// Probe actual drawable geometry and opacity through the selected engine. Parameter
// names in metadata alone are not evidence that the rig supports a control.
export function capabilitiesFor(model, engine) {
  if(engine.coreRef!=='canonical'&&Object.values(model.rig.eyes||{}).some(eye=>eye.continuous))throw new Error('Continuous eyes require the canonical engine');
  const params = new engine.Params();
  const deform = new engine.Deformer(model);
  deform.update(params.values, null);
  const verify = () => {
    for (const [id, points] of deform.out) for (const x of points) if (!Number.isFinite(x)) throw new Error('Nonfinite deformed geometry: ' + id);
    for (const [id, opacity] of deform.opacity) if (!Number.isFinite(opacity)) throw new Error('Nonfinite drawable opacity: ' + id);
  };
  verify();
  const rest = new Map([...deform.out].map(([id, a]) => [id, new Float32Array(a)]));
  const opacity = new Map(deform.opacity);
  // An authored key may change only its artwork. Group identical atlas+UV
  // mappings so a redundant key does not advertise a nonexistent capability.
  const artPlans=new Map(model.drawables.filter(d=>d.eyeRig?.artKeys).map(d=>{
    const unique=[];
    const groups=EYE_ART_KEYS.map(key=>{
      const entry=d.eyeRig.artKeys[key]??d.eyeRig.artKeys.open;
      let i=unique.findIndex(other=>other.atlas===entry.atlas&&other.uvs.length===entry.uvs.length&&other.uvs.every((v,j)=>v===entry.uvs[j]));
      if(i<0){i=unique.length;unique.push(entry);}return i;
    });return[d.id,{side:d.eyeRig.side,groups,count:unique.length}];
  }));
  const artState=plan=>{
    const weights=deform.eyeArtWeights?.get(plan.side)??[1,0,0,0,0,0,0,0],result=new Float64Array(plan.count);
    plan.groups.forEach((group,i)=>{result[group]+=weights[i];});return result;
  };
  const restArt=new Map([...artPlans].map(([id,plan])=>[id,artState(plan)]));
  const supported = [];
  for (const def of params.defs) {
    if (['Wind','WindBlow','PhysicsGain','MouthVowel'].includes(def.id)) continue;
    let changed = false;
    for (const value of [def.min, def.max]) {
      params.reset(); params.set(def.id, value); deform.update(params.values, null); verify();
      for (const d of model.drawables) {
        const op = deform.opacity.get(d.id) ?? 1, old = opacity.get(d.id) ?? 1;
        if (Math.abs(op - old) > 1e-6) { changed = true; break; }
        if (op <= 0.001 && !d.isMask) continue;
        if(artPlans.has(d.id)&&artState(artPlans.get(d.id)).some((v,i)=>Math.abs(v-restArt.get(d.id)[i])>1e-6)){changed=true;break;}
        const a = deform.out.get(d.id), b = rest.get(d.id);
        for (let i = 0; i < a.length; i++) if (Math.abs(a[i] - b[i]) > 1e-4) { changed = true; break; }
        if (changed) break;
      }
      if (changed) break;
    }
    if (changed) supported.push(def.id);
  }
  const boundChains = new Set(model.drawables.flatMap(d => (d.deformers || []).filter(df => df.type === 'chain' && df.w?.some(x => x > 0)).map(df => df.chain)));
  const boundStrands = new Set(model.drawables.filter(d => d.attrs.stw?.some(x => x > 0)).flatMap(d => d.strandIds || []));
  const physics = (model.chains || []).some(c => boundChains.has(c.id)) || (model.strands || []).some(s => boundStrands.has(s.id) && !s.physics?.static);
  if (physics) {
    supported.push('PhysicsGain');
    if ((model.chains || []).some(c => boundChains.has(c.id) && (c.wind ?? 30) !== 0) || (model.strands || []).some(s => boundStrands.has(s.id) && !s.physics?.static && (s.physics?.wind ?? 0.02) !== 0)) supported.push('Wind');
    // WindBlow only exists in the current engine; baseline Params intentionally lacks it.
    if (params.byId.WindBlow && (model.strands || []).some(s => boundStrands.has(s.id) && !s.physics?.static)) supported.push('WindBlow');
  }
  // Verify each discrete image is actually selectable, exclusive and closes.
  const mouthVowels = [];
  if (params.byId.MouthVowel) {
    const mouths = model.drawables.filter(d => d.id === 'mouth' || d.id.startsWith('mouth-'));
    for (const [i, vowel] of ['a','i','u','e','o'].entries()) {
      const id = 'mouth-vowel_' + vowel;
      if (!mouths.some(d => d.id === id)) continue;
      params.reset(); params.set('MouthOpenY', 1); params.set('MouthVowel', i + 1);
      deform.update(params.values, null); verify();
      const visible = mouths.filter(d => (deform.opacity.get(d.id) ?? 1) > 0.001);
      if (visible.length !== 1 || visible[0].id !== id || deform.opacity.get(id) < 0.999) continue;
      params.set('MouthOpenY', 0); deform.update(params.values, null); verify();
      const closed = mouths.filter(d => (deform.opacity.get(d.id) ?? 1) > 0.001);
      if ((deform.opacity.get(id) ?? 1) <= 0.001 && closed.length === 1
          && !closed[0].id.startsWith('mouth-vowel_') && deform.opacity.get(closed[0].id) >= 0.999) mouthVowels.push(vowel);
    }
  }
  if (mouthVowels.length) supported.push('MouthVowel');
  const set = new Set(supported);
  const capabilities = { parameters: Object.freeze(supported), expressions: Object.freeze((model.expressions || []).filter(e => Object.entries(e.params || {}).some(([k,v]) => set.has(k) && v !== 0) || !Object.keys(e.params || {}).length).map(e => e.id)), views: Object.freeze(Object.keys(model.views || {})), mouth: set.has('MouthOpenY'), mouthVowels: Object.freeze(mouthVowels), gaze: set.has('EyeBallX') || set.has('EyeBallY'), blink: set.has('EyeLOpen') || set.has('EyeROpen'), physics, headYaw: set.has('AngleX'), deterministicSeek: true, externalMouth: true, coreRef: engine.coreRef };
  capabilities.playbackSettings = Object.freeze(playbackSettingPaths(capabilities));
  return Object.freeze(capabilities);
}
export class RuntimeHost {
  static async create(canvas, modelUrl, options = {}) {
    this.resolveEngine(options); // reject unsupported versions before I/O or allocations
    const model = await loadModel(modelUrl, options);
    let result;
    try {
      let settings = options.settings;
      if (settings == null && options.settingsUrl) {
        const fetcher = options.fetch ?? globalThis.fetch;
        const r = await abortable(fetcher(options.settingsUrl, { cache: 'no-cache', signal: options.signal }), options.signal);
        if (!r.ok) throw new Error('Failed to load settings: ' + r.status);
        settings = await abortable(r.json(), options.signal);
      }
      checkAbort(options.signal);
      result = new this(canvas, model, { ...options, settings });
      result._ownedImages = model.images;
      checkAbort(options.signal);
      return result;
    } catch (e) { if (result) result.dispose(); else releaseImages(model.images); throw e; }
  }
  static fromData(canvas, model, options = {}) { return new this(canvas, model, options); }
  constructor(canvas, model, options = {}) {
    checkAbort(options.signal);
    this.engine = this.constructor.resolveEngine(options);
    this.profile = profileOptions(options);
    this.seed = this.profile.seed;
    this.model = decodeModel(model);
    const modelPlayback = this.model.playbackSettings;
    if (options.manifest?.playbackSettings !== undefined) {
      if (options.manifest.playbackSettings?.version !== 1) throw new Error('Manifest playbackSettings requires version: 1');
      validatePlaybackSettings(options.manifest.playbackSettings);
    }
    applySettings(this.model, options.settings);
    // Validate merged settings and model bindings before creating GPU resources.
    this.model = decodeModel(this.model);
    const continuous = Object.values(this.model.rig.eyes || {}).some(eye => eye.continuous);
    const eyeVersion = Math.max(0, ...Object.values(this.model.rig.eyes || {}).map(eye => eye.continuous?.version ?? 0));
    if (this.engine.coreRef !== 'canonical' && continuous) throw new Error('Continuous eye geometry requires the canonical eye-rig engine');
    if (options.manifest) {
      const runtime = options.manifest.runtime;
      const expected=eyeVersion===3?['0.4.0','continuous-eyes-v3']:eyeVersion===2?['0.3.0','continuous-eyes-v2']:continuous?['0.2.0','continuous-eyes-v1']:['0.1.0',null];
      if(runtime.version!==expected[0] || (runtime.requires||[]).join(',')!==(expected[1]??''))throw new Error('Continuous eye geometry requires an explicit '+expected[0]+' '+(expected[1]??'legacy')+' manifest');
    }
    if (!Array.isArray(this.model.images) || this.model.images.length !== this.model.atlases.length || this.model.images.some(x => !x)) throw new Error('Decoded model requires one image per atlas');
    this._playbackEditable = this.engine.coreRef === 'canonical' && this.profile.id !== 'comparison-v1';
    const resolvedPlayback = resolvePlaybackSettings(playbackSettingsFromProfile(this.profile), modelPlayback, options.settings?.playbackSettings, options.manifest?.playbackSettings, options.playbackSettings);
    this._playbackSettings = this._playbackEditable ? resolvedPlayback : resolvePlaybackSettings(playbackSettingsFromProfile(this.profile));
    const capabilities = capabilitiesFor(this.model, this.engine);
    this.capabilities = Object.freeze({...capabilities, playbackSettings: Object.freeze(playbackSettingPaths(capabilities, this.profile))});
    this.time = 0; this.paused = false; this.disposed = false; this._ownedImages = [];
    this.physicsEnabled = true;
    this.renderOptions = { hidden: new Set(), highlight: null, wireframe: false };
    this._controls = { expression: null, gaze: null, manual: {}, mouth: null, conversation: 'idle' };
    this._generation = 0; this._frameGeneration = -1; this._frameIndex = 0; this._frameFps = null;
    this._resetSimulation();
    this.renderer = new Renderer(canvas, options);
    try { this.renderer.setModel(this.model); } catch (e) { this.renderer.dispose(); throw e; }
  }
  get puppet() { return this; }
  _assertAlive() { if (this.disposed) throw new Error('RigRuntime disposed'); }
  _resetSimulation() {
    const E = this.engine;
    this.params = new E.Params(); this.deformer = new E.Deformer(this.model);
    this.physics = new E.Physics(this.model, this.deformer.tf);
    this.animator = new E.Animator(this.params, this.model, this.seed);
    for (const k of ['idle','breath','blink','saccade','lookAt','follow']) this.animator.options[k] = this.profile[k];
    this.animator.manual.Wind = this.profile.wind;
    // "wind blowing" (hair, ribbon cords, clothes): only engines that have it
    if (this.profile.windBlow > 0) {
      if (!this.params.byId.WindBlow) throw new Error('This runtime core has no WindBlow');
      this.animator.manual.WindBlow = this.profile.windBlow;
    }
    if (this._playbackEditable) {
      this.animator.setPlaybackSettings(this._playbackSettings);
      this.physicsEnabled = this._playbackSettings.physics.enabled;
    }
    const originalSample = this.animator.lip.sample.bind(this.animator.lip);
    this.animator.lip.sample = (t, dt) => {
      const mouth = this._controls.mouth;
      return mouth ? { open: mouth.speaking ? mouth.open : 0, form: mouth.speaking ? mouth.form : 0 } : originalSample(t, dt);
    };
    this.time = 0; this._applyControls();
    for (const [k,v] of Object.entries(this.animator.manual)) this.params.set(k,v);
    if (this._playbackEditable && !this._playbackSettings.wind.enabled) { this.params.set('Wind', 0); this.params.set('WindBlow', 0); }
    const mouth = this._controls.mouth;
    if (mouth) {
      this.params.set('MouthOpenY', mouth.speaking ? mouth.open : 0);
      this.params.set('MouthForm', mouth.speaking ? mouth.form : 0);
      this.params.set('MouthVowel', mouth.speaking ? mouth.vowelIndex : 0);
    }
  }
  _applyControls() {
    Object.assign(this.animator.manual, this._controls.manual);
    if (this._controls.gaze) { Object.assign(this.animator.look, this._controls.gaze); this.animator.options.lookAt = !this._playbackEditable || this._playbackSettings.gaze.enabled; }
    this.animator.setExpression(this._controls.expression);
  }
  _advance(dt) {
    this.time += dt;
    const expressionData = this.model.expressions;
    const mouth = this._controls.mouth;
    const manual = this.animator.manual;
    if (mouth?.speaking) {
      // Do this before Animator.update: expression art, open/form, syllable nod
      // and follow-through springs must all see the real external mouth input.
      this.model.expressions = (expressionData || []).map(e => ({ ...e, params: Object.fromEntries(Object.entries(e.params || {}).filter(([k]) => !k.startsWith('Mouth'))) }));
      this.animator.manual = Object.fromEntries(Object.entries(manual).filter(([k]) => !k.startsWith('Mouth')));
    }
    let P;
    try { P = this.animator.update(this.time, dt).values; }
    finally { this.model.expressions = expressionData; this.animator.manual = manual; }
    if (mouth) {
      this.params.set('MouthOpenY', mouth.speaking ? mouth.open : 0);
      if (mouth.speaking) this.params.set('MouthForm', mouth.form);
      this.params.set('MouthVowel', mouth.speaking ? mouth.vowelIndex : 0);
    }
    if (this.physicsEnabled) this.physics.update(P, dt);
    this.deformer.update(P, this.physicsEnabled ? this.physics : null);
    return P;
  }
  _draw() { this.renderer.resize(); this.renderer.render(this.deformer, this.renderOptions); }
  step(dt) {
    this._assertAlive(); number(dt, 'dt');
    if (dt < 0 || dt > 60) throw new RangeError('dt must be in [0,60]');
    if (this.paused) return this.params.values;
    this._frameFps = null;
    if (dt === 0) { this.deformer.update(this.params.values, this.physicsEnabled ? this.physics : null); this._draw(); return this.params.values; }
    const n = Math.max(1, Math.ceil(dt / 0.1));
    for (let i = 0; i < n; i++) this._advance(dt / n);
    this._draw(); return this.params.values;
  }
  frame(absoluteSeconds, fps = 60) {
    this._assertAlive(); number(absoluteSeconds, 'absoluteSeconds'); number(fps, 'fps');
    if (absoluteSeconds < 0 || !Number.isInteger(fps) || fps < 1 || fps > 240 || absoluteSeconds * fps > 1000000) throw new RangeError('Invalid seek time/fps');
    if (this.paused) return this.params.values;
    const target = Math.floor(absoluteSeconds * fps + 1e-8);
    if (this._frameFps !== fps || this._frameGeneration !== this._generation || target < this._frameIndex) {
      this._resetSimulation(); this._frameIndex = 0;
      this._frameFps = fps; this._frameGeneration = this._generation;
    }
    while (this._frameIndex < target) { this._advance(1 / fps); this._frameIndex++; }
    // Avoid dt=0 Animator updates: they mutate blink/expression/spring state.
    this.deformer.update(this.params.values, this.physicsEnabled ? this.physics : null);
    this._draw(); return this.params.values;
  }
  setExpression(id) {
    this._assertAlive();
    if (id !== null && !(this.model.expressions || []).some(e => e.id === id)) throw new RangeError('Unknown expression: ' + id);
    // Hosts may repeat their desired state each frame. Do not restart a pending
    // blink-before-swap transition unless the requested expression actually changes.
    if (this._controls.expression === id) return;
    this._controls.expression = id; this.animator.setExpression(id); this._generation++;
  }
  setGaze(x, y) {
    this._assertAlive(); this._controls.gaze = { x: clamp(number(x, 'gaze.x'), -1, 1), y: clamp(number(y, 'gaze.y'), -1, 1) };
    Object.assign(this.animator.look, this._controls.gaze); this.animator.options.lookAt = !this._playbackEditable || this._playbackSettings.gaze.enabled; this._generation++;
  }
  setConversation(state, cue) {
    this._assertAlive();
    if (!['idle','listening','thinking','speaking'].includes(state)) throw new RangeError('Unknown conversation state');
    if (cue) { if (cue.expression !== undefined) this.setExpression(cue.expression); if (cue.gaze) this.setGaze(cue.gaze.x, cue.gaze.y); }
    this._controls.conversation = state;
    // Speaking state alone never manufactures mouth pulses. Host supplies actual audio.
    if (state !== 'speaking' && this._controls.mouth?.speaking) this.setMouth({ open: 0, form: 0, speaking: false });
    this._generation++;
  }
  setMouth({ open, form = 0, speaking, vowel = null }) {
    this._assertAlive();
    if (typeof speaking !== 'boolean') throw new TypeError('mouth.speaking must be boolean');
    number(open, 'mouth.open');
    const vowelIndex = vowel === null ? 0 : ['a','i','u','e','o'].indexOf(vowel) + 1;
    if (vowel !== null && !vowelIndex) throw new RangeError('Unknown mouth vowel: ' + vowel);
    if (vowel !== null && !this.capabilities.mouthVowels.includes(vowel)) throw new RangeError('Model/core does not support mouth vowel: ' + vowel);
    this._controls.mouth = { open: speaking ? clamp(open, 0, 1) : 0, form: clamp(number(form, 'mouth.form'), -1, 1), speaking, vowelIndex };
    this.animator.lip.fake = null; this.animator.lip.analyser = null;
    this.params.set('MouthOpenY', this._controls.mouth.open);
    this.params.set('MouthVowel', speaking ? vowelIndex : 0);
    if (!speaking) this.params.set('MouthForm', 0);
    this._generation++;
  }
  setView(name, { zoomScale = 1 } = {}) {
    this._assertAlive(); const view = this.model.views?.[name];
    if (!view || ![view.x,view.y,view.zoom].every(Number.isFinite) || view.zoom <= 0) throw new RangeError('Unknown/invalid view: ' + name);
    number(zoomScale, 'view.zoomScale');
    if (zoomScale <= 0) throw new RangeError('view.zoomScale must be positive');
    Object.assign(this.renderer.camera, view, { zoom: view.zoom * zoomScale });
  }
  setParameters(values) {
    this._assertAlive(); assertSafeData(values);
    for (const [k,v] of Object.entries(values)) {
      number(v, k);
      if (!this.params.byId[k]) throw new RangeError('Unknown parameter: ' + k);
    }
    Object.assign(this._controls.manual, values); Object.assign(this.animator.manual, values); this._generation++;
  }
  getPlaybackSettings() {
    this._assertAlive(); return resolvePlaybackSettings(this._playbackSettings);
  }
  setPlaybackSettings(patch) {
    this._assertAlive();
    if (!this._playbackEditable) throw new Error('Playback settings are fixed for comparison profiles and historical cores');
    const next = resolvePlaybackSettings(this._playbackSettings, patch);
    const supported = new Set(this.capabilities.playbackSettings);
    let changed = false;
    for (const group of PLAYBACK_SETTINGS_DEFINITION.groups) for (const field of group.fields) {
      if (next[group.id][field.id] === this._playbackSettings[group.id][field.id]) continue;
      if (!supported.has(group.id + '.' + field.id)) throw new RangeError('Unsupported playback setting: ' + group.id + '.' + field.id);
      changed = true;
    }
    if (!changed) return;
    for (const [group,field,parameter] of [['wind','wind','Wind'],['wind','windBlow','WindBlow'],['physics','gain','PhysicsGain']]) {
      if (next[group][field] !== this._playbackSettings[group][field] || group === 'wind' && next.wind.enabled !== this._playbackSettings.wind.enabled) delete this._controls.manual[parameter];
    }
    const physicsChanged = this.physicsEnabled !== next.physics.enabled;
    if (!next.gaze.enabled || this._playbackSettings.gaze.tracking && !next.gaze.tracking) this._controls.gaze = null;
    this._playbackSettings = next;
    this.animator.setPlaybackSettings(next, this.time);
    this.physicsEnabled = next.physics.enabled;
    if (physicsChanged) this.physics.reset();
    this._applyControls();
    this.params.set('Wind', next.wind.enabled ? this.animator.manual.Wind : 0);
    this.params.set('WindBlow', next.wind.enabled ? this.animator.manual.WindBlow : 0);
    this._generation++;
  }
  pause() { this._assertAlive(); this.paused = true; }
  resume() { this._assertAlive(); this.paused = false; }
  resize(width, height, pixelRatio) { this._assertAlive(); this.renderer.resize(width, height, pixelRatio); }
  refreshRig() {
    this._assertAlive(); this.deformer.tf.rig = this.model.rig;
    this.physics = new this.engine.Physics(this.model, this.deformer.tf);
    const capabilities = capabilitiesFor(this.model, this.engine);
    this.capabilities = Object.freeze({...capabilities, playbackSettings: Object.freeze(playbackSettingPaths(capabilities, this.profile))}); this._generation++;
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true; this.paused = true;
    this.renderer?.dispose(); releaseImages(this._ownedImages); this._ownedImages = [];
    this.animator.lip.fake = this.animator.lip.analyser = this.animator.lip.buf = null;
    this.deformer.out.clear(); this.deformer.opacity.clear(); this.physics.chains?.clear(); this.physics.strands?.clear();
    this.model.images = [];
  }
}
