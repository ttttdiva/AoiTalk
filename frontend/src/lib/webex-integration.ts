export type WebexSettings = {
  configured: boolean;
  connected: boolean;
  callback_origin?: string | null;
  email?: string | null;
  display_name?: string | null;
  scope?: string | null;
  selected_space_count: number;
  selected_room_ids: string[];
  max_selected_spaces: number;
};

export type WebexSpace = {
  id: string;
  title: string;
  type?: string | null;
  last_activity?: string | null;
  created?: string | null;
  selected: boolean;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

export function getWebexSettings(): Promise<WebexSettings> {
  return request<WebexSettings>("/webex/settings");
}

export async function startWebexConnect(): Promise<string> {
  const data = await request<{ authorization_url: string }>("/webex/connect", {
    method: "POST",
  });
  return data.authorization_url;
}

export function disconnectWebex(): Promise<WebexSettings> {
  return request<WebexSettings>("/webex/disconnect", { method: "POST" });
}

export async function listWebexSpaces(): Promise<WebexSpace[]> {
  const data = await request<{ spaces: WebexSpace[] }>("/webex/spaces");
  return data.spaces;
}

export async function updateWebexSpaces(
  roomIds: string[],
): Promise<WebexSpace[]> {
  const data = await request<{ spaces: WebexSpace[] }>("/webex/spaces", {
    method: "PUT",
    body: JSON.stringify({ room_ids: roomIds }),
  });
  return data.spaces;
}
