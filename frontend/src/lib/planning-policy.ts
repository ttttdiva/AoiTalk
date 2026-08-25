export const PLANNING_POLICY_STORAGE_KEY = "aoitalk-planning-policy";

export const PLANNING_POLICY_VALUES = [
  "auto",
  "plan_first",
  "direct",
] as const;

export type PlanningPolicy = (typeof PLANNING_POLICY_VALUES)[number];

export const DEFAULT_PLANNING_POLICY: PlanningPolicy = "auto";

const VALID_PLANNING_POLICIES = new Set<string>(PLANNING_POLICY_VALUES);

type PlanningPolicyStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function normalizePlanningPolicy(value: unknown): PlanningPolicy | null {
  if (typeof value !== "string") return null;
  return VALID_PLANNING_POLICIES.has(value) ? (value as PlanningPolicy) : null;
}

export function loadStoredPlanningPolicy(
  storage: PlanningPolicyStorage | null | undefined,
): PlanningPolicy {
  if (!storage) return DEFAULT_PLANNING_POLICY;
  const stored = normalizePlanningPolicy(
    storage.getItem(PLANNING_POLICY_STORAGE_KEY),
  );
  if (!stored) {
    storage.removeItem(PLANNING_POLICY_STORAGE_KEY);
  }
  return stored ?? DEFAULT_PLANNING_POLICY;
}

export function saveStoredPlanningPolicy(
  storage: PlanningPolicyStorage | null | undefined,
  policy: PlanningPolicy,
) {
  if (!storage) return;
  storage.setItem(PLANNING_POLICY_STORAGE_KEY, policy);
}

export function getSettingsPlanningPolicy(
  settings: Record<string, unknown>,
): PlanningPolicy | null {
  const chat = settings.chat;
  if (typeof chat !== "object" || chat === null || Array.isArray(chat)) {
    return null;
  }
  return normalizePlanningPolicy(
    (chat as Record<string, unknown>).planning_policy,
  );
}
