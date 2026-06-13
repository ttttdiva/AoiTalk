"use client";

import { useEffect, useState } from "react";
import { ExternalLink, Loader2 } from "lucide-react";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import {
  TASK_STATUS_LABELS,
  TASK_STATUS_OPTIONS,
} from "@/lib/task-status";
import {
  addRemoteTaskComment,
  patchRemoteTask,
} from "@/lib/remote-tasks";

export type RemoteTaskDialogTarget = {
  profileId: string;
  profileName: string;
  profileColor?: string | null;
  baseUrl: string;
  taskId: string;
  title: string;
  status: string;
  startAt?: string | null;
  endAt?: string | null;
};

/** "2026-06-12T10:00:00" 形式を datetime-local 入力用に整形する。 */
function toLocalInput(value?: string | null): string {
  if (!value) return "";
  // 既にローカル形式ならそのまま、ISO(Z付き)なら秒以下を落とす。
  const trimmed = value.replace("Z", "");
  return trimmed.length >= 16 ? trimmed.slice(0, 16) : trimmed;
}

export function RemoteTaskDialog({
  target,
  onClose,
  onUpdated,
}: {
  target: RemoteTaskDialogTarget | null;
  onClose: () => void;
  onUpdated?: () => void;
}) {
  const [status, setStatus] = useState("open");
  const [startAt, setStartAt] = useState("");
  const [endAt, setEndAt] = useState("");
  const [comment, setComment] = useState("");
  const [savingField, setSavingField] = useState<string | null>(null);

  useEffect(() => {
    if (!target) return;
    setStatus(target.status || "open");
    setStartAt(toLocalInput(target.startAt));
    setEndAt(toLocalInput(target.endAt));
    setComment("");
  }, [target]);

  if (!target) return null;

  const externalUrl = `${target.baseUrl.replace(/\/$/, "")}/tasks`;

  const handleStatusSave = async (next: string) => {
    setStatus(next);
    setSavingField("status");
    try {
      await patchRemoteTask(target.profileId, target.taskId, { status: next });
      toast.success("ステータスを更新しました");
      onUpdated?.();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新に失敗しました");
    } finally {
      setSavingField(null);
    }
  };

  const handleDatesSave = async () => {
    setSavingField("dates");
    try {
      await patchRemoteTask(target.profileId, target.taskId, {
        start_at: startAt || null,
        end_at: endAt || null,
      });
      toast.success("日付を更新しました");
      onUpdated?.();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新に失敗しました");
    } finally {
      setSavingField(null);
    }
  };

  const handleCommentSave = async () => {
    if (!comment.trim()) return;
    setSavingField("comment");
    try {
      await addRemoteTaskComment(
        target.profileId,
        target.taskId,
        comment.trim(),
      );
      setComment("");
      toast.success("コメントを追加しました");
      onUpdated?.();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "追加に失敗しました");
    } finally {
      setSavingField(null);
    }
  };

  return (
    <Dialog open={!!target} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <span
              className="size-3 shrink-0 rounded-full"
              style={{ backgroundColor: target.profileColor || "#3b82f6" }}
            />
            <span className="truncate">{target.title}</span>
          </DialogTitle>
          <div className="flex items-center gap-2">
            <Badge variant="secondary">{target.profileName}</Badge>
            <span className="text-xs text-muted-foreground">
              外部サーバーのタスク
            </span>
          </div>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1">
            <Label className="text-xs">ステータス</Label>
            <Select
              value={status}
              onValueChange={(v) => v && handleStatusSave(v)}
            >
              <SelectTrigger className="w-full">
                <span>{TASK_STATUS_LABELS[status] ?? status}</span>
                {savingField === "status" ? (
                  <Loader2 className="ml-2 size-3 animate-spin" />
                ) : null}
              </SelectTrigger>
              <SelectContent>
                {TASK_STATUS_OPTIONS.map((opt) => (
                  <SelectItem key={opt} value={opt}>
                    {TASK_STATUS_LABELS[opt] ?? opt}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label className="text-xs">日付</Label>
            <div className="flex flex-col gap-2">
              <Input
                type="datetime-local"
                value={startAt}
                onChange={(e) => setStartAt(e.target.value)}
                className="h-8"
              />
              <Input
                type="datetime-local"
                value={endAt}
                onChange={(e) => setEndAt(e.target.value)}
                className="h-8"
              />
              <Button
                type="button"
                size="sm"
                onClick={handleDatesSave}
                disabled={savingField === "dates"}
              >
                {savingField === "dates" ? (
                  <Loader2 className="mr-1 size-3 animate-spin" />
                ) : null}
                日付を保存
              </Button>
            </div>
          </div>

          <div className="space-y-2">
            <Label className="text-xs">コメント追加</Label>
            <Textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="コメントを入力..."
              rows={2}
            />
            <Button
              type="button"
              size="sm"
              variant="secondary"
              onClick={handleCommentSave}
              disabled={savingField === "comment" || !comment.trim()}
            >
              {savingField === "comment" ? (
                <Loader2 className="mr-1 size-3 animate-spin" />
              ) : null}
              コメントを追加
            </Button>
          </div>

          <div className="border-t pt-3">
            <a
              href={externalUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            >
              <ExternalLink className="size-3" />
              詳細な編集は外部サーバーのWeb画面で開く
            </a>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
