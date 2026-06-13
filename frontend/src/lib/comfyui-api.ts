// ComfyUI Management API Client
// ─── Types ───────────────────────────────────────────────────────────

export interface ComfyUIWorkflow {
  name: string;
  path: string;
  is_default: boolean;
  mtime: number;
}

export interface ComfyUIConfig {
  enabled: boolean;
  url: string;
  default_workflow: string;
}

export interface ComfyUIStatus {
  enabled: boolean;
  is_available: boolean;
  url: string;
}

// ─── Fetch helper ────────────────────────────────────────────────────

const API_BASE = "/api/python-proxy/api/comfyui";
const TIMEOUT_MS = 10000;

async function fetchWithTimeout<T>(
  url: string,
  options?: RequestInit,
): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      ...options,
      signal: controller.signal,
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...options?.headers,
      },
    });

    if (res.status === 401) {
      if (typeof window !== "undefined") {
        window.location.href = "/login";
      }
      throw new Error("認証が必要です");
    }

    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(detail.detail || res.statusText);
    }

    if (res.status === 204) return undefined as T;
    return res.json();
  } finally {
    clearTimeout(timeout);
  }
}

function get<T>(path: string): Promise<T> {
  return fetchWithTimeout<T>(`${API_BASE}${path}`);
}

function post<T>(
  path: string,
  body?: unknown,
  headers?: Record<string, string>,
): Promise<T> {
  return fetchWithTimeout<T>(`${API_BASE}${path}`, {
    method: "POST",
    headers: headers,
    body:
      body instanceof FormData
        ? body
        : body !== undefined
          ? JSON.stringify(body)
          : undefined,
  });
}

function put<T>(path: string, body: unknown): Promise<T> {
  return fetchWithTimeout<T>(`${API_BASE}${path}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

function del<T>(path: string): Promise<T> {
  return fetchWithTimeout<T>(`${API_BASE}${path}`, { method: "DELETE" });
}

// ─── API Methods ─────────────────────────────────────────────────────

export const comfyuiApi = {
  getStatus: () => get<ComfyUIStatus & { success: boolean }>("/status"),

  getConfig: () => get<ComfyUIConfig & { success: boolean }>("/config"),

  updateConfig: (data: Partial<ComfyUIConfig>) =>
    put<ComfyUIConfig & { success: boolean }>("/config", {
      enabled: data.enabled,
      url: data.url,
      default_workflow: data.default_workflow,
    }),

  listWorkflows: () =>
    get<{ success: boolean; workflows: ComfyUIWorkflow[] }>("/workflows"),

  uploadWorkflow: (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    // FormDataの場合は Content-Type を自動設定させるため、headersから削除する
    return fetchWithTimeout<{ success: boolean; name: string; path: string }>(
      `${API_BASE}/workflows`,
      {
        method: "POST",
        body: formData,
        // Content-Typeはブラウザが自動的に boundary を含む形式で設定するため、ここでは指定しない
        headers: {},
      },
    );
  },

  saveWorkflow: (name: string, workflow: unknown) =>
    post<{ success: boolean; name: string; path: string }>("/workflows", {
      name,
      workflow,
    }),

  deleteWorkflow: (name: string) =>
    del<{ success: boolean }>(`/workflows/${encodeURIComponent(name)}`),
};
