"use client";

import { useCallback, useEffect, useState } from "react";
import { Calendar, ChevronDown, ChevronUp, Loader2, Link2, Unplug } from "lucide-react";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import {
  disconnectGoogleCalendar,
  getGoogleCalendarSettings,
  startGoogleCalendarConnect,
  updateGoogleCalendarSettings,
  type GoogleCalendarSettings,
} from "@/lib/google-calendar-integration";

export function GoogleCalendarSection() {
  const [expanded, setExpanded] = useState(false);
  const [settings, setSettings] = useState<GoogleCalendarSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [reminderMinutes, setReminderMinutes] = useState("10");

  const loadSettings = useCallback(async () => {
    setLoading(true);
    try {
      setSettings(await getGoogleCalendarSettings());
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (expanded && !settings) void loadSettings();
  }, [expanded, loadSettings, settings]);

  useEffect(() => {
    if (!settings) return;
    setReminderMinutes(String(settings.default_event_reminder_minutes ?? 10));
  }, [settings]);

  useEffect(() => {
    const handler = (event: MessageEvent) => {
      const data = event.data as
        | { source?: string; success?: boolean; error?: string }
        | undefined;
      if (data?.source !== "aoitalk-google-calendar") return;
      setConnecting(false);
      if (data.success) {
        toast.success("Google Calendar connected");
        void loadSettings();
      } else {
        toast.error(data.error || "Google Calendar connection failed");
      }
    };
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [loadSettings]);

  const handleConnect = useCallback(async () => {
    setConnecting(true);
    try {
      const url = await startGoogleCalendarConnect();
      const popup = window.open(
        url,
        "aoitalk-google-calendar",
        "width=520,height=720,noopener,noreferrer",
      );
      if (!popup) {
        window.location.href = url;
        return;
      }
    } catch (error) {
      setConnecting(false);
      toast.error(error instanceof Error ? error.message : "Connect failed");
    }
  }, []);

  const handleDisconnect = useCallback(async () => {
    setDisconnecting(true);
    try {
      setSettings(await disconnectGoogleCalendar());
      toast.success("Google Calendar disconnected");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Disconnect failed");
    } finally {
      setDisconnecting(false);
    }
  }, []);

  const handleActionChange = useCallback(
    async (value: "open_template" | "create_event" | null) => {
      if (value !== "open_template" && value !== "create_event") return;
      setSaving(true);
      try {
        setSettings(
          await updateGoogleCalendarSettings({ default_action: value }),
        );
        toast.success("Google Calendar settings saved");
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "Save failed");
      } finally {
        setSaving(false);
      }
    },
    [],
  );

  const handleReminderMinutesSave = useCallback(async () => {
    const minutes = Number(reminderMinutes);
    if (!Number.isFinite(minutes) || minutes < 0) return;
    setSaving(true);
    try {
      setSettings(
        await updateGoogleCalendarSettings({
          default_event_reminder_minutes: Math.floor(minutes),
        }),
      );
      toast.success("Google Calendar settings saved");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }, [reminderMinutes]);

  return (
    <Card size="sm">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={() => setExpanded((v) => !v)}
      >
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2">
            <Calendar className="size-4" />
            Google Calendar
            {settings ? (
              <span className="text-xs font-normal text-muted-foreground">
                {settings.connected ? "接続済み" : "未接続"}
              </span>
            ) : null}
          </span>
          {expanded ? (
            <ChevronUp className="size-4" />
          ) : (
            <ChevronDown className="size-4" />
          )}
        </CardTitle>
      </CardHeader>
      {expanded && (
      <CardContent className="space-y-3">
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-3 animate-spin" />
            Loading...
          </div>
        ) : settings ? (
          <>
            <div className="space-y-1 text-sm">
              <p>
                状態:{" "}
                <span className="font-medium">
                  {settings.connected ? "接続済み" : "未接続"}
                </span>
              </p>
              {settings.email ? <p>アカウント: {settings.email}</p> : null}
              {!settings.configured ? (
                <p className="text-xs text-muted-foreground">
                  Server is missing Google Calendar OAuth environment variables.
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Direct create also auto-syncs tasks with a timed Start Date.
                </p>
              )}
            </div>

            <div className="space-y-2">
              <Label className="text-xs">Default Google Calendar action</Label>
              <Select
                value={settings.default_action}
                onValueChange={handleActionChange}
              >
                <SelectTrigger className="w-full">
                  <span>
                    {settings.default_action === "create_event"
                      ? "Direct create + auto-sync"
                      : "Open Google create form"}
                  </span>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="open_template">
                    Open Google create form
                  </SelectItem>
                  <SelectItem value="create_event">
                    Direct create + auto-sync
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label
                htmlFor="google-calendar-reminder-minutes"
                className="text-xs"
              >
                Google event reminder minutes before
              </Label>
              <div className="flex items-center gap-2">
                <Input
                  id="google-calendar-reminder-minutes"
                  type="number"
                  min={0}
                  step={1}
                  value={reminderMinutes}
                  onChange={(event) => setReminderMinutes(event.target.value)}
                  className="h-8 w-28"
                />
                <Button
                  type="button"
                  size="sm"
                  onClick={handleReminderMinutesSave}
                  disabled={
                    saving ||
                    !Number.isFinite(Number(reminderMinutes)) ||
                    Number(reminderMinutes) < 0
                  }
                >
                  Save
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                Used for events created directly in Google Calendar. Per-task
                AoiTalk notifications stay controlled on each task.
              </p>
            </div>

            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant={settings.connected ? "secondary" : "default"}
                onClick={handleConnect}
                disabled={connecting || !settings.configured}
              >
                {connecting ? (
                  <Loader2 className="mr-1 size-3 animate-spin" />
                ) : (
                  <Link2 className="mr-1 size-3" />
                )}
                {settings.connected ? "Reconnect" : "Connect Google"}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={handleDisconnect}
                disabled={disconnecting || !settings.connected}
              >
                {disconnecting ? (
                  <Loader2 className="mr-1 size-3 animate-spin" />
                ) : (
                  <Unplug className="mr-1 size-3" />
                )}
                Disconnect
              </Button>
            </div>
            {saving ? (
              <p className="text-xs text-muted-foreground">Saving...</p>
            ) : null}
          </>
        ) : (
          <p className="text-sm text-muted-foreground">
            Failed to load Google Calendar settings.
          </p>
        )}
      </CardContent>
      )}
    </Card>
  );
}
