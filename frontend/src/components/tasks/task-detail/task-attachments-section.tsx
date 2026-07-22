"use client";

import { useCallback, useRef, useState } from "react";
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
  type TaskAttachment,
  type TaskReference,
} from "@/lib/task-api";
import { navigateChatSessionInPlace } from "@/lib/chat-navigation";
import { formatBytes } from "@/lib/utils";
import { useConfirm } from "@/hooks/use-confirm";

type ReferenceKind = "workspace_file" | "conversation_session" | "docs_node" | "url";

function referenceIcon(reference: TaskReference) {
  if (reference.reference_type === "task_attachment") return reference.attachment?.kind === "image" ? <ImageIcon className="size-5" /> : <FileText className="size-5" />;
  if (reference.reference_type.startsWith("conversation")) return <MessageSquare className="size-5" />;
  if (reference.reference_type === "docs_node") return <BookOpen className="size-5" />;
  if (reference.reference_type === "workspace_file") return <FileCode2 className="size-5" />;
  return <LinkIcon className="size-5" />;
}

function referenceCategory(reference: TaskReference) {
  if (reference.relation_type === "source") return "作成元";
  if (reference.reference_type === "task_attachment") return "Files";
  if (reference.reference_type.startsWith("conversation")) return "Chat";
  if (reference.reference_type === "docs_node") return "Docs";
  if (reference.reference_type === "workspace_file") return "Workspace";
  return "Links";
}

/** ファイル互換を維持しつつ、チャット/Docs/workspace/URLを同じReferencesとして扱う。 */
export function TaskReferencesSection({
  effectiveTaskId,
  attachments,
  setAttachments,
  references,
  setReferences,
}: {
  effectiveTaskId: string | null;
  attachments: TaskAttachment[];
  setAttachments: React.Dispatch<React.SetStateAction<TaskAttachment[]>>;
  references: TaskReference[];
  setReferences: React.Dispatch<React.SetStateAction<TaskReference[]>>;
}) {
  const confirm = useConfirm();
  const [uploading, setUploading] = useState(false);
  const [referenceKind, setReferenceKind] = useState<ReferenceKind | null>(null);
  const [referenceTarget, setReferenceTarget] = useState("");
  const [referenceName, setReferenceName] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const referenceAttachmentIds = new Set(
    references
      .filter((reference) => reference.reference_type === "task_attachment")
      .map((reference) => reference.target_id),
  );
  const allReferences: TaskReference[] = [
    ...references,
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

  const handleFiles = useCallback(async (files: FileList | null) => {
    if (!effectiveTaskId || !files?.length) return;
    setUploading(true);
    try {
      const uploaded: TaskAttachment[] = [];
      for (const file of Array.from(files)) uploaded.push(await taskApi.uploadAttachment(effectiveTaskId, file));
      setAttachments((prev) => [...uploaded, ...prev]);
      toast.success("ファイルをアップロードしました");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "ファイルのアップロードに失敗しました");
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }, [effectiveTaskId, setAttachments]);

  const openReference = useCallback((reference: TaskReference) => {
    if (!reference.exists) return;
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
  }, []);

  const removeReference = useCallback(async (reference: TaskReference) => {
    if (!effectiveTaskId) return;
    try {
      if (reference.reference_type === "task_attachment" && reference.attachment) {
        await taskApi.deleteAttachment(effectiveTaskId, reference.attachment.id);
        setAttachments((prev) => prev.filter((item) => item.id !== reference.attachment?.id));
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
        await taskApi.removeReference(effectiveTaskId, reference.id, confirmSource);
        setReferences((prev) => prev.filter((item) => item.id !== reference.id));
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "参照の解除に失敗しました");
    }
  }, [effectiveTaskId, setAttachments, setReferences, confirm]);

  const addReference = useCallback(async () => {
    if (!effectiveTaskId || !referenceKind || !referenceTarget.trim()) return;
    const data = referenceKind === "url"
      ? { reference_type: referenceKind, target_url: referenceTarget.trim(), display_name: referenceName.trim() || referenceTarget.trim() }
      : referenceKind === "workspace_file"
        ? { reference_type: referenceKind, target_path: referenceTarget.trim(), display_name: referenceName.trim() || referenceTarget.trim() }
        : { reference_type: referenceKind, target_id: referenceTarget.trim(), display_name: referenceName.trim() || referenceTarget.trim() };
    try {
      const created = await taskApi.addReference(effectiveTaskId, data);
      setReferences((prev) => [created, ...prev.filter((item) => item.id !== created.id)]);
      setReferenceKind(null); setReferenceTarget(""); setReferenceName("");
      toast.success("参照を追加しました");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "参照の追加に失敗しました");
    }
  }, [effectiveTaskId, referenceKind, referenceName, referenceTarget, setReferences]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-medium"><Paperclip className="size-4" />References{allReferences.length ? <Badge variant="secondary">{allReferences.length}</Badge> : null}</h2>
        <div className="flex items-center gap-1">
          <DropdownMenu>
            <DropdownMenuTrigger
              type="button"
              disabled={!effectiveTaskId || uploading}
              className="inline-flex h-9 items-center justify-center rounded-md border border-input bg-background px-3 text-sm font-medium shadow-xs hover:bg-accent hover:text-accent-foreground disabled:pointer-events-none disabled:opacity-50"
            >
              <Plus className="mr-1 size-4" />参照を追加
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onClick={() => inputRef.current?.click()}><Upload className="mr-2 size-4" />ファイルをアップロード</DropdownMenuItem>
              <DropdownMenuItem onClick={() => setReferenceKind("workspace_file")}><FileCode2 className="mr-2 size-4" />workspaceファイルを関連付け</DropdownMenuItem>
              <DropdownMenuItem onClick={() => setReferenceKind("conversation_session")}><MessageSquare className="mr-2 size-4" />チャットセッションを関連付け</DropdownMenuItem>
              <DropdownMenuItem onClick={() => setReferenceKind("docs_node")}><BookOpen className="mr-2 size-4" />Docsを関連付け</DropdownMenuItem>
              <DropdownMenuItem onClick={() => setReferenceKind("url")}><LinkIcon className="mr-2 size-4" />URLを追加</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <input ref={inputRef} type="file" multiple className="hidden" onChange={(event) => void handleFiles(event.target.files)} />
        </div>
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
            {reference.can_remove && <Button type="button" size="icon" variant="ghost" onClick={() => void removeReference(reference)}><Trash2 className="size-4" /></Button>}
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
    </div>
  );
}

/** 既存importとの互換用。新規コードではTaskReferencesSectionを使う。 */
export const TaskAttachmentsSection = TaskReferencesSection;
