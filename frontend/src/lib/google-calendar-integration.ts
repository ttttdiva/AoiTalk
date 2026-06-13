export type GoogleCalendarSettings = {
  configured: boolean;
  connected: boolean;
  email?: string | null;
  calendar_id: string;
  default_action: "open_template" | "create_event";
  default_event_reminder_minutes: number;
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

export async function getGoogleCalendarSettings(): Promise<GoogleCalendarSettings> {
  return request<GoogleCalendarSettings>("/google-calendar/settings");
}

export async function updateGoogleCalendarSettings(
  patch: Partial<
    Pick<
      GoogleCalendarSettings,
      "default_action" | "default_event_reminder_minutes"
    >
  >,
): Promise<GoogleCalendarSettings> {
  return request<GoogleCalendarSettings>("/google-calendar/settings", {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export async function startGoogleCalendarConnect(): Promise<string> {
  const data = await request<{ authorization_url: string }>(
    "/google-calendar/connect",
    {
      method: "POST",
      body: JSON.stringify({ platform: "web" }),
    },
  );
  return data.authorization_url;
}

export async function disconnectGoogleCalendar(): Promise<GoogleCalendarSettings> {
  return request<GoogleCalendarSettings>("/google-calendar/disconnect", {
    method: "POST",
  });
}

export async function createGoogleCalendarEvent(taskId: string): Promise<{
  event_id?: string | null;
  html_link?: string | null;
  calendar_id: string;
}> {
  return request(`/tasks/${taskId}/google-calendar-event`, {
    method: "POST",
  });
}
