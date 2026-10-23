# AoiTalk Plugin（41単独導入）

このディレクトリはAoiTalk MCP v2サーバーの正本です。41 checkout内で、AoiTalk API本体とは別プロセスとして動きます。80 checkout、80のPython環境・`.env`・`PYTHONPATH`は使いません。41本体のMCP v1依存、API、DBには手を加えません。

公開catalogは35 tools（旧21 toolsと管理14 tools）です。Task fields、Projects、Tags、Knowledge sourcesを扱い、Docs候補は`docs_search`です。Agent、ExecutionProfile、Characterは別概念です。API契約とACLは41本体を使います。

## 41だけで導入する

1. 41 checkoutのルートで次を実行します。専用venvはpackage内の`venv/`に作られ、41本体の`venv/`には触れません。

   ```powershell
   Copy-Item plugins\AoiTalk_Plugin\.env.example plugins\AoiTalk_Plugin\.env
   powershell -NoProfile -ExecutionPolicy Bypass -File plugins\AoiTalk_Plugin\scripts\setup.ps1
   ```

2. [OpenAI公式tunnel-client v0.0.14 release](https://github.com/openai/tunnel-client/releases/tag/v0.0.14)からWindows AMD64版を取得します。確認済みの版は`0.0.14+0f870e50a973fa820d4c409000059e181e8d242b`です。公式asset/checksumを確認して展開し、`.env`の`TUNNEL_CLIENT_PATH`に`tunnel-client.exe`の絶対pathを設定します。代わりに`plugins\AoiTalk_Plugin\.tools\tunnel-client.exe`へ置くか、`PATH`上の`tunnel-client`を使えます。binaryはGitに含めません。setupはclientをdownload/updateしたりTunnelを作ったりしません。

   公式CLIで`--version`、`help quickstart`、`help runtimes`、`runtimes connect --help`、`runtimes status --help`、`run --help`を実行して確認済みです。この構成は`runtimes connect`を使います。

3. `.env`に既存の`AOITALK_BOOTSTRAP_ADMIN_USERNAME` / `AOITALK_BOOTSTRAP_ADMIN_PASSWORD`を設定し、発行済みのAoiTalk用IDを`HOME_MCP_AOITALK_TUNNEL_ID`へ、runtime keyを`HOME_MCP_AOITALK_RUNTIME_API_KEY`へ設定します。既存のlogin identityを使い、新しいadmin、bypass identity、scope拡張は作りません。実ID/keyをREADMEやログへ書かないでください。

   41本体の`.env`を参照する場合は`HOME_MCP_AOITALK_ENV_FILE`に絶対pathを書きます。この場合pluginはbootstrap username/passwordだけを読みます。80の`.env`は参照しません。API URLは`HOME_MCP_AOITALK_URL`で指定でき、既定は`http://127.0.0.1:3000`です。plain HTTPはloopbackに限ります。

   新AoiTalk Tunnel IDはこの41側設定だけに置きます。80側の`HOME_MCP_AOITALK_TUNNEL_ID`は未設定のままにし、homeの`CONTROL_PLANE_TUNNEL_ID`やruntimeを変更しません。1 IDで2つのPlugin catalogを公開できるかは検証していないため、2 IDがOpenAIの一般仕様として必須とは説明しません。

4. 設定を検証してから手動起動します。検証は接続せず、起動はTaskを登録しません。

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File plugins\AoiTalk_Plugin\scripts\connect_tunnel.ps1 -ValidateOnly
   powershell -NoProfile -ExecutionPolicy Bypass -File plugins\AoiTalk_Plugin\scripts\connect_tunnel.ps1
   ```

   aliasは`aoitalk-41`、profileは`aoitalk-plugin-41`、health portは既定`127.0.0.1:8783`です。portが使用中ならlauncherは接続前に失敗します。`AOITALK_PLUGIN_HEALTH_LISTEN_ADDR`を空きloopback portへ変更してください。別checkout/entryのalias、profile、Taskは停止・上書きせず失敗します。

5. ChatGPTのPlugins/Connectors画面でcustom MCP serverを追加し、Connectionを**Tunnel**にして新しいAoiTalk tunnelを選択します。自分のaccountでPluginをInstallし、tools一覧をRefresh/再読込してください。tool call中もtunnel-clientを起動したままにします。

## Windowsログオン時の起動

手動起動を確認してから専用Taskを登録できます。登録scriptは`-ValidateOnly`を実行し、Taskを自動開始しません。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File plugins\AoiTalk_Plugin\scripts\install_autostart.ps1
Start-ScheduledTask -TaskName 'AoiTalk Plugin 41'
```

Task名は`AoiTalk Plugin 41`、working directoryは41 checkout、entryはこのpackageの`connect_tunnel.ps1 -Watch`です。homeのTask/config/runtimeは変更しません。watchは30秒ごとに専用aliasのhealth/readyを確認し、専用aliasだけを再接続します。

状態・PID・時刻は`logs/aoitalk_plugin/status.json`、watchログは`logs/aoitalk_plugin/supervisor.log`、clientログは`logs/aoitalk_plugin/tunnel-client.log`、tool telemetryは`logs/aoitalk_plugin/tool_logs/`です。profileは`data/aoitalk_plugin/profiles/`、cacheは`cache/aoitalk_plugin/`です。

切断・再起動前に利用者へ通知してください。手動復帰はこの41 checkoutで行います。

```powershell
Stop-ScheduledTask -TaskName 'AoiTalk Plugin 41'
powershell -NoProfile -ExecutionPolicy Bypass -File plugins\AoiTalk_Plugin\scripts\connect_tunnel.ps1 -Stop
powershell -NoProfile -ExecutionPolicy Bypass -File plugins\AoiTalk_Plugin\scripts\connect_tunnel.ps1 -Watch
```

`-Stop`はidentity markerとaliasを照合後、このruntimeだけを停止します。Task削除は同じcheckoutで`install_autostart.ps1 -Unregister`を使います。homeのTunnel ID/Taskを復旧目的に変更しません。

## 開発・テスト

root pytestの既定collectionは`tests/`だけなのでpackageのMCP v2 testsはroot MCP v1 suiteに混ざりません。専用venvで実行します。

```powershell
plugins\AoiTalk_Plugin\venv\Scripts\python.exe -m pip install -r plugins\AoiTalk_Plugin\requirements-test.lock
plugins\AoiTalk_Plugin\venv\Scripts\python.exe -m pytest plugins\AoiTalk_Plugin\tests
```

`scripts/smoke.py`は41 APIへ接続せず、stdio initializeと35 toolsのlistを確認します。管理contract fixtureは80から移した固定copyで、41側OpenAPI/Pydantic出典SHAを含みます。API/DTO検証は41 checkoutの既存`venv/`を使います。

41と80の両checkoutが隣接している開発環境では、次のコマンドが80のGit HEADにある対象client moduleとfixtureだけをSHA-256で照合します。無関係なHEAD進行やworking tree差分は比較対象にしません。

```powershell
plugins\AoiTalk_Plugin\venv\Scripts\python.exe plugins\AoiTalk_Plugin\scripts\check_migration_sources.py
```

## 移管元と重複

Plugin正本は41の`plugins/AoiTalk_Plugin/aoitalk_plugin`です。80のclient modulesはdispatcher/supervisor等の内部実装として残ります。MCP packageを80からimportせず、大きな共通化もしません。移管元は80の`e130d4744623f4518cad6785fe200b09e288c317`で、診断分類1 hunkだけ同repo現HEADを採用しています。管理contract fixtureはSHA付き固定copyで両repo同じ内容です。移管中の重複は契約回帰でdriftを確認します。

Enterprise handoffからこの任意Plugin packageを除外します。Enterprise外部連携制約は変更しません。Public配布は既存tracked-file規則でpackageを含めます。ID、runtime key、`.env`、binary、venv、profile、log、cacheは配布しません。
