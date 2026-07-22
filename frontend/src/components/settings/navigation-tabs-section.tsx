"use client";

import { useState } from "react";
import { PanelTop } from "lucide-react";
import { toast } from "sonner";

import { useUserSettings } from "@/contexts/user-settings-context";
import { getAppNavigationVisibility } from "@/lib/user-settings";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { SettingsDisclosure } from "@/components/settings/settings-disclosure";

type OptionalTab = "scenarios" | "trpg";

const OPTIONAL_TABS: Array<{ id: OptionalTab; label: string }> = [
  { id: "scenarios", label: "シナリオ" },
  { id: "trpg", label: "TRPG" },
];

export function NavigationTabsSection() {
  const { settings, patch } = useUserSettings();
  const visibility = getAppNavigationVisibility(settings);
  const [saving, setSaving] = useState<OptionalTab | null>(null);

  const updateVisibility = async (id: OptionalTab, visible: boolean) => {
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
    >
      <p className="text-xs text-muted-foreground">
        チェックした項目を画面上部のタブに表示します。
      </p>
      <div className="space-y-2">
        {OPTIONAL_TABS.map((tab) => (
          <div
            key={tab.id}
            className="flex items-center gap-2 rounded-md border px-3 py-2"
          >
            <Checkbox
              id={`navigation-tab-${tab.id}`}
              checked={visibility[tab.id]}
              disabled={saving !== null}
              onCheckedChange={(checked) =>
                void updateVisibility(tab.id, checked === true)
              }
            />
            <Label
              htmlFor={`navigation-tab-${tab.id}`}
              className="cursor-pointer text-sm"
            >
              {tab.label}タブを表示
            </Label>
          </div>
        ))}
      </div>
    </SettingsDisclosure>
  );
}
