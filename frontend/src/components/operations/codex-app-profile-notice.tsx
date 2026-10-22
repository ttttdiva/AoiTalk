"use client";
import { useLayoutEffect, useRef } from "react";
import { CodexAppConnection } from "./codex-app-connection";
export function CodexAppProfileNotice({ owner = null, enabled = false, onSessionInvalid = () => {}, onConnection }: { owner?: string | null; enabled?: boolean; onSessionInvalid?: () => void; onConnection?: (connected: boolean) => void }) {
  const liveOwner = useRef(owner);
  useLayoutEffect(() => { liveOwner.current = owner; }, [owner]);
  return <aside aria-label="専用Codex接続" className="space-y-2 rounded-md border p-3 text-sm text-muted-foreground">
    <p className="font-medium">専用 Codex 接続は既定で無効です。有効な個別配備と本人承認が揃った場合だけ、公式ログインを開始できます。</p>
    <dl className="space-y-2">
      <div><dt className="font-medium">保存先と保持</dt><dd>{enabled ? "個別の保存先とアカウントは接続条件の確認で表示します。" : "専用 profile 保存先: 未設定。"}本人承認後は OS keyring を使います。Windows では専用保存先の暗号化ファイルと OS keyring の鍵を使います。認証情報は自動更新される場合があり、固定の失効日は保証されません。アプリ内の接続状態は切断・再起動で失効します。</dd></div>
      <div><dt className="font-medium">公式ログインの認証範囲</dt><dd><code className="break-words">openid profile email offline_access api.connectors.read api.connectors.invoke</code><p>この認証範囲への本人承認と公式ログインが必要です。</p></dd></div>
      <div><dt className="font-medium">用途と利用記録</dt><dd>Operations の読み取り専用処理に使用し、観測した利用量を所有者に記録します。接続してもファイル変更やコマンド実行の権限は増えません。</dd></div>
      <div><dt className="font-medium">切断と認証情報の削除</dt><dd>アプリ側の切断は接続の許可と承認待ちの情報を失効させ、この接続が所有する処理を停止します。保存した認証情報の削除や提供元の認証許可の取消は行いません。公式ログアウト・提供元での取消は別途必要です。</dd></div>
    </dl>
    {enabled && owner ? <CodexAppConnection key={owner} owner={owner} isCurrent={() => liveOwner.current === owner} onSessionInvalid={onSessionInvalid} onConnection={onConnection} /> : <p>現在の配備では接続を開始できません。この接続による認証情報の保存は行っていません。</p>}
  </aside>;
}
