import { normalizePreferences, type PetPreferences } from "./codex-pet";
const key = (userId: string) => `aoitalk-pet-view-v1:${encodeURIComponent(userId)}`;
/** Only presentation preferences live here. No asset or registration flag is stored. */
export function loadPetPreferences(userId: string): PetPreferences | null {
  const text = localStorage.getItem(key(userId));
  if (!text) return null;
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return normalizePreferences(value);
}
export function savePetPreferences(userId: string, preferences: PetPreferences): void {
  localStorage.setItem(key(userId), JSON.stringify(normalizePreferences(preferences)));
}
