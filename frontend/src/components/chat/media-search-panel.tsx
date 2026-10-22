"use client";

import { useEffect, useRef, useState } from "react";
import { FileText, Loader2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import { Checkbox } from "@/components/ui/checkbox";
import { mediaSearchApi, mediaSearchLink, type ChatSearchAttachment, type MediaQueryPlan, type MediaUnifiedHit, type MediaUnifiedSearchResponse, type SearchEvidence } from "@/lib/media-search-api";

type Props = { open: boolean; query: string; projectId?: string | null; spaceId?: string | null; sessionId?: string | null; messageId?: string | null };
const targetLabels = { docs: "Docs", knowledge: "Knowledge", conversation: "会話", media: "画像・音声・資料", files: "ファイル" };
const statusLabels: Record<string, string> = { prepared: '準備済み', not_processed: '未処理', complete: "検索済み", completed: "検索済み", ready: "検索済み", failed: "失敗", unavailable: "利用不可", partial: "部分検索", not_searched: "未検索", skipped: "未検索", pending: "待機中" };
const targets = Object.keys(targetLabels) as NonNullable<MediaQueryPlan['targets']>;

type State = { key: string; phase: 'loading' | 'ready' | 'error' | 'cancelled'; response?: MediaUnifiedSearchResponse; error?: string };
function hitKey(hit: MediaUnifiedHit) { return JSON.stringify([hit.target_kind, hit.unit_id, hit.locator]); }
function location(hit: MediaUnifiedHit) {
  return [hit.page_number ? `${hit.page_number}ページ` : null,
    hit.start_seconds != null ? `${hit.start_seconds}–${hit.end_seconds ?? '?'}秒` : null,
    hit.text_start != null ? `文字 ${hit.text_start}–${hit.text_end ?? '?'}${hit.text_length != null ? ` / ${hit.text_length}` : ''}` : null,
    hit.citation?.derived_text_start != null ? `索引本文 ${hit.citation.derived_text_start}–${hit.citation.derived_text_end}文字` : null,
    Array.isArray(hit.citation?.heading_path) ? `章: ${hit.citation.heading_path.join(' / ')}` : null].filter(Boolean).join(' · ');
}

export function MediaSearchPanel(props: Props) {
  // Closing and switching context discard references, URLs and in-flight reads.
  return props.open ? <SearchContent key={JSON.stringify([props.sessionId, props.messageId, props.projectId, props.spaceId])} {...props} /> : null;
}

function SearchContent({ query, projectId, spaceId, sessionId, messageId }: Props) {
  const [scope, setScope] = useState<MediaQueryPlan['scope']>('all_readable');
  const [mode, setMode] = useState<MediaQueryPlan['mode']>('combined');
  const [selectedTargets, setSelectedTargets] = useState(targets);
  const [attachments, setAttachments] = useState<ChatSearchAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState('');
  const [attachmentRetry, setAttachmentRetry] = useState(0);
  const [attachmentsLoading, setAttachmentsLoading] = useState(Boolean(sessionId && messageId));
  const [indices, setIndices] = useState<number[]>([]);
  const [source, setSource] = useState<NonNullable<MediaQueryPlan['sources']>[number] | null>(null);
  const [retry, setRetry] = useState(0);
  const [state, setState] = useState<State | null>(null);
  const [evidence, setEvidence] = useState<{ key: string; hit: string; value: SearchEvidence } | null>(null);
  const [readError, setReadError] = useState<{ key: string; text: string } | null>(null);
  const [reading, setReading] = useState<{ key: string; hit: string } | null>(null);
  const searchController = useRef<AbortController | null>(null);
  const readController = useRef<AbortController | null>(null);
  const project = scope === 'project' || scope === 'current_context' ? projectId ?? null : null;
  const space = scope === 'space' || scope === 'project' || scope === 'current_context' ? spaceId ?? null : null;
  const trimmed = query.trim();
  const key = JSON.stringify([trimmed, scope, mode, selectedTargets, indices, source, project, space, retry]);
  const [previousKey, setPreviousKey] = useState(key);
  if (previousKey !== key) {
    // Discard the data itself: returning to a previous query/scope must never
    // resurrect an authorized URL or quote from an earlier request.
    setPreviousKey(key);
    setState(null);
    setEvidence(null);
    setReadError(null);
    setReading(null);
  }
  const current = state?.key === key ? state : null;
  const canSearch = Boolean(trimmed || source || indices.length) && selectedTargets.length > 0;

  useEffect(() => {
    if (!sessionId || !messageId) return;
    const controller = new AbortController();
    mediaSearchApi.attachments(sessionId, messageId, controller.signal).then((value) => {
      if (!controller.signal.aborted) { setAttachments(value); setAttachmentError(''); }
    }).catch(() => { if (!controller.signal.aborted) setAttachmentError('保存済み添付を取得できません'); })
      .finally(() => { if (!controller.signal.aborted) setAttachmentsLoading(false); });
    return () => controller.abort();
  }, [sessionId, messageId, attachmentRetry]);

  useEffect(() => {
    const controller = new AbortController();
    searchController.current = controller;
    readController.current?.abort();
    if (!canSearch) return () => controller.abort();
    const timer = window.setTimeout(() => {
      setState({ key, phase: 'loading' });
      const plan: MediaQueryPlan = { version: 1, instruction: trimmed, sources: source ? [source] : [], mode, scope, targets: selectedTargets, limit: 20 };
      // Source authority stays in the active conversation context even when
      // the target corpus expands to all readable partitions.
      mediaSearchApi.unified({ plan, project_id: projectId ?? null, space_id: spaceId ?? null,
        use_current_attachments: indices.length > 0, attachment_indices: indices.length ? indices : null,
        session_id: sessionId ?? null, message_id: messageId ?? null, query: '', image: null, limit: 20 }, controller.signal)
        .then((response) => { if (!controller.signal.aborted) setState({ key, phase: 'ready', response }); })
        .catch((error: unknown) => { if (!controller.signal.aborted) setState({ key, phase: 'error', error: error instanceof Error ? error.message : '検索を利用できません' }); });
    }, 180);
    const read = readController;
    return () => { window.clearTimeout(timer); controller.abort(); read.current?.abort(); };
  }, [key, canSearch, trimmed, mode, scope, selectedTargets, source, projectId, spaceId, indices, sessionId, messageId]);

  async function inspect(hit: MediaUnifiedHit) {
    readController.current?.abort();
    const controller = new AbortController();
    readController.current = controller;
    const id = hitKey(hit);
    setReading({ key, hit: id }); setEvidence(null); setReadError(null);
    try {
      const value = await mediaSearchApi.readHit(hit, { project_id: project, space_id: space }, controller.signal);
      if (!controller.signal.aborted) setEvidence({ key, hit: id, value });
    } catch (error) {
      if (!controller.signal.aborted) setReadError({ key, text: error instanceof Error ? error.message : '資料を取得できません' });
    } finally { if (!controller.signal.aborted) setReading(null); }
  }

  return <div className="flex min-h-0 max-h-[65dvh] flex-col overflow-y-auto" role="tabpanel" aria-label="統合検索結果">
    <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
      <AppSelect aria-label="資料の検索範囲" size="sm" value={scope} onValueChange={(value) => setScope(value as MediaQueryPlan['scope'])}>
        <option value="all_readable">閲覧できるすべて</option>
        {sessionId && <option value="current_context">現在のキャラクター・コンテキスト</option>}
        {spaceId && <option value="space">現在のSpace</option>}
        {projectId && <option value="project">現在のプロジェクト</option>}
      </AppSelect>
      <p className="w-full text-xs text-muted-foreground">{scope === 'current_context'
        ? '現在の会話のキャラクターとプロジェクトで探します。'
        : scope === 'project' ? '現在のプロジェクト内の、閲覧できる全キャラクターで探します。'
        : scope === 'space' ? '現在のSpace内の、閲覧できる全キャラクターで探します。'
        : '閲覧できる全プロジェクト・キャラクターで探します。'}</p>
      <AppSelect aria-label="手がかりの検索方法" size="sm" value={mode} onValueChange={(value) => setMode(value as MediaQueryPlan['mode'])}>
        <option value="combined">手がかりをまとめる</option><option value="separate">手がかりごとに探す</option>
      </AppSelect>
      <p className="w-full text-xs text-muted-foreground">複数の手がかりをまとめて関連度で探します。すべての条件への一致を保証する検索ではありません。</p>
      {targets.map((target) => <label key={target} className="flex items-center gap-1 text-xs"><Checkbox checked={selectedTargets.includes(target)} onCheckedChange={() => setSelectedTargets((prev) => prev.includes(target) ? prev.filter((t) => t !== target) : [...prev, target])} />{targetLabels[target]}</label>)}
      {attachments.map((attachment) => <label key={attachment.index} className="inline-flex max-w-full items-center gap-1 rounded bg-muted px-2 py-1 text-xs"><Checkbox checked={indices.includes(attachment.index)} onCheckedChange={() => setIndices((prev) => prev.includes(attachment.index) ? prev.filter((i) => i !== attachment.index) : [...prev, attachment.index])} /><span className="truncate">{attachment.index}: {attachment.filename} · {attachment.kind}</span></label>)}
      {attachmentsLoading && <p role="status" className="text-xs">保存済み添付を取得中...</p>}
      {attachmentError && <div role="alert" className="flex items-center gap-2 text-xs text-destructive">{attachmentError}
        <Button size="xs" variant="outline" onClick={() => { setAttachmentError(''); setAttachmentsLoading(true); setAttachmentRetry((prev) => prev + 1); }}>添付一覧を再取得</Button>
      </div>}
      {source && <span className="inline-flex max-w-full items-center gap-1 rounded bg-muted px-2 py-1 text-xs">選択した原本で検索<button type="button" aria-label="原本検索を解除" onClick={() => setSource(null)}><X className="size-3" /></button></span>}
      {current?.phase === 'loading' && <Button size="xs" variant="outline" onClick={() => { searchController.current?.abort(); readController.current?.abort(); setState({ key, phase: 'cancelled' }); setEvidence(null); }}>取消</Button>}
      {current && current.phase !== 'loading' && <Button size="xs" variant="outline" onClick={() => setRetry((prev) => prev + 1)}>再試行</Button>}
    </div>
    <div className="min-h-0 shrink-0 px-3 py-2" aria-live="polite">
      {!canSearch && <p className="py-6 text-center text-sm text-muted-foreground">自然な言葉や保存済み添付を手がかりに、Docs・Knowledge・会話・資料を検索できます</p>}
      {canSearch && (!current || current.phase === 'loading') && <p role="status" className="flex items-center justify-center gap-2 py-6 text-sm"><Loader2 className="size-4 animate-spin" />検索中...</p>}
      {current?.phase === 'error' && <p role="alert" className="py-4 text-sm text-destructive">{current.error}</p>}
      {current?.phase === 'cancelled' && <p role="status">検索を取り消しました</p>}
      {current?.response && <>
        <div className="flex flex-wrap gap-2 text-xs">{targets.map((target) => {
          const lane = current.response?.lane_status.find((status) => status.target_kind === target);
          return <span key={target}>{targetLabels[target]}: {lane ? statusLabels[lane.status] ?? lane.status : '未検索'}</span>;
        })}</div>
        {!current.response.complete && <p role="status" className="py-2 text-xs">検索は完了していません。未検索・部分検索・失敗の範囲を確認してください。</p>}
        {current.response.query_sources.map((s, index) => <p key={index} className="py-1 text-xs">手がかり{index + 1}: {statusLabels[s.status] ?? s.status} · {s.processed_units}/{s.total_units ?? '?'}区間{Boolean(s.processed_ranges?.length) && ` · 処理範囲 ${JSON.stringify(s.processed_ranges)}`}{s.reason && ` · ${s.reason}`}{Boolean(s.warnings?.length) && ` · ${s.warnings?.join('、')}`}</p>)}
        {current.response.warnings.length > 0 && <p role="status" className="py-2 text-xs">{current.response.warnings.join('、')}</p>}
        {current.response.complete && current.response.results.length === 0 && <p className="py-6 text-center text-sm text-muted-foreground">見つかりません</p>}
        {current.response.results.map((hit) => {
          const id = hitKey(hit);
          const active = evidence?.key === key && evidence.hit === id ? evidence.value : null;
          const original = mediaSearchLink(active?.original_url);
          const conversation = mediaSearchLink(active?.conversation_url);
          return <article key={id} className="border-b py-3 last:border-b-0"><div className="flex items-start gap-3"><FileText className="mt-1 size-5 shrink-0 text-muted-foreground" /><div className="min-w-0 flex-1">
            <p className="break-words text-sm font-medium">{hit.title}</p><p className="text-xs text-muted-foreground">{targetLabels[hit.target_kind]} · {location(hit)}</p>
            <p className="mt-1 line-clamp-3 break-words text-xs">{active?.text ?? hit.text}</p>
            {active && !active.complete && <p className="text-xs text-muted-foreground">引用の一部を表示しています。原本で全体を確認できます。</p>}
            {Boolean(hit.matched_query_sources?.length) && <p className="text-xs text-muted-foreground">一致した手がかり: {hit.matched_query_sources?.map((match) => {
              const index = current.response?.query_sources.findIndex((s) => s.source === match.source) ?? -1;
              return index >= 0 ? `手がかり${index + 1}` : '原本参照';
            }).join('、')}</p>}
            <div className="mt-2 flex flex-wrap gap-2"><Button size="xs" variant="outline" disabled={reading?.key === key && reading.hit === id} onClick={() => void inspect(hit)}>{reading?.key === key && reading.hit === id ? '確認中...' : '資料を確認'}</Button>
              {hit.source_kind && hit.source_id && <Button size="xs" variant="ghost" onClick={() => setSource({ source_kind: hit.source_kind!, source_id: hit.source_id!, selector: { pages: hit.page_number ? [hit.page_number] : [], frames: [], seconds: null, characters: null }, text: null })}>この原本で検索</Button>}
              {original && <a href={original} target="_blank" rel="noopener noreferrer" className="text-xs underline">原本を開く</a>}{conversation && <a href={conversation} className="text-xs underline">元の会話へ</a>}
            </div></div></div></article>;
        })}
      </>}
      {readError?.key === key && <p role="alert" className="py-3 text-sm text-destructive">{readError.text}</p>}
    </div>
  </div>;
}
