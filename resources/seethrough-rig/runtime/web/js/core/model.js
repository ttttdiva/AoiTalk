import { validatePlaybackSettings, resolvePlaybackSettings } from '../runtime/playback-settings.js';
import { validateContinuousEyes } from './continuous-eyes.js';
// Model loading: model.json + texture atlases. Existing source notices retained.
// Binary arrays are {b64, dtype: f32|u16|u32}; hosts may inject fetch and loadImage.
const forbidden = new Set(['__proto__', 'constructor', 'prototype']);
export function assertSafeData(value, depth = 0, budget = { n: 0 }) {
  if (depth > 64 || ++budget.n > 8000000) throw new Error('Model/settings data exceeds limits');
  if (typeof value === 'number' && (!Number.isFinite(value) || Math.abs(value) > 1e12)) throw new Error('Invalid numeric data');
  if (!value || typeof value !== 'object') return;
  if (ArrayBuffer.isView(value)) { for (const x of value) if (!Number.isFinite(x)) throw new Error('Invalid typed array'); return; }
  for (const [k, v] of Object.entries(value)) {
    if (forbidden.has(k)) throw new Error('Unsafe data key: ' + k);
    assertSafeData(v, depth + 1, budget);
  }
}
export function abortError() { const e = new Error('Rig load aborted'); e.name = 'AbortError'; return e; }
export function checkAbort(signal) { if (signal?.aborted) throw abortError(); }
export function releaseImages(images) {
  for (const img of new Set(images || [])) { try { if (typeof img?.close === 'function') img.close(); else if (typeof img?.dispose === 'function') img.dispose(); } catch { /* finish cleanup */ } }
}
// Also releases late results from loaders that do not implement cancellation.
export function abortable(promise, signal, release = () => {}) {
  if (!signal) return Promise.resolve(promise);
  return new Promise((resolve, reject) => {
    let done = false;
    const abort = () => { if (!done) { done = true; signal.removeEventListener('abort', abort); reject(abortError()); } };
    signal.addEventListener('abort', abort, { once: true });
    Promise.resolve(promise).then(value => {
      if (done) { release(value); return; }
      done = true; signal.removeEventListener('abort', abort); resolve(value);
    }, err => { if (!done) { done = true; signal.removeEventListener('abort', abort); reject(err); } });
    if (signal.aborted) abort();
  });
}
function decodeArray(v, indices = false) {
  if (ArrayBuffer.isView(v) || Array.isArray(v)) {
    if (v instanceof DataView || [...v].some(x => typeof x !== 'number' || !Number.isFinite(x) || Math.abs(x) > 1e12)) throw new Error('Invalid array number');
    if (indices && [...v].some(x => !Number.isInteger(x) || x < 0 || x > 4294967295)) throw new Error('Invalid index');
    if (ArrayBuffer.isView(v)) return indices ? Uint32Array.from(v) : Float32Array.from(v);
    return indices ? Uint32Array.from(v) : Float32Array.from(v);
  }
  if (!v || typeof v.b64 !== 'string' || !['f32', 'u16', 'u32'].includes(v.dtype)) throw new Error('Invalid encoded array');
  if (v.b64.length > 128 * 1024 * 1024 || v.b64.length % 4 || /[^A-Za-z0-9+/=]/.test(v.b64) || !/^[^=]*={0,2}$/.test(v.b64)) throw new Error('Invalid base64');
  const bin = globalThis.atob(v.b64);
  const stride = v.dtype === 'u16' ? 2 : 4;
  if (bin.length % stride) throw new Error('Invalid array byte length');
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const C = v.dtype === 'u16' ? Uint16Array : v.dtype === 'u32' ? Uint32Array : Float32Array;
  if (indices && v.dtype === 'f32') throw new Error('Indices must be integers');
  const out = new C(bytes.buffer);
  for (const x of out) if (!Number.isFinite(x) || Math.abs(x) > 1e12) throw new Error('Invalid array number');
  return indices ? Uint32Array.from(out) : Float32Array.from(out);
}
function clone(value) {
  if (ArrayBuffer.isView(value)) return new value.constructor(value);
  if (Array.isArray(value)) return value.map(clone);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, clone(v)]));
}
function unique(items, label) {
  const ids = new Set();
  for (const item of items) {
    if (typeof item.id !== 'string' || !item.id || forbidden.has(item.id) || ids.has(item.id)) throw new Error('Invalid/duplicate ' + label + ' id');
    ids.add(item.id);
  }
  return ids;
}
export function decodeModel(data) {
  const { images, drawableById, ...source } = data;
  assertSafeData(source);
  if (source.playbackSettings !== undefined) {
    if (source.playbackSettings?.version !== 1) throw new Error('Model playbackSettings requires version: 1');
    validatePlaybackSettings(source.playbackSettings);
  }
  const json = clone(source);
  if (!json.canvas || ![json.canvas.width, json.canvas.height].every(x => Number.isFinite(x) && x > 0 && x <= 32768)) throw new Error('Invalid model canvas');
  if (!json.rig?.head || !json.rig?.body || !Array.isArray(json.drawables) || !json.drawables.length || json.drawables.length > 4096 || !Array.isArray(json.atlases) || !json.atlases.length || json.atlases.length > 64) throw new Error('Invalid model structure');
  const files = new Set();
  for (const a of json.atlases) {
    if (typeof a.file !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(a.file) || a.file.includes('..') || files.has(a.file)) throw new Error('Invalid/duplicate atlas file');
    files.add(a.file);
  }
  const ids = unique(json.drawables, 'drawable');
  const chainIds = unique(json.chains || [], 'chain');
  const strandIds = unique(json.strands || [], 'strand');
  unique(json.expressions || [], 'expression');
  let vertices = 0;
  for (const d of json.drawables) {
    d.positions = decodeArray(d.positions); d.uvs = decodeArray(d.uvs); d.indices = decodeArray(d.indices, true);
    const n = d.positions.length / 2; vertices += n;
    if (!Number.isInteger(n) || n < 3 || n > 1000000 || vertices > 2000000 || d.uvs.length !== n * 2 || !d.indices.length || d.indices.length % 3 || d.indices.length > 12000000) throw new Error('Invalid mesh lengths');
    if (!Number.isInteger(d.atlas) || d.atlas < 0 || d.atlas >= json.atlases.length) throw new Error('Invalid atlas reference');
    for (const x of d.indices) if (!Number.isInteger(x) || x < 0 || x >= n) throw new Error('Index out of bounds');
    for (const x of d.uvs) if (x < -0.00001 || x > 1.00001) throw new Error('UV out of bounds');
    if (d.eyeRig?.artKeys !== undefined) {
      const art=d.eyeRig.artKeys;
      if(!art || typeof art!=='object' || Array.isArray(art) || Object.keys(art).length>8)throw new Error('Invalid registered eye art');
      for(const entry of Object.values(art)) {
        if(!entry || typeof entry!=='object' || Array.isArray(entry))throw new Error('Invalid registered eye art entry');
        if(entry.uvs?.b64?.length > Math.ceil(n*2*4/3)*4)throw new Error('Registered eye UV byte budget');
        entry.uvs=decodeArray(entry.uvs);
      }
    }
    if (!d.attrs) throw new Error('Missing vertex attributes');
    for (const k of Object.keys(d.attrs)) {
      d.attrs[k] = decodeArray(d.attrs[k]);
      const expected = k === 'sw' ? n * (d.sub?.length || 0) : ['stw','sts'].includes(k) ? n * (d.strandIds?.length || 0) : n;
      if (d.attrs[k].length !== expected) throw new Error('Invalid attribute length: ' + k);
    }
    for (const k of ['z','head','zb','twist','lean','breath']) if (!d.attrs[k]) throw new Error('Missing attribute: ' + k);
    for (const k of Object.keys(d.keyforms || {})) d.keyforms[k] = decodeArray(d.keyforms[k]);
    for (const df of d.deformers || []) {
      for (const k of ['t','w']) if (df[k]) { df[k] = decodeArray(df[k]); if (df[k].length !== n) throw new Error('Invalid deformer weights'); }
      if (df.type === 'chain' && (!chainIds.has(df.chain) || !df.t || !df.w)) throw new Error('Invalid chain binding');
    }
    for (const id of d.clip || []) if (!ids.has(id)) throw new Error('Invalid clip reference');
    for (const id of d.strandIds || []) if (!strandIds.has(id)) throw new Error('Invalid strand reference');
    if (d.strandIds && (!d.attrs.stw || !d.attrs.sts)) throw new Error('Missing strand weights');
    if (d.pfollow && !ids.has(d.pfollow)) throw new Error('Invalid part follow reference');
  }
  for (const c of json.chains || []) {
    if (!Array.isArray(c.points) || c.points.length < 2 || c.points.length > 4096) throw new Error('Invalid chain points');
    for (const k of Object.keys(c.attrs || {})) { c.attrs[k] = decodeArray(c.attrs[k]); if (c.attrs[k].length !== c.points.length) throw new Error('Invalid chain attribute length'); }
    for (const k of Object.keys(c.keyforms || {})) c.keyforms[k] = decodeArray(c.keyforms[k]);
  }
  for (const s of json.strands || []) if (!Array.isArray(s.pts) || s.pts.length < 2 || s.pts.length > 4096 || !s.root) throw new Error('Invalid strand');
  json.drawables.sort((a, b) => a.order - b.order);
  json.drawableById = Object.fromEntries(json.drawables.map(d => [d.id, d]));
  validateContinuousEyes(json);
  if (images) json.images = [...images];
  return json;
}
export async function browserLoadImage(src, signal) {
  checkAbort(signal);
  if (typeof globalThis.Image !== 'function') throw new Error('Host must supply loadImage');
  return new Promise((resolve, reject) => {
    const img = new globalThis.Image();
    const cleanup = () => { img.onload = img.onerror = null; signal?.removeEventListener('abort', abort); };
    const abort = () => { cleanup(); img.src = ''; reject(abortError()); };
    img.onload = () => { cleanup(); resolve(img); };
    img.onerror = () => { cleanup(); reject(new Error('Failed to load ' + src)); };
    signal?.addEventListener('abort', abort, { once: true });
    img.src = src;
  });
}
export async function loadModel(url, { fetch: fetcher = globalThis.fetch, loadImage = browserLoadImage, signal } = {}) {
  checkAbort(signal);
  if (!fetcher) throw new Error('Host must supply fetch');
  const res = await abortable(fetcher(url, { cache: 'no-cache', signal }), signal);
  if (!res.ok) throw new Error('Failed to load model: ' + res.status);
  const data = await abortable(res.json(), signal);
  const json = decodeModel(data);
  const base = url.slice(0, url.lastIndexOf('/') + 1);
  const loaded = [];
  try {
    // Sequential loads bound transient resource use and simplify ownership on failure.
    for (const a of json.atlases) {
      checkAbort(signal);
      const img = await abortable(loadImage(base + a.file, signal), signal, img => releaseImages([img]));
      loaded.push(img);
    }
    checkAbort(signal);
    json.images = loaded; json.baseUrl = base; return json;
  } catch (e) { releaseImages(loaded); throw e; }
}
// Merge only safe user tuning. Caller-owned settings are never mutated.
export function applySettings(model, settings) {
  if (!settings) return model;
  assertSafeData(settings);
  const merge = (dst, src) => {
    for (const [k, v] of Object.entries(src)) {
      if (v && typeof v === 'object' && !Array.isArray(v) && dst[k] && typeof dst[k] === 'object') merge(dst[k], v);
      else dst[k] = clone(v);
    }
  };
  if (settings.playbackSettings !== undefined) {
    if (settings.playbackSettings?.version !== 1) throw new Error('Settings playbackSettings requires version: 1');
    resolvePlaybackSettings(model.playbackSettings, settings.playbackSettings); // Validate merged bounds.
    model.playbackSettings ??= {version:1};
    merge(model.playbackSettings, validatePlaybackSettings(settings.playbackSettings));
  }
  if (settings.rig) merge(model.rig, settings.rig);
  if (settings.chains) for (const c of model.chains || []) if (settings.chains[c.id]) merge(c, settings.chains[c.id]);
  if (settings.expressions) { unique(settings.expressions, 'expression'); model.expressions = clone(settings.expressions); }
  return model;
}
