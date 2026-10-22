"use client";

import { useMemo, useState, type FormEvent } from "react";
import { Download, FileOutput, Loader2, Play } from "lucide-react";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatRuntimeStatus } from "@/components/apps/app-runtime-format";
import type {
  AppRuntimeAction,
  AppRuntimeFile,
  AppRuntimeFormField,
  AppRuntimeNode,
  AppRuntimeRun,
  AppRuntimeUi,
} from "@/lib/apps-api";

export type AppRuntimeRenderContext = {
  run?: AppRuntimeRun | null;
  logs?: string | null;
  files?: AppRuntimeFile[];
};

export type AppRuntimeRendererProps = {
  ui: AppRuntimeUi;
  actions: AppRuntimeAction[];
  context?: AppRuntimeRenderContext;
  canRun: boolean;
  online?: boolean;
  busyActionId?: string | null;
  busyActionIds?: ReadonlySet<string>;
  onRunAction: (action: AppRuntimeAction, input: Record<string, unknown>) => void;
  onDownloadFile?: (file: AppRuntimeFile) => void;
};

const SAFE_VALUE_PATH = /^run\.(?:result|status)(?:\.[A-Za-z][A-Za-z0-9_-]*)*$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function readValuePath(path: string | undefined, context: AppRuntimeRenderContext): unknown {
  if (!path || !SAFE_VALUE_PATH.test(path)) return undefined;
  return path.split(".").reduce<unknown>((value, key) => {
    if (!isRecord(value)) return undefined;
    return value[key];
  }, context);
}

function valueForNode(node: AppRuntimeNode, context: AppRuntimeRenderContext): unknown {
  if (node.value_path) return readValuePath(node.value_path, context);
  if (node.value !== undefined) return node.value;
  return node.data;
}

function displayValue(value: unknown): string {
  if (value === undefined || value === null || value === "") return "—";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "はい" : "いいえ";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function actionForNode(node: AppRuntimeNode, actions: AppRuntimeAction[]): AppRuntimeAction | undefined {
  const actionId = node.action_id;
  if (!actionId) return undefined;
  return actions.find((action) => action.action_id === actionId);
}

function actionDisabled(action: AppRuntimeAction | undefined, canRun: boolean, online: boolean, busyActionId?: string | null, busyActionIds?: ReadonlySet<string>): boolean {
  return !action || !canRun || !online || !action.available || busyActionId === action.action_id || Boolean(action && busyActionIds?.has(action.action_id));
}

function actionReason(action: AppRuntimeAction | undefined, canRun: boolean, online: boolean): string | null {
  if (!action) return "この操作はReleaseに定義されていません。";
  if (!canRun) return "このAppを実行する権限がありません。";
  if (!online) return "オフライン中は実行できません。";
  if (!action.available) return action.unavailable_reason || "現在この操作は利用できません。";
  return null;
}

function fileFromValue(value: unknown): AppRuntimeFile | null {
  if (!isRecord(value)) return null;
  if (
    typeof value.artifact_id !== "string"
    || typeof value.filename !== "string"
    || typeof value.run_id !== "string"
    || typeof value.content_type !== "string"
    || typeof value.size_bytes !== "number"
    || typeof value.sha256 !== "string"
    || typeof value.download_url !== "string"
  ) return null;
  return {
    artifact_id: value.artifact_id,
    run_id: value.run_id,
    filename: value.filename,
    content_type: value.content_type,
    size_bytes: value.size_bytes,
    sha256: value.sha256,
    download_url: value.download_url,
  };
}

function RuntimeFormNode({
  node,
  action,
  canRun,
  online,
  busyActionId,
  busyActionIds,
  context,
  onRunAction,
}: {
  node: AppRuntimeNode;
  action?: AppRuntimeAction;
  canRun: boolean;
  online: boolean;
  busyActionId?: string | null;
  busyActionIds?: ReadonlySet<string>;
  context: AppRuntimeRenderContext;
  onRunAction: (action: AppRuntimeAction, input: Record<string, unknown>) => void;
}) {
  const fields = useMemo(() => node.fields || [], [node.fields]);
  const initial = useMemo(
    () => Object.fromEntries(fields.map((field) => [field.name, field.kind === "switch" ? false : ""])),
    [fields],
  );
  const [values, setValues] = useState<Record<string, unknown>>(initial);
  const [fileNames, setFileNames] = useState<Record<string, string>>({});
  const disabled = actionDisabled(action, canRun, online, busyActionId, busyActionIds);
  const reason = actionReason(action, canRun, online);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!action || disabled) return;
    onRunAction(action, values);
  };
  const setField = (field: AppRuntimeFormField, value: unknown) => {
    setValues((current) => ({ ...current, [field.name]: value }));
  };
  return (
    <form className="space-y-3 rounded-lg border border-border/70 bg-background/30 p-3" onSubmit={submit}>
      {node.label ? <h3 className="text-sm font-semibold">{node.label}</h3> : null}
      {node.text ? <p className="text-xs text-muted-foreground">{node.text}</p> : null}
      {fields.map((field) => {
        const fieldId = `app-runtime-${node.id}-${field.name}`;
        const fieldValue = values[field.name];
        return (
          <div key={field.name} className="space-y-1.5">
            <Label htmlFor={fieldId}>{field.label || field.name}{field.required ? " *" : ""}</Label>
            {field.kind === "select" ? (
              <AppSelect
                id={fieldId}
                value={typeof fieldValue === "string" ? fieldValue : ""}
                onValueChange={(value) => setField(field, value)}
                className="w-full"
                disabled={disabled}
                required={field.required}
              >
                <option value="">選択してください</option>
                {(field.options || []).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </AppSelect>
            ) : field.kind === "switch" ? (
              <button
                id={fieldId}
                type="button"
                role="switch"
                aria-checked={fieldValue === true}
                disabled={disabled}
                onClick={() => setField(field, fieldValue !== true)}
                className="inline-flex h-7 items-center gap-2 rounded-full border border-border/70 px-3 text-xs disabled:opacity-50"
              >
                <span className={`size-2 rounded-full ${fieldValue === true ? "bg-primary" : "bg-muted-foreground/40"}`} />
                {fieldValue === true ? "有効" : "無効"}
              </button>
            ) : field.kind === "file" ? (
              <div className="space-y-1">
                <Input
                  id={fieldId}
                  type="file"
                  required={field.required}
                  disabled={disabled}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (!file) return;
                    setField(field, file);
                    setFileNames((current) => ({ ...current, [field.name]: file.name }));
                  }}
                />
                {fileNames[field.name] ? <p className="text-[11px] text-muted-foreground">{fileNames[field.name]}</p> : null}
              </div>
            ) : (
              <Input
                id={fieldId}
                type={field.kind === "number" ? "number" : "text"}
                value={typeof fieldValue === "string" || typeof fieldValue === "number" ? String(fieldValue) : ""}
                required={field.required}
                disabled={disabled}
                onChange={(event) => setField(field, field.kind === "number" ? (event.target.value === "" ? "" : Number(event.target.value)) : event.target.value)}
              />
            )}
          </div>
        );
      })}
      <div className="flex items-center gap-2 pt-1">
        <Button type="submit" size="sm" disabled={disabled}>
          {busyActionId === action?.action_id ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
          {node.label || action?.label || "実行"}
        </Button>
        {reason ? <p className="text-xs text-muted-foreground">{reason}</p> : null}
      </div>
      {context.run?.status ? <p className="text-[11px] text-muted-foreground">前回の実行: {formatRuntimeStatus(context.run)}</p> : null}
    </form>
  );
}

function RuntimeSelectNode({
  node,
  action,
  canRun,
  online,
  busyActionId,
  busyActionIds,
  onRunAction,
}: {
  node: AppRuntimeNode;
  action?: AppRuntimeAction;
  canRun: boolean;
  online: boolean;
  busyActionId?: string | null;
  busyActionIds?: ReadonlySet<string>;
  onRunAction: (action: AppRuntimeAction, input: Record<string, unknown>) => void;
}) {
  const [selected, setSelected] = useState(typeof node.value === "string" ? node.value : "");
  const disabled = actionDisabled(action, canRun, online, busyActionId, busyActionIds);
  return (
    <div className="space-y-1.5">
      <Label htmlFor={`app-runtime-${node.id}`}>{node.label || "選択"}</Label>
      <AppSelect
        id={`app-runtime-${node.id}`}
        value={selected}
        disabled={disabled}
        onValueChange={(next) => {
          setSelected(next);
          if (action) onRunAction(action, { value: next });
        }}
        className="w-full"
      >
        <option value="">選択してください</option>
        {(node.options || []).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </AppSelect>
    </div>
  );
}

function RuntimeSwitchNode({
  node,
  action,
  canRun,
  online,
  busyActionId,
  busyActionIds,
  onRunAction,
}: {
  node: AppRuntimeNode;
  action?: AppRuntimeAction;
  canRun: boolean;
  online: boolean;
  busyActionId?: string | null;
  busyActionIds?: ReadonlySet<string>;
  onRunAction: (action: AppRuntimeAction, input: Record<string, unknown>) => void;
}) {
  const [checked, setChecked] = useState(node.value === true);
  const disabled = actionDisabled(action, canRun, online, busyActionId, busyActionIds);
  return (
    <div className="flex items-center justify-between gap-3 rounded-md border border-border/70 px-3 py-2">
      <Label htmlFor={`app-runtime-${node.id}`}>{node.label || "切替"}</Label>
      <button
        id={`app-runtime-${node.id}`}
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => {
          const next = !checked;
          setChecked(next);
          if (action) onRunAction(action, { value: next });
        }}
        className="inline-flex h-7 items-center gap-2 rounded-full border border-border/70 px-3 text-xs disabled:opacity-50"
      >
        <span className={`size-2 rounded-full ${checked ? "bg-primary" : "bg-muted-foreground/40"}`} />
        {checked ? "有効" : "無効"}
      </button>
    </div>
  );
}

function RuntimeNode({
  node,
  actions,
  context,
  canRun,
  online,
  busyActionId,
  busyActionIds,
  onRunAction,
  onDownloadFile,
}: {
  node: AppRuntimeNode;
  actions: AppRuntimeAction[];
  context: AppRuntimeRenderContext;
  canRun: boolean;
  online: boolean;
  busyActionId?: string | null;
  busyActionIds?: ReadonlySet<string>;
  onRunAction: (action: AppRuntimeAction, input: Record<string, unknown>) => void;
  onDownloadFile?: (file: AppRuntimeFile) => void;
}) {
  const children = node.children || [];
  const action = actionForNode(node, actions);
  const disabled = actionDisabled(action, canRun, online, busyActionId, busyActionIds);
  const reason = actionReason(action, canRun, online);
  const value = valueForNode(node, context);

  switch (node.kind) {
    case "stack":
      return <div className="grid min-w-0 gap-3">{children.map((child) => <RuntimeNode key={child.id} node={child} actions={actions} context={context} canRun={canRun} online={online} busyActionId={busyActionId} busyActionIds={busyActionIds} onRunAction={onRunAction} onDownloadFile={onDownloadFile} />)}</div>;
    case "card":
      return <Card className="border-border/80 bg-card/80"><CardHeader className="border-b border-border/70">{node.label ? <CardTitle className="text-sm">{node.label}</CardTitle> : null}{node.text ? <p className="text-xs text-muted-foreground">{node.text}</p> : null}</CardHeader><CardContent className="space-y-3 p-4">{children.map((child) => <RuntimeNode key={child.id} {...{ node: child, actions, context, canRun, online, busyActionId, busyActionIds, onRunAction, onDownloadFile }} />)}</CardContent></Card>;
    case "text":
      return <p className="whitespace-pre-wrap text-sm leading-5" data-runtime-node={node.id}>{node.text ?? node.label ?? displayValue(value)}</p>;
    case "button":
      return <div className="flex flex-wrap items-center gap-2" data-runtime-node={node.id}><Button type="button" size="sm" disabled={disabled} onClick={() => { if (action) onRunAction(action, {}); }}><Play className="size-3.5" />{node.label || action?.label || "実行"}</Button>{busyActionId === action?.action_id ? <Loader2 className="size-3.5 animate-spin text-muted-foreground" /> : null}{reason ? <span className="text-xs text-muted-foreground">{reason}</span> : null}</div>;
    case "form":
      return <RuntimeFormNode {...{ node, action, canRun, online, busyActionId, busyActionIds, context, onRunAction }} />;
    case "select": {
      return <RuntimeSelectNode node={node} action={action} canRun={canRun} online={online} busyActionId={busyActionId} busyActionIds={busyActionIds} onRunAction={onRunAction} />;
    }
    case "switch":
      return <RuntimeSwitchNode node={node} action={action} canRun={canRun} online={online} busyActionId={busyActionId} busyActionIds={busyActionIds} onRunAction={onRunAction} />;
    case "list": {
      const items = Array.isArray(value) ? value : [];
      return <div className="space-y-2" data-runtime-node={node.id}>{node.label ? <h3 className="text-sm font-semibold">{node.label}</h3> : null}{items.length ? items.map((item, index) => <div key={`${node.id}-${index}`} className="rounded-md border border-border/70 bg-background/30 px-3 py-2 text-sm">{isRecord(item) ? displayValue(item.label ?? item.text ?? item) : displayValue(item)}</div>) : <p className="text-sm text-muted-foreground">表示する項目がありません。</p>}</div>;
    }
    case "table": {
      const rows = Array.isArray(value) ? value : [];
      const columns = node.columns || [];
      return <div className="overflow-x-auto rounded-md border border-border/70"><table className="w-full min-w-[24rem] text-left text-xs"><thead className="bg-muted/30"><tr>{columns.map((column) => <th key={column.name} className="px-3 py-2 font-semibold">{column.label}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={`${node.id}-${rowIndex}`} className="border-t border-border/60">{columns.map((column) => <td key={column.name} className="px-3 py-2 align-top">{displayValue(isRecord(row) ? row[column.name] : undefined)}</td>)}</tr>)}</tbody></table>{!rows.length ? <p className="p-3 text-xs text-muted-foreground">表示する行がありません。</p> : null}</div>;
    }
    case "detail": {
      const record = isRecord(value) ? value : {};
      const entries = Object.entries(record);
      return <dl className="grid gap-2 rounded-md border border-border/70 bg-background/30 p-3 sm:grid-cols-2" data-runtime-node={node.id}>{entries.length ? entries.map(([key, item]) => <div key={key} className="min-w-0"><dt className="text-[11px] text-muted-foreground">{key}</dt><dd className="mt-0.5 break-words text-sm">{displayValue(item)}</dd></div>) : <p className="text-xs text-muted-foreground">詳細情報がありません。</p>}</dl>;
    }
    case "progress": {
      const numeric = typeof value === "number" ? value : Number(value || 0);
      const dataRecord = isRecord(node.data) ? node.data : undefined;
      const max = Number(dataRecord?.max || 100);
      const percent = Math.max(0, Math.min(100, max > 0 ? (numeric / max) * 100 : 0));
      const unit = typeof dataRecord?.unit === "string" ? dataRecord.unit : "";
      return <div className="space-y-1.5" data-runtime-node={node.id}><div className="flex items-center justify-between gap-2 text-xs"><span>{node.label || "進捗"}</span><span className="text-muted-foreground">{displayValue(value)}{unit}</span></div><div className="h-2 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${percent}%` }} /></div></div>;
    }
    case "log":
      return <div className="space-y-1.5" data-runtime-node={node.id}><div className="flex items-center gap-2 text-xs font-semibold"><span>{node.label || "ログ"}</span>{context.run?.status ? <span className="font-normal text-muted-foreground">{context.run.status}</span> : null}</div><pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md border border-border/70 bg-muted/20 p-3 font-mono text-[11px] leading-5">{context.logs || displayValue(value)}</pre></div>;
    case "result":
      return <div className="space-y-1.5 rounded-md border border-border/70 bg-background/30 p-3" data-runtime-node={node.id}><div className="text-xs font-semibold">{node.label || "結果"}</div><pre className="max-h-80 overflow-auto whitespace-pre-wrap text-xs leading-5">{displayValue(value ?? context.run?.result)}</pre></div>;
    case "file": {
      const explicitFile = fileFromValue(value)
        || fileFromValue(node.data)
        || null;
      const referencedValue = typeof value === "string" ? value : typeof node.value === "string" ? node.value : null;
      const files = explicitFile
        ? [explicitFile]
        : (context.files || []).filter((item) => !referencedValue || item.artifact_id === referencedValue || item.filename === referencedValue || item.sha256 === referencedValue);
      return <div className="space-y-2" data-runtime-node={node.id}>{files.length ? files.map((file) => <div key={file.artifact_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border/70 bg-background/30 px-3 py-2"><div className="flex min-w-0 items-center gap-2"><FileOutput className="size-4 text-muted-foreground" /><span className="truncate text-sm">{file.filename}</span></div>{onDownloadFile ? <Button type="button" size="sm" variant="outline" onClick={() => onDownloadFile(file)}><Download className="size-3.5" />取得</Button> : null}</div>) : <p className="rounded-md border border-dashed border-border/70 px-3 py-2 text-xs text-muted-foreground">取得できるファイルはありません。</p>}</div>;
    }
    default:
      return <div className="grid gap-3">{children.map((child) => <RuntimeNode key={child.id} {...{ node: child, actions, context, canRun, online, busyActionId, busyActionIds, onRunAction, onDownloadFile }} />)}</div>;
  }
}

export function AppRuntimeRenderer({ ui, actions, context = {}, canRun, online = true, busyActionId, busyActionIds, onRunAction, onDownloadFile }: AppRuntimeRendererProps) {
  return <div className="grid min-w-0 gap-4" data-runtime-schema-version={ui.schema_version}><RuntimeNode node={ui.root} actions={actions} context={context} canRun={canRun} online={online} busyActionId={busyActionId} busyActionIds={busyActionIds} onRunAction={onRunAction} onDownloadFile={onDownloadFile} /></div>;
}

