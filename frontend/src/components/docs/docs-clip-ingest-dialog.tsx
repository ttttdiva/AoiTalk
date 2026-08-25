"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type DragEvent,
} from "react";
import { Check, ChevronsUpDown, FileText, Image as ImageIcon, Loader2, Upload, X } from "lucide-react";
import Image from "next/image";
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
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  formatClipIngestBreadcrumb,
  isAllowedClipIngestTarget,
} from "@/lib/clip-ingest-settings";
import { formatBytes } from "@/lib/utils";
import { apiFetch } from "./docs-utils";

export type DocsClipIngestResult = {
  target_id: string;
  target_label: string;
  action: "create" | "append" | "duplicate_skip";
  changed_node_id: string | null;
  changed_node_title: string | null;
  open_node_id: string;
  open_node_title: string;
  direct_urls: string[];
  supplemental_urls: string[];
  failed_urls: Array<{ url?: string; error?: string; acquisition_status?: string }>;
  used_urls: string[];
  unconfirmed: string[];
};

export const CLIP_INGEST_ACTION_LABELS: Record<DocsClipIngestResult["action"], string> = {
  create: "新規作成",
  append: "既存ノードへ追記",
  duplicate_skip: "重複のため保存をスキップ",
};

/**
 * 取り込みに渡す値。ファイル本体は含めず、サーバーが返した短命な
 * staging IDだけを後続のjobへ渡す。これにより履歴やretryへBlobが混入しない。
 */
export type DocsClipIngestSubmission = {
  source: string;
  /** 明示指定時だけ設定する。targetNodeIdは内部呼び出し向け互換名。 */
  target_node_id?: string | null;
  targetNodeId?: string | null;
  upload_ids?: string[];
  skip_image_recognition?: boolean;
  /** 今回の取り込みでURL/Webによる追加調査を許可する。省略時はtrue。 */
  enable_external_research?: boolean;
};

type DocsClipTargetOption = {
  id: string;
  title: string;
  breadcrumb: string[];
  aliases: string[];
  system_key?: string | null;
};

type DocsClipTargetSearchResponse = { pages?: unknown };

export type DocsClipStagedUpload = {
  id: string;
  file_name?: string;
  mime_type?: string | null;
  size_bytes?: number | null;
};

type PendingClipFile = {
  file: File;
  key: string;
  previewUrl: string | null;
};

type UploadsResponse = {
  uploads?: unknown;
  items?: unknown;
  upload_ids?: unknown;
  staging_ids?: unknown;
  ids?: unknown;
};

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : [];
}

function stagedUploadId(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  for (const key of ["id", "upload_id", "staging_id", "stagingId"]) {
    if (typeof record[key] === "string" && record[key].trim()) return record[key].trim();
  }
  return null;
}

function normalizeClipTarget(value: unknown): DocsClipTargetOption | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const id = typeof record.id === "string" ? record.id.trim() : "";
  const title = typeof record.title === "string" ? record.title.trim() : "";
  if (!id || !title || !isAllowedClipIngestTarget(record)) return null;
  const breadcrumb = Array.isArray(record.breadcrumb)
    ? record.breadcrumb.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : [];
  const aliases = Array.isArray(record.aliases)
    ? record.aliases.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : [];
  return {
    id,
    title,
    breadcrumb,
    aliases,
    system_key: typeof record.system_key === "string" ? record.system_key : null,
  };
}

function normalizeClipTargetSearch(value: unknown): DocsClipTargetOption[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const pages = (value as DocsClipTargetSearchResponse).pages;
  if (!Array.isArray(pages)) return [];
  const seen = new Set<string>();
  return pages.flatMap((item) => {
    const target = normalizeClipTarget(item);
    if (!target || seen.has(target.id)) return [];
    seen.add(target.id);
    return [target];
  });
}

function normalizeStagedUploadIds(value: UploadsResponse): string[] {
  const values = [
    ...(Array.isArray(value.uploads) ? value.uploads : []),
    ...(Array.isArray(value.items) ? value.items : []),
    ...stringArray(value.upload_ids),
    ...stringArray(value.staging_ids),
    ...stringArray(value.ids),
  ];
  return Array.from(new Set(values.map(stagedUploadId).filter((item): item is string => Boolean(item))));
}

/** 同じファイルを複数回選択・dropしても一度だけstagingへ送る。 */
export function clipFileKey(file: Pick<File, "name" | "size" | "lastModified" | "type">): string {
  return [file.name, file.size, file.lastModified, file.type].join("\u0000");
}

export function dedupeClipFiles(files: File[]): File[] {
  const seen = new Set<string>();
  return files.filter((file) => {
    const key = clipFileKey(file);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function isClipImageFile(file: Pick<File, "name" | "type">): boolean {
  if (file.type.toLowerCase().startsWith("image/")) return true;
  return /\.(?:avif|bmp|gif|jpe?g|png|svg|webp)$/i.test(file.name);
}

function fileListFromDataTransfer(event: DragEvent<HTMLElement>): File[] {
  if (!event.dataTransfer || !Array.from(event.dataTransfer.types).includes("Files")) return [];
  return Array.from(event.dataTransfer.files || []);
}

function getPreviewUrl(file: File): string | null {
  if (typeof URL === "undefined" || typeof URL.createObjectURL !== "function") return null;
  return isClipImageFile(file) ? URL.createObjectURL(file) : null;
}

async function stageFiles(files: File[]): Promise<string[]> {
  if (files.length === 0) return [];
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);
  // apiFetch's empty headers object deliberately lets the browser set the
  // multipart boundary. Never serialize the File/Blob into JSON or cache it.
  const response = await apiFetch<UploadsResponse>("/api/docs/ingest/uploads", {
    method: "POST",
    headers: {},
    body: form,
  });
  const ids = normalizeStagedUploadIds(response || {});
  if (ids.length !== files.length) {
    throw new Error("ファイルのstaging IDを取得できませんでした");
  }
  return ids;
}

function fileLabel(file: File): string {
  const size = formatBytes(file.size);
  return size === "-" ? file.name : `${file.name} · ${size}`;
}

/** 入力、複数ファイルのstaging、画像認識skipを一つのモーダルで扱う。 */
export function DocsClipIngestDialog({
  open,
  onOpenChange,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (
    submission: DocsClipIngestSubmission | string,
  ) => void | Promise<void>;
}) {
  const [source, setSource] = useState("");
  const [skipImageRecognition, setSkipImageRecognition] = useState(false);
  const [enableExternalResearch, setEnableExternalResearch] = useState(true);
  const [files, setFiles] = useState<PendingClipFile[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [targetOpen, setTargetOpen] = useState(false);
  const [targetQuery, setTargetQuery] = useState("");
  const [targetOptions, setTargetOptions] = useState<DocsClipTargetOption[]>([]);
  const [targetLoading, setTargetLoading] = useState(false);
  const [targetError, setTargetError] = useState("");
  const [target, setTarget] = useState<DocsClipTargetOption | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragDepthRef = useRef(0);
  const filesRef = useRef<PendingClipFile[]>([]);

  useEffect(() => {
    filesRef.current = files;
  }, [files]);

  useEffect(() => {
    if (!targetOpen) return;
    let active = true;
    const timer = window.setTimeout(() => {
      setTargetLoading(true);
      setTargetError("");
      const query = encodeURIComponent(targetQuery.trim());
      // The API is the ACL authority for picker candidates.  The client-side
      // Film guard remains a defense-in-depth check for stale/malformed rows.
      void apiFetch<DocsClipTargetSearchResponse>(`/api/docs/pages?q=${query}&limit=30&writable=true`)
        .then((response) => {
          if (active) setTargetOptions(normalizeClipTargetSearch(response));
        })
        .catch((requestError) => {
          if (!active) return;
          setTargetOptions([]);
          setTargetError(requestError instanceof Error ? requestError.message : "Docsノードの検索に失敗しました");
        })
        .finally(() => {
          if (active) setTargetLoading(false);
        });
    }, 120);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [targetOpen, targetQuery]);

  const revokePreviews = useCallback((items: PendingClipFile[]) => {
    if (typeof URL === "undefined" || typeof URL.revokeObjectURL !== "function") return;
    for (const item of items) {
      if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
    }
  }, []);

  // The Dialog is controlled by the provider, so it can be closed without
  // flowing through the local cancel button (for example when the provider
  // unmounts or another surface takes over). Keep that external close path
  // identical to an explicit cancel: clear every transient value and revoke
  // any object URLs before the next open.
  const resetClosedState = useCallback(() => {
    setSource("");
    setTarget(null);
    setTargetOpen(false);
    setTargetQuery("");
    setTargetOptions([]);
    setTargetLoading(false);
    setTargetError("");
    setSkipImageRecognition(false);
    setEnableExternalResearch(true);
    setError("");
    setDragActive(false);
    dragDepthRef.current = 0;
    if (fileInputRef.current) fileInputRef.current.value = "";
    setFiles((current) => {
      revokePreviews(current);
      filesRef.current = [];
      return [];
    });
  }, [revokePreviews]);

  useEffect(() => {
    if (!open) resetClosedState();
  }, [open, resetClosedState]);

  useEffect(() => () => {
    // The effect is intentionally scoped to unmount. Closing the dialog also
    // clears files below, and the cleanup there revokes each preview once.
    revokePreviews(filesRef.current);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const close = useCallback((force = false) => {
    if (submitting && !force) return;
    resetClosedState();
    onOpenChange(false);
  }, [onOpenChange, resetClosedState, submitting]);

  const addFiles = useCallback((incoming: File[]) => {
    const unique = dedupeClipFiles(incoming);
    if (unique.length === 0) return;
    setError("");
    setFiles((current) => {
      const existing = new Set(current.map((item) => item.key));
      const additions: PendingClipFile[] = [];
      for (const file of unique) {
        const key = clipFileKey(file);
        if (existing.has(key)) continue;
        existing.add(key);
        additions.push({ file, key, previewUrl: getPreviewUrl(file) });
      }
      return additions.length > 0 ? [...current, ...additions] : current;
    });
  }, []);

  const removeFile = useCallback((key: string) => {
    setFiles((current) => {
      const removed = current.find((item) => item.key === key);
      if (removed?.previewUrl && typeof URL !== "undefined") URL.revokeObjectURL(removed.previewUrl);
      return current.filter((item) => item.key !== key);
    });
  }, []);

  const handleDragEnter = useCallback((event: DragEvent<HTMLDivElement>) => {
    const incoming = fileListFromDataTransfer(event);
    if (!event.dataTransfer || (!incoming.length && !Array.from(event.dataTransfer.types).includes("Files"))) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current += 1;
    if (!submitting) {
      event.dataTransfer.dropEffect = "copy";
      setDragActive(true);
    }
  }, [submitting]);

  const handleDragOver = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!event.dataTransfer || !Array.from(event.dataTransfer.types).includes("Files")) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = submitting ? "none" : "copy";
  }, [submitting]);

  const handleDragLeave = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragActive(false);
  }, []);

  const handleDrop = useCallback((event: DragEvent<HTMLDivElement>) => {
    const incoming = fileListFromDataTransfer(event);
    if (incoming.length === 0) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current = 0;
    setDragActive(false);
    if (!submitting) addFiles(incoming);
  }, [addFiles, submitting]);

  const handleSubmit = useCallback(async () => {
    const trimmedSource = source.trim();
    if (submitting || (!trimmedSource && files.length === 0)) return;
    const submissionForSource = (): DocsClipIngestSubmission | string => {
      if (enableExternalResearch) {
        return target ? { source, target_node_id: target.id } : source;
      }
      return {
        source,
        enable_external_research: false,
        ...(target ? { target_node_id: target.id } : {}),
      };
    };
    // Keep the original text-only path synchronous for keyboard/button callers
    // and for consumers that immediately open the history panel. Uploads and
    // the image-recognition skip option takes the async staging path below.
    if (files.length === 0 && !skipImageRecognition) {
      try {
        const callbackResult = onSubmit(submissionForSource());
        if (callbackResult && typeof (callbackResult as Promise<void>).then === "function") {
          setSubmitting(true);
          await callbackResult;
        }
        close(true);
      } catch (submitError) {
        setError(submitError instanceof Error ? submitError.message : "クリップ取り込みに失敗しました");
      } finally {
        setSubmitting(false);
      }
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const uploadIds = await stageFiles(files.map((item) => item.file));
      const submission: DocsClipIngestSubmission = {
        source,
        upload_ids: uploadIds,
        skip_image_recognition: skipImageRecognition,
        ...(enableExternalResearch ? {} : { enable_external_research: false }),
        ...(target ? { target_node_id: target.id } : {}),
      };
      // Preserve the long-standing text-only callback shape. Upload and image
      // recognition skip requests use the explicit object.
      if (uploadIds.length === 0 && !skipImageRecognition) {
        await onSubmit(submissionForSource());
      } else {
        await onSubmit(submission);
      }
      setSubmitting(false);
      close(true);
    } catch (submitError) {
      setSubmitting(false);
      setError(submitError instanceof Error ? submitError.message : "ファイルのstagingに失敗しました");
    }
  }, [close, enableExternalResearch, files, onSubmit, skipImageRecognition, source, submitting, target]);

  const hasInput = source.trim().length > 0 || files.length > 0;

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (nextOpen) onOpenChange(true);
        else close();
      }}
    >
      <DialogContent
        size="2xl"
        className="max-h-[min(90vh,52rem)] overflow-y-auto"
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        <DialogHeader>
          <DialogTitle>クリップ取り込み</DialogTitle>
          <DialogDescription>
            保存先を指定すると、選択したDocsノード配下で今回の内容をtopicとして整理・統合して保存します（本文だけでも添付ありでも同じ階層です）。指定しない場合は設定済みの取り込み先から自動判定します。
            複数ファイルはここへドロップできます。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1.5">
            <div className="text-sm font-medium">保存先</div>
            <Popover open={targetOpen} onOpenChange={setTargetOpen}>
              <PopoverTrigger
                render={
                  <Button
                    type="button"
                    variant="outline"
                    className="h-auto min-h-9 w-full justify-between gap-2 text-left font-normal"
                    disabled={submitting}
                    aria-label="クリップの保存先"
                  />
                }
              >
                <span className="min-w-0 truncate">
                  {target
                    ? formatClipIngestBreadcrumb({ breadcrumb: target.breadcrumb, label: target.title })
                    : "自動判定（従来どおり）"}
                </span>
                <ChevronsUpDown className="size-4 shrink-0 opacity-50" />
              </PopoverTrigger>
              <PopoverContent className="w-[min(32rem,calc(100vw-2rem))] p-1" align="start">
                <Command shouldFilter={false}>
                  <CommandInput
                    placeholder="Docsノードを検索..."
                    value={targetQuery}
                    onValueChange={setTargetQuery}
                  />
                  <CommandList>
                    <CommandItem
                      value="__clip_ingest_auto__"
                      onSelect={() => {
                        setTarget(null);
                        setTargetOpen(false);
                      }}
                    >
                      <Check className={`size-4 ${target ? "opacity-0" : "opacity-100"}`} />
                      <span>自動判定（従来どおり）</span>
                    </CommandItem>
                    {targetLoading ? (
                      <div className="px-2 py-3 text-center text-xs text-muted-foreground">検索中…</div>
                    ) : null}
                    {targetError ? (
                      <div role="alert" className="px-2 py-2 text-xs text-destructive">{targetError}</div>
                    ) : null}
                    <CommandEmpty>{targetLoading ? "" : "候補が見つかりません"}</CommandEmpty>
                    {targetOptions.map((item) => (
                      <CommandItem
                        key={item.id}
                        value={`${item.title} ${item.breadcrumb.join(" ")} ${item.aliases.join(" ")}`}
                        onSelect={() => {
                          setTarget(item);
                          setTargetOpen(false);
                        }}
                      >
                        <Check className={`size-4 ${target?.id === item.id ? "opacity-100" : "opacity-0"}`} />
                        <span className="min-w-0">
                          <span className="block truncate">{item.title}</span>
                          <span className="block truncate text-xs text-muted-foreground">
                            {formatClipIngestBreadcrumb({ breadcrumb: item.breadcrumb, label: item.title })}
                          </span>
                        </span>
                      </CommandItem>
                    ))}
                  </CommandList>
                </Command>
              </PopoverContent>
            </Popover>
          </div>
          <Textarea
            aria-label="取り込むURLまたは文章"
            value={source}
            onChange={(event) => setSource(event.target.value)}
            onKeyDown={(event) => {
              if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
                event.preventDefault();
                event.stopPropagation();
                void handleSubmit();
              }
            }}
            placeholder={"https://example.com/article\n補足したい文章やメモ（ファイルだけでも可）"}
            className="min-h-32 resize-y"
            autoFocus
            disabled={submitting}
          />

          <div
            className={`rounded-xl border-2 border-dashed p-4 text-center transition-colors ${
              dragActive ? "border-primary bg-primary/10" : "border-border bg-muted/10"
            }`}
            onDragEnter={handleDragEnter}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
          >
            <Upload className="mx-auto size-7 text-muted-foreground" />
            <p className="mt-2 text-sm font-medium">ファイルをここへドロップ</p>
            <p className="mt-1 text-xs text-muted-foreground">画像はプレビュー、それ以外はファイル名とサイズを表示します。</p>
            <Input
              ref={fileInputRef}
              type="file"
              multiple
              className="mx-auto mt-3 max-w-sm"
              aria-label="取り込みファイルを選択"
              disabled={submitting}
              onChange={(event) => {
                addFiles(Array.from(event.currentTarget.files || []));
                event.currentTarget.value = "";
              }}
            />
          </div>

          {files.length > 0 ? (
            <div className="grid gap-2 sm:grid-cols-2" aria-label="取り込みファイル一覧">
              {files.map((item) => (
                <div key={item.key} className="flex min-w-0 items-center gap-2 rounded-lg border bg-background p-2">
                  <div className="flex size-12 shrink-0 items-center justify-center overflow-hidden rounded-md bg-muted">
                    {item.previewUrl ? (
                      <Image
                        src={item.previewUrl}
                        alt={item.file.name}
                        width={96}
                        height={96}
                        unoptimized
                        className="size-full object-cover"
                      />
                    ) : isClipImageFile(item.file) ? (
                      <ImageIcon className="size-5 text-muted-foreground" />
                    ) : (
                      <FileText className="size-5 text-muted-foreground" />
                    )}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs font-medium" title={item.file.name}>{item.file.name}</p>
                    <p className="truncate text-[11px] text-muted-foreground">{fileLabel(item.file)}</p>
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`${item.file.name}を削除`}
                    disabled={submitting}
                    onClick={() => removeFile(item.key)}
                  >
                    <X className="size-4" />
                  </Button>
                </div>
              ))}
            </div>
          ) : null}

          <label className="flex items-start gap-2 text-sm">
            <Checkbox
              checked={skipImageRecognition}
              onCheckedChange={(checked) => setSkipImageRecognition(checked === true)}
              disabled={submitting}
            />
            <span>
              画像認識を実施しない
              <span className="mt-0.5 block text-xs text-muted-foreground">
                添付画像をモデルへ送信せず、ファイル情報だけを取り込みます。
              </span>
            </span>
          </label>

          <label className="flex items-start gap-2 text-sm">
            <Checkbox
              checked={enableExternalResearch}
              onCheckedChange={(checked) => setEnableExternalResearch(checked === true)}
              disabled={submitting}
            />
            <span>
              追加調査を行う
              <span className="mt-0.5 block text-xs text-muted-foreground">
                OFFにするとURL本文の取得やWeb検索による追加調査を行わず、入力した文章と添付内容だけを整理して保存します。
              </span>
            </span>
          </label>

          {error ? (
            <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
              {error}
            </div>
          ) : null}
        </div>

        <DialogFooter className="items-center gap-2 sm:justify-between">
          <span className="text-xs text-muted-foreground">Ctrl+Enter で取り込み開始</span>
          <div className="flex gap-2">
            <Button type="button" variant="outline" onClick={() => close()} disabled={submitting}>
              キャンセル
            </Button>
            <Button type="button" disabled={!hasInput || submitting} onClick={() => void handleSubmit()}>
              {submitting ? <><Loader2 className="mr-1 size-4 animate-spin" />準備中…</> : "取り込む"}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
