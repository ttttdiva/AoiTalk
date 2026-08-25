"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import useSWR from "swr";
import {
  Eye,
  Pencil,
  ArrowDownToLine,
  ArrowUpToLine,
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  ClipboardPaste,
  Copy,
  CopyPlus,
  Flag,
  GitBranch,
  History,
  Loader2,
  MoreHorizontal,
  Plus,
  Save,
  Scissors,
  Sparkles,
  Split,
  TextSelect,
  Trash2,
  Unlink,
  WandSparkles,
} from "lucide-react";
import { toast } from "sonner";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { LongTextEditor, type LongTextEditorHandle } from "@/components/editor/long-text-editor";
import { storyApi, StoryApiError, type StoryRestoreToken } from "@/lib/story/api";
import { readStoryDrag, reorderStoryIds, resolveStoryDropMode, serializeStoryDrag, STORY_EPISODE_DND_MIME } from "@/lib/story/dnd";
import { countRouteCharacters, getBranchSiblingGroup, isEpisodeOnRoute, resolveStoryRoute, routeLinkChoices, type StoryRouteLink } from "@/lib/story/route";
import { normalizeRevisions, objectOf, type StoryEpisodeView } from "@/lib/story/view-model";
import { pointFromEvent, pointFromTrigger, StoryContextMenu, type StoryMenuEntry, type StoryMenuPoint } from "@/components/story/manuscript/story-context-menu";
import { useStoryGraph, useStoryJob, useStoryEpisode } from "@/components/story/hooks/use-story-data";
import { useStoryWorkContext } from "@/components/story/shell/story-workspace-shell";
import { saveStatusLabel } from "@/components/story/shell/save-status";
import { StoryPromptPreview } from "@/components/story/generate/story-prompt-preview";
import { StoryModelSelect } from "@/components/story/generate/story-model-select";
import { StoryRevisionsPanel } from "@/components/story/revisions/story-revisions-panel";
import { StoryIllustrationLayer } from "@/components/story/illustrations/story-illustration-layer";
import { StoryAssistDialog } from "@/components/story/assist/story-assist-dialog";
import { StoryAssistField } from "@/components/story/assist/story-assist-field";
import { toAssistSelection } from "@/components/story/assist/assist-selection";
import { seedInstructionFromSelection, useStoryAssist } from "@/components/story/assist/use-story-assist";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";

function statusLabel(status: string): string {
  return ({ unwritten: "未着手", draft: "下書き", revising: "推敲中", done: "完成", on_hold: "保留" } as Record<string, string>)[status] || status;
}

function statusVariant(status: string): "default" | "secondary" | "outline" {
  if (status === "completed" || status === "done") return "default";
  if (status === "draft" || status === "unwritten") return "secondary";
  return "outline";
}

function formatCount(value: number): string {
  return new Intl.NumberFormat("ja-JP").format(value);
}

function getCreatedEpisodeId(value: unknown): string | null {
  const record = objectOf(value);
  const firstResult = Array.isArray(record.results) ? objectOf(record.results[0]) : {};
  const created = objectOf(record.created ?? record.episode);
  for (const candidate of [record.episode_id, record.created_episode_id, record.id, created.id, firstResult.episode_id, objectOf(firstResult.episode).id]) {
    if (typeof candidate === "string" && candidate) return candidate;
  }
  return null;
}

function parseStoryModel(value: string): { provider: string; model: string } | undefined {
  const [provider, model] = value.split("::", 2);
  return provider && model ? { provider, model } : undefined;
}

/** リビジョン系レスポンスから rev_no を取り出す（§6.6 の「#N として保存しました」通知用）。 */
function revNoOf(value: unknown): number | null {
  const revNo = objectOf(value).rev_no;
  return typeof revNo === "number" && Number.isFinite(revNo) ? revNo : null;
}

/** CodeMirror の contenteditable に対する標準の編集コマンド（§4.8 エディタの右クリック操作）。 */
async function runEditorCommand(command: "cut" | "copy" | "paste" | "selectAll", focusEditor: () => void) {
  focusEditor();
  try {
    if (command !== "paste") {
      document.execCommand(command);
      return;
    }
    const text = await navigator.clipboard.readText();
    if (text) document.execCommand("insertText", false, text);
  } catch {
    toast.error("この操作はブラウザの制限で実行できませんでした。キーボードショートカットをお使いください。");
  }
}

function useEpisodeRevisions(episodeId: string | null) {
  const result = useSWR(episodeId ? `story-revisions:${episodeId}` : null, () => storyApi.listRevisions(episodeId as string));
  return { ...result, revisions: result.data ? normalizeRevisions(result.data) : [] };
}

function EpisodeStatus({ status }: { status: string }) {
  return <Badge variant={statusVariant(status)} className="shrink-0 text-[10px]">{statusLabel(status)}</Badge>;
}

type BodySaveResult = { ok: true; etag: string | null } | { ok: false; etag: null };

export function StoryManuscriptPage({ workId }: { workId: string }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { work, saveWork, saveState, registerSaveScope } = useStoryWorkContext();
  const { graph, mutate: mutateGraph, isLoading: graphLoading, error: graphError } = useStoryGraph(workId);
  const queryEpisodeId = searchParams.get("episode");
  const [selectedId, setSelectedId] = useState<string | null>(queryEpisodeId);
  const [routeIds, setRouteIds] = useState<string[]>([]);
  const [body, setBody] = useState("");
  const [plot, setPlot] = useState("");
  const [title, setTitle] = useState("");
  const [selection, setSelection] = useState<{ from: number; to: number; text: string } | null>(null);
  const [dirty, setDirty] = useState(false);
  const [bodyViewMode, setBodyViewMode] = useState<"edit" | "preview">("edit");
  const [branchDialog, setBranchDialog] = useState<{ mode: "new" | "duplicate"; episodeId: string } | null>(null);
  const [branchLabel, setBranchLabel] = useState("");
  const [branchTitle, setBranchTitle] = useState("");
  const [branchBusy, setBranchBusy] = useState(false);
  const [generateDialog, setGenerateDialog] = useState(false);
  const [generateBusy, setGenerateBusy] = useState(false);
  const [generateModel, setGenerateModel] = useState("");
  const [allowOverwrite, setAllowOverwrite] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [batchDialog, setBatchDialog] = useState(false);
  const [batchSelected, setBatchSelected] = useState<string[]>([]);
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchModel, setBatchModel] = useState("");
  const [batchJobId, setBatchJobId] = useState<string | null>(null);
  const [checkpointDialog, setCheckpointDialog] = useState(false);
  const [checkpointName, setCheckpointName] = useState("");
  const [historyVisible, setHistoryVisible] = useState(false);
  const [contextVisible, setContextVisible] = useState(false);
  const assist = useStoryAssist();
  const [deleteDialog, setDeleteDialog] = useState<{ episodeId: string; title: string } | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [episodeMenu, setEpisodeMenu] = useState<{ point: StoryMenuPoint; episodeId: string } | null>(null);
  const [editorMenu, setEditorMenu] = useState<StoryMenuPoint | null>(null);
  const editorRef = useRef<LongTextEditorHandle>(null);
  const bodyRef = useRef("");
  const loadedBodyRef = useRef("");
  const bodyEtagRef = useRef("");
  const activeEpisodeIdRef = useRef<string | null>(null);
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const saveInFlightRef = useRef(false);
  const mountedRef = useRef(true);
  const selectionRequestRef = useRef(0);
  const loadedMetaRef = useRef({ title: "", plot: "" });
  const [metaSaving, setMetaSaving] = useState(false);
  const [bodySaveFailed, setBodySaveFailed] = useState(false);
  const [metaSaveFailed, setMetaSaveFailed] = useState(false);
  const draggingIdRef = useRef<string | null>(null);
  const { episode: detailEpisode, mutate: mutateEpisode } = useStoryEpisode(selectedId);
  const { revisions, mutate: mutateRevisions } = useEpisodeRevisions(selectedId);
  const { job } = useStoryJob(jobId);
  const { job: batchJob } = useStoryJob(batchJobId);
  const detailEpisodeId = detailEpisode?.id ?? null;
  const detailEpisodeBody = detailEpisode?.body ?? "";
  const detailEpisodePlot = detailEpisode?.plot ?? "";
  const detailEpisodeTitle = detailEpisode?.title ?? "";
  const detailEpisodeEtag = detailEpisode?.bodyEtag ?? "";

  const generatedJobRef = useRef<string | null>(null);
  useEffect(() => {
    if (job?.status !== "done" || generatedJobRef.current === job.id) return;
    generatedJobRef.current = job.id;
    void mutateEpisode();
    void mutateGraph();
    void mutateRevisions();
    // §6.6-3: AI 操作で積まれた pre_ai リビジョンを必ず通知し、履歴へ跳べるようにする。
    const preRevNo = revNoOf(objectOf(job.result).pre_revision);
    if (preRevNo === null) toast.success("AIで本文を生成しました（履歴から戻せます）");
    else toast.success(`生成前の状態を #${preRevNo} として保存しました（履歴から戻せます）`, { action: { label: `#${preRevNo} を見る`, onClick: () => setHistoryVisible(true) } });
  }, [job?.id, job?.result, job?.status, mutateEpisode, mutateGraph, mutateRevisions]);

  const batchDoneRef = useRef<string | null>(null);
  useEffect(() => {
    if (batchJob?.status !== "done" || batchDoneRef.current === batchJob.id) return;
    batchDoneRef.current = batchJob.id;
    void mutateGraph();
    void mutateEpisode();
    toast.success("まとめて生成が完了しました（各章の生成前の状態は履歴から戻せます）", { action: { label: "履歴を見る", onClick: () => setHistoryVisible(true) } });
  }, [batchJob?.id, batchJob?.status, mutateEpisode, mutateGraph]);

  const route = useMemo(() => resolveStoryRoute({ startEpisodeId: graph.startEpisodeId ?? work.startEpisodeId, episodes: graph.episodes, links: graph.links, currentRoute: work.currentRoute }), [graph.episodes, graph.links, graph.startEpisodeId, work.currentRoute, work.startEpisodeId]);
  const routeEpisodeIds = useMemo(() => route.map((episode) => episode.id), [route]);
  const editingEpisode = useMemo(() => (selectedId ? graph.episodes.find((episode) => episode.id === selectedId) ?? null : null), [graph.episodes, selectedId]);
  const editingOffRoute = useMemo(() => Boolean(editingEpisode && !isEpisodeOnRoute(editingEpisode.id, routeEpisodeIds)), [editingEpisode, routeEpisodeIds]);
  const selectedSiblingGroup = useMemo(() => (selectedId ? getBranchSiblingGroup(selectedId, graph.links, graph.episodes) : null), [graph.episodes, graph.links, selectedId]);
  const visibleEpisodes = useMemo(() => {
    const byId = new Map(graph.episodes.map((episode) => [episode.id, episode]));
    const ids = routeIds.length ? routeIds : routeEpisodeIds;
    return ids.map((id) => byId.get(id)).filter((episode): episode is StoryEpisodeView => Boolean(episode));
  }, [graph.episodes, routeEpisodeIds, routeIds]);
  const outgoingByEpisode = useMemo(() => {
    const map = new Map<string, StoryRouteLink[]>();
    for (const link of graph.links) map.set(link.from, [...(map.get(link.from) ?? []), link]);
    return map;
  }, [graph.links]);

  /**
   * §7.3 線形区間の判定。出次数 1 かつ次ノードの入次数 1 が連続する範囲を 1 区間とし、
   * 同じ区間内でだけリストの D&D 並べ替えを許す。区間境界（分岐点・合流点）をまたぐ移動は拒否する。
   */
  const linearSegmentByEpisode = useMemo(() => {
    const outDegree = new Map<string, number>();
    const inDegree = new Map<string, number>();
    const connected = new Set<string>();
    for (const link of graph.links) {
      outDegree.set(link.from, (outDegree.get(link.from) ?? 0) + 1);
      inDegree.set(link.to, (inDegree.get(link.to) ?? 0) + 1);
      connected.add(`${link.from}>${link.to}`);
    }
    const segments = new Map<string, number>();
    let segment = 0;
    visibleEpisodes.forEach((episode, index) => {
      const previous = visibleEpisodes[index - 1];
      if (previous && !(connected.has(`${previous.id}>${episode.id}`) && outDegree.get(previous.id) === 1 && inDegree.get(episode.id) === 1)) segment += 1;
      segments.set(episode.id, segment);
    });
    return segments;
  }, [graph.links, visibleEpisodes]);

  const isSameLinearSegment = useCallback((leftId: string, rightId: string) => {
    const left = linearSegmentByEpisode.get(leftId);
    return left !== undefined && left === linearSegmentByEpisode.get(rightId);
  }, [linearSegmentByEpisode]);

  const routeSignature = routeEpisodeIds.join("|");
  useEffect(() => {
    setRouteIds(routeSignature ? routeSignature.split("|") : []);
  }, [routeSignature]);

  useEffect(() => {
    if (!queryEpisodeId && routeEpisodeIds[0]) {
      setSelectedId(routeEpisodeIds[0]);
      router.replace(`/scenarios/${encodeURIComponent(workId)}/manuscript?episode=${encodeURIComponent(routeEpisodeIds[0])}`);
    }
  }, [queryEpisodeId, routeEpisodeIds, router, workId]);

  useEffect(() => {
    if (!detailEpisodeId) return;
    const episodeChanged = activeEpisodeIdRef.current !== detailEpisodeId;
    const bodyDirty = bodyRef.current !== loadedBodyRef.current;
    const metaDirty = title !== loadedMetaRef.current.title || plot !== loadedMetaRef.current.plot;

    if (episodeChanged) {
      activeEpisodeIdRef.current = detailEpisodeId;
      bodyRef.current = detailEpisodeBody;
      setBody(detailEpisodeBody);
      setPlot(detailEpisodePlot);
      setTitle(detailEpisodeTitle);
      loadedBodyRef.current = detailEpisodeBody;
      bodyEtagRef.current = detailEpisodeEtag;
      loadedMetaRef.current = { title: detailEpisodeTitle, plot: detailEpisodePlot };
      setDirty(false);
      setSelection(null);
      setBodyViewMode("edit");
      return;
    }

    if (!saveInFlightRef.current && !bodyDirty) {
      bodyRef.current = detailEpisodeBody;
      setBody(detailEpisodeBody);
      loadedBodyRef.current = detailEpisodeBody;
      bodyEtagRef.current = detailEpisodeEtag;
    }

    if (!metaDirty) {
      setPlot(detailEpisodePlot);
      setTitle(detailEpisodeTitle);
      loadedMetaRef.current = { title: detailEpisodeTitle, plot: detailEpisodePlot };
    }

    const stillBodyDirty = bodyRef.current !== loadedBodyRef.current;
    const stillMetaDirty = title !== loadedMetaRef.current.title || plot !== loadedMetaRef.current.plot;
    setDirty(stillBodyDirty || stillMetaDirty);
  }, [detailEpisodeBody, detailEpisodeEtag, detailEpisodeId, detailEpisodePlot, detailEpisodeTitle, plot, title]);

  const saveBody = useCallback(async (explicit = false): Promise<BodySaveResult> => {
    let result: BodySaveResult = { ok: false, etag: null };
    const queuedSave = saveQueueRef.current.then(async () => {
      const episodeId = activeEpisodeIdRef.current;
      if (!episodeId) return;
      const nextBody = bodyRef.current;
      if (nextBody === loadedBodyRef.current) {
        if (explicit) toast.message("変更はありません");
        result = { ok: true, etag: bodyEtagRef.current || null };
        return;
      }

      const expectedEtag = bodyEtagRef.current;
      saveInFlightRef.current = true;
      try {
        const response = await storyApi.updateBody(episodeId, {
          body: nextBody,
          expected_etag: expectedEtag,
          commit: explicit,
          message: null,
        });
        const updated = objectOf(response);
        const nextEtag = typeof updated.body_etag === "string" ? updated.body_etag : expectedEtag;
        loadedBodyRef.current = nextBody;
        bodyEtagRef.current = nextEtag;
        result = { ok: true, etag: nextEtag || null };
        if (mountedRef.current) {
          setBodySaveFailed(false);
          setDirty(bodyRef.current !== nextBody);
          void Promise.all([mutateEpisode(), mutateGraph()]).catch((error) => {
            console.error("保存後の章情報を再取得できませんでした", error);
          });
          if (nextEtag && nextEtag !== expectedEtag) {
            toast.success(explicit ? "本文を保存しました" : "本文を自動保存しました");
          }
        }
      } catch (error) {
        if (mountedRef.current) {
          setBodySaveFailed(true);
          setDirty(true);
          if (error instanceof StoryApiError && error.status === 409) {
            toast.error("他の場所でこの章が更新されました。履歴から差分を確認してください。");
          } else {
            toast.error(error instanceof Error ? error.message : "本文を保存できませんでした");
          }
        }
      } finally {
        saveInFlightRef.current = false;
      }
    });
    saveQueueRef.current = queuedSave.catch(() => undefined);
    await queuedSave;
    return result;
  }, [mutateEpisode, mutateGraph]);

  const isMetaDirty = useCallback(() => {
    return title !== loadedMetaRef.current.title || plot !== loadedMetaRef.current.plot;
  }, [plot, title]);

  const flushEpisodeMeta = useCallback(async (): Promise<boolean> => {
    if (!detailEpisode || !isMetaDirty()) return true;
    setMetaSaving(true);
    try {
      await storyApi.updateEpisode(detailEpisode.id, { title, plot });
      loadedMetaRef.current = { title, plot };
      await mutateEpisode();
      await mutateGraph();
      setMetaSaveFailed(false);
      return true;
    } catch (error) {
      setMetaSaveFailed(true);
      toast.error(error instanceof Error ? error.message : "章情報を保存できませんでした");
      return false;
    } finally {
      setMetaSaving(false);
    }
  }, [detailEpisode, isMetaDirty, mutateEpisode, mutateGraph, plot, title]);

  const selectEpisode = useCallback(async (episodeId: string, updateRoute = true): Promise<boolean> => {
    if (episodeId === selectedId) return true;
    const request = ++selectionRequestRef.current;
    const activeEpisodeId = activeEpisodeIdRef.current;
    if (activeEpisodeId && activeEpisodeId !== episodeId) {
      const metaOk = await flushEpisodeMeta();
      if (request !== selectionRequestRef.current || !metaOk) return false;
      const saveResult = await saveBody(false);
      if (request !== selectionRequestRef.current || !saveResult.ok) return false;
    }
    if (request !== selectionRequestRef.current) return false;
    if (updateRoute) {
      router.push(`/scenarios/${encodeURIComponent(workId)}/manuscript?episode=${encodeURIComponent(episodeId)}`);
    } else {
      setSelectedId(episodeId);
    }
    return true;
  }, [flushEpisodeMeta, router, saveBody, selectedId, workId]);

  useEffect(() => {
    if (!queryEpisodeId || queryEpisodeId === selectedId) return;
    if (!graph.episodes.some((episode) => episode.id === queryEpisodeId)) return;
    const previousId = selectedId;
    void selectEpisode(queryEpisodeId, false).then((selected) => {
      if (!selected && previousId) {
        router.replace(`/scenarios/${encodeURIComponent(workId)}/manuscript?episode=${encodeURIComponent(previousId)}`);
      }
    });
  }, [graph.episodes, queryEpisodeId, router, selectEpisode, selectedId, workId]);

  useEffect(() => {
    if (!detailEpisode || body === loadedBodyRef.current) return;
    setDirty(true);
    const timeout = window.setTimeout(() => void saveBody(false), 2000);
    return () => window.clearTimeout(timeout);
  }, [body, detailEpisode, saveBody]);

  useEffect(() => {
    return registerSaveScope({
      id: "manuscript",
      isDirty: () => dirty || isMetaDirty(),
      isSaving: () => saveInFlightRef.current || metaSaving,
      isFailed: () => bodySaveFailed || metaSaveFailed,
      flush: async () => {
        const metaOk = await flushEpisodeMeta();
        const bodyResult = await saveBody(false);
        return metaOk && bodyResult.ok;
      },
    });
  }, [bodySaveFailed, dirty, flushEpisodeMeta, isMetaDirty, metaSaveFailed, metaSaving, registerSaveScope, saveBody]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (bodyRef.current !== loadedBodyRef.current) void saveBody(false);
    };
  }, [saveBody]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        void saveBody(true);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [saveBody]);

  const updateEpisodeMeta = useCallback(
    async (patch: Record<string, unknown>) => {
      if (!detailEpisode) return;
      await storyApi.updateEpisode(detailEpisode.id, patch);
      await mutateEpisode();
      await mutateGraph();
    },
    [detailEpisode, mutateEpisode, mutateGraph],
  );

  const openBranchDialog = (episodeId: string, mode: "new" | "duplicate") => {
    const episode = graph.episodes.find((item) => item.id === episodeId);
    setBranchDialog({ mode, episodeId });
    setBranchLabel(mode === "duplicate" ? outgoingByEpisode.get(episodeId)?.[0]?.choiceLabel || "別パターン" : "");
    setBranchTitle(mode === "duplicate" ? `${episode?.title || "章"}（別パターン）` : "");
  };

  const createBranch = async () => {
    if (!branchDialog) return;
    setBranchBusy(true);
    try {
      let result: unknown;
      if (branchDialog.mode === "duplicate") {
        result = await storyApi.updateStructure(workId, { ops: [{ op: "duplicate_as_branch", episode_id: branchDialog.episodeId, choice_label: branchLabel.trim(), new_title: branchTitle.trim() || undefined }] });
      } else {
        result = await storyApi.createEpisode(workId, { title: branchTitle.trim() || "新しい分岐", plot: "", body: "", status: "unwritten", sort_hint: 0, after_episode_id: branchDialog.episodeId, choice_label: branchLabel.trim() });
      }
      const newId = getCreatedEpisodeId(result);
      await mutateGraph();
      setBranchDialog(null);
      if (newId) selectEpisode(newId);
      toast.success(branchDialog.mode === "duplicate" ? "章を複製して分岐を作成しました" : "白紙の続きの分岐を作成しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "分岐を作成できませんでした");
    } finally {
      setBranchBusy(false);
    }
  };

  const createEpisode = async () => {
    setBranchBusy(true);
    try {
      const afterId = visibleEpisodes.at(-1)?.id;
      const result = await storyApi.createEpisode(workId, { title: `第${graph.episodes.length + 1}章`, plot: "", body: "", status: "unwritten", sort_hint: 0, ...(afterId ? { after_episode_id: afterId } : {}) });
      const newId = getCreatedEpisodeId(result);
      await mutateGraph();
      if (newId) selectEpisode(newId);
      toast.success("章を追加しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "章を追加できませんでした");
    } finally {
      setBranchBusy(false);
    }
  };

  const insertEpisode = async (episodeId: string, direction: "above" | "below") => {
    const index = visibleEpisodes.findIndex((episode) => episode.id === episodeId);
    if (index < 0) return;
    const neighbor = direction === "above" ? visibleEpisodes[index - 1] : visibleEpisodes[index + 1];
    setBranchBusy(true);
    try {
      const created = await storyApi.createEpisode(workId, {
        title: direction === "above" ? "前に挿入した章" : "後に挿入した章",
        plot: "",
        body: "",
        status: "unwritten",
        sort_hint: 0,
      });
      const newId = getCreatedEpisodeId(created);
      if (!newId) throw new Error("挿入する章のIDが返りませんでした");
      if (!neighbor) {
        await storyApi.updateStructure(workId, {
          ops: direction === "below"
            ? [{ op: "add_link", from: episodeId, to: newId, is_primary: true }]
            : [{ op: "add_link", from: newId, to: episodeId, is_primary: true }, { op: "set_start", episode_id: newId }],
        });
      } else {
        const link = direction === "above"
          ? graph.links.find((item) => item.from === neighbor.id && item.to === episodeId)
          : graph.links.find((item) => item.from === episodeId && item.to === neighbor.id);
        if (!link) throw new Error("線形区間の接続を特定できませんでした。分岐マップで操作してください。");
        await storyApi.updateStructure(workId, { ops: [{ op: "insert_between", link_id: link.id, episode_id: newId }] });
      }
      await mutateGraph();
      selectEpisode(newId);
      toast.success("章を挿入しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "章を挿入できませんでした");
    } finally {
      setBranchBusy(false);
    }
  };

  const unplaceEpisode = async (episodeId: string) => {
    const ops = graph.links
      .filter((link) => link.from === episodeId || link.to === episodeId)
      .map((link) => ({ op: "remove_link", id: link.id }));
    if (!ops.length) return;
    try {
      await storyApi.updateStructure(workId, { ops });
      await mutateGraph();
      toast.success("章を未配置へ移しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "章を未配置へ移せませんでした");
    }
  };

  const requestDelete = (episodeId: string) => {
    const episode = graph.episodes.find((item) => item.id === episodeId);
    if (episode) setDeleteDialog({ episodeId, title: episode.title });
  };

  const undoEpisodeDelete = useCallback(
    async (episodeId: string, restoreToken: StoryRestoreToken | null | undefined) => {
      try {
        await storyApi.restoreArchivedEpisode(episodeId, restoreToken ?? undefined);
        await mutateGraph();
        await selectEpisode(episodeId);
        toast.success("章の削除を元に戻しました");
      } catch (firstError) {
        if (!restoreToken) {
          toast.error(firstError instanceof Error ? firstError.message : "章を復元できませんでした");
          return;
        }
        try {
          await storyApi.restoreArchivedEpisode(episodeId);
          await mutateGraph();
          await selectEpisode(episodeId);
          toast.error("リンクを復元できなかったため、章は未配置として戻しました");
        } catch (secondError) {
          toast.error(secondError instanceof Error ? secondError.message : "章を復元できませんでした");
        }
      }
    },
    [mutateGraph, selectEpisode],
  );

  const confirmDelete = async () => {
    if (!deleteDialog) return;
    const episodeId = deleteDialog.episodeId;
    setDeleteBusy(true);
    try {
      const result = await storyApi.deleteEpisode(episodeId);
      const restoreToken = result.restore_token;
      await mutateGraph();
      setDeleteDialog(null);
      if (selectedId === episodeId) {
        const fallback = routeEpisodeIds.find((id) => id !== episodeId);
        if (fallback) selectEpisode(fallback);
        else router.push(`/scenarios/${encodeURIComponent(workId)}/manuscript`);
      }
      toast.success("章を削除しました", {
        action: { label: "元に戻す", onClick: () => void undoEpisodeDelete(episodeId, restoreToken) },
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "章を削除できませんでした");
    } finally {
      setDeleteBusy(false);
    }
  };

  const handleDrop = async (event: React.DragEvent<HTMLDivElement>, targetId: string) => {
    event.preventDefault();
    draggingIdRef.current = null;
    const movingId = readStoryDrag(event.dataTransfer);
    if (!movingId) return;
    const moving = visibleEpisodes.find((episode) => episode.id === movingId);
    const target = visibleEpisodes.find((episode) => episode.id === targetId);
    if (!moving || !target) return;
    const mode = resolveStoryDropMode(event.clientY, event.currentTarget.getBoundingClientRect(), isSameLinearSegment(movingId, targetId));
    if (mode === "blocked") {
      toast.message("分岐を含む範囲の移動は分岐マップで行ってください");
      return;
    }
    const next = reorderStoryIds(visibleEpisodes.map((episode) => episode.id), movingId, targetId, mode);
    if (!next) return;
    const previous = routeIds;
    setRouteIds(next);
    try {
      await storyApi.updateStructure(workId, { ops: [{ op: "reorder_linear", episode_ids: next }] });
      await mutateGraph();
    } catch (error) {
      setRouteIds(previous);
      toast.error(error instanceof Error ? error.message : "章の並べ替えに失敗しました");
    }
  };

  const splitAtSelection = async () => {
    if (!detailEpisode || !selection || selection.from <= 0 || selection.from >= body.length) return;
    const saveResult = await saveBody(true);
    if (!saveResult.ok || !saveResult.etag) return;
    try {
      const result = await storyApi.splitEpisode(detailEpisode.id, { offset: selection.from, new_title: `${title}（後半）`, expected_etag: saveResult.etag });
      const newId = getCreatedEpisodeId(result);
      await mutateGraph();
      if (newId) selectEpisode(newId);
      toast.success("章を分割しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "章を分割できませんでした");
    }
  };

  /** §4.8: 行メニューからの分割は、対象章を開いてエディタへフォーカスを移すところまでを担う。 */
  const focusSplitTarget = (episodeId: string) => {
    if (selectedId !== episodeId) selectEpisode(episodeId);
    window.setTimeout(() => editorRef.current?.focus(), 0);
    toast.message("分割したい位置にカーソルを置いてから、もう一度「カーソル位置で章を分割」を実行してください");
  };

  const generateEpisode = async () => {
    if (!detailEpisode) return;
    if (detailEpisode.charCount > 0 && !allowOverwrite) {
      toast.error("既存本文を上書きする確認が必要です");
      return;
    }
    setGenerateBusy(true);
    try {
      const model = parseStoryModel(generateModel);
      const result = await storyApi.generate(detailEpisode.id, model ? { model } : {});
      const id = objectOf(objectOf(result).job).id ?? objectOf(result).job_id ?? objectOf(result).id;
      setJobId(typeof id === "string" ? id : null);
      setGenerateDialog(false);
      toast.success("本文生成ジョブを開始しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "本文生成を開始できませんでした");
    } finally {
      setGenerateBusy(false);
    }
  };

  const openBatchDialog = () => {
    setBatchSelected(routeEpisodeIds);
    setBatchJobId(null);
    setBatchModel("");
    setBatchDialog(true);
  };

  const startBatch = async () => {
    if (!batchSelected.length) return;
    setBatchBusy(true);
    try {
      const model = parseStoryModel(batchModel);
      const queued = objectOf(await storyApi.batchGenerate(workId, { episode_ids: batchSelected, instruction: "プロットと作品設定に従って本文を生成してください。", ...(model ? { model } : {}) }));
      const id = typeof queued.id === "string" ? queued.id : null;
      if (!id) throw new Error("まとめて生成ジョブIDが返りませんでした");
      setBatchJobId(id);
      toast.success("まとめて生成を開始しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "まとめて生成を開始できませんでした");
    } finally {
      setBatchBusy(false);
    }
  };

  const resumeBatch = async () => {
    if (!batchJobId) return;
    try {
      await storyApi.resumeJob(batchJobId);
      toast.success("失敗した章から再開しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "ジョブを再開できませんでした");
    }
  };

  const cancelJob = async (id: string | null) => {
    if (!id) return;
    try {
      await storyApi.cancelJob(id);
      toast.message("ジョブのキャンセルを受け付けました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "ジョブをキャンセルできませんでした");
    }
  };

  const checkpoint = async () => {
    if (!detailEpisode || !checkpointName.trim()) return;
    try {
      await storyApi.checkpoint(detailEpisode.id, { message: checkpointName.trim() });
      setCheckpointDialog(false);
      setCheckpointName("");
      await mutateRevisions();
      toast.success("チェックポイントを作成しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "チェックポイントを作成できませんでした");
    }
  };

  const saveMeta = async () => {
    const ok = await flushEpisodeMeta();
    if (ok) toast.success("章情報を保存しました");
  };

  const openBodyAssist = useCallback(() => {
    if (!detailEpisode) return;
    const selectionSnapshot = toAssistSelection(selection);
    assist.openAssist(
      {
        fieldKind: "episode_body",
        fieldLabel: "本文",
        workId,
        episodeId: detailEpisode.id,
        getCurrentText: () => bodyRef.current,
        getSelection: () => selectionSnapshot,
        getBodyEtag: () => bodyEtagRef.current,
      },
      seedInstructionFromSelection(selectionSnapshot),
    );
  }, [assist, detailEpisode, selection, workId]);

  const selectRouteBranch = async (fromId: string, toId: string) => {
    const choices = { ...work.currentRoute, [fromId]: toId };
    const nextRoute = resolveStoryRoute({ startEpisodeId: graph.startEpisodeId ?? work.startEpisodeId, episodes: graph.episodes, links: graph.links, currentRoute: choices });
    setRouteIds(nextRoute.map((episode) => episode.id));
    try {
      await saveWork({ ui_state: { current_route: nextRoute.map((episode) => episode.id) } });
      if (selectedId && !nextRoute.some((episode) => episode.id === selectedId)) {
        const fallback = nextRoute.at(-1)?.id ?? nextRoute[0]?.id;
        if (fallback) selectEpisode(fallback);
      }
      toast.success("表示ルートを切り替えました");
    } catch {
      setRouteIds(routeEpisodeIds);
    }
  };

  const adoptEditingEpisodeToRoute = async () => {
    if (!selectedId || !selectedSiblingGroup) return;
    await selectRouteBranch(selectedSiblingGroup.parentId, selectedId);
  };

  const siblingChoiceLabel = useCallback((choice: StoryRouteLink) => {
    return choice.choiceLabel?.trim() || graph.episodes.find((episode) => episode.id === choice.to)?.title || "分岐";
  }, [graph.episodes]);

  // Story のインスペクタは Shared Shell の Context Rail へ登録する。
  // 生成ジョブ・履歴・分岐・保存状態の正本は引き続きこのページ／
  // StoryWorkContext にあり、レールは表示と既存操作への委譲だけを担う。
  const storyInspectorRail = useMemo(
    () => (
      <aside
        className="flex min-h-0 flex-1 flex-col bg-surface-charcoal text-on-surface"
        data-testid="story-inspector-context-rail"
        data-shell-workspace="story"
      >
          <div className="flex h-12 shrink-0 items-center gap-1 border-b border-border-subtle bg-surface-container p-2">
          <span className="mr-auto px-1 text-sm font-semibold text-on-surface">Episode Properties</span>
          <Button
            variant={!historyVisible ? "secondary" : "ghost"}
            size="sm"
            onClick={() => setHistoryVisible(false)}
          >
            情報
          </Button>
          <Button
            variant={historyVisible ? "secondary" : "ghost"}
            size="sm"
            onClick={() => setHistoryVisible(true)}
          >
            <History className="size-3.5" />
            履歴
          </Button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="mb-3 rounded-sm border border-border-subtle bg-surface-container-lowest p-3 text-xs">
            <div className="flex items-center justify-between gap-2">
              <span className="text-muted-foreground">保存状態</span>
              <span className="font-medium" data-testid="story-manuscript-save-state">
                {saveStatusLabel(saveState)}
              </span>
            </div>
          </div>
          {(job || batchJob) && (
            <section className="mb-3 rounded-sm border border-border-subtle bg-surface-container-lowest p-3" data-testid="story-generation-jobs">
              <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                生成ジョブ
              </div>
              <div className="mt-2 space-y-1 text-xs">
                {job && <div className="flex items-center justify-between gap-2"><span>本文生成</span><span className="text-muted-foreground">{job.status}</span></div>}
                {batchJob && <div className="flex items-center justify-between gap-2"><span>まとめて生成</span><span className="text-muted-foreground">{batchJob.status}</span></div>}
              </div>
            </section>
          )}
          {historyVisible ? (
            <StoryRevisionsPanel
              episodeId={selectedId}
              revisions={revisions}
              onCheckpoint={() => setCheckpointDialog(true)}
              onRestore={async (revNo) => {
                if (!selectedId) return;
                await storyApi.restore(selectedId, { rev_no: revNo });
                await mutateEpisode();
                await mutateGraph();
                await mutateRevisions();
                toast.success(`履歴 #${revNo} を復元しました`);
              }}
            />
          ) : detailEpisode ? (
            <div className="space-y-4">
              <div>
                <Label>状態</Label>
                <AppSelect
                  className="mt-1 w-full bg-surface-container-lowest"
                  aria-label="章の状態"
                  value={detailEpisode.status}
                  onValueChange={(value) => void updateEpisodeMeta({ status: value })}
                >
                  <option value="unwritten">未着手</option>
                  <option value="draft">下書き</option>
                  <option value="revising">推敲中</option>
                  <option value="done">完成</option>
                  <option value="on_hold">保留</option>
                </AppSelect>
              </div>
              <div>
                <Label>目標文字数</Label>
                <Input
                  className="mt-1 h-8"
                  type="number"
                  value={detailEpisode.targetChars}
                  onChange={(event) =>
                    void updateEpisodeMeta({ target_chars: Number(event.target.value) || 0 })
                  }
                />
              </div>
              <div className="rounded-sm border border-border-subtle bg-surface-container-lowest p-3">
                <Label>要約</Label>
                <textarea
                  className="mt-1 min-h-24 w-full resize-y border-0 bg-transparent p-0 text-sm outline-none"
                  defaultValue={detailEpisode.summary}
                  onBlur={(event) => void updateEpisodeMeta({ summary: event.target.value })}
                  placeholder="自動生成・編集可"
                />
              </div>
              <div className="rounded-sm border border-border-subtle bg-surface-container-lowest p-3">
                <div className="flex items-center gap-1 text-xs font-medium">
                  <CircleAlert className="size-3.5 text-primary" />
                  前提メモ
                </div>
                <p className="mt-1 text-[11px] text-muted-foreground">
                  合流エピソードではAI文脈に必ず注入されます。
                </p>
                <textarea
                  className="mt-2 min-h-24 w-full resize-y border-0 bg-transparent p-0 text-sm outline-none"
                  defaultValue={detailEpisode.premiseNote}
                  onBlur={(event) => void updateEpisodeMeta({ premise_note: event.target.value })}
                  placeholder="確定事項・禁止事項"
                />
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">章を選択してください。</p>
          )}
        </div>
      </aside>
    ),
    [
      detailEpisode,
      historyVisible,
      job,
      batchJob,
      mutateEpisode,
      mutateGraph,
      mutateRevisions,
      revisions,
      selectedId,
      saveState,
      updateEpisodeMeta,
    ],
  );
  useWorkspaceShellRegistration({
    contextRail: storyInspectorRail,
    priority: 60,
    id: `story-manuscript-inspector-${workId}`,
  });

  const canSplitHere = Boolean(detailEpisode && selection && selection.from > 0 && selection.from < body.length);
  // graph / detail いずれの DTO でも char_count は返るため、body の有無ではなく文字数で既存本文を判定する。
  const hasExistingBody = (detailEpisode?.charCount ?? 0) > 0;

  const renderEpisodeListRow = (episode: StoryEpisodeView, index: number | null, options?: { offRoute?: boolean }) => {
    const branch = (outgoingByEpisode.get(episode.id)?.length ?? 0) > 1;
    const isSelected = selectedId === episode.id;
    return (
      <div
        key={episode.id}
        draggable={!options?.offRoute}
        onDragStart={options?.offRoute ? undefined : (event) => { draggingIdRef.current = episode.id; event.dataTransfer.effectAllowed = "move"; const payload = serializeStoryDrag(episode.id); event.dataTransfer.setData(STORY_EPISODE_DND_MIME, payload); event.dataTransfer.setData("text/plain", payload); }}
        onDragEnd={options?.offRoute ? undefined : () => { draggingIdRef.current = null; }}
        onDragOver={options?.offRoute ? undefined : (event) => { event.preventDefault(); const movingId = draggingIdRef.current; event.dataTransfer.dropEffect = !movingId || isSameLinearSegment(movingId, episode.id) ? "move" : "none"; }}
        onDrop={options?.offRoute ? undefined : (event) => void handleDrop(event, episode.id)}
        onContextMenu={(event) => { event.preventDefault(); setEditorMenu(null); setEpisodeMenu({ point: pointFromEvent(event), episodeId: episode.id }); }}
        className={`group mb-1 flex items-center gap-1 rounded-sm border px-2 py-2 text-left transition-colors ${isSelected ? "border-primary bg-surface-container-highest" : "border-transparent hover:border-border-subtle hover:bg-surface-container-high"}`}
        data-testid={`story-episode-row-${episode.id}`}
        data-off-route={options?.offRoute ? "true" : undefined}
      >
        <button type="button" onClick={() => selectEpisode(episode.id)} className="min-w-0 flex-1 text-left">
          <div className="flex items-center gap-1.5">
            {index !== null ? <span className="w-5 shrink-0 text-right text-[10px] text-muted-foreground">{index + 1}</span> : <span className="w-5 shrink-0" />}
            <span className="truncate text-sm font-medium">{episode.title}</span>
            {branch && <GitBranch className="size-3 shrink-0 text-muted-foreground" />}
            {isSelected && <Badge variant="secondary" className="shrink-0 text-[9px]">編集中</Badge>}
          </div>
          <div className="mt-1 flex items-center gap-1.5 pl-6 text-[10px] text-muted-foreground">
            <span>{formatCount(episode.charCount)} / {formatCount(episode.targetChars)}字</span>
            <EpisodeStatus status={episode.status} />
          </div>
        </button>
        <Button variant="ghost" size="icon-xs" aria-label="続きの分岐を追加" className="opacity-0 group-hover:opacity-100 focus-visible:opacity-100" onClick={() => openBranchDialog(episode.id, "new")}><GitBranch className="size-3.5" /></Button>
        <Button variant="ghost" size="icon-xs" aria-label="章のメニュー" className="opacity-0 group-hover:opacity-100 focus-visible:opacity-100" data-testid={`story-episode-menu-${episode.id}`} onClick={(event) => { setEditorMenu(null); setEpisodeMenu({ point: pointFromTrigger(event.currentTarget), episodeId: episode.id }); }}><MoreHorizontal className="size-3.5" /></Button>
      </div>
    );
  };

  const renderSiblingSwitcher = (testId: string, options?: { showAdopt?: boolean; siblingTestIdPrefix?: string }) => {
    if (!selectedSiblingGroup || selectedSiblingGroup.choices.length <= 1) return null;
    const adoptedId = work.currentRoute[selectedSiblingGroup.parentId] ?? selectedSiblingGroup.choices[0]?.to;
    const siblingPrefix = options?.siblingTestIdPrefix ?? "story-sibling";
    return (
      <div className="flex flex-wrap items-center gap-1.5" data-testid={testId}>
        <span className="text-[10px] font-medium uppercase tracking-[0.12em] text-muted-foreground">別パターン</span>
        {selectedSiblingGroup.choices.map((choice) => {
          const label = siblingChoiceLabel(choice);
          const active = selectedId === choice.to;
          const adopted = adoptedId === choice.to;
          return (
            <Button
              key={choice.to}
              type="button"
              variant={active ? "secondary" : "outline"}
              size="xs"
              className="h-6 text-[11px]"
              data-testid={`${siblingPrefix}-${choice.to}`}
              onClick={() => void selectEpisode(choice.to)}
            >
              {label}
              {adopted ? "（採用中）" : ""}
            </Button>
          );
        })}
        {options?.showAdopt && editingOffRoute ? (
          <Button type="button" variant="default" size="xs" className="ml-auto h-6 text-[11px]" data-testid="story-adopt-route" onClick={() => void adoptEditingEpisodeToRoute()}>
            このパターンを現在のルートにする
          </Button>
        ) : null}
      </div>
    );
  };

  /** §4.8 章リスト行のコンテキストメニュー。⋯ ボタンと行の右クリックが同じ内容を開く。 */
  const episodeMenuEntries = (episodeId: string): StoryMenuEntry[] => [
    { key: "open", label: "開く", icon: ChevronRight, mnemonic: "O", onSelect: () => selectEpisode(episodeId) },
    { key: "sep-branch", separator: true },
    { key: "branch", label: "続きの分岐を追加", description: "この章の続きとして別パターンを作る", icon: GitBranch, mnemonic: "B", tone: "primary", onSelect: () => openBranchDialog(episodeId, "new") },
    { key: "duplicate", label: "複製して分岐にする", description: "この章のコピーを同じ前提からの別パターンとして隣に並べる", icon: CopyPlus, mnemonic: "C", tone: "primary", onSelect: () => openBranchDialog(episodeId, "duplicate") },
    { key: "sep-insert", separator: true },
    { key: "insert-above", label: "上に章を挿入", icon: ArrowUpToLine, mnemonic: "U", onSelect: () => void insertEpisode(episodeId, "above") },
    { key: "insert-below", label: "下に章を挿入", icon: ArrowDownToLine, mnemonic: "N", onSelect: () => void insertEpisode(episodeId, "below") },
    { key: "split", label: "カーソル位置で章を分割", description: "カーソル以降を新しい章として切り出す", icon: Split, mnemonic: "S", onSelect: () => (selectedId === episodeId && canSplitHere ? void splitAtSelection() : focusSplitTarget(episodeId)) },
    { key: "sep-history", separator: true },
    { key: "checkpoint", label: "チェックポイントを作成", icon: Flag, mnemonic: "K", onSelect: () => { selectEpisode(episodeId); setCheckpointDialog(true); } },
    { key: "history", label: "履歴を見る", icon: History, mnemonic: "H", onSelect: () => { selectEpisode(episodeId); setHistoryVisible(true); } },
    { key: "unplace", label: "未配置へ外す", icon: Unlink, mnemonic: "P", onSelect: () => void unplaceEpisode(episodeId) },
    { key: "sep-delete", separator: true },
    { key: "delete", label: "削除", icon: Trash2, mnemonic: "D", tone: "destructive", onSelect: () => requestDelete(episodeId) },
  ];

  /** §4.8 エディタの右クリック操作。標準の編集項目に「カーソル位置で章を分割」を加える。 */
  const editorMenuEntries: StoryMenuEntry[] = [
    { key: "split", label: "カーソル位置で章を分割", description: "カーソル以降を新しい章として切り出す", icon: Split, mnemonic: "S", tone: "primary", disabled: !canSplitHere, onSelect: () => void splitAtSelection() },
    { key: "sep-ai", separator: true },
    { key: "revise", label: "AIに修正を依頼", icon: WandSparkles, mnemonic: "A", tone: "primary", disabled: !detailEpisode, onSelect: () => openBodyAssist() },
    { key: "checkpoint", label: "チェックポイントを作成", icon: Flag, mnemonic: "K", disabled: !detailEpisode, onSelect: () => setCheckpointDialog(true) },
    { key: "sep-edit", separator: true },
    { key: "cut", label: "切り取り", icon: Scissors, mnemonic: "X", disabled: !selection?.text, onSelect: () => void runEditorCommand("cut", () => editorRef.current?.focus()) },
    { key: "copy", label: "コピー", icon: Copy, mnemonic: "C", disabled: !selection?.text, onSelect: () => void runEditorCommand("copy", () => editorRef.current?.focus()) },
    { key: "paste", label: "貼り付け", icon: ClipboardPaste, mnemonic: "V", onSelect: () => void runEditorCommand("paste", () => editorRef.current?.focus()) },
    { key: "select-all", label: "すべて選択", icon: TextSelect, mnemonic: "L", onSelect: () => void runEditorCommand("selectAll", () => editorRef.current?.focus()) },
  ];

  if (graphLoading) return <div className="flex h-full items-center justify-center text-sm text-muted-foreground"><Loader2 className="mr-2 size-4 animate-spin" />章構成を読み込み中…</div>;
  if (graphError) return <div className="m-6 rounded-lg border border-destructive/40 bg-card p-5 text-sm text-destructive">章構成を読み込めませんでした。{graphError instanceof Error ? graphError.message : ""}</div>;

  const unplaced = graph.episodes.filter((episode) => episode.unplaced || !graph.links.some((link) => link.from === episode.id || link.to === episode.id));
  const branchEpisode = branchDialog ? graph.episodes.find((episode) => episode.id === branchDialog.episodeId) : null;

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-background text-on-surface" data-testid="story-manuscript">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border-subtle bg-surface-container-low px-4 py-2">
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto text-xs text-on-surface-variant" data-testid="story-route-bar">
          {route.map((episode, index) => {
            const choices = routeLinkChoices(episode.id, graph.links, graph.episodes);
            const nextId = route[index + 1]?.id ?? choices[0]?.to ?? "";
            return <span key={`${episode.id}-${index}`} className="flex shrink-0 items-center gap-1">
              <button
                type="button"
                onClick={() => void selectEpisode(episode.id)}
                className={`rounded-sm px-1 py-0.5 transition-colors hover:bg-accent hover:text-foreground ${index === route.length - 1 ? "font-medium text-foreground" : ""}`}
              >
                {episode.title}
              </button>
              {choices.length > 1 ? (
                <AppSelect
                  size="sm"
                  aria-label={`${episode.title} の分岐`}
                  value={nextId}
                  onValueChange={(value) => void selectRouteBranch(episode.id, value)}
                  className="max-w-44 bg-card text-xs font-medium text-foreground"
                >
                  {choices.map((choice) => (
                    <option key={choice.to} value={choice.to}>
                      {choice.choiceLabel || graph.episodes.find((item) => item.id === choice.to)?.title || "分岐"}
                    </option>
                  ))}
                </AppSelect>
              ) : null}
              {index < route.length - 1 && <ChevronRight className="size-3" />}
            </span>;
          })}
          <span className="shrink-0 rounded-sm border border-border-subtle bg-surface-container-high px-2 py-0.5 text-[11px] text-on-surface-variant">{route.length}章 / {formatCount(countRouteCharacters(route))}字</span>
          {editingOffRoute && editingEpisode ? (
            <span className="shrink-0 rounded-sm border border-primary/40 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary" data-testid="story-route-editing-off-route">
              編集中: {editingEpisode.title}（別パターン）
            </span>
          ) : null}
        </div>
        <Button variant="outline" size="sm" onClick={() => void createEpisode()} disabled={branchBusy}><Plus className="size-3.5" />章を追加</Button>
        <Button variant="outline" size="sm" onClick={openBatchDialog} disabled={!routeEpisodeIds.length}><Sparkles className="size-3.5" />まとめて生成</Button>
        <Button variant="outline" size="sm" onClick={() => setContextVisible(true)} disabled={!detailEpisode}><WandSparkles className="size-3.5" />プロンプトプレビュー</Button>
        <Button size="sm" onClick={() => setGenerateDialog(true)} disabled={!detailEpisode}><Sparkles className="size-3.5" />AIで本文を生成</Button>
      </div>
      {job && <div className="flex shrink-0 items-center gap-2 border-b border-border bg-muted/40 px-3 py-2 text-xs"><Loader2 className="size-3.5 animate-spin" />生成ジョブ: {job.status}{job.message ? ` — ${job.message}` : ""}{["queued", "running"].includes(job.status) && <Button variant="ghost" size="xs" className="ml-auto" onClick={() => void cancelJob(jobId)}>キャンセル</Button>}</div>}
      <div className="grid min-h-0 flex-1 grid-cols-1 overflow-hidden lg:grid-cols-[minmax(15rem,20rem)_minmax(0,1fr)]">
        <aside className="flex min-h-0 flex-col border-b border-border-subtle bg-surface-charcoal lg:border-b-0 lg:border-r" data-testid="story-episode-list">
          <div className="border-b border-border-subtle px-3"><div className="flex items-center justify-between pb-3 pt-3"><div><div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-on-surface-variant">Manuscript</div><h2 className="text-sm font-semibold text-on-surface">現在のルート</h2></div><span className="text-xs text-muted-foreground">{visibleEpisodes.length}章</span></div></div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            {visibleEpisodes.map((episode, index) => renderEpisodeListRow(episode, index))}
            {editingOffRoute && editingEpisode ? (
              <div className="mt-4 border-t border-border pt-3" data-testid="story-editing-off-route">
                <div className="mb-2">
                  <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-primary">編集中（別パターン）</div>
                  <p className="mt-0.5 text-[11px] text-muted-foreground">現在のルート外の章です。採用ルートは左の一覧のままです。</p>
                </div>
                {renderEpisodeListRow(editingEpisode, null, { offRoute: true })}
                <div className="mt-2">{renderSiblingSwitcher("story-sibling-switcher-list", { showAdopt: true, siblingTestIdPrefix: "story-sibling-list" })}</div>
              </div>
            ) : null}
            <div className="mt-4 border-t border-border pt-3"><div className="mb-2 flex items-center gap-2 text-[10px] uppercase tracking-[0.12em] text-muted-foreground"><span>未配置</span><span className="rounded-full bg-muted px-1.5">{unplaced.length}</span></div>{unplaced.map((episode) => <button key={episode.id} type="button" onClick={() => selectEpisode(episode.id)} className="mb-1 flex w-full items-center gap-2 rounded-md border border-dashed border-border px-2 py-1.5 text-left text-xs hover:bg-accent"><span className="size-1.5 rounded-full bg-muted-foreground" /><span className="truncate">{episode.title}</span></button>)}{unplaced.length === 0 && <p className="text-[11px] text-muted-foreground">未配置の章はありません。</p>}</div>
          </div>
        </aside>
        <section className="flex min-h-0 min-w-0 flex-col bg-background" data-testid="story-manuscript-editor">
          {!detailEpisode ? <div className="flex h-full items-center justify-center p-8 text-center text-sm text-muted-foreground">左の章を選択してください。</div> : <>
            <div className="shrink-0 border-b border-border-subtle px-6 py-4">
              <div className="flex flex-wrap items-center gap-2">
                <Input value={title} onChange={(event) => setTitle(event.target.value)} className="h-9 min-w-48 flex-1 border-transparent bg-transparent px-0 text-lg font-semibold shadow-none focus-visible:border-input focus-visible:bg-card focus-visible:px-2" aria-label="章タイトル" />
                <EpisodeStatus status={detailEpisode.status} />
                <Button variant="outline" size="sm" onClick={() => void saveMeta()}><Save className="size-3.5" />章情報を保存</Button>
              </div>
              {selectedSiblingGroup ? <div className="mt-2 rounded-md border border-border-subtle bg-muted/20 p-2">{renderSiblingSwitcher("story-sibling-switcher-editor", { siblingTestIdPrefix: "story-sibling-editor" })}</div> : null}
              <details className="mt-2 rounded-md border border-border bg-muted/30 p-2" open><summary className="flex cursor-pointer items-center gap-1 text-xs font-medium"><ChevronDown className="size-3.5" />章プロット</summary><StoryAssistField assist={assist} target={{ fieldKind: "episode_plot", fieldLabel: "章プロット", workId, episodeId: detailEpisode.id, getCurrentText: () => plot }}><textarea value={plot} onChange={(event) => setPlot(event.target.value)} className="mt-2 min-h-36 w-full resize-y rounded-md border border-input bg-card p-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50" placeholder="この章で起こること" /></StoryAssistField></details>
            </div>
            <div className="relative min-h-0 flex-1 overflow-y-auto px-10 py-8">
              <div className="mb-4 flex items-center justify-end gap-2" data-testid="story-body-view-mode">
                <Button
                  type="button"
                  variant={bodyViewMode === "edit" ? "default" : "outline"}
                  size="sm"
                  onClick={() => setBodyViewMode("edit")}
                  data-testid="story-body-view-edit"
                >
                  <Pencil className="size-3.5" />編集
                </Button>
                <Button
                  type="button"
                  variant={bodyViewMode === "preview" ? "default" : "outline"}
                  size="sm"
                  onClick={() => setBodyViewMode("preview")}
                  data-testid="story-body-view-preview"
                >
                  <Eye className="size-3.5" />プレビュー
                </Button>
              </div>
              {bodyViewMode === "edit" ? (
                <>
                  <StoryIllustrationLayer episodeId={detailEpisode.id} body={body} variant="manage" />
                  <StoryAssistField
                    assist={assist}
                    showTrigger={false}
                    target={{
                      fieldKind: "episode_body",
                      fieldLabel: "本文",
                      workId,
                      episodeId: detailEpisode.id,
                      getCurrentText: () => bodyRef.current,
                      getSelection: () => toAssistSelection(selection),
                      getBodyEtag: () => bodyEtagRef.current,
                    }}
                  >
                    <div
                      className="relative min-h-0 flex-1"
                      onContextMenu={(event) => {
                        event.preventDefault();
                        setEpisodeMenu(null);
                        setEditorMenu(pointFromEvent(event));
                      }}
                    >
                      <LongTextEditor
                        ref={editorRef}
                        value={body}
                        onChange={(nextBody) => {
                          bodyRef.current = nextBody;
                          setBody(nextBody);
                          setDirty(nextBody !== loadedBodyRef.current);
                        }}
                        onSelectionChange={setSelection}
                        placeholder="本文を書き始める…"
                        minHeight={420}
                        className="min-h-[28rem]"
                      />
                    </div>
                  </StoryAssistField>
                </>
              ) : (
                <StoryIllustrationLayer episodeId={detailEpisode.id} body={body} variant="reading" />
              )}
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2 border-t border-border-subtle px-6 py-2 text-xs text-muted-foreground"><span>{formatCount(Array.from(body).length)} / {formatCount(detailEpisode.targetChars)}字</span><span className="text-border">•</span><span data-testid="story-manuscript-editor-save-state">{saveStatusLabel(saveState)}</span><span className="ml-auto flex items-center gap-2"><Button variant="outline" size="sm" onClick={() => void splitAtSelection()} disabled={!canSplitHere}><Split className="size-3.5" />カーソル位置で章を分割</Button><Button size="sm" onClick={() => void saveBody(true)} disabled={!dirty}><Check className="size-3.5" />保存</Button></span></div>
          </>}
        </section>
      </div>

      <Dialog open={Boolean(branchDialog)} onOpenChange={(open) => { if (!open) setBranchDialog(null); }}><DialogContent><DialogHeader><DialogTitle>{branchDialog?.mode === "duplicate" ? "複製して分岐にする" : "続きの分岐を追加"}</DialogTitle></DialogHeader><p className="text-sm text-muted-foreground">{branchDialog?.mode === "duplicate" ? `${branchEpisode?.title || "この章"}をコピーし、同じ親から別パターンを作成します。元の章は変更されません。` : `${branchEpisode?.title || "この章"}の続きとして白紙の章を作成します。`}</p><div className="space-y-3"><div><Label>新しい章のタイトル</Label><Input className="mt-1" value={branchTitle} onChange={(event) => setBranchTitle(event.target.value)} /></div><div><Label>選択肢ラベル</Label><Input className="mt-1" value={branchLabel} onChange={(event) => setBranchLabel(event.target.value)} placeholder="例: 王を疑う" /></div></div><DialogFooter><Button variant="outline" onClick={() => setBranchDialog(null)}>キャンセル</Button><Button onClick={() => void createBranch()} disabled={branchBusy || !branchTitle.trim()}>{branchBusy && <Loader2 className="size-3.5 animate-spin" />}作成</Button></DialogFooter></DialogContent></Dialog>
      <Dialog open={generateDialog} onOpenChange={(open) => { setGenerateDialog(open); if (open) setAllowOverwrite(false); }}><DialogContent><DialogHeader><DialogTitle>AIで本文を生成</DialogTitle></DialogHeader><p className="text-sm text-muted-foreground">現在の章のプロットと作品設定を使って本文を生成します。</p>{hasExistingBody && <label className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm"><Checkbox className="mt-0.5" checked={allowOverwrite} onCheckedChange={(checked) => setAllowOverwrite(checked === true)} /><span><span className="font-medium text-destructive">既存本文（{formatCount(detailEpisode?.charCount ?? 0)}字）を上書きする</span><span className="mt-1 block text-xs text-muted-foreground">生成前の本文は pre_ai 履歴として保存されます。</span></span></label>}<div className="rounded-md border border-border bg-muted/30 p-3 text-sm"><div className="text-xs text-muted-foreground">使用モデル（このジョブのみ）</div><StoryModelSelect value={generateModel} onChange={setGenerateModel} /><div className="mt-2 text-xs text-muted-foreground">解決結果: {work.resolvedModel || "設定に従う"}（{work.resolvedModelLayer || "執筆クラス"}）</div></div><DialogFooter><Button variant="outline" onClick={() => setGenerateDialog(false)}>キャンセル</Button><Button onClick={() => void generateEpisode()} disabled={generateBusy || (hasExistingBody && !allowOverwrite)}>{generateBusy && <Loader2 className="size-3.5 animate-spin" />}生成を開始</Button></DialogFooter></DialogContent></Dialog>
      <Dialog open={batchDialog} onOpenChange={setBatchDialog}><DialogContent size="2xl" className="max-h-[80vh] overflow-y-auto"><DialogHeader><DialogTitle>まとめて生成</DialogTitle></DialogHeader><div className="rounded-md border border-border bg-muted/30 p-3 text-sm"><div className="text-xs text-muted-foreground">使用モデル（全章共通・このジョブのみ）</div><StoryModelSelect value={batchModel} onChange={setBatchModel} /><div className="mt-2 text-xs text-muted-foreground">解決結果: {work.resolvedModel || "設定に従う"}（{work.resolvedModelLayer || "執筆クラス"}）</div></div><div className="flex items-center gap-2 text-xs"><Button variant="link" size="sm" className="h-6 px-0" onClick={() => setBatchSelected(routeEpisodeIds.filter((id) => visibleEpisodes.find((episode) => episode.id === id)?.plot.trim()))}>プロットのある全章を選択</Button><Button variant="link" size="sm" className="h-6 px-0" onClick={() => setBatchSelected([])}>選択解除</Button><span className="ml-auto text-muted-foreground">{batchSelected.length}章選択</span></div><div className="space-y-1">{visibleEpisodes.map((episode) => { const checked = batchSelected.includes(episode.id); const item = batchJob?.items?.find((value) => value.id === episode.id); return <label key={episode.id} className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm"><Checkbox checked={checked} onCheckedChange={() => setBatchSelected((current) => checked ? current.filter((id) => id !== episode.id) : [...current, episode.id])} /><span className="min-w-0 flex-1 truncate">{episode.title}</span>{episode.charCount > 0 ? <Badge variant="outline" className="text-[10px]">上書き</Badge> : null}{item && <span className="text-xs text-muted-foreground">{item.status}</span>}</label>; })}</div>{batchJob && <div className="rounded-md border border-border bg-muted/30 p-3 text-xs">ジョブ: {batchJob.status}{batchJob.error ? ` — ${batchJob.error}` : ""}{batchJob.progress && <span className="ml-2">{String(batchJob.progress.completed ?? 0)} / {String(batchJob.progress.total ?? batchSelected.length)}</span>}</div>}<DialogFooter><Button variant="outline" onClick={() => setBatchDialog(false)}>閉じる</Button>{batchJob && ["error", "canceled"].includes(batchJob.status) && <Button variant="outline" onClick={() => void resumeBatch()}>失敗した章から再開</Button>}{batchJob && ["queued", "running"].includes(batchJob.status) && <Button variant="destructive" onClick={() => void cancelJob(batchJobId)}>キャンセル</Button>}<Button onClick={() => void startBatch()} disabled={batchBusy || !batchSelected.length || batchJob?.status === "running" || batchJob?.status === "queued"}>{batchBusy && <Loader2 className="size-3.5 animate-spin" />}生成開始</Button></DialogFooter></DialogContent></Dialog>
      <Dialog open={checkpointDialog} onOpenChange={setCheckpointDialog}><DialogContent><DialogHeader><DialogTitle>チェックポイントを作成</DialogTitle></DialogHeader><Label>名前・メモ</Label><Input value={checkpointName} onChange={(event) => setCheckpointName(event.target.value)} placeholder="第1稿" /><DialogFooter><Button variant="outline" onClick={() => setCheckpointDialog(false)}>キャンセル</Button><Button onClick={() => void checkpoint()} disabled={!checkpointName.trim()}>作成</Button></DialogFooter></DialogContent></Dialog>
      <Dialog open={contextVisible} onOpenChange={setContextVisible}><DialogContent size="3xl" className="max-h-[80vh] overflow-y-auto"><DialogHeader><DialogTitle>プロンプトプレビュー</DialogTitle></DialogHeader><StoryPromptPreview episodeId={selectedId} /></DialogContent></Dialog>
      <StoryAssistDialog
        assist={assist}
        onApplied={async (nextText) => {
          if (!detailEpisode) return;
          if (assist.target?.fieldKind === "episode_body") {
            bodyRef.current = nextText;
            setBody(nextText);
            loadedBodyRef.current = nextText;
            setDirty(false);
            await mutateEpisode();
            await mutateGraph();
            await mutateRevisions();
            toast.success("AI修正案を適用しました（履歴から戻せます）", {
              action: { label: "履歴を見る", onClick: () => setHistoryVisible(true) },
            });
            return;
          }
          if (assist.target?.fieldKind === "episode_plot") {
            setPlot(nextText);
            try {
              await storyApi.updateEpisode(detailEpisode.id, { plot: nextText });
              loadedMetaRef.current = { ...loadedMetaRef.current, plot: nextText };
              await mutateEpisode();
              await mutateGraph();
              toast.success("章プロットのAI修正案を適用しました");
            } catch (error) {
              toast.error(error instanceof Error ? error.message : "章プロットを保存できませんでした");
            }
          }
        }}
      />
      <Dialog open={Boolean(deleteDialog)} onOpenChange={(open) => { if (!open) setDeleteDialog(null); }}><DialogContent><DialogHeader><DialogTitle>章を削除</DialogTitle></DialogHeader><p className="text-sm text-muted-foreground">「{deleteDialog?.title}」を論理削除します。前後の接続は可能な範囲で繋ぎ直し、直後は通知の「元に戻す」で復元できます。</p><DialogFooter><Button variant="outline" onClick={() => setDeleteDialog(null)}>キャンセル</Button><Button variant="destructive" onClick={() => void confirmDelete()} disabled={deleteBusy}>{deleteBusy && <Loader2 className="size-3.5 animate-spin" />}削除</Button></DialogFooter></DialogContent></Dialog>

      <StoryContextMenu point={episodeMenu?.point ?? null} entries={episodeMenu ? episodeMenuEntries(episodeMenu.episodeId) : []} label="章の操作メニュー" onClose={() => setEpisodeMenu(null)} />
      <StoryContextMenu point={editorMenu} entries={editorMenuEntries} label="本文エディタの操作メニュー" onClose={() => setEditorMenu(null)} />
    </div>
  );
}
