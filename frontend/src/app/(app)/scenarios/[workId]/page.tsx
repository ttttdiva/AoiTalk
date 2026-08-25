"use client";

import { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";
import { Loader2 } from "lucide-react";
import { useStoryWorkContext } from "@/components/story/shell/story-workspace-shell";

export default function StoryWorkIndexPage() {
  const router = useRouter();
  const params = useParams<{ workId: string }>();
  const { work, isLoaded } = useStoryWorkContext();
  const workId = typeof params?.workId === "string" ? params.workId : work.id;

  useEffect(() => {
    // overview が返るまで episodeCount は既定値 0 のままなので、確定してから遷移先を決める。
    if (!isLoaded || !workId) return;
    // 相対パスだと /scenarios/<id> の末尾セグメントが置換されて /scenarios/manuscript になるため、
    // 必ず作品IDを含む絶対パスで遷移する。
    const segment = work.episodeCount > 0 ? "manuscript" : "settings";
    router.replace(`/scenarios/${encodeURIComponent(workId)}/${segment}`);
  }, [isLoaded, router, work.episodeCount, workId]);

  return <div className="flex h-full items-center justify-center text-sm text-muted-foreground"><Loader2 className="mr-2 size-4 animate-spin" />作品を開いています…</div>;
}
