/**
 * フォールバック関連のエラーメッセージ組み立て（純関数）。
 * 重い依存を持たないよう独立モジュールに分離してテスト可能にする。
 */

export function errorTextOf(error: unknown, fallbackText: string): string {
  if (error instanceof Error && error.name === "ApiEndpointUnavailableError") {
    return "AoiTalkサーバーへ一時的に接続できませんでした。通信状態を確認して再試行してください。";
  }
  return error instanceof Error ? error.message : fallbackText;
}

export function isLikelyConnectivityFailure(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  return (
    error.name === "AbortError" ||
    error.name === "ApiEndpointUnavailableError" ||
    /abort|timeout|timed out|network request failed|failed to fetch|networkerror|connection refused/i.test(
      error.message,
    )
  );
}

/** メイン失敗とフォールバック失敗を区別できるエラーメッセージを組み立てる。 */
export function describeFallbackFailure(
  mainError: unknown,
  fallbackError: unknown,
): string {
  return `メイン応答に失敗しました（${errorTextOf(
    mainError,
    "原因不明",
  )}）。フォールバックも失敗しました（${errorTextOf(
    fallbackError,
    "原因不明",
  )}）。`;
}
