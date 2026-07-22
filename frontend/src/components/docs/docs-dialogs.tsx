"use client";

import { useRef } from "react";
import {
  CalendarDays,
  CheckSquare,
  ChevronRight,
  Columns2,
  ExternalLink,
  Hash,
  KanbanSquare,
  Link2,
  ListFilter,
  Plus,
  SlidersHorizontal,
  Sparkles,
  Table2,
  Type,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { DocsField, DocsNode, DocsSupertag } from "./types";
import {
  docsFieldType,
  fieldOptions,
  tagColorStyle,
} from "./docs-utils";
import {
  nodeText,
  type DocsAiCommand,
  type DocsAiPreview,
  type DocsCommandMode,
  type DocsSupertagTool,
  type SearchView,
} from "./docs-workspace-shared";

export function AliasEditorDialog({
  node,
  onOpenChange,
  onSave,
}: {
  node: DocsNode | null;
  onOpenChange: (open: boolean) => void;
  onSave: (node: DocsNode, aliases: string[]) => Promise<void>;
}) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const save = async () => {
    if (!node) return;
    const aliases = Array.from(new Set(
      (textareaRef.current?.value ?? "")
        .split(/[\n,]/)
        .map((item) => item.trim())
        .filter(Boolean),
    )).slice(0, 20);
    await onSave(node, aliases);
  };

  return (
    <Dialog open={Boolean(node)} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>エイリアス編集</DialogTitle>
          <DialogDescription>Ctrl+Pのページ検索で使う別名を1行ずつ入力します。</DialogDescription>
        </DialogHeader>
        <textarea
          key={node?.id ?? "alias-editor"}
          ref={textareaRef}
          className="min-h-32 rounded-md border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring"
          defaultValue={(node?.aliases ?? []).join("\n")}
          placeholder={"イントラ\nintra-mart"}
        />
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>キャンセル</Button>
          <Button type="button" onClick={() => void save()}>保存</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function DocsAiPreviewDialog({
  preview,
  onApply,
  onReject,
  onOpenChange,
}: {
  preview: DocsAiPreview | null;
  onApply: () => void;
  onReject: () => void;
  onOpenChange: (open: boolean) => void;
}) {
  const result = preview?.result;
  const lines = Array.isArray(result?.lines) ? result.lines.map(String) : [];
  return (
    <Dialog open={Boolean(preview)} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>AI候補の確認</DialogTitle>
          <DialogDescription>
            反映前に内容を確認します。破棄すると保存済み候補は rejected として記録されます。
          </DialogDescription>
        </DialogHeader>
        {preview ? (
          <div className="space-y-3">
            <div className="rounded border bg-muted/20 p-3">
              <div className="mb-1 text-[11px] font-medium uppercase text-muted-foreground">Target</div>
              <div className="text-sm font-medium">{nodeText(preview.node)}</div>
            </div>
            {result?.mode === "replace_title" && typeof result.replacement === "string" ? (
              <div className="grid gap-2 md:grid-cols-2">
                <div className="rounded border p-3">
                  <div className="mb-1 text-[11px] font-medium uppercase text-muted-foreground">Current</div>
                  <div className="text-sm">{nodeText(preview.node)}</div>
                </div>
                <div className="rounded border border-primary/40 bg-primary/5 p-3">
                  <div className="mb-1 text-[11px] font-medium uppercase text-muted-foreground">Proposed</div>
                  <div className="text-sm">{result.replacement}</div>
                </div>
              </div>
            ) : null}
            {result?.mode === "insert_children" ? (
              <div className="rounded border border-primary/40 bg-primary/5 p-3">
                <div className="mb-2 text-[11px] font-medium uppercase text-muted-foreground">Child nodes to insert</div>
                <div className="max-h-72 overflow-auto font-mono text-xs leading-6">
                  {lines.map((line, index) => (
                    <div key={`${line}-${index}`} className="whitespace-pre-wrap border-b border-border/60 py-1 last:border-0">
                      {line}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onReject}>破棄</Button>
          <Button type="button" onClick={onApply}>反映</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function DocsCommandPalette({
  open,
  onOpenChange,
  mode,
  setMode,
  selectedNode,
  selectionCount,
  tags,
  fields,
  moveTargets,
  onAddChild,
  onOpenSplit,
  onToggleCheckbox,
  onApplyTag,
  onMove,
  onSetView,
  onSetField,
  onRunAi,
  onGoBack,
  nodeTools,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: DocsCommandMode;
  setMode: (mode: DocsCommandMode) => void;
  selectedNode: DocsNode | null;
  selectionCount: number;
  tags: DocsSupertag[];
  fields: DocsField[];
  moveTargets: DocsNode[];
  nodeTools: DocsSupertagTool[];
  onAddChild: (node: DocsNode) => void;
  onOpenSplit: (node: DocsNode) => void;
  onToggleCheckbox: (node: DocsNode) => void;
  onApplyTag: (node: DocsNode, tag: DocsSupertag) => void;
  onMove: (node: DocsNode, target: DocsNode, leaveReference: boolean) => void;
  onSetView: (node: DocsNode, view: SearchView) => void;
  onSetField: (node: DocsNode, field: DocsField, value: string) => void;
  onRunAi: (node: DocsNode, command: DocsAiCommand) => void;
  onGoBack: (node: DocsNode) => void;
}) {
  const close = () => onOpenChange(false);
  const viewItems: Array<{ view: SearchView; label: string; icon: LucideIcon }> = [
    { view: "list", label: "View as list", icon: ListFilter },
    { view: "table", label: "View as table", icon: Table2 },
    { view: "board", label: "View as board", icon: KanbanSquare },
    { view: "calendar", label: "View as calendar", icon: CalendarDays },
    { view: "cards", label: "View as cards", icon: Columns2 },
  ];

  const fieldValueItems = selectedNode && mode.kind === "field"
    ? (() => {
        const field = fields.find((item) => item.id === mode.fieldId);
        if (!field) return [];
        const type = docsFieldType(field);
        if (type === "checkbox") return ["true", "false"].map((value) => ({ label: value, value, field }));
        const options = fieldOptions(field);
        if (options.length > 0) return options.map((value) => ({ label: value, value, field }));
        return [{ label: `Clear ${field.name}`, value: "", field }];
      })()
    : [];

  return (
    <CommandDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Docs command palette"
      description="Docs node commands"
      className="max-w-xl"
    >
      <Command>
        <CommandInput
          placeholder={
            mode.kind === "move"
              ? mode.leaveReference ? "Move and leave reference to..." : "Move to..."
              : mode.kind === "tag"
                ? "Add tag..."
                : mode.kind === "view"
                  ? "View as..."
                  : mode.kind === "field"
                    ? "Set field..."
                    : "Enter command..."
          }
        />
        <CommandList className="max-h-80">
          <CommandEmpty>見つかりません</CommandEmpty>
          {!selectedNode ? (
            <CommandGroup heading="Docs">
              <CommandItem disabled>ノードを選択してください</CommandItem>
            </CommandGroup>
          ) : mode.kind === "root" ? (
            <>
              {selectionCount > 1 ? (
                <CommandGroup heading="Selection">
                  <CommandItem disabled value={`${selectionCount} nodes selected`}>
                    <CheckSquare className="size-4" />
                    {selectionCount} nodes selected
                  </CommandItem>
                </CommandGroup>
              ) : null}
              {selectionCount > 1 ? <CommandSeparator /> : null}
              <CommandGroup heading="Command">
                <CommandItem onSelect={() => { onOpenSplit(selectedNode); close(); }} value="open in right panel">
                  <Columns2 className="size-4" />
                  Open in right panel
                </CommandItem>
                <CommandItem onSelect={() => { onAddChild(selectedNode); close(); }} value="add child node">
                  <Plus className="size-4" />
                  Add child node
                </CommandItem>
                <CommandItem onSelect={() => { onToggleCheckbox(selectedNode); close(); }} value="add checkbox toggle checkbox">
                  <CheckSquare className="size-4" />
                  {selectedNode.display_props?.show_checkbox === true ? "Toggle checkbox" : "Add checkbox"}
                </CommandItem>
                <CommandItem onSelect={() => { onRunAi(selectedNode, "continue"); close(); }} value="ai continue generate children">
                  <Sparkles className="size-4" />
                  AI: continue as child nodes
                </CommandItem>
                <CommandItem onSelect={() => { onRunAi(selectedNode, "rewrite"); close(); }} value="ai rewrite title">
                  <Sparkles className="size-4" />
                  AI: rewrite title
                </CommandItem>
                <CommandItem onSelect={() => { onRunAi(selectedNode, "extract_tasks"); close(); }} value="ai extract tasks">
                  <Sparkles className="size-4" />
                  AI: extract tasks
                </CommandItem>
                <CommandItem onSelect={() => { onRunAi(selectedNode, "generate_minutes"); close(); }} value="ai generate minutes 議事録生成">
                  <Sparkles className="size-4" />
                  AI: 議事録生成
                </CommandItem>
                {nodeTools.map((tool) => (
                  <CommandItem
                    key={tool.command}
                    onSelect={() => { onRunAi(selectedNode, tool.command); close(); }}
                    value={`${tool.command} ${tool.label}`}
                  >
                    <Sparkles className="size-4" />
                    {tool.label}
                  </CommandItem>
                ))}
                <CommandItem onSelect={() => setMode({ kind: "move", leaveReference: false })} value="move to">
                  <ExternalLink className="size-4" />
                  Move to
                </CommandItem>
                <CommandItem onSelect={() => setMode({ kind: "move", leaveReference: true })} value="move and leave reference">
                  <Link2 className="size-4" />
                  Move and leave reference to
                </CommandItem>
                <CommandItem onSelect={() => setMode({ kind: "view" })} value="view as">
                  <Table2 className="size-4" />
                  View as
                </CommandItem>
                <CommandItem onSelect={() => { onGoBack(selectedNode); close(); }} value="go back parent">
                  <ChevronRight className="size-4 rotate-180" />
                  Go back
                </CommandItem>
              </CommandGroup>
              <CommandSeparator />
              {fields.length > 0 ? (
                <CommandGroup heading="Fields">
                  {fields.map((field) => (
                    <CommandItem key={field.id} value={`set ${field.name}`} onSelect={() => setMode({ kind: "field", fieldId: field.id })}>
                      <SlidersHorizontal className="size-4" />
                      Set {field.name}
                    </CommandItem>
                  ))}
                </CommandGroup>
              ) : null}
              <CommandGroup heading="Tags">
                {tags.map((tag) => (
                  <CommandItem key={tag.id} value={`tag ${tag.name}`} keywords={[tag.description ?? ""]} onSelect={() => { onApplyTag(selectedNode, tag); close(); }}>
                    <Hash className="size-4" style={tagColorStyle(tag.color)} />
                    Add tag #{tag.name}
                  </CommandItem>
                ))}
              </CommandGroup>
            </>
          ) : mode.kind === "move" ? (
            <CommandGroup heading={mode.leaveReference ? "Move and leave reference to" : "Move to"}>
              {moveTargets.map((target) => (
                <CommandItem key={target.id} value={`${nodeText(target)} ${target.id}`} onSelect={() => { onMove(selectedNode, target, mode.leaveReference); close(); }}>
                  <ExternalLink className="size-4" />
                  <span className="truncate">{nodeText(target)}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          ) : mode.kind === "view" ? (
            <CommandGroup heading="View as">
              {viewItems.map((item) => {
                const Icon = item.icon;
                return (
                  <CommandItem key={item.view} value={item.label} onSelect={() => { onSetView(selectedNode, item.view); close(); }}>
                    <Icon className="size-4" />
                    {item.label}
                  </CommandItem>
                );
              })}
            </CommandGroup>
          ) : mode.kind === "field" ? (
            <CommandGroup heading="Set field">
              {fieldValueItems.map((item) => (
                <CommandItem key={`${item.field.id}:${item.value}`} value={`${item.field.name} ${item.label}`} onSelect={() => { onSetField(selectedNode, item.field, item.value); close(); }}>
                  <Type className="size-4" />
                  {item.field.name}: {item.label || "empty"}
                </CommandItem>
              ))}
            </CommandGroup>
          ) : (
            <CommandGroup heading="Tags">
              {tags.map((tag) => (
                <CommandItem key={tag.id} value={`tag ${tag.name}`} onSelect={() => { onApplyTag(selectedNode, tag); close(); }}>
                  <Hash className="size-4" style={tagColorStyle(tag.color)} />
                  Add tag #{tag.name}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
        </CommandList>
      </Command>
    </CommandDialog>
  );
}
