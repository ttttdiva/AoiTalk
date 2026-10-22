"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";
import { operationsApi, type MediaActionPackagePreview, type OperationsAction } from "@/lib/operations-api";

const reasonLabels: Record<string, string> = {
  unsupported_artifact_reference: "商品実体への対応が未登録です",
  artifact_content_unavailable: "登録済み素材または実体が見つかりません",
  generation_output_artifact_binding_unavailable: "生成出力から素材実体への対応が未登録です",
  artifact_hash_not_bound_to_action: "素材のhashが凍結Actionに含まれていません",
  package_total_size_exceeded: "素材合計25 MiBの上限を超えるため収録できません",
};

function packageError(error: unknown): string {
  const status = typeof error === "object" && error !== null && "status" in error ? error.status : null;
  const detail = error instanceof Error ? error.message : "素材の取得に失敗しました";
  if (status === 403) return `素材を閲覧する権限がありません。${detail}`;
  if (status === 409) return `Actionまたは素材が変更されました。Actionと素材を再読込してください。${detail}`;
  return detail;
}

/** The keyed child discards every preview and in-flight download on snapshot changes. */
export function ActionPackagePanel({ action }: { action: OperationsAction }) {
  if (!["dlsite", "fanza"].includes(String(action.payload?.platform))) return null;
  return <PackageSnapshotPanel key={`${action.id}:${action.action_version}:${action.payload_hash}`} action={action} />;
}

function PackageSnapshotPanel({ action }: { action: OperationsAction }) {
  const [preview, setPreview] = useState<MediaActionPackagePreview | null>(null);
  const [busy, setBusy] = useState<"preview" | "download" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloadStarted, setDownloadStarted] = useState(false);
  const mounted = useRef(false);
  const request = useRef<AbortController | null>(null);
  const version = action.action_version;
  const payloadHash = action.payload_hash;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; request.current?.abort(); };
  }, []);

  const run = async (operation: "preview" | "download") => {
    if (request.current || !version || !payloadHash) return;
    const controller = new AbortController();
    request.current = controller;
    const active = () => mounted.current && !controller.signal.aborted && request.current === controller;
    setBusy(operation);
    setError(null);
    setDownloadStarted(false);
    if (operation === "preview") setPreview(null);
    try {
      if (operation === "preview") {
        const result = await operationsApi.getActionPackage(action.id, version, payloadHash, controller.signal);
        if (!active()) return;
        const binding = result.manifest.action;
        if (binding.id !== action.id || binding.action_version !== version || binding.payload_hash !== payloadHash) {
          throw Object.assign(new Error("Package snapshot does not match the selected Action"), { status: 409 });
        }
        setPreview(result);
      } else if (preview) {
        const blob = await operationsApi.downloadActionPackage(action.id, version, payloadHash, preview.manifest_sha256, controller.signal);
        if (!active()) return;
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = preview.filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 0);
        setDownloadStarted(true);
      }
    } catch (failure) {
      if (active()) { setPreview(null); setError(packageError(failure)); }
    } finally {
      if (active()) { request.current = null; setBusy(null); }
    }
  };

  const manifest = preview?.manifest;
  const available = manifest?.assets.filter((asset) => asset.status === "included") ?? [];
  const fileCount = new Set(available.map((asset) => asset.archive_path || asset.filename)).size;
  return (
    <Card size="sm" data-testid="operations-action-package">
      <CardHeader><CardTitle className="inline-flex items-center gap-1 text-sm">手動投稿用の素材<HelpPopover label="手動投稿用の素材"><p>凍結Actionの素材とmanifestを取得します。品質・利用権・公開承認は別途確認してください。</p><p>素材の合計上限は25 MiBです。ZIPにはmanifestも含まれます。取得だけでAttempt・Receipt・投稿状態は更新されません。</p></HelpPopover></CardTitle></CardHeader>
      <CardContent className="space-y-3 text-xs">
        <p>Action {action.id} · action version {version ?? "—"}</p>
        <Button size="sm" variant="outline" disabled={busy !== null || !version || !payloadHash} onClick={() => void run("preview")}>{busy === "preview" ? "素材を確認中…" : "素材を確認・再読込"}</Button>
        {error ? <p role="alert">{error}</p> : null}
        {manifest ? <>
          <p className={manifest.status === "partial" ? "text-amber-700 dark:text-amber-300" : "text-muted-foreground"}>
            {manifest.status === "partial" ? "一部の素材を収録できません。完成パッケージとして使用できません。" : "参照された素材の実体を検証しました。公開可能性を保証するものではありません。"}
          </p>
          <p>素材参照 {manifest.assets.length}件 · ZIP内素材 {fileCount}件</p>
          <ol className="space-y-2">
            {manifest.assets.map((asset, index) => <li key={`${asset.ordinal}:${asset.role}:${index}`}>
              <span>{asset.ordinal}. {asset.role}: {asset.filename || asset.reference_id || "未解決の素材"}</span>
              {asset.reason ? <p>{reasonLabels[asset.reason] || asset.reason}（{asset.reason}）</p> : <p>{asset.mime_type} · {asset.size_bytes} bytes · SHA-256 <span className="break-all">{asset.sha256}</span></p>}
            </li>)}
          </ol>
          {manifest.reasons.length ? <ul>{manifest.reasons.map((reason, index) => <li key={index}>{String(reason.role || "package")}: {reasonLabels[String(reason.code)] || String(reason.code)}</li>)}</ul> : null}
          <Button size="sm" disabled={busy !== null} onClick={() => void run("download")}>{busy === "download" ? "取得中…" : manifest.status === "partial" ? "取得可能な素材とmanifestをダウンロード" : "素材とmanifestをダウンロード"}</Button>
          <p>外部管理画面で操作した後、このActionのManual attemptで結果を記録し、下のReceiptを確認してください。</p>
        </> : null}
        {downloadStarted ? <p role="status">ダウンロードを開始しました。Receiptはまだ更新していません。</p> : null}
      </CardContent>
    </Card>
  );
}
