# Portable Bridge: 限定 Windows 受け入れ試験

`scripts/accept_pc_bridge_windows.py` は、既存 Edge と配布済みの
agent-browser `0.38.2+bridge.4` を使う、一接続・再試行なしの検証入口。
通常の pytest から実ブラウザには接続しない。

## 範囲と事前条件

- 最新の対象ソース、既存 `venv`、既存 kit4 helper bundle を用意する。
  この手順ではインストール、ビルド、ダウンロード、設定変更を行わない
- 対象 Edge はすでに起動し、利用が承認された正確な loopback WebSocket
  endpoint が分かっていること。別の endpoint や profile は探さない
- 同時に Edge を操作しない。新しい接続許可・Windows/Edge セキュリティ画面、
  CAPTCHA 等が出た場合、操作担当は承認・回避せず Ctrl+C で停止する
- runner は Edge を起動・終了せず、既存タブへ navigation しない。
  表示された専用 URL を、対象 Edge の新しい一タブに操作担当が開く。
  承認済みのローカル QA 担当も、通常の許可済みアプリ URL 起動経路で
  **その表示 URL だけ**を開き、フォームを確認して `RUN` を入力してよい。
  ユーザーに同じ範囲の再確認を求めるための手順ではない。
  debug flag・registry・profile の変更や別 browser の起動は行わない
- Bridge の保存済み設定・token は読み込まない。API クライアント用の設定は
  メモリ内だけ。テストの対象は自作 form の二つの control だけ

## 実行

接続実行を明示的に依頼された Windows QA の一回で使用する。
`$Endpoint` は当該 Edge の検証済み endpoint、`$HelperBundle` は
`manifest.json` と `agent-browser.exe` を直接含む配布ディレクトリとする。
onefile exe のみの場合、この runner 用の helper を別途ダウンロードしたり
実行中の展開先から推測したりせず、既存の正式 bundle を確認する。

```powershell
.\venv\Scripts\python.exe scripts\accept_pc_bridge_windows.py --live `
  --bundle-dir $HelperBundle --endpoint $Endpoint --channel stable
```

必要なら `--source-revision <40桁のcommit SHA>` を加えられる。
これは申告値として記録し、実際の runner / `pc_bridge/*.py` の SHA256 は
別途保存する。commit SHA だけで未コミット差分がないとは判定しない。

1. コンソールに出た `http://127.0.0.1:<一時port>/<nonce>/` を対象 Edge の
   新しいタブで開く。自作フォームを確認して `RUN` と入力する
2. 以後は一度だけ discover/connect、正確な title＋origin が一つだけ一致した
   タブを select。resolve の origin と scoped interactive snapshot の
   helper 内の full URL predicate、固有 textbox / combobox 名を確認する
   （AX tree が select の option を ref として返す場合、二候補まで許容。
   bridge.4 の編集値 redaction で option 名が空になる場合も含む。
   固有 textbox / combobox は各一個を必須とし、任意 label は許容しない）
3. 日本語入力 → 値 verify、option select → 値 verify を各一回実行する。
   このrunnerはJEV service、Chat tool、device routingを呼ばず、JEVへ渡す
   候補projectionに相当する安全な候補境界と、helperのfill/selectだけを検証する。
4. 再観測前の ref を意図的に使い、`edge_stale_element` / `executed=false` を
   確認する。未適用であるべき文字列と現在値は異なり、現在値も再 verify する
5. `end_run` の ref 無効化と接続保持、終了後 observe の `run_required`、
   接続試行数が厳密に 1 であることを確認する
6. 自分の helper Job と loopback server を停止。OS の読み取り専用検査で
   記録済み helper identity、server port、元 Edge の identity を照合する。
   コンソールの案内に従い、最後に検証専用タブだけ閉じる

いずれかの不一致・unknown・timeout は、その stage で停止し、新しい接続や
失敗 action の再送をしない。helper 呼出しのタイムアウトは製品既定の35秒、
preflight は10秒。同じ stage 内の境界検査にもそれぞれ上限が適用される。
新しい許可 UI を runner 自身が検出できる保証はなく、操作担当の監視が必要。
デスクトップが表示できず screenshot が黒い場合は、撮り直しを無期限に
繰り返さず画面 QA を未検証として報告する。API 証跡は画面 QA と分けて扱う。

## 証跡と後片付け

### 接続失敗の専用診断

snapshot以前で失敗するときは、`scripts/diagnose_pc_bridge_connection.py`を
明示的に許可したWindows実行で使う。これは再利用可能なsource API診断で、
単一EXEの受入やChat/Jev成功を示すものではない。設定の有効化、権限変更、
保留された接続の自動再試行には使わない。

入力はrepo配下`.local/`の最小JSONを作り、`endpoint`、`channel`、必要なら
既に明示選択済みの正確な`target_id`だけ指定する。登録設定JSONやtokenは
渡さない。target指定時はそのtargetの`resolve_tab`、省略時は未選択の
metadata discoveryまで実行する。新しいtargetの自動選択、navigation、
snapshot、入力操作は実行しない。既存Edgeの所有・loopback検証と、配布用の
manifest/helper固定hash、strict refと既存targetの制約は製品実装を使う。

```text
venv\Scripts\python.exe scripts\diagnose_pc_bridge_connection.py --live --bundle-dir <同じ配布helperのbundle> --input .local\bridge-connect-input.json
```

`--live`なし、またはWindows以外では接続前に停止する。1回の実行で
接続は1回だけ。`preflight`、`initial_connect`、`target_select`、
`tab_list`、`local_target_list`の該当stage、boolean、allowlist内の固定codeを
`.local/bridge-connect-diagnostic-<nonce>/report.json`と標準出力へ保存する。
helperの`code`と`data.executed`は公開エラー変換前に記録する。未知code、
例外本文、endpoint、target ID、private argv、本文、cookie、設定値は保存しない。

対応helperが`code=unavailable`と共に返す`data.connectionDiagnostic`は、
次の6値だけ`connection_diagnostic`へ記録する。

- `websocket_timeout` / `websocket_connect_error`: WebSocketのhandshake・接続
- `target_discovery_timeout` / `target_discovery_error`: `Target.setDiscoverTargets`
- `target_metadata_timeout` / `target_metadata_error`: `Target.getTargets`と応答の型検証

公開codeとexecutedの意味は変更しない。errorは権限拒否を断定するものではない。
旧固定helperはこの任意fieldを持たず、同じバイナリの再実行だけでは切り分けが
増えない。source checkpointの変更後はRust回帰、Windows build、実体hashの
計測と配布pin更新が必要。診断目的でもhash照合を無効にしない。

所有helperのJob終了とOS上の記録済みprocess終了、既存Edge identity維持を
検証する。helper終了を確認できた場合のみ専用stateを削除し、確認不能なら
失敗として保持する。`connection_succeeded`とcleanupを含む全体の`success`を
分けて記録する。入力JSONと未清掃stateにはendpointが含まれるため共有せず、
診断後はその実行用入力だけを担当者が削除する。

### 完全な受入試験の証跡

`artifacts/pc-bridge-acceptance/<nonce>/` に次を保持する。

- `runner.py`: 実際に使った入口の完全なコピー
- `fixture.html`: 自作 fixture の完全なソース
- `report.json`: source/helper hash、各 stage の expected / allowlist による
  actual、成否、失敗 frame / 行番号、cleanup の実測結果

各 stage は呼出し前、返り値投影の直後、assertion 後に atomic replace で
保存し、cleanup 前にも保存する。failed stage の実際の返り値を捨てない。
生の例外本文・source line・locals は保存せず、traceback の型・既知の固定
error code・frame / 行番号を保存する。非公開タブの title / origin / URL /
target ID、raw snapshot、debugger endpoint、profile path、token は保存しない。
discover は総数と自作タブ一致数だけを証跡にする。

自作 nonce scope の snapshot だけ、既に返った helper 応答をメモリ内で
再解析し、`snapshot_diagnostics` に型・件数・有限の parse 理由と parser の
失敗行を保存する。追加 helper / CDP 呼出しや再接続はしない。origin が
専用 loopback origin と一致しない場合は診断を抑止する。raw snapshot、
ref、label、入力値、任意の key / exception 本文はここにも保存しない。
`repeated_ref` は helper が同じ ref を複数行に描画したことを示す。
Python は重複を捨てて通さず、従来どおり snapshot 全体を拒否する。

この診断は原因を決めつけるためのものではない。helper の scope root 判定は
backend ID を持たない AX wrapper を挟むと子孫 ref を重複描画し得るため、
ソース回帰で修正を検証する。実 Windows 失敗の直接原因と呼ぶには、
当該実行の parse 理由による確認が別途必要。

一時 helper state は `.local/pc-bridge-acceptance/<nonce>/`。helper の
Job 終了と server 停止・port 解放が確認できた場合だけ削除する。
確認できなければ PASS にせず、一時 state を残す。その内容には endpoint が
含まれ得るので、証跡フォルダと混ぜて共有しない。既存の設定ディレクトリは
削除しない。作成された空のディレクトリだけ安全に除去する。

元 Edge の identity 検査は read-only。プロセス名一括 kill、直接 CDP、
既存ブラウザの close、別タブへの復帰操作をしない。
adapter に close_tab API がないため、専用タブの終了だけ操作担当が行う。

## オフライン検証

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_pc_bridge_windows_acceptance.py `
  --basetemp=.local\acceptance-offline
.\venv\Scripts\python.exe -m ruff check scripts\accept_pc_bridge_windows.py `
  tests\test_pc_bridge_windows_acceptance.py
```

単体テストは実 `BridgeClient.browser_setup` / `dispatch` と実 adapter を
mock helper ABI で通す。live 入口は呼ばず、socket / browser / helper を起動しない。
テスト後の `.local/acceptance-offline` は削除してよい。

この runner の PASS は **source API＋packaged helper＋自作 form＋手動tab cleanup確認** の結果。
JEVの実呼出し、Chatの同device routing、GUIの接続ボタン、Computer UseのUIA/SendInput、
通常Edge login sessionの保持は含まれない。
exe の GUI、WSS relay、外部アカウント、任意サイト、native permission UI、
実際の Windows 試験が未実施の状態を、このオフライン PASS で代用しない。
helper/server cleanup後に検証専用tabを操作担当が閉じて `CLOSED` と入力するまで、
runnerのstatusは `cleanup_pending` であり、PASSではない。
source API の backend factory の build/test 用 injection seam を使い、bundle と
一時 state の場所だけを選ぶ。identity verifier / owned Job / helper contract /
scope・strict-ref 処理は製品実装を使用する。


## 背景GUIからの初回入力回帰

背景入力修正後のhelperと単一EXEの別々のSHA-256を記録し、正式Hub/API → WS →
EXE GUI → 同梱helper → 選択済み既存Edge tabの経路で対象だけを再確認する。
既に合格した無関係なGUI項目を全て再実行する必要はない。

1. 許可済み合成tabを選択後、Bridge GUIを前面、Edgeを背面にして新snapshotを取る
2. 最初の日本語typeで既存tabが前面化し、入力が正しく反映されることを確認する
3. 同様にtextareaの改行を含むliteral fillを確認し、別tab・別アプリへ入力されないことを確認する
4. 前面化してもnative focusが成立しないfixtureではclear/insertが0、executed:false、接続保持、新snapshot必須を確認する
5. clear後・途中入力後のfocus driftとCDPエラーはunknownで停止し、自動再送しないことをRust回帰で確認する

単体回帰の成功はWindows実helper/実GUIでの修正完了を意味しない。

## 通常チャットを開始する前の確認

製品GUIの表示だけで実行可能と判断しない。同じ認証ユーザーの選択PCについて、
devices APIで `online=true`、`browser_backend=agent_browser`、`browser` capabilityを確認する。
Edge接続・タブ選択後は `edge_connected=true` と、選択済みタブに対する
`resolve_tab`（URLを渡さない）・`verify(expected_url)` の一致を確認する。
`resolve_tab` にURLを渡すとnavigationするため、対象の確認に使わない。
接続が継続していることを再確認してから、通常のconversation dispatchを一回送る。
`browser_agent` と選択PCの既存Edgeを操作する依頼であることを明示し、実runの
tool call、JEVの選択trace、製品の入力結果を照合する。queuedやモデルの完了返答だけをPASSにしない。

切断または再接続後は、同じPCのEdge接続・対象タブ選択と上記確認を再度行う。
offline、対象不一致、別の業務ツール実行、unknownの場合は送信・入力を繰り返さず停止する。
未実施の入力・背景条件・再接続・Computer Useは未実施として残す。
PyInstaller onefileのlauncherとchildは一組であり、PIDが二つあるだけで二重起動と判断しない。

検証PCの接続設定は秘密値を出力せず `data/pc-bridge-qa/` に保管する。
実行用コピー・fixture・TEMP/MEIはMEGA除外した `.local/` の固有領域へ置き、
所有プロセスだけを停止して撤去する。既存のPC登録・接続設定・Edgeは維持する。
