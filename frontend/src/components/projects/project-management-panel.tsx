"use client";

import {
  AlertTriangle,
  ChevronLeft,
  FileText,
  Folder,
  FolderOpen,
  Link2,
  Loader2,
  RefreshCw,
  Settings2,
  Unlink,
  Upload,
  UploadCloud,
  X,
} from "lucide-react";
import {
  useCallback,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from "react";
import type { ProjectManagementFilesController } from "@/components/projects/hooks/use-project-management-files";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { formatBytes } from "@/lib/utils";

type ManagementDocumentCardProps = {
  title: string;
  description: string;
  value?: string | null;
  values?: string[];
  accept?: string;
  multiple?: boolean;
  uploading?: boolean;
  onFiles: (files: File[]) => void;
  onPickFromFiler: () => void;
  onClear?: () => void;
  onRemove?: (path: string) => void;
};

function fileNameFromPath(filePath: string): string {
  const normalized = filePath.replace(/\\/g, "/");
  return normalized.split("/").filter(Boolean).at(-1) || normalized;
}

function folderFromPath(filePath: string): string {
  const normalized = filePath.replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/") || "Project Files直下";
}

function acceptMatchesPath(filePath: string, accept?: string): boolean {
  if (!accept) return true;
  const extension =
    `.${fileNameFromPath(filePath).split(".").pop() || ""}`.toLowerCase();
  return accept
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
    .some((item) => item === extension || item === "*/*");
}

function ManagementDocumentCard({
  title,
  description,
  value,
  values,
  accept,
  multiple = false,
  uploading = false,
  onFiles,
  onPickFromFiler,
  onClear,
  onRemove,
}: ManagementDocumentCardProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const registeredFiles = multiple ? values || [] : value ? [value] : [];

  const submitFiles = useCallback(
    (files: FileList | File[]) => {
      const list = Array.from(files);
      if (list.length > 0) onFiles(list);
    },
    [onFiles],
  );

  const handleDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      event.stopPropagation();
      setDragging(false);
      submitFiles(event.dataTransfer.files);
    },
    [submitFiles],
  );

  const handleSelect = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      if (event.target.files) submitFiles(event.target.files);
      event.target.value = "";
    },
    [submitFiles],
  );

  return (
    <div
      className={`rounded-md border p-4 transition-colors ${
        dragging ? "border-primary bg-primary/5" : "border-border bg-card"
      }`}
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <FileText className="size-4 text-muted-foreground" />
            <h3 className="text-sm font-medium">{title}</h3>
            {registeredFiles.length > 0 && (
              <Badge variant="secondary" className="text-[11px]">
                {multiple ? `${registeredFiles.length}件` : "登録済み"}
              </Badge>
            )}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">{description}</p>
        </div>
      </div>

      {registeredFiles.length > 0 ? (
        <div className="mt-3 space-y-2">
          {registeredFiles.map((filePath) => (
            <div
              key={filePath}
              className="flex items-center justify-between gap-3 rounded-md border bg-muted/30 px-3 py-2"
            >
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">
                  {fileNameFromPath(filePath)}
                </div>
                <div className="truncate text-[11px] text-muted-foreground">
                  {folderFromPath(filePath)}
                </div>
              </div>
              {multiple && onRemove ? (
                <Button
                  type="button"
                  size="icon-sm"
                  variant="ghost"
                  onClick={() => onRemove(filePath)}
                  aria-label={`${fileNameFromPath(filePath)} の登録を解除`}
                  title={`${fileNameFromPath(filePath)} の登録を解除`}
                >
                  <X className="size-3.5" />
                </Button>
              ) : null}
            </div>
          ))}
        </div>
      ) : (
        <button
          type="button"
          className="mt-3 flex min-h-24 w-full flex-col items-center justify-center rounded-md border border-dashed bg-muted/20 px-3 py-4 text-center transition-colors hover:border-primary hover:bg-primary/5"
          onClick={() => inputRef.current?.click()}
          disabled={uploading}
        >
          {uploading ? (
            <Loader2 className="mb-2 size-5 animate-spin text-muted-foreground" />
          ) : (
            <UploadCloud className="mb-2 size-5 text-muted-foreground" />
          )}
          <span className="text-sm font-medium">
            ファイルをドロップ、またはアップロード
          </span>
          <span className="mt-1 text-xs text-muted-foreground">
            ローカルパスは保存しません
          </span>
        </button>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => inputRef.current?.click()}
          disabled={uploading}
        >
          {uploading ? (
            <Loader2 className="mr-1 size-3 animate-spin" />
          ) : (
            <Upload className="mr-1 size-3" />
          )}
          アップロード
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onPickFromFiler}
          disabled={uploading}
        >
          <Link2 className="mr-1 size-3" />
          Project Filesから選択
        </Button>
        {registeredFiles.length > 0 && !multiple && onClear ? (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={onClear}
            disabled={uploading}
          >
            <Unlink className="mr-1 size-3" />
            解除
          </Button>
        ) : null}
      </div>
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        accept={accept}
        multiple={multiple}
        onChange={handleSelect}
      />
    </div>
  );
}

export function ProjectManagementPanel({
  projectName,
  controller,
}: {
  projectName: string;
  controller: ProjectManagementFilesController;
}) {
  const {
    wbsFile,
    issueFile,
    riskFile,
    requestFiles,
    wbsScan,
    managementLoading,
    managementSaving,
    managementUploading,
    managementError,
    syncResult,
    uploadResult,
    filePicker,
    filePickerData,
    filePickerLoading,
    filePickerError,
    refreshManagement,
    clearManagementFile,
    openFilePicker,
    closeFilePicker,
    setFilePickerPath,
    selectExistingManagementFile,
    uploadManagementFiles,
  } = controller;

  return (
    <div className="flex-1 space-y-5 overflow-auto pb-8">
      <section className="space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border pb-4">
          <div>
            <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
              Project configuration
            </p>
            <div className="mt-1 flex items-center gap-2 text-base font-semibold">
              <Settings2 className="size-4" />
              Management documents
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              Configure WBS, issue, risk, and supporting material for{" "}
              {projectName}.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              className="h-8"
              onClick={() => void refreshManagement()}
              disabled={managementLoading}
            >
              <RefreshCw
                className={`mr-1 size-3.5 ${managementLoading ? "animate-spin" : ""}`}
              />
              Reload
            </Button>
          </div>
        </div>
        <div className="space-y-4">
          <div className="grid gap-3 xl:grid-cols-2">
            <div className="rounded-md border border-border bg-card p-3 text-xs text-muted-foreground xl:col-span-2">
              案件資料はProject
              Filesで管理します。新規アップロードするか、既にProject
              Filesにあるファイルを選択してください。ローカルPCの絶対パスは保存しません。
            </div>
            <ManagementDocumentCard
              title="WBS"
              description="ExcelのWBSを登録します。アップロードするとProject Filesの management/ に保存されます。"
              value={wbsFile}
              accept=".xlsx,.xlsm,.xls"
              uploading={managementUploading === "wbs" || managementSaving}
              onFiles={(files) => void uploadManagementFiles("wbs", files)}
              onPickFromFiler={() =>
                openFilePicker("wbs", "WBSを選択", ".xlsx,.xlsm,.xls")
              }
              onClear={() => void clearManagementFile("wbs")}
            />
            <ManagementDocumentCard
              title="課題管理表"
              description="課題一覧として扱うExcel/CSVを登録します。"
              value={issueFile}
              accept=".xlsx,.xlsm,.xls,.csv,.tsv"
              uploading={managementUploading === "issue" || managementSaving}
              onFiles={(files) => void uploadManagementFiles("issue", files)}
              onPickFromFiler={() =>
                openFilePicker(
                  "issue",
                  "課題管理表を選択",
                  ".xlsx,.xlsm,.xls,.csv,.tsv",
                )
              }
              onClear={() => void clearManagementFile("issue")}
            />
            <ManagementDocumentCard
              title="リスク管理表"
              description="リスク一覧として扱うExcel/CSVを登録します。"
              value={riskFile}
              accept=".xlsx,.xlsm,.xls,.csv,.tsv"
              uploading={managementUploading === "risk" || managementSaving}
              onFiles={(files) => void uploadManagementFiles("risk", files)}
              onPickFromFiler={() =>
                openFilePicker(
                  "risk",
                  "リスク管理表を選択",
                  ".xlsx,.xlsm,.xls,.csv,.tsv",
                )
              }
              onClear={() => void clearManagementFile("risk")}
            />
            <ManagementDocumentCard
              title="補助資料・議事録"
              description="確認事項、議事録、補足資料を複数登録できます。"
              values={requestFiles}
              accept=".md,.txt,.csv,.tsv,.xlsx,.xlsm,.xls,.docx,.pdf"
              multiple
              uploading={managementUploading === "request" || managementSaving}
              onFiles={(files) => void uploadManagementFiles("request", files)}
              onPickFromFiler={() =>
                openFilePicker(
                  "request",
                  "補助資料・議事録を選択",
                  ".md,.txt,.csv,.tsv,.xlsx,.xlsm,.xls,.docx,.pdf",
                )
              }
              onRemove={(path) => void clearManagementFile("request", path)}
            />
          </div>
          {managementError && (
            <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 p-2 text-xs text-destructive">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              <span>{managementError}</span>
            </div>
          )}
          {syncResult && (
            <p className="text-xs text-muted-foreground">{syncResult}</p>
          )}
          {uploadResult && (
            <div className="space-y-1 text-xs" role="status">
              {uploadResult.succeeded.length > 0 && (
                <p className="text-muted-foreground">
                  成功: {uploadResult.succeeded.map((item) => item.name).join(", ")}
                </p>
              )}
              {uploadResult.failed.length > 0 && (
                <ul className="space-y-1 text-destructive">
                  {uploadResult.failed.map((item) => (
                    <li key={`${uploadResult.kind}:${item.name}`}>
                      {item.name}: {item.error}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      </section>

      <Dialog
        open={!!filePicker}
        onOpenChange={(open) => {
          if (!open) closeFilePicker();
        }}
      >
        <DialogContent size="2xl">
          <DialogHeader>
            <DialogTitle>
              {filePicker?.title || "Project Filesから選択"}
            </DialogTitle>
            <DialogDescription>
              Project Files内のファイルを案件資料として登録します。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <FolderOpen className="size-3.5" />
              <span className="truncate">
                {filePickerData?.currentPath || "Project Files直下"}
              </span>
            </div>
            <div className="max-h-[420px] overflow-auto rounded-md border">
              {filePickerLoading ? (
                <div className="flex items-center justify-center gap-2 p-8 text-sm text-muted-foreground">
                  <Loader2 className="size-4 animate-spin" />
                  読み込み中
                </div>
              ) : filePickerError ? (
                <div className="p-4 text-sm text-destructive">
                  {filePickerError}
                </div>
              ) : (
                <div className="divide-y">
                  {filePickerData && filePickerData.parentPath !== null && (
                    <button
                      type="button"
                      className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-muted"
                      onClick={() =>
                        setFilePickerPath(filePickerData.parentPath || "")
                      }
                    >
                      <ChevronLeft className="size-4 text-muted-foreground" />
                      上のフォルダ
                    </button>
                  )}
                  {filePickerData?.directories.map((directory) => (
                    <button
                      key={directory.path}
                      type="button"
                      className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-muted"
                      onClick={() => setFilePickerPath(directory.path)}
                    >
                      <Folder className="size-4 text-muted-foreground" />
                      <span className="min-w-0 truncate">{directory.name}</span>
                    </button>
                  ))}
                  {filePickerData?.files
                    .filter((file) =>
                      acceptMatchesPath(file.path, filePicker?.accept),
                    )
                    .map((file) => (
                      <button
                        key={file.path}
                        type="button"
                        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-muted"
                        onClick={() => {
                          if (!filePicker) return;
                          void selectExistingManagementFile(
                            filePicker.kind,
                            file.path,
                          );
                        }}
                      >
                        <span className="flex min-w-0 items-center gap-2">
                          <FileText className="size-4 shrink-0 text-muted-foreground" />
                          <span className="min-w-0">
                            <span className="block truncate text-sm">
                              {file.name}
                            </span>
                            <span className="block truncate text-[11px] text-muted-foreground">
                              {folderFromPath(file.path)}
                            </span>
                          </span>
                        </span>
                        <span className="shrink-0 text-[11px] text-muted-foreground">
                          {formatBytes(file.size)}
                        </span>
                      </button>
                    ))}
                  {filePickerData &&
                    filePickerData.directories.length === 0 &&
                    filePickerData.files.filter((file) =>
                      acceptMatchesPath(file.path, filePicker?.accept),
                    ).length === 0 && (
                      <div className="p-4 text-sm text-muted-foreground">
                        選択できるファイルがありません
                      </div>
                    )}
                </div>
              )}
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">WBS状況</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {wbsScan ? (
              <>
                <div className="grid grid-cols-4 gap-2 text-center">
                  <div className="rounded-md border p-2">
                    <div className="text-lg font-semibold">
                      {wbsScan.summary.total}
                    </div>
                    <div className="text-[11px] text-muted-foreground">
                      総数
                    </div>
                  </div>
                  <div className="rounded-md border p-2">
                    <div className="text-lg font-semibold">
                      {wbsScan.summary.open}
                    </div>
                    <div className="text-[11px] text-muted-foreground">
                      未完了
                    </div>
                  </div>
                  <div className="rounded-md border p-2">
                    <div className="text-lg font-semibold">
                      {wbsScan.summary.review}
                    </div>
                    <div className="text-[11px] text-muted-foreground">
                      確認待ち
                    </div>
                  </div>
                  <div className="rounded-md border p-2">
                    <div className="text-lg font-semibold">
                      {wbsScan.summary.overdue}
                    </div>
                    <div className="text-[11px] text-muted-foreground">
                      超過
                    </div>
                  </div>
                </div>
                {wbsScan.errors.length > 0 && (
                  <div className="rounded-md border p-2 text-xs text-muted-foreground">
                    {wbsScan.errors.join(" / ")}
                  </div>
                )}
                <div className="space-y-2">
                  {wbsScan.upcoming.length > 0 ? (
                    wbsScan.upcoming.slice(0, 8).map((row) => (
                      <div
                        key={`${row.sheetName}-${row.rowNumber}`}
                        className="rounded-md border p-2"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0">
                            <p className="truncate text-sm font-medium">
                              {row.title}
                            </p>
                            <p className="text-xs text-muted-foreground">
                              {row.wbsId || `${row.sheetName}:${row.rowNumber}`}
                              {row.assignee ? ` / ${row.assignee}` : ""}
                            </p>
                          </div>
                          <Badge
                            variant={
                              row.priority === "urgent"
                                ? "destructive"
                                : "secondary"
                            }
                          >
                            {row.plannedEnd || "期限なし"}
                          </Badge>
                        </div>
                      </div>
                    ))
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      直近のWBSタスクはありません
                    </p>
                  )}
                </div>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                管理資料設定を読み込んでください
              </p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm">要依頼・要確認事項</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {wbsScan?.requests && wbsScan.requests.length > 0 ? (
              wbsScan.requests.slice(0, 10).map((item) => (
                <div
                  key={`${item.sourcePath}-${item.sourceRef}-${item.title}`}
                  className="rounded-md border p-2"
                >
                  <div className="flex items-start justify-between gap-2">
                    <p className="text-sm font-medium">{item.title}</p>
                    <Badge variant="outline">{item.target}</Badge>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {item.reason}
                  </p>
                  <p className="mt-1 truncate text-[11px] text-muted-foreground">
                    {item.sourceType}: {item.sourceRef}
                    {item.dueAt ? ` / ${item.dueAt}` : ""}
                  </p>
                </div>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">
                要依頼事項は検出されていません
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
