"use client";

import { useCallback, useEffect, useState, type RefObject } from "react";
import { Loader2, X } from "lucide-react";
import { toast } from "sonner";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent } from "@/components/ui/popover";
import { appsApi, type AppSummary, type AppTarget } from "@/lib/apps-api";

export type ChatAppContextSelection = {
  appId: string;
  appName: string;
  targetId: string;
  targetKey: string;
  targetDisplayName: string;
};

type AppContextPickerProps = {
  value: ChatAppContextSelection | null;
  projectId?: string | null;
  onChange: (value: ChatAppContextSelection | null) => void;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** popoverの表示位置を合わせる要素（通常はツールメニューの＋ボタン） */
  anchorRef?: RefObject<HTMLElement | null>;
};

export function AppContextPicker({
  value,
  projectId,
  onChange,
  open,
  onOpenChange,
  anchorRef,
}: AppContextPickerProps) {
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [targets, setTargets] = useState<AppTarget[]>([]);
  const [loading, setLoading] = useState(false);
  const [targetsLoading, setTargetsLoading] = useState(false);

  const loadApps = useCallback(async () => {
    setLoading(true);
    try {
      const response = await appsApi.list(projectId || undefined);
      setApps(response.apps || []);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "App一覧の取得に失敗しました");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (open) void loadApps();
  }, [loadApps, open]);

  const loadTargets = useCallback(async (appId: string) => {
    setTargetsLoading(true);
    try {
      const response = await appsApi.getTargets(appId, projectId || undefined);
      setTargets(response.targets || []);
      return response.targets || [];
    } catch (error) {
      setTargets([]);
      toast.error(error instanceof Error ? error.message : "Target一覧の取得に失敗しました");
      return [];
    } finally {
      setTargetsLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (value?.appId) void loadTargets(value.appId);
  }, [loadTargets, value?.appId]);

  const handleAppChange = async (appId: string) => {
    if (!appId) {
      setTargets([]);
      onChange(null);
      return;
    }
    const app = apps.find((item) => item.id === appId);
    const nextTargets = await loadTargets(appId);
    const selected = nextTargets.find((target) => target.target_key === app?.default_target_key) || nextTargets[0];
    if (!app || !selected) {
      onChange(null);
      return;
    }
    onChange({
      appId: app.id,
      appName: app.name,
      targetId: selected.id,
      targetKey: selected.target_key,
      targetDisplayName: selected.display_name,
    });
  };

  const handleTargetChange = (targetId: string) => {
    if (!value) return;
    const target = targets.find((item) => item.id === targetId);
    if (!target) return;
    onChange({
      ...value,
      targetId: target.id,
      targetKey: target.target_key,
      targetDisplayName: target.display_name,
    });
  };

  const handleOpenChange = (nextOpen: boolean) => {
    onOpenChange?.(nextOpen);
    if (nextOpen && apps.length === 0) void loadApps();
  };

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverContent
        side="top"
        align="start"
        anchor={anchorRef}
        className="w-80 space-y-3"
      >
        <div>
          <p className="text-sm font-medium">App context</p>
          <p className="text-xs text-muted-foreground">編集・build・test対象のAppとTargetを選択します。</p>
        </div>
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />Appを読み込み中</div>
        ) : (
          <AppSelect
            value={value?.appId || ""}
            onValueChange={(next) => void handleAppChange(next)}
            placeholder="Appを選択"
            className="w-full justify-between"
          >
            <option value="">Appを選択</option>
            {apps.map((app) => <option key={app.id} value={app.id}>{app.name}</option>)}
          </AppSelect>
        )}
        {value && (
          <>
            {targetsLoading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />Targetを読み込み中</div>
            ) : (
              <AppSelect
                value={value.targetId}
                onValueChange={handleTargetChange}
                placeholder="Targetを選択"
                className="w-full justify-between"
              >
                {targets.map((target) => <option key={target.id} value={target.id}>{target.display_name} · {target.target_key}</option>)}
              </AppSelect>
            )}
            <div className="flex items-center justify-between rounded-md border bg-muted/30 px-2 py-1.5 text-xs">
              <span className="truncate">App: {value.appName} / {value.targetKey}</span>
              <Button type="button" variant="ghost" size="icon" className="size-6" onClick={() => onChange(null)} aria-label="App contextを解除"><X className="size-3" /></Button>
            </div>
          </>
        )}
      </PopoverContent>
    </Popover>
  );
}
