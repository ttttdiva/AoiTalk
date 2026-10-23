import type { components } from "@/lib/api-types.gen";

export type FinanceEntry = components["schemas"]["FinanceEntryOut"];
export type FinanceEntryPage = components["schemas"]["FinanceEntryPage"];
export type FinanceEntryCreate = components["schemas"]["FinanceEntryCreate"];
export type FinanceEntryPreviewRequest = components["schemas"]["FinanceEntryPreviewRequest"];
export type FinanceEntryPreviewResponse = components["schemas"]["FinanceEntryPreviewResponse"];
export type FinanceEntryWriteResponse = components["schemas"]["FinanceEntryWriteResponse"];
export type FinanceDatasetAccept = components["schemas"]["FinanceDatasetAccept"];
export type FinanceDatasetOut = components["schemas"]["FinanceDatasetOut"];
export type FinanceDatasetPage = components["schemas"]["FinanceDatasetPage"];
export type FinanceSummaryResponse = components["schemas"]["FinanceSummaryResponse"];
export type ReceiptSource = components["schemas"]["ReceiptSourceOut"];
export type ReceiptCandidate = components["schemas"]["ReceiptCandidateOut"];
export type ReceiptCandidateProposal = components["schemas"]["ReceiptCandidateProposal"];
export type ReceiptCandidateProposalOut = components["schemas"]["ReceiptCandidateProposalOut"];
export type ReceiptConfirmationPreview = components["schemas"]["ReceiptConfirmationPreview"];
export type ReceiptConfirmationPreviewOut = components["schemas"]["ReceiptConfirmationPreviewOut"];
export type PersonalAgentProfile = components["schemas"]["PersonalAgentProfileOut"];
export type PersonalAgentProfiles = components["schemas"]["PersonalAgentProfilesOut"];
export type PersonalAgentInvokeRequest = components["schemas"]["PersonalAgentInvokeRequest"];
export type PersonalAgentInvokeResponse = components["schemas"]["PersonalAgentInvokeResponse"];
export type ReceiptCandidateConfirmation = components["schemas"]["ReceiptCandidateConfirmation"];
export type ReceiptCandidateAcceptanceOut = components["schemas"]["ReceiptCandidateAcceptanceOut"];
export type ReceiptSourcePreviewOut = components["schemas"]["ReceiptSourcePreviewOut"];

const BASE = "/api/python-proxy/personal-finance";
const AGENT_BASE = "/api/python-proxy/personal-agents";

export class PersonalFinanceHttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = "PersonalFinanceHttpError";
  }
}

export function isAmbiguousFinanceWrite(error: unknown): boolean {
  return !(error instanceof PersonalFinanceHttpError) || error.status >= 500 || error.status === 408;
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
    throw new PersonalFinanceHttpError(response.status,
      typeof detail === "string" ? detail : `個人財務APIに接続できませんでした（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

async function financeFetch<T>(path: string, init?: RequestInit): Promise<T> {
  return ownerFetch<T>(BASE, path, init);
}

export function newFinanceIdempotencyKey(): string {
  return `ui-${crypto.randomUUID()}`;
}

export const personalFinanceApi = {
  entries: (signal?: AbortSignal) => financeFetch<FinanceEntryPage>("/entries?limit=1000", { signal }),
  previewEntry: (body: FinanceEntryPreviewRequest, signal?: AbortSignal) => financeFetch<FinanceEntryPreviewResponse>(
    "/entries/preview", { method: "POST", body: JSON.stringify(body), signal },
  ),
  createEntry: (body: FinanceEntryCreate) => financeFetch<FinanceEntryWriteResponse>(
    "/entries", { method: "POST", body: JSON.stringify(body) },
  ),
  datasets: (signal?: AbortSignal) => financeFetch<FinanceDatasetPage>("/datasets?limit=100", { signal }),
  acceptDataset: (body: FinanceDatasetAccept) => financeFetch<FinanceDatasetOut>(
    "/datasets/accept", { method: "POST", body: JSON.stringify(body) },
  ),
  summary: (datasetRef: string, month: string, signal?: AbortSignal) => financeFetch<FinanceSummaryResponse>(
    `/datasets/${encodeURIComponent(datasetRef)}/summary?month=${encodeURIComponent(month)}`, { signal },
  ),
  receiptSources: (signal?: AbortSignal) => financeFetch<ReceiptSource[]>("/receipts/sources", { signal }),
  receiptCandidates: (sourceRef: string, signal?: AbortSignal) => financeFetch<ReceiptCandidate[]>(
    `/receipts/candidates?source_ref=${encodeURIComponent(sourceRef)}`, { signal },
  ),
  proposeReceipt: (body: ReceiptCandidateProposal) => financeFetch<ReceiptCandidateProposalOut>(
    "/receipts/candidates", { method: "POST", body: JSON.stringify(body) },
  ),
  receiptCandidate: (candidateRef: string, signal?: AbortSignal) => financeFetch<ReceiptCandidate>(
    `/receipts/candidates/${encodeURIComponent(candidateRef)}`, { signal },
  ),
  receiptPreview: (candidateRef: string, signal?: AbortSignal) => financeFetch<ReceiptSourcePreviewOut>(
    `/receipts/candidates/${encodeURIComponent(candidateRef)}/preview`, { signal },
  ),
  previewReceiptConfirmation: (candidateRef: string, body: ReceiptConfirmationPreview) => financeFetch<ReceiptConfirmationPreviewOut>(
    `/receipts/candidates/${encodeURIComponent(candidateRef)}/confirmation-preview`,
    { method: "POST", body: JSON.stringify(body) },
  ),
  confirmReceipt: (candidateRef: string, body: ReceiptCandidateConfirmation) => financeFetch<ReceiptCandidateAcceptanceOut>(
    `/receipts/candidates/${encodeURIComponent(candidateRef)}/confirm`,
    { method: "POST", body: JSON.stringify(body) },
  ),
  invokePersonalAgent: (body: PersonalAgentInvokeRequest) => ownerFetch<PersonalAgentInvokeResponse>(
    AGENT_BASE, "/invoke", { method: "POST", body: JSON.stringify(body) },
  ),
};
