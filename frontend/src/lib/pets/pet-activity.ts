import { motionForPhase, type PetMotion } from "./codex-pet";
const sources = new Map<string, PetMotion>();
const listeners = new Set<() => void>();
const priority: PetMotion[] = ["failed", "running", "waiting", "waving", "idle"];
export function getPetActivity(): PetMotion {
  return priority.find((motion) => [...sources.values()].includes(motion)) ?? "idle";
}
export function publishPetActivity(source: string, phase: string | null): void {
  const previous = getPetActivity();
  if (phase === null) sources.delete(source); else sources.set(source, motionForPhase(phase));
  if (getPetActivity() !== previous) listeners.forEach((listener) => listener());
}
export function subscribePetActivity(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
