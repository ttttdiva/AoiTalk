/**
 * How a model route is paid for, from Free Team credentials (billing_mode is
 * per credential, not per provider). Anything that cannot be classified is
 * treated as paid, so it never hides a charge.
 */
export type BillingClass = "free" | "subscription" | "paid" | "local";

export const BILLING_LABELS: Record<BillingClass, string> = {
  free: "無料枠",
  subscription: "サブスク",
  paid: "従量課金",
  local: "ローカル",
};

const LOCAL_PROVIDERS = new Set(["ollama", "sglang", "openai_compatible_local", "llama_cpp", "llama-cpp", "lmstudio", "mage_vl"]);
const FREE_BILLING_MODES = new Set(["complimentary", "free_tier", "promo_credit"]);

export type FreeTeamCredential = { id: string; provider?: string; billing_mode?: string };
export type FreeTeamCandidate = { id: string; provider: string; model: string; credential_profile_id?: string; enabled?: boolean; quota_pool_ids?: string[] };
export type FreeTeamQuotaPool = { id: string; credential_profile_id?: string; available?: number | null; daily_cap?: number | null; configured_daily_cap?: number | null; metric_type?: string; stop_reason?: string | null };

export type BillingContext = {
  /** `provider/model` → free quota pools that pay for it. */
  freeRoutes: Map<string, FreeTeamQuotaPool[]>;
};

export function routeKey(provider: string, model: string): string {
  return `${provider}/${model}`.toLowerCase();
}

export function buildBillingContext(credentials: FreeTeamCredential[], candidates: FreeTeamCandidate[], pools: FreeTeamQuotaPool[]): BillingContext {
  const billingByCredential = new Map(credentials.map(credential => [credential.id, credential.billing_mode ?? ""]));
  const poolsByCredential = new Map<string, FreeTeamQuotaPool[]>();
  for (const pool of pools) {
    if (!pool.credential_profile_id) continue;
    poolsByCredential.set(pool.credential_profile_id, [...(poolsByCredential.get(pool.credential_profile_id) ?? []), pool]);
  }
  const freeRoutes = new Map<string, FreeTeamQuotaPool[]>();
  for (const candidate of candidates) {
    if (candidate.enabled === false || !candidate.credential_profile_id) continue;
    const mode = billingByCredential.get(candidate.credential_profile_id);
    const candidatePools = pools.filter(pool => candidate.quota_pool_ids?.includes(pool.id));
    const credentialPools = poolsByCredential.get(candidate.credential_profile_id) ?? [];
    // Complimentary OpenAI daily tokens have a quota pool but no separate credential row.
    const free = mode ? FREE_BILLING_MODES.has(mode) : credentialPools.length > 0;
    if (free) freeRoutes.set(routeKey(candidate.provider, candidate.model), candidatePools.length ? candidatePools : credentialPools);
  }
  return { freeRoutes };
}

export function routeBillingClass(provider: string, model: string, context: BillingContext): BillingClass {
  const id = provider.toLowerCase();
  if (id === "routing-profile" || id === "routing_profile") return "free";
  if (LOCAL_PROVIDERS.has(id)) return "local";
  if (context.freeRoutes.has(routeKey(provider, model))) return "free";
  if (id.endsWith("-cli")) return "subscription";
  return "paid";
}

/** Free quota left for a route, as short text; null when it is not quota-backed. */
export function freeQuotaText(provider: string, model: string, context: BillingContext): string | null {
  const pools = context.freeRoutes.get(routeKey(provider, model));
  if (!pools?.length) return null;
  const pool = pools.find(item => item.stop_reason) ?? pools[0];
  if (pool.stop_reason) return "残量なし";
  if (typeof pool.available !== "number") return null;
  return `残 ${compactNumber(pool.available)}`;
}

export function compactNumber(value: number): string {
  if (value >= 1_000_000) return `${Math.round(value / 100_000) / 10}M`;
  if (value >= 1_000) return `${Math.round(value / 100) / 10}k`;
  return String(Math.round(value * 100) / 100);
}
