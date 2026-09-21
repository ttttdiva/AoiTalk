# ポータブルPC Bridge（Edge / Windows Computer Use）

## 操作先

AoiTalkサーバーと操作されるPCを分離する。操作先PCで `AoiTalk-PC-Bridge.exe` を起動し、そこからサーバーへWebSocket接続する。サーバー側のWindowsやEdgeへ暗黙に切り替える経路はない。

```text
PC-A: AoiTalkサーバー / 認証・会話・LLM・Jev
        ↑ 操作先PCから張る WSS 接続
PC-B: AoiTalk-PC-Bridge.exe
        ├─ Edge Native Messaging → PC-Bのログイン済みEdge
        └─ Windows UI Automation / SendInput → PC-Bのデスクトップ
```

PC-Cにも別の登録コードを発行して同じexeを置ける。Web設定「PC接続」で選んだPCをユーザーごとの既定にし、APIでは会話ごとの選択も指定できる。切断中のPCを選んだ場合はエラーで停止し、他の接続PCへ切り替えない。サーバーPCも操作したい場合は、同じexeで明示的にそのPCを登録する。

## 初回利用

1. AoiTalkを通常どおり起動する。「設定 → PC接続」で名前を付けてPCを登録し、接続設定JSONを保存する。exeは同画面から取得できる。開発環境ではリポジトリ直下の `AoiTalk-PC-Bridge.exe` が実体。
2. **操作するPC** へexeをコピーして起動する。「設定ファイル読込」でJSONを開き「接続」を押す。受け側PCへのPython導入、待受ポートの開放、常駐サービス登録は不要。
3. Edge操作を使う場合だけ「Edge拡張を準備」を押す。そのPCのEdgeの `edge://extensions` で開発者モードを有効にし、「展開して読み込み」から表示されたフォルダを読み込む。既存のAoiTalk拡張がある場合は再読込する。タブ内の拡張で「このタブを使う」を選ぶ。
4. AoiTalkの「PC接続」で接続済みPCを選ぶ。ブラウザ操作が無効になっている環境では、管理者設定「ブラウザ操作」も有効にする。現在のChat runtimeのbrowser packにはBrowser AgentとComputer Useの両方を含める。

JSON内の `server_url` は **そのPCから到達できるAoiTalkのURL** を使用する。公開アクセスは `https://...` とし、Caddy等が `/api/pc-bridge/connect` のWebSocketをFastAPIへ転送する。開発専用Next.jsの3002番ポートにはWebSocketリレーを設けていない。開発時はFastAPIの3000番ポート、通常の利用ではCaddyのHTTPS公開URLを指定する。

独自CAを使用する環境はBridgeの「独自CA証明書ファイル」に信頼するCAのPEMファイルを指定する。TLS検証を無効にするオプションはない。

## Chatからの利用

`browser_agent` は選択したPCの既存Edgeタブに対する目的を受ける。URL省略時は拡張で選択したタブ、`tab_id` 指定時はそのタブ、URL指定時は既存の同一URLまたは同じEdgeプロファイル内の新規タブを使う。Jevは候補選択だけを担当し、利用不能時は同じ会話LLMへ切り替わる。

`computer_use` はWindowsアプリを扱う。`goal` を指定すると、画面・UI Automation情報を読み、次の操作をLLMで決め、実行後に再観測する。操作対象のUIA要素選択には同じJev/LLMの切替処理を利用する。

例: 「選択中のPCのメモ帳を開いているウィンドウに切り替え、本文を読んで」「選択中のPCのEdgeで今のタブを操作して」。既存アプリの前面化、クリック、右クリック、ダブルクリック、移動、ドラッグ、Unicode文字入力、キーの同時押し、スクロールを実装する。

単発の `action` と `window_id` / 座標 / キーも指定できる。座標は**物理デスクトップ座標**で、マルチモニターの負座標も使用する。縮小スクリーンショットのピクセル座標をそのまま使わず、返却される元のデスクトップ範囲と画像サイズから変換する。

`use_vision=true` は既存 `MediaRecognitionService` の画像認識設定を利用する。既定の操作判断はUI Automationを用いる。画像バイナリをテキストpromptやChatの結果JSONへ埋め込まない。画面URLの取得は同じユーザーの登録PCだけを対象にする。

## exeとデータ

- `AoiTalk-PC-Bridge.exe`: Python実行環境・依存・Edge拡張を含めたWindows x64の単体実行ファイル。Gitには巨大binaryを追加せず、ビルドで生成する。
- `AoiTalk-PC-Bridge-data/connection.json`: サーバーURL・接続コード・任意のCAパス。exe横に保存するローカル設定。
- `AoiTalk-PC-Bridge-data/edge-extension/`: exe内から展開したEdge拡張。
- `AoiTalk-PC-Bridge-data/com.aoitalk.edge.json`: Edge Native Messaging登録。HKCUの `com.aoitalk.edge` から参照する。

exe自体がNative Messaging hostも兼ねる。Edgeから `chrome-extension://...` 引数で起動された場合はGUIを出さずstdioプロトコルを処理する。旧server-localのPythonホストと専用バッチは削除し、別実装を残さない。

exeを別フォルダへ移動した場合は、保存設定も必要に応じて移し、「Edge拡張を準備」を再実行する。切断・終了はGUIから行う。途中の入力やドラッグは停止要求を確認して中断し、送信済み操作は再送しない。

## 接続・保存の実装

各登録は通常のAoiTalk認証ユーザーに紐づく。接続コードは初回発行時だけ返し、サーバーにはハッシュのみを保存する。`app_config_settings` の `pc_bridge:<user_id>` 名前空間を使い、通常の `global` 設定とは分離する。登録解除は接続中WebSocketも切断する。

Bridgeは外向き接続を維持し、10秒ごとのheartbeatで状態を報告する。サーバーは現在のユーザー・選択PC・接続を解決してコマンドを送る。コマンドは要求IDと応答を対応付け、切断時は未完了を失敗として返す。再接続はコマンド再実行を意味しない。操作結果が不明なtimeoutでも自動再送しない。

各接続のWebSocketはその接続を所有するasyncio loopで送受信する。LLMツールが別thread/loopで動いても、そのloopへ転送する。操作対象ごとのロックで同一PCへのAgent作業を直列化する。

ブラウザ独自の確認dialog、サイトallowlist、Cookieコピー、隔離ブラウザ起動は追加していない。AoiTalkログインと登録ユーザーの識別は接続・操作先を区別するために用いる。

## ビルド

開発PCで `Build-PC-Bridge.bat` を実行する。専用ビルドvenvを `.local/portable-bridge/build-venv` に作り、依存を読み込み、onedirの起動検証後にonefileを生成する。実行中のexeを上書きできない場合はBridge/Edge接続を終了してから、表示されるビルド済みexeをルートへコピーする。

コマンド入口:

```text
AoiTalk-PC-Bridge.exe                         GUI
AoiTalk-PC-Bridge.exe --headless --config connection.json
AoiTalk-PC-Bridge.exe --install-edge
AoiTalk-PC-Bridge.exe --self-test
```

## 検証

`tests/test_pc_bridge.py`: ユーザー識別、登録/解除、選択、別ユーザーのPC拒否、WebSocket relay、切断、別loopからの呼び出し、コマンドtimeout、入力停止、別PCへfallbackしないことを確認する。

`AOITALK_PC_BRIDGE_DB_TEST=1` は実PostgreSQLに一時のユーザー名前空間を作り、保存・再読込・解除を確認する。既存ユーザーやglobal設定を変更せず、作った行は最後に削除する。

`AOITALK_PC_BRIDGE_LIVE=1` と `tests/test_pc_bridge_live.py` は、productionのrelayコードで一時サーバーを起動し、**実際のビルド済みexe**を別プロセスで接続する。通常WSと証明書を検証したWSSの双方で、既存Edgeでのfixtureログイン→LLMによる検索/POST保存、Windowsメモ帳への日本語入力/保存、LLMによるUIA本文読み取り、LLMに本文の全置換と保存を任せる複数ステップ作業を確認する。Cookie注入はしない。最終操作先はユーザーのWindows上の実アプリである。

接続先はループバック上の別プロセス。別の物理PCと実インターネット回線を使った試験を済ませた、という意味ではない。

Web設定コンポーネントのVitest、API型生成、Web/Native Mobileの型検査も実施する。`frontend/e2e-live/pc-bridge.live.spec.ts` は既存AoiTalkアカウントでログインして登録→選択→画面確認→通常ChatからEdge/Windows操作まで行う再実行用テスト。

今回、既存アカウントの認証情報を使う実UI試験の開始が作業ツール側で拒否されたため、この**本番アカウントのChat UI完走と独立AIによるWebUI QAは未検証**。一時テスト用認証による実relay/exe操作、GUI単体起動・スクリーンショット、コンポーネントテストの成功とは区別する。

## この変更での検証結果

- 関連backend/実PostgreSQL/既存Edgeの回帰: **252 passed**（既存依存の警告6件）。
- ビルド済みexeを通した実操作: **WS/WSSの2ケースがPASS**。ログイン済みEdgeの保存、メモ帳の日本語入力・保存、LLMの画面読み取りと本文置換・保存まで確認。
- Web設定: **9 passed**。Web/Native Mobileの型検査、対象ESLint/Ruff、Caddy設定検査、Mobile Product Contract検査を実施。
- Next.jsのproduction buildとexeのonedir/onefile起動検査がPASS。exeだけを別ディレクトリへ置いたGUIも起動し、スクリーンショットで表示を確認。
- 共有API型更新に伴いNative Mobile **0.1.187 / versionCode 187** をビルド・公開し、更新用latest.jsonへの反映を確認。

途中、Windowsの前面化と高速Unicode入力の不具合を実機で修正した。本文置換では全選択がクリックで解除されないようUIAのfocusを使用し、読み取り専用Textラベルを入力先候補に含めないよう修正した。最後のWS/WSS試験では、LLMの完了メッセージだけでなく保存済み本文の一致を検証している。

## 限界

Windowsの対話デスクトップが必要。UACのsecure desktop、ロック画面、通常権限からの昇格アプリ操作、ブラウザ保護ページやOSダイアログの全面自動化は保証しない。Canvas/画像だけのアプリに対するVision操作も今回の実機試験対象外。exeは未署名であり、組織の実行・拡張機能ポリシーは別途適用される。

モバイルChatも同じサーバー側のユーザー選択PCへルーティングする。スマートフォンそのものをWindows Bridgeとして動かす機能ではない。PC登録UIはWeb設定に実装しており、Native Mobile専用のPC設定画面は追加していない。

## 一次資料

- https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/native-messaging
- https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput
- https://pyinstaller.org/en/stable/operating-mode.html
- https://websockets.readthedocs.io/en/stable/reference/sync/client.html

PyInstallerの原文: “PyInstaller can bundle your script and all its dependencies into a single executable”

訳: スクリプトとその依存関係を、単一の実行ファイルへまとめられる。
