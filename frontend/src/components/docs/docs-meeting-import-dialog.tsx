"use client";

import { useEffect, useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { FileText, Loader2, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  filterMeetingImportFiles,
  isMeetingImportFile,
  submitMeetingImport,
  type MeetingImportApiFetch,
  type MeetingImportResponse,
  type MeetingImportRole,
  type MeetingImportSubmission,
} from "./docs-meeting-import";

function parseParticipants(value: string): string[] {
  const trimmed = value.trim();
  if (!trimmed) return [];
  if (trimmed.startsWith("[")) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (Array.isArray(parsed)) {
        return parsed.filter((item): item is string => typeof item === "string");
      }
    } catch {
      // Fall through to the convenient line/comma-separated form.
    }
  }
  return trimmed
    .split(/[\n,]/u)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function DocsMeetingImportDialog({
  open,
  initialFile,
  onOpenChange,
  onImported,
  fetcher,
}: {
  open: boolean;
  initialFile?: File | null;
  onOpenChange: (open: boolean) => void;
  onImported?: (
    result: MeetingImportResponse,
    submission: MeetingImportSubmission,
  ) => void | Promise<void>;
  fetcher?: MeetingImportApiFetch;
}) {
  const [file, setFile] = useState<File | null>(initialFile ?? null);
  const [role, setRole] = useState<MeetingImportRole>("minutes");
  const [title, setTitle] = useState("");
  const [meetingDate, setMeetingDate] = useState("");
  const [participants, setParticipants] = useState("");
  const [dragActive, setDragActive] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) {
      setFile(null);
      setRole("minutes");
      setTitle("");
      setMeetingDate("");
      setParticipants("");
      setDragActive(false);
      setSubmitting(false);
      setError("");
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    if (initialFile) setFile(initialFile);
  }, [initialFile, open]);

  const chooseFile = (candidate: File | undefined) => {
    if (!candidate) return;
    if (!isMeetingImportFile(candidate)) {
      setFile(null);
      setError(".md または .txt ファイルを指定してください");
      return;
    }
    setError("");
    setFile(candidate);
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    chooseFile(event.target.files?.[0]);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    setDragActive(false);
    const files = filterMeetingImportFiles(Array.from(event.dataTransfer.files ?? []));
    if (files.length !== 1) {
      setError(files.length > 1 ? "一度に1ファイルだけ指定してください" : ".md または .txt ファイルを指定してください");
      return;
    }
    chooseFile(files[0]);
  };

  const handleSubmit = async () => {
    if (!file) {
      setError("ファイルを指定してください");
      return;
    }
    setSubmitting(true);
    setError("");
    const submission: MeetingImportSubmission = {
      file,
      role,
      title: title.trim() || undefined,
      meetingDate: meetingDate.trim() || undefined,
      participants: parseParticipants(participants),
    };
    try {
      const result = await submitMeetingImport(submission, fetcher);
      await onImported?.(result, submission);
      onOpenChange(false);
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "議事録の取り込みに失敗しました");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg" data-testid="docs-meeting-import-dialog">
        <DialogHeader>
          <DialogTitle>議事録をDocsへ取り込む</DialogTitle>
          <DialogDescription>
            完了済みの .md / .txt をそのまま保存します。LLMやWeb検索は実行しません。
          </DialogDescription>
        </DialogHeader>
        <div
          className={`grid gap-4 rounded-md border border-dashed p-4 ${dragActive ? "border-primary bg-primary/5" : "border-border"}`}
          data-testid="docs-meeting-import-file-dropzone"
          onDragEnter={(event) => {
            event.preventDefault();
            setDragActive(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            event.preventDefault();
            setDragActive(false);
          }}
          onDrop={handleDrop}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept=".md,.txt,text/markdown,text/plain"
            className="sr-only"
            data-testid="docs-meeting-import-file-input"
            onChange={handleFileChange}
          />
          <Button type="button" variant="outline" onClick={() => fileInputRef.current?.click()}>
            <Upload className="mr-2 size-4" />
            ファイルを選択
          </Button>
          <div className="text-xs text-muted-foreground">ここへ .md / .txt をドロップ</div>
          {file ? (
            <div className="flex items-center gap-2 text-sm" data-testid="docs-meeting-import-selected-file">
              <FileText className="size-4 shrink-0 text-primary" />
              <span className="truncate">{file.name}</span>
            </div>
          ) : null}
        </div>

        <fieldset className="grid gap-2">
          <legend className="text-sm font-medium">文書種別</legend>
          <div className="flex gap-4 text-sm">
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="meeting-import-role"
                value="minutes"
                checked={role === "minutes"}
                onChange={() => setRole("minutes")}
              />
              議事録
            </label>
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="meeting-import-role"
                value="memo"
                checked={role === "memo"}
                onChange={() => setRole("memo")}
              />
              議事メモ
            </label>
          </div>
        </fieldset>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="grid gap-1 text-sm">
            タイトル（任意）
            <Input value={title} maxLength={240} onChange={(event) => setTitle(event.target.value)} />
          </label>
          <label className="grid gap-1 text-sm">
            会議日（任意）
            <Input type="date" value={meetingDate} onChange={(event) => setMeetingDate(event.target.value)} />
          </label>
        </div>
        <label className="grid gap-1 text-sm">
          参加者（任意、1人1行またはカンマ区切り）
          <Textarea value={participants} rows={3} onChange={(event) => setParticipants(event.target.value)} />
        </label>
        {error ? <p className="text-sm text-destructive" role="alert">{error}</p> : null}
        <DialogFooter>
          <Button type="button" variant="ghost" disabled={submitting} onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button type="button" disabled={submitting || !file} data-testid="docs-meeting-import-submit" onClick={() => void handleSubmit()}>
            {submitting ? <Loader2 className="mr-2 size-4 animate-spin" /> : null}
            {submitting ? "取り込み中…" : "Docsへ取り込む"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
