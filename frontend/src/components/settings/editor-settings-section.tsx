"use client";

import { useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import { useUserSettings } from "@/contexts/user-settings-context";
import {
  normalizeEditorLinkDefaultDisplayMode,
  type EditorLinkDefaultDisplayMode,
} from "@/lib/user-settings";
import { ChevronDown, ChevronUp, Link2 } from "lucide-react";

const DISPLAY_MODE_LABELS: Record<EditorLinkDefaultDisplayMode, string> = {
  embed: "埋め込みカード",
  link: "通常リンク",
};

export function EditorSettingsSection() {
  const { editorLinkDefaultDisplayMode, patch } = useUserSettings();
  const [expanded, setExpanded] = useState(false);
  const [saving, setSaving] = useState(false);

  const handleModeChange = async (value: string | null) => {
    const next = normalizeEditorLinkDefaultDisplayMode(value);
    if (next === editorLinkDefaultDisplayMode) return;

    setSaving(true);
    try {
      await patch({
        editor: {
          link_default_display_mode: next,
        },
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card size="sm">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={() => setExpanded((v) => !v)}
      >
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2">
            <Link2 className="size-4" />
            埋め込みカード
            <span className="text-xs font-normal text-muted-foreground">
              {DISPLAY_MODE_LABELS[editorLinkDefaultDisplayMode]}
            </span>
          </span>
          {expanded ? (
            <ChevronUp className="size-4" />
          ) : (
            <ChevronDown className="size-4" />
          )}
        </CardTitle>
        {expanded && (
          <CardDescription>
            エディタ内で使用するURLカードの既定表示を変更します。
          </CardDescription>
        )}
      </CardHeader>
      {expanded && (
        <CardContent className="space-y-3">
          <div className="space-y-2">
            <Label className="text-xs">URL の既定表示</Label>
            <Select
              value={editorLinkDefaultDisplayMode}
              onValueChange={handleModeChange}
              disabled={saving}
            >
              <SelectTrigger className="w-48">
                <span>{DISPLAY_MODE_LABELS[editorLinkDefaultDisplayMode]}</span>
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="embed">
                  {DISPLAY_MODE_LABELS.embed}
                </SelectItem>
                <SelectItem value="link">{DISPLAY_MODE_LABELS.link}</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      )}
    </Card>
  );
}
