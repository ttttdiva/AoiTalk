"use client";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { productFilesApi, type ProductFile } from "@/lib/media-product-files-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";

export function ProductFilePicker({ scope, onSelect }: { scope: PersonaWorkspaceScope; onSelect: (reference: string) => void }) {
  const [files, setFiles] = useState<ProductFile[]>([]);
  const [name, setName] = useState("");
  const [maximum, setMaximum] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const request = useRef<AbortController | null>(null);
  const latestSelect = useRef(onSelect);
  useEffect(() => { latestSelect.current = onSelect; });
  useEffect(() => () => request.current?.abort(), [scope.ownerUserId, scope.projectId, scope.personaId]);
  const run = async (file?: ProductFile) => {
    if (request.current) return;
    const controller = new AbortController(); request.current = controller;
    setBusy(true); setError(""); setStatus(file ? "Filesから配布物を取り込み中…" : "Filesを取得中…");
    try {
      if (file) {
        const result = await productFilesApi.import(scope.personaId, file, controller.signal);
        if (controller.signal.aborted) return;
        if (result.owner_user_id !== scope.ownerUserId || result.project_id !== scope.projectId || result.size_bytes !== file.size_bytes || !result.artifact_version_id || !/^[a-f0-9]{64}$/u.test(result.sha256)) throw new Error("取り込んだ配布物の固定版・所属を照合できません。");
        latestSelect.current(`artifact:${result.artifact_version_id}`);
        setStatus(`固定版として保存しました: ${result.filename ?? file.filename}`);
      } else {
        const result = await productFilesApi.list(scope.personaId, controller.signal, name);
        if (controller.signal.aborted) return;
        setFiles(result.files.filter(row => row.owner_user_id === scope.ownerUserId && row.project_id === scope.projectId));
        setMaximum(result.max_bytes); setStatus(result.files.length ? "配布物を選択してください。" : "対象のFiles配布物がありません。");
      }
    } catch (failure) {
      if (!controller.signal.aborted) { setError(failure instanceof Error ? failure.message : "Filesから取り込めません。"); setStatus(""); }
    } finally {
      if (request.current === controller) { request.current = null; setBusy(false); }
    }
  };
  return <div className="space-y-2 border-t pt-3" data-testid="product-files-picker">
    <div className="flex items-center gap-1 text-xs font-medium">Files配布物<HelpPopover label="Files配布物"><p>大きい配布物はFilesに保存して選択します。選択した内容を固定版に保存し、元ファイルの変更から独立して取得できます。</p><p>一覧は100件までです。配布物名で絞り込んで更新できます。</p></HelpPopover></div>
    <p className="text-xs text-muted-foreground">取込上限: {maximum == null ? "1 GiB以下・Files設定上限以下" : `${maximum.toLocaleString()} bytes`}。一括ZIPの素材合計上限は25 MiBです。</p>
    <label className="block text-xs">Filesの配布物名で絞込<Input value={name} maxLength={255} disabled={busy} onChange={event => setName(event.target.value)} /></label>
    <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void run()}>Filesの配布物を更新</Button>
    {files.map(file => <div key={file.source_id} className="flex flex-wrap items-center gap-2 text-xs"><span className="min-w-0 break-all">{file.filename} · {file.size_bytes.toLocaleString()} bytes</span><Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void run(file)}>固定版として取り込む</Button></div>)}
    {busy && <Button type="button" size="sm" variant="outline" onClick={() => { request.current?.abort(); setStatus("取込を中止しました。完了状態はFilesを更新して再確認してください。"); }}>中止</Button>}
    {status && <p role="status" className="text-xs">{status}</p>}{error && <p role="alert" className="text-xs">{error}</p>}
  </div>;
}
