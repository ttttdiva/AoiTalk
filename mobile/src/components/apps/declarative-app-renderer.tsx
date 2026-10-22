import React, { useCallback, useMemo, useState } from "react";
import {
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import {
  Button,
  Chip,
  Divider,
  Menu,
  ProgressBar,
  Surface,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import type {
  AppRuntimeAction,
  AppRuntimeFileRef,
  AppRuntimeFormField,
  AppRuntimeNode,
  AppRuntimeOption,
  AppRuntimeRun,
  AppRuntimeUi,
} from "../../lib/apps-api";

const TEXT = "#cdd6f4";
const MUTED = "#a6adc8";
const SURFACE = "#1e1e2e";
const BORDER = "#313244";
const ERROR = "#f38ba8";
const TERMINAL_RUN_STATUSES = new Set(["succeeded", "failed", "cancelled", "blocked", "rejected", "timed_out", "completed"]);

export function isRuntimeRunTerminal(status: string | undefined): boolean {
  return Boolean(status && TERMINAL_RUN_STATUSES.has(status.toLowerCase()));
}

export function runtimeRunNeedsStop(run: AppRuntimeRun): boolean {
  return ["queued", "dispatching", "accepted", "starting", "running", "stopping"].includes(run.status.toLowerCase());
}

export function formatRuntimeRunState(run: AppRuntimeRun): string {
  const exit = run.exit_code === null || run.exit_code === undefined ? "未確定" : String(run.exit_code);
  const release = run.release_id ? ` · release ${run.release_id}` : "";
  return `状態 ${run.status} · mode ${run.mode || "unknown"} · readiness ${run.readiness_status || "unknown"} · process ${run.process_status || "unknown"} · exit ${exit}${release}`;
}

export function explainRuntimeRun(run: AppRuntimeRun): string | null {
  const status = run.status.toLowerCase();
  if (status === "unknown") return "実行状態を確定できません。再送せず、同じApp・Project範囲で再読込してください。";
  if (status === "blocked" || status === "rejected") return run.error ? displayValue(run.error) : "このActionはサーバー側で停止または拒否されています。";
  if (status === "failed" || status === "timed_out") return run.error ? displayValue(run.error) : "このActionは正常に完了しませんでした。";
  return null;
}

export type RuntimeFileSelection = {
  /** Opaque, owner/run-scoped server identifier. Never a local URI. */
  file_id: string;
  name?: string;
  size_bytes?: number | null;
  content_type?: string | null;
};

function serializeActionInput(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(serializeActionInput);
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    // File references are intentionally reduced to the one opaque key the
    // server accepts.  Names, sizes and local URIs never cross the Action
    // request boundary.
    if (typeof record.file_id === "string") return { file_id: record.file_id };
    return Object.fromEntries(Object.entries(record).map(([key, item]) => [key, serializeActionInput(item)]));
  }
  return value;
}

export type DeclarativeAppRendererProps = {
  ui: AppRuntimeUi | null | undefined;
  /** Release identity is part of the form state boundary. */
  releaseKey?: string | null;
  actions: AppRuntimeAction[];
  runs?: AppRuntimeRun[];
  canRun?: boolean;
  online: boolean;
  busyActionId?: string | null;
  activeActionIds?: readonly string[];
  onRun: (actionId: string, input: Record<string, unknown>) => Promise<void>;
  /** The host owns the authenticated file upload and returns an opaque ID. */
  onPickFile?: (field: AppRuntimeFormField, actionId?: string) => Promise<RuntimeFileSelection | null>;
  onOpenFile?: (file: AppRuntimeFileRef) => void;
};

type RuntimeSnapshot = {
  runs: AppRuntimeRun[];
  latestRun?: AppRuntimeRun;
};

function readPath(value: unknown, path?: string): unknown {
  if (!path) return undefined;
  const parts = path.split(".");
  if (parts[0] !== "run" || !/^run\.(?:result|status)(?:\.[A-Za-z][A-Za-z0-9_-]*)*$/.test(path)) return undefined;
  return parts.reduce<unknown>((node, part) => {
    if (node && typeof node === "object" && part in node) {
      return (node as Record<string, unknown>)[part];
    }
    return undefined;
  }, value);
}

function resolveNodeValue(node: AppRuntimeNode, snapshot: RuntimeSnapshot): unknown {
  const fromPath = readPath({ run: snapshot.latestRun }, node.value_path ?? undefined);
  if (fromPath !== undefined) return fromPath;
  if (node.value !== undefined) return node.value;
  if (node.data !== undefined) return node.data;
  return undefined;
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function optionsFor(node: AppRuntimeNode): AppRuntimeOption[] {
  return Array.isArray(node.options) ? node.options : [];
}

function actionFor(node: AppRuntimeNode, actions: Map<string, AppRuntimeAction>): AppRuntimeAction | null {
  const id = typeof node.action_id === "string" ? node.action_id : null;
  return id ? actions.get(id) ?? null : null;
}

function ActionButton({
  node,
  action,
  canRun,
  online,
  busy,
  active,
  onRun,
}: {
  node: AppRuntimeNode;
  action: AppRuntimeAction | null;
  canRun: boolean;
  online: boolean;
  busy: boolean;
  active: boolean;
  onRun: (actionId: string, input: Record<string, unknown>) => Promise<void>;
}) {
  const label = node.label || node.text || action?.label || "実行";
  const disabled = !canRun || !online || busy || active || !action || !action.available;
  const reason = !canRun
    ? "このAppを実行する権限がありません"
    : !online
    ? "オフラインでは実行できません"
      : active
      ? "このActionは実行中です"
      : action && !action.available
      ? action.unavailable_reason || "現在は利用できません"
      : !action
        ? "このActionは利用できません"
        : null;
  return (
    <View style={styles.actionBlock}>
      <Button
        mode="contained-tonal"
        disabled={disabled}
        loading={busy}
        onPress={() => {
          if (action) void onRun(action.action_id, {});
        }}
        accessibilityLabel={label}
      >
        {label}
      </Button>
      {reason ? <Text style={styles.disabledText}>{reason}</Text> : null}
    </View>
  );
}

function SelectField({
  label,
  value,
  options,
  disabled,
  onChange,
}: {
  label: string;
  value: string;
  options: AppRuntimeOption[];
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const [visible, setVisible] = useState(false);
  const selected = options.find((option) => option.value === value);
  return (
    <View style={styles.fieldBlock}>
      <Text style={styles.fieldLabel}>{label}</Text>
      <Menu
        visible={visible}
        onDismiss={() => setVisible(false)}
        anchor={
          <Button
            mode="outlined"
            disabled={disabled}
            onPress={() => setVisible(true)}
            contentStyle={styles.selectButtonContent}
          >
            {selected?.label || "選択してください"}
          </Button>
        }
      >
        {options.map((option) => (
          <Menu.Item
            key={option.value}
            title={option.label}
            leadingIcon={option.value === value ? "check" : undefined}
            onPress={() => {
              setVisible(false);
              onChange(option.value);
            }}
          />
        ))}
      </Menu>
    </View>
  );
}

function RuntimeForm({
  node,
  action,
  canRun,
  online,
  busy,
  active,
  onRun,
  onPickFile,
}: {
  node: AppRuntimeNode;
  action: AppRuntimeAction | null;
  canRun: boolean;
  online: boolean;
  busy: boolean;
  active: boolean;
  onRun: (actionId: string, input: Record<string, unknown>) => Promise<void>;
  onPickFile?: (field: AppRuntimeFormField, actionId?: string) => Promise<RuntimeFileSelection | null>;
}) {
  const fields = Array.isArray(node.fields) ? node.fields : [];
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);
  const disabled = !canRun || !online || busy || active || !action || !action.available;
  const setValue = (name: string, value: unknown) => {
    setValues((current) => ({ ...current, [name]: value }));
    setError(null);
  };
  const submit = () => {
    if (!action || disabled) return;
    const missing = fields.find((field) => field.required && (values[field.name] === undefined || values[field.name] === ""));
    if (missing) {
      setError(`${missing.label || missing.name} を入力してください`);
      return;
    }
    void onRun(action.action_id, serializeActionInput(values) as Record<string, unknown>);
  };
  return (
    <Surface style={styles.card} elevation={1}>
      {node.label ? <Text variant="titleMedium" style={styles.cardTitle}>{node.label}</Text> : null}
      {fields.map((field) => {
        const value = values[field.name];
        if (field.kind === "select") {
          return (
            <SelectField
              key={field.name}
              label={field.label || field.name}
              value={typeof value === "string" ? value : ""}
              options={field.options || []}
              disabled={disabled}
              onChange={(next) => setValue(field.name, next)}
            />
          );
        }
        if (field.kind === "switch") {
          return (
            <View key={field.name} style={styles.switchRow}>
              <Text style={styles.fieldLabel}>{field.label || field.name}</Text>
              <Switch value={value === true} disabled={disabled} onValueChange={(next) => setValue(field.name, next)} />
            </View>
          );
        }
        if (field.kind === "file") {
          const file = value && typeof value === "object" ? value as RuntimeFileSelection : null;
          return (
            <View key={field.name} style={styles.fieldBlock}>
              <Text style={styles.fieldLabel}>{field.label || field.name}</Text>
              <Button
                mode="outlined"
                disabled={disabled || !onPickFile}
                icon="paperclip"
                onPress={() => {
                  if (!onPickFile) return;
                    void onPickFile(field, action?.action_id).then((picked) => {
                    if (picked) setValue(field.name, picked);
                  }).catch((cause) => setError(cause instanceof Error ? cause.message : "ファイルを選択できませんでした"));
                }}
              >
                {file?.name || "ファイルを選択"}
              </Button>
              {!onPickFile ? <Text style={styles.disabledText}>ファイル入力はサーバーのopaque file ID接続後に利用できます</Text> : null}
            </View>
          );
        }
        return (
          <TextInput
            key={field.name}
            mode="outlined"
            label={field.label || field.name}
            value={value === undefined || value === null ? "" : String(value)}
            keyboardType={field.kind === "number" ? "numeric" : "default"}
            disabled={disabled}
            onChangeText={(next) => setValue(field.name, field.kind === "number" && next !== "" ? Number(next) : next)}
            style={styles.input}
          />
        );
      })}
      {error ? <Text accessibilityRole="alert" style={styles.error}>{error}</Text> : null}
      <ActionButton node={{ ...node, label: node.label || "実行" }} action={action} canRun={canRun} online={online} busy={busy} active={active} onRun={async () => submit()} />
    </Surface>
  );
}

function renderArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function fileFromValue(value: unknown): AppRuntimeFileRef | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const id = record.file_id ?? record.artifact_id ?? record.id;
  return typeof id === "string" && id.length > 0
    ? {
      id,
      ...(typeof record.file_id === "string" ? { file_id: record.file_id } : {}),
      ...(typeof record.artifact_id === "string" ? { artifact_id: record.artifact_id } : {}),
      ...(typeof record.filename === "string" ? { filename: record.filename } : {}),
      ...(record.direction === "input" || record.direction === "output" ? { direction: record.direction } : {}),
      ...(typeof record.run_id === "string" ? { run_id: record.run_id } : {}),
    } satisfies AppRuntimeFileRef
    : null;
}

function RuntimeFileNode({
  node,
  action,
  value,
  snapshot,
  canRun,
  online,
  busy,
  active,
  onRun,
  onPickFile,
  onOpenFile,
}: {
  node: AppRuntimeNode;
  action: AppRuntimeAction | null;
  value: unknown;
  snapshot: RuntimeSnapshot;
  canRun: boolean;
  online: boolean;
  busy: boolean;
  active: boolean;
  onRun: (actionId: string, input: Record<string, unknown>) => Promise<void>;
  onPickFile?: (field: AppRuntimeFormField, actionId?: string) => Promise<RuntimeFileSelection | null>;
  onOpenFile?: (file: AppRuntimeFileRef) => void;
}) {
  const initialFile = fileFromValue(value);
  const [selected, setSelected] = useState<RuntimeFileSelection | null>(initialFile ? {
    file_id: initialFile.file_id || initialFile.id,
    name: initialFile.filename,
  } : null);
  const fileDescriptor = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
  const isInput = fileDescriptor?.kind === "input"
    || fileDescriptor?.direction === "input";
  const declaredFile = isInput ? null : fileFromValue(value);
  const outputFiles: AppRuntimeFileRef[] = isInput
    ? []
    : declaredFile
      ? [declaredFile]
      : (snapshot.latestRun?.files ?? []);
  const disabled = !canRun || !online || busy || active || !action || !action.available;
  const field: AppRuntimeFormField = {
    name: node.name || node.id,
    label: node.label || "ファイル",
    kind: "file",
    required: true,
  };
  return (
    <View style={styles.file}>
      <Text style={styles.body}>{node.label || selected?.name || (outputFiles.length === 1 ? outputFiles[0].filename : undefined) || "ファイル"}</Text>
      {isInput ? (
        <>
          <Button
            mode="outlined"
            icon="paperclip"
            disabled={disabled || active || !onPickFile}
            onPress={() => {
              if (!onPickFile) return;
              void onPickFile(field, action?.action_id).then((picked) => {
                if (picked) setSelected(picked);
              });
            }}
          >
            {selected?.name || "ファイルを選択"}
          </Button>
          {selected && action ? (
            <ActionButton
              node={{ ...node, label: node.label || action.label || "実行" }}
              action={action}
              canRun={canRun}
              online={online}
              busy={busy}
              active={active}
              onRun={async (actionId) => onRun(actionId, { [field.name]: { file_id: selected.file_id } })}
            />
          ) : null}
          {!onPickFile ? <Text style={styles.disabledText}>ファイル入力を利用できません</Text> : null}
        </>
      ) : outputFiles.length ? (
        <View style={styles.fileList}>
          {outputFiles.map((file) => (
            <Button key={file.id} mode="outlined" disabled={!onOpenFile} onPress={() => onOpenFile?.(file)}>
              {file.filename || file.id} · 取得して共有
            </Button>
          ))}
        </View>
      ) : (
        <Text style={styles.muted}>実行結果にファイルがありません</Text>
      )}
    </View>
  );
}

function RuntimeNodeView({
  node,
  actions,
  snapshot,
  online,
  canRun,
  busyActionId,
  activeActionIds,
  onRun,
  onPickFile,
  onOpenFile,
}: {
  node: AppRuntimeNode;
  actions: Map<string, AppRuntimeAction>;
  snapshot: RuntimeSnapshot;
  online: boolean;
  canRun: boolean;
  busyActionId?: string | null;
  activeActionIds: ReadonlySet<string>;
  onRun: (actionId: string, input: Record<string, unknown>) => Promise<void>;
  onPickFile?: (field: AppRuntimeFormField, actionId?: string) => Promise<RuntimeFileSelection | null>;
  onOpenFile?: (file: AppRuntimeFileRef) => void;
}) {
  const action = actionFor(node, actions);
  const actionActive = Boolean(action && activeActionIds.has(action.action_id));
  const value = resolveNodeValue(node, snapshot);
  const children = node.children || [];
  if (node.kind === "stack") {
    return (
      <View style={styles.stack}>
        {children.map((child) => (
          <RuntimeNodeView
            key={child.id}
            node={child}
            actions={actions}
            snapshot={snapshot}
            online={online}
            canRun={canRun}
            busyActionId={busyActionId}
            activeActionIds={activeActionIds}
            onRun={onRun}
            onPickFile={onPickFile}
            onOpenFile={onOpenFile}
          />
        ))}
      </View>
    );
  }
  if (node.kind === "card") {
    return (
      <Surface style={styles.card} elevation={1}>
        {node.label ? <Text variant="titleMedium" style={styles.cardTitle}>{node.label}</Text> : null}
        {children.map((child) => (
          <RuntimeNodeView
            key={child.id}
            node={child}
            actions={actions}
            snapshot={snapshot}
            online={online}
            canRun={canRun}
            busyActionId={busyActionId}
            activeActionIds={activeActionIds}
            onRun={onRun}
            onPickFile={onPickFile}
            onOpenFile={onOpenFile}
          />
        ))}
      </Surface>
    );
  }
  if (node.kind === "text") {
    return <Text style={styles.body}>{node.text || node.label || displayValue(value)}</Text>;
  }
  if (node.kind === "button") {
    return <ActionButton node={node} action={action} canRun={canRun} online={online} busy={busyActionId === action?.action_id} active={actionActive} onRun={onRun} />;
  }
  if (node.kind === "form") {
    return <RuntimeForm node={node} action={action} canRun={canRun} online={online} busy={busyActionId === action?.action_id} active={actionActive} onRun={onRun} onPickFile={onPickFile} />;
  }
  if (node.kind === "select") {
    const optionList = optionsFor(node);
    return <SelectField label={node.label || "選択"} value={typeof value === "string" ? value : ""} options={optionList} disabled={!online || !canRun || !action || actionActive} onChange={(next) => { if (action) void onRun(action.action_id, { value: next }); }} />;
  }
  if (node.kind === "switch") {
    return <View style={styles.switchRow}><Text style={styles.fieldLabel}>{node.label || "設定"}</Text><Switch disabled={!online || !canRun || !action || actionActive} value={value === true} onValueChange={(next) => { if (action) void onRun(action.action_id, { value: next }); }} /></View>;
  }
  if (node.kind === "list") {
    return <View style={styles.list}>{renderArray(value).map((item, index) => <Text key={`${node.id}-${index}`} style={styles.listRow}>{displayValue(item)}</Text>)}</View>;
  }
  if (node.kind === "table") {
    const rows = renderArray(value).filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)));
    const columns = node.columns || [];
    return <ScrollView horizontal style={styles.tableScroll}><View><View style={styles.tableRow}>{columns.map((column) => <Text key={column.name} style={styles.tableHead}>{column.label}</Text>)}</View>{rows.map((row, index) => <View key={`${node.id}-${index}`} style={styles.tableRow}>{columns.map((column) => <Text key={column.name} style={styles.tableCell}>{displayValue(row[column.name])}</Text>)}</View>)}</View></ScrollView>;
  }
  if (node.kind === "detail") {
    const record = value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
    return <View style={styles.detail}>{Object.entries(record).map(([key, item]) => <View key={key} style={styles.detailRow}><Text style={styles.detailKey}>{key}</Text><Text style={styles.detailValue}>{displayValue(item)}</Text></View>)}</View>;
  }
  if (node.kind === "progress") {
    const numeric = typeof value === "number" ? value : Number(value || 0);
    const progress = numeric > 1 ? Math.min(1, numeric / 100) : Math.max(0, numeric);
    return <View style={styles.progress}><Text style={styles.muted}>{node.label || "進行状況"} {Math.round(progress * 100)}%</Text><ProgressBar progress={progress} color="#7c3aed" /></View>;
  }
  if (node.kind === "log") {
    return <Surface style={styles.log} elevation={0}><Text selectable style={styles.logText}>{displayValue(value || snapshot.latestRun?.result || "ログはありません")}</Text></Surface>;
  }
  if (node.kind === "result") {
    return <Surface style={styles.result} elevation={0}><Text style={styles.body}>{displayValue(value || snapshot.latestRun?.result || "結果はまだありません")}</Text></Surface>;
  }
  if (node.kind === "file") {
    return <RuntimeFileNode node={node} action={action} value={value} snapshot={snapshot} canRun={canRun} online={online} busy={busyActionId === action?.action_id} active={actionActive} onRun={onRun} onPickFile={onPickFile} onOpenFile={onOpenFile} />;
  }
  return <Text style={styles.muted}>未対応のUI要素: {node.kind}</Text>;
}

export function DeclarativeAppRenderer({
  ui,
  releaseKey,
  actions,
  runs = [],
  canRun = true,
  online,
  busyActionId,
  activeActionIds = [],
  onRun,
  onPickFile,
  onOpenFile,
}: DeclarativeAppRendererProps) {
  const actionMap = useMemo(() => new Map(actions.map((action) => [action.action_id, action])), [actions]);
  const activeActionIdSet = useMemo(() => new Set(activeActionIds), [activeActionIds]);
  const snapshot = useMemo<RuntimeSnapshot>(() => ({ runs, latestRun: runs[0] }), [runs]);
  const renderNode = useCallback((node: AppRuntimeNode) => (
    <RuntimeNodeView
      key={`${releaseKey || "runtime"}:${node.id}`}
      node={node}
      actions={actionMap}
      snapshot={snapshot}
      online={online}
      canRun={canRun}
      busyActionId={busyActionId}
      activeActionIds={activeActionIdSet}
      onRun={onRun}
      onPickFile={onPickFile}
      onOpenFile={onOpenFile}
    />
  ), [actionMap, activeActionIdSet, busyActionId, canRun, onOpenFile, onPickFile, onRun, online, releaseKey, snapshot]);
  if (!ui?.root) return null;
  return (
    <View style={styles.container} testID="declarative-app-runtime">
      {!online ? <Text style={styles.offline}>オフライン: AppのAction実行は利用できません。再接続後に手動で実行してください。</Text> : !canRun ? <Text style={styles.disabledText}>このAppを実行する権限がありません。</Text> : null}
      {renderNode(ui.root)}
      {runs.length > 0 ? <>
        <Divider style={styles.divider} />
        <Text style={styles.sectionLabel}>直近の実行</Text>
        {runs.slice(0, 5).map((run) => <View key={run.id} style={styles.runRow}><View style={styles.runCopy}><Text style={styles.body}>{run.action_id || "Action"}</Text><Text style={styles.muted}>{formatRuntimeRunState(run)}</Text></View><Chip compact>{run.status}</Chip></View>)}
      </> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { gap: 10 },
  stack: { gap: 10 },
  card: { backgroundColor: SURFACE, borderRadius: 14, padding: 14, gap: 10 },
  cardTitle: { color: TEXT },
  body: { color: TEXT, lineHeight: 20 },
  muted: { color: MUTED, fontSize: 12, lineHeight: 18 },
  offline: { color: "#f9e2af", backgroundColor: "#3b3341", padding: 10, borderRadius: 8, fontSize: 12 },
  disabledText: { color: MUTED, fontSize: 11, lineHeight: 16 },
  actionBlock: { gap: 4 },
  fieldBlock: { gap: 5 },
  fieldLabel: { color: TEXT, fontWeight: "600" },
  input: { backgroundColor: SURFACE },
  selectButtonContent: { justifyContent: "space-between" },
  switchRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
  error: { color: ERROR, fontSize: 12 },
  list: { gap: 4 },
  listRow: { color: TEXT, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: BORDER, paddingVertical: 6 },
  tableScroll: { borderWidth: StyleSheet.hairlineWidth, borderColor: BORDER, borderRadius: 8 },
  tableRow: { flexDirection: "row", borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: BORDER },
  tableHead: { color: TEXT, fontWeight: "700", minWidth: 120, padding: 8 },
  tableCell: { color: TEXT, minWidth: 120, padding: 8 },
  detail: { gap: 6 },
  detailRow: { flexDirection: "row", justifyContent: "space-between", gap: 10 },
  detailKey: { color: MUTED, flexShrink: 1 },
  detailValue: { color: TEXT, flex: 1, textAlign: "right" },
  progress: { gap: 5 },
  log: { backgroundColor: "#0b0b12", borderRadius: 8, padding: 10 },
  logText: { color: "#bac2de", fontFamily: "monospace", fontSize: 12 },
  result: { backgroundColor: "#181825", borderRadius: 8, padding: 10 },
  file: { gap: 6 },
  fileList: { gap: 6 },
  divider: { backgroundColor: BORDER },
  sectionLabel: { color: MUTED, fontWeight: "700", fontSize: 12 },
  runRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: BORDER, paddingVertical: 6 },
  runCopy: { flex: 1, gap: 2, paddingRight: 8 },
});
