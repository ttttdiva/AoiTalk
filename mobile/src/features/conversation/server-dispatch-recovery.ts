/**
 * A short-lived failed reachability observation should protect automatic
 * background retries, but it must not turn a user tap into a silent no-op.
 * The dispatch endpoint is idempotent by client message ID, so an explicit
 * send/retry may probe once whenever a physical network path exists.
 */
export function shouldHoldServerDispatch(args: {
  networkConnected: boolean;
  serverKnownUnreachable: boolean;
  userInitiated: boolean;
}): boolean {
  if (!args.networkConnected) return true;
  return !args.userInitiated && args.serverKnownUnreachable;
}

export const SERVER_SEND_DEFERRED_ERROR =
  "AoiTalkサーバーへの接続を確認できるまで送信を保留します。";

export const SERVER_RETRY_DEFERRED_ERROR =
  "AoiTalkサーバーへの接続を確認できるまで再送を保留します。";

export const SERVER_RESULT_UNCONFIRMED_ERROR =
  "送信結果を確認できませんでした。接続復旧後に自動確認します。";

const RESOLVED_BY_PENDING_FLUSH = new Set<string>([
  SERVER_SEND_DEFERRED_ERROR,
  SERVER_RETRY_DEFERRED_ERROR,
  SERVER_RESULT_UNCONFIRMED_ERROR,
]);

/** Clear only errors whose condition is resolved by a successful pending flush. */
export function clearResolvedServerPendingError(
  error: string | null,
): string | null {
  return error && RESOLVED_BY_PENDING_FLUSH.has(error) ? null : error;
}
