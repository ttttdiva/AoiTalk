"use client";

import { useEffect, useMemo, useState } from "react";
import { Download, File, Folder, MoreHorizontal, RotateCcw, ShieldCheck } from "lucide-react";
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { appsApi, type AppArchiveExclusions, type AppFile } from "@/lib/apps-api";

type ArchiveOption = {
  key: keyof AppArchiveExclusions;
  label: string;
  description: string;
};

const ARCHIVE_OPTIONS: ArchiveOption[] = [
  { key: "git", label: "Git履歴を除外", description: ".gitフォルダを含めません。ソースだけ共有する場合に推奨します。" },
  { key: "dependencies", label: "依存・build・cacheを除外", description: "node_modules、venv、dist、build、cache等を含めません。ZIPを軽量化します。" },
  { key: "runtime", label: "ログ・runtimeデータを除外", description: "logsとruntime dataを含めません。実行履歴を共有しない場合に推奨します。" },
  { key: "credentials", label: "secrets・認証関連を除外", description: "secrets、.env、鍵、device_list.csv等を含めません。共有先に不要な場合だけ選択してください。" },
];

type ArchiveTreeNode = {
  path: string;
  name: string;
  directory: boolean;
  files: string[];
  children: ArchiveTreeNode[];
};

type ArchiveSettings = {
  /** null means all paths, while [] means an explicit empty selection. */
  includePaths: string[] | null;
  exclusions: AppArchiveExclusions;
};

const DEFAULT_SETTINGS: ArchiveSettings = { includePaths: null, exclusions: {} };
const STORAGE_PREFIX = "aoitalk-app-archive-download:";
const SETTINGS_EVENT = "aoitalk-app-archive-download-settings";

function settingsKey(appId: string, projectId?: string): string {
  return `${STORAGE_PREFIX}${projectId || "global"}:${appId}`;
}

function sanitizeSettings(value: unknown): ArchiveSettings {
  if (!value || typeof value !== "object") return DEFAULT_SETTINGS;
  const record = value as Record<string, unknown>;
  const includePaths = record.includePaths === null
    ? null
    : Array.isArray(record.includePaths)
      ? Array.from(new Set(record.includePaths.filter((item): item is string => typeof item === "string" && item.trim().length > 0)))
      : DEFAULT_SETTINGS.includePaths;
  const rawExclusions = record.exclusions && typeof record.exclusions === "object" ? record.exclusions as Record<string, unknown> : {};
  const exclusions: AppArchiveExclusions = {};
  for (const option of ARCHIVE_OPTIONS) {
    if (rawExclusions[option.key] === true) exclusions[option.key] = true;
  }
  return { includePaths, exclusions };
}

function readSettings(key: string): ArchiveSettings {
  if (typeof window === "undefined") return DEFAULT_SETTINGS;
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? sanitizeSettings(JSON.parse(raw)) : DEFAULT_SETTINGS;
  } catch {
    return DEFAULT_SETTINGS;
  }
}

function normalizeFilePath(value: unknown): string {
  return String(value || "").replaceAll("\\", "/").split("/").filter(Boolean).join("/");
}

function buildArchiveTree(files: AppFile[]): ArchiveTreeNode[] {
  type MutableNode = {
    path: string;
    name: string;
    directory: boolean;
    files: string[];
    children: Map<string, MutableNode>;
  };
  const root = new Map<string, MutableNode>();
  const filePaths = Array.from(new Set(
    files
      .filter((file) => !file.is_dir)
      .map((file) => normalizeFilePath(file.path || file.filename))
      .filter(Boolean),
  ));

  for (const filePath of filePaths) {
    let nodes = root;
    const parts = filePath.split("/");
    for (let index = 0; index < parts.length; index += 1) {
      const name = parts[index];
      const path = parts.slice(0, index + 1).join("/");
      let node = nodes.get(name);
      if (!node) {
        node = { path, name, directory: index < parts.length - 1, files: [], children: new Map() };
        nodes.set(name, node);
      }
      node.files.push(filePath);
      nodes = node.children;
    }
  }

  const toTree = (nodes: Map<string, MutableNode>): ArchiveTreeNode[] => Array.from(nodes.values())
    .sort((left, right) => Number(right.directory) - Number(left.directory) || left.name.localeCompare(right.name, "ja"))
    .map((node) => ({
      path: node.path,
      name: node.name,
      directory: node.directory,
      files: Array.from(new Set(node.files)),
      children: toTree(node.children),
    }));
  return toTree(root);
}

function pathMatchesScope(path: string, scope: string): boolean {
  const normalizedPath = path.toLocaleLowerCase();
  const normalizedScope = scope.toLocaleLowerCase();
  return normalizedPath === normalizedScope || normalizedPath.startsWith(`${normalizedScope}/`);
}

function selectedFilesForScope(filePaths: string[], includePaths: string[] | null): Set<string> {
  if (includePaths === null) return new Set(filePaths);
  return new Set(filePaths.filter((path) => includePaths.some((scope) => pathMatchesScope(path, scope))));
}

function compressSelectedFiles(selectedFiles: Set<string>, tree: ArchiveTreeNode[], allFileCount: number): string[] | null {
  if (selectedFiles.size === allFileCount) return null;
  const compress = (nodes: ArchiveTreeNode[]): string[] => nodes.flatMap((node) => {
    const selectedCount = node.files.filter((path) => selectedFiles.has(path)).length;
    if (selectedCount === 0) return [];
    if (selectedCount === node.files.length) return [node.path];
    return node.directory ? compress(node.children) : [node.path];
  });
  return compress(tree);
}

function nodeCheckedState(node: ArchiveTreeNode, selectedFiles: Set<string>): boolean | "indeterminate" {
  const selectedCount = node.files.filter((path) => selectedFiles.has(path)).length;
  if (selectedCount === 0) return false;
  if (selectedCount === node.files.length) return true;
  return "indeterminate";
}

function ScopeTree({
  nodes,
  selectedFiles,
  onToggle,
  depth = 0,
}: {
  nodes: ArchiveTreeNode[];
  selectedFiles: Set<string>;
  onToggle: (node: ArchiveTreeNode, checked: boolean) => void;
  depth?: number;
}) {
  return <div className="space-y-1">{nodes.map((node) => {
    const id = `app-archive-scope-${node.path.replaceAll(/[^a-zA-Z0-9_-]/g, "-")}`;
    const checked = nodeCheckedState(node, selectedFiles);
    return <div key={node.path}>
      <div className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-muted/60" style={{ paddingLeft: `${depth * 16 + 8}px` }}>
        <Checkbox id={id} checked={checked === true} aria-checked={checked === "indeterminate" ? "mixed" : checked ? "true" : "false"} onCheckedChange={(value) => onToggle(node, value === true)} aria-label={`${node.path}をダウンロード`} />
        {node.directory ? <Folder className="size-3.5 shrink-0 text-muted-foreground" /> : <File className="size-3.5 shrink-0 text-muted-foreground" />}
        <label htmlFor={id} className="min-w-0 cursor-pointer truncate text-xs" title={node.path}>{node.name}</label>
      </div>
      {node.directory && node.children.length > 0 && <ScopeTree nodes={node.children} selectedFiles={selectedFiles} onToggle={onToggle} depth={depth + 1} />}
    </div>;
  })}</div>;
}

type AppArchiveDownloadDialogProps = {
  appId: string;
  appName?: string;
  projectId?: string;
  label?: string;
  compact?: boolean;
  files?: AppFile[];
  filesLoading?: boolean;
};

export function AppArchiveDownloadDialog({
  appId,
  appName = "App",
  projectId,
  label = "ダウンロード",
  compact = false,
  files = [],
  filesLoading = false,
}: AppArchiveDownloadDialogProps) {
  const key = settingsKey(appId, projectId);
  const [settings, setSettings] = useState<ArchiveSettings>(() => readSettings(key));
  const [draft, setDraft] = useState<ArchiveSettings>(() => readSettings(key));
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const handleSettingsChange = () => {
      const next = readSettings(key);
      setSettings(next);
      if (!open) setDraft(next);
    };
    window.addEventListener(SETTINGS_EVENT, handleSettingsChange);
    return () => window.removeEventListener(SETTINGS_EVENT, handleSettingsChange);
  }, [key, open]);

  const tree = useMemo(() => buildArchiveTree(files), [files]);
  const filePaths = useMemo(() => tree.flatMap((node) => node.files).filter((path, index, all) => all.indexOf(path) === index), [tree]);
  const selectedFiles = useMemo(() => selectedFilesForScope(filePaths, draft.includePaths), [draft.includePaths, filePaths]);
  const savedSelectedFiles = useMemo(() => selectedFilesForScope(filePaths, settings.includePaths), [filePaths, settings.includePaths]);
  const selectedCount = ARCHIVE_OPTIONS.filter(({ key: optionKey }) => draft.exclusions[optionKey]).length;
  const downloadUrl = useMemo(
    () => appsApi.downloadArchive(appId, projectId, settings.exclusions, settings.includePaths ?? undefined),
    [appId, projectId, settings.exclusions, settings.includePaths],
  );
  const scopeSummary = settings.includePaths === null
    ? "全階層"
    : settings.includePaths.length
      ? settings.includePaths.join(", ")
      : "選択なし";
  const allChecked = filePaths.length === 0 || savedSelectedFiles.size === filePaths.length;
  const allIndeterminate = !allChecked && savedSelectedFiles.size > 0;

  const openSettings = () => {
    setDraft(settings);
    setOpen(true);
  };
  const saveSettings = () => {
    setSettings(draft);
    try {
      window.localStorage.setItem(key, JSON.stringify(draft));
      window.dispatchEvent(new Event(SETTINGS_EVENT));
    } catch {
      // Private browsing or a disabled storage must not block downloading.
    }
    setOpen(false);
  };
  const resetExclusions = () => setDraft((current) => ({ ...current, exclusions: {} }));
  const setRecommended = () => setDraft((current) => ({
    ...current,
    exclusions: { git: true, dependencies: true, runtime: true, credentials: true },
  }));
  const toggleNode = (node: ArchiveTreeNode, checked: boolean) => setDraft((current) => {
    const selected = selectedFilesForScope(filePaths, current.includePaths);
    for (const path of node.files) {
      if (checked) selected.add(path);
      else selected.delete(path);
    }
    return { ...current, includePaths: compressSelectedFiles(selected, tree, filePaths.length) };
  });
  const selectAll = (checked: boolean) => setDraft((current) => ({ ...current, includePaths: checked ? null : [] }));

  return <div className="flex items-center gap-1">
    <Button
      type="button"
      size="sm"
      variant="outline"
      className={compact ? "h-7 px-2 text-[11px]" : "h-8 px-3"}
      nativeButton={false}
      render={<a href={downloadUrl} download title={`${appName}（${scopeSummary}）をダウンロード`} />}
    >
      <Download className="size-3.5" /> {label}
    </Button>
    <Dialog open={open} onOpenChange={(next) => { if (next) openSettings(); else setOpen(false); }}>
      <DropdownMenu>
        <DropdownMenuTrigger render={<Button type="button" size="icon-sm" variant="ghost" aria-label="ダウンロード設定" className={compact ? "size-7" : "size-8"} />}>
          <MoreHorizontal className="size-4" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onClick={openSettings}>ダウンロード範囲・除外を設定</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <DialogContent size="lg" className="max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>ダウンロード設定</DialogTitle>
          <DialogDescription>
            メインのダウンロードボタンは、ここで保存した設定をすぐに使います。現在の範囲: {scopeSummary}
          </DialogDescription>
        </DialogHeader>

        <section className="space-y-2">
          <div>
            <h3 className="text-sm font-medium">ダウンロードする階層</h3>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">フォルダを選ぶと、その配下だけをZIPに含めます。</p>
          </div>
          <div className="rounded-lg border p-2">
            <div className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-muted/60">
              <Checkbox id="app-archive-scope-all" checked={allChecked} aria-checked={allIndeterminate ? "mixed" : allChecked ? "true" : "false"} onCheckedChange={(value) => selectAll(value === true)} aria-label="すべての階層をダウンロード" />
              <Folder className="size-3.5 shrink-0 text-muted-foreground" />
              <label htmlFor="app-archive-scope-all" className="cursor-pointer text-xs font-medium">すべての階層</label>
            </div>
            <div className="mt-1 max-h-56 overflow-y-auto border-t pt-1">
              {filesLoading ? <p className="px-2 py-3 text-xs text-muted-foreground">階層を読み込み中…</p> : tree.length ? <ScopeTree nodes={tree} selectedFiles={selectedFiles} onToggle={toggleNode} /> : <p className="px-2 py-3 text-xs text-muted-foreground">選択できるファイルがありません。全体ダウンロードはサーバー側のファイルを対象にします。</p>}
            </div>
          </div>
        </section>

        <section className="space-y-2">
          <div>
            <h3 className="text-sm font-medium">除外するカテゴリ</h3>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">共有・軽量化が必要なときだけ、三点リーダー内で指定します。</p>
          </div>
          <div className="flex flex-wrap gap-2 rounded-lg border bg-muted/20 p-3">
            <Button type="button" size="sm" variant="outline" onClick={setRecommended}>
              <ShieldCheck className="size-3.5" /> 共有用の推奨除外を選択
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={resetExclusions} disabled={selectedCount === 0}>
              <RotateCcw className="size-3.5" /> 除外を解除
            </Button>
          </div>
          <div className="space-y-2">
            {ARCHIVE_OPTIONS.map((option) => {
              const id = `app-archive-exclude-${option.key}`;
              return <div key={option.key} className="flex items-start gap-3 rounded-lg border p-3">
                <Checkbox id={id} checked={draft.exclusions[option.key] === true} onCheckedChange={(checked) => setDraft((current) => ({ ...current, exclusions: { ...current.exclusions, [option.key]: checked === true } }))} className="mt-0.5" aria-label={option.label} />
                <label htmlFor={id} className="min-w-0 cursor-pointer">
                  <span className="block text-sm font-medium">{option.label}</span>
                  <span className="mt-0.5 block text-xs leading-5 text-muted-foreground">{option.description}</span>
                </label>
              </div>;
            })}
          </div>
        </section>

        <div className="rounded-lg border bg-muted/20 p-3 text-xs">
          <p className="font-medium">{selectedCount === 0 ? "除外なし" : `${selectedCount}カテゴリを除外`}</p>
          <p className="mt-1 leading-5 text-muted-foreground">シンボリックリンクはworkspace外の参照を巻き込まないため、リンク先を追跡しません。</p>
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => setOpen(false)}>キャンセル</Button>
          <Button type="button" onClick={saveSettings}>設定を保存</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>;
}
