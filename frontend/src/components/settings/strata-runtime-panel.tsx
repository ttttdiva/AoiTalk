"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { LocalRuntimeStatus, LocalRuntimeTask, StrataSetupRequest } from "./llm-model-section-types";

const STARTUP_PHASE_LABELS = {
  loading: "読込準備中",
  reading_weights: "重みを読込中",
  allocating_cache: "キャッシュを確保中",
  initializing: "初期化中",
} as const;

function startupProgressText(status: LocalRuntimeStatus | null): string | null {
  const server = status?.server;
  if (!server || server.ready === true) return null;
  const progress = server.startup_progress;
  if (!progress || !Number.isFinite(progress.elapsed_s) || progress.elapsed_s < 0) return null;
  const phase = STARTUP_PHASE_LABELS[progress.phase];
  return phase ? `${phase} · 経過 ${progress.elapsed_s}秒` : null;
}

/** Setup is an explicit admin action. Starting chat never triggers installation. */
export function StrataRuntimePanel({ status, task, busy, onAction, onCancel }: {
  status: LocalRuntimeStatus | null;
  task: LocalRuntimeTask | null;
  busy: boolean;
  onAction: (request: StrataSetupRequest) => Promise<void>;
  onCancel?: () => Promise<void>;
}) {
  const settings = status?.strata;
  const [sourceRoot, setSourceRoot] = useState("");
  const [dataDir, setDataDir] = useState("");
  const [ggufDir, setGgufDir] = useState("");
  const [context, setContext] = useState(settings?.context_size ?? 32768);
  const [vision, setVision] = useState(settings?.vision ?? "none");
  const [port, setPort] = useState(settings?.port ?? 8080);
  const [managed, setManaged] = useState(settings?.auto_start ?? true);
  const [baseUrl, setBaseUrl] = useState(status?.server.base_url ?? "http://127.0.0.1:8080/v1");
  const [confirmed, setConfirmed] = useState(false);
  const [validationError, setValidationError] = useState("");
  const operatorOnly = settings?.operator_only === true;
  const activeTask = task && !task.done ? task : null;
  const active = activeTask ?? status;
  const server = status?.server;
  const serverLabel = server?.ready === true
    ? "ready"
    : server?.status === "loading"
      ? "モデル未ロード／読込待ち"
    : server?.status && server.status !== "ready"
      ? server.status
      : server
        ? "loading"
        : "未確認";
  const setupComplete = Boolean(status?.done && status.prepared);
  const startupProgress = startupProgressText(status);
  const showRuntimeStatus = Boolean(
    (busy && activeTask) || startupProgress || serverLabel === "モデル未ロード／読込待ち" || serverLabel === "not_checked",
  );
  const change = () => setConfirmed(false);

  async function submit(action: "setup" | "configure") {
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      setValidationError("Portは1〜65535の整数で指定してください");
      return;
    }
    if (action === "setup" && !dataDir.trim()) {
      setValidationError("大容量データの保存先を指定してください");
      return;
    }
    setValidationError("");
    setConfirmed(false);
    await onAction({
      action,
      setup_confirmed: action === "setup" && confirmed,
      strata: {
        ...(sourceRoot.trim() ? { source_root: sourceRoot.trim() } : {}),
        ...(dataDir.trim() ? { data_dir: dataDir.trim() } : {}),
        ...(ggufDir.trim() ? { gguf_dir: ggufDir.trim() } : {}),
        context_size: context,
        vision,
        port,
        auto_start: managed,
      },
      ...(!managed ? { base_url: baseUrl.trim() } : {}),
    });
  }

  return (
    <section className="space-y-3 rounded-md border border-sky-500/30 p-3 text-xs" aria-label="Strata runtime" aria-busy={busy}>
      <h3 className="font-semibold">Strata · {setupComplete ? "準備済み" : active?.status ?? "確認中"}</h3>
      <p aria-live="polite">Server: {server?.auto_start_on_send ? "停止中（送信時に自動起動）" : serverLabel} · Context: {server?.context_size ?? settings?.context_size ?? "未取得"} · Vision: {server?.vision ? "ON" : "OFF"}</p>
      <p className="text-muted-foreground">OpenAI互換API /v1 · healthと選択モデルの/v1/models一致で起動を確認します</p>
      {settings?.source_url && <a href={settings.source_url} target="_blank" rel="noreferrer" className="underline">公式Strata固定revision {settings.source_revision?.slice(0, 12)}</a>}
      {operatorOnly ? (
        <p>EnterpriseのStrataは外部operator管理です。NVIDIAホスト等のLAN endpointはEnterprise外部接続設定で指定してください</p>
      ) : (
        <>
        <fieldset disabled={busy} className="space-y-3">
          <label className="flex items-center gap-2"><Checkbox checked={managed} onCheckedChange={(checked) => { setManaged(checked === true); change(); }} />Personalで起動・停止を管理する</label>
          {!managed ? (
            <label className="block space-y-1">外部API endpoint<Input aria-label="Strata external endpoint" value={baseUrl} onChange={(event) => { setBaseUrl(event.target.value); change(); }} /></label>
          ) : (
            <>
              <p>{settings?.setup_notice ?? "公式setupはモデル約66〜84GB、MTP約6GB、画像約1GB、Q2_0/AVX-512では追加約40GBを使用します。統合GPUは非対応です"}</p>
              <label className="block space-y-1">Source folder（空欄は管理領域へ固定source取得）<Input aria-label="Strata source folder" value={sourceRoot} onChange={(event) => { setSourceRoot(event.target.value); change(); }} /></label>
              <label className="block space-y-1">大容量データ保存先（repository外）<Input aria-label="Strata data folder" placeholder="例: <configured-model-root>\strata" value={dataDir} onChange={(event) => { setDataDir(event.target.value); change(); }} /></label>
              <label className="block space-y-1">既存GGUF folder（任意・全shard、空欄は保存済み設定を保持）<Input aria-label="Strata GGUF folder" value={ggufDir} onChange={(event) => { setGgufDir(event.target.value); change(); }} /></label>
              <p className="text-muted-foreground">GGUF指定でも未準備のMTP・依存があれば公式setupが取得します。完全offlineでは準備済みsource/engine/tokenizer/MTP/visionも配置してください</p>
              <div className="grid gap-2 sm:grid-cols-3">
                <label className="space-y-1">Context<Select value={String(context)} onValueChange={(value) => { if (value) { setContext(Number(value)); change(); } }}><SelectTrigger aria-label="Strata context" className="w-full"><SelectValue /></SelectTrigger><SelectContent>{[8192, 32768, 65536, 131072, 262144].map((value) => <SelectItem key={value} value={String(value)}>{value}</SelectItem>)}</SelectContent></Select></label>
                <label className="space-y-1">Vision<Select value={vision} onValueChange={(value) => { if (value === "none" || value === "cpu" || value === "gpu") { setVision(value); change(); } }}><SelectTrigger aria-label="Strata vision" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="none">OFF</SelectItem><SelectItem value="cpu">CPU（AMD HIP）</SelectItem><SelectItem value="gpu">GPU（NVIDIA）</SelectItem></SelectContent></Select></label>
                <label className="space-y-1">Port<Input aria-label="Strata port" type="number" value={port} min={1} max={65535} onChange={(event) => { setPort(Number(event.target.value)); change(); }} /></label>
              </div>
              <label className="flex items-start gap-2"><Checkbox checked={confirmed} onCheckedChange={(checked) => setConfirmed(checked === true)} />公式setupによるソフトウェア導入と大容量downloadを確認しました（Windows: START-HERE.bat / Linux: setup.sh）</label>
            </>
          )}
          <p className="text-muted-foreground">Context/Visionの変更は公式setupで適用します。既に準備したoffline runtimeは設定保存で接続できます</p>
          <div className="flex flex-wrap gap-2">
            {managed && <Button type="button" size="sm" variant="outline" onClick={() => void submit("configure")}>設定保存 / 準備済みruntimeへ接続</Button>}
            <Button type="button" size="sm" disabled={managed && (!confirmed || !dataDir.trim())} onClick={() => void submit(managed ? "setup" : "configure")}>{managed ? "初回セットアップ / 再試行" : "外部接続設定を保存"}</Button>
            {settings?.auto_start && status?.prepared && <Button type="button" size="sm" variant="outline" onClick={() => void onAction({ action: "start" })}>起動</Button>}
            {settings?.auto_start && status?.server.ready && <Button type="button" size="sm" variant="outline" onClick={() => void onAction({ action: "stop" })}>停止</Button>}
          </div>
        </fieldset>
        {!operatorOnly && busy && task?.task_id && !task.done && task.actions?.cancel !== false && onCancel && (
          <Button type="button" size="sm" variant="outline" onClick={() => void onCancel()}>
            キャンセル
          </Button>
        )}
        </>
      )}
      {showRuntimeStatus && <p role="status" aria-live="polite">
        {busy && activeTask?.phase
          ? `${activeTask.phase}${activeTask.status ? ` · ${activeTask.status}` : ""} 実行中。`
          : startupProgress
            ? `Strata ${startupProgress}。`
          : `Server: ${serverLabel}。`}
        {busy && " 公式setupの進捗は logs/models/strata-setup.log に記録します"}
      </p>}
      {(validationError || active?.error) && <p role="alert" className="text-destructive">{validationError || active?.reason || active?.error}</p>}
      {active?.status === "failed" && <p>部分downloadは公式setupが再利用します。同じ保存先で再試行してください</p>}
    </section>
  );
}
