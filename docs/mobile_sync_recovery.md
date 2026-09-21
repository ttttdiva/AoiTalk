# モバイル同期が一時障害後に停止する問題の修正

## 修正内容

`mobile/src/lib/api-client.ts` の共通通信経路を修正した。タスクだけでなく、同じクライアントを利用するDocs・プロジェクト等の同期も対象となる。接続先URL、LAN/publicの切替条件、認証スコープ、SQLite、outboxの内容は変更していない。

従来の `refreshTokenOnce()` は通信断やHTTP 503等でも `false` を返していた。呼び出し元はそれを認証失効として通知し、`AuthContext` が保存済みtokenを削除する。その後 `runSync()` はtoken不在で処理を開始できず、一時障害が継続的な同期停止になる。

修正後は更新APIの401/403による認証拒否と、通信・HTTP・応答形式の問題を区別する。後者では元のエラーを伝播して認証情報を残し、次の同期で更新を再試行できる。起動時の非同期refreshにもreject処理を追加した。認証拒否後の停止、並行refreshの集約、token世代による保存制御は維持する。

また `return parseResponse(res)` は本文の完了より先に `finally` でtimeoutを解除していた。`return await parseResponse(res)` に変更し、refreshでも本文受信までtimeoutを維持する。停止した本文によって同期のsingle-flightが占有され続けることを防ぐ。timeoutを「未送信」とは扱わず、更新リクエストを自動再送する挙動は追加していない。

## 回帰テスト

Node.js 20以上とmobileの開発依存を使用する。

```sh
cd mobile
npm run test:sync-recovery
```

`npm test` のpretestにも登録している。実際のTypeScript APIクライアントを読み込んで実行し、認証ストレージ等は代替する。本文受信停止はloopback HTTPサーバーと実際のfetch/AbortControllerを用いて検証する。実サーバーや端末のデータには接続しない。

追加した24件は修正前ソースで18件失敗、修正後ソースで24件通過した。通信断からの回復、404/408/429/5xx、不正token・JSON、保存失敗、401/403、並行refresh、JSON/text/エラー本文とrefresh本文のtimeout、ログアウト競合、接続先固定を検証している。変更APIの依存を宣言に置き換えた限定strict型検査とAuthContextのTSX構文検査も通過した。これらは全mobileのtypecheck/lint、既存Jest全件、Android実機検証の代替ではない。

## 配布と既存端末（0.1.167作成時点の記録）

ソースのリリース番号は `0.1.167` / `versionCode: 167`。この番号のAPKをビルド・公開したことは意味しない。本修正作成環境にはAndroid SDKとPowerShellがなく、APKビルド、Release upload、公開latest.json更新、実機同期確認は未実施。PowerShellのrelease gate自体も未実行だが、mobile変更があるためリリースが必要となる条件に該当し、version/versionCodeは引き上げている。

配布には既存の署名・ビルド手順を使い、APKの確認後にのみ公開Releaseとlatest.jsonを更新する。ソースpushだけではインストール済みアプリには反映されない。

旧不具合でtokenが削除済みの端末では、修正版への更新後、同じサーバー・アカウントでの再ログインが必要となる。削除済みの認証情報をこの変更から復元することはできない。アプリのデータ削除やアンインストールを復旧手順にはしない。利用者端末の実際の停止原因が上記と一致するかは、実機ログと修正版での同期確認が必要。

## Docs同期中のチャット操作（0.1.168）

Pixel実機では、Docsのnative SQLite transactionとチャットの既読・本文保存が競合し、`database is locked` が発生するケースも確認された。サーバーにはグループ応答が保存されていても、端末への反映だけ失敗して空表示になる。

チャットの画面操作によるsession適用、既読、本文・cursor、タイトル、送信状態の書き込みは `runForegroundSqliteWrite` でDocsと直列化する。HTTP待機はSQLiteの実行枠に含めない。既に実行枠を取得した内部処理はraw適用関数を使い、同じqueueを入れ子で待たない。resumeで取得したsession metadataも保存し、後続refreshで古いタイトルへ戻さない。

送信中は重複操作を抑止する。送信結果の取得に失敗した場合はコマンドを含む下書きを保持し、先に会話を再読み込みして送信済みか確認するよう案内する。サーバー受理後の受信失敗もあるため、自動再送はしない。待機中に書いた別の下書きは上書きしない。

回帰検証は `conversations-delta.test.ts` のDocs書き込みとの競合・HTTP待機、`conversation-data.test.ts` のmetadata保存、`chat-composer-submission.test.tsx` の送信失敗・二重操作・コマンド保持を対象とする。実機では大量Docs同期中の会話再訪・送信・通信復帰を併せて確認する。

0.1.169では、実機で多数の項目が返ったコンテキスト診断が画面外へはみ出す問題も修正した。診断本文だけを高さ制限付きでスクロールし、タイトルと閉じる操作を画面内に残す。

## 取得中に作成したタスクの誤削除（0.1.169）

通信復帰時、古い一覧応答に含まれない新規タスクを端末が削除扱いにする競合も実機で確認された。サーバーへ作成できても、端末の削除記録によって一覧から消えたままになる。

一覧取得前のID・プロジェクト・更新日時と、取得前／適用時のoutboxを照合し、取得中に作成・変更・移動・送信された行を削除推定から除外する。blockedや失敗中のoutboxも保護する。認証が切り替わった応答は適用せず、旧アカウントの一覧へfallbackしない。安定した既存行の可視性喪失と、本当の削除記録による再出現防止は維持する。`task-list-race.test.ts` は実SQLiteと遅延応答でこの競合を検証する。
