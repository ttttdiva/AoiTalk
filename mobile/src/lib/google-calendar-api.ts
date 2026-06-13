import * as Linking from "expo-linking";
import { fetchApi } from "./api-client";

export type GoogleCalendarSettings = {
  configured: boolean;
  connected: boolean;
  email?: string | null;
  calendar_id: string;
  default_action: "open_template" | "create_event";
};

export const googleCalendarApi = {
  getSettings: () =>
    fetchApi<GoogleCalendarSettings>("/api/google-calendar/settings"),

  updateSettings: (patch: {
    default_action?: "open_template" | "create_event";
  }) =>
    fetchApi<GoogleCalendarSettings>("/api/google-calendar/settings", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  connect: async () => {
    const mobileRedirectUri = Linking.createURL("/settings/connection");
    const data = await fetchApi<{ authorization_url: string }>(
      "/api/google-calendar/connect",
      {
        method: "POST",
        body: JSON.stringify({
          platform: "mobile",
          mobile_redirect_uri: mobileRedirectUri,
        }),
      },
    );
    await Linking.openURL(data.authorization_url);
  },

  disconnect: () =>
    fetchApi<GoogleCalendarSettings>("/api/google-calendar/disconnect", {
      method: "POST",
    }),

  createEvent: (taskId: string) =>
    fetchApi<{
      event_id?: string | null;
      html_link?: string | null;
      calendar_id: string;
    }>(`/api/tasks/${taskId}/google-calendar-event`, {
      method: "POST",
    }),
};
