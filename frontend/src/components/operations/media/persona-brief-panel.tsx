"use client";

import { useLayoutEffect, useRef, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { mediaResearchApi } from "@/lib/media-operations-research-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { PersonaDelegationRequestPanel } from "./persona-delegation-request";

export function PersonaBriefPanel({ scope, selectedContentItemId, onSelect, onContinue }: {
  scope: PersonaWorkspaceScope; selectedContentItemId: string;
  onSelect: (id: string) => void; onContinue: () => void;
}) {
  const programs = useSWR(["persona-programs", scope.personaId, scope.projectId, scope.ownerUserId],
    () => mediaResearchApi.listPrograms(scope.projectId), { shouldRetryOnError: false, revalidateOnFocus: false });
  const items = useSWR(["persona-briefs", scope.personaId, scope.personaRevisionId, scope.projectId, scope.ownerUserId],
    () => mediaResearchApi.listContentItems(undefined, { limit: 100, offset: 0, projectId: scope.projectId }),
    { shouldRetryOnError: false, revalidateOnFocus: false });
  const availablePrograms = programs.data?.filter(p => p.persona_id === scope.personaId && p.project_id === scope.projectId && p.owner_user_id === scope.ownerUserId && p.state !== "archived") ?? [];
  const content = items.data?.filter(item => item.persona_revision_id === scope.personaRevisionId && item.project_id === scope.projectId && item.owner_user_id === scope.ownerUserId && item.status !== "archived") ?? [];
  const [programId, setProgramId] = useState("");
  const [title, setTitle] = useState("");
  const [brief, setBrief] = useState("");
  const [objective, setObjective] = useState("");
  const [contentType, setContentType] = useState("x_post");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const lock = useRef(false);
  const key = useRef(crypto.randomUUID());
  const request = useRef(0);
  const context = useRef({ mounted: false, scopeKey: "" });
  const scopeKey = JSON.stringify([scope.ownerUserId, scope.projectId, scope.personaId, scope.personaRevisionId]);
  useLayoutEffect(() => {
    context.current = { mounted: true, scopeKey }; request.current += 1;
    return () => { context.current.mounted = false; request.current += 1; };
  }, [scopeKey]);
  const currentRequest = (id: number, pinnedScope: string) => request.current === id && context.current.mounted && context.current.scopeKey === pinnedScope;
  const selectedProgram = !programs.error && !items.error ? availablePrograms.find(p => p.id === programId) : undefined;
  const selectedItem = content.find(item => item.id === selectedContentItemId);
  const reload = async () => { await Promise.all([programs.mutate(), items.mutate()]); };

  const createProgram = async () => {
    if (lock.current) return;
    const id = ++request.current; const pinnedScope = scopeKey;
    lock.current = true; setBusy(true); setError("");
    try {
      const program = await mediaResearchApi.createProgram({ persona_id: scope.personaId, name: `${scope.displayName}の制作計画`,
        objective: "本人がbrief・QA・previewを確認して公開する", state: "draft", enabled: false,
        cadence: "manual", content_type: "social_content", draft_generation_policy: "human_review",
      }, key.current);
      if (!currentRequest(id, pinnedScope)) return;
      if (program.persona_id !== scope.personaId || program.project_id !== scope.projectId || program.owner_user_id !== scope.ownerUserId) throw new Error("保存された計画の所属を確認できません。");
      await programs.mutate(); if (!currentRequest(id, pinnedScope)) return;
      setProgramId(program.id); key.current = crypto.randomUUID();
    } catch { if (currentRequest(id, pinnedScope)) { setUncertain(true); setError("制作計画の保存結果を確認できません。一覧を更新して保存済みの計画を確認してください。"); } }
    finally { lock.current = false; if (currentRequest(id, pinnedScope)) setBusy(false); }
  };

  return <section aria-label="制作brief" className="space-y-4">
    <div><h2 className="inline-flex items-center gap-1 text-lg font-semibold">何を作るか<HelpPopover label="何を作るか">この人格の目的・読者・表現を、本人入力の創作briefとして保存します。研究を採用した企画も保存済みの一覧から選べます。</HelpPopover></h2></div>
    {(programs.error || items.error || error) && <div role="alert" className="rounded-lg border border-destructive/30 p-3 text-sm text-destructive">{error || "この人格の制作briefを取得できませんでした。"}<Button type="button" variant="outline" size="sm" onClick={() => void reload()}>brief一覧を更新</Button></div>}
    {uncertain && <p role="status" className="text-sm">保存結果の確認が必要です。一覧の保存済み記録から作業を再開してください。</p>}
    <div className="grid items-start gap-4 lg:grid-cols-2">
      <form className="space-y-3 rounded-xl border bg-card p-4" aria-label="人格の制作briefを保存" onSubmit={async event => {
        event.preventDefault();
        if (lock.current || uncertain || !selectedProgram || !title.trim() || !brief.trim()) return;
        const id = ++request.current; const pinnedScope = scopeKey;
        lock.current = true; setBusy(true); setError("");
        try {
          const input = { editorial_program_id: selectedProgram.id,
            title: title.trim(), brief, objective: objective.trim() || null,
            persona_revision_id: scope.personaRevisionId, content_type: contentType, input_kind: "user_authored" as const,
          };
          const item = await mediaResearchApi.createContentItem(input, key.current);
          if (!currentRequest(id, pinnedScope)) return;
          if (item.project_id !== scope.projectId || item.owner_user_id !== scope.ownerUserId || item.persona_revision_id !== scope.personaRevisionId || item.input_kind !== "user_authored" || item.brief !== brief) throw new Error("保存されたbriefの所属・種別・原文を確認できません。");
          await items.mutate(); if (!currentRequest(id, pinnedScope)) return;
          onSelect(item.id); key.current = crypto.randomUUID();
        } catch { if (currentRequest(id, pinnedScope)) { setUncertain(true); setError("briefの保存結果を確認できません。一覧を更新して保存済みのbriefを確認してください。"); } }
        finally { lock.current = false; if (currentRequest(id, pinnedScope)) setBusy(false); }
      }}>
        <label className="block space-y-1.5 text-sm"><span>制作計画</span><AppSelect value={programId} onValueChange={setProgramId} disabled={busy} aria-label="制作計画"><option value="">制作計画を選択</option>{availablePrograms.map(p => <option key={p.id} value={p.id}>{p.current_revision.name}</option>)}</AppSelect></label>
        {!programs.isLoading && !programs.error && !availablePrograms.length && <Button type="button" variant="outline" disabled={busy || uncertain} onClick={() => void createProgram()}>この人格の制作計画を用意</Button>}
        <label className="block space-y-1.5 text-sm"><span>企画名</span><Input required maxLength={500} value={title} onChange={event => setTitle(event.target.value)} disabled={busy || uncertain} /></label>
        <label className="block space-y-1.5 text-sm"><span>制作brief</span><Textarea required maxLength={8000} rows={5} value={brief} onChange={event => setBrief(event.target.value)} disabled={busy || uncertain} placeholder="届けたい内容、読者、構成、必要な素材、QAの観点" /></label>
        <label className="block space-y-1.5 text-sm"><span>目的・測る成果</span><Input maxLength={4000} value={objective} onChange={event => setObjective(event.target.value)} disabled={busy || uncertain} /></label>
        <label className="block space-y-1.5 text-sm"><span>制作形式</span><AppSelect value={contentType} onValueChange={setContentType} disabled={busy || uncertain} aria-label="制作形式"><option value="x_post">X投稿</option><option value="image">画像・イラスト</option><option value="youtube_video">YouTube動画</option><option value="youtube_short">YouTube Shorts</option><option value="music">音楽</option><option value="social_content">その他の媒体向け</option></AppSelect></label>
        <div className="flex flex-wrap items-center gap-3"><Button type="submit" disabled={busy || uncertain || !selectedProgram || !title.trim() || !brief.trim()}>制作briefを保存</Button><Link className="text-xs text-primary underline" href="/operations?tab=research">研究から企画を採用</Link></div>
      </form>
      <div className="space-y-3 rounded-xl border bg-card p-4"><h3 className="inline-flex items-center gap-1 font-semibold">保存済みの企画<HelpPopover label="保存済みの企画">最新100件を表示しています。過去の企画はResearchから確認できます。</HelpPopover></h3>
        {items.isLoading ? <p role="status">制作briefを確認中…</p> : !items.error && !content.length ? <p className="text-sm text-muted-foreground">この人格の制作briefはまだありません。</p> : null}
        {content.map(item => <button key={item.id} type="button" aria-pressed={item.id === selectedContentItemId} onClick={() => onSelect(item.id)} className={`block w-full rounded-lg border p-3 text-left ${item.id === selectedContentItemId ? "border-primary bg-primary/5" : ""}`}><span className="block break-words text-sm font-medium">{item.title}</span><span className="mt-1 block text-xs text-muted-foreground">{item.content_type} · {item.status}{item.input_kind === "user_authored" ? " · 本人入力" : item.input_kind === "research" ? " · 研究から採用" : ""}</span></button>)}
        {selectedItem && <div className="space-y-3 border-t pt-3"><p className="whitespace-pre-wrap break-words text-sm">{selectedItem.brief}</p><p className="text-xs text-muted-foreground">{selectedItem.objective}</p><Button type="button" variant="outline" onClick={onContinue}>このbriefで計画・生成へ</Button><PersonaDelegationRequestPanel key={`${scope.ownerUserId}:${scope.projectId}:${scope.personaId}:${selectedItem.id}:${selectedItem.content_hash}`} scope={scope} brief={selectedItem} /></div>}
      </div>
    </div>
  </section>;
}
