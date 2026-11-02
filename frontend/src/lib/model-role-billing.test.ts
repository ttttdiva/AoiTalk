import { describe, expect, it } from "vitest";
import { buildBillingContext, freeQuotaText, routeBillingClass } from "./model-role-billing";

// Shapes and values as returned by /free-team/settings and /free-team/usage today.
const context = buildBillingContext(
  [
    { id: "codex-spark", provider: "codex-cli", billing_mode: "subscription_cli" },
    { id: "gemini-free", provider: "gemini", billing_mode: "free_tier" },
    { id: "openrouter-free", provider: "openrouter", billing_mode: "free_tier" },
  ],
  [
    { id: "openai-luna", provider: "openai", model: "gpt-5.6-luna", credential_profile_id: "openai_api_key", enabled: true, quota_pool_ids: ["openai-luna-daily"] },
    { id: "gemini-free-flash", provider: "gemini", model: "gemini-2.5-flash", credential_profile_id: "gemini-free", enabled: true, quota_pool_ids: ["gemini-free-rpd"] },
    { id: "openrouter-free-router", provider: "openrouter", model: "openrouter/free", credential_profile_id: "openrouter-free", enabled: true },
    { id: "codex-spark", provider: "codex-cli", model: "gpt-5.3-codex-spark", credential_profile_id: "codex-spark", enabled: true },
  ],
  [
    { id: "openai-luna-daily", credential_profile_id: "openai_api_key", available: 6_777_323 },
    { id: "gemini-free-rpd", credential_profile_id: "gemini-free", available: 0, stop_reason: "budget_exhausted" },
  ],
);

describe("routeBillingClass", () => {
  it("classifies by the credential that pays, not by provider", () => {
    expect(routeBillingClass("openai", "gpt-5.6-luna", context)).toBe("free");
    expect(routeBillingClass("openai", "gpt-5.6-sol", context)).toBe("paid");
    expect(routeBillingClass("openrouter", "openrouter/free", context)).toBe("free");
    expect(routeBillingClass("openrouter", "deepseek/deepseek-v4.1-flash-1017", context)).toBe("paid");
    expect(routeBillingClass("codex-cli", "gpt-5.3-codex-spark", context)).toBe("subscription");
    expect(routeBillingClass("claude-cli", "opus", context)).toBe("subscription");
    expect(routeBillingClass("ollama", "gpt-oss:20b", context)).toBe("local");
    expect(routeBillingClass("routing-profile", "free-team", context)).toBe("free");
    expect(routeBillingClass("unknown-provider", "x", context)).toBe("paid");
  });

  it("shows remaining free quota and exhausted pools", () => {
    expect(freeQuotaText("openai", "gpt-5.6-luna", context)).toBe("残 6.8M");
    expect(freeQuotaText("gemini", "gemini-2.5-flash", context)).toBe("残量なし");
    expect(freeQuotaText("openai", "gpt-5.6-sol", context)).toBeNull();
  });
});
