/**
 * 外部AoiTalkサーバー接続プロファイルのモバイルクライアント。
 * バックエンドの /api/remote-servers を fetchApi 経由で叩く。
 */

import { fetchApi, getBaseUrl } from "./api-client";
import { getToken } from "./auth";

export type RemoteCapabilities = {
  version?: string;
  profile?: string;
  features?: Record<string, boolean>;
  server_time?: string;
  user?: { id?: string; username?: string; role?: string } | null;
};

export type RemoteServerProfile = {
  id: string;
  user_id: string;
  name: string;
  base_url: string;
  display_color?: string | null;
  enabled: boolean;
  has_token: boolean;
  last_status?: string | null;
  last_checked_at?: string | null;
  last_capabilities?: RemoteCapabilities | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type CreateRemoteServerInput = {
  name: string;
  base_url: string;
  auth_token?: string | null;
  display_color?: string | null;
  enabled?: boolean;
};

export type UpdateRemoteServerInput = Partial<CreateRemoteServerInput>;

export type ConnectionTestResult = {
  success: boolean;
  status: string;
  capabilities?: RemoteCapabilities;
  error?: string;
};

export async function listRemoteServers(): Promise<RemoteServerProfile[]> {
  const data = await fetchApi<{ profiles?: RemoteServerProfile[] }>(
    "/api/remote-servers",
  );
  return data.profiles ?? [];
}

export async function createRemoteServer(
  input: CreateRemoteServerInput,
): Promise<RemoteServerProfile> {
  const data = await fetchApi<{ profile: RemoteServerProfile }>(
    "/api/remote-servers",
    { method: "POST", body: JSON.stringify(input) },
  );
  return data.profile;
}

export async function updateRemoteServer(
  id: string,
  input: UpdateRemoteServerInput,
): Promise<RemoteServerProfile> {
  const data = await fetchApi<{ profile: RemoteServerProfile }>(
    `/api/remote-servers/${id}`,
    { method: "PATCH", body: JSON.stringify(input) },
  );
  return data.profile;
}

export async function deleteRemoteServer(id: string): Promise<void> {
  await fetchApi<{ success: boolean }>(`/api/remote-servers/${id}`, {
    method: "DELETE",
  });
}

/**
 * 接続テスト。失敗時もボディに status/error を含めて返るため、
 * 502 でも例外化せずレスポンスボディを読む（fetchApi は使わない）。
 */
export async function testRemoteServer(
  id: string,
): Promise<ConnectionTestResult> {
  const token = await getToken();
  const baseUrl = await getBaseUrl();
  try {
    const res = await fetch(`${baseUrl}/api/remote-servers/${id}/test`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    });
    const data = (await res.json().catch(() => ({}))) as ConnectionTestResult;
    if (data && typeof data.success === "boolean") return data;
    return { success: false, status: "error", error: res.statusText };
  } catch (err) {
    return {
      success: false,
      status: "error",
      error: err instanceof Error ? err.message : "接続に失敗しました",
    };
  }
}
