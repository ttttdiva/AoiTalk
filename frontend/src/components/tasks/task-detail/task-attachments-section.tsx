"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type React from "react";
import {
  ExternalLink,
  FileText,
  Image as ImageIcon,
  Link as LinkIcon,
  MessageSquare,
  Paperclip,
  Plus,
  Trash2,
  Upload,
  FileCode2,
  BookOpen,
  ListTodo,
  Loader2,
  AppWindow,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  taskApi,
  type Task,
  type TaskAttachment,
  type TaskReference,
  type TaskAppLink,
} from "@/lib/task-api";
import { appsApi, type AppSummary, type AppTarget } from "@/lib/apps-api";
import { AppSelect } from "@/components/ui/app-select";
import { navigateChatSessionInPlace } from "@/lib/chat-navigation";
import { formatBytes } from "@/lib/utils";
import { useConfirm } from "@/hooks/use-confirm";

type ReferenceKind = "workspace_file" | "conversation_session" | "docs_node" | "url";

type ReferenceMutation = {
  deletedId?: string;
  deletedTargetId?: string;
  deletionFailed?: boolean;
};

type Tombstones = ReadonlySet<string> | ReadonlyMap<string, number>;

function mergeTaskItems<T extends { id: string }>(
  fetched: T[],
  previous: T[],
  tombstones: Tombstones,
): T[] {
  const fetchedIds = new Set(fetched.map((item) => item.id));
  return [
    ...previous.filter(
      (item) => !fetchedIds.has(item.id) && !tombstones.has(item.id),
    ),
    ...fetched.filter((item) => !tombstones.has(item.id)),
  ];
}

function reconcileTombstones(
  tombstones: Map<string, number>,
  fetchedIds: ReadonlySet<string>,
  requestedGeneration: number,
  currentGeneration: number,
) {
  if (requestedGeneration !== currentGeneration) return;
  for (const id of tombstones.keys()) {
    if (!fetchedIds.has(id)) tombstones.delete(id);
  }
}

function referenceIcon(reference: TaskReference) {
  if (reference.reference_type === "task_attachment") return reference.attachment?.kind === "image" ? <ImageIcon className="size-5" /> : <FileText className="size-5" />;
  if (reference.reference_type === "task") return <ListTodo className="size-5" />;
  if (reference.reference_type.startsWith("conversation")) return <MessageSquare className="size-5" />;
  if (reference.reference_type === "docs_node") return <BookOpen className="size-5" />;
  if (reference.reference_type === "workspace_file") return <FileCode2 className="size-5" />;
  if (reference.reference_type === "app") return <AppWindow className="size-5" />;
  return <LinkIcon className="size-5" />;
}

function referenceCategory(reference: TaskReference) {
  if (reference.relation_type === "source") return "作成元";
  if (reference.reference_type === "task_attachment") return "Files";
  if (reference.reference_type === "task") return "関連タスク";
  if (reference.reference_type.startsWith("conversation")) return "Chat";
  if (reference.reference_type === "docs_node") return "Docs";
  if (reference.reference_type === "workspace_file") return "Workspace";
  if (reference.reference_type === "app") return `App · ${reference.relation_type}`;
  return "Links";
}

/** ファイル互換を維持しつつ、チャット/Docs/workspace/URLを同じReferencesとして扱う。 */
export function TaskReferencesSection({
  effectiveTaskId,
  projectId,
  attachments,
  setAttachments,
  references,
  setReferences,
  uploading,
  onFilesSelected,
  onEnsureTask,
  onAttachmentMutation,
  onReferenceMutation,
  onOpenTask,
  readOnly = false,
}: {
  effectiveTaskId: string | null;
  projectId?: string | null;
  attachments: TaskAttachment[];
  setAttachments: React.Dispatch<React.SetStateAction<TaskAttachment[]>>;
  references: TaskReference[];
  setReferences: React.Dispatch<React.SetStateAction<TaskReference[]>>;
  uploading: boolean;
  onFilesSelected: (files: FileList | File[]) => Promise<void>;
  /** 新規タスクのドラフトを保存して、参照操作に使えるIDを確保する。 */
  onEnsureTask?: () => Promise<string | null>;
  /** 一覧GETより先に完了した添付／参照mutationを初期reloadへ反映する。 */
  onAttachmentMutation?: (mutation?: ReferenceMutation) => void;
  onReferenceMutation?: (mutation?: ReferenceMutation) => void;
  onOpenTask?: (taskId: string) => void;
  readOnly?: boolean;
}) {
  const confirm = useConfirm();
  const [referenceKind, setReferenceKind] = useState<ReferenceKind | null>(null);
  const [referenceTarget, setReferenceTarget] = useState("");
  const [referenceName, setReferenceName] = useState("");
  const [taskPickerOpen, setTaskPickerOpen] = useState(false);
  const [taskSearch, setTaskSearch] = useState("");
  const [taskCandidates, setTaskCandidates] = useState<Task[]>([]);
  const [taskCandidatesLoading, setTaskCandidatesLoading] = useState(false);
  const [linkingTaskId, setLinkingTaskId] = useState<string | null>(null);
  const [appLinks, setAppLinks] = useState<TaskAppLink[]>([]);
  const [appPickerOpen, setAppPickerOpen] = useState(false);
  const [appCandidates, setAppCandidates] = useState<AppSummary[]>([]);
  const [appTargets, setAppTargets] = useState<AppTarget[]>([]);
  const [selectedAppId, setSelectedAppId] = useState("");
  const [selectedTargetId, setSelectedTargetId] = useState("");
  const [selectedAppRelation, setSelectedAppRelation] = useState<TaskAppLink["relation_type"]>("related");
  const inputRef = useRef<HTMLInputElement>(null);
  const activeTaskIdRef = useRef(effectiveTaskId);
  const referenceOperationInFlightRef = useRef(false);
  const appLinksMutationGenerationRef = useRef(0);
  const appLinksTombstonesRef = useRef(new Map<string, number>());
  const appLinksInitialReloadTaskIdRef = useRef<string | null>(null);

  const ensureEffectiveTaskId = useCallback(async () => {
    if (readOnly) return null;
    const taskId = effectiveTaskId ?? (await onEnsureTask?.()) ?? null;
    if (taskId) activeTaskIdRef.current = taskId;
    return taskId;
  }, [effectiveTaskId, onEnsureTask, readOnly]);

  const runReferenceOperation = useCallback(
    async (operation: (taskId: string) => Promise<void>) => {
      if (readOnly || referenceOperationInFlightRef.current) return;
      referenceOperationInFlightRef.current = true;
      try {
        const taskId = await ensureEffectiveTaskId();
        if (!taskId) {
          toast.error("タイトルを入力してから参照を追加してください");
          return;
        }
        await operation(taskId);
      } catch (err) {
        toast.error(
          err instanceof Error ? err.message : "参照操作に失敗しました",
        );
      } finally {
        referenceOperationInFlightRef.current = false;
      }
    },
    [ensureEffectiveTaskId, readOnly],
  );

  const markAppLinksMutation = useCallback((mutation?: ReferenceMutation) => {
    appLinksMutationGenerationRef.current += 1;
    if (!mutation?.deletedId) return;
    if (mutation.deletionFailed) {
      appLinksTombstonesRef.current.delete(mutation.deletedId);
    } else {
      appLinksTombstonesRef.current.set(
        mutation.deletedId,
        appLinksMutationGenerationRef.current,
      );
    }
  }, []);

  useEffect(() => {
    const previousTaskId = activeTaskIdRef.current;
    activeTaskIdRef.current = effectiveTaskId;
    // ドラフトを保存した直後は、ensureEffectiveTaskId() が設定したIDと
    // 同じなので、開こうとしているPickerを閉じない。既存タスクを
    // 切り替えた場合だけ、前のタスクの非同期操作を破棄する。
    if (previousTaskId && previousTaskId !== effectiveTaskId) {
      setLinkingTaskId(null);
      setTaskPickerOpen(false);
      setTaskSearch("");
      setAppPickerOpen(false);
      setAppLinks([]);
      appLinksTombstonesRef.current.clear();
    }
  }, [effectiveTaskId]);
  useEffect(() => {
    if (!readOnly) return;
    setReferenceKind(null);
    setTaskPickerOpen(false);
    setAppPickerOpen(false);
  }, [readOnly]);
  useEffect(() => {
    if (!effectiveTaskId) return;
    const taskId = effectiveTaskId;
    const requestMutationGeneration = appLinksMutationGenerationRef.current;
    appLinksInitialReloadTaskIdRef.current = taskId;
    const listAppLinks = (taskApi as unknown as {
      listAppLinks?: (id: string) => Promise<{ apps?: TaskAppLink[] }>;
    }).listAppLinks;
    if (!listAppLinks) return;
    void listAppLinks(taskId)
      .then((result) => {
        if (activeTaskIdRef.current !== taskId) return;
        const nextAppLinks = result.apps || [];
        const nextAppLinkIds = new Set(
          nextAppLinks.map((appLink) => appLink.id),
        );
        reconcileTombstones(
          appLinksTombstonesRef.current,
          nextAppLinkIds,
          requestMutationGeneration,
          appLinksMutationGenerationRef.current,
        );
        const shouldMerge =
          requestMutationGeneration !== appLinksMutationGenerationRef.current ||
          appLinksInitialReloadTaskIdRef.current === taskId;
        setAppLinks((previous) =>
          shouldMerge
            ? mergeTaskItems(
                nextAppLinks,
                previous,
                appLinksTombstonesRef.current,
              )
            : nextAppLinks.filter(
                (appLink) => !appLinksTombstonesRef.current.has(appLink.id),
              ),
        );
        if (appLinksInitialReloadTaskIdRef.current === taskId) {
          appLinksInitialReloadTaskIdRef.current = null;
        }
      })
      .catch(() => {
        if (activeTaskIdRef.current !== taskId) return;
        const shouldPreserve =
          requestMutationGeneration !== appLinksMutationGenerationRef.current ||
          appLinksInitialReloadTaskIdRef.current === taskId;
        if (!shouldPreserve && appLinksTombstonesRef.current.size === 0) {
          setAppLinks([]);
        }
        if (appLinksInitialReloadTaskIdRef.current === taskId) {
          appLinksInitialReloadTaskIdRef.current = null;
        }
      });
  }, [effectiveTaskId]);
  const referenceAttachmentIds = new Set(
    references
      .filter((reference) => reference.reference_type === "task_attachment")
      .map((reference) => reference.target_id),
  );
  const allReferences: TaskReference[] = [
    ...references,
    ...appLinks.map((link) => ({
      id: `app:${link.id}`,
      reference_type: "app",
      relation_type: link.relation_type,
      display_name: link.app?.name || link.app_id,
       subtitle: link.target?.display_name || link.target?.target_key || null,
      target_id: link.app_id,
      metadata: {
        app_link_id: link.id,
        target_id: link.target_id,
        relation_type: link.relation_type,
      },
      created_by: link.created_by,
      created_at: link.created_at,
      can_remove: true,
      exists: true,
      open: { id: link.app_id, url: `/apps/${encodeURIComponent(link.app_id)}${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}` },
    } satisfies TaskReference)),
    ...attachments.filter((attachment) => !referenceAttachmentIds.has(attachment.id)).map((attachment) => ({
      id: `attachment:${attachment.id}`,
      reference_type: "task_attachment",
      relation_type: "related",
      display_name: attachment.display_name,
      subtitle:
        formatBytes(attachment.size_bytes ?? undefined) === "-"
          ? attachment.kind
          : `${attachment.kind} · ${formatBytes(attachment.size_bytes ?? undefined)}`,
      target_id: attachment.id,
      target_path: attachment.file_path,
      target_url: attachment.url,
      metadata: attachment.metadata ?? {},
      created_by: attachment.created_by,
      created_at: attachment.created_at,
      can_remove: true,
      exists: true,
      open: { id: attachment.id, path: attachment.file_path, url: attachment.url },
      attachment,
    } satisfies TaskReference)),
  ];
  const relatedTaskIds = useMemo(
    () =>
      new Set(
        references
          .filter((reference) => reference.reference_type === "task")
          .map((reference) => reference.target_id)
          .filter((taskId): taskId is string => Boolean(taskId)),
      ),
    [references],
  );
  const filteredTaskCandidates = useMemo(() => {
    const query = taskSearch.trim().toLocaleLowerCase("ja");
    return taskCandidates
      .filter(
        (candidate) =>
          candidate.id !== effectiveTaskId && !relatedTaskIds.has(candidate.id),
      )
      .filter((candidate) => {
        if (!query) return true;
        return `${candidate.title} ${candidate.project_name ?? ""}`
          .toLocaleLowerCase("ja")
          .includes(query);
      })
      .slice(0, 50);
  }, [
    effectiveTaskId,
    relatedTaskIds,
    taskCandidates,
    taskSearch,
  ]);

  const openReference = useCallback((reference: TaskReference) => {
    if (!reference.exists) return;
    if (reference.reference_type === "app" && reference.target_id) {
      window.location.href = reference.open.url || `/apps/${encodeURIComponent(reference.target_id)}${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`;
      return;
    }
    if (reference.reference_type === "task" && reference.target_id) {
      if (onOpenTask) onOpenTask(reference.target_id);
      else window.location.href = `/tasks?detail=${encodeURIComponent(reference.target_id)}`;
      return;
    }
    const url = reference.open.url;
    const path = reference.open.path;
    const chatUrl = url ?? path;
    if (chatUrl?.startsWith("/chat")) {
      if (!navigateChatSessionInPlace(chatUrl)) window.location.href = chatUrl;
      return;
    }
    if (url) { window.open(url, "_blank", "noopener,noreferrer"); return; }
    if (path?.startsWith("/")) { window.location.href = path; return; }
    if (reference.reference_type === "workspace_file" && reference.target_path) {
      window.location.href = `/filer?path=${encodeURIComponent(reference.target_path)}`;
      return;
    }
    if (reference.target_id) window.location.href = `/docs/${encodeURIComponent(reference.target_id)}`;
  }, [onOpenTask, projectId]);

  const openTaskPicker = useCallback(async () => {
    await runReferenceOperation(async (taskId) => {
      setTaskPickerOpen(true);
      setTaskSearch("");
      setTaskCandidatesLoading(true);
      try {
        const candidates = await taskApi.listTasks();
        if (activeTaskIdRef.current === taskId) setTaskCandidates(candidates);
      } catch (err) {
        if (activeTaskIdRef.current !== taskId) return;
        setTaskCandidates([]);
        toast.error(
          err instanceof Error ? err.message : "タスク候補の取得に失敗しました",
        );
      } finally {
        if (activeTaskIdRef.current === taskId) {
          setTaskCandidatesLoading(false);
        }
      }
    });
  }, [runReferenceOperation]);

  const openAppPicker = useCallback(async () => {
    await runReferenceOperation(async (taskId) => {
      setAppPickerOpen(true);
      setSelectedAppId("");
      setSelectedTargetId("");
      setAppTargets([]);
      try {
        const result = await appsApi.list(projectId || undefined);
        if (activeTaskIdRef.current === taskId) {
          setAppCandidates(result.apps || []);
        }
      } catch (err) {
        if (activeTaskIdRef.current !== taskId) return;
        setAppCandidates([]);
        toast.error(
          err instanceof Error ? err.message : "App候補の取得に失敗しました",
        );
      }
    });
  }, [projectId, runReferenceOperation]);

  const handleAppCandidateChange = useCallback(async (appId: string) => {
    setSelectedAppId(appId);
    setSelectedTargetId("");
    if (!appId) {
      setAppTargets([]);
      return;
    }
    try {
      const result = await appsApi.getTargets(appId, projectId || undefined);
      setAppTargets(result.targets || []);
    } catch (err) {
      setAppTargets([]);
      toast.error(err instanceof Error ? err.message : "Target候補の取得に失敗しました");
    }
  }, [projectId]);

  const addAppLink = useCallback(async () => {
    if (!selectedAppId) return;
    await runReferenceOperation(async (taskId) => {
      markAppLinksMutation();
      try {
        const result = await taskApi.linkApp(taskId, {
          app_id: selectedAppId,
          target_id: selectedTargetId || null,
          relation_type: selectedAppRelation,
        });
        if (activeTaskIdRef.current !== taskId) return;
        setAppLinks((prev) => [
          result.link,
          ...prev.filter((item) => item.id !== result.link.id),
        ]);
        setAppPickerOpen(false);
        toast.success("Appを関連付けました");
      } catch (err) {
        if (activeTaskIdRef.current !== taskId) return;
        toast.error(
          err instanceof Error ? err.message : "Appの関連付けに失敗しました",
        );
      } finally {
        if (activeTaskIdRef.current === taskId) markAppLinksMutation();
      }
    });
  }, [
    markAppLinksMutation,
    runReferenceOperation,
    selectedAppId,
    selectedAppRelation,
    selectedTargetId,
  ]);

  const addTaskReference = useCallback(
    async (targetTask: Task) => {
      if (linkingTaskId) return;
      await runReferenceOperation(async (taskId) => {
        setLinkingTaskId(targetTask.id);
        onReferenceMutation?.();
        try {
          const created = await taskApi.addReference(taskId, {
            reference_type: "task",
            relation_type: "related",
            target_id: targetTask.id,
          });
          if (activeTaskIdRef.current !== taskId) return;
          setReferences((prev) => [
            created,
            ...prev.filter((item) => item.id !== created.id),
          ]);
          setTaskPickerOpen(false);
          setTaskSearch("");
          toast.success("タスクを関連付けました");
        } catch (err) {
          if (activeTaskIdRef.current !== taskId) return;
          toast.error(
            err instanceof Error ? err.message : "タスクの関連付けに失敗しました",
          );
        } finally {
          if (activeTaskIdRef.current === taskId) onReferenceMutation?.();
          if (activeTaskIdRef.current === taskId) {
            setLinkingTaskId(null);
          }
        }
      });
    },
    [linkingTaskId, onReferenceMutation, runReferenceOperation, setReferences],
  );

  const removeReference = useCallback(async (reference: TaskReference) => {
    await runReferenceOperation(async (taskId) => {
      if (reference.reference_type === "task_attachment" && reference.attachment) {
        const attachmentId = reference.attachment.id;
        if (activeTaskIdRef.current !== taskId) return;
        onAttachmentMutation?.({ deletedId: attachmentId });
        onReferenceMutation?.({
          deletedId: reference.id,
          deletedTargetId: attachmentId,
        });
        try {
          await taskApi.deleteAttachment(taskId, attachmentId);
          if (activeTaskIdRef.current !== taskId) return;
          setAttachments((prev) =>
            prev.filter((item) => item.id !== attachmentId),
          );
          setReferences((prev) =>
            prev.filter(
              (item) =>
                item.id !== reference.id &&
                !(
                  item.reference_type === "task_attachment" &&
                  (item.target_id === attachmentId ||
                    item.attachment?.id === attachmentId)
                ),
            ),
          );
        } catch (err) {
          if (activeTaskIdRef.current !== taskId) return;
          onAttachmentMutation?.({
            deletedId: attachmentId,
            deletionFailed: true,
          });
          onReferenceMutation?.({
            deletedId: reference.id,
            deletedTargetId: attachmentId,
            deletionFailed: true,
          });
          toast.error(
            err instanceof Error ? err.message : "参照の解除に失敗しました",
          );
        } finally {
          if (activeTaskIdRef.current === taskId) {
            onAttachmentMutation?.();
            onReferenceMutation?.();
          }
        }
      } else if (reference.reference_type === "app" && reference.target_id) {
        const appLinkId =
          typeof reference.metadata?.app_link_id === "string"
            ? reference.metadata.app_link_id
            : null;
        if (activeTaskIdRef.current !== taskId) return;
        markAppLinksMutation({ deletedId: appLinkId ?? undefined });
        try {
          await taskApi.unlinkApp(taskId, reference.target_id, {
            targetId:
              typeof reference.metadata?.target_id === "string"
                ? reference.metadata.target_id
                : null,
            relationType: reference.relation_type,
          });
          if (activeTaskIdRef.current !== taskId) return;
          setAppLinks((prev) =>
            prev.filter((item) => item.id !== reference.metadata?.app_link_id),
          );
        } catch (err) {
          if (activeTaskIdRef.current !== taskId) return;
          markAppLinksMutation({
            deletedId: appLinkId ?? undefined,
            deletionFailed: true,
          });
          toast.error(
            err instanceof Error ? err.message : "参照の解除に失敗しました",
          );
        } finally {
          if (activeTaskIdRef.current === taskId) markAppLinksMutation();
        }
      } else {
        const confirmSource = reference.relation_type === "source";
        if (
          confirmSource &&
          !(await confirm({
            description: "作成元チャットとの紐づきを解除しますか？",
            destructive: true,
          }))
        )
          return;
        if (activeTaskIdRef.current !== taskId) return;
        onReferenceMutation?.({ deletedId: reference.id });
        try {
          await taskApi.removeReference(taskId, reference.id, confirmSource);
          if (activeTaskIdRef.current !== taskId) return;
          setReferences((prev) => prev.filter((item) => item.id !== reference.id));
        } catch (err) {
          if (activeTaskIdRef.current !== taskId) return;
          onReferenceMutation?.({
            deletedId: reference.id,
            deletionFailed: true,
          });
          toast.error(
            err instanceof Error ? err.message : "参照の解除に失敗しました",
          );
        } finally {
          if (activeTaskIdRef.current === taskId) onReferenceMutation?.();
        }
      }
    });
  }, [
    confirm,
    markAppLinksMutation,
    onAttachmentMutation,
    onReferenceMutation,
    runReferenceOperation,
    setAppLinks,
    setAttachments,
    setReferences,
  ]);

  const addReference = useCallback(async () => {
    if (!referenceKind || !referenceTarget.trim()) return;
    const data = referenceKind === "url"
      ? { reference_type: referenceKind, target_url: referenceTarget.trim(), display_name: referenceName.trim() || referenceTarget.trim() }
      : referenceKind === "workspace_file"
        ? { reference_type: referenceKind, target_path: referenceTarget.trim(), display_name: referenceName.trim() || referenceTarget.trim() }
        : { reference_type: referenceKind, target_id: referenceTarget.trim(), display_name: referenceName.trim() || referenceTarget.trim() };
    await runReferenceOperation(async (taskId) => {
      onReferenceMutation?.();
      try {
        const created = await taskApi.addReference(taskId, data);
        if (activeTaskIdRef.current !== taskId) return;
        setReferences((prev) => [
          created,
          ...prev.filter((item) => item.id !== created.id),
        ]);
        setReferenceKind(null);
        setReferenceTarget("");
        setReferenceName("");
        toast.success("参照を追加しました");
      } catch (err) {
        if (activeTaskIdRef.current !== taskId) return;
        toast.error(
          err instanceof Error ? err.message : "参照の追加に失敗しました",
        );
      } finally {
        onReferenceMutation?.();
      }
    });
  }, [
    onReferenceMutation,
    referenceKind,
    referenceName,
    referenceTarget,
    runReferenceOperation,
    setReferences,
  ]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-medium"><Paperclip className="size-4" />References{allReferences.length ? <Badge variant="secondary">{allReferences.length}</Badge> : null}</h2>
        {!readOnly && <div className="flex items-center gap-1">
          <DropdownMenu>
            <DropdownMenuTrigger
              type="button"
              disabled={uploading}
              className="inline-flex h-9 items-center justify-center rounded-md border border-input bg-background px-3 text-sm font-medium shadow-xs hover:bg-accent hover:text-accent-foreground disabled:pointer-events-none disabled:opacity-50"
            >
              <Plus className="mr-1 size-4" />参照を追加
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onClick={() => inputRef.current?.click()}><Upload className="mr-2 size-4" />ファイルをアップロード</DropdownMenuItem>
              <DropdownMenuItem onClick={() => void openTaskPicker()}><ListTodo className="mr-2 size-4" />タスクを関連付け</DropdownMenuItem>
              <DropdownMenuItem onClick={() => void openAppPicker()}><AppWindow className="mr-2 size-4" />Appを関連付け</DropdownMenuItem>
              <DropdownMenuItem onClick={() => setReferenceKind("workspace_file")}><FileCode2 className="mr-2 size-4" />workspaceファイルを関連付け</DropdownMenuItem>
              <DropdownMenuItem onClick={() => setReferenceKind("conversation_session")}><MessageSquare className="mr-2 size-4" />チャットセッションを関連付け</DropdownMenuItem>
              <DropdownMenuItem onClick={() => setReferenceKind("docs_node")}><BookOpen className="mr-2 size-4" />Docsを関連付け</DropdownMenuItem>
              <DropdownMenuItem onClick={() => setReferenceKind("url")}><LinkIcon className="mr-2 size-4" />URLを追加</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <input
            ref={inputRef}
            type="file"
            multiple
            className="hidden"
            onChange={(event) => {
              const input = event.currentTarget;
              if (input.files) void onFilesSelected(input.files).finally(() => { input.value = ""; });
            }}
          />
        </div>}
      </div>
      {allReferences.length === 0 ? <p className="text-sm text-muted-foreground">参照はありません</p> : <div className="grid gap-2 sm:grid-cols-2">
        {allReferences.map((reference) => {
          const attachment = reference.attachment;
          const href = attachment?.url || reference.open.url || reference.open.path || "#";
          return <div key={reference.id} className="flex min-w-0 items-center gap-3 rounded-lg border p-2">
            <div className="flex size-12 shrink-0 items-center justify-center overflow-hidden rounded-md bg-muted">{attachment?.kind === "image" ? <img src={href} alt="" className="size-full object-cover" /> : referenceIcon(reference)}</div>
            <button type="button" className="min-w-0 flex-1 text-left" onClick={() => openReference(reference)} disabled={!reference.exists}>
              <p className="truncate text-sm font-medium">{reference.display_name}</p>
              <p className="truncate text-xs text-muted-foreground">{referenceCategory(reference)}{reference.subtitle ? ` · ${reference.subtitle}` : ""}{!reference.exists ? " · 参照先が見つかりません" : ""}</p>
            </button>
            {reference.exists && <Button type="button" size="icon" variant="ghost" onClick={() => openReference(reference)}><ExternalLink className="size-4" /></Button>}
            {reference.can_remove && !readOnly && <Button type="button" size="icon" variant="ghost" onClick={() => void removeReference(reference)}><Trash2 className="size-4" /></Button>}
          </div>;
        })}
      </div>}
      <Dialog open={referenceKind != null} onOpenChange={(open) => { if (!open) setReferenceKind(null); }}>
        <DialogContent>
          <DialogHeader><DialogTitle>参照を追加</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <Input value={referenceTarget} onChange={(event) => setReferenceTarget(event.target.value)} placeholder={referenceKind === "url" ? "https://..." : referenceKind === "workspace_file" ? "プロジェクト相対パス" : "参照先ID"} autoFocus />
            <Input value={referenceName} onChange={(event) => setReferenceName(event.target.value)} placeholder="表示名（任意）" />
          </div>
          <DialogFooter><Button variant="outline" onClick={() => setReferenceKind(null)}>キャンセル</Button><Button onClick={() => void addReference()} disabled={!referenceTarget.trim()}>追加</Button></DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={taskPickerOpen}
        onOpenChange={(open) => {
          setTaskPickerOpen(open);
          if (!open) setTaskSearch("");
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>関連タスクを選択</DialogTitle>
          </DialogHeader>
          <Input
            value={taskSearch}
            onChange={(event) => setTaskSearch(event.target.value)}
            placeholder="タスク名・プロジェクト名で検索"
            autoFocus
          />
          <div className="max-h-80 space-y-1 overflow-y-auto">
            {taskCandidatesLoading ? (
              <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                タスクを読み込み中
              </div>
            ) : filteredTaskCandidates.length === 0 ? (
              <p className="py-8 text-center text-sm text-muted-foreground">
                関連付けできるタスクがありません
              </p>
            ) : (
              filteredTaskCandidates.map((candidate) => (
                <button
                  key={candidate.id}
                  type="button"
                  className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-left hover:bg-accent disabled:opacity-50"
                  disabled={linkingTaskId != null}
                  onClick={() => void addTaskReference(candidate)}
                >
                  <ListTodo className="size-4 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">
                      {candidate.title}
                    </span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {candidate.project_name ?? "プロジェクト不明"} · {candidate.status}
                    </span>
                  </span>
                  {linkingTaskId === candidate.id ? (
                    <Loader2 className="size-4 animate-spin" />
                  ) : null}
                </button>
              ))
            )}
          </div>
        </DialogContent>
      </Dialog>
      <Dialog
        open={appPickerOpen}
        onOpenChange={(open) => {
          setAppPickerOpen(open);
          if (!open) {
            setSelectedAppId("");
            setSelectedTargetId("");
            setAppTargets([]);
          }
        }}
      >
        <DialogContent>
          <DialogHeader><DialogTitle>Appを関連付け</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <AppSelect
              value={selectedAppId}
              onValueChange={(value) => void handleAppCandidateChange(value)}
              placeholder="Appを選択"
              className="w-full justify-between"
            >
              <option value="">Appを選択</option>
              {appCandidates.map((app) => <option key={app.id} value={app.id}>{app.name}</option>)}
            </AppSelect>
            <AppSelect
              value={selectedTargetId}
              onValueChange={setSelectedTargetId}
              placeholder="Target（任意）"
              className="w-full justify-between"
              disabled={!selectedAppId}
            >
              <option value="">Targetなし</option>
              {appTargets.map((target) => <option key={target.id} value={target.id}>{target.display_name} · {target.target_key}</option>)}
            </AppSelect>
            <AppSelect
              value={selectedAppRelation}
              onValueChange={setSelectedAppRelation}
              className="w-full justify-between"
            >
              <option value="related">related</option>
              <option value="develops">develops</option>
              <option value="fixes">fixes</option>
              <option value="tests">tests</option>
              <option value="releases">releases</option>
              <option value="uses">uses</option>
            </AppSelect>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAppPickerOpen(false)}>キャンセル</Button>
            <Button onClick={() => void addAppLink()} disabled={!selectedAppId}>関連付け</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/** 既存importとの互換用。新規コードではTaskReferencesSectionを使う。 */
export const TaskAttachmentsSection = TaskReferencesSection;
