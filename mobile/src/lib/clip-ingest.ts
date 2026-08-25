/**
 * クリップ取り込みの3段フォールバック。
 *
 * 1. サーバー経由: `POST /api/docs/ingest`（従来動作）。
 *    HTTPエラー（4xx/5xx）は従来どおり失敗として呼び出し側へ投げる。
 *    通信不能のときだけ 2 へ落とす。
 *    利用者が明示的に押した操作なので、インターネット到達性や直近30秒の別API失敗
 *    では諦めない。AoiTalkサーバーはLAN内にあることがあり、その場合
 *    `isInternetReachable=false` でも到達できる。
 *    クライアントtimeoutはサーバーが処理を続けている可能性があり、再送すると
 *    二重取り込みになるため、通信不能へ寄せず失敗として返す。
 * 2. モバイルLLMでローカル完結: 端末のLLMで保存先判定と要約を行い、
 *    `docsRepo.createNode()` へ乗せる（outbox 経由で後日サーバーへ同期）。
 *    ログイン済みのときだけ試す。未ログインで作ったローカルノードは、ログイン時の
 *    `clearLocalSyncCache()` で消えるため成功扱いにできない。
 *    オフラインでも実行する。クラウドLLMへ届かない場合は分類も要約も行わず、
 *    入力そのままを未分類の保存先へ残す（入力を失わないことを優先する）。
 * 3. 保留キュー: 未ログイン、または取り込み先キャッシュが無いなど端末だけでは
 *    保存先を決められないときに入力を保留し、サーバーへ到達できた同期で再送する。
 */

import { docsApi, type ClipIngestResult } from "./docs-api";
import { isApiConnectionError, isApiTimeoutError } from "./api-client";
import { getToken } from "./auth";
import { useNetworkStore } from "../stores/network";
import {
  LocalClipIngestUnavailableError,
  runLocalClipIngest,
} from "./clip-ingest-local";
import { enqueuePendingClipIngest } from "../repositories/pending-clip-ingest";

export type ClipIngestOutcome =
  | { mode: "server"; result: ClipIngestResult; syncWarning: string }
  | { mode: "local"; result: ClipIngestResult }
  | { mode: "queued"; pendingId: string };

export async function runClipIngest(
  source: string,
): Promise<ClipIngestOutcome> {
  const { connected, online } = useNetworkStore.getState();
  const hasAuth = Boolean(await getToken());

  // 1. サーバー経由。ネットワークに繋がっていて、ログイン済みなら必ず一度は試す。
  if (connected && hasAuth) {
    try {
      const response = await docsApi.ingest(source);
      return {
        mode: "server",
        result: response.result,
        syncWarning: response.local_sync_warning ?? "",
      };
    } catch (error) {
      // timeoutは「届かなかった」ではない。サーバー側が保存を終えている可能性が
      // あるため、自動再送やローカル保存へ回さずそのまま失敗として返す。
      if (isApiTimeoutError(error)) throw error;
      // HTTPエラーはサーバーが応答している＝フォールバックしても同じ結果になる。
      if (!isApiConnectionError(error)) throw error;
      useNetworkStore.getState().setServerReachable(false);
    }
  }

  // 2. 端末だけでローカル完結。未ログインでは作ったノードが消えるため試さない。
  if (hasAuth) {
    try {
      const result = await runLocalClipIngest(source, {
        // オフラインではURL本文を取れる経路が他に無い。未確認と明示して保存する。
        allowUnfetchedUrls: !online,
        allowWithoutLlm: !online,
      });
      return { mode: "local", result };
    } catch (error) {
      // 実行条件が満たせなかった（＝副作用が発生していない）ときだけ 3 へ落とす。
      // ノード作成を始めた後の失敗は保留に積むと二重取り込みになるため投げる。
      if (!(error instanceof LocalClipIngestUnavailableError)) throw error;
    }
  }

  // 3. 保留キュー。
  const pendingId = await enqueuePendingClipIngest(source);
  return { mode: "queued", pendingId };
}
