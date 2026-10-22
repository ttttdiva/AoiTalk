import type { components } from "./api-types.gen";
import { EmployeeApiError } from "./agent-employees-api";

export type BrandPortfolio = components["schemas"]["BrandPortfolioResponse"];
export type BrandPortfolioDefinition = components["schemas"]["BrandPortfolioDefinitionResponse"];
export type BrandPortfolioSetup = components["schemas"]["BrandPortfolioSetupRequest"];

/** Only reads recover automatically; setup/save commands retain their explicit replay rules. */
export const portfolioReadOptions = {
  keepPreviousData: false,
  revalidateOnFocus: true,
  revalidateOnReconnect: true,
  errorRetryCount: 3,
  errorRetryInterval: 3000,
  shouldRetryOnError: (error: unknown) => {
    const status = error && typeof error === "object" && "status" in error ? error.status : undefined;
    return error instanceof TypeError || status === 0 || typeof status === "number" && status >= 500 && status < 600;
  },
};

async function request<T>(suffix: string, input?: BrandPortfolioSetup, idempotencyKey?: string): Promise<T> {
  const response = await fetch(`/api/python-proxy/operations/media/brand-portfolio${suffix}`, {
    method: input ? "POST" : "GET", credentials: "include", cache: "no-store",
    headers: { "Content-Type": "application/json", ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}) },
    ...(input ? { body: JSON.stringify(input) } : {}),
  }).catch(() => { throw new EmployeeApiError(0); });
  if (!response.ok) throw new EmployeeApiError(response.status);
  const value: unknown = await response.json().catch(() => { throw new EmployeeApiError(502); });
  const record = (input: unknown): input is Record<string, unknown> => Boolean(input) && typeof input === "object" && !Array.isArray(input);
  if (!record(value) || !record(value.source) || typeof value.source.sha256 !== "string" || !/^[a-f0-9]{64}$/iu.test(value.source.sha256) || !Array.isArray(value.brands)) throw new EmployeeApiError(502);
  if (suffix === "/definition") {
    if (!value.brands.every(brand => record(brand) && typeof brand.brand_key === "string" && typeof brand.display_name === "string" && Array.isArray(brand.channels))) throw new EmployeeApiError(502);
  } else {
    if (typeof value.owner_user_id !== "string" || !value.owner_user_id || !["ready", "partial", "not_configured"].includes(String(value.status)) || !value.brands.every(brand => record(brand) && record(brand.definition) && typeof brand.definition.brand_key === "string" && typeof brand.definition.display_name === "string" && Array.isArray(brand.channels) && (brand.project_id === null || typeof brand.project_id === "string") && (brand.persona === null || record(brand.persona) && typeof brand.persona.id === "string"))) throw new EmployeeApiError(502);
  }
  return value as T;
}

export const mediaBrandPortfolioApi = {
  get: () => request<BrandPortfolio>(""),
  definition: () => request<BrandPortfolioDefinition>("/definition"),
  setup: (input: BrandPortfolioSetup, key: string) => request<BrandPortfolio>("/setup", input, key),
};
