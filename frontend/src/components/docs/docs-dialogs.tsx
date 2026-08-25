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
  CommandGroup,
  CommandItem,
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
import type { DocsNode } from "./types";
import {
  docsFieldType,
  fieldOptions,
  tagColorStyle,
} from "./docs-utils";
import {
  nodeText,
  type DocsAiPreview,
  type DocsCommandMode,
  type SearchView,
} from "./docs-workspace-shared";
import type { DocsCommandRegistration } from "./hooks/use-docs-command-palette";

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
      <DialogContent size="md">
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
      <DialogContent size="2xl">
        <DialogHeader>
          <DialogTitle>AI候補の確認</DialogTitle>
          <DialogDescription>
            反映前に内容を確認します。破棄すると保存済み候補は「却下」として記録されます。
          </DialogDescription>
        </DialogHeader>
        {preview ? (
          <div className="space-y-3">
            <div className="rounded border bg-muted/20 p-3">
              <div className="mb-1 text-[11px] font-medium text-muted-foreground">対象ノード</div>
              <div className="text-sm font-medium">{nodeText(preview.node)}</div>
            </div>
            {result?.mode === "replace_title" && typeof result.replacement === "string" ? (
              <div className="grid gap-2 md:grid-cols-2">
                <div className="rounded border p-3">
                  <div className="mb-1 text-[11px] font-medium text-muted-foreground">現在のタイトル</div>
                  <div className="text-sm">{nodeText(preview.node)}</div>
                </div>
                <div className="rounded border border-primary/40 bg-primary/5 p-3">
                  <div className="mb-1 text-[11px] font-medium text-muted-foreground">提案タイトル</div>
                  <div className="text-sm">{result.replacement}</div>
                </div>
              </div>
            ) : null}
            {result?.mode === "insert_children" ? (
              <div className="rounded border border-primary/40 bg-primary/5 p-3">
                <div className="mb-2 text-[11px] font-medium text-muted-foreground">追加する子ノード</div>
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

export function DocsCommandItems({
  context,
  mode,
  setMode,
  onClose,
}: {
  context: DocsCommandRegistration;
  mode: DocsCommandMode;
  setMode: (mode: DocsCommandMode) => void;
  onClose: () => void;
}) {
  const {
    selectedNode,
    selectionCount,
    tags,
    fields,
    moveTargets,
    nodeTools,
    onAddChild,
    onOpenSplit,
    onToggleCheckbox,
    onApplyTag,
    onMove,
    onSetView,
    onSetField,
    onRunAi,
    onGoBack,
  } = context;
  const viewItems: Array<{ view: SearchView; label: string; icon: LucideIcon }> = [
    { view: "list", label: "リスト", icon: ListFilter },
    { view: "table", label: "テーブル", icon: Table2 },
    { view: "board", label: "ボード", icon: KanbanSquare },
    { view: "calendar", label: "カレンダー", icon: CalendarDays },
    { view: "cards", label: "カード", icon: Columns2 },
  ];

  const fieldValueItems = selectedNode && mode.kind === "field"
    ? (() => {
        const field = fields.find((item) => item.id === mode.fieldId);
        if (!field) return [];
        const type = docsFieldType(field);
        if (type === "checkbox") return ["true", "false"].map((value) => ({
          label: value === "true" ? "オン" : "オフ",
          value,
          field,
        }));
        const options = fieldOptions(field);
        if (options.length > 0) return options.map((value) => ({ label: value, value, field }));
        return [{ label: `${field.name}をクリア`, value: "", field }];
      })()
    : [];

  return (
    !selectedNode ? (
      <CommandGroup heading="Docsノード操作">
        <CommandItem disabled>ノードを選択してください</CommandItem>
      </CommandGroup>
    ) : mode.kind === "root" ? (
      <>
        {selectionCount > 1 ? (
          <CommandGroup heading="選択中">
            <CommandItem disabled value={`${selectionCount}件選択中`}>
              <CheckSquare className="size-4" />
              {selectionCount}件選択中
            </CommandItem>
          </CommandGroup>
        ) : null}
        {selectionCount > 1 ? <CommandSeparator /> : null}
        <CommandGroup heading="Docsノード操作">
          <CommandItem onSelect={() => { onOpenSplit(selectedNode); onClose(); }} value="右パネルで開く open in right panel">
            <Columns2 className="size-4" />
            右パネルで開く
          </CommandItem>
          <CommandItem onSelect={() => { onAddChild(selectedNode); onClose(); }} value="子ノードを追加 add child node">
            <Plus className="size-4" />
            子ノードを追加
          </CommandItem>
          <CommandItem onSelect={() => { onToggleCheckbox(selectedNode); onClose(); }} value="チェックボックス checkbox toggle">
            <CheckSquare className="size-4" />
            {selectedNode.display_props?.show_checkbox === true ? "チェックボックスを切り替え" : "チェックボックスを追加"}
          </CommandItem>
          <CommandItem onSelect={() => { onRunAi(selectedNode, "continue"); onClose(); }} value="AI 子ノードとして続ける continue generate children">
            <Sparkles className="size-4" />
            AI: 子ノードとして続ける
          </CommandItem>
          <CommandItem onSelect={() => { onRunAi(selectedNode, "rewrite"); onClose(); }} value="AI タイトルを書き換える rewrite title">
            <Sparkles className="size-4" />
            AI: タイトルを書き換える
          </CommandItem>
          <CommandItem onSelect={() => { onRunAi(selectedNode, "extract_tasks"); onClose(); }} value="AI タスクを抽出 extract tasks">
            <Sparkles className="size-4" />
            AI: タスクを抽出
          </CommandItem>
          <CommandItem onSelect={() => { onRunAi(selectedNode, "generate_minutes"); onClose(); }} value="AI 議事録を生成 generate minutes">
            <Sparkles className="size-4" />
            AI: 議事録を生成
          </CommandItem>
          {nodeTools.map((tool) => (
            <CommandItem
              key={tool.command}
              onSelect={() => { onRunAi(selectedNode, tool.command); onClose(); }}
              value={`${tool.command} ${tool.label}`}
            >
              <Sparkles className="size-4" />
              {tool.label}
            </CommandItem>
          ))}
          <CommandItem onSelect={() => setMode({ kind: "move", leaveReference: false })} value="移動する move to">
            <ExternalLink className="size-4" />
            移動する
          </CommandItem>
          <CommandItem onSelect={() => setMode({ kind: "move", leaveReference: true })} value="参照を残して移動する move and leave reference">
            <Link2 className="size-4" />
            参照を残して移動する
          </CommandItem>
          <CommandItem onSelect={() => setMode({ kind: "view" })} value="表示形式を変更 view as">
            <Table2 className="size-4" />
            表示形式を変更
          </CommandItem>
          <CommandItem onSelect={() => { onGoBack(selectedNode); onClose(); }} value="親ノードへ戻る go back parent">
            <ChevronRight className="size-4 rotate-180" />
            親ノードへ戻る
          </CommandItem>
        </CommandGroup>
        <CommandSeparator />
        {fields.length > 0 ? (
          <CommandGroup heading="フィールド">
            {fields.map((field) => (
              <CommandItem key={field.id} value={`${field.name}を設定 set ${field.name}`} onSelect={() => setMode({ kind: "field", fieldId: field.id })}>
                <SlidersHorizontal className="size-4" />
                {field.name}を設定
              </CommandItem>
            ))}
          </CommandGroup>
        ) : null}
        <CommandGroup heading="タグ">
          {tags.map((tag) => (
            <CommandItem key={tag.id} value={`#${tag.name}を追加 tag ${tag.name}`} keywords={[tag.description ?? ""]} onSelect={() => { onApplyTag(selectedNode, tag); onClose(); }}>
              <Hash className="size-4" style={tagColorStyle(tag.color)} />
              #{tag.name}を追加
            </CommandItem>
          ))}
        </CommandGroup>
      </>
    ) : mode.kind === "move" ? (
      <CommandGroup heading={mode.leaveReference ? "参照を残して移動" : "移動先"}>
        {moveTargets.map((target) => (
          <CommandItem key={target.id} value={`${nodeText(target)} ${target.id}`} onSelect={() => { onMove(selectedNode, target, mode.leaveReference); onClose(); }}>
            <ExternalLink className="size-4" />
            <span className="truncate">{nodeText(target)}</span>
          </CommandItem>
        ))}
      </CommandGroup>
    ) : mode.kind === "view" ? (
      <CommandGroup heading="表示形式">
        {viewItems.map((item) => {
          const Icon = item.icon;
          return (
            <CommandItem key={item.view} value={item.label} onSelect={() => { onSetView(selectedNode, item.view); onClose(); }}>
              <Icon className="size-4" />
              {item.label}
            </CommandItem>
          );
        })}
      </CommandGroup>
    ) : mode.kind === "field" ? (
      <CommandGroup heading="フィールド値を設定">
        {fieldValueItems.map((item) => (
          <CommandItem key={`${item.field.id}:${item.value}`} value={`${item.field.name} ${item.label}`} onSelect={() => { onSetField(selectedNode, item.field, item.value); onClose(); }}>
            <Type className="size-4" />
            {item.field.name}: {item.label || "空"}
          </CommandItem>
        ))}
      </CommandGroup>
    ) : (
      <CommandGroup heading="タグ">
        {tags.map((tag) => (
          <CommandItem key={tag.id} value={`#${tag.name}を追加 tag ${tag.name}`} onSelect={() => { onApplyTag(selectedNode, tag); onClose(); }}>
            <Hash className="size-4" style={tagColorStyle(tag.color)} />
            #{tag.name}を追加
          </CommandItem>
        ))}
      </CommandGroup>
    )
  );
}
