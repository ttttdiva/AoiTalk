"use client";

import {
  Plus,
  Settings2,
} from "lucide-react";
import {
  Button,
} from "@/components/ui/button";
import {
  Input,
} from "@/components/ui/input";
import {
  tagColorStyle,
} from "./docs-utils";
import {
  nodeText,
} from "./docs-workspace-shared";
import type {
  DocsField,
  DocsNode,
  DocsSupertag,
} from "./types";
import {
  SupertagConfigPanel,
} from "./docs-supertag-config-panel";

// メイン右パネル（関連コンテンツ / Supertag 設定）の表示切り替え。
export function RightPanel({
  mode,
  selectedNode,
  selectedTag,
  tags,
  nodeTags,
  fields,
  fieldsByTag,
  newTagName,
  setNewTagName,
  onApplyTag,
  onOpenTag,
  onCreateTag,
  onCreateField,
  onUpdateSupertag,
  onUpdateField,
  onCreateSearchNode,
  relatedNodes,
  onOpenNode,
}: {
  mode: "related" | "tags";
  selectedNode: DocsNode | null;
  selectedTag: DocsSupertag | null;
  tags: DocsSupertag[];
  nodeTags: DocsSupertag[];
  fields: DocsField[];
  fieldsByTag: Map<string, DocsField[]>;
  newTagName: string;
  setNewTagName: (value: string) => void;
  onApplyTag: (tagId: string) => void;
  onOpenTag: (tagId: string) => void;
  onCreateTag: () => void;
  onCreateField: (tagId: string, name: string, fieldType: string) => void;
  onUpdateSupertag: (tagId: string, patch: Partial<Pick<DocsSupertag, "name" | "description" | "color" | "icon" | "template_json" | "config_json" | "title_template" | "ai_instructions" | "parent_supertag_id">>) => void;
  onUpdateField: (fieldId: string, patch: Partial<Pick<DocsField, "name" | "field_type" | "required" | "options_json" | "sort_order">> & { default_value_json?: unknown }) => void;
  onCreateSearchNode: (tag: DocsSupertag) => void;
  relatedNodes: DocsNode[];
  onOpenNode: (nodeId: string) => void;
}) {
  const heading =
    mode === "tags"
      ? "Supertags"
      : "Related content";
  return (
    <div className="p-3">
      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
        <Settings2 className="size-4" />
        {heading}
      </div>
      {mode === "tags" ? (
        selectedTag ? (
          <SupertagConfigPanel
            tag={selectedTag}
            tags={tags}
            fields={fieldsByTag.get(selectedTag.id) ?? fields.filter((field) => field.supertag_id === selectedTag.id)}
            onCreateField={onCreateField}
            onUpdateSupertag={onUpdateSupertag}
            onUpdateField={onUpdateField}
          />
        ) : (
          <div className="space-y-3">
            {selectedNode ? (
              <div className="space-y-2">
                <div className="text-xs font-medium text-muted-foreground">{nodeText(selectedNode)} のタグ</div>
                {nodeTags.length === 0 ? (
                  <div className="rounded border border-dashed p-2 text-xs text-muted-foreground">付与済みのタグはありません</div>
                ) : (
                  nodeTags.map((tag) => {
                    const tagFields = fieldsByTag.get(tag.id) ?? fields.filter((field) => field.supertag_id === tag.id);
                    return (
                      <div key={tag.id} className="rounded border p-2">
                        <button type="button" onClick={() => onOpenTag(tag.id)} className="flex w-full items-center justify-between rounded px-1 py-0.5 text-left text-sm hover:bg-accent">
                          <span style={tagColorStyle(tag.color)}>#{tag.name}</span>
                        </button>
                        {tagFields.length > 0 ? (
                          <div className="mt-1 flex flex-wrap gap-1 px-1">
                            {tagFields.map((field) => (
                              <span key={field.id} className="rounded border px-1.5 py-0.5 text-[11px] text-muted-foreground">{field.name}</span>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    );
                  })
                )}
              </div>
            ) : null}
            <div className="space-y-2">
              <div className="text-xs font-medium text-muted-foreground">タグを追加</div>
              <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-2">
                <Input value={newTagName} onChange={(event) => setNewTagName(event.target.value)} placeholder="タグ名で検索または作成" className="h-8" />
                <Button type="button" size="sm" variant="secondary" onClick={onCreateTag} title="新規タグを作成">
                  <Plus className="size-4" />
                </Button>
              </div>
              {newTagName.trim() ? (
                <div className="space-y-2">
                  {tags
                    .filter((tag) => tag.name.toLowerCase().includes(newTagName.trim().toLowerCase()))
                    .slice(0, 50)
                    .map((tag) => (
                      <div key={tag.id} className="rounded border p-2">
                        <button type="button" onClick={() => onOpenTag(tag.id)} className="flex w-full items-center justify-between rounded px-1 py-1 text-left text-sm hover:bg-accent">
                          <span style={tagColorStyle(tag.color)}>#{tag.name}</span>
                          {nodeTags.some((item) => item.id === tag.id) ? <span className="text-xs text-muted-foreground">applied</span> : null}
                        </button>
                        <div className="mt-1 flex gap-1">
                          <Button type="button" variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={() => onApplyTag(tag.id)}>Apply</Button>
                          <Button type="button" variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={() => onCreateSearchNode(tag)}>Search node</Button>
                        </div>
                      </div>
                    ))}
                </div>
              ) : null}
            </div>
          </div>
        )
      ) : null}
      {mode === "related" ? (
        <div className="space-y-2">
          <div className="text-xs text-muted-foreground">{selectedNode ? `${nodeText(selectedNode)} のタグ関連ノード` : "ノードを選択してください"}</div>
          {selectedNode && relatedNodes.length === 0 ? <div className="rounded border border-dashed p-3 text-xs text-muted-foreground">No related tag matches</div> : null}
          {relatedNodes.map((node) => (
            <button key={node.id} type="button" onClick={() => onOpenNode(node.id)} className="block w-full truncate rounded border px-2 py-1.5 text-left text-sm hover:bg-accent">
              {nodeText(node)}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
