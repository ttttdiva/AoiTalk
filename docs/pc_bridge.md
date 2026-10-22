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
3. `extension` 方式を使う場合だけ「Edge拡張を準備」を押す。そのPCのEdgeの `edge://extensions` で開発者モードを有効にし、「展開して読み込み」から表示されたフォルダを読み込む。既存のAoiTalk拡張がある場合は再読込する。タブ内の拡張で「このタブを使う」を選ぶ。`agent_browser` 方式では拡張を使わず、起動済みの既存EdgeへCDPで接続する設定にして、配布EXEに同梱されたhelperと、同じEdgeプロファイルのloopback WebSocket候補を使う。Edgeを新しいプロファイルや隔離ブラウザとして起動する経路はない。
4. AoiTalkの「PC接続」で接続済みPCを選ぶ。ブラウザ操作が無効になっている環境では、管理者設定「ブラウザ操作」も有効にする。現在のChat runtimeのbrowser packにはBrowser Agent、Computer Observe、Computer Useを含める。

JSON内の `server_url` は **そのPCから到達できるAoiTalkのURL** を使用する。公開アクセスは `https://...` とし、Caddy等が `/api/pc-bridge/connect` のWebSocketをFastAPIへ転送する。開発専用Next.jsの3002番ポートにはWebSocketリレーを設けていない。開発時はFastAPIの3000番ポート、通常の利用ではCaddyのHTTPS公開URLを指定する。

独自CAを使用する環境はBridgeの「独自CA証明書ファイル」に信頼するCAのPEMファイルを指定する。TLS検証を無効にするオプションはない。

## Chatからの利用

`browser_agent` は選択したPCの既存Edgeに対する目的を受ける。`extension` 方式では、URL省略時は拡張で選択したタブ、`tab_id` 指定時はそのタブ、URL指定時は既存の同一URLまたは同じEdgeプロファイル内の新規タブを使う。Jevが利用できない場合は同じ会話LLMへ切り替わる。`agent_browser` 方式では、Bridge GUIの「Edgeに接続・タブを確認」で一覧を取得し、「このタブを選択」で既存Edgeの操作対象を明示してから使う。URL指定は選択済みtarget上のnavigationであり、URLから別tabを自動検索・作成しない。`tab_id` はこの接続でBridgeが返したtab handleを指定する。Jevのtimeout・接続失敗・候補判断失敗では会話LLMへfallbackせず、操作を停止して結果不明または失敗として返す。

`computer_observe` は読み取り専用の単発観測を扱う。`action` は `observe` / `screenshot` / `windows` のみで、`goal` や操作引数は受け付けない。通常Chatのbrowser packに公開し、読み取り専用の依頼・Review・計画作成中にも利用できる。既存のブラウザ操作の有効設定、ユーザー識別、選択PC、切断時停止、PC単位ロックを共用する。

`computer_use` はWindowsアプリの操作を扱う。`goal` を指定すると、画面・UI Automation情報を読み、次の操作をLLMで決め、実行後に再観測する。操作対象のUIA要素選択には同じJEV/LLMの切替処理を利用する。既存の単発操作も維持するが、このツール全体は操作可能として既存policyに従う。画面を読むだけなら `computer_observe` を使い、前面化・クリック・入力や自動操作を読み取り扱いにしない。

`agent_browser` のBrowser Agentは、サーバーからhelperやendpointを受け取らず、Bridgeのローカル設定で固定した既存Edgeのloopback CDP endpointへ接続する。snapshotは完全なscope内を検証して安全な候補へ投影し、JEVが候補を一つ選び、同じ接続・同じsnapshotのopaque refだけでclick/fill/selectを一回実行する。timeout、unknown outcome、許可待ち、Edge対象の変化ではCDPへ暗黙に再接続したり操作を再送したりせず、extensionへfallbackしない。idleなWSS transportの再接続と、browser/CDP connectionの再接続は別であり、前者が起きても新しいCDP接続や操作の再送は行わない。CDP connectionを再開する場合はBridgeを切断して、GUIから明示的に再接続する。

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

`extension` は既存端末設定との互換接続方式として明示選択できます。`agent_browser` のCDP経路が利用できないときに自動でextensionへ切り替えることはありません。既存のextension接続を使う市場cycleを停止・再起動せずに移行する場合は、先に別のBridge設定でCDP endpoint、helper検証、GUI接続を確認してから接続方式を変更します。

## 接続・保存の実装

各登録は通常のAoiTalk認証ユーザーに紐づく。接続コードは初回発行時だけ返し、サーバーにはハッシュのみを保存する。`app_config_settings` の `pc_bridge:<user_id>` 名前空間を使い、通常の `global` 設定とは分離する。登録解除は接続中WebSocketも切断する。

Bridgeは外向き接続を維持し、10秒ごとのheartbeatで状態を報告する。サーバーは現在のユーザー・選択PC・接続を解決してコマンドを送る。コマンドは要求IDと応答を対応付け、切断時は未完了を失敗として返す。再接続はコマンド再実行を意味しない。操作結果が不明なtimeoutでも自動再送しない。

各接続のWebSocketはその接続を所有するasyncio loopで送受信する。LLMツールが別thread/loopで動いても、そのloopへ転送する。操作対象ごとのロックで同一PCへのAgent作業を直列化する。

ブラウザ独自の確認dialog、サイトallowlist、Cookieコピー、隔離ブラウザ起動は追加していない。AoiTalkログインと登録ユーザーの識別は接続・操作先を区別するために用いる。

## Apps保存Actionの設定

Appsの保存Actionは、サーバー側の実行Targetと操作先PC側の保存Actionを別々に設定する。サーバーの `app_config_settings` にある `apps.actions.execution_targets` は、Appの `execution_target_id` をどの owner / machine / device へ配送できるかを決める認可情報である。PCの `AoiTalk-PC-Bridge-data/connection.json` にある `saved_actions` は、実際のコマンド、作業フォルダ、probeをPC内で解決するローカルカタログであり、サーバーへコマンドやローカルパスを送る場所ではない。

新規環境では `apps.actions.enabled: false` と空の `execution_targets` が既定値である。管理者が対象ごとに `enabled`、`owner_user_id`、`machine_id`、`device_id`、`allowed_app_ids`（または `app_ids`）を設定し、必要なActionのTarget IDと一致させてから有効化する。owner、machine、device、App許可リスト、Actionの保存版hashが一つでも一致しない場合、Targetは利用不可として扱われる。PC側では `saved_action_id`、SHA-256、`mode`、readiness `probe` を保存カタログから検証する。

設定値は秘密情報を含めず、形式だけを確認できる最小例にする。

```yaml
apps:
  actions:
    enabled: false
    execution_targets:
      pc-office:
        enabled: false
        owner_user_id: "<user-uuid>"
        machine_id: "<machine-id>"
        device_id: "<device-uuid>"
        allowed_app_ids: ["<app-uuid>"]
```

通常の実行要求はReleaseに束縛された `Action ID` とユーザー入力だけを送る。`release`、Target、コマンド、パス、保存Action本文はクライアントから指定しない。`Idempotency-Key` は同じ要求の再試行を同じRunへ束縛する。再起動後に照合結果が `unknown` になったRunは、状態照合だけを行い、開始を再送しない。接続epoch、machine/device、Action hashの照合に失敗した場合もfail-closedで停止する。

初回のフレームワーク更新を適用した後は、既存AppのReleaseを作り直さなくても、サーバー設定とPC側保存Actionカタログが同じAction契約を満たせば利用できる。新しいmanifestのActionや入力/output schemaを配布した場合だけ、そのAppのReleaseを更新する。保存Actionの追加・hash変更、Targetのowner/device変更は設定変更として扱い、実行前に接続済みPCの再読込と状態確認を行う。実機PCでの初回接続、保存Actionの実行、再起動後のunknown照合は別途QAが必要であり、CLI検証のPASSだけでは完了としない。

## ビルド

開発PCで `Build-PC-Bridge.bat` を実行する。専用ビルドvenvを `.local/portable-bridge/build-venv` に作り、依存を読み込み、onedirの起動検証後にonefileを生成する。実行中のexeを上書きできない場合はBridge/Edge接続を終了してから、表示されるビルド済みexeをルートへコピーする。

コマンド入口:

```text
AoiTalk-PC-Bridge.exe                         GUI
AoiTalk-PC-Bridge.exe --headless --config connection.json
AoiTalk-PC-Bridge.exe --install-edge
AoiTalk-PC-Bridge.exe --self-test
AoiTalk-PC-Bridge.exe --licenses
AoiTalk-PC-Bridge.exe --export-licenses NEW_LICENSES.zip
```

### ライセンスの閲覧・再配布用ビルド

ライセンス情報を組み込んだビルドでは、接続前でもGUIの「ライセンス」または
`--licenses` からライセンスと対象ソースの入手先をオフラインで読める。
`--export-licenses NEW_LICENSES.zip` は原文、必要な対象ソース、component inventory、
SHA-256 manifestを新規ZIPへ書き出す。既存ファイルやリンク先を上書きしない。
ライセンス表示・書出しは、Edge接続、desktop操作、サーバー接続を開始しない。

配布用の `scripts/build_pc_bridge_release.ps1` は `-DistributionNotices` に
レビュー済みのnoticeディレクトリを必要とする。既存の専用ビルドvenvを維持し、
AoiTalk本体の大きなvenvを配布依存一覧として使用しない。
`scripts/build_pc_bridge.py` へ直接渡す場合は
`--distribution-notices DIRECTORY --require-distribution-notices` を指定する。
通常の開発ビルドは未完了notice候補も組み込めるが、それは再配布確認済みを意味しない。

notice入力の `manifest.json` は schema 1 / product `AoiTalk-PC-Bridge`。
`files` は相対path、kind（notice / source / inventory）、size_bytes、sha256の
固定allowlistで、未知ファイル、リンク、パス逸脱、hash不一致を持ち込まない。
`component_inventory` が指す原文inventoryも同じhash検証対象とする。
配布ビルドは `review_status: complete` に加え、`build_environment` の
platform / python_version / 実同梱 `runtime_distributions` の版、helper SHA、
`packaged_inventory` の実CArchive / PYZ一覧との一致を確認する。
bridge.4 helperは新しいsource再ビルド候補の固定SHA/size（SHA-256 `83cacd4103400c1a1ae73c9a1fc0148d9afa1c9c65f4ea7f6e131018ac6d7217`、20,263,424 bytes）を使い、`agent_browser` helperを含むproduction候補、配布gate、実機受入入口で同じ値を照合する。bundleのbuild_manifestポインタは `build-manifest.json` の76,101 bytes / SHA-256 `3a92c1662308f9f4d7269bf52763e59cb5e33245cebb6cacee82812368c663ff`を必須とし、source・patch・依存checksum・toolchain・生成placeholder・時刻入力の原文を同梱する。未記録の完全なビルド環境とlinker argvは未記録と明記し、旧binaryの復元やbyte再現性を主張しない。この候補のmain統合・製品置換・公開配布は親レビュー後に行う。
無関係なinstalled distributionの存在だけを配布条件にしない。
同梱構成が変わった場合は新しい実体のinventoryを採取して必要なnoticeを更新する。

構成確認の最初の候補はrelease wrapperの `-AuditOnly` で作る。
専用の空venvでビルドし、候補exeの横へ `.distribution-audit.json` を出力する。
このモードは未完了notice入力を認めるが、配布確認済みとは表示しない。
直接実行する場合は `--audit-distribution --no-install` を使用し、onefileに限る。
auditのinstalled package一覧は参考であり、実同梱の根拠はCArchive/PYZ一覧。
新しい実同梱構成に対応する原文・由来をレビューしてmanifestを更新した後、
`-AuditOnly` なしで配布用gateを実行する。

ビルド後のexe自身が書き出したZIPを元のnotice全ファイルとbyte比較し、
一致後にだけexeを配置する。manifestは署名や一般的な法的保証ではなく、
レビュー対象の実同梱構成と原文を固定するためのもの。
完成扱いには、この検証に加えて当該Windows exeのGUI/接続動作確認が必要。

MPL対象の `option-ext 0.2.0` は小さな原版ソースをnotice ZIPにも同梱する。
`axe-core 4.12.1` は改変していないminified payloadの由来と、編集用ソースの
固定revision / 入手URL / SHA-256を知らせる。minified JSを編集用ソースと扱わない。
新しい配布条件で対象ソースを変更した場合はソース提供内容も更新する。
これらのためにAoiTalkの無関係な非公開ソース全体を公開する必要はない。

確認に使用する一次資料:
- [MPL 2.0 3.1–3.4](https://www.mozilla.org/en-US/MPL/2.0/)
- [MPL FAQ（未改変コードの配布、ファイル単位の範囲、minified JS）](https://www.mozilla.org/en-US/MPL/2.0/FAQ/)
- [PyInstaller 6.22.3 の配布例外](https://pyinstaller.org/en/v6.22.3/license.html)
- [rav1e 0.8.1 のAOM特許許諾原文](https://github.com/xiph/rav1e/blob/v0.8.1/PATENTS)

## 検証

実Edgeへ触れないCLI受入試験は [Portable Bridge Windows受入試験](pc_bridge_windows_acceptance.md) に従う。`tests/test_pc_bridge_windows_acceptance.py` は専用loopbackフォームを使ったhelper ABI、snapshot scope、安全な候補projection、fill/select、verify、stale ref、`end_run`、接続試行一回、unknown/timeout時の再送禁止を、BridgeClientと実adapterで確認する。JEV service、Chat tool、device routing、page clickはこのrunnerの対象外である。`scripts/accept_pc_bridge_windows.py --live` は明示的に許可したWindows試験だけで使い、既存Edgeを起動・終了せず、保存済み設定・token・cookieを読み込まず、CDP endpointを証跡へ書かない。これはCLI/source APIとhelperの受入であり、GUI・WSS relay・本番Chat・実ログインsessionのPASSへ置き換えない。

`tests/test_pc_bridge.py`: ユーザー識別、登録/解除、選択、別ユーザーのPC拒否、WebSocket relay、切断、別loopからの呼び出し、コマンドtimeout、入力停止、別PCへfallbackしないことを確認する。

`AOITALK_PC_BRIDGE_DB_TEST=1` は実PostgreSQLに一時のユーザー名前空間を作り、保存・再読込・解除を確認する。既存ユーザーやglobal設定を変更せず、作った行は最後に削除する。

`AOITALK_PC_BRIDGE_LIVE=1` と `tests/test_pc_bridge_live.py` は、productionのrelayコードで一時サーバーを起動し、**実際のビルド済みexe**を別プロセスで接続する。通常WSと証明書を検証したWSSの双方で、既存Edgeでのfixtureログイン→LLMによる検索/POST保存、Windowsメモ帳への日本語入力/保存、LLMによるUIA本文読み取り、LLMに本文の全置換と保存を任せる複数ステップ作業を確認する。Cookie注入はしない。最終操作先はユーザーのWindows上の実アプリである。

接続先はループバック上の別プロセス。別の物理PCと実インターネット回線を使った試験を済ませた、という意味ではない。

Web設定コンポーネントのVitest、API型生成、Web/Native Mobileの型検査も実施する。`frontend/e2e-live/pc-bridge.live.spec.ts` は既存AoiTalkアカウントでログインして登録→選択→画面確認→通常ChatからEdge/Windows操作まで行う再実行用テスト。

旧試行では、既存アカウントの認証情報を使う実UI試験の開始が作業ツール側で拒否されたため、**本番アカウントのChat UI完走と独立AIによるWebUI QAは未検証**だった。一時テスト用認証による実relay/exe操作、GUI単体起動・スクリーンショット、コンポーネントテストの成功とは区別する。2026-10-06の公開HTTPS QA login/auth/API probeは別途PASSだが、PC deviceは選択されておらず、新focus版のChat/JEV/GUI通しは未実施である。

## 1.0.0実装時点の検証結果

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


## 1.1.0の対象確認と配布状態

observeはobservation_id、foreground_identity、coverageを返す。computer_useの直接操作でも
直前のobservation_idとtargetを渡せる。自動操作はこれらを伝播し、HWND/PID/作成時刻、
UIA runtime ID・名前・geometry、monitor配置、foregroundとpointer hit-testを確認する。
4,000 node/depth32の有限走査とserver側の候補shard評価により、従来の190/250件切捨てを見直した。

[追加実装と検証境界](jev_execution_search.md) を参照。正式候補のonefile/runtime、self-test、固定helper、notice/hash gateと、専用GUIの接続・切断・再接続、Nativeの日本語desktop操作、Chatの同じdeviceへのfocus確認はPASSしている。
実EdgeのCDP attach、JEVの候補選択、snapshot/ref、click/fill/select、Browser経路のWebUI完走は保存済みpermission blockのため未検証であり、正式候補の公開や稼働中runtimeへの適用とは別に扱う。稼働中のルートexeは上書きしない。


## Windows配布のruntime

正式Windows 10/11 x64候補では、API-set/UCRTをOSから解決し、MFC機能を維持する。`-WindowsRuntimeRedist`はMicrosoft公式x64 REDISTから抽出し、署名確認済み配布元からのmanifestでhash固定したMFC/VC runtime三本を受け取る。builderはmanifestと実DLLのhash/sizeを照合し、インストーラ実行・OS設定変更は行わない。

### 2026-10-04 配布候補の検証結果

- 正式wrapperの専用venvでビルドし、`-AuditOnly`なしの配布gate、self-test、固定helper検証がPASS
- ライセンスZIPの114 files（113本文/source/inventoryとmanifest）は元bytesに一致。既存ZIPへの上書き拒否も確認
- native47 files（DLL12、PYD35）の起源・hashを全件確認。API-set/UCRTの同梱は0件。MFC/VC runtimeは公式Microsoft署名REDISTの14.51.36247.0を使用
- ライセンスGUIの表示・通常終了、製品APIの`BridgeClient.dispatch computer`経路で実nativeを使った日本語入力と再観測がPASS。MFCとwin32uiは保持
- EXE: 31,633,148 bytes、SHA-256 `3be2f956ed5299ff573641c805fb72c660af025af917e2490cd7f221838178d4`

これは配布artifactと上記Computer Use経路の確認結果。Browser/Chat/Jev/WSSを通した実連携、通常のChat UI完走、公開releaseや稼働exeへの適用まで完了したという意味ではない。それらは別の検証項目として扱う。

### 2026-10-06 focus helper / 配布候補の状態

- focus版helperは `0.38.2+bridge.4`、20,263,424 bytes、SHA-256 `d119d2b2d38e747763f5a847e340f25fcc60c3eeb4ef48ba46dc8043545f53fd`。実binaryの `--version --json` とmanifestのprotocol 1、10 capabilities、version 0.38.2が一致した。
- README-licensesとdependency-notices-statusのtechnical input reviewを反映したreviewed EXEは `<private-artifacts>/pc-bridge/cce3bc745-focus-reviewed-20261006/AoiTalk-PC-Bridge.exe`、31,636,364 bytes、SHA-256 `f5738d3ea23cd064817b39e50c870475e2aeb2213f872ad42c1d8062a9a08af5`。旧focus EXE `bb63078a…` は比較証跡として保持する。
- `artifacts/operations/bridge-focus-20261006/product-wss-test.json` の最終WSS transport testは、reviewed EXEの同一hash copy、公開 `/at` HTTPS auth、production Hub WSS、agent_browser protocol/capabilities、online→owned stop→offline→DELETE 200、selected/non-target unchanged、health・private config・TEMP_MEI cleanupをPASSした。これはtransport/lifecycleの確認で、GUI/CDP/act/Chat/JEV/OS focus/Grok通しを含まない。
- `cce3bc7450430805c4e982cd6f7bd4ea8f1d756f` を基点にしたBridge修正ソース（upstream patch SHA-256 `ae6fe51bc3250f0a1e2b26a13fc2b63dd0946d4b4e872e11fc0175b6882aeff2`）に対するfocused Rust 9 suitesは **122 passed**、Python候補は対象496件中 **492 passed / 4 environment skip**。import error 40件は既存baselineの対象外として別証跡に分離した。
- 通常runtimeの管理者restartはPASS、公開 `https://aoi.az-vnetspace.com/at` のQA login/auth status/API probeもPASS。ただしprobe時点の同owner PC Bridgeは `selected_device_id: none`、`online: false`、`edge_connected: false` で、既存offline deviceを流用・削除していない。
- 旧candidateの前面GUI・Native入力・切断再接続PASSは不変証跡として参照する。focus版helperの単一EXE self-test・配布notice/hash gateはPASSしたが、背景Edge入力、Chat/JEV、公開WSS通し、Grokの新GUI確認は未実施または進行中であり、完了扱いにしない。

## notice整備のreview候補

公開source向けに、`resources/agent-browser-bridge/build-manifest.redacted-historical-provenance.json` を参考情報として置く。原本 `build-manifest.json` からuser名と開発checkoutのpath 3箇所だけを伏せた写し（76,125 bytes / `d1d68246…`）で、redacted historical provenanceであり、新しいbuildの証跡でもproduction bundleの成果物でもない。runtimeが検証するのは、明示的に指定したexternal bundleの原本（76,101 bytes / `3a92c166…`）だけである。

このbranchはruntimeのhelper許可pinも更新する。新しいBridge runtimeの通常bundle検証（`load_helper_bundle()`の既定、または `require_pinned=True`）はhelper `83cacd41…` とbuild-manifest `3a92c166…` の両方を要求し、旧helper `1b239a67…`／旧manifest `630a5d99…` を拒否する。`verify_helper()` とSupervisorの既定pinも新helperを要求する。明示directoryを渡すbuild/test用の検証境界は通常設定からのunpin経路ではない。

更新時はruntimeコード・helper EXE・bundle manifest・build-manifest・元のsource/noticeを同時に配布し、packaged helper検証と通常browser／media profile双方のreadinessを確認する。コードだけ、またはhelperだけの差替えでは接続対応済みとしない。既存frozen Bridgeの内蔵pinはサーバーコード更新で自動変更されないため、その自己検証PASSを新しい専用準備への互換性証拠に使わない。新候補はMicrosoft再頒布権とrelease reviewが未確認で、正式配布済みではない。

Rust aggregateは2,241,101 bytes / SHA-256 `ef228f39562540bc5ba40dfb99074f0c03d640ae2b4694a7c0a14b9fab52e18a` の原本をそのままcopy・exportする。内部のexr LICENSE.md 32個、generic-array LICENSE 20個、lebe LICENSE-BSD-3-Clause 26個のCRLFも維持する。全427原本の実bytes SHAをexportから再計算する回帰テストを持つ。旧notice候補で改行変換された2,241,023 bytes / `2c064b48…` はこの原本条件を満たさず、正式配布へ使用しない。

`scripts/prepare_pc_bridge_distribution_notices.py --seed REVIEWED_INPUT --helper-bundle HELPER --output .local/CANDIDATE` は、既存のhash検証済み入力から新規候補だけを組み立てる。原本・AoiTalk自身のMIT本文・Microsoft規約本文・補足crateのCOPYRIGHT/PATENTS・変更表示を含める。`--inventory-executable` を付けると、実onefileのPE payload集合と全native hashを照合し、current helper以外は既存の公式原本hashとの一致を要求する。既存出力は上書きしない。

notice assemblyの完了と再配布権の確認は別の条件である。新候補は `pending_parent_release_review` を維持し、Zuneの選択、Microsoftの適用licenseと再配布権の根拠、release reviewを自動で承認しない。正式配布gateはrequired原文hashと `distribution_checks` を確認するため、manifestのcomplete表示だけでは通過しない。

Windows対象5版のZuneはCargo.tomlとsource headerがMIT / Apache-2.0 / Zlibの三択を示す。featureは変更せずZlibへ統一する案を親reviewへ提示する。旧3版のcrateにはlicense fileがないため、固定revisionのZlib本文とattribution原文を使い、既存の別license原文も削除しない。

rav1e 0.8.1のPATENTSは同じcrateから回収したAOM Patent License 1.0の原本で、CP1252 bytesを変更せず同梱・exportする。Rand系、utf8_iter、tiffのCOPYRIGHTも補足する。tiffのtest素材の表示は保守的に保持し、binaryへのlinkageを主張しない。ringのAND条件、unicode-identのUnicode条件、webpki-rootsのCDLA本文は元のaggregateに保持する。

Microsoftの対象は公式14.51.36247.0由来の `mfc140u.dll` / `vcruntime140.dll` / `vcruntime140_1.dll` に限定する。公式署名と原本hashの一致は技術的な由来確認であり、配布主体のVisual Studio / Build Toolsの再配布権や規約同意の証明には代えない。新規規約同意・購入・製品置換はこのnotice作業では行わない。
