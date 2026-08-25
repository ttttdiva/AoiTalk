"use client";

import { useState } from "react";
import { PanelTop } from "lucide-react";
import { toast } from "sonner";

import { useUserSettings } from "@/contexts/user-settings-context";
import {
  OPTIONAL_APP_VIEW_TABS,
  type AppNavigationVisibilityKey,
} from "@/lib/app-navigation";
import { getAppNavigationVisibility } from "@/lib/user-settings";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { SettingsDisclosure } from "@/components/settings/settings-disclosure";

export function NavigationTabsSection() {
  const { settings, patch } = useUserSettings();
  const visibility = getAppNavigationVisibility(settings);
  const [saving, setSaving] = useState<AppNavigationVisibilityKey | null>(null);

  const updateVisibility = async (
    id: AppNavigationVisibilityKey,
    visible: boolean,
  ) => {
    setSaving(id);
    try {
      await patch({
        navigation_tabs: {
          ...visibility,
          [id]: visible,
        },
      });
    } catch (error) {
      toast.error(
        error instanceof Error
          ? error.message
          : "タブ表示設定を保存できませんでした",
      );
    } finally {
      setSaving(null);
    }
  };

  return (
    <SettingsDisclosure
      title="タブ表示"
      icon={<PanelTop className="size-4" />}
      targetId="navigation-tabs"
    >
      <p className="text-xs text-muted-foreground">
        チェックした項目を画面上部のタブに表示します。
      </p>
      <div className="space-y-2">
        {OPTIONAL_APP_VIEW_TABS.map((tab) => (
          <div
            key={tab.visibilityKey}
            className="flex items-center gap-2 rounded-md border px-3 py-2"
          >
            <Checkbox
              id={`navigation-tab-${tab.visibilityKey}`}
              checked={visibility[tab.visibilityKey]}
              disabled={saving !== null}
              onCheckedChange={(checked) =>
                void updateVisibility(tab.visibilityKey, checked === true)
              }
            />
            <Label
              htmlFor={`navigation-tab-${tab.visibilityKey}`}
              className="cursor-pointer text-sm"
            >
              {tab.title}タブを表示
            </Label>
          </div>
        ))}
      </div>
    </SettingsDisclosure>
  );
}
