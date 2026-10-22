// Shared, framework-independent playback contract. No DOM, renderer or host imports.
import definition from './playback-settings-schema.js';
const copy = value => Array.isArray(value) ? value.map(copy) : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).map(([k,v]) => [k,copy(v)])) : value;
const freeze = value => { if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); } return value; };
export const PLAYBACK_SETTINGS_VERSION = definition.version;
export const PLAYBACK_SETTINGS_DEFINITION = freeze(definition);
export const DEFAULT_PLAYBACK_SETTINGS = freeze({version: definition.version, ...Object.fromEntries(definition.groups.map(group => [group.id, Object.fromEntries(group.fields.map(field => [field.id,field.default]))]))});
const object = (value, path) => {
  if (!value || typeof value !== 'object' || Array.isArray(value) || ![Object.prototype, null].includes(Object.getPrototypeOf(value))) throw new TypeError(path + ' must be an object');
};
const at = (value,path) => path.split('.').reduce((v,k) => v?.[k],value);
function checkConstraints(value) {
  for (const constraint of definition.constraints) {
    // A partial patch cannot decide a condition it does not specify. Resolve
    // validates again after all layers supply the complete effective state.
    if (constraint.when && Object.entries(constraint.when).some(([path,expected]) => at(value,path) === undefined || at(value,path) !== expected)) continue;
    const [a,b] = constraint.paths.map(path => at(value,path));
    if (a !== undefined && b !== undefined && a > b) throw new RangeError(constraint.paths.join(' must be <= '));
  }
}
/** Validate and copy a partial patch. resolvePlaybackSettings validates merged constraints. */
export function validatePlaybackSettings(value) {
  object(value, 'playbackSettings');
  const out = {};
  for (const [key,groupValue] of Object.entries(value)) {
    if (key === 'version') {
      if (groupValue !== PLAYBACK_SETTINGS_VERSION) throw new RangeError('Unsupported playbackSettings version');
      out.version = groupValue; continue;
    }
    const group = definition.groups.find(group => group.id === key);
    if (!group) throw new RangeError('Unknown playbackSettings group: ' + key);
    object(groupValue, 'playbackSettings.' + key); out[key] = {};
    for (const [id,v] of Object.entries(groupValue)) {
      const field = group.fields.find(field => field.id === id), path = key + '.' + id;
      if (!field) throw new RangeError('Unknown playbackSettings field: ' + path);
      if (field.type === 'number' && (typeof v !== 'number' || !Number.isFinite(v) || v < field.min || v > field.max)) throw new RangeError(path + ' must be finite and in [' + field.min + ',' + field.max + ']');
      if (field.type === 'boolean' && typeof v !== 'boolean') throw new TypeError(path + ' must be boolean');
      if (field.type === 'enum' && !field.options.some(option => option.value === v)) throw new RangeError('Invalid ' + path);
      out[key][id] = v;
    }
  }
  checkConstraints(out); return out;
}
/** Merge any number of partial layers, in precedence order, into fresh full settings. */
export function resolvePlaybackSettings(...layers) {
  const out = copy(DEFAULT_PLAYBACK_SETTINGS);
  for (const layer of layers) {
    if (layer === undefined) continue;
    const patch = validatePlaybackSettings(layer);
    for (const group of definition.groups) if (patch[group.id]) Object.assign(out[group.id],patch[group.id]);
  }
  checkConstraints(out); return out;
}
export function playbackSettingsFromProfile(profile = {}) {
  const out = {};
  const put = (group,field,key) => { if (profile[key] !== undefined) (out[group] ??= {})[field] = profile[key]; };
  for (const name of ['blink','breath','idle']) put(name,'enabled',name);
  put('wind','wind','wind'); put('wind','windBlow','windBlow');
  put('gaze','enabled','saccade'); put('gaze','tracking','lookAt'); put('gaze','follow','follow');
  return out;
}
/** Editable field paths are capability-gated; pinned engines never gain new behavior. */
export function playbackSettingPaths(capabilities, profile) {
  const id = typeof profile === 'string' ? profile : profile?.id;
  if (!capabilities || capabilities.coreRef !== 'canonical' || id === 'comparison-v1') return [];
  const parameters = new Set(capabilities.parameters || []);
  return definition.groups.flatMap(group => group.fields.filter(field => field.parameters.some(parameter => parameters.has(parameter))).map(field => group.id + '.' + field.id));
}
export function playbackSettingsFields(capabilities, profile) {
  const paths = new Set(capabilities?.playbackSettings ?? playbackSettingPaths(capabilities,profile));
  const id = typeof profile === 'string' ? profile : profile?.id;
  if (id === 'comparison-v1') paths.clear();
  return definition.groups.map(group => {
    const fields = group.fields.map(field => ({...copy(field), supported: paths.has(group.id + '.' + field.id)}));
    return {...copy(group), fields, supported: fields.some(field => field.supported)};
  });
}
/** UI edit helper: keep ordered bounds valid while dragging either endpoint. */
export function updatePlaybackSetting(settings, group, field, value) {
  const next = resolvePlaybackSettings(settings);
  // Validate the individual value before assigning unknown keys or coercing bounds.
  validatePlaybackSettings({[group]:{[field]:value}});
  next[group][field] = value;
  const changed = group + '.' + field;
  for (const constraint of definition.constraints) {
    if (constraint.when && Object.entries(constraint.when).some(([path,expected]) => at(next,path) !== expected)) continue;
    const [low,high] = constraint.paths;
    if (at(next,low) <= at(next,high)) continue;
    const target = changed === high ? low : high;
    const [targetGroup,targetField] = target.split('.');
    next[targetGroup][targetField] = at(next, changed === high ? high : low);
  }
  return resolvePlaybackSettings(next);
}
/** Select supported fields before applying an external stored/full snapshot live. */
export function filterPlaybackSettings(settings, capabilities) {
  const source = validatePlaybackSettings(settings), out = {version: PLAYBACK_SETTINGS_VERSION};
  const paths = new Set(capabilities?.playbackSettings ?? playbackSettingPaths(capabilities));
  for (const group of definition.groups) for (const field of group.fields) {
    if (paths.has(group.id + '.' + field.id) && source[group.id]?.[field.id] !== undefined) (out[group.id] ??= {})[field.id] = source[group.id][field.id];
  }
  return out;
}
