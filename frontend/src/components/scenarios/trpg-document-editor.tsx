"use client";

import { useState, useEffect, useMemo } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import { Plus, Trash2, Loader2 } from "lucide-react";
import {
  pyFetch,
  selectClassName,
  TRPG_STRUCTURE_NODE_TYPES,
  parseStructureText,
  makeStructureNodeId,
  type TRPGScenarioDocument,
  type TRPGStructure,
  type TRPGStructureNode,
} from "@/lib/scenarios-page-utils";

function TRPGDocumentEditor({
  documents,
  scenarioId,
  onUpdate,
}: {
  documents: TRPGScenarioDocument[];
  scenarioId: string;
  onUpdate: () => void;
}) {
  const [selectedDocumentId, setSelectedDocumentId] = useState<string>("");
  const [ruleset, setRuleset] = useState("coc6");
  const [sourceLabel, setSourceLabel] = useState("");
  const [sourceText, setSourceText] = useState("");
  const [structureText, setStructureText] = useState("{}");
  const [newNodeType, setNewNodeType] = useState("location");
  const [newNodeTitle, setNewNodeTitle] = useState("");
  const [newNodeSummary, setNewNodeSummary] = useState("");
  const [newNodeTags, setNewNodeTags] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const selectedDocument = documents.find((doc) => doc.id === selectedDocumentId);
  const structureDraft = useMemo(() => {
    try {
      return { structure: parseStructureText(structureText), error: "" };
    } catch {
      return {
        structure: null as TRPGStructure | null,
        error: "構造メタデータJSONを解釈できません。",
      };
    }
  }, [structureText]);
  const structureNodes = structureDraft.structure?.nodes ?? [];

  useEffect(() => {
    if (!selectedDocumentId && documents.length > 0) {
      setSelectedDocumentId(documents[0].id);
    }
  }, [documents, selectedDocumentId]);

  useEffect(() => {
    if (!selectedDocument) {
      setRuleset("coc6");
      setSourceLabel("");
      setSourceText("");
      setStructureText("{}");
      return;
    }
    setRuleset(selectedDocument.ruleset || "generic");
    setSourceLabel(selectedDocument.source_label || "");
    setSourceText(selectedDocument.source_text || "");
    setStructureText(
      JSON.stringify(selectedDocument.structure || {}, null, 2),
    );
  }, [selectedDocument]);

  const handleNew = () => {
    setSelectedDocumentId("");
    setRuleset("coc6");
    setSourceLabel("");
    setSourceText("");
    setStructureText("{}");
    setNewNodeType("location");
    setNewNodeTitle("");
    setNewNodeSummary("");
    setNewNodeTags("");
    setError("");
  };

  const handleAddStructureNode = () => {
    const title = newNodeTitle.trim();
    if (!title) {
      setError("ノード名を入力してください。");
      return;
    }
    try {
      const structure = parseStructureText(structureText);
      const node: TRPGStructureNode = {
        id: makeStructureNodeId(newNodeType, title, structure.nodes),
        type: newNodeType,
        title,
        summary: newNodeSummary.trim(),
        body: "",
        tags: newNodeTags
          .split(",")
          .map((tag) => tag.trim())
          .filter(Boolean),
        metadata: {},
      };
      setStructureText(
        JSON.stringify(
          { ...structure, nodes: [...structure.nodes, node] },
          null,
          2,
        ),
      );
      setNewNodeTitle("");
      setNewNodeSummary("");
      setNewNodeTags("");
      setError("");
    } catch {
      setError("構造メタデータJSONを直してからノードを追加してください。");
    }
  };

  const handleRemoveStructureNode = (nodeId: string) => {
    try {
      const structure = parseStructureText(structureText);
      setStructureText(
        JSON.stringify(
          {
            ...structure,
            nodes: structure.nodes.filter((node) => node.id !== nodeId),
            links: structure.links.filter(
              (link) => link.from !== nodeId && link.to !== nodeId,
            ),
          },
          null,
          2,
        ),
      );
      setError("");
    } catch {
      setError("構造メタデータJSONを直してからノードを削除してください。");
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setError("");
    try {
      const structure = parseStructureText(structureText);
      await pyFetch(`/scenarios/${scenarioId}/trpg-documents`, {
        method: "PUT",
        body: JSON.stringify({
          id: selectedDocumentId || undefined,
          ruleset,
          source_label: sourceLabel,
          source_text: sourceText,
          structure,
        }),
      });
      onUpdate();
    } catch (err) {
      console.error("TRPG本文保存失敗:", err);
      setError("保存できませんでした。JSONと本文を確認してください。");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!selectedDocumentId) return;
    setSaving(true);
    setError("");
    try {
      await pyFetch(
        `/scenarios/${scenarioId}/trpg-documents/${selectedDocumentId}`,
        { method: "DELETE" },
      );
      setSelectedDocumentId("");
      onUpdate();
    } catch (err) {
      console.error("TRPG本文削除失敗:", err);
      setError("削除できませんでした。");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <select
            value={selectedDocumentId}
            onChange={(e) => setSelectedDocumentId(e.target.value)}
            className={selectClassName + " max-w-72"}
          >
            <option value="">新規本文</option>
            {documents.map((doc) => (
              <option key={doc.id} value={doc.id}>
                {doc.ruleset || "generic"} / {doc.source_label || doc.id}
              </option>
            ))}
          </select>
          <Badge variant="outline">{sourceText.length.toLocaleString()}字</Badge>
        </div>
        <Button variant="outline" size="sm" onClick={handleNew}>
          <Plus className="mr-1 size-3.5" />
          新規
        </Button>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <div className="space-y-1.5">
          <Label>ルールセット</Label>
          <select
            value={ruleset}
            onChange={(e) => setRuleset(e.target.value)}
            className={selectClassName}
          >
            <option value="coc6">coc6</option>
            <option value="coc7">coc7</option>
            <option value="generic">generic</option>
          </select>
        </div>
        <div className="col-span-2 space-y-1.5">
          <Label>出典</Label>
          <Input
            value={sourceLabel}
            onChange={(e) => setSourceLabel(e.target.value)}
            placeholder="URL / ファイル名 / 出典メモ"
          />
        </div>
      </div>

      <div className="space-y-1.5">
        <Label>本文</Label>
        <LongTextEditor
          value={sourceText}
          onChange={setSourceText}
          minHeight={420}
          maxHeight={640}
          fontFamily="monospace"
          fontSize={12}
        />
      </div>

      <div className="space-y-1.5">
        <Label>構造メタデータ JSON</Label>
        <div className="rounded-md border bg-muted/20 p-3">
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-32 flex-1 space-y-1">
              <Label className="text-xs">種別</Label>
              <select
                value={newNodeType}
                onChange={(e) => setNewNodeType(e.target.value)}
                className={selectClassName}
              >
                {TRPG_STRUCTURE_NODE_TYPES.map((nodeType) => (
                  <option key={nodeType.value} value={nodeType.value}>
                    {nodeType.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="min-w-48 flex-[2] space-y-1">
              <Label className="text-xs">名前</Label>
              <Input
                value={newNodeTitle}
                onChange={(e) => setNewNodeTitle(e.target.value)}
                placeholder="部屋 / NPC / アイテム / 手掛かり"
              />
            </div>
            <div className="min-w-48 flex-[3] space-y-1">
              <Label className="text-xs">要約</Label>
              <Input
                value={newNodeSummary}
                onChange={(e) => setNewNodeSummary(e.target.value)}
                placeholder="GMが参照する短い要約"
              />
            </div>
            <div className="min-w-40 flex-1 space-y-1">
              <Label className="text-xs">タグ</Label>
              <Input
                value={newNodeTags}
                onChange={(e) => setNewNodeTags(e.target.value)}
                placeholder="探索, 重要"
              />
            </div>
            <Button type="button" size="sm" onClick={handleAddStructureNode}>
              <Plus className="mr-1 size-3.5" />
              追加
            </Button>
          </div>
          <div className="mt-3 grid gap-2 md:grid-cols-2">
            {structureNodes.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                場所、NPC、アイテム、手掛かり、脅威、結末などをノードとして分けておくと、AI GMが本文を参照しやすくなります。
              </p>
            ) : (
              structureNodes.map((node) => (
                <div
                  key={node.id}
                  className="flex min-w-0 items-start justify-between gap-2 rounded-md border bg-background p-2"
                >
                  <div className="min-w-0 space-y-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge variant="secondary">{node.type}</Badge>
                      <span className="truncate text-sm font-medium">
                        {node.title}
                      </span>
                    </div>
                    {node.summary && (
                      <p className="line-clamp-2 text-xs text-muted-foreground">
                        {node.summary}
                      </p>
                    )}
                    {node.tags && node.tags.length > 0 && (
                      <p className="truncate text-[11px] text-muted-foreground">
                        {node.tags.join(", ")}
                      </p>
                    )}
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="size-7 shrink-0 text-muted-foreground"
                    onClick={() => handleRemoveStructureNode(node.id)}
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              ))
            )}
          </div>
        </div>
        {structureDraft.error && (
          <p className="text-xs text-destructive">{structureDraft.error}</p>
        )}
        <LongTextEditor
          value={structureText}
          onChange={setStructureText}
          minHeight={220}
          maxHeight={420}
          language="json"
          fontFamily="monospace"
          fontSize={12}
        />
      </div>

      {error && <p className="text-xs text-destructive">{error}</p>}

      <div className="flex justify-between gap-2">
        <Button
          variant="destructive"
          size="sm"
          onClick={handleDelete}
          disabled={!selectedDocumentId || saving}
        >
          <Trash2 className="mr-1 size-3.5" />
          削除
        </Button>
        <Button size="sm" onClick={handleSave} disabled={saving || !sourceText.trim()}>
          {saving && <Loader2 className="mr-1 size-3.5 animate-spin" />}
          保存
        </Button>
      </div>
    </div>
  );
}

export { TRPGDocumentEditor };
