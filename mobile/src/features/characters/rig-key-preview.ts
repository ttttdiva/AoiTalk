import type { MouthInput, RigRuntime, RuntimeCapabilities } from '@aoitalk/seethrough-rig-runtime';

export const RIG_VOWELS = ['a', 'i', 'u', 'e', 'o'] as const;
export type RigVowel = typeof RIG_VOWELS[number];
export type RigKeyPreview = { eyes: 0 | 0.5 | 1 | 'jitome' | null; vowel: RigVowel | null };
export const EMPTY_RIG_KEY_PREVIEW: RigKeyPreview = { eyes: null, vowel: null };
const EYE_PARAMETERS = ['EyeLOpen', 'EyeROpen'] as const;

export function rigKeyPreviewCapabilities(caps: Pick<RuntimeCapabilities, 'parameters' | 'mouthVowels'> | null) {
  return {
    eyes: EYE_PARAMETERS.filter((key) => caps?.parameters?.includes(key)),
    jitome: Boolean(caps?.parameters?.includes('EyeJitome')),
    vowels: RIG_VOWELS.filter((vowel) => caps?.mouthVowels?.includes(vowel)),
  };
}

export function isRigJitomePreviewActive(caps: RuntimeCapabilities, preview?: RigKeyPreview) {
  return preview?.eyes === 'jitome' && rigKeyPreviewCapabilities(caps).jitome;
}

export function isRigKeyPreviewActive(caps: RuntimeCapabilities, preview?: RigKeyPreview) {
  const supported = rigKeyPreviewCapabilities(caps);
  return Boolean(preview && (
    (preview.eyes === 'jitome' ? supported.jitome : preview.eyes !== null && supported.eyes.length > 0) ||
    preview.vowel !== null && supported.vowels.includes(preview.vowel)
  ));
}

/** Ephemeral host inputs only. The canonical runtime owns key selection/rendering. */
export function createRigKeyPreviewController() {
  let hadEyes = false;
  let hadJitome = false;
  let wasActive = false;
  return {
    apply(runtime: Pick<RigRuntime, 'capabilities' | 'setParameters' | 'setMouth'>, preview: RigKeyPreview | undefined, mouth: MouthInput) {
      const supported = rigKeyPreviewCapabilities(runtime.capabilities);
      const eyes = preview?.eyes ?? null;
      const vowel = preview?.vowel && supported.vowels.includes(preview.vowel) ? preview.vowel : null;
      const hasJitome = eyes === 'jitome' && supported.jitome;
      const hasEyes = eyes !== null && (eyes !== 'jitome' || hasJitome) && supported.eyes.length > 0;
      if (hasEyes || hadEyes || hasJitome || hadJitome) {
        // Canonical EyeLOpen/EyeROpen rest values are 1. Restore automatic blink
        // to that base when leaving the explicit pose; no setting is persisted.
        runtime.setParameters({
          ...Object.fromEntries(supported.eyes.map((key) => [key, hasEyes && eyes !== 'jitome' ? eyes : 1])),
          ...(supported.jitome ? { EyeJitome: hasJitome ? 1 : 0 } : {}),
        });
      }
      hadEyes = hasEyes;
      hadJitome = hasJitome;
      runtime.setMouth(vowel ? { open: 1, form: 0, speaking: true, vowel } : mouth);
      const active = hasEyes || hasJitome || vowel !== null;
      const seek = active || wasActive;
      wasActive = active;
      return { active, seek };
    },
  };
}
