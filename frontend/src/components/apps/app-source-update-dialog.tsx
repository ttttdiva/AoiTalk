"use client";

import { useRef, useState } from "react";
import { toast } from "sonner";
import { CheckCircle2, FileArchive, Loader2, Upload, X } from "lucide-react";
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
import { getDroppedExplorerFiles } from "@/lib/file-drop";
import {
  appsApi,
  AppsApiError,
  type AppGitStatus,
  type AppSourceImportFile,
  type AppSourceImportFileChange,
  type AppSourceImportPreview,
} from "@/lib/apps-api";

interface AppSourceUpdateDialogProps {
  appId: string;
  projectId?: string;
  status?: AppGitStatus;
  canEdit: boolean;
  onApplied: () => Promise<unknown> | unknown;
}

function fileKey(file: File, relativePath: string): string {
  return `${relativePath}:${file.size}:${file.lastModified}`;
}

function changeEntries(preview: AppSourceImportPreview): AppSourceImportFileChange[] {
  if (Array.isArray(preview.files)) return preview.files as AppSourceImportFileChange[];
  return Object.values(preview.changes || {}).flatMap((items) => items || []);
}

function actionLabel(action: string): string {
  return { add: "追加", modify: "更新", modified: "更新", delete: "削除", unchanged: "変更なし" }[action] || action;
}

export function AppSourceUpdateDialog({ appId, projectId, status, canEdit, onApplied }: AppSourceUpdateDialogProps) {
  const [open, setOpen] = useState(false);
  const [files, setFiles] = useState<AppSourceImportFile[]>([]);
  const [preview, setPreview] = useState<AppSourceImportPreview | null>(null);
  const [deletePaths, setDeletePaths] = useState<string[]>([]);
  const [syncDeletes, setSyncDeletes] = useState(false);
  const [busy, setBusy] = useState<"preview" | "apply" | null>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const reset = () => {
    setFiles([]);
    setPreview(null);
    setDeletePaths([]);
    setSyncDeletes(false);
    setError(null);
    setBusy(null);
  };

  const acceptFiles = (items: AppSourceImportFile[]) => {
    const unique = new Map<string, AppSourceImportFile>();
    for (const item of items) unique.set(fileKey(item.file, item.relativePath), item);
    setFiles(Array.from(unique.values()));
    setPreview(null);
    setError(null);
  };

  const handleDrop = async (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    try {
      acceptFiles((await getDroppedExplorerFiles(event.dataTransfer)).map(({ file, relativePath }) => ({
        file,
        relativePath: relativePath || file.name,
      })));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "ドロップしたソースを読み込めませんでした");
    }
  };

  const handleFileInput = (event: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(event.target.files || []).map((file) => ({
      file,
      relativePath: file.webkitRelativePath || file.name,
    }));
    acceptFiles(selected);
    event.target.value = "";
  };

  const previewImport = async () => {
    if (!files.length) {
      setError("更新するフォルダ、ZIP、またはファイルをドロップしてください");
      return;
    }
    if (!status?.revision) {
      setError("AppのGit revisionを取得できないため更新できません");
      return;
    }
    setBusy("preview");
    setError(null);
    try {
      const result = await appsApi.previewSourceImport(
        appId,
        { files, expected_revision: status.revision, root_mode: "strip_common" },
        projectId,
      );
      setPreview(result);
      const incoming = new Set(changeEntries(result).map((entry) => String(entry.path || "").replaceAll("\\", "/").toLowerCase()));
      const protectedNames = new Set(["aoitalk.app.yaml", "readme.md", "device_list.csv"]);
      const ignoredDirectories = new Set([".agents", ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", "cache", "logs", "runtime", "secrets"]);
      const deletions = (result.current_files || []).filter((path) => {
        const normalized = String(path || "").replaceAll("\\", "/");
        const parts = normalized.toLowerCase().split("/");
        return Boolean(normalized) && !incoming.has(normalized.toLowerCase()) && !protectedNames.has(parts.at(-1) || "") && !parts.some((part) => ignoredDirectories.has(part));
      });
      setDeletePaths(deletions);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "更新候補を確認できませんでした");
    } finally {
      setBusy(null);
    }
  };

  const applyImport = async () => {
    if (!preview || !status?.revision) return;
    const rejected = Array.isArray(preview.rejected) ? preview.rejected : [];
    if (rejected.length) {
      setError("取り込み対象から拒否ファイルを外してから適用してください");
      return;
    }
    setBusy("apply");
    setError(null);
    try {
      await appsApi.applySourceImport(
        appId,
        preview.import_id,
        { expected_revision: status.revision, delete_paths: syncDeletes ? deletePaths : [] },
        projectId,
      );
      toast.success("App workspaceを更新し、Git checkpointを作成しました");
      setOpen(false);
      reset();
      await onApplied();
    } catch (caught) {
      const message = caught instanceof AppsApiError && caught.status === 409
        ? `${caught.message}（最新の状態を再読込してから、もう一度確認してください）`
        : caught instanceof Error ? caught.message : "App workspaceを更新できませんでした";
      setError(message);
    } finally {
      setBusy(null);
    }
  };

  const entries = preview ? changeEntries(preview) : [];
  const changedEntries = entries.filter((entry) => entry.action !== "unchanged");
  const rejected = Array.isArray(preview?.rejected) ? preview.rejected : [];
  const summary = preview?.summary || {};
  const removeRejectedFile = (path: string) => {
    const normalized = path.replaceAll("\\", "/").toLowerCase();
    setFiles((current) => current.filter((item) => item.relativePath.replaceAll("\\", "/").toLowerCase() !== normalized));
    setPreview(null);
    setError(null);
  };

  return (
    <Dialog open={open} onOpenChange={(next) => { setOpen(next); if (!next) reset(); }}>
      <Button type="button" size="sm" variant="outline" onClick={() => setOpen(true)} disabled={!canEdit}>
        <Upload className="size-3.5" /> ソースを更新
      </Button>
      <DialogContent size="2xl" className="max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Appのソースを更新</DialogTitle>
          <DialogDescription>
            外部フォルダやZIPを一時的に読み込み、差分を確認してからApp workspaceへ反映します。元フォルダの場所は保存しません。
          </DialogDescription>
        </DialogHeader>

        <div
          className={`rounded-xl border-2 border-dashed p-7 text-center transition-colors ${dragging ? "border-primary bg-primary/10" : "border-border bg-muted/20"}`}
          onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => { event.preventDefault(); setDragging(false); }}
          onDrop={(event) => void handleDrop(event)}
        >
          <Upload className="mx-auto size-8 text-muted-foreground" />
          <p className="mt-2 text-sm font-medium">更新元のフォルダ、ZIP、ファイルをここへドロップ</p>
          <p className="mt-1 text-xs text-muted-foreground">フォルダの相対構造を保ったまま、同じAppのworkspaceへ追加・更新します。</p>
          <input ref={fileInputRef} type="file" multiple className="hidden" onChange={handleFileInput} />
          <Button type="button" variant="outline" size="sm" className="mt-4" onClick={() => fileInputRef.current?.click()}>
            <FileArchive className="size-3.5" /> ファイルを選択
          </Button>
        </div>

        {files.length > 0 && (
          <div className="rounded-lg border p-3">
            <div className="flex items-center justify-between gap-2">
              <p className="text-sm font-medium">選択中: {files.length}件</p>
              <Button type="button" size="icon-sm" variant="ghost" onClick={() => reset()} aria-label="選択をクリア"><X className="size-4" /></Button>
            </div>
            <div className="mt-2 max-h-28 space-y-1 overflow-y-auto text-xs text-muted-foreground">
              {files.slice(0, 80).map(({ file, relativePath }) => <div key={fileKey(file, relativePath)} className="truncate">{relativePath}</div>)}
              {files.length > 80 && <div>…ほか {files.length - 80}件</div>}
            </div>
            <Button type="button" className="mt-3" size="sm" onClick={() => void previewImport()} disabled={busy !== null || !status?.revision}>
              {busy === "preview" && <Loader2 className="size-3.5 animate-spin" />} 差分を確認
            </Button>
          </div>
        )}

        {preview && (
          <div className="space-y-3 rounded-lg border p-3">
            <div className="flex flex-wrap items-center gap-2">
              <CheckCircle2 className="size-4 text-emerald-500" />
              <p className="text-sm font-medium">適用前の差分</p>
              <Badge variant="outline">追加 {summary.added ?? changedEntries.filter((item) => item.action === "add").length}</Badge>
              <Badge variant="outline">更新 {summary.modified ?? changedEntries.filter((item) => item.action === "modify").length}</Badge>
              <Badge variant="secondary">変更なし {summary.unchanged ?? entries.filter((item) => item.action === "unchanged").length}</Badge>
            </div>
            <div className="max-h-48 space-y-1 overflow-y-auto text-xs">
              {changedEntries.length ? changedEntries.map((entry) => <div key={entry.path} className="flex items-center justify-between gap-3 rounded bg-muted/40 px-2 py-1.5"><span className="min-w-0 truncate">{entry.path}</span><Badge variant="outline">{actionLabel(String(entry.action || "変更"))}</Badge></div>) : <p className="text-muted-foreground">変更されるファイルはありません。</p>}
            </div>
            {deletePaths.length > 0 && <div className="rounded-md border border-amber-500/40 bg-amber-500/5 p-2 text-xs"><div className="flex items-start gap-2"><Checkbox id="app-source-sync-deletes" checked={syncDeletes} onCheckedChange={(checked) => setSyncDeletes(checked === true)} className="mt-0.5" aria-label="D&D元に存在しないファイルも削除する" /><label htmlFor="app-source-sync-deletes"><span className="font-medium">D&D元に存在しないファイルも削除する</span><span className="mt-1 block text-muted-foreground">{deletePaths.length}件。App workspace側のファイルを削除するため、必要な場合だけ選択してください。</span></label></div>{syncDeletes && <div className="mt-2 max-h-24 space-y-1 overflow-y-auto text-muted-foreground">{deletePaths.map((path) => <div key={path} className="truncate">削除: {path}</div>)}</div>}</div>}
            {rejected.length > 0 && <div className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive"><p className="font-medium">取り込めないファイル</p>{rejected.map((item) => { const path = String(item.path || ""); return <div key={path} className="mt-1 flex items-start justify-between gap-2"><span className="min-w-0 break-all">{path}: {String(item.reason || item.rejection_reason || "保護対象")}</span><Button type="button" size="icon-sm" variant="ghost" className="-my-1 shrink-0 text-destructive hover:text-destructive" onClick={() => removeRejectedFile(path)} aria-label={`${path}を取り込み対象から外す`}><X className="size-3.5" /></Button></div>; })}<p className="mt-2 text-[11px]">×で対象から外してから、もう一度「差分を確認」を押してください。</p></div>}
          </div>
        )}

        {error && <p className="rounded-md bg-destructive/10 p-2 text-xs text-destructive">{error}</p>}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => setOpen(false)}>キャンセル</Button>
          <Button type="button" onClick={() => void applyImport()} disabled={!preview || rejected.length > 0 || busy !== null || (changedEntries.length === 0 && !(syncDeletes && deletePaths.length > 0))}>
            {busy === "apply" && <Loader2 className="size-3.5 animate-spin" />} 更新を適用
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
