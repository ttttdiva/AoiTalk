import type { components } from "@/lib/api-types.gen";
import type { MediaPlatform } from "@/lib/media-operations-api";

type Schemas = components["schemas"];

/** Account type and lifecycle status have backend defaults; callers may omit
 * them and let the server apply the canonical values. */
type OptionalDefaults<T, K extends keyof T> = Omit<T, K> &
  Partial<Pick<T, K>>;

export type DraftFactState =
  Schemas["DraftFactState"];
export type PlatformCapabilityStatus =
  Schemas["PlatformCapabilityStatus"];
export type PlatformCredentialStatus =
  Schemas["PlatformCredentialStatus"];

export type PersonaBulkDraftSlot =
  Schemas["PersonaBulkDraftSlotWire"];
export type PersonaBulkDraftPreview =
  Schemas["PersonaBulkDraftPreviewResponse"];
export type PersonaBulkDraftApplyResult =
  Schemas["PersonaBulkDraftApplyResponse"];

export type PersonaBulkDraftImportInput =
  Schemas["PersonaBulkDraftImportRequest"];
export type PersonaBulkDraftCorrectionInput =
  Schemas["PersonaBulkDraftCorrectionRequest"];

export type PlatformAccount =
  Schemas["PlatformAccountSummaryResponse"];
export type PlatformAccountDetail =
  Schemas["PlatformAccountDetailResponse"];
export type PlatformAccountCreateInput = OptionalDefaults<
  Schemas["PlatformAccountCreateRequest"],
  "account_type" | "status"
>;
export type PlatformAccountRevisionInput =
  Schemas["PlatformAccountRevisionCreateRequest"];

/** Credential Vault DTOs are generated from the FastAPI contract.  The
 * generated projection intentionally contains status/capability metadata
 * only; secret package values, ciphertext, keys, references, and digests do
 * not cross the browser boundary. */
export type CredentialConnectionType =
  Schemas["MediaCredentialResponse"]["connection_type"];
export type CredentialVaultStatus =
  Schemas["MediaCredentialResponse"]["status"];
export type CredentialCapabilityStatus =
  Schemas["MediaCredentialCapabilitiesResponse"]["identity"];
export type PlatformCredentialCapabilities =
  Schemas["MediaCredentialCapabilitiesResponse"];
export type PlatformCredentialDetail =
  Schemas["MediaCredentialResponse"];
export type PlatformCredentialAuditEvent =
  Schemas["MediaCredentialAuditResponse"];

export type CharacterPlatformConnectionInput = {
  platform: MediaPlatform;
  account_ref: string;
  display_name: string;
  connection_type: CredentialConnectionType;
  package: File;
};

export type CharacterPlatformConnectionResponse =
  Schemas["MediaCredentialCommandResponse"];

export type PlatformCredentialMutationResponse = PlatformCredentialDetail;

export type PlatformCredentialAuditResponse =
  Schemas["MediaCredentialAuditResponse"][];

export class MediaOperationsSetupApiError extends Error {
  readonly status: number;
  readonly detail?: unknown;
  readonly body?: unknown;

  constructor(
    message: string,
    options: {
      status: number;
      detail?: unknown;
      body?: unknown;
    },
  ) {
    // Response bodies are untrusted provider data. Never retain them (or a
    // server detail that may include a secret) in an Error object that React
    // state, telemetry, or an error boundary could later render.
    void message;
    super(safeRequestErrorMessage(options.status));
    this.name = "MediaOperationsSetupApiError";
    this.status = options.status;
    this.detail = undefined;
    this.body = undefined;
  }
}

function safeRequestErrorMessage(
  status: number,
): string {
  if (status === 401 || status === 403) return "権限がありません。";
  if (status === 404) return "対象が見つかりません。";
  if (status === 409) return "状態が更新されています。";
  if (status === 413) return "アップロードサイズが上限を超えています。";
  if (status === 422) return "入力を確認してください。";
  if (status >= 500) return "サーバーで処理できませんでした。";
  return "リクエストを処理できませんでした。";
}

function isRecord(
  value: unknown,
): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value)
  );
}

async function parseResponse(
  response: Response,
): Promise<unknown> {
  if (response.status === 204) {
    return undefined;
  }

  const contentType =
    response.headers.get("content-type") ?? "";

  if (contentType.includes("json")) {
    return response
      .json()
      .catch(() => undefined);
  }

  const text = await response
    .text()
    .catch(() => "");

  if (!text) {
    return undefined;
  }

  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  idempotencyKey?: string,
): Promise<T> {
  const headers = new Headers(
    init.headers,
  );

  if (
    init.body &&
    !(init.body instanceof FormData) &&
    !headers.has("content-type")
  ) {
    headers.set(
      "content-type",
      "application/json",
    );
  }

  if (idempotencyKey?.trim()) {
    headers.set(
      "idempotency-key",
      idempotencyKey.trim(),
    );
  }

  const response = await fetch(
    `/api/python-proxy${path}`,
    {
      ...init,
      cache: "no-store",
      credentials: "include",
      headers,
    },
  );

  const body = await parseResponse(
    response,
  );

  if (!response.ok) {
    const record = isRecord(body)
      ? body
      : {};
    const detail =
      record.detail ??
      record.message ??
      record.error;

    const message =
      typeof detail === "string"
        ? detail
        : response.statusText ||
          `Media Operations setup request failed (${response.status})`;

    throw new MediaOperationsSetupApiError(
      message,
      {
        status: response.status,
        detail,
        body,
      },
    );
  }

  return body as T;
}

function jsonBody(
  value: unknown,
): RequestInit {
  return {
    body: JSON.stringify(value),
    headers: {
      "content-type": "application/json",
    },
  };
}

function encodeId(
  id: string,
): string {
  return encodeURIComponent(id);
}

export const mediaOperationsSetupApi = {
  async importPersonaBulkDraft(
    input: PersonaBulkDraftImportInput,
    idempotencyKey: string,
  ): Promise<PersonaBulkDraftPreview> {
    return request<PersonaBulkDraftPreview>(
      "/operations/media/persona-drafts",
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  async getPersonaBulkDraft(
    id: string,
  ): Promise<PersonaBulkDraftPreview> {
    return request<PersonaBulkDraftPreview>(
      `/operations/media/persona-drafts/${encodeId(id)}`,
    );
  },

  async correctPersonaBulkDraft(
    id: string,
    input: PersonaBulkDraftCorrectionInput,
  ): Promise<PersonaBulkDraftPreview> {
    return request<PersonaBulkDraftPreview>(
      `/operations/media/persona-drafts/${encodeId(id)}`,
      {
        method: "PUT",
        ...jsonBody(input),
      },
    );
  },

  async applyPersonaBulkDraft(
    id: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): Promise<PersonaBulkDraftApplyResult> {
    return request<PersonaBulkDraftApplyResult>(
      `/operations/media/persona-drafts/${encodeId(id)}/apply`,
      {
        method: "POST",
        ...jsonBody({
          expected_version:
            expectedVersion,
        }),
      },
      idempotencyKey,
    );
  },

  async listPlatformAccounts(
    projectId?: string | null,
    characterId?: string | null,
  ): Promise<PlatformAccount[]> {
    const query =
      new URLSearchParams();

    if (projectId) {
      query.set(
        "project_id",
        projectId,
      );
    }

    if (characterId) {
      query.set(
        // MediaOps stores the Character association on the existing
        // Persona-scoped PlatformAccount row. The Character UI owns this ID,
        // while the HTTP contract keeps the established persona_id name.
        "persona_id",
        characterId,
      );
    }

    const suffix =
      query.toString()
        ? `?${query.toString()}`
        : "";

    return request<PlatformAccount[]>(
      `/operations/media/platform-accounts${suffix}`,
    );
  },

  async createPlatformAccount(
    input: PlatformAccountCreateInput,
    idempotencyKey: string,
  ): Promise<PlatformAccountDetail> {
    return request<PlatformAccountDetail>(
      "/operations/media/platform-accounts",
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  async getPlatformAccount(
    id: string,
  ): Promise<PlatformAccountDetail> {
    return request<PlatformAccountDetail>(
      `/operations/media/platform-accounts/${encodeId(id)}`,
    );
  },

  async appendPlatformAccountRevision(
    id: string,
    input: PlatformAccountRevisionInput,
    idempotencyKey: string,
  ): Promise<
    Schemas["PlatformAccountRevisionResponse"]
  > {
    return request<
      Schemas["PlatformAccountRevisionResponse"]
    >(
      `/operations/media/platform-accounts/${encodeId(id)}/revisions`,
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  /** Add a Character-scoped platform connection and its encrypted package. */
  async addCharacterPlatformConnection(
    characterId: string,
    input: CharacterPlatformConnectionInput,
    idempotencyKey: string,
  ): Promise<CharacterPlatformConnectionResponse> {
    const form = new FormData();
    form.append("platform", input.platform);
    form.append("account_ref", input.account_ref);
    form.append("display_name", input.display_name);
    form.append("connection_type", input.connection_type);
    // ``file`` is the generated OpenAPI multipart field.  The backend keeps
    // accepting the legacy ``package`` alias for older clients, but new UI
    // requests must follow the canonical contract.
    form.append("file", input.package, input.package.name);

    return request<CharacterPlatformConnectionResponse>(
      `/operations/media/characters/${encodeId(characterId)}/platform-connections`,
      {
        method: "POST",
        body: form,
      },
      idempotencyKey,
    );
  },

  async getPlatformCredential(
    accountId: string,
  ): Promise<PlatformCredentialDetail> {
    return request<PlatformCredentialDetail>(
      `/operations/media/platform-accounts/${encodeId(accountId)}/credential`,
    );
  },

  /** Create/replace a credential package for an existing account. */
  async createPlatformCredential(
    accountId: string,
    input: {
      package: File;
      connection_type: CredentialConnectionType;
    },
    idempotencyKey: string,
  ): Promise<PlatformCredentialMutationResponse> {
    const form = new FormData();
    form.append("file", input.package, input.package.name);
    form.append("connection_type", input.connection_type);

    return request<PlatformCredentialMutationResponse>(
      `/operations/media/platform-accounts/${encodeId(accountId)}/credential`,
      {
        method: "POST",
        body: form,
      },
      idempotencyKey,
    );
  },

  async rotatePlatformCredential(
    accountId: string,
    input: {
      package: File;
      expected_revision: number;
      connection_type: CredentialConnectionType;
    },
    idempotencyKey: string,
  ): Promise<PlatformCredentialMutationResponse> {
    const form = new FormData();
    form.append("file", input.package, input.package.name);
    form.append(
      "expected_revision",
      String(input.expected_revision),
    );
    form.append("connection_type", input.connection_type);

    return request<PlatformCredentialMutationResponse>(
      `/operations/media/platform-accounts/${encodeId(accountId)}/credential/rotate`,
      {
        method: "POST",
        body: form,
      },
      idempotencyKey,
    );
  },

  async verifyPlatformCredential(
    accountId: string,
    expectedRevision: number,
    idempotencyKey: string,
  ): Promise<PlatformCredentialMutationResponse> {
    return request<PlatformCredentialMutationResponse>(
      `/operations/media/platform-accounts/${encodeId(accountId)}/credential/verify`,
      {
        method: "POST",
        ...jsonBody({
          expected_revision: expectedRevision,
        }),
      },
      idempotencyKey,
    );
  },

  async disablePlatformCredential(
    accountId: string,
    expectedRevision: number,
    idempotencyKey: string,
  ): Promise<PlatformCredentialMutationResponse> {
    return request<PlatformCredentialMutationResponse>(
      `/operations/media/platform-accounts/${encodeId(accountId)}/credential/disable`,
      {
        method: "POST",
        ...jsonBody({
          expected_revision: expectedRevision,
        }),
      },
      idempotencyKey,
    );
  },

  async listPlatformCredentialAudit(
    accountId: string,
  ): Promise<PlatformCredentialAuditEvent[]> {
    const payload = await request<PlatformCredentialAuditResponse>(
      `/operations/media/platform-accounts/${encodeId(accountId)}/credential/audit`,
    );
    return payload;
  },
};
