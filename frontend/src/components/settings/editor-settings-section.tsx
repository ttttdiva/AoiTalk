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
  const [feedback, setFeedback] = useState<string | null>(null);

  const handleModeChange = async (value: string | null) => {
    const next = normalizeEditorLinkDefaultDisplayMode(value);
    if (next === editorLinkDefaultDisplayMode) return;

    setSaving(true);
    setFeedback(null);
    try {
      await patch({
        editor: {
          link_default_display_mode: next,
        },
      });
      setFeedback("エディタ設定を保存しました。");
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : "保存に失敗しました。");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card size="sm" className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0">
      <CardHeader
        className="cursor-pointer select-none"
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        aria-controls="editor-settings-content"
        onClick={() => setExpanded((v) => !v)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            setExpanded((v) => !v);
          }
        }}
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
        <CardContent id="editor-settings-content" className="space-y-3">
          {feedback && <p role="status" className="text-xs text-muted-foreground">{feedback}</p>}
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
