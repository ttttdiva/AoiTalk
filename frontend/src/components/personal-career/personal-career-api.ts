import type { components } from "@/lib/api-types.gen";

export type CareerSourceOut = components["schemas"]["CareerSourceOut"];
export type CareerSourcePage = components["schemas"]["CareerSourcePage"];
export type CareerAvailableSourceOut = components["schemas"]["CareerAvailableSourceOut"];
export type CareerAvailableSourcePage = components["schemas"]["CareerAvailableSourcePage"];
export type CareerSourceRegister = components["schemas"]["CareerSourceRegister"];
export type CareerSourceSelection = components["schemas"]["CareerSourceSelection"];
export type CareerProfileVersionOut = components["schemas"]["CareerProfileVersionOut"];
export type CareerProfileVersionPage = components["schemas"]["CareerProfileVersionPage"];
export type CareerProfileCreate = components["schemas"]["CareerProfileCreate"];
export type CareerProfileConfirmation = components["schemas"]["CareerProfileConfirmation"];
export type CareerProfileConfirmationOut = components["schemas"]["CareerProfileConfirmationOut"];
export type CareerContextRequest = components["schemas"]["CareerContextRequest"];
export type CareerContextOut = components["schemas"]["CareerContextOut"];
export type PersonalAgentInvokeRequest = components["schemas"]["PersonalAgentInvokeRequest"];
export type PersonalAgentInvokeResponse = components["schemas"]["PersonalAgentInvokeResponse"];

const BASE = "/api/python-proxy/personal-career";
const AGENT_BASE = "/api/python-proxy/personal-agents";

export class PersonalCareerHttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = "PersonalCareerHttpError";
  }
}

export function isAmbiguousCareerWrite(error: unknown): boolean {
  return !(error instanceof PersonalCareerHttpError) || error.status >= 500 || error.status === 408;
}

async function ownerFetch<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    credentials: "include",
    cache: "no-store",
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => ({}));
    const detail = payload && typeof payload === "object"
      ? (payload as Record<string, unknown>).detail : undefined;
    throw new PersonalCareerHttpError(response.status,
      typeof detail === "string" ? detail : `キャリアAPIに接続できませんでした（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

async function careerFetch<T>(path: string, init?: RequestInit): Promise<T> {
  return ownerFetch<T>(BASE, path, init);
}

export function newCareerIdempotencyKey(): string {
  return `ui-${crypto.randomUUID()}`;
}

export const personalCareerApi = {
  sources: (signal?: AbortSignal) => careerFetch<CareerSourcePage>("/sources?limit=200", { signal }),
  availableSources: (signal?: AbortSignal) => careerFetch<CareerAvailableSourcePage>("/sources/available?limit=200", { signal }),
  registerSource: (body: CareerSourceRegister) => careerFetch<CareerSourceOut>(
    "/sources", { method: "POST", body: JSON.stringify(body) },
  ),
  selectSource: (sourceRef: string, body: CareerSourceSelection) => careerFetch<CareerSourceOut>(
    `/sources/${encodeURIComponent(sourceRef)}/selection`, { method: "POST", body: JSON.stringify(body) },
  ),
  versions: (signal?: AbortSignal) => careerFetch<CareerProfileVersionPage>("/profile-versions?limit=100", { signal }),
  version: (versionRef: string, signal?: AbortSignal) => careerFetch<CareerProfileVersionOut>(
    `/profile-versions/${encodeURIComponent(versionRef)}`, { signal },
  ),
  createVersion: (body: CareerProfileCreate) => careerFetch<CareerProfileVersionOut>(
    "/profile-versions", { method: "POST", body: JSON.stringify(body) },
  ),
  confirmVersion: (versionRef: string, body: CareerProfileConfirmation) => careerFetch<CareerProfileConfirmationOut>(
    `/profile-versions/${encodeURIComponent(versionRef)}/confirm`,
    { method: "POST", body: JSON.stringify(body) },
  ),
  context: (body: CareerContextRequest) => careerFetch<CareerContextOut>(
    "/context", { method: "POST", body: JSON.stringify(body) },
  ),
  invokePersonalAgent: (body: PersonalAgentInvokeRequest) => ownerFetch<PersonalAgentInvokeResponse>(
    AGENT_BASE, "/invoke", { method: "POST", body: JSON.stringify(body) },
  ),
};
