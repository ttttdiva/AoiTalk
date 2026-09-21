"use client";

import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Download, File, Folder, Loader2, MoreHorizontal, RotateCcw, ShieldCheck } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
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

export type AppArchiveTreeEntry = {
  path: string;
  name: string;
  directory: boolean;
  files: string[];
  children: AppArchiveTreeEntry[];
};

type ArchiveTreeNode = AppArchiveTreeEntry;

export type MinimumArchivePackage = {
  /** Visible files that the client can pass to the archive include_paths contract. */
  requiredSelectablePaths: string[];
  /** Entries the archive endpoint adds independently of include_paths. */
  backendAlwaysIncludedPaths: string[];
  /** Visible paths intentionally omitted from the minimum package. */
  excludedPaths: string[];
};

export type MinimumArchivePathClassification = "required" | "excluded" | "optional";

type MinimumArchiveTarget = {
  entrypoint?: string | null;
  runtime?: string | null;
  surface?: string | null;
  target_key?: string | null;
};

type MinimumArchiveResolverInput = {
  files?: AppFile[] | AppArchiveTreeEntry[] | string[];
  entrypoint?: string | null;
  target?: MinimumArchiveTarget | null;
  manifest?: Record<string, unknown> | null;
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

function pathKey(value: string): string {
  return normalizeFilePath(value).toLowerCase();
}

const ARCHIVE_DEPENDENCY_PARTS = new Set([
  "node_modules",
  "venv",
  ".venv",
  "__pycache__",
  "dist",
  "build",
  "cache",
]);
const ARCHIVE_RUNTIME_PARTS = new Set(["logs", "runtime data"]);
const ARCHIVE_GENERATED_PARTS = new Set([
  "input",
  "inputs",
  "output",
  "outputs",
  "log",
  "logs",
  "ttl",
  "temp",
  "tmp",
  "generated",
]);
const ARCHIVE_CREDENTIAL_PARTS = new Set(["secrets", "credential", "credentials"]);
const ARCHIVE_PRIVATE_NAMES = new Set([
  "credentials",
  "credential",
  "device_list.csv",
  "業務備忘録.txt",
  "id_rsa",
  "id_ed25519",
]);
const ARCHIVE_PRIVATE_SUFFIXES = [".key", ".pem", ".p12", ".pfx", ".secret", ".secrets"];

/**
 * Classify a visible App path for the minimum package shortcut.
 *
 * This mirrors the existing archive categories conservatively.  It never
 * invents a dependency graph: only the declared target entrypoint and the
 * established Office/VBA macro support directories can become required.
 */
export function classifyMinimumArchivePath(
  value: string,
  options: { entrypoint?: string | null; officeVba?: boolean } = {},
): MinimumArchivePathClassification {
  const normalized = normalizeFilePath(value);
  if (!normalized) return "excluded";
  const parts = normalized.split("/");
  const lowerParts = parts.map((part) => part.toLowerCase());
  const lowerDirectoryParts = lowerParts.slice(0, -1);
  const fileName = lowerParts.at(-1) || "";
  const entrypoint = normalizeFilePath(options.entrypoint);
  const entrypointKey = pathKey(entrypoint);
  const pathKeyValue = pathKey(normalized);
  const macroRoot = macroSupportRoot(entrypoint);
  const macroRootKey = macroRoot?.toLowerCase();

  if (
    lowerParts.includes(".git")
    || lowerParts.some((part) => ARCHIVE_DEPENDENCY_PARTS.has(part))
    || lowerParts.some((part) => ARCHIVE_RUNTIME_PARTS.has(part))
    || lowerParts.some((part) => ARCHIVE_CREDENTIAL_PARTS.has(part))
    || ARCHIVE_PRIVATE_NAMES.has(fileName)
    || fileName === "device_list"
    || fileName.startsWith("device_list.")
    || fileName === ".env"
    || fileName.startsWith(".env.")
    || ARCHIVE_PRIVATE_SUFFIXES.some((suffix) => fileName.endsWith(suffix))
    || fileName.endsWith(".ttl")
    || lowerDirectoryParts.some((part) => ARCHIVE_GENERATED_PARTS.has(part))
  ) {
    return "excluded";
  }

  if (options.officeVba && entrypointKey && pathKeyValue === entrypointKey) return "required";
  const configPrefix = macroRootKey === undefined ? null : `${macroRootKey ? `${macroRootKey}/` : ""}config/`;
  const templatesPrefix = macroRootKey === undefined ? null : `${macroRootKey ? `${macroRootKey}/` : ""}templates/`;
  if (options.officeVba && ((configPrefix && pathKeyValue.startsWith(configPrefix)) || (templatesPrefix && pathKeyValue.startsWith(templatesPrefix)))) {
    return "required";
  }
  return "optional";
}

/** Backwards-compatible concise alias for tests and consumers. */
export const classifyArchivePath = classifyMinimumArchivePath;

function manifestTargetEntry(
  manifest: Record<string, unknown> | null | undefined,
  targetKey?: string | null,
): MinimumArchiveTarget | undefined {
  const rawTargets = manifest?.targets;
  if (!rawTargets || typeof rawTargets !== "object" || Array.isArray(rawTargets)) return undefined;
  const targets = rawTargets as Record<string, unknown>;
  const candidateKeys = targetKey ? [targetKey] : Object.keys(targets);
  for (const key of candidateKeys) {
    const value = targets[key];
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    const target = value as Record<string, unknown>;
    if (typeof target.entrypoint === "string" || typeof target.runtime === "string" || typeof target.surface === "string") {
      return {
        entrypoint: typeof target.entrypoint === "string" ? target.entrypoint : null,
        runtime: typeof target.runtime === "string" ? target.runtime : null,
        surface: typeof target.surface === "string" ? target.surface : null,
        target_key: key,
      };
    }
  }
  return undefined;
}

function isOfficeVbaTarget(target: MinimumArchiveTarget | undefined, entrypoint: string): boolean {
  const runtime = String(target?.runtime || "").toLowerCase();
  const surface = String(target?.surface || "").toLowerCase();
  return runtime === "vba"
    || surface === "office"
    || /\.(xlsm|xltm|xlsb|xlam)$/i.test(entrypoint);
}

function macroSupportRoot(entrypoint: string): string | null {
  const parts = normalizeFilePath(entrypoint).split("/").filter(Boolean);
  if (!parts.length) return null;
  return parts.slice(0, -1).join("/");
}

function visibleFilePaths(
  value: AppFile[] | AppArchiveTreeEntry[] | string[] | undefined,
): string[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const paths: string[] = [];
  const add = (raw: unknown) => {
    const path = normalizeFilePath(raw);
    if (!path) return;
    const key = pathKey(path);
    if (seen.has(key)) return;
    seen.add(key);
    paths.push(path);
  };
  const visitTree = (node: AppArchiveTreeEntry) => {
    if (!node.directory) add(node.path);
    node.children?.forEach(visitTree);
  };
  for (const item of value) {
    if (typeof item === "string") {
      add(item);
    } else if (item && typeof item === "object") {
      const candidate = item as Partial<AppFile & AppArchiveTreeEntry>;
      if ("directory" in candidate || "children" in candidate) {
        visitTree(candidate as AppArchiveTreeEntry);
      } else if (!candidate.is_dir) {
        add(candidate.path || candidate.filename || candidate.name);
      }
    }
  }
  return paths;
}

function resolveMinimumArchiveInput(
  value: AppFile[] | AppArchiveTreeEntry[] | string[] | MinimumArchiveResolverInput,
  entrypoint?: string | null,
): { files: AppFile[] | AppArchiveTreeEntry[] | string[]; entrypoint?: string | null; target?: MinimumArchiveTarget | null; manifest?: Record<string, unknown> | null } {
  if (Array.isArray(value)) return { files: value, entrypoint };
  return {
    files: value.files || [],
    entrypoint: entrypoint ?? value.entrypoint,
    target: value.target,
    manifest: value.manifest,
  };
}

/**
 * Resolve the minimum reproducible package from currently visible files.
 *
 * Overloads support both ``resolveMinimumArchivePackage(files, entrypoint)``
 * and an object input for callers that also have a manifest/target.  The
 * backend currently adds no implicit entries, so that result remains empty.
 */
export function resolveMinimumArchivePackage(
  files: AppFile[] | AppArchiveTreeEntry[] | string[],
  entrypoint?: string | null,
): MinimumArchivePackage;
export function resolveMinimumArchivePackage(input: MinimumArchiveResolverInput): MinimumArchivePackage;
export function resolveMinimumArchivePackage(
  value: AppFile[] | AppArchiveTreeEntry[] | string[] | MinimumArchiveResolverInput,
  entrypoint?: string | null,
): MinimumArchivePackage {
  const input = resolveMinimumArchiveInput(value, entrypoint);
  const manifestTarget = manifestTargetEntry(input.manifest, input.target?.target_key);
  const target = manifestTarget
    ? { ...input.target, ...manifestTarget }
    : input.target || manifestTargetEntry(input.manifest, undefined);
  const declaredEntrypoint = normalizeFilePath(input.entrypoint ?? target?.entrypoint);
  const files = visibleFilePaths(input.files);
  const officeVba = isOfficeVbaTarget(target, declaredEntrypoint);
  const requiredSelectablePaths: string[] = [];
  const excludedPaths: string[] = [];
  for (const path of files) {
    const classification = classifyMinimumArchivePath(path, { entrypoint: declaredEntrypoint, officeVba });
    if (classification === "excluded") excludedPaths.push(path);
    else if (officeVba && classification === "required") requiredSelectablePaths.push(path);
  }
  const sortPaths = (paths: string[]) => paths.sort((left, right) => left.localeCompare(right, "ja"));
  return {
    requiredSelectablePaths: sortPaths(requiredSelectablePaths),
    backendAlwaysIncludedPaths: [],
    excludedPaths: sortPaths(excludedPaths),
  };
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
  const normalizedPath = path.toLowerCase();
  const normalizedScope = scope.toLowerCase();
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
  expandedPaths,
  onToggleExpanded,
  depth = 0,
}: {
  nodes: ArchiveTreeNode[];
  selectedFiles: Set<string>;
  onToggle: (node: ArchiveTreeNode, checked: boolean) => void;
  expandedPaths: Set<string>;
  onToggleExpanded: (path: string) => void;
  depth?: number;
}) {
  return <div className="space-y-1">{nodes.map((node) => {
    // Keep the checkbox/label association injective. Replacing separators with
    // `-` makes `a/b` collide with a real `a-b` file once both are visible.
    const id = `app-archive-scope-${encodeURIComponent(node.path)}`;
    const checked = nodeCheckedState(node, selectedFiles);
    const expandable = node.directory && node.children.length > 0;
    const expanded = expandable && expandedPaths.has(node.path);
    return <div key={node.path}>
      <div className="flex min-w-0 items-center gap-1.5 rounded-md px-2 py-1.5 hover:bg-muted/60" style={{ paddingLeft: `${depth * 16 + 8}px` }}>
        <Checkbox id={id} checked={checked === true} indeterminate={checked === "indeterminate"} aria-checked={checked === "indeterminate" ? "mixed" : checked ? "true" : "false"} onCheckedChange={(value) => onToggle(node, value === true)} aria-label={`${node.path}をダウンロード`} />
        {expandable ? <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
          aria-expanded={expanded}
          aria-label={`${node.path}を${expanded ? "格納" : "展開"}`}
          title={node.path}
          onClick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            onToggleExpanded(node.path);
          }}
        >
          {expanded ? <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" /> : <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" />}
          <Folder className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="min-w-0 truncate text-xs">{node.name}</span>
        </button> : <label htmlFor={id} className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5" title={node.path}>
          <File className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="min-w-0 truncate text-xs">{node.name}</span>
        </label>}
      </div>
      {expandable && expanded && <ScopeTree nodes={node.children} selectedFiles={selectedFiles} onToggle={onToggle} expandedPaths={expandedPaths} onToggleExpanded={onToggleExpanded} depth={depth + 1} />}
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
  filesError?: Error | null;
  onRetryFiles?: () => void | Promise<unknown>;
  manifest?: Record<string, unknown>;
  target?: MinimumArchiveTarget | null;
};

export function AppArchiveDownloadDialog(props: AppArchiveDownloadDialogProps) {
  const key = settingsKey(props.appId, props.projectId);
  return <AppArchiveDownloadDialogState key={key} {...props} settingsStorageKey={key} />;
}

function AppArchiveDownloadDialogState({
  appId,
  appName = "App",
  projectId,
  label = "ダウンロード",
  compact = false,
  files = [],
  filesLoading = false,
  filesError = null,
  onRetryFiles,
  manifest,
  target,
  settingsStorageKey: key,
}: AppArchiveDownloadDialogProps & { settingsStorageKey: string }) {
  const [settings, setSettings] = useState<ArchiveSettings>(() => readSettings(key));
  const [draft, setDraft] = useState<ArchiveSettings>(() => readSettings(key));
  const [open, setOpen] = useState(false);
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(() => new Set());
  const [minimumPreset, setMinimumPreset] = useState(false);
  const [filesRetrying, setFilesRetrying] = useState(false);

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
  const minimumPackage = useMemo(
    () => resolveMinimumArchivePackage({ files, target, manifest }),
    [files, manifest, target],
  );
  const fileListAvailable = !filesLoading && !filesError;
  const minimumPresetAvailable = fileListAvailable && minimumPackage.requiredSelectablePaths.length > 0;
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
  const allChecked = fileListAvailable && (filePaths.length === 0 || selectedFiles.size === filePaths.length);
  const allIndeterminate = fileListAvailable && !allChecked && selectedFiles.size > 0;

  const openSettings = () => {
    setDraft(settings);
    setExpandedPaths(new Set());
    setMinimumPreset(false);
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
  const resetExclusions = () => {
    setMinimumPreset(false);
    setDraft((current) => ({ ...current, exclusions: {} }));
  };
  const setRecommended = () => {
    setMinimumPreset(false);
    setDraft((current) => ({
      ...current,
      exclusions: { git: true, dependencies: true, runtime: true, credentials: true },
    }));
  };
  const toggleNode = (node: ArchiveTreeNode, checked: boolean) => {
    setMinimumPreset(false);
    setDraft((current) => {
      const selected = selectedFilesForScope(filePaths, current.includePaths);
      for (const path of node.files) {
        if (checked) selected.add(path);
        else selected.delete(path);
      }
      return { ...current, includePaths: compressSelectedFiles(selected, tree, filePaths.length) };
    });
  };
  const selectAll = (checked: boolean) => {
    setMinimumPreset(false);
    setDraft((current) => ({ ...current, includePaths: checked ? null : [] }));
  };
  const toggleMinimumPreset = (checked: boolean) => {
    if (!checked) {
      setMinimumPreset(false);
      return;
    }
    if (!minimumPresetAvailable) return;
    setMinimumPreset(true);
    setDraft((current) => ({
      ...current,
      includePaths: minimumPackage.requiredSelectablePaths,
      exclusions: { ...current.exclusions, credentials: true },
    }));
  };
  const toggleExclusion = (optionKey: keyof AppArchiveExclusions, checked: boolean) => {
    setMinimumPreset(false);
    setDraft((current) => ({ ...current, exclusions: { ...current.exclusions, [optionKey]: checked } }));
  };
  const toggleExpanded = (path: string) => setExpandedPaths((current) => {
    const next = new Set(current);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    return next;
  });
  const retryFiles = async () => {
    if (!onRetryFiles || filesRetrying) return;
    setFilesRetrying(true);
    try {
      await onRetryFiles();
    } catch {
      // SWR owns the error state; leave the dialog open so the user can retry
      // again without changing the saved download settings.
    } finally {
      setFilesRetrying(false);
    }
  };

  return <div className="flex items-center gap-1">
    <a
      href={downloadUrl}
      download
      title={`${appName}（${scopeSummary}）をダウンロード`}
      data-slot="button"
      className={buttonVariants({
        size: "sm",
        variant: "outline",
        className: compact ? "h-7 px-2 text-[11px]" : "h-8 px-3",
      })}
    >
      <Download className="size-3.5" /> {label}
    </a>
    <Dialog open={open} onOpenChange={(next) => { if (next) openSettings(); else setOpen(false); }}>
      <DropdownMenu>
        <DropdownMenuTrigger render={<Button type="button" size="icon-sm" variant="ghost" aria-label="ダウンロード設定" className={compact ? "size-7" : "size-8"} />}>
          <MoreHorizontal className="size-4" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onClick={openSettings}>ダウンロード範囲・除外を設定</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <DialogContent size="4xl" className="flex h-[min(calc(100dvh-2rem),64rem)] max-h-[min(calc(100dvh-2rem),64rem)] w-[min(56rem,calc(100vw-2rem))] min-w-0 flex-col gap-0 overflow-hidden p-0">
        <DialogHeader className="shrink-0 border-b px-4 py-4 pr-12">
          <DialogTitle>ダウンロード設定</DialogTitle>
          <DialogDescription className="min-w-0 break-words">
            メインのダウンロードボタンは、ここで保存した設定をすぐに使います。現在の範囲: {scopeSummary}
          </DialogDescription>
        </DialogHeader>

        <div data-archive-dialog-body className="flex min-h-0 flex-1 flex-col gap-4 overflow-x-hidden overflow-y-auto px-4 py-4 [@media(max-height:900px)]:gap-2 [@media(max-height:900px)]:py-2">
          <section className="shrink-0 space-y-2 [@media(max-height:900px)]:space-y-1">
            <div>
              <h3 className="text-sm font-medium">クイック選択</h3>
              <p className="mt-1 text-xs leading-5 text-muted-foreground [@media(max-height:900px)]:hidden">実行に必要なファイルだけを選び、資格情報は除外します。</p>
            </div>
            <div className="flex min-w-0 items-start gap-3 rounded-lg border border-primary/30 bg-primary/5 p-3 [@media(max-height:900px)]:p-2">
              <Checkbox
                id="app-archive-minimum-preset"
                checked={minimumPreset}
                disabled={!minimumPresetAvailable}
                onCheckedChange={(value) => toggleMinimumPreset(value === true)}
                className="mt-0.5"
                aria-label="マクロの動作に最低限必要なものだけON"
              />
              <label htmlFor="app-archive-minimum-preset" className="min-w-0 cursor-pointer">
                <span className="block truncate text-sm font-medium" title="マクロの動作に最低限必要なものだけON">マクロの動作に最低限必要なものだけON</span>
                <span className="mt-0.5 block text-xs leading-5 text-muted-foreground [@media(max-height:900px)]:hidden">
                  {minimumPresetAvailable
                    ? `${minimumPackage.requiredSelectablePaths.length}件の必要ファイルを選択します。サーバー側の暗黙追加はなく、生成物・credentialは含めません。`
                    : "Office/VBA Targetのentrypointを確認できると利用できます。"}
                </span>
              </label>
            </div>
          </section>

          <section data-archive-scope className="shrink-0 min-w-0 space-y-2">
            <div className="shrink-0">
              <h3 className="text-sm font-medium">ダウンロードする階層</h3>
              <p className="mt-1 text-xs leading-5 text-muted-foreground [@media(max-height:900px)]:hidden">フォルダを選ぶと、その配下だけをZIPに含めます。</p>
            </div>
            <div data-archive-scope-resize className="flex h-[clamp(14rem,32dvh,24rem)] min-h-[14rem] max-h-[min(32rem,56dvh)] min-w-0 resize-y flex-col overflow-hidden rounded-lg border p-2">
              <div className="flex shrink-0 items-center gap-2 rounded-md px-2 py-1.5 hover:bg-muted/60">
                <Checkbox id="app-archive-scope-all" checked={allChecked} indeterminate={allIndeterminate} disabled={!fileListAvailable} aria-checked={allIndeterminate ? "mixed" : allChecked ? "true" : "false"} onCheckedChange={(value) => selectAll(value === true)} aria-label="すべての階層をダウンロード" />
                <Folder className="size-3.5 shrink-0 text-muted-foreground" />
                <label htmlFor="app-archive-scope-all" className="cursor-pointer text-xs font-medium">すべての階層</label>
              </div>
              <div data-archive-scope-scroll className="mt-1 min-h-[10rem] min-w-0 flex-1 overflow-y-auto border-t pt-1">
                {filesLoading ? <p className="px-2 py-3 text-xs text-muted-foreground">階層を読み込み中…</p> : filesError ? <div className="space-y-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">
                  <p className="font-medium">ファイル一覧を取得できませんでした。</p>
                  <p className="break-words">{filesError.message || "サーバーとの通信に失敗しました。"}</p>
                  <p className="leading-5 text-muted-foreground">「再試行」で一覧だけを再取得します。保存済みのダウンロード範囲・除外設定は変更しません。ZIPダウンロードは一覧APIとは独立して利用できます。</p>
                  {onRetryFiles && <Button type="button" size="sm" variant="outline" onClick={() => void retryFiles()} disabled={filesRetrying}>
                    {filesRetrying ? <Loader2 className="size-3.5 animate-spin" /> : <RotateCcw className="size-3.5" />} 再試行
                  </Button>}
                </div> : tree.length ? <ScopeTree nodes={tree} selectedFiles={selectedFiles} onToggle={toggleNode} expandedPaths={expandedPaths} onToggleExpanded={toggleExpanded} /> : <p className="px-2 py-3 text-xs text-muted-foreground">ファイル一覧は空です。選択できるダウンロード範囲はありません。全体ダウンロードは保存済み設定に従ってサーバー側で処理されます。</p>}
              </div>
            </div>
          </section>

          <section data-archive-exclusions className="shrink-0 space-y-2 [@media(max-height:900px)]:space-y-1">
            <div>
              <h3 className="text-sm font-medium">除外するカテゴリ</h3>
              <p className="mt-1 text-xs leading-5 text-muted-foreground [@media(max-height:900px)]:hidden">共有・軽量化が必要なときだけ、三点リーダー内で指定します。</p>
            </div>
            <div className="flex min-w-0 flex-wrap gap-2 rounded-lg border bg-muted/20 p-3 [@media(max-height:900px)]:p-2">
              <Button type="button" size="sm" variant="outline" onClick={setRecommended}>
                <ShieldCheck className="size-3.5" /> 共有用の推奨除外を選択
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={resetExclusions} disabled={selectedCount === 0}>
                <RotateCcw className="size-3.5" /> 除外を解除
              </Button>
            </div>
            <div className="grid min-w-0 gap-2 sm:grid-cols-2 lg:grid-cols-4">
              {ARCHIVE_OPTIONS.map((option) => {
                const id = `app-archive-exclude-${option.key}`;
                return <div key={option.key} className="flex min-w-0 items-start gap-3 rounded-lg border p-3 [@media(max-height:900px)]:p-2">
                  <Checkbox data-archive-exclusion={option.key} id={id} checked={draft.exclusions[option.key] === true} onCheckedChange={(checked) => toggleExclusion(option.key, checked === true)} className="mt-0.5" aria-label={option.label} />
                  <label htmlFor={id} className="min-w-0 cursor-pointer">
                    <span className="block truncate text-sm font-medium" title={option.label}>{option.label}</span>
                    <span className="mt-0.5 block text-xs leading-5 text-muted-foreground [@media(max-height:900px)]:hidden">{option.description}</span>
                  </label>
                </div>;
              })}
            </div>
          </section>

          <div className="shrink-0 rounded-lg border bg-muted/20 p-3 text-xs [@media(max-height:900px)]:p-2">
            <p className="font-medium">{selectedCount === 0 ? "除外なし" : `${selectedCount}カテゴリを除外`}</p>
            <p className="mt-1 leading-5 text-muted-foreground [@media(max-height:900px)]:hidden">シンボリックリンクはworkspace外の参照を巻き込まないため、リンク先を追跡しません。</p>
          </div>
        </div>

        <DialogFooter className="shrink-0 mx-0 mb-0 rounded-b-xl">
          <Button type="button" variant="outline" onClick={() => setOpen(false)}>キャンセル</Button>
          <Button type="button" onClick={saveSettings}>設定を保存</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>;
}
