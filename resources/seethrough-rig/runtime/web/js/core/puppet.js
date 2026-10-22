// Puppet: canonical model + parameters + deformer + physics + animator + renderer.
// Standalone player/editor and packaged hosts use the same lifecycle adapter.
import { RuntimeHost, resolveCoreRef } from '../runtime/host.js';
import { canonicalEngine } from '../runtime/engine.js';
export class Puppet extends RuntimeHost {
  static resolveEngine(options = {}) {
    const ref = resolveCoreRef(options);
    if (ref !== 'canonical') throw new Error('Puppet uses canonical engine; use RigRuntime for a pinned core_ref');
    return canonicalEngine;
  }
}
