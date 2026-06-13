/**
 * 外部AoiTalkサーバー接続プロファイルのクライアント。
 * Python API へ /api/python-proxy 経由でアクセスする（cookie認証）。
 */

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

export type RemoteCapabilities = {
  version?: string;
  profile?: string;
  features?: Record<string, boolean>;
  server_time?: string;
  user?: { id?: string; username?: string; role?: string } | null;
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res
      .json()
      .catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

export async function listRemoteServers(): Promise<RemoteServerProfile[]> {
  const data = await request<{ profiles: RemoteServerProfile[] }>(
    "/remote-servers",
  );
  return data.profiles ?? [];
}

export async function createRemoteServer(
  input: CreateRemoteServerInput,
): Promise<RemoteServerProfile> {
  const data = await request<{ profile: RemoteServerProfile }>(
    "/remote-servers",
    { method: "POST", body: JSON.stringify(input) },
  );
  return data.profile;
}

export async function updateRemoteServer(
  id: string,
  input: UpdateRemoteServerInput,
): Promise<RemoteServerProfile> {
  const data = await request<{ profile: RemoteServerProfile }>(
    `/remote-servers/${id}`,
    { method: "PATCH", body: JSON.stringify(input) },
  );
  return data.profile;
}

export async function deleteRemoteServer(id: string): Promise<void> {
  await request<{ success: boolean }>(`/remote-servers/${id}`, {
    method: "DELETE",
  });
}

export async function testRemoteServer(
  id: string,
): Promise<ConnectionTestResult> {
  // 失敗時もボディに status/error が入るため、ここでは例外化せず返す。
  const res = await fetch(`/api/python-proxy/remote-servers/${id}/test`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
  });
  const data = (await res.json().catch(() => ({}))) as ConnectionTestResult;
  if (data && typeof data.success === "boolean") return data;
  return { success: false, status: "error", error: res.statusText };
}
