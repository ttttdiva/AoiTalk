"use client";

import { useCallback, useRef, useState } from "react";
import type React from "react";

import {
  ExternalLink,
  FileText,
  Image as ImageIcon,
  Paperclip,
  Trash2,
  Upload,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { taskApi, type TaskAttachment } from "@/lib/task-api";
import { formatBytes } from "@/components/tasks/task-detail/task-detail-utils";

/** 添付ファイルセクション（アップロード / 一覧 / 削除）。 */
export function TaskAttachmentsSection({
  effectiveTaskId,
  attachments,
  setAttachments,
}: {
  effectiveTaskId: string | null;
  attachments: TaskAttachment[];
  setAttachments: React.Dispatch<React.SetStateAction<TaskAttachment[]>>;
}) {
  const [uploadingAttachment, setUploadingAttachment] = useState(false);
  const attachmentInputRef = useRef<HTMLInputElement>(null);

  const handleAttachmentFiles = useCallback(
    async (files: FileList | null) => {
      if (!effectiveTaskId || !files?.length) return;
      setUploadingAttachment(true);
      try {
        const uploaded: TaskAttachment[] = [];
        for (const file of Array.from(files)) {
          uploaded.push(await taskApi.uploadAttachment(effectiveTaskId, file));
        }
        setAttachments((prev) => [...uploaded, ...prev]);
        toast.success("Attachment uploaded");
      } catch (err) {
        console.error("添付ファイルアップロード失敗", err);
        toast.error(
          err instanceof Error ? err.message : "Attachment upload failed",
        );
      } finally {
        setUploadingAttachment(false);
        if (attachmentInputRef.current) {
          attachmentInputRef.current.value = "";
        }
      }
    },
    [effectiveTaskId, setAttachments],
  );

  const handleDeleteAttachment = useCallback(
    async (attachmentId: string) => {
      if (!effectiveTaskId) return;
      try {
        await taskApi.deleteAttachment(effectiveTaskId, attachmentId);
        setAttachments((prev) =>
          prev.filter((item) => item.id !== attachmentId),
        );
      } catch (err) {
        console.error("添付ファイル削除失敗", err);
        toast.error(
          err instanceof Error ? err.message : "Attachment delete failed",
        );
      }
    },
    [effectiveTaskId, setAttachments],
  );

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-medium">
          <Paperclip className="size-4" />
          Attachments
          {attachments.length > 0 ? (
            <Badge variant="secondary">{attachments.length}</Badge>
          ) : null}
        </h2>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => attachmentInputRef.current?.click()}
          disabled={!effectiveTaskId || uploadingAttachment}
        >
          <Upload className="mr-2 size-4" />
          {uploadingAttachment ? "Uploading..." : "Upload"}
        </Button>
        <input
          ref={attachmentInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(event) => {
            void handleAttachmentFiles(event.target.files);
          }}
        />
      </div>
      {attachments.length === 0 ? (
        <p className="text-sm text-muted-foreground">No attachments yet.</p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {attachments.map((attachment) => {
            const href =
              attachment.url ||
              `/api/tasks/${attachment.task_id}/attachments/${attachment.id}`;
            const isImage = attachment.kind === "image";
            return (
              <div
                key={attachment.id}
                className="flex min-w-0 items-center gap-3 rounded-lg border p-2"
              >
                <div className="flex size-12 shrink-0 items-center justify-center overflow-hidden rounded-md bg-muted">
                  {isImage ? (
                    <img
                      src={href}
                      alt=""
                      className="size-full object-cover"
                    />
                  ) : (
                    <FileText className="size-5 text-muted-foreground" />
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">
                    {attachment.display_name}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {formatBytes(attachment.size_bytes)}
                  </p>
                </div>
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  onClick={() => window.open(href, "_blank", "noreferrer")}
                >
                  {isImage ? (
                    <ImageIcon className="size-4" />
                  ) : (
                    <ExternalLink className="size-4" />
                  )}
                </Button>
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  onClick={() => {
                    void handleDeleteAttachment(attachment.id);
                  }}
                >
                  <Trash2 className="size-4" />
                </Button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
