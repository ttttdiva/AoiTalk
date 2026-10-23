# AoiTalk Plugin の起動

Pluginの通常起動には、リポジトリ直下の `run_plugin.bat` を使います。Plugin専用設定は `plugins/AoiTalk_Plugin/.env` に置きます。root `.env` へのTunnel IDやruntime keyのコピーはしません。

```bat
run_plugin.bat
run_plugin.bat --stop
```

引数なしは起動ensureです。未設定なら `NOT_CONFIGURED` を表示して終了コード2を返し、Pluginは接続しません。初回起動では `AoiTalk Plugin` というタイトルの独立した監視窓を開きます。`STARTED` は監視窓の起動受付を表し、runtimeのhealth/readinessを表しません。実runtimeがhealthyかつreadyになった後だけ、監視窓のstatusに `ready` が記録されます。既に同じwatcherが確認できる場合は既存窓を維持して `ALREADY_RUNNING` を返します。

rootの `run.bat` もmain起動前に `run_plugin.bat` を一度呼びます。Plugin未設定やPlugin側の起動失敗はAoiTalk本体の起動を止めません。AoiTalkのexit 42再起動はmainだけを再起動し、Plugin監視窓やruntimeを再spawnしません。AoiTalkの通常終了でもPluginは独立して動き続けます。

AoiTalk backendが停止している間もPlugin runtimeは監視窓の下で維持します。その間のAoiTalk API呼出は利用できず、backend復帰後は既存tunnel-clientの認証で再開します。

`run_plugin.bat --stop` はinstance nonceに紐づくstop requestを監視窓へ送り、watcherがこのcheckoutのownership marker・alias・profile・Tunnel IDを照合してruntimeを停止した後、停止確認とwatcher終了を待ちます。確認できない場合は `STOP_UNCONFIRMED` を返し、force-killしません。所有を照合できないreceiptやruntimeは変更しません。

監視窓で通常のCtrl+Cを押してPowerShellのfinally処理が動いた場合、所有確認後にそのruntimeを停止します。Windowsの強制的な窓閉じやprocess killではfinallyを保証できません。その場合、窓が消えてもruntime停止済みとはみなしません。次回の起動ensureまたは `--stop` が記録済みPIDとprocess start timeを照合し、同じruntimeの状態を再調停します。

Windows Scheduled Taskは任意の既存background方式として残します。この起動入口はTaskを登録・有効化・無効化しません。同じ専用Taskのwatcherが実行中と確認できた場合は新しい窓を作りません。Home MCP GatewayのTask・process・alias・profileには作用しません。
