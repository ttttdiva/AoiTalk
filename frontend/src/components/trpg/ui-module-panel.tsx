"use client";

/* eslint-disable @next/next/no-img-element */

import { useMemo, useState } from "react";
import { CircleDot, Grid3x3, Hash, ListChecks, Map, MousePointer2, Plus, ScanSearch, SquareAsterisk, SquareCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { cn } from "@/lib/utils";

type UnknownRecord = Record<string, unknown>;

export type TRPGUIModule = {
  id: string;
  module?: string;
  type?: string;
  title?: string;
  description?: string;
  visibility?: "public" | "gm" | "private" | string;
  target_participant_ids?: string[];
  config?: UnknownRecord;
  state?: UnknownRecord;
  onAction?: unknown[];
  onSuccess?: unknown[];
  onFailure?: unknown[];
};

export type TRPGDisclosureSummary = {
  id: string;
  title: string;
  content?: string;
  image_url?: string;
  image_path?: string;
  disclosure_type?: string;
  visibility?: string;
  is_pinned?: boolean;
};

type Props = {
  modules: TRPGUIModule[];
  moduleState: Record<string, UnknownRecord>;
  disclosures?: TRPGDisclosureSummary[];
  isGm: boolean;
  myParticipantId: string;
  imageSrc?: (path?: string) => string;
  onAction: (module: TRPGUIModule, actionType: string, payload?: UnknownRecord) => Promise<void> | void;
};

function isRecord(value: unknown): value is UnknownRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asRecordList(value: unknown): UnknownRecord[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function moduleKind(module: TRPGUIModule): string {
  return module.module || module.type || "button_grid";
}

function moduleConfig(module: TRPGUIModule): UnknownRecord {
  return isRecord(module.config) ? module.config : {};
}

function labelOf(item: UnknownRecord, fallback: string): string {
  return asString(item.label) || asString(item.title) || asString(item.name) || fallback;
}

function idOf(item: UnknownRecord, fallback: string): string {
  return asString(item.id) || asString(item.value) || labelOf(item, fallback);
}

function moduleIcon(kind: string) {
  switch (kind) {
    case "keypad": return Hash;
    case "button_grid": return Grid3x3;
    case "choice": return CircleDot;
    case "counter": return Plus;
    case "checklist": return ListChecks;
    case "map": return Map;
    case "image_hotspot": return MousePointer2;
    case "handout_viewer": return ScanSearch;
    default: return SquareAsterisk;
  }
}

export function canSeeModule(module: TRPGUIModule, isGm: boolean, myParticipantId: string): boolean {
  const visibility = module.visibility || "public";
  if (visibility === "gm") return isGm;
  if (visibility === "private") {
    return isGm || asStringList(module.target_participant_ids).includes(myParticipantId);
  }
  return true;
}

function resolveImagePath(value: unknown, imageSrc?: (path?: string) => string): string {
  const path = asString(value);
  if (!path) return "";
  return imageSrc ? imageSrc(path) : path;
}

export function TRPGUIModulePanel({
  modules,
  moduleState,
  disclosures = [],
  isGm,
  myParticipantId,
  imageSrc,
  onAction,
}: Props) {
  const visibleModules = useMemo(
    () => modules.filter((module) => canSeeModule(module, isGm, myParticipantId)),
    [isGm, modules, myParticipantId],
  );
  const [keypadValues, setKeypadValues] = useState<Record<string, string>>({});
  const [busyKey, setBusyKey] = useState("");

  if (visibleModules.length === 0) return null;

  const runAction = async (module: TRPGUIModule, actionType: string, payload: UnknownRecord = {}) => {
    const key = `${module.id}:${actionType}`;
    setBusyKey(key);
    try {
      await onAction(module, actionType, payload);
    } finally {
      setBusyKey("");
    }
  };

  return (
    <div className="space-y-3">
      {visibleModules.map((module) => {
        const kind = moduleKind(module);
        const Icon = moduleIcon(kind);
        const config = moduleConfig(module);
        const state = { ...(isRecord(module.state) ? module.state : {}), ...(moduleState[module.id] || {}) };
        const title = module.title || module.id;

        return (
          <Card key={module.id}>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center justify-between gap-2 text-sm">
                <span className="flex min-w-0 items-center gap-2">
                  <Icon className="h-4 w-4 shrink-0" />
                  <span className="truncate">{title}</span>
                </span>
                {module.visibility === "gm" && <Badge variant="outline">GM</Badge>}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-xs">
              {module.description && <div className="whitespace-pre-wrap text-muted-foreground">{module.description}</div>}

              {kind === "keypad" && (
                <div className="space-y-2">
                  <div className="rounded border bg-muted px-3 py-2 text-center font-mono text-lg tracking-[0.2em]">
                    {(keypadValues[module.id] || "").replace(/./g, "*") || "----"}
                  </div>
                  <div className="grid grid-cols-3 gap-2">
                    {["1", "2", "3", "4", "5", "6", "7", "8", "9", "C", "0", "OK"].map((key) => (
                      <Button
                        key={key}
                        type="button"
                        size="sm"
                        variant={key === "OK" ? "default" : "outline"}
                        className="h-9 font-mono"
                        disabled={busyKey === `${module.id}:keypad_submit`}
                        onClick={() => {
                          if (key === "C") {
                            setKeypadValues((prev) => ({ ...prev, [module.id]: "" }));
                            return;
                          }
                          if (key === "OK") {
                            void runAction(module, "keypad_submit", { value: keypadValues[module.id] || "" });
                            setKeypadValues((prev) => ({ ...prev, [module.id]: "" }));
                            return;
                          }
                          setKeypadValues((prev) => ({ ...prev, [module.id]: `${prev[module.id] || ""}${key}`.slice(0, 12) }));
                        }}
                      >
                        {key}
                      </Button>
                    ))}
                  </div>
                  <div className="text-muted-foreground">
                    {state.unlocked ? "解除済み" : "待機中"}
                    {typeof state.attempts === "number" ? ` / 試行 ${state.attempts}` : ""}
                  </div>
                </div>
              )}

              {kind === "button_grid" && (
                <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${Math.max(1, asNumber(config.columns, 3))}, minmax(0, 1fr))` }}>
                  {asRecordList(config.buttons).map((button, index) => {
                    const id = idOf(button, String(index + 1));
                    const selected = state.selected === id || (isRecord(state.buttons) && Boolean(state.buttons[id]));
                    return (
                      <Button key={id} type="button" size="sm" variant={selected ? "default" : "outline"} className="min-h-9 whitespace-normal" onClick={() => void runAction(module, "button_press", { button_id: id })}>
                        {labelOf(button, id)}
                      </Button>
                    );
                  })}
                </div>
              )}

              {kind === "choice" && (
                <div className="space-y-2">
                  {asRecordList(config.options).map((option, index) => {
                    const id = idOf(option, String(index + 1));
                    const selected = state.selected === id;
                    return (
                      <Button key={id} type="button" size="sm" variant={selected ? "default" : "outline"} className="w-full justify-start whitespace-normal" onClick={() => void runAction(module, "choice_select", { choice_id: id })}>
                        <SquareCheck className={cn("mr-2 h-4 w-4", !selected && "opacity-30")} />
                        {labelOf(option, id)}
                      </Button>
                    );
                  })}
                </div>
              )}

              {kind === "counter" && (
                <div className="flex items-center justify-between gap-2">
                  <Button type="button" size="icon" variant="outline" className="h-8 w-8" onClick={() => void runAction(module, "counter_update", { delta: -1 })}>-</Button>
                  <div className="min-w-0 text-center">
                    <div className="text-lg font-semibold">{asNumber(state.value, asNumber(config.initial, 0))}</div>
                    <div className="text-muted-foreground">{asString(config.label, "count")}</div>
                  </div>
                  <Button type="button" size="icon" variant="outline" className="h-8 w-8" onClick={() => void runAction(module, "counter_update", { delta: 1 })}>+</Button>
                </div>
              )}

              {kind === "checklist" && (
                <div className="space-y-2">
                  {asRecordList(config.items).map((item, index) => {
                    const id = idOf(item, String(index + 1));
                    const checked = isRecord(state.checked) && Boolean(state.checked[id]);
                    return (
                      <label key={id} className="flex items-start gap-2 rounded border p-2">
                        <Checkbox checked={checked} onCheckedChange={() => void runAction(module, "checklist_toggle", { item_id: id })} />
                        <span className={cn("min-w-0", checked && "text-muted-foreground line-through")}>{labelOf(item, id)}</span>
                      </label>
                    );
                  })}
                </div>
              )}

              {kind === "map" && (
                <div className="space-y-2">
                  <div className="relative aspect-[4/3] overflow-hidden rounded border bg-muted">
                    {resolveImagePath(config.image_path || config.image_url, imageSrc) ? (
                      <img src={resolveImagePath(config.image_path || config.image_url, imageSrc)} alt={title} className="h-full w-full object-cover" />
                    ) : (
                      <div className="flex h-full items-center justify-center text-muted-foreground">map</div>
                    )}
                    {asRecordList(config.pins).map((pin, index) => {
                      const id = idOf(pin, String(index + 1));
                      const selected = state.selected === id;
                      return (
                        <button
                          key={id}
                          type="button"
                          className={cn("absolute h-4 w-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-background bg-primary shadow", selected && "ring-2 ring-primary ring-offset-2")}
                          style={{ left: `${asNumber(pin.x, 50)}%`, top: `${asNumber(pin.y, 50)}%` }}
                          title={labelOf(pin, id)}
                          onClick={() => void runAction(module, "map_pin_select", { pin_id: id })}
                        />
                      );
                    })}
                  </div>
                  {Boolean(state.selected) && <div className="text-muted-foreground">選択: {String(state.selected)}</div>}
                </div>
              )}

              {kind === "image_hotspot" && (
                <div className="relative aspect-video overflow-hidden rounded border bg-muted">
                  {resolveImagePath(config.image_path || config.image_url, imageSrc) ? (
                    <img src={resolveImagePath(config.image_path || config.image_url, imageSrc)} alt={title} className="h-full w-full object-cover" />
                  ) : (
                    <div className="flex h-full items-center justify-center text-muted-foreground">image</div>
                  )}
                  {asRecordList(config.hotspots).map((hotspot, index) => {
                    const id = idOf(hotspot, String(index + 1));
                    const discovered = isRecord(state.discovered) && Boolean(state.discovered[id]);
                    return (
                      <button
                        key={id}
                        type="button"
                        className={cn("absolute rounded border border-primary bg-background/80 px-2 py-1 text-[10px] shadow", discovered && "bg-primary text-primary-foreground")}
                        style={{ left: `${asNumber(hotspot.x, 50)}%`, top: `${asNumber(hotspot.y, 50)}%` }}
                        onClick={() => void runAction(module, "hotspot_select", { hotspot_id: id })}
                      >
                        {labelOf(hotspot, id)}
                      </button>
                    );
                  })}
                </div>
              )}

              {kind === "handout_viewer" && (
                <div className="space-y-2">
                  {disclosures
                    .filter((disclosure) => asStringList(config.disclosure_ids).length === 0 ? disclosure.is_pinned : asStringList(config.disclosure_ids).includes(disclosure.id))
                    .map((disclosure) => (
                      <div key={disclosure.id} className="rounded border p-2">
                        <div className="font-medium">{disclosure.title}</div>
                        {disclosure.content && <div className="mt-1 whitespace-pre-wrap text-muted-foreground">{disclosure.content}</div>}
                      </div>
                    ))}
                </div>
              )}
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
