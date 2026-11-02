# 固定source runtimeの候補exportと起動preflight

この契約は、通常の41開発起動を変えずに、85が既存の `GATEWAY_AOITALK_ROOT` / `GATEWAY_AOITALK_WORKING_DIRECTORY` で指定したrelease sourceをpreflightするための狭いopt-inです。新しいrelease service、DB移設、credential複製は導入しません。

通常の開発起動は `GATEWAY_AOITALK_ROOT` が未設定、またはentrypointと同じsourceを指す場合の既定OFF動作を使います。env/cwdを変更せず、既存のdotenv・import順・catalogを保ちます。release modeは別source rootとcanonical working directoryの両方が明示され、sourceがcanonical rootの `artifacts/releases/portal-runtime-<40桁SHA>` 配下にある場合だけ判定します。working directory、`data/`、`workspaces/`、`config/.env`が存在しなければfail closedし、作成・copy・fallbackしません。

release sourceの先頭import前に、次をread-onlyで照合します。

- source SHA、Git archive SHA-256、全export file inventory、source/backend tree digest
- entrypoint/manifest/fingerprint/lockfile/dependency inventoryとPython/Node version
- .nextのBUILD_ID・source fingerprint、候補内専用venvとnode_modules、PYTHONPATH・site-packagesのeditable path
- main entry、source/artifacts境界、canonical cwd・path contract・DB identity
- 前版のaccepted receipt、同じcanonical path contract、DB identity
- admin restart sourceにsupervisor固定root連携のversion tokenがあること

preflightの後にだけcanonical `.env`を指定読込し、passwordを含まないDB identity hashと、effective absolute data/workspace/storage/media/operations/voice/mobile/lock/cache/config pathsを再検証します。passwordや接続文字列をreceipt・エラーへ含めません。PostgreSQLへ接続せず、migrationも行いません。runtime helperはpathを作りません。dotenv読込後に、path selectorをabsolute値でプロセス内に固定します。ローカルruntime cacheの既定rootもcanonical rootに結びます。明示済みのLLM model rootは外部pathのままです。

`python main.py`でのrelease preflight、Next.js workspace path解決、docs attachment読み込み、mainの設定YAML/secret参照、heartbeat/skills/workflows、mobile identity、voice capture、GPU trial metadata、shared local runtime cache、app source preview、feedback/log、operations artifact、transfer lockは、上記path contractに従います。Personal/Enterprise feature profiles、既定のconfig生成コード、rig/source資産はrelease source側です。ユーザーが編集する `config/`、`.env`、workspace/dataと外部storeはcanonical側です。外部絶対pathは保持し、release rootへの付替えを行いません。

## source export

source exporterはfull commit SHAを必須にし、Gitのworking treeやcurrent branchをexport対象にしません。固定runtime source rootsのGit tree metadataだけを選び、canonical `data/`、`workspaces/`、`certs/`配下は問い合わせません。private/credential roots、`.env*`、certificate/key suffix、`artifacts/`、`.local`、cache/log/runtime output、`node_modules`、`venv`とfrontend user upload rootを含むblob pathを除外し、許可済みの個別blob pathだけを `git archive` へ渡します。frontend publicはdirect child namesからuploadsを外して列挙します。リンクや特殊tar entryを拒否し、source exportをrepository内 `.local/runtime-release-candidates/portal-runtime-<SHA>` にのみ作ります。85・AoiTalk Plugin source・operator documentsはruntime exportに含めません。Native Mobile sourceはclient contractのsourceとして含めますが、Mobile build/releaseは実施しません。toolはcandidate manifestを作りますが、accepted表示やsource activationは行いません。

```powershell
$Repo = '<aoitalk-repository-root>'
$Python = Join-Path $Repo 'venv\Scripts\python.exe'
$Sha = '<明示的にレビューしたfull private commit SHA>'
$Candidate = Join-Path $Repo ".local\runtime-release-candidates\portal-runtime-$Sha"
$Archive = "$Candidate.tar"
& $Python -I -B scripts/export_runtime_release.py export --repository-root $Repo --commit $Sha --destination $Candidate --archive $Archive
$ReproArchive = Join-Path $Repo ".local\runtime-source-repro-$Sha.tar"
& $Python -I -B scripts/export_runtime_release.py archive --repository-root $Repo --commit $Sha --archive $ReproArchive
if ((Get-FileHash $Archive -Algorithm SHA256).Hash -ne (Get-FileHash $ReproArchive -Algorithm SHA256).Hash) { throw 'fixed source export is not reproducible' }
```

isolated output build後、現行artifactから得たnon-secret build receipt、canonical path/database identity contract、以前にacceptedしたartifact receiptで `seal` します。exporter自体は `.accepted.json` を生成できません。

```powershell
& $Python -I -B scripts/export_runtime_release.py seal --candidate-root $Candidate --build-receipt $BuildReceipt --runtime-contract $RuntimeContract --previous-accepted $PreviousReceipt
```

accepted receiptは、sourceをレビューしているrelease ownerがmanifest SHAと同じaccepted commitを明示して作ります。source manifestやaccepted receiptはhost security sandboxではなく、意図的に両方書き換える管理者を防ぐ署名にもなりません。hash検証は記録後の偶発的変更、Dev import、混成buildとpath driftを検出するための契約です。deployment側はcandidate artifactを従来の `artifacts/releases` へ登録した後、同じsource SHAをservice rootとreceiptでpinします。

## まだ解除されていない統合gate

現行 `src/api/routes/system_routes.py` はexit code 42のときに `run.bat` watchdogへ戻ります。85のchild launcherも自分では次のapp childを再spawnしません。したがって本moduleの `restart_launch()` は固定rootの起動内容を表すfixture contractだけで、production routeにはまだ接続されていません。Sealerとruntime preflightは `aoitalk-pinned-release-supervisor-exit-42-v1` tokenをsource内で必須にするため、incident ownerがadmin restartを85所有のsame-root exit/restart contractへつなぐまで、candidateは起動拒否になります。

85 lifecycle ownerは既存のroot/cwd bindingをchildへ引き渡し、per-restart sourceを変更せず、promotionのみ別の受入窓として扱います。85 child environment allowlist、supervisor exit handling、startup healthとpromotion lockingの変更は85 ownerの範囲です。この実装は85 source/service registryを編集しません。admin restart route/UI等のownerも同じhandoffに従い、重複送信を追加しません。

今回のworkでは `npm ci` / production Next buildを実施しません。clean exportにはnode_modulesとvenvを含めず、現行releaseには受入済みdependency inventoryもありません。候補専用Node/Python dependencies、build、compiled branding、Next build fingerprint、runtime path/DB fixtureとaccepted previous receiptは、owner acceptance後のbuild stageで用意する必要があります。85の既存build wrapperはlive 3002所有時に拒否するので、未文書flagで呼ぶこともありません。

再起動時のhash checksとread-only modeは、同一ユーザーが受入manifestとcodeをともに故意に変更することを防ぎません。source/release directoryへ開発編集者が書かない運用と別worktreeを継続し、受入済みartifactだけをbindingしてください。
