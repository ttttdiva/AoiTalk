export type TrpgPlayParticipant = {
  id: string;
  session_id: string;
  user_id?: string | null;
  display_name: string;
  role: string;
  story_character_id?: string | null;
  is_npc?: boolean;
  joined_at?: string | null;
  left_at?: string | null;
};

export type TrpgPlayPrivateState = {
  id: string;
  session_id: string;
  participant_id: string;
  state: { entries: Record<string, { value: unknown; shared_with_gm?: boolean }> };
  created_at?: string | null;
  updated_at?: string | null;
};

export type TrpgPlayGmPrivateState = {
  participant_id: string;
  display_name?: string | null;
  state: { entries: Record<string, { value: unknown; shared_with_gm?: boolean }> };
  updated_at?: string | null;
};

export type TrpgPlayEvent = {
  id: string;
  session_id: string;
  actor_participant_id?: string | null;
  kind: string;
  body: string;
  meta?: Record<string, unknown>;
  created_at?: string | null;
  actor_display_name?: string | null;
};

export type TrpgPlayWhisper = {
  id: string;
  session_id: string;
  sender_participant_id: string;
  body: string;
  recipient_participant_ids: string[];
  created_at?: string | null;
};

export type TrpgPlaySession = {
  id: string;
  work_id: string;
  host_user_id: string;
  title: string;
  gm_mode: string;
  status: string;
  invite_code?: string | null;
  snapshot?: Record<string, unknown>;
  image_settings?: Record<string, unknown>;
  participants?: TrpgPlayParticipant[];
  recent_events?: TrpgPlayEvent[];
  viewer_participant_id?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  ended_at?: string | null;
};

function proxyUrl(apiPath: string): string {
  return apiPath.startsWith("/api/") ? `/api/python-proxy${apiPath.slice(4)}` : `/api/python-proxy${apiPath}`;
}

async function parseJson<T>(response: Response): Promise<T> {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload?.detail === "string" ? payload.detail : "TRPG Play API error";
    throw new Error(detail);
  }
  return payload as T;
}

async function playFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(proxyUrl(path), {
    ...init,
    credentials: "include",
    cache: "no-store",
  });
}

export const trpgPlayApi = {
  async listSessions(): Promise<TrpgPlaySession[]> {
    const response = await playFetch("/api/trpg/sessions");
    const data = await parseJson<{ sessions: TrpgPlaySession[] }>(response);
    return data.sessions;
  },

  async createSession(payload: {
    work_id: string;
    gm_mode?: "human" | "ai";
    title?: string;
  }): Promise<TrpgPlaySession> {
    const response = await playFetch("/api/trpg/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return parseJson(response);
  },

  async getSession(sessionId: string): Promise<TrpgPlaySession> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}`);
    return parseJson(response);
  },

  async joinSession(
    sessionId: string,
    payload: {
      invite_code: string;
      display_name: string;
      role?: string;
      story_character_id?: string | null;
    },
  ): Promise<TrpgPlayParticipant> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/join`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return parseJson(response);
  },

  async startSession(sessionId: string): Promise<TrpgPlaySession> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/start`, {
      method: "POST",
    });
    return parseJson(response);
  },

  async endSession(sessionId: string): Promise<TrpgPlaySession> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/end`, {
      method: "POST",
    });
    return parseJson(response);
  },

  async postAction(
    sessionId: string,
    payload: { kind: "speech" | "action" | "ooc"; text: string },
  ): Promise<TrpgPlayEvent[]> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/actions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await parseJson<{ events: TrpgPlayEvent[] }>(response);
    return data.events;
  },

  async rollDice(
    sessionId: string,
    payload: { expression: string; note?: string },
  ): Promise<TrpgPlayEvent> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/dice`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await parseJson<{ event: TrpgPlayEvent }>(response);
    return data.event;
  },

  async listWhispers(sessionId: string): Promise<TrpgPlayWhisper[]> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/whispers`);
    const data = await parseJson<{ whispers: TrpgPlayWhisper[] }>(response);
    return data.whispers;
  },

  async postWhisper(
    sessionId: string,
    payload: { body: string; recipient_participant_ids: string[] },
  ): Promise<TrpgPlayWhisper> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/whispers`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return parseJson(response);
  },

  async patchSnapshot(sessionId: string, snapshot: Record<string, unknown>): Promise<TrpgPlaySession> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/snapshot`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ snapshot }),
    });
    return parseJson(response);
  },

  async patchImageSettings(
    sessionId: string,
    imageSettings: Record<string, unknown>,
  ): Promise<TrpgPlaySession> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/image-settings`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image_settings: imageSettings }),
    });
    return parseJson(response);
  },

  async generateImage(sessionId: string, prompt?: string): Promise<{ event?: TrpgPlayEvent; media_id?: string }> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/images/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(prompt ? { prompt } : {}),
    });
    return parseJson(response);
  },

  async leaveSession(sessionId: string): Promise<TrpgPlayParticipant> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/leave`, {
      method: "POST",
    });
    return parseJson(response);
  },

  async getPrivateState(sessionId: string): Promise<TrpgPlayPrivateState> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/private-state`);
    return parseJson(response);
  },

  async patchPrivateState(
    sessionId: string,
    state: TrpgPlayPrivateState["state"],
  ): Promise<TrpgPlayPrivateState> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/private-state`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state }),
    });
    return parseJson(response);
  },

  async listGmPrivateStates(sessionId: string): Promise<TrpgPlayGmPrivateState[]> {
    const response = await playFetch(`/api/trpg/sessions/${sessionId}/private-states`);
    const data = await parseJson<{ private_states: TrpgPlayGmPrivateState[] }>(response);
    return data.private_states;
  },
};

export function playGeneratedMediaUrl(mediaId: string): string {
  return `/api/python-proxy/api/generated-media/${encodeURIComponent(mediaId)}`;
}
