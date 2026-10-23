import type { components } from "@/lib/api-types.gen";

type Schemas = components["schemas"];

export type AgentExecutionProfileCatalogItem =
  Schemas["AgentRuntimeExecutionProfile"];
export type AgentExecutionProfileCatalogDiagnostic =
  Schemas["AgentRuntimeProfileDiagnostic"];
export type AgentExecutionProfileCatalog =
  Schemas["AgentRuntimeProfileCatalogResponse"];
export type AgentExecutionIngress = AgentExecutionProfileCatalogItem["ingress"];
export type AgentExecutionAvailabilityCategory =
  AgentExecutionProfileCatalogItem["availability_category"];
export type AgentExecutionProviderReadiness =
  AgentExecutionProfileCatalogItem["provider_readiness"];

const EXECUTION_INGRESSES = new Set<AgentExecutionIngress>([
  "api",
  "cli_session",
  "remote_agent",
]);
const AVAILABILITY_CATEGORIES = new Set<AgentExecutionAvailabilityCategory>([
  "available",
  "context_required",
  "policy_denied",
  "provider_unavailable",
  "ingress_unavailable",
]);
const PROVIDER_READINESS_STATES = new Set<AgentExecutionProviderReadiness>([
  "unknown",
  "ready",
  "unavailable",
  "not_applicable",
]);

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && Boolean(value.trim());
}

export function isAgentExecutionProfileCatalog(
  value: unknown,
): value is AgentExecutionProfileCatalog {
  if (!value || typeof value !== "object") return false;
  const catalog = value as Record<string, unknown>;
  if (
    !nonEmptyString(catalog.agent_id) ||
    !nonEmptyString(catalog.agent_revision_id) ||
    !nonEmptyString(catalog.agent_team_id) ||
    catalog.acting_role !== "operator" ||
    !nonEmptyString(catalog.default_execution_profile_id) ||
    (catalog.context_source !== "agent_revision" &&
      catalog.context_source !== "session_binding") ||
    !Array.isArray(catalog.profiles) ||
    !Array.isArray(catalog.diagnostics)
  ) {
    return false;
  }
  const profileIds = new Set<string>();
  for (const raw of catalog.profiles) {
    if (!raw || typeof raw !== "object") return false;
    const profile = raw as Record<string, unknown>;
    if (
      !nonEmptyString(profile.profile_id) ||
      profileIds.has(profile.profile_id) ||
      !nonEmptyString(profile.name) ||
      typeof profile.is_default !== "boolean" ||
      !nonEmptyString(profile.acting_subagent_id) ||
      !nonEmptyString(profile.provider) ||
      (profile.model != null && typeof profile.model !== "string") ||
      (profile.effort != null && typeof profile.effort !== "string") ||
      !EXECUTION_INGRESSES.has(profile.ingress as AgentExecutionIngress) ||
      typeof profile.available !== "boolean" ||
      !AVAILABILITY_CATEGORIES.has(
        profile.availability_category as AgentExecutionAvailabilityCategory,
      ) ||
      !PROVIDER_READINESS_STATES.has(
        profile.provider_readiness as AgentExecutionProviderReadiness,
      )
    ) {
      return false;
    }
    profileIds.add(profile.profile_id);
  }
  return catalog.diagnostics.every((raw) => {
    if (!raw || typeof raw !== "object") return false;
    const diagnostic = raw as Record<string, unknown>;
    return nonEmptyString(diagnostic.profile_id) && nonEmptyString(diagnostic.reason);
  });
}

/**
 * Per-Chat runtime (ExecutionProfile) choice for an Agent-bound Chat. It is a
 * send-time admission value only: the Agent revision, its role Team and the
 * session LLM settings are never rewritten from it.
 */
export type AgentRuntimeSelection = {
  scope: string;
  profileId: string;
};

export function agentRuntimeScope(
  userId: string | null | undefined,
  sessionId: string | null | undefined,
  agentId: string | null | undefined,
): string {
  return JSON.stringify([userId ?? "", sessionId ?? "", agentId ?? ""]);
}

/** Unset/blank means "use the role revision default": never an explicit value. */
export function normalizeRuntimeProfileId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

export function selectedRuntimeProfileId(
  selection: AgentRuntimeSelection | null,
  scope: string,
): string | null {
  return selection && selection.scope === scope
    ? normalizeRuntimeProfileId(selection.profileId)
    : null;
}

export function runtimeProfileById(
  catalog: AgentExecutionProfileCatalog | null | undefined,
  profileId: string | null | undefined,
): AgentExecutionProfileCatalogItem | null {
  const normalized = normalizeRuntimeProfileId(profileId);
  if (!catalog || !normalized) return null;
  return catalog.profiles.find((profile) => profile.profile_id === normalized) ?? null;
}

export function resolvedRuntimeProfile(
  catalog: AgentExecutionProfileCatalog | null | undefined,
  selectedProfileId: string | null | undefined,
): AgentExecutionProfileCatalogItem | null {
  if (!catalog) return null;
  return runtimeProfileById(
    catalog,
    normalizeRuntimeProfileId(selectedProfileId) ?? catalog.default_execution_profile_id,
  );
}

export function canUseConversationIngress(
  profile: AgentExecutionProfileCatalogItem | null | undefined,
): boolean {
  return Boolean(
    profile &&
      profile.ingress === "api" &&
      profile.available &&
      profile.provider_readiness !== "unavailable",
  );
}

export function executionIngressLabel(ingress: AgentExecutionIngress): string {
  if (ingress === "api") return "会話/API";
  if (ingress === "cli_session") return "CLIセッション";
  return "Remote Agent";
}

export function executionAvailabilityLabel(
  profile: AgentExecutionProfileCatalogItem,
): string {
  if (profile.availability_category === "available") return "入口の条件を確認済み";
  if (profile.availability_category === "context_required") return "作業場所の指定が必要";
  if (profile.availability_category === "policy_denied") return "実行ポリシーで拒否";
  if (profile.availability_category === "provider_unavailable") return "providerを利用できません";
  return "この入口は利用できません";
}

export function providerReadinessLabel(
  readiness: AgentExecutionProviderReadiness,
): string {
  if (readiness === "ready") return "provider確認済み";
  if (readiness === "unavailable") return "provider利用不可";
  if (readiness === "not_applicable") return "provider確認対象外";
  return "provider未確認";
}
