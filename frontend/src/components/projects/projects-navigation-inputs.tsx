"use client";

import {
  useCallback,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";

import { AppSelect } from "@/components/ui/app-select";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Check, Layers, Loader2, X } from "lucide-react";
import {
  ProjectColorPicker,
  ResourceColorPicker,
} from "@/components/projects/resource-color-picker";

export type ProjectNavigationSpace = {
  id: string;
  name: string;
  color?: string | null;
};

export type ProjectNavigationProject = {
  id: string;
  name: string;
  description?: string | null;
  aliases?: string[];
  color?: string | null;
  estimated_hours?: number | null;
  space_id?: string | null;
  metadata?: {
    workspace_tools_enabled?: boolean;
    [key: string]: unknown;
  };
};

export type ProjectCreatePayload = {
  name: string;
  description: string | null;
  aliases: string[];
  color: string | null;
  estimated_hours: number | null;
  space_id: string | null;
};

export type ProjectUpdatePayload = ProjectCreatePayload & {
  id: string;
  metadata: {
    workspace_tools_enabled: boolean;
  };
};

export type SpaceUpdatePayload = {
  id: string;
  name: string;
  color: string | null;
};

export type ProjectNavigationAsyncCallback<T> = (
  payload: T,
) => Promise<void> | void;

/**
 * Enter is emitted by some IME implementations before composition is
 * committed. Browsers that do not expose `isComposing` use keyCode 229 as the
 * compatibility signal instead.
 */
export function isImeCompositionKeyDown(
  event: ReactKeyboardEvent<HTMLElement>,
): boolean {
  return event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229;
}

function useAsyncSubmitLock() {
  const lockRef = useRef(false);

  const run = useCallback(async (submit: () => Promise<void> | void) => {
    if (lockRef.current) return false;
    lockRef.current = true;
    try {
      await submit();
      return true;
    } finally {
      lockRef.current = false;
    }
  }, []);

  return run;
}

export function CreateSpaceForm({
  onCreated,
  onCancel,
}: {
  onCreated: ProjectNavigationAsyncCallback<string>;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const runWithLock = useAsyncSubmitLock();

  const submit = useCallback(async () => {
    const trimmedName = name.trim();
    if (!trimmedName || creating) return;

    try {
      await runWithLock(async () => {
        setCreating(true);
        try {
          await onCreated(trimmedName);
          setName("");
        } finally {
          setCreating(false);
        }
      });
    } catch (error) {
      console.error("スペース作成失敗:", error);
    }
  }, [creating, name, onCreated, runWithLock]);

  return (
    <Card size="sm" className="border-border-subtle bg-surface-container-low">
      <CardContent className="space-y-2 pt-4">
        <Input
          placeholder="スペース名"
          value={name}
          onChange={(event) => setName(event.target.value)}
          className="h-8"
          autoFocus
          onKeyDown={(event) => {
            if (event.key !== "Enter" || isImeCompositionKeyDown(event)) return;
            event.preventDefault();
            void submit();
          }}
        />
        <div className="flex gap-2">
          <Button
            size="sm"
            onClick={() => void submit()}
            disabled={creating || !name.trim()}
          >
            {creating && <Loader2 className="size-3 animate-spin mr-1" />}
            作成
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={onCancel}
            disabled={creating}
          >
            キャンセル
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export function SpaceRenameEditor({
  space,
  activeProjectCount,
  onUpdated,
  onCancel,
  navigationExpanded,
  renderActions,
}: {
  space: ProjectNavigationSpace;
  activeProjectCount: number;
  onUpdated: ProjectNavigationAsyncCallback<SpaceUpdatePayload>;
  onCancel: () => void;
  /**
   * Keep structural navigation changes observable to the shell registry even
   * when the action renderer itself is an inline callback.
   */
  navigationExpanded?: boolean;
  renderActions?: (actions: {
    submit: () => Promise<void>;
    cancel: () => void;
    pending: boolean;
  }) => ReactNode;
}) {
  const [name, setName] = useState(space.name);
  const [color, setColor] = useState(space.color || "#3b82f6");
  const [saving, setSaving] = useState(false);
  const runWithLock = useAsyncSubmitLock();

  const submit = useCallback(async () => {
    const trimmedName = name.trim();
    if (!trimmedName || saving) return;

    try {
      await runWithLock(async () => {
        setSaving(true);
        try {
          await onUpdated({
            id: space.id,
            name: trimmedName,
            color: color ? color.toLowerCase() : null,
          });
        } finally {
          setSaving(false);
        }
      });
    } catch (error) {
      console.error("スペース更新失敗:", error);
    }
  }, [color, name, onUpdated, runWithLock, saving, space.id]);

  return (
    <>
      <div
        className="min-w-0 flex-1 space-y-1.5 text-sm font-medium"
        data-navigation-expanded={navigationExpanded}
      >
        <div className="flex min-w-0 items-center gap-1.5">
          <Layers
            className="size-3.5 shrink-0 text-muted-foreground"
            style={color ? { color } : undefined}
          />
          <Input
            value={name}
            onChange={(event) => setName(event.target.value)}
            className="h-6 min-w-0 flex-1 text-sm"
            autoFocus
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                if (!saving) onCancel();
                return;
              }
              if (event.key !== "Enter" || isImeCompositionKeyDown(event)) return;
              event.preventDefault();
              void submit();
            }}
          />
          <Badge variant="secondary" className="shrink-0 px-1.5 py-0 text-[10px]">
            {activeProjectCount}
          </Badge>
        </div>
        <div className="flex min-w-0 items-start gap-1.5 pl-5">
          <span className="pt-1 text-[10px] text-muted-foreground">色</span>
          <ResourceColorPicker
            value={color}
            onChange={setColor}
            inputClassName="h-6"
            compact
            showLabel={false}
          />
        </div>
        <div className="sr-only" aria-live="polite">
          {saving ? "保存中" : ""}
        </div>
      </div>
      {renderActions ? (
        renderActions({ submit, cancel: onCancel, pending: saving })
      ) : (
        <div className="flex gap-0.5 text-on-surface-variant">
          <Button
            type="button"
            size="icon-sm"
            variant="ghost"
            onClick={() => void submit()}
            disabled={saving}
            aria-label={`${space.name}を保存`}
            title={`${space.name}を保存`}
          >
            <Check className="size-3" />
          </Button>
          <Button
            type="button"
            size="icon-sm"
            variant="ghost"
            onClick={onCancel}
            aria-label={`${space.name}の編集をキャンセル`}
            title={`${space.name}の編集をキャンセル`}
          >
            <X className="size-3" />
          </Button>
        </div>
      )}
    </>
  );
}

export function CreateProjectForm({
  spaces,
  onCreated,
  onCancel,
}: {
  spaces: ProjectNavigationSpace[];
  onCreated: ProjectNavigationAsyncCallback<ProjectCreatePayload>;
  onCancel: () => void;
}) {
  const [spaceId, setSpaceId] = useState<string | null>(
    () => spaces[0]?.id ?? null,
  );
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [aliases, setAliases] = useState("");
  const [estimatedHours, setEstimatedHours] = useState("");
  const [color, setColor] = useState("#3b82f6");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const runWithLock = useAsyncSubmitLock();

  const submit = useCallback(async () => {
    const trimmedName = name.trim();
    if (!trimmedName || creating) return;

    const parsedAliases = aliases
      .split(",")
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean);
    try {
      await runWithLock(async () => {
        setCreating(true);
        setError("");
        try {
          await onCreated({
            name: trimmedName,
            description: description.trim() || null,
            aliases: parsedAliases,
            color: color ? color.toLowerCase() : null,
            estimated_hours: estimatedHours ? parseFloat(estimatedHours) : null,
            space_id: spaceId || null,
          });
          setName("");
          setDescription("");
          setAliases("");
          setEstimatedHours("");
          setColor("#3b82f6");
          setSpaceId(null);
        } finally {
          setCreating(false);
        }
      });
    } catch (submitError) {
      console.error("プロジェクト作成失敗:", submitError);
      setError(
        submitError instanceof Error
          ? submitError.message
          : "プロジェクト作成に失敗しました",
      );
    }
  }, [
    aliases,
    color,
    creating,
    description,
    estimatedHours,
    name,
    onCreated,
    runWithLock,
    spaceId,
  ]);

  return (
    <Card size="sm" className="border-border-subtle bg-surface-container-low">
      <CardContent className="space-y-2 pt-4">
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <AppSelect
          value={spaceId || ""}
          onChange={(event) => setSpaceId(event.target.value || null)}
          className="h-8 w-full rounded border border-input bg-transparent px-2 text-sm outline-none"
        >
          <option value="">スペースなし</option>
          {spaces.map((space) => (
            <option key={space.id} value={space.id}>
              {space.name}
            </option>
          ))}
        </AppSelect>
        <Input
          placeholder="プロジェクト名"
          value={name}
          onChange={(event) => setName(event.target.value)}
          className="h-8"
          autoFocus
        />
        <Textarea
          placeholder="説明（任意）"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          className="min-h-16 text-sm"
        />
        <Input
          placeholder="エイリアス（カンマ区切り）"
          value={aliases}
          onChange={(event) => setAliases(event.target.value)}
          className="h-8"
        />
        <ProjectColorPicker value={color} onChange={setColor} />
        <Input
          type="number"
          placeholder="見積工数（時間）"
          value={estimatedHours}
          onChange={(event) => setEstimatedHours(event.target.value)}
          className="h-8"
          min="0"
          step="0.5"
        />
        <div className="flex gap-2">
          <Button
            size="sm"
            onClick={() => void submit()}
            disabled={creating || !name.trim()}
          >
            {creating && <Loader2 className="size-3 animate-spin mr-1" />}
            作成
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={onCancel}
            disabled={creating}
          >
            キャンセル
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export function ProjectEditForm({
  project,
  spaces,
  onUpdated,
  onCancel,
}: {
  project: ProjectNavigationProject;
  spaces: ProjectNavigationSpace[];
  onUpdated: ProjectNavigationAsyncCallback<ProjectUpdatePayload>;
  onCancel: () => void;
}) {
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description || "");
  const [aliases, setAliases] = useState((project.aliases || []).join(", "));
  const [estimatedHours, setEstimatedHours] = useState(
    project.estimated_hours != null ? String(project.estimated_hours) : "",
  );
  const [color, setColor] = useState(project.color || "#3b82f6");
  const [spaceId, setSpaceId] = useState(project.space_id || "");
  const [workspaceToolsEnabled, setWorkspaceToolsEnabled] = useState(
    project.metadata?.workspace_tools_enabled === true,
  );
  const [saving, setSaving] = useState(false);
  const runWithLock = useAsyncSubmitLock();

  const submit = useCallback(async () => {
    const trimmedName = name.trim();
    if (!trimmedName || saving) return;

    const parsedAliases = aliases
      .split(",")
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean);
    try {
      await runWithLock(async () => {
        setSaving(true);
        try {
          await onUpdated({
            id: project.id,
            name: trimmedName,
            description: description.trim() || null,
            aliases: parsedAliases,
            color: color ? color.toLowerCase() : null,
            estimated_hours: estimatedHours ? parseFloat(estimatedHours) : null,
            space_id: spaceId || null,
            metadata: {
              workspace_tools_enabled: workspaceToolsEnabled,
            },
          });
        } finally {
          setSaving(false);
        }
      });
    } catch (error) {
      console.error("プロジェクト更新失敗:", error);
    }
  }, [
    aliases,
    color,
    description,
    estimatedHours,
    name,
    onUpdated,
    project.id,
    runWithLock,
    saving,
    spaceId,
    workspaceToolsEnabled,
  ]);

  return (
    <div className="space-y-2" onClick={(event) => event.stopPropagation()}>
      <Input
        value={name}
        onChange={(event) => setName(event.target.value)}
        className="h-7 text-sm"
        autoFocus
      />
      <Textarea
        value={description}
        onChange={(event) => setDescription(event.target.value)}
        className="min-h-12 text-xs"
      />
      <Input
        placeholder="エイリアス（カンマ区切り 例: tokyo, fy25）"
        value={aliases}
        onChange={(event) => setAliases(event.target.value)}
        className="h-7 text-xs"
      />
      <ProjectColorPicker
        value={color}
        onChange={setColor}
        inputClassName="h-7"
      />
      <Input
        type="number"
        placeholder="見積工数（時間）"
        value={estimatedHours}
        onChange={(event) => setEstimatedHours(event.target.value)}
        className="h-7 text-xs"
        min="0"
        step="0.5"
      />
      <AppSelect
        value={spaceId}
        onChange={(event) => setSpaceId(event.target.value)}
        className="h-7 w-full rounded border border-input bg-transparent px-2 text-xs outline-none"
      >
        <option value="">スペースなし</option>
        {spaces.map((space) => (
          <option key={space.id} value={space.id}>
            {space.name}
          </option>
        ))}
      </AppSelect>
      <label className="flex cursor-pointer items-start gap-2 rounded border border-amber-500/30 bg-amber-500/5 p-2">
        <Checkbox
          checked={workspaceToolsEnabled}
          onCheckedChange={(checked) => setWorkspaceToolsEnabled(checked === true)}
          aria-label="プロジェクトツールを有効にする"
          className="mt-0.5"
        />
        <span className="min-w-0">
          <span className="block text-xs font-medium">
            プロジェクトツールを有効にする
          </span>
          <span className="mt-0.5 block text-[11px] leading-relaxed text-muted-foreground">
            tools/ 配下のプログラムをエージェントが実行できるようにします。信頼できるツールだけを配置してください。
          </span>
        </span>
      </label>
      <div className="flex gap-1">
        <Button
          type="button"
          size="icon-sm"
          variant="ghost"
          onClick={() => void submit()}
          disabled={saving}
          aria-label={`${project.name}を保存`}
          title={`${project.name}を保存`}
        >
          <Check className="size-3" />
        </Button>
        <Button
          type="button"
          size="icon-sm"
          variant="ghost"
          onClick={onCancel}
          disabled={saving}
          aria-label={`${project.name}の編集をキャンセル`}
          title={`${project.name}の編集をキャンセル`}
        >
          <X className="size-3" />
        </Button>
      </div>
    </div>
  );
}
