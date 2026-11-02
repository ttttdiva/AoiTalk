# 41 の操作対象の一本化（Local First）設計

状態: 設計（2026-10-29 更新）。実装は段階 S1〜S6 に分ける。切り替えが終わるまで、動いている PC Bridge は止めない。各段階を merge したら「再起動要」を統括に報告する。

## 目的と最終の形

- **既定**: エージェントの操作（ブラウザ、画面、アプリ、ファイル）は、AoiTalk を動かしているサーバー PC だけで行う。41 の中から直接使い、別の exe を経由しない。Bridge が無くても、すべての機能が動く。
- **オプション**: 設定タブの「他の PC の制御」を**オン**にしたときだけ、Bridge の exe を入れた別の PC を登録し、操作対象として選べる。既定はオフ。
- **PC Bridge の exe**: 削除しない。オプション用の配布物として残す（build の手順と配布の場所は、本書の「Bridge の配布」を参照）。この PC では常駐させない。
- **ブラウザ**: 41 が起動時に管理する専用プロファイルの Edge と Jev に一本化する。普段使いの Edge を操作する経路（DevToolsActivePort の許可方式）は、既定では使わない。
- **呼び出し側から見える面は 1 つ**にそろえる。中身は、ローカル実装（41 の中）とリモート実装（Bridge 経由）の 2 つとし、対象 PC（device）で選ぶ。

## 現状（調査結果、コードで確認済み）

- `src/services/local_browser/cdp_broker.py` が専用 Edge の起動、CDP 接続、loopback endpoint の発行を管理する。broker manager は同じリポジトリの Python でこれを起動する。`pc_bridge/edge_broker_control.py` は broker と認証済み named pipe で通信する共有 client で、Bridge exe からも利用する。
- broker 実装、起動、専用プロファイルの保管先はすべて 41 の中にあり、**80_HomeMCPGateway への依存は無い**。Playwright は遅延 import され、他の local browser module は起動時に broker 用 Playwright を読み込まない。
- Bridge に依存する箇所は、「Bridge を取得する場所」と「readiness の判定」に集中している。
  - 取得: `crowdworks_source.py:211`、`crowdworks_workflow.py:3320`、`crowdworks_browser_adapter.py:93`、`tools/computer_use.py`（`resolve_control_bridge`）
  - readiness: `agent_action_policy_service.py:270-297` の `_ready()`
  - これらが使う面は `request(command, **params)`、`browser_backend`、`operation_lock`（browser）と `computer(action, **params)`（画面）だけである。
- 登録は専用テーブルを持たない。`app_config_settings` の `pc_bridge:<user_id>` に JSON で保存されている（トークンは SHA-256 のみ）。

## 構成（ローカルとリモートの 2 backend）

```text
呼び出し側（CrowdWorks / AoiDots / Jev / computer_use / apps / media）
      |
  resolve_target(owner, device_id=None)   <- 既定は LocalTarget
      |
  +---+-------------------------+
  | LocalTarget (41 の中)       |  RemoteTarget(device)  <- 設定「他の PC の制御」オン時のみ
  |  browser  -> LocalBrowser   |    PcBridgeHub / RemoteControlBridge (WSS)
  |  computer -> LocalDesktop   |
  |  apps     -> (server target)|
  |  files    -> 共有ファイルシステム
  +-----------------------------+
```

- 共通の面（暫定名。S2 で確定）: `target.browser()`（`request` / `browser_backend` / `operation_lock`）、`target.computer()`（`computer(action, **params)`）、`target.status()`。いずれも今の `RemoteControlBridge` と同じ形にして、既存の `BridgeInspectionLease` と `BrowserAgentService` を変更なしで動かす。
- `device_id` を指定しなければ `LocalTarget`。リモートを使えるのは、設定「他の PC の制御」がオンで、device が登録済みのときだけ。

## ブラウザ（S1〜S4）

### 起動の順番（41 → broker → 専用 Edge → Jev ready）

1. 41 の起動: `service_manager.start_services` が qdrant の直後に `start_managed_browser_broker` を呼ぶ。失敗しても 41 の起動は止めない。
2. broker: control（named pipe）に status を聞く。生きていて `mode=dedicated` なら attach、いなければ 41 の Python で `python -m src.services.local_browser.cdp_broker --serve` を detach 起動する（broker は単一インスタンス lock を持つ。detach するので 41 の再起動で broker と Edge は落ちない）。broker は 41 の中で完結し、80_HomeMCPGateway を検索・起動しない。
3. 専用 Edge: broker の `ensure_browser` が専用プロファイルの Edge を起動または再利用する。許可画面は出ない（`--user-data-dir` が非既定のため）。
4. LocalBrowserRuntime: endpoint を取得し、`BrokeredPlaywrightRuntime` で接続する。
5. Jev ready: `LocalBrowserBridge` が使える状態になったら ready を報告する。

### 旧プロファイルからの移行

- 新しい専用プロファイルは `%LOCALAPPDATA%\AoiTalk\browser-profile` に保存する。初回起動時、新しい場所が無く `%LOCALAPPDATA%\HomeMCPGateway\edge-profile` があれば、旧プロファイル直下の `lockfile` を排他的に開けるか、旧 `DevToolsActivePort` の port に loopback TCP 接続できないことを確認してから、同じボリューム内の `os.replace` で移す。コピーは作らず、移行後に旧ディレクトリは残らない。
- 新旧両方のディレクトリがある場合は新しいプロファイルを使い、旧側には触れない。旧 Edge が使用中なら移行を中止し、固定コード `edge_broker_profile_in_use` で失敗する。
- 切り替え前に、タスク スケジューラの旧タスク `Home MCP Edge CDP Broker` を停止し、無効化してから AoiTalk を再起動する。旧 broker と旧 Edge が動いたままだと、新 broker は `edge_broker_profile_in_use` で起動を拒否する。

### 落ちたときの立て直しと通知

- watcher が 15 秒ごとに broker の status と接続を確認する。health は no-I/O のスナップショットだけを読む。
- 立て直し: broker が死んでいれば再起動、endpoint が変わった/切断されたら再接続、Edge が死んでいれば `ensure_browser` で起動し直す。backoff は 5、15、45、120、300 秒で、10 分に 5 回まで。上限を超えたら固定コード `browser_restart_limit` で止める。
- 通知: `ready=false` または `cdp_connected=false` が 10 分続いたら、AoiTalk の通知（`notification_deliveries` の in_app。既存のワーカーが画面とスマホへ届ける）を出す。同じ理由は同じ UTC 時間帯に 1 回まで（`dedupe_key` = `browser:<reason>:<UTC の YYYYMMDDHH>`）。`browser_login_required` も連続失敗が 10 分続いたら同じ仕組みで通知する。通知先は `AOITALK_LOCAL_BROWSER_OWNER_USER_ID` の UUID で指定する。未設定・不正時は通知とログイン注入を行わず、`owner_not_configured` を一度だけ警告する。通知の積み方は `frontend/scripts/notify-owner.mjs` を使う。
- 理由コードは固定のもののみ。endpoint・token・cookie の値は、ログにも通知にも出さない。

### ログインの持ち方

- 専用プロファイル（`%LOCALAPPDATA%\AoiTalk\browser-profile`）に保存する。PC を再起動しても残る。専用プロファイルには Edge の同期と拡張機能を入れない（`--disable-sync --disable-extensions`）。
- **AoiTalk 自身**: パスワードを使わず、cookie を入れる。`frontend/scripts/issue-local-browser-session.mjs`（実装済み。有効期間 7 日）で発行し、ブラウザが ready になったときと、その後 1 時間ごとに期限を確認する。cookie が無い、または期限まで 48 時間未満なら専用プロファイルへ入れる（`LocalBrowserLogin`、S1b）。cookie 値は発行から注入までのメモリ内だけで扱う。
- **外部サイト（CrowdWorks など）**: 専用 Edge のウィンドウで、利用者が最初に 1 回ログインする（エージェントはパスワードを入力しない）。以後は cookie で維持される。
- **確認**: `login_state(site_url)` は同期 I/O を行い、Playwright で指定 origin に一致する cookie の件数・最長期限・ログイン有無だけを返す（cookie の名前・値は参照しない）。

### 共通の API（ブラウザ）

1. 状態: `local_browser.status() -> BrowserStatus`（`ready`、`cdp_connected`、`mode`、`broker_phase`、`error_code`）。no-I/O のスナップショットで、ログイン情報は含まない。前提なしで skip する判定は `ready and cdp_connected`。
2. 操作: `local_browser.bridge()` が `LocalBrowserBridge` を返す。`await bridge.request(command, **params)`（`resolve_tab`、`observe`、`act`、`navigate`、`back`、`scroll`、`verify`、`crowdworks_inspect`、`end_run`）、`browser_backend`、`operation_lock`。
   - 同じ専用 Edge を、サーバー内の全呼び出し元が 1 つの lock で順番に使う。
   - 操作は CDP を基本とし、ウィンドウを前面に出さない。前面でないと止まる操作（click の actionability など）が必要なときだけ、その操作の直前に前面化する。

## ブラウザ以外の機能の棚卸し（S5 の根拠）

調査はコードの読解のみ。本番の設定値（`browser_agent.dedicated_enabled`、`apps.actions.*`）と DB の中身は未確認。

| 機能 | 状態 | 主な呼び出し元 | ローカル化の工数 | 推奨 |
| --- | --- | --- | --- | --- |
| `computer` | (a) LLM ツール `computer_use` / `computer_observe` で使用（deferred の `browser` pack）。UI は「画面を確認」のみ | `tools/computer_use.py`、`computer_use_service.py`、`pc_bridge_routes.py` の screen | S〜M | **置き換え** |
| `apps` | (b) 既定で無効（`apps.actions.enabled=false`）。`pc_saved_action` を宣言した配布アプリは無く、UI も無い | `PcBridgeAppActionTransport`、`app_action_runtime_service.py` | 移植は L、削除は S | **削除案**（server target が既にある）。要承認 |
| `media_asset` | (b) Operations の「Bridgeで準備のみ」でアセットがあるときだけ | `media_bridge_asset_transfer.py` | S | **置き換え**（同じ PC なのでファイルパスの受け渡しにする） |
| `media_prepare` | (b) 利用者の明示操作。コード側で「受入未了」の blocker が付いている | `media_browser_preparation.py`、`media_bridge_connection.py`、`persona-publish-panel.tsx` | L | **置き換え**（S5 の後半。ローカル browser backend 上で動かす）。要確認: 機能を残すか |
| `dedicated_browser` | (b) 既定で無効（`dedicated_enabled=False`）。管理者の明示有効化が必要 | `browser_agent_dedicated`、`dedicated_jev_runtime.py`、`skill_*` | L（79k 行の同梱 Jev を含む） | **削除案**。要承認 |
| `browser` | (a) 使用中（CrowdWorks、`browser_agent`） | `crowdworks_*`、`tools/browser_agent.py` | L | **S1〜S4 で置き換え** |

各機能の要点:

- **computer**: Bridge の `DesktopController`（`pc_bridge/desktop.py`、451 行）は Bridge への依存がほぼ無く、ライブラリとして移せる。`pythoncom` / `PIL` / `pywinauto` は関数内で遅延 import する（41 の起動を重くしない）。COM の初期化があるため、専用の 1 スレッドで動かす。サーバーが対話的なデスクトップセッションで動いている必要がある（Windows サービスやセッション 0 では画面取得も入力もできない）。`ComputerUseService` に必要なのは `computer()`、`device_id`、`operation_lock` だけなので、`LocalDesktopBridge` を同じ形で作る。
- **apps**: 41 には既にサーバー実行の経路（`app_action_server_transport.py`、`app_action_server_supervisor.py`、`app_action_server_journal.py`）がある。`pc_saved_action` と `pc_bridge` target を使う配布アプリが実在するかは、本番の DB で未確認。実在しなければ削除（`execution_target_kind` の CHECK 制約の migration が要る）。実在するなら、`action_*` 一式（約 2,200 行）をサーバーに移す。
- **media_asset / media_prepare**: `MediaBrowserPreparationService` は `bridge` 面だけに依存し、`bridge_factory` の差し込み口がある。ローカルでは `LocalBrowserBridge` の上で動かす。DOM の手順は現在 Bridge 内の `driver.cjs` と拡張機能の JS にあり、移す作業が本体になる。承認・冪等性・「最終クリックをしない」・入力の fence は維持する。接続の epoch と `device_id` は不要になる。
- **dedicated_browser**: `media_prepare` と同じ土台（プロファイル、ドライバー）を使う。既定で無効のため、使われていなければ削除する。

## 段階

| 段階 | 内容 | 主な変更 | 担当 |
| --- | --- | --- | --- |
| S0 | 本設計の合意 | `docs/local_browser_architecture.md` | Edge 担当 |
| S1 | `src/services/local_browser/`（status、broker 管理、runtime）、service manager への hook、`/health` の `browser`。既存の挙動は変えない | 新規ファイル中心 | Edge 担当（restart-drain と調整） |
| S1b | `LocalBrowserLogin`（41 自身の cookie を専用プロファイルへ。`login_state`）、通知 | 新規 | Edge 担当 |
| S2 | `LocalBrowserBridge`、`resolve_target`。CrowdWorks の取得場所と `_ready` を設定 `browser.backend=local\|bridge`（既定 bridge）で切り替え可能にする | `crowdworks_*`、`agent_action_policy_service.py` | CrowdWorks 担当（bridge は Edge 担当が提供） |
| S3 | CrowdWorks の scan を local で実機確認し、既定を `local` にする。確認後に Bridge（PID 55904 / 70344）を止める | 設定のみ | 統括の指示で切り替え |
| S4 | 他のブラウザ consumer（`browser_agent`、AoiDots、skill）を local に移す | 各 consumer | 担当ごと |
| S5 | `computer`、`media_*` をローカル実装に置き換える。`apps` と `dedicated_browser` は承認後に削除。共有している定義（`action_protocol` など）を `src/` に移し、`pc_bridge/` への import を解く | `src/services/local_*`、`pc_bridge_service.py` | Edge 担当（調整） |
| S6 | 設定タブに「他の PC の制御」（既定オフ）。オン時だけ device 登録・選択を有効化。UI は Operations の再設計の最大幅とカードの形にそろえる（必要なら ops-ui-redesign に相談） | `frontend/src/components/settings/`、`pc_bridge_routes.py` | Edge 担当 + UI |

## Bridge の配布（オプション用の成果物として残す）

- build: `Build-PC-Bridge.bat` → `scripts/build_pc_bridge.py`（PyInstaller 6.22.3）。配布物は `AoiTalk-PC-Bridge.exe`。リリース手順は `scripts/build_pc_bridge_release.ps1` と `docs/pc_bridge.md`、`docs/pc_bridge_windows_acceptance.md`。
- 配布の場所: サーバーの `/api/pc-bridge/download`（リポジトリ直下の `AoiTalk-PC-Bridge.exe` を配る）。S6 の後は、設定「他の PC の制御」がオンのときだけ表示する。
- S5 で `pc_bridge/` のうちサーバーが共有している定義（`action_protocol`、`browser_commands`、`edge` の `EdgeBridgeError`、`action_identity`、`media_asset_transfer` の定数）は `src/` 側に移し、Bridge の exe は `src/` を import せずに build できる状態を保つ。`pc_bridge/` 内のブラウザ系コード（broker 制御、agent-browser アダプタ）は、サーバー側でも使うため、共有ライブラリとして残す。

## 決めてほしいこと

1. **apps**: 本番の DB で `apps.actions.execution_targets` に device が設定され、`pc_saved_action` のアプリが実在するか。無ければ削除でよいか。
2. **dedicated_browser**: `browser_agent.dedicated_enabled` が本番で有効か。無効なら削除でよいか。
3. **media_prepare（Bridgeで準備のみ）**: 機能を残すか。残す場合は工数 L（ローカル browser backend 上に DOM 手順を移す）で、受入未了の blocker は別途残る。

## 未確認

- `computer` はサーバーが対話的なデスクトップセッションで動いていることを前提にする（起動方法は未確認）。
- `ManagedQdrant` の watcher の再起動方針、service manager の再設計（restart-drain）とのフック形式の調整。
- 本番設定値（上記 1〜3）と、`ExternalConnection.metadata_json.binding_mode == "bridge_manual"` の行の有無。
- 専用 Edge を複数の呼び出し元が共有することによる前面の奪い合い（lock と CDP 基本で緩和するが、完全には防げない）。
