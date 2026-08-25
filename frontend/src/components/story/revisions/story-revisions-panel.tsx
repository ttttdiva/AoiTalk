"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { toast } from "sonner";
import { ArrowDown, ArrowUp, Flag, Loader2 } from "lucide-react";
import { AlertDialog } from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { storyApi } from "@/lib/story/api";
import { objectOf, type StoryRevisionView } from "@/lib/story/view-model";
import { useDiffWorker } from "@/components/story/hooks/use-diff-worker";
import { formatRelativeTime } from "@/lib/utils";
import type { StoryDiffGranularity, StoryDiffOperation } from "@/lib/story/diff.worker";

/** origin の日本語ラベル（設計 §6.2 の 9 契機）。 */
const ORIGIN_LABELS: Record<string, string> = {
  manual: "手動保存",
  checkpoint: "チェックポイント",
  pre_ai: "AI実行前",
  ai_generate: "AI生成",
  ai_edit: "AI修正",
  pre_restore: "復元前",
  restore: "復元",
  auto: "自動保存",
  import: "取り込み",
};

const GRANULARITIES: ReadonlyArray<{ value: StoryDiffGranularity; label: string }> = [
  { value: "character", label: "文字" },
  { value: "word", label: "単語" },
  { value: "paragraph", label: "段落" },
];

function originLabel(origin: string): string {
  return ORIGIN_LABELS[origin] || origin;
}

function formatCharDelta(delta: number): string {
  if (delta > 0) return `+${delta}`;
  if (delta < 0) return `-${Math.abs(delta)}`;
  return "±0";
}

function revisionBodyOf(value: unknown): string {
  const body = objectOf(value).body;
  return typeof body === "string" ? body : "";
}

type DiffChangeBlocks = {
  /** operations と同じ添字で、その操作が属する変更ブロック番号（未変更は -1）。 */
  blockOf: number[];
  /** その操作が変更ブロックの先頭かどうか（ジャンプ先の ref を張る位置）。 */
  isStart: boolean[];
  count: number;
};

/** 連続する追加/削除を 1 つの「変更箇所」としてまとめ、ジャンプ用の番号を振る。 */
function computeChangeBlocks(operations: StoryDiffOperation[]): DiffChangeBlocks {
  const blockOf: number[] = [];
  const isStart: boolean[] = [];
  let count = 0;
  let inChange = false;
  for (const operation of operations) {
    if (!operation.added && !operation.removed) {
      blockOf.push(-1);
      isStart.push(false);
      inChange = false;
      continue;
    }
    if (!inChange) count += 1;
    isStart.push(!inChange);
    blockOf.push(count - 1);
    inChange = true;
  }
  return { blockOf, isStart, count };
}

function StoryDiffOperationSpans({
  operations,
  blocks,
  activeChange = -1,
  blockRefs,
}: {
  operations: StoryDiffOperation[];
  blocks: DiffChangeBlocks;
  activeChange?: number;
  blockRefs?: React.RefObject<Map<number, HTMLSpanElement>>;
}) {
  return operations.map((operation, index) => {
    const block = blocks.blockOf[index];
    const tone = operation.removed
      ? "bg-destructive/15 text-destructive line-through"
      : operation.added
        ? "bg-primary/15 text-primary underline decoration-primary/60"
        : "";
    const active = block >= 0 && block === activeChange ? " rounded-sm ring-2 ring-ring" : "";
    const trackable = block >= 0 && blocks.isStart[index] && blockRefs;
    return (
      <span
        key={index}
        data-change-block={block >= 0 ? block : undefined}
        ref={
          trackable
            ? (node: HTMLSpanElement) => {
                blockRefs.current.set(block, node);
                return () => {
                  blockRefs.current.delete(block);
                };
              }
            : undefined
        }
        className={`${tone}${active}`}
      >
        {operation.value}
      </span>
    );
  });
}

/** 差分をそのまま流し込む軽量プレビュー（AI修正案プレビューなどから利用）。 */
export function StoryDiffPreview({
  oldBody,
  newBody,
  granularity = "word",
  className = "",
}: {
  oldBody: string;
  newBody: string;
  granularity?: StoryDiffGranularity;
  className?: string;
}) {
  const { operations, pending } = useDiffWorker(oldBody, newBody, granularity);
  const blocks = useMemo(() => computeChangeBlocks(operations), [operations]);
  return (
    <div className={`whitespace-pre-wrap ${className}`}>
      {pending ? (
        <span className="text-muted-foreground">
          <Loader2 className="mr-1 inline size-3 animate-spin" />
          差分を計算中…
        </span>
      ) : (
        <StoryDiffOperationSpans operations={operations} blocks={blocks} />
      )}
    </div>
  );
}

/** 履歴差分モーダル（設計 §4.11）。2 版を選択してから開く。 */
function StoryRevisionDiffDialog({
  open,
  onOpenChange,
  episodeId,
  oldRevision,
  newRevision,
  onRequestRestore,
}: {
  open: boolean;
  onOpenChange: (next: boolean) => void;
  episodeId: string;
  oldRevision: StoryRevisionView;
  newRevision: StoryRevisionView;
  onRequestRestore: (revNo: number) => void;
}) {
  const [granularity, setGranularity] = useState<StoryDiffGranularity>("word");
  const [cursor, setCursor] = useState(0);
  const blockRefs = useRef(new Map<number, HTMLSpanElement>());

  const oldData = useSWR(open ? `story-revision-body:${episodeId}:${oldRevision.revNo}` : null, () =>
    storyApi.getRevision(episodeId, oldRevision.revNo),
  );
  const newData = useSWR(open ? `story-revision-body:${episodeId}:${newRevision.revNo}` : null, () =>
    storyApi.getRevision(episodeId, newRevision.revNo),
  );
  const loading = (!oldData.data && !oldData.error) || (!newData.data && !newData.error);
  const oldBody = revisionBodyOf(oldData.data);
  const newBody = revisionBodyOf(newData.data);

  const { operations, pending } = useDiffWorker(oldBody, newBody, granularity);
  const blocks = useMemo(() => computeChangeBlocks(operations), [operations]);
  // 粒度切替で変更箇所の総数が変わっても破綻しないよう、カーソルは描画時に丸める。
  const activeChange = blocks.count ? ((cursor % blocks.count) + blocks.count) % blocks.count : -1;

  const jump = (delta: number) => {
    if (!blocks.count) return;
    const next = ((activeChange + delta) % blocks.count + blocks.count) % blocks.count;
    setCursor(next);
    blockRefs.current.get(next)?.scrollIntoView({ block: "center" });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="4xl" className="flex max-h-[85vh] flex-col gap-3">
        <DialogHeader>
          <DialogTitle>差分を見る</DialogTitle>
          <DialogDescription>
            #{oldRevision.revNo}（{originLabel(oldRevision.origin)}） → #{newRevision.revNo}（
            {originLabel(newRevision.origin)}）
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex gap-1 rounded-lg bg-muted p-0.5">
            {GRANULARITIES.map((item) => (
              <Button
                key={item.value}
                variant={granularity === item.value ? "secondary" : "ghost"}
                size="xs"
                aria-pressed={granularity === item.value}
                onClick={() => {
                  setGranularity(item.value);
                  setCursor(0);
                }}
              >
                {item.label}
              </Button>
            ))}
          </div>
          <span className="ml-auto text-xs text-muted-foreground">
            変更箇所 {blocks.count ? activeChange + 1 : 0} / {blocks.count}
          </span>
          <Button variant="outline" size="icon-xs" aria-label="前の変更箇所へ" disabled={!blocks.count} onClick={() => jump(-1)}>
            <ArrowUp />
          </Button>
          <Button variant="outline" size="icon-xs" aria-label="次の変更箇所へ" disabled={!blocks.count} onClick={() => jump(1)}>
            <ArrowDown />
          </Button>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
          <span className="text-destructive line-through">削除</span>
          <span className="text-primary underline decoration-primary/60">追加</span>
          <span className="ml-auto">
            {oldRevision.charCount}字 → {newRevision.charCount}字（
            {formatCharDelta(newRevision.charCount - oldRevision.charCount)}）
          </span>
        </div>
        <div className="min-h-0 flex-1 overflow-auto rounded-md border border-border bg-muted/20 p-3 text-sm leading-7 whitespace-pre-wrap">
          {loading || pending ? (
            <span className="text-muted-foreground">
              <Loader2 className="mr-1 inline size-3 animate-spin" />
              差分を計算中…
            </span>
          ) : blocks.count ? (
            <StoryDiffOperationSpans
              operations={operations}
              blocks={blocks}
              activeChange={activeChange}
              blockRefs={blockRefs}
            />
          ) : (
            <span className="text-muted-foreground">この 2 版に差分はありません。</span>
          )}
        </div>
        <p className="text-[11px] text-muted-foreground">
          復元しても履歴は消えません。復元前の状態と復元結果が新しいリビジョンとして積まれます。
        </p>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            閉じる
          </Button>
          <Button onClick={() => onRequestRestore(oldRevision.revNo)}>#{oldRevision.revNo} を復元</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** インスペクタの履歴タブ（設計 §4.8）。 */
export function StoryRevisionsPanel({
  episodeId,
  revisions,
  onCheckpoint,
  onRestore,
}: {
  episodeId: string | null;
  revisions: StoryRevisionView[];
  onCheckpoint: () => void;
  onRestore: (revNo: number) => Promise<void>;
}) {
  const [selection, setSelection] = useState<{ episodeId: string | null; revNos: number[] }>({
    episodeId: null,
    revNos: [],
  });
  const [diffOpen, setDiffOpen] = useState(false);
  const [restoreTarget, setRestoreTarget] = useState<number | null>(null);
  const [restoreFromDiff, setRestoreFromDiff] = useState(false);
  // 復元後、親が履歴を再取得して新しい版が現れた時点でトーストを出す。
  const restoreBaseline = useRef<number | null>(null);

  const byRevNo = useMemo(
    () => new Map(revisions.map((revision) => [revision.revNo, revision])),
    [revisions],
  );
  const selectedRevNos =
    selection.episodeId === episodeId ? selection.revNos.filter((revNo) => byRevNo.has(revNo)) : [];
  const comparable = selectedRevNos.length === 2 && Boolean(episodeId);
  const oldRevision = comparable ? byRevNo.get(Math.min(...selectedRevNos)) : undefined;
  const newRevision = comparable ? byRevNo.get(Math.max(...selectedRevNos)) : undefined;

  useEffect(() => {
    const baseline = restoreBaseline.current;
    if (baseline === null) return;
    const latest = revisions[0];
    if (!latest || latest.revNo <= baseline) return;
    restoreBaseline.current = null;
    toast.success(`#${latest.revNo} 復元 を作成しました`);
  }, [revisions]);

  const toggleSelection = (revNo: number) => {
    setSelection((previous) => {
      const base = previous.episodeId === episodeId ? previous.revNos : [];
      const next = base.includes(revNo) ? base.filter((value) => value !== revNo) : [...base, revNo].slice(-2);
      return { episodeId, revNos: next };
    });
  };

  const requestRestore = (revNo: number, fromDiff: boolean) => {
    setRestoreFromDiff(fromDiff);
    if (fromDiff) setDiffOpen(false);
    setRestoreTarget(revNo);
  };

  const runRestore = async (revNo: number) => {
    setRestoreTarget(null);
    setRestoreFromDiff(false);
    restoreBaseline.current = revisions[0]?.revNo ?? 0;
    try {
      await onRestore(revNo);
      setSelection({ episodeId, revNos: [] });
    } catch (error) {
      restoreBaseline.current = null;
      toast.error(error instanceof Error ? error.message : "復元できませんでした");
    }
  };

  const hint =
    selectedRevNos.length === 0
      ? "2つの版を選ぶと差分を比較できます"
      : selectedRevNos.length === 1
        ? "もう1つ選んでください"
        : `#${Math.min(...selectedRevNos)} ↔ #${Math.max(...selectedRevNos)} を比較`;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold">履歴</h3>
          <p className="text-[11px] text-muted-foreground">新しい順・無制限保存</p>
        </div>
        <span className="text-[11px] text-muted-foreground">{revisions.length} 件</span>
      </div>
      <div className="flex gap-2">
        <Button variant="outline" size="sm" className="flex-1" onClick={onCheckpoint}>
          <Flag />
          チェックポイント
        </Button>
        <Button
          variant={comparable ? "default" : "outline"}
          size="sm"
          className="flex-1"
          disabled={!comparable}
          onClick={() => setDiffOpen(true)}
        >
          差分を見る
        </Button>
      </div>
      <p className="text-[11px] text-muted-foreground">{hint}</p>
      {revisions.length ? (
        revisions.map((revision, index) => {
          const previous = revisions[index + 1];
          const delta = previous ? revision.charCount - previous.charCount : null;
          const selected = selectedRevNos.includes(revision.revNo);
          return (
            <div
              key={revision.id}
              className={`rounded-md border bg-card p-2 ${selected ? "border-primary" : "border-border"}`}
            >
              <div className="flex items-start gap-2">
                <Checkbox
                  className="mt-0.5"
                  aria-label={`#${revision.revNo} を比較対象にする`}
                  checked={selected}
                  onCheckedChange={() => toggleSelection(revision.revNo)}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 text-xs">
                    <span className="font-semibold">#{revision.revNo}</span>
                    <Badge variant="outline" className="text-[10px]">
                      {originLabel(revision.origin)}
                    </Badge>
                    <span className="ml-auto text-muted-foreground">{revision.charCount}字</span>
                  </div>
                  <div className="mt-0.5 flex items-center gap-2 text-[11px] text-muted-foreground">
                    <span title={revision.createdAt ?? undefined}>
                      {formatRelativeTime(revision.createdAt) || "日時不明"}
                    </span>
                    {delta === null ? null : (
                      <span
                        className={delta > 0 ? "text-primary" : delta < 0 ? "text-destructive" : undefined}
                      >
                        {formatCharDelta(delta)}
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">{revision.message || "メッセージなし"}</p>
                  <Button
                    variant="link"
                    size="sm"
                    className="mt-1 h-6 px-0"
                    onClick={() => requestRestore(revision.revNo, false)}
                  >
                    この版を復元
                  </Button>
                </div>
              </div>
            </div>
          );
        })
      ) : (
        <p className="text-xs text-muted-foreground">履歴はありません。</p>
      )}
      {episodeId && oldRevision && newRevision ? (
        <StoryRevisionDiffDialog
          open={diffOpen}
          onOpenChange={setDiffOpen}
          episodeId={episodeId}
          oldRevision={oldRevision}
          newRevision={newRevision}
          onRequestRestore={(revNo) => requestRestore(revNo, true)}
        />
      ) : null}
      <AlertDialog
        open={restoreTarget !== null}
        title="この版を復元しますか？"
        description={`#${restoreTarget ?? 0} の内容で本文を上書きします。\n現在の本文は新しいリビジョンとして履歴に残るため、元に戻せます。`}
        confirmLabel="復元する"
        onConfirm={() => {
          if (restoreTarget !== null) void runRestore(restoreTarget);
        }}
        onCancel={() => {
          const backToDiff = restoreFromDiff;
          setRestoreTarget(null);
          setRestoreFromDiff(false);
          if (backToDiff) setDiffOpen(true);
        }}
      />
    </div>
  );
}
