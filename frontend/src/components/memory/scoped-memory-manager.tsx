"use client";

import { useCallback, useMemo, useState } from "react";
import useSWR from "swr";
import {
  AlertTriangle,
  ArrowRightLeft,
  Check,
  History,
  Loader2,
  Pencil,
  Pin,
  PinOff,
  Plus,
  RefreshCw,
  Send,
  Trash2,
  X,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { useConfirm } from "@/hooks/use-confirm";
import {
  memoryApi,
  type MemoryScope,
  type ScopedMemory,
  type ScopedMemoryListOptions,
} from "@/lib/ecc-api";

type MemoryTab = "user" | "project" | "task" | "session" | "candidate" | "history";

const TAB_LABELS: Record<MemoryTab, string> = {
  user: "ユーザー",
  project: "プロジェクト",
  task: "タスク",
  session: "セッション",
  candidate: "候補",
  history: "履歴",
};

const SCOPE_LABELS: Record<MemoryScope, string> = {
  global: "全体",
  user: "ユーザー",
  project: "プロジェクト",
  task: "タスク",
  session: "セッション",
};

function dateTime(value?: string | null): string {
  return value ? new Date(value).toLocaleString("ja-JP") : "—";
}

function evidenceText(memory: ScopedMemory): string {
  const refs = memory.evidence_refs ?? [];
  if (refs.length === 0) return "根拠なし";
  return refs
    .map((ref) =>
      String(ref.label ?? ref.path ?? ref.value ?? ref.memory_id ?? ref.knowledge_node_id ?? ref.type ?? "根拠"),
    )
    .join(" / ");
}

function memoryReason(memory: ScopedMemory): string {
  const structured = memory.structured_data ?? {};
  const projection = memory.projection_metadata ?? {};
  return String(
    structured.reason ??
      structured.evidence ??
      projection.reason ??
      memory.rejection_reason ??
      memory.source_type ??
      "—",
  );
}

function filtersFor(
  tab: MemoryTab,
  projectId?: string,
): ScopedMemoryListOptions {
  if (tab === "candidate") {
    return { status: "candidate", ...(projectId ? { project_id: projectId } : {}) };
  }
  if (tab === "history") {
    return { include_history: true, ...(projectId ? { project_id: projectId } : {}) };
  }
  return {
    scope: tab,
    ...(tab === "project" && projectId ? { project_id: projectId } : {}),
  };
}

type EditorState = {
  memory?: ScopedMemory;
  content: string;
  memoryType: string;
  scope: MemoryScope;
  scopeId: string;
  importance: number;
  evidence: string;
};

export function ScopedMemoryManager({
  projectId,
  projectName,
  projectOnly = false,
  readOnly = false,
}: {
  projectId?: string;
  projectName?: string;
  projectOnly?: boolean;
  readOnly?: boolean;
}) {
  const confirm = useConfirm();
  const [tab, setTab] = useState<MemoryTab>(projectOnly ? "project" : "user");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [moveMemory, setMoveMemory] = useState<ScopedMemory | null>(null);
  const [targetScope, setTargetScope] = useState<MemoryScope>("user");
  const [targetScopeId, setTargetScopeId] = useState("");
  const [lineage, setLineage] = useState<{
    memory: ScopedMemory;
    ancestors: ScopedMemory[];
    descendants: ScopedMemory[];
  } | null>(null);

  const filters = useMemo(() => filtersFor(tab, projectId), [projectId, tab]);
  const memoryKey = `scoped-memory:${tab}:${projectId ?? "all"}`;
  const { data: memories = [], mutate, isLoading } = useSWR(
    memoryKey,
    async () => (await memoryApi.list(filters)).memories ?? [],
    { revalidateOnFocus: false },
  );
  const { data: settings, mutate: mutateSettings } = useSWR(
    `scoped-memory-settings:${projectId ?? "user"}`,
    async () => (await memoryApi.getSettings(projectId)).settings,
    { revalidateOnFocus: false },
  );
  const { data: jobs = [], mutate: mutateJobs } = useSWR(
    tab === "history" ? "scoped-memory-jobs" : null,
    async () => (await memoryApi.listJobs()).jobs ?? [],
    { revalidateOnFocus: false },
  );

  const visibleTabs = projectOnly
    ? (["project", "candidate", "history"] as MemoryTab[])
    : (Object.keys(TAB_LABELS) as MemoryTab[]);

  const run = useCallback(
    async (key: string, action: () => Promise<unknown>) => {
      setBusy(key);
      setError(null);
      try {
        await action();
        await Promise.all([mutate(), mutateJobs()]);
      } catch (err) {
        setError(err instanceof Error ? err.message : "メモリ操作に失敗しました");
      } finally {
        setBusy(null);
      }
    },
    [mutate, mutateJobs],
  );

  const openNew = () => {
    const scope: MemoryScope = tab === "project" && projectId ? "project" : "user";
    setEditor({
      content: "",
      memoryType: "fact",
      scope,
      scopeId: scope === "project" ? projectId ?? "" : "",
      importance: 5,
      evidence: "",
    });
  };

  const openEdit = (memory: ScopedMemory) => {
    setEditor({
      memory,
      content: memory.content,
      memoryType: memory.memory_type,
      scope: memory.scope_type,
      scopeId: memory.scope_id ?? "",
      importance: memory.importance ?? 5,
      evidence: evidenceText(memory) === "根拠なし" ? "" : evidenceText(memory),
    });
  };

  const saveEditor = async () => {
    if (!editor?.content.trim()) return;
    await run(editor.memory?.id ?? "new", async () => {
      if (editor.memory) {
        await memoryApi.update(editor.memory.id, {
          version: editor.memory.version,
          content: editor.content.trim(),
          memory_type: editor.memoryType,
          importance: editor.importance,
        });
      } else {
        const scopeId = editor.scopeId.trim() || undefined;
        await memoryApi.create({
          content: editor.content.trim(),
          memory_type: editor.memoryType,
          scope: editor.scope,
          scope_id: scopeId,
          project_id: editor.scope === "project" ? projectId ?? scopeId : undefined,
          task_id: editor.scope === "task" ? scopeId : undefined,
          session_id: editor.scope === "session" ? scopeId : undefined,
          evidence_refs: editor.evidence.trim()
            ? [{ type: "manual", value: editor.evidence.trim() }]
            : [],
          importance: editor.importance,
          idempotency_key: crypto.randomUUID(),
        });
      }
      setEditor(null);
    });
  };

  const forget = async (memory: ScopedMemory) => {
    if (!(await confirm({
      description: `「${memory.content.slice(0, 40)}」を忘却しますか？履歴は監査用に残ります。`,
      destructive: true,
    }))) return;
    await run(memory.id, () => memoryApi.delete(memory.id, memory.version));
  };

  const decide = (memory: ScopedMemory, approve: boolean) =>
    run(memory.id, () =>
      approve
        ? memoryApi.approve(memory.id, memory.version, "approved_in_memory_ui")
        : memoryApi.reject(memory.id, memory.version, "rejected_in_memory_ui"),
    );

  const togglePin = (memory: ScopedMemory) =>
    run(memory.id, () =>
      memoryApi.update(memory.id, {
        version: memory.version,
        is_pinned: !memory.is_pinned,
      }),
    );

  const openMove = (memory: ScopedMemory) => {
    setMoveMemory(memory);
    setTargetScope(memory.scope_type === "user" ? "project" : "user");
    setTargetScopeId(memory.scope_type === "project" ? "" : projectId ?? "");
  };

  const move = async () => {
    if (!moveMemory) return;
    const scopeId = targetScopeId.trim() || undefined;
    await run(moveMemory.id, async () => {
      await memoryApi.moveScope(moveMemory.id, {
        version: moveMemory.version,
        scope: targetScope,
        scope_id: scopeId,
        project_id: targetScope === "project" ? projectId ?? scopeId : undefined,
        task_id: targetScope === "task" ? scopeId : undefined,
        session_id: targetScope === "session" ? scopeId : undefined,
      });
      setMoveMemory(null);
    });
  };

  const explain = async (memory: ScopedMemory) => {
    await run(`explain:${memory.id}`, async () => {
      const response = await memoryApi.explain(memory.id);
      setLineage({
        memory: response.memory,
        ancestors: response.lineage.ancestors,
        descendants: response.lineage.descendants,
      });
    });
  };

  const updateUserToggle = async (enabled: boolean) => {
    setError(null);
    try {
      await memoryApi.updateSettings({ user_auto_enabled: enabled });
      await mutateSettings();
    } catch (err) {
      setError(err instanceof Error ? err.message : "設定の保存に失敗しました");
    }
  };

  const updateProjectToggle = async (enabled: boolean) => {
    if (!projectId) return;
    setError(null);
    try {
      await memoryApi.updateSettings({
        project_id: projectId,
        project_auto_enabled: enabled,
      });
      await mutateSettings();
    } catch (err) {
      setError(err instanceof Error ? err.message : "設定の保存に失敗しました");
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-muted/20 p-2.5">
        <div className="flex flex-wrap items-center gap-4 text-xs">
          <Label className="flex items-center gap-2">
            <Checkbox
              checked={settings?.user_auto_enabled ?? true}
              disabled={readOnly}
              onCheckedChange={(value) => void updateUserToggle(value === true)}
            />
            自動メモリ（全体）
          </Label>
          {projectId && (
            <Label className="flex items-center gap-2">
              <Checkbox
                checked={settings?.project_auto_enabled ?? settings?.user_auto_enabled ?? true}
                disabled={readOnly}
                onCheckedChange={(value) => void updateProjectToggle(value === true)}
              />
              {projectName ?? "このプロジェクト"}の自動メモリ
            </Label>
          )}
        </div>
        <div className="flex items-center gap-1">
          {!readOnly && (
            <Button size="sm" variant="outline" onClick={openNew}>
              <Plus className="size-3.5" />追加
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => void Promise.all([mutate(), mutateJobs()])}>
            <RefreshCw className="size-3.5" />更新
          </Button>
        </div>
      </div>

      <Tabs value={tab} onValueChange={(value) => setTab(value as MemoryTab)}>
        <TabsList className="max-w-full overflow-x-auto">
          {visibleTabs.map((value) => (
            <TabsTrigger key={value} value={value}>{TAB_LABELS[value]}</TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {error && (
        <div role="alert" className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span>{error}。競合の場合は更新して最新版を確認してください。</span>
        </div>
      )}

      {isLoading ? (
        <div className="flex items-center justify-center py-8 text-sm text-muted-foreground">
          <Loader2 className="mr-2 size-4 animate-spin" />読み込み中...
        </div>
      ) : memories.length === 0 ? (
        <p className="rounded-md border border-dashed p-5 text-center text-sm text-muted-foreground">
          この範囲のメモリはありません。
        </p>
      ) : (
        <div className="max-h-[34rem] space-y-2 overflow-auto pr-1">
          {memories.map((memory) => (
            <article key={memory.id} className="rounded-md border p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">{memory.content}</p>
                  <div className="mt-2 flex flex-wrap gap-1">
                    <Badge variant="outline">{SCOPE_LABELS[memory.scope_type] ?? memory.scope_type}</Badge>
                    <Badge variant="outline">{memory.memory_type}</Badge>
                    <Badge variant={memory.status === "active" ? "secondary" : "outline"}>{memory.status}</Badge>
                    {memory.is_pinned && <Badge variant="secondary"><Pin className="size-3" />固定</Badge>}
                    {memory.sensitivity && memory.sensitivity !== "normal" && (
                      <Badge variant="destructive">{memory.sensitivity}</Badge>
                    )}
                  </div>
                </div>
                <div className="flex shrink-0 flex-wrap justify-end gap-0.5">
                  {!readOnly && memory.status === "candidate" && (
                    <>
                      <Button aria-label="候補を承認" title="承認" size="icon-sm" variant="ghost" onClick={() => void decide(memory, true)} disabled={busy === memory.id}><Check className="size-3.5" /></Button>
                      <Button aria-label="候補を拒否" title="拒否" size="icon-sm" variant="ghost" onClick={() => void decide(memory, false)} disabled={busy === memory.id}><X className="size-3.5" /></Button>
                    </>
                  )}
                  {!readOnly && memory.status === "active" && (
                    <>
                      <Button aria-label="固定を切替" title="固定を切替" size="icon-sm" variant="ghost" onClick={() => void togglePin(memory)} disabled={busy === memory.id}>{memory.is_pinned ? <PinOff className="size-3.5" /> : <Pin className="size-3.5" />}</Button>
                      <Button aria-label="編集" title="編集" size="icon-sm" variant="ghost" onClick={() => openEdit(memory)}><Pencil className="size-3.5" /></Button>
                      <Button aria-label="スコープを移動" title="スコープを移動" size="icon-sm" variant="ghost" onClick={() => openMove(memory)}><ArrowRightLeft className="size-3.5" /></Button>
                      {memory.scope_type === "project" && memory.project_id && (
                        <Button aria-label="案件情報へ昇格" title="案件情報へ明示昇格" size="icon-sm" variant="ghost" onClick={() => void run(memory.id, () => memoryApi.promote(memory.id, memory.version))} disabled={busy === memory.id}><Send className="size-3.5" /></Button>
                      )}
                      <Button aria-label="忘却" title="忘却" size="icon-sm" variant="ghost" className="text-destructive" onClick={() => void forget(memory)} disabled={busy === memory.id}><Trash2 className="size-3.5" /></Button>
                    </>
                  )}
                  <Button aria-label="根拠と履歴" title="根拠と履歴" size="icon-sm" variant="ghost" onClick={() => void explain(memory)} disabled={busy === `explain:${memory.id}`}>
                    {busy === `explain:${memory.id}` ? <Loader2 className="size-3.5 animate-spin" /> : <History className="size-3.5" />}
                  </Button>
                </div>
              </div>
              <dl className="mt-3 grid gap-x-4 gap-y-1 text-[11px] text-muted-foreground sm:grid-cols-2 lg:grid-cols-3">
                <div><dt className="inline font-medium text-foreground">理由: </dt><dd className="inline">{memoryReason(memory)}</dd></div>
                <div><dt className="inline font-medium text-foreground">根拠: </dt><dd className="inline break-all">{evidenceText(memory)}</dd></div>
                <div><dt className="inline font-medium text-foreground">信頼: </dt><dd className="inline">{memory.trust_level ?? "—"} / {Math.round((memory.confidence ?? 0) * 100)}%</dd></div>
                <div><dt className="inline font-medium text-foreground">重要度: </dt><dd className="inline">{memory.importance ?? "—"}</dd></div>
                <div><dt className="inline font-medium text-foreground">作成者: </dt><dd className="inline break-all">{memory.created_by_actor ?? "—"}</dd></div>
                <div><dt className="inline font-medium text-foreground">最終利用: </dt><dd className="inline">{dateTime(memory.last_used_at)}</dd></div>
                <div><dt className="inline font-medium text-foreground">作成: </dt><dd className="inline">{dateTime(memory.created_at)}</dd></div>
                <div><dt className="inline font-medium text-foreground">更新: </dt><dd className="inline">{dateTime(memory.updated_at)}</dd></div>
                <div><dt className="inline font-medium text-foreground">系譜: </dt><dd className="inline break-all">v{memory.version}{memory.supersedes_id ? ` ← ${memory.supersedes_id}` : ""}</dd></div>
              </dl>
            </article>
          ))}
        </div>
      )}

      {tab === "history" && jobs.length > 0 && (
        <section className="space-y-1.5 rounded-md border p-3">
          <h4 className="text-xs font-semibold">自動抽出ジョブ</h4>
          {jobs.map((job) => (
            <div key={job.id} className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
              <Badge variant={job.status === "failed" ? "destructive" : "outline"}>{job.status}</Badge>
              <span>試行 {job.attempts}</span><span>{dateTime(job.created_at)}</span>
              {job.error && <span className="break-all text-destructive">{job.error}</span>}
            </div>
          ))}
        </section>
      )}

      <Dialog open={Boolean(editor)} onOpenChange={(open) => !open && setEditor(null)}>
        <DialogContent size="lg">
          <DialogHeader><DialogTitle>{editor?.memory ? "メモリを編集" : "メモリを追加"}</DialogTitle></DialogHeader>
          {editor && (
            <div className="space-y-3">
              <div className="space-y-1"><Label>内容</Label><Textarea rows={4} value={editor.content} onChange={(event) => setEditor({ ...editor, content: event.target.value })} /></div>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1"><Label>種類</Label><Input value={editor.memoryType} onChange={(event) => setEditor({ ...editor, memoryType: event.target.value })} /></div>
                <div className="space-y-1"><Label>重要度（1〜10）</Label><Input type="number" min={1} max={10} value={editor.importance} onChange={(event) => setEditor({ ...editor, importance: Math.max(1, Math.min(10, Number(event.target.value) || 1)) })} /></div>
              </div>
              {!editor.memory && (
                <>
                  <div className="space-y-1"><Label>スコープ</Label><AppSelect className="w-full" value={editor.scope} onChange={(event) => setEditor({ ...editor, scope: event.target.value as MemoryScope, scopeId: event.target.value === "project" ? projectId ?? editor.scopeId : editor.scopeId })}>{Object.entries(SCOPE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</AppSelect></div>
                  {!(["global", "user"] as string[]).includes(editor.scope) && <div className="space-y-1"><Label>対象ID</Label><Input value={editor.scopeId} onChange={(event) => setEditor({ ...editor, scopeId: event.target.value })} placeholder={`${SCOPE_LABELS[editor.scope]} ID`} /></div>}
                  <div className="space-y-1"><Label>根拠</Label><Textarea rows={2} value={editor.evidence} onChange={(event) => setEditor({ ...editor, evidence: event.target.value })} placeholder="この記憶の出典や理由" /></div>
                </>
              )}
              <div className="flex justify-end gap-2"><Button variant="outline" onClick={() => setEditor(null)}>キャンセル</Button><Button onClick={() => void saveEditor()} disabled={!editor.content.trim() || busy !== null}>{busy && <Loader2 className="size-3.5 animate-spin" />}保存</Button></div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(moveMemory)} onOpenChange={(open) => !open && setMoveMemory(null)}>
        <DialogContent size="md">
          <DialogHeader><DialogTitle>メモリのスコープを移動</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1"><Label>移動先</Label><AppSelect className="w-full" value={targetScope} onChange={(event) => { const scope = event.target.value as MemoryScope; setTargetScope(scope); if (scope === "project" && projectId) setTargetScopeId(projectId); }}><option value="user">ユーザー</option><option value="project">プロジェクト</option><option value="task">タスク</option><option value="session">セッション</option></AppSelect></div>
            {!(["global", "user"] as string[]).includes(targetScope) && <div className="space-y-1"><Label>対象ID</Label><Input value={targetScopeId} onChange={(event) => setTargetScopeId(event.target.value)} /></div>}
            <div className="flex justify-end gap-2"><Button variant="outline" onClick={() => setMoveMemory(null)}>キャンセル</Button><Button onClick={() => void move()} disabled={busy !== null || (!(["global", "user"] as string[]).includes(targetScope) && !targetScopeId.trim())}>移動</Button></div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(lineage)} onOpenChange={(open) => !open && setLineage(null)}>
        <DialogContent size="xl">
          <DialogHeader><DialogTitle>根拠と系譜</DialogTitle></DialogHeader>
          {lineage && <div className="max-h-[60vh] space-y-3 overflow-auto text-sm"><p className="whitespace-pre-wrap">{lineage.memory.content}</p><div className="rounded border p-2 text-xs"><p><strong>根拠:</strong> {evidenceText(lineage.memory)}</p><p><strong>置換前:</strong> {lineage.ancestors.length}件</p><p><strong>置換後:</strong> {lineage.descendants.length}件</p></div>{lineage.ancestors.map((item) => <div key={item.id} className="rounded border-l-2 p-2 text-xs text-muted-foreground">v{item.version} {item.status}: {item.content}</div>)}{lineage.descendants.map((item) => <div key={item.id} className="rounded border-l-2 p-2 text-xs">v{item.version} {item.status}: {item.content}</div>)}</div>}
        </DialogContent>
      </Dialog>
    </div>
  );
}
