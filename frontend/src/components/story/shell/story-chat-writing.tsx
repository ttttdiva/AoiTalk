import { toast } from "sonner";

const FLUSH_ERROR_MESSAGE =
  "未保存の変更を保存できませんでした。内容を確認してから再度お試しください。";

/** チャット執筆離脱中の編集ロック overlay。 */
export function StoryWorkspaceLeavingLock({
  testId = "story-workspace-leaving-lock",
}: {
  testId?: string;
}) {
  return (
    <div
      className="absolute inset-0 z-50"
      data-testid={testId}
      aria-hidden="true"
    />
  );
}

/**
 * チャット執筆開始後の writingBusy 更新。
 * 成功時は遷移先へアンマウントされるまで true を維持し、失敗時だけ解除する。
 */
export function applyWritingBusyAfterStartChatWriting(
  succeeded: boolean,
  setWritingBusy: (busy: boolean) => void,
): void {
  if (!succeeded) {
    setWritingBusy(false);
  }
}

/**
 * チャット執筆開始: flush（最大2回）→ API 準備 → navigate。
 * flush が失敗したら prepare / push は呼ばない。
 */
export async function executeStartChatWriting(options: {
  flushAllScopes: () => Promise<boolean>;
  push: (href: string) => void;
  prepareChatHref: () => Promise<string>;
}): Promise<boolean> {
  const firstFlush = await options.flushAllScopes();
  if (!firstFlush) {
    toast.error(FLUSH_ERROR_MESSAGE);
    return false;
  }

  const secondFlush = await options.flushAllScopes();
  if (!secondFlush) {
    toast.error(FLUSH_ERROR_MESSAGE);
    return false;
  }

  try {
    const href = await options.prepareChatHref();
    options.push(href);
    return true;
  } catch (error) {
    toast.error(error instanceof Error ? error.message : "チャット執筆を開始できませんでした");
    return false;
  }
}
