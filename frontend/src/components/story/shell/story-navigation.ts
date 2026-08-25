import { toast } from "sonner";

/** チャット執筆離脱中は Story Workspace のタブ遷移を抑止する。 */
export function shouldNavigateStoryWorkspaceSegment(
  writingBusy: boolean,
  segment: string,
  activeSegment: string,
): boolean {
  if (writingBusy) return false;
  if (segment === activeSegment) return false;
  return true;
}

export async function navigateStoryWorkspaceSegment(options: {
  writingBusy: boolean;
  segment: string;
  activeSegment: string;
  workId: string;
  navigateAfterFlush: (href: string) => Promise<boolean>;
}): Promise<void> {
  if (
    !shouldNavigateStoryWorkspaceSegment(
      options.writingBusy,
      options.segment,
      options.activeSegment,
    )
  ) {
    return;
  }
  await options.navigateAfterFlush(
    `/scenarios/${encodeURIComponent(options.workId)}/${options.segment}`,
  );
}

export async function navigateAfterFlush(
  flushAllScopes: () => Promise<boolean>,
  navigate: (href: string) => void,
  href: string,
): Promise<boolean> {
  const ok = await flushAllScopes();
  if (!ok) {
    toast.error("未保存の変更を保存できませんでした。内容を確認してから再度お試しください。");
    return false;
  }
  navigate(href);
  return true;
}
