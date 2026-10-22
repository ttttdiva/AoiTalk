# Native Mobile の Gateway 接続

Native Mobile の接続設定では、Wi-Fi の LAN 接続とインターネット接続を別々に指定できます。

- `Direct` は、基本 API URL またはインターネット用の直接 API URLへ接続します。
- `Portal / Gateway` は、公開ポータルの AoiTalk サービス（既定は `https://az-vnetspace.com/at`）へ接続します。Gateway 認証は AoiTalk アプリ JWT と別の資格情報として保存されます。
- Wi-Fi SSID が一致する場合だけ LAN API URLを候補にし、それ以外は選択したインターネット経路を使います。Gateway 選択時に直接 API URLへフォールバックしません。

Gateway 認証は接続設定の `Gateway認証` から開始・再認証・接続テスト・連携解除・解除再試行を実行できます。アクセストークン、リフレッシュ期限、付与期限は SDK の状態として表示され、アプリ再起動後も SecureStore から復元されます。SecureStore key は issuer、service path、service id、client id の binding を SHA-256 hex suffix にした名前空間 key です。URL の `:` や `/` を key に保存せず、別 issuer は別 key になります。旧形式の未使用 key は読み出しや資格情報移行に使いません。Gateway の 401 は Gateway 資格情報の再認証を意味し、アプリ JWT の失効やログアウトには変換されません。

認証コールバックは検証済み Android App Link の `https://az-vnetspace.com/.gateway/mobile/callback/aoitalk` へ戻ります。認可サーバーは `code`、`state`、`iss` を URL fragment にだけ返し、query には載せません。アプリが終了していた場合も起動時に raw callback を SDK の pending state 処理へ渡し、SDK が認可コード交換を再開します。未知の pending state や issuer は SDK が拒否し、コールバック URLや認証コードをログ・アプリ設定・Router stateへ保存しません。`aoitalk` scheme は既存の通常 deep link 用に保持しますが、Gateway callback には使用しません。

すべての HTTP、WebSocket、multipart、ネイティブ画像・動画・音声取得は中央の認証ヘッダー経路を使います。JWT や Gateway 資格情報を URL query に置きません。WebSocket は React Native の handshake headerを使い、保護されたメディアは `mobile/src/lib/media-cache.ts` の共通 helper と手動リダイレクト拒否付き fetch でアプリ専用キャッシュへ保存してからネイティブローダーへ渡します。

Android の大容量 multipart は `mobile/modules/native-multipart` のネイティブ経路を使います。Expo SDK の通常の FormData は URI 部品を送信前に全体バッファへ変換するため、ファイル URI を OkHttp の 64 KiB ストリームとして送信します。認証ヘッダーは中央の送信境界から受け取り、リダイレクト、Cookie、制御された multipart ヘッダーはネイティブ側で拒否します。中断時は実行中の native call をキャンセルし、応答本文は上限付きで読み取ります。iOS は `expo-file-system` の `File` 部品を `protectedFetch` へ渡し、Web は `blob:` / `data:` のローカル URIだけを認証なしで保護付き GET して Blob 部品に変換した後、同じ `protectedFetch` 境界で POSTします。Web の HTTP(S) ファイル URIは意図しない外部ファイル読込を避けるため拒否します。モジュールを含まない Android ビルドでは送信を開始せず、Expo prebuild の autolinking 対象として同梱を検証します。

Android の React Native WebSocket は OkHttp の native handshake を使うため、Expo prebuild plugin `mobile/plugins/with-safe-websocket.cjs` が生成後の `MainApplication` で `CustomClientBuilder` を登録し、HTTP(S) redirect と TLS redirect を拒否します。生成済み `android/` を直接編集せず、plugin を通らないnative生成物は配布対象にしません。HTTPの手動redirect拒否は `expo/fetch` の経路で別に適用されます。

非fetch通信の認証headerは、保存済みの接続modeと選択中portalの`/at` namespaceを毎回照合してから組み立てます。Gateway modeでもLANまたは別originのtargetにはGateway headerを付けません。WebSocketのroute解決・identity proof・Gateway認証の一時的な失敗は既存の5秒再接続へ戻し、primary pin不一致や再認証要求など利用者の操作が必要な失敗は自動再試行しません。診断カウンターにはtransport stageと安全なerror codeだけを記録します。

FilesのExplorer・Project削除は、接続先確認とGateway認証・署名identity確認を含む1回の要求に30秒の期限を設けます。期限切れの削除は自動再送せず、結果が不明なら対象の存在を再確認します。通常の一覧取得とLAN不通検出の既定期限は維持します。

接続先へアプリ資格情報を送る前に、サーバーの署名付き identity proof を検証します。基本接続先を primary として明示的に pin した後だけ、同一 identity の LAN / Gateway service prefix を alias として利用できます。接続先、アカウント、設定世代が変わった途中の要求は送信せず、キュー済みデータも別サーバーへ再送しません。
