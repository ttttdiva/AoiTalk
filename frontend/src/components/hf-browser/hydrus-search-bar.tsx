"use client";

/**
 * Hydrus 検索バー: タグ/数値フィルタを入力して検索し、結果を ExplorerListResponse に
 * 変換して ExplorerContext にセットする。結果は既存の FileGrid/FileList で表示される。
 *
 * タグ構文 (参考リポジトリと同等):
 *   - "rating:like/1"    → 好評価1件以上
 *   - "system:filetype:image" 等のシステムタグも素通しで渡す
 *   - 数値フィルタ: "width>1000", "filesize<50MB" のような system: プリセットを
 *     UI 側でサジェストする（現状は入力のまま transmit）
 */

import { useCallback, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Search, X } from "lucide-react";
import {
  hydrusSearch,
  hydrusGetMetadata,
  type HydrusFileMetadata,
} from "@/lib/hf-api";
import type { ExplorerFile, ExplorerListResponse } from "@/lib/explorer-api";
import { buildHydrusPath } from "@/lib/hydrus/virtual-path";

interface Props {
  onResults: (data: ExplorerListResponse) => void;
  onError: (msg: string) => void;
}

function guessExt(mime?: string): string {
  if (!mime) return "";
  if (mime.startsWith("image/")) return "." + mime.slice(6);
  if (mime.startsWith("video/")) return "." + mime.slice(6);
  if (mime.startsWith("audio/")) return "." + mime.slice(6);
  return "";
}

function metadataToFile(m: HydrusFileMetadata): ExplorerFile {
  const ext = guessExt(m.mime);
  return {
    name: (m.hash ? m.hash.slice(0, 12) : `file_${m.file_id}`) + ext,
    path: buildHydrusPath(m.file_id),
    type: m.mime || "application/octet-stream",
    size: m.size,
    modified_at: m.time_modified
      ? new Date(m.time_modified * 1000).toISOString()
      : undefined,
    extension: ext,
  };
}

export function HydrusSearchBar({ onResults, onError }: Props) {
  const [tagInput, setTagInput] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [perPage] = useState(60);
  const [totalPages, setTotalPages] = useState(1);
  const [total, setTotal] = useState(0);

  const runSearch = useCallback(
    async (targetTags: string[], targetPage: number) => {
      if (targetTags.length === 0) {
        onResults({
          success: true,
          current_path: "Hydrus",
          parent_path: null,
          can_go_up: false,
          directories: [],
          files: [],
          total_items: 0,
        });
        return;
      }
      setLoading(true);
      try {
        const searchResp = await hydrusSearch({
          tags: targetTags,
          page: targetPage,
          perPage,
        });
        setTotalPages(searchResp.total_pages || 1);
        setTotal(searchResp.total || 0);

        const ids = searchResp.file_ids || [];
        let files: ExplorerFile[] = [];
        if (ids.length > 0) {
          const meta = await hydrusGetMetadata(ids, true);
          const metaById = new Map(meta.metadata.map((m) => [m.file_id, m]));
          files = ids
            .map((id) => metaById.get(id))
            .filter((m): m is HydrusFileMetadata => m != null)
            .map(metadataToFile);
        }
        onResults({
          success: true,
          current_path: "Hydrus",
          parent_path: null,
          can_go_up: false,
          directories: [],
          files,
          total_items: files.length,
        });
      } catch (e) {
        onError(`Hydrus 検索失敗: ${String(e)}`);
      } finally {
        setLoading(false);
      }
    },
    [onResults, onError, perPage],
  );

  const handleAddTag = () => {
    const t = tagInput.trim();
    if (!t) return;
    const next = [...tags, t];
    setTags(next);
    setTagInput("");
    setPage(1);
    runSearch(next, 1);
  };

  const handleRemoveTag = (idx: number) => {
    const next = tags.filter((_, i) => i !== idx);
    setTags(next);
    setPage(1);
    runSearch(next, 1);
  };

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleAddTag();
    }
  };

  const goPage = (p: number) => {
    if (p < 1 || p > totalPages) return;
    setPage(p);
    runSearch(tags, p);
  };

  return (
    <div className="flex flex-col gap-2 rounded-md border bg-muted/20 p-2">
      <div className="flex items-center gap-2">
        <Search className="size-4 text-muted-foreground" />
        <Input
          value={tagInput}
          onChange={(e) => setTagInput(e.target.value)}
          onKeyDown={handleKey}
          placeholder="タグ追加（例: rating:like/1, system:filetype:image, width>1000）Enterで追加"
          className="h-8"
        />
        <Button size="sm" onClick={handleAddTag} disabled={!tagInput.trim()}>
          追加
        </Button>
      </div>
      {tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {tags.map((t, i) => (
            <button
              key={`${t}-${i}`}
              className="inline-flex items-center gap-1 rounded bg-primary/10 px-2 py-0.5 text-xs hover:bg-primary/20"
              onClick={() => handleRemoveTag(i)}
              title="クリックで削除"
            >
              {t}
              <X className="size-3" />
            </button>
          ))}
        </div>
      )}
      {(loading || total > 0) && (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {loading ? (
            <span>検索中...</span>
          ) : (
            <>
              <span>
                {total}件 / {page}/{totalPages}ページ
              </span>
              <Button
                size="xs"
                variant="ghost"
                onClick={() => goPage(page - 1)}
                disabled={page <= 1}
              >
                前
              </Button>
              <Button
                size="xs"
                variant="ghost"
                onClick={() => goPage(page + 1)}
                disabled={page >= totalPages}
              >
                次
              </Button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
