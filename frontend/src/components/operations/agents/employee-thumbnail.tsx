"use client";

import Image from "next/image";
import { useRef, useState } from "react";
import { Camera, ImageOff, Loader2 } from "lucide-react";
import useSWR from "swr";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { buildPreview, rigFetch, rigUrl } from "@/lib/seethrough-rig/api";
import { employeeApi, type Employee } from "@/lib/agent-employees-api";
import { operationsApi } from "@/lib/operations-api";
import { withBasePath } from "@/lib/base-path";
import { Button } from "@/components/ui/button";

export function EmployeeThumbnail({ agent, className = "" }: { agent: Employee; className?: string }) {
  const owner = useCurrentUserId();
  const url = !agent.thumbnail_artifact_id && agent.character_id ? rigUrl(agent.character_id) : null;
  const { data, error } = useSWR(url ? ["employee-thumbnail", owner, url] : null, () => rigFetch(url!), { shouldRetryOnError: false, keepPreviousData: false });
  const active = data?.builds.find(build => build.id === data.active_revision);
  const source = agent.thumbnail_artifact_id ? withBasePath(`/api/python-proxy/operations/artifacts/${encodeURIComponent(agent.thumbnail_artifact_id)}/content`) : data?.has_source ? `${url}/source.png`
    : active && buildPreview(active) ? `${url}/assets/${encodeURIComponent(active.id)}/${buildPreview(active)}` : null;
  const [failedSource, setFailedSource] = useState<string | null>(null);

  return <span className={`relative flex shrink-0 items-center justify-center overflow-hidden bg-accent ${className}`}>
    {source && source !== failedSource
      ? <Image src={source} alt={`${agent.display_name}のサムネイル`} fill unoptimized sizes="240px" className="object-cover" onError={() => setFailedSource(source)} />
      : <span className="flex flex-col items-center gap-1.5 text-muted-foreground"><ImageOff aria-hidden="true" className="size-5 opacity-50" /><span className="text-[11px]">{error || source ? "画像を表示できません" : "画像未設定"}</span></span>}
  </span>;
}

export function EmployeeThumbnailControl({ agent, onChanged, className = "", cover = false }: { agent: Employee; onChanged: (id: string, artifactId: string | null) => Promise<void>; className?: string; cover?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  const pending = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function change(file: File) {
    if (pending.current) return;
    setError("");
    if (!["image/png", "image/jpeg"].includes(file.type) || file.size > 25 * 1024 * 1024) {
      setError("PNG / JPEG、25MB以下の画像を選んでください。"); return;
    }
    pending.current = true; setBusy(true);
    try {
      const artifact = await operationsApi.uploadArtifact({ file, label: file.name }, crypto.randomUUID());
      const saved = await employeeApi.thumbnail(agent.id, { artifact_id: artifact.id, expected_artifact_id: agent.thumbnail_artifact_id ?? null });
      await onChanged(agent.id, saved.artifact_id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "画像を保存できませんでした。");
    } finally { pending.current = false; setBusy(false); }
  }
  return <span className={className}>
    <input ref={input} type="file" accept="image/png,image/jpeg" className="hidden" aria-label={`${agent.display_name}の画像ファイル`} onChange={event => { const file = event.target.files?.[0]; event.target.value = ""; if (file) void change(file); }} />
    <Button type="button" size="icon" variant="ghost" aria-label={`${agent.display_name}の画像を変更`} aria-busy={busy} disabled={busy} onClick={() => input.current?.click()} className={cover ? "relative size-full rounded-xl bg-transparent hover:bg-black/10" : "rounded-full bg-black/35 text-white hover:bg-black/55 hover:text-white"}>{busy ? <Loader2 aria-hidden="true" className="size-4 animate-spin motion-reduce:animate-none" /> : <Camera aria-hidden="true" className={cover ? "absolute bottom-1 right-1 size-4 rounded bg-black/40 p-0.5 text-white" : "size-4"} />}</Button>
    {error && <span role="alert" className="absolute left-0 top-full z-20 mt-1 w-36 rounded-lg bg-card p-2 text-xs text-destructive shadow-md">{error}<Button type="button" size="xs" variant="ghost" onClick={() => setError("")}>閉じる</Button></span>}
  </span>;
}
