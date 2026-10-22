import { Puppet } from '../core/puppet.js';
import { canonicalEngine } from './engine.js';
import { resolveCoreRef, capabilitiesFor, BASELINE_CORE_REF } from './host.js';
import { Params } from '../runtime-baseline/params.js';
import { Deformer } from '../runtime-baseline/deform.js';
import { Physics } from '../runtime-baseline/physics.js';
import { Animator } from '../runtime-baseline/motion.js';
export { ABI, VERSION, BASELINE_CORE_REF, SUPPORTED_MODEL_VERSIONS, SUPPORTED_MODEL_FEATURES } from './host.js';
const baselineEngine = { Params, Deformer, Physics, Animator, coreRef: BASELINE_CORE_REF };
function engineFor(options = {}) {
  const ref = resolveCoreRef(options);
  if (ref === 'canonical') return canonicalEngine;
  if (ref === BASELINE_CORE_REF) return baselineEngine;
  throw new Error('Unsupported runtime core_ref: ' + ref);
}
export function deriveCapabilities(model, coreRef = 'canonical') { return capabilitiesFor(model, engineFor({coreRef})); }
export class RigRuntime extends Puppet {
  static resolveEngine(options = {}) { return engineFor(options); }
}

export { PLAYBACK_SETTINGS_VERSION, PLAYBACK_SETTINGS_DEFINITION, DEFAULT_PLAYBACK_SETTINGS, validatePlaybackSettings, resolvePlaybackSettings, playbackSettingsFields, updatePlaybackSetting, filterPlaybackSettings } from './playback-settings.js';
export { createPlaybackSettingsPanel } from './playback-settings-panel.js';
