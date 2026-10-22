# 共有ローカルLLMのライフサイクル

## 通常の起動

AoiTalkの既存 `run.bat` を使用する。managedなllama.cpp / FreeToken / Strataのensure時に、必要なら独立coordinatorが起動する。AoiTalkのWebUIやPostgreSQLをcoordinator自身は起動・importしない。

- 制御API: `http://127.0.0.1:47641`
- 独立起動: 41の既存 `venv` のPythonで、41ルートから `python -m src.shared_local_runtime --serve`
- 設定snapshot: `cache/shared_local_runtime/`
- coordinatorログ: `logs/shared_local_runtime/coordinator.log`

OSへの自動起動登録、モデルdownload、新規credentialは作らない。外部deployment、`local-model`、`auto_start=false` は既存の外部所有を維持する。coordinator障害時にアプリ内で別workerを黙って起動しない。

設定は41の既存設定境界でrequest元の環境変数・設定・profileの優先順位を既存resolverにより解決し、許可した実効起動項目だけをsnapshotへ書き出す。coordinatorでは既知のruntime/deployment環境変数だけを除外し、最初のlauncherのcontext/model指定が後続clientを上書きしないようにする。PATH、GPU device指定、model cache、storage safety policyは維持する。HTTPにcommand、モデルpath、任意configは送信できない。モデルIDは一度41で正常に選択した登録profileを使用する。41未起動・DB停止中でも登録済みprofileは他アプリから起動できる。初回登録前のprofileは明示的な未登録エラーになる。

## Native clientのHTTP契約（version 1）

ブラウザから直接呼ばない。`Origin` または `Sec-Fetch-Site` がある要求と不正なHostは拒否する。HTTPはIPv4 loopbackのみにbindし、proxyとredirectを使わない。JSON bodyの上限は64KiB。上限超過は処理せず413を返す。Windowsの未読uploadによる接続resetを減らすため、Host/Origin等のheaderで拒否する場合も含め、拒否時には宣言されたContent-Lengthの範囲だけを最大65,537 bytesまで読み捨てる。長さ不明・不正・chunkedの場合や既に読み込んだbodyは再読せず、接続を閉じる。これより大きいuploadは最後まで読み込まず、途中で止まるuploadや少量ずつ続くuploadも、拒否処理全体で5秒の期限を設けて打ち切る。その場合はclientから接続エラーに見えることがあるが、backendへは一切渡さない。

- `GET /v1/capabilities`: service、protocol_version、broker_id、capabilities、ttl_seconds
- `GET /v1/status`: broker_id、runtimes、profiles。profileの登録とworkerの実際のrunning状態を区別する
- `POST /v1/acquire`: broker_id、client_id（UUID）、client_pid、request_id（UUID）、profile_id、kind（consumer / generation）
- `POST /v1/heartbeat`: broker_id、client_id、tickets（UUIDの配列）。10秒間隔を推奨
- `POST /v1/release`: broker_id、client_id、ticket。二重releaseは安全
- `POST /v1/cancel`: broker_id、client_id、client_pid、request_id。途中のacquireを取り消す。応答を失ったacquireにも同じIDで使用できる
- `POST /v1/stop`: broker_id、client_id、profile_id（未登録clientの初回停止時はclient_pidも必要）。consumer / generationが残っていれば409。強制停止APIは提供しない

acquire応答はticket、broker_id、profile_id、base_url、model（served alias）、runtime（`llama_cpp` / `freetoken` / `strata`）、launch_fingerprint、ttl_seconds、started。推論自体は返されたbase_urlへ送信する。推論開始前にgeneration leaseを取得し、streamやfetchが停止した後にreleaseする。アプリ/sessionのconsumerはidle中も保持できる。

要求の失敗は `{ "error": "..." }`。400は不正・未登録profile、409はlease/世代競合、503は起動等の失敗。同じrequest_idのacquire再送は同じticketを返す。release済みIDを新規取得に再利用しない。

### 終了・切替

アプリ終了は自分のticketをreleaseする。41のkill_servicesはcoordinatorや共有workerをtracked childに含めない。最後のconsumerとgenerationが解放されたら既存のexact-process停止処理でworkerを終了する。

45秒のheartbeat期限だけで生きているclientのgenerationを剥奪しない。停止したevent loopを誤ってcrashと扱わないため、PIDとprocess creation timeから同じprocess incarnationが終了したことを検証して初めてcrash cleanupする。プロセスが生きたままleaseを放置した場合は安全側に保持する。cancel失敗時のrequest IDは捨てず、接続復帰後に再照合する。

Strataの保存先変更は既存のprepare経路で完了させてから使用する。共有preflightではlive configのartifact pathを書き換えず、不一致なら失敗する。

### Agent委譲経路との関係（Strata client）

共有brokerが所有するのは、runtimeのlifecycle（worker・生成世代）とlease（acquire / heartbeat / release / cancel、ticket、`base_url`）の契約だけである。Agent委譲（正準 prefix `/api/agent-tasks`、`src/services/agent_task_invocation.py` の `AgentTaskInvocationService` / `AgentTaskExecutionAdapter`、adapter key `api_agent_task`）は、返された `base_url` / lease契約をそのまま使い、pin済みの推論clientは `OpenAICompatibleLocalClient`（native `AgentTurnRunner` ではない）である。

Agent identity・session・revision・tool policy・認可は、brokerではなく正準サービスと固定した `TurnContext` が所有する。brokerのlease / ticketはAgentの権限根拠にならず、brokerはsessionやtool状態を保持しない。委譲実行のturn loopは [local_agent_tasks.md](local_agent_tasks.md)、profile契約は [strata_runtime.md](strata_runtime.md) が扱い、このlease契約が複数会話 parking を有効化したり会話・tool状態を共有したりすることはない。

context、GPU設定、MTP、実行ファイル、GGUF shard等の解決済み起動identityが違えば、既存のleaseがある間は同じaliasでも共有しない。preflightは設定・artifact検証のみで、実際の切替時にleaseを再検査する。41自身のmain-route保持だけは切替で解放するが、同じ41内のsessionやagentのticketも他アプリと同様に保護する。

Windowsでは、backendを読み込む前にcoordinator自身を匿名のkill-on-close Job Objectへ所属させ、その後に起動したworkerだけを継承させる。brokerを強制終了してもOSがその所有process treeを終了する。設定・検証・所属に失敗した場合はworkerを起動せず、診断を出して失敗する。親のJob Objectが残る場合は実際のlimit flagsを照会する。KILL_ON_JOB_CLOSEが有効なら独立性を保証できないため拒否し、flagsを読めない場合も失敗する。Modern Standby等の非kill-on-close system jobは、既存制限を変更せずnested jobへの所属を試み、OSが拒否すれば失敗する。41からの自動起動はbreakawayを要求し、OSが拒否した場合も無断で弱い方式へfallbackしない。Windows venvでは、CPython自身のmultiprocessingと同じ方式で `sys._base_executable` を起動し、子プロセスだけに `__PYVENV_LAUNCHER__=sys.executable` を渡してvenvを維持する。Node等から従来のvenv Python経由で起動した場合も、固定moduleのbootstrapが一度だけこの起動方式へ移る。子プロセスの所有guardは省略しない。Linuxには同等の強制終了時保証をまだ提供せず、未知listenerへのfail-closedのみとする。

coordinatorの再起動後は古いticketを拒否する。以前のworkerや同じaliasの外部listenerを自動adopt・killしない。ポートが残る場合は未追跡processとして明確に失敗するため、元processを確認して正規の停止経路から整理する。稼働中ticketがある間のcoordinatorの通常終了要求は拒否する。

## 検証範囲

`tests/test_shared_local_runtime.py` は軽量backendによる実HTTP、独立した2つのclient process、worker PIDの一意性、片方の終了後の生存、最終解放、異なるfingerprint、二重release、誤ったowner/世代、冷起動中のclose/crash、応答消失、Host/Origin/body境界、外部listener非adopt、DB非importを検証する。

GPU推論、実機Windowsの起動・console、実際の41→86モデル切替は別途実機で確認する。embedding workerの共有はこのLLM契約では実装しない。

### Windowsの通常起動経路での確認

2026-10-12に、Gatewayと通常consoleの両方で `limit_flags=12288 (0x3000)` を確認した。Gateway固有の制限という当初の推測は誤りだった。CPython 3.12.10のvenv redirector自身が `KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK` のJobを作るため、venv Pythonをもう一度起動するだけではbrokerの独立性を保証できない。修正ではCPython標準のmultiprocessingと同じbase-interpreter起動方式を使い、余分なredirectorを作らない。既存Jobの制限やsecurity設定は変更せず、OSが起動・所属を拒否した場合はそのまま失敗する。

普段AoiTalkを起動するWindows consoleで、41のルートを作業ディレクトリとして次を確認する。管理者権限、OS設定の変更、GPUモデルのdownloadは不要。

1. 次の読み取り専用診断を実行する。

   ```bat
   venv\Scripts\python.exe -c "import json; from src.shared_local_runtime.windows_job import current_job_limits; print(json.dumps(current_job_limits()))"
   ```

   この診断はvenv redirectorのJobも表示するため、`0x3000` だけでは外部の制限とは判断できない。出力を記録し、次のテストで実際のbroker起動方式を確認する。照会のアクセス拒否は回避しない。

2. synthetic Python workerと独立sentinelだけを使用する受入テストを実行する。

   ```bat
   venv\Scripts\python.exe -m pytest tests\test_shared_runtime_windows_job.py::test_forced_broker_exit_kills_owned_worker_but_not_external_sentinel --basetemp=.local\pytest-shared-job-normal -q
   ```

   合格条件は、venvの `sys.prefix` と `sys.executable` を維持しながら実際のPython PIDを直接追跡でき、broker強制終了後に所有workerが終了し、無関係なsentinelが生存すること。Jobの照会・設定・所属で失敗した場合は未合格とし、skipや弱い所有方式へのfallbackで代用しない。

3. 受入テストが通った後に既存 `run.bat` で41を起動し、登録済みmanaged profileを使用する。86で同じprofileを取得して推論を開始し、`GET http://127.0.0.1:47641/v1/status` のworker PIDが共通であることを確認する。
4. 86のconsumerまたはgenerationを保持した状態で41を通常終了し、worker PIDと86の推論が維持されることを確認する。最後の86 consumerとgenerationを解放した後に、同じworkerの終了を確認する。手動停止時にcoordinatorへ接続できなければ、停止成功とは報告しない。

手順1〜2は通常起動環境の所有保証、手順3〜4は実際のアプリ連携の確認であり、Gatewayで他のテストが通っただけでは代替しない。

既存のdirect-backendテストは `tests/conftest.py` で `_shared_runtime_direct_backend=True` を明示注入する。実行時にpytestを検出して挙動を変えない。

Windows job仕様: [Nested Jobs](https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs)、[QueryInformationJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject)。NULL handleによる照会対象はimmediate jobであり、既存のjob制限を書き換えたりアクセス拒否を回避したりしない。

CPython 3.12.10の根拠: [venv redirectorのJob生成](https://github.com/python/cpython/blob/v3.12.10/PC/launcher.c#L740-L770)、[標準multiprocessingのvenv起動処理](https://github.com/python/cpython/blob/v3.12.10/Lib/multiprocessing/popen_spawn_win32.py#L55-L60)。launcher用環境変数をユーザー設定へ保存する必要はない。
