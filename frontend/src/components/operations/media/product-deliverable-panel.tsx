"use client";
import { useEffect, useRef, useState } from "react";
import { Button, buttonVariants } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { productDeliverableUrl, productFilesApi, type ProductDeliverable } from "@/lib/media-product-files-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";

export function ProductDeliverablePanel({ scope, actionId, version, payloadHash }: { scope: PersonaWorkspaceScope; actionId: string; version: number; payloadHash: string }) {
  const [value, setValue] = useState<ProductDeliverable | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  useEffect(() => { setValue(null); return () => request.current?.abort(); }, [scope.ownerUserId, scope.projectId, actionId, version, payloadHash]);
  const load = async () => {
    if (request.current) return;
    const controller = new AbortController(); request.current = controller; setBusy(true); setError(""); setValue(null);
    try {
      const result = await productFilesApi.manifest(actionId, version, payloadHash, controller.signal);
      if (controller.signal.aborted) return;
      const bound = result?.manifest?.action, file = result?.manifest?.deliverable;
      if (bound?.id !== actionId || bound.action_version !== version || bound.payload_hash !== payloadHash || file?.owner_user_id !== scope.ownerUserId || file.project_id !== scope.projectId || !file.artifact_version_id || !/^[a-f0-9]{64}$/u.test(file.sha256) || !/^[a-f0-9]{64}$/u.test(result.manifest_sha256) || typeof result.manifest_json !== "string") throw new Error("配布物のmanifestが選択Actionと一致しません。再取得してください。");
      setValue(result);
    } catch (failure) { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "配布物を確認できません。"); }
    finally { if (request.current === controller) { request.current = null; setBusy(false); } }
  };
  const saveManifest = () => {
    if (!value) return;
    const url = URL.createObjectURL(new Blob([value.manifest_json], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = `product-${actionId}-manifest.json`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  };
  const file = value?.manifest.deliverable;
  return <div className="space-y-2 rounded-md border p-3 text-xs" data-testid="product-direct-deliverable">
    <p className="inline-flex items-center gap-1 font-medium">配布物の固定版・manifest<HelpPopover label="配布物の固定版・manifest">配布物は単独で取得できます。取得時に実体を検証します。外部登録・Receipt・販売状態は変わりません。</HelpPopover></p>
    <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void load()}>{busy ? "確認中…" : "配布物の固定版を確認"}</Button>
    {error && <p role="alert">{error}</p>}
    {file && value && <><p className="break-all">{file.filename} · {file.size_bytes.toLocaleString()} bytes · SHA-256 {file.sha256}</p><div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" onClick={saveManifest}>manifestを保存</Button><a className={buttonVariants({ size: "sm" })} href={productDeliverableUrl(actionId, version, payloadHash, file)} download>配布物を直接ダウンロード</a></div><p className="break-all">manifest SHA-256 {value.manifest_sha256}</p></>}
  </div>;
}
