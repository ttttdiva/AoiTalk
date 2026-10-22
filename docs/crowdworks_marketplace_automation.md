# CrowdWorks案件自動化

CrowdWorks案件は既存の Operations / Agent / AgentWork を使って処理する。Web制作・アプリ開発案件は専門Agent（Web制作担当 / アプリ開発担当）が応募前評価と受注後の実開発を担う。独立schedulerやretry queueは持たない。

## フロー

1. ExternalConnection.provider_key=crowdworks と、ログイン済みEdgeを持つ選択PC Bridgeを用意する。
2. Agentの AgentActionPolicy に engagement.submit_application を設定する。
3. CrowdWorksWorkSource がpolicyの scan_interval_minutes ごとに既存 AgentWorkCoordinator へworkをmaterializeする。
4. 検索ページをEdgeで開き、1ページにつき1回のDOM観測から案件リンク・カード文脈・報酬をまとめて抽出する。
5. EngagementOpportunity をsource snapshot/hash付きで保存し、複数案件を10件単位でAgentの `planner` ルート（下記「モデルルーティング」）へ送り採算評価する。
6. OpportunityEvaluation を保存し、期待利益・利益/時間・confidence・worker kindはOpportunity metadataに保存する。
7. 閾値を満たす上位案件から ApplicationDraft を作り、engagement.submit_application の ExternalAction を提案する。標準bootstrapは human_approval で作成し、Operationsの案件詳細で期待利益・利益/時間・評価要約・リスクを見せ、「この案件を進める」を人間の最終ゲートにする。
8. 人間が進行を承認したActionだけ、既存の ExternalActionWorkSource -> ExternalActionExecutionAdapter durable laneを通る。provider呼出し前にAttemptを永続化し、結果不明時は再送せず uncertain にする。
9. 応募成功後は案件ページを読み取り、契約/仮払い/作業開始状態を確認する。受注済みになったら worker kind に応じた受注後laneへ進む。
10. 文章・調査・データ入力・一般案件は、CrowdWorks担当がレビュー用Markdownを生成し ArtifactVersion として保存する。
11. Web制作・アプリ開発案件は、専門Agentが顧客案件ごとの独立ワークスペースで実際に開発し、AoiTalkが独立に検証して成果物を登録する（後述）。
12. Opportunityを ready_for_review / READY_FOR_REVIEW にして停止する。最終納品は自動化しない。

受注後の成果物作成は、CrowdWorksページで仮払い完了を確認できた場合だけ
開始する。「契約成立」「契約中」だけでは `escrow_pending` で停止し、
「作業中」「業務開始」も同じページで仮払い済みの証拠を確認できない限り
開始しない。判定不能・仮払い待ちは人間確認へ回す。

## 専門Agent

ワンクリックbootstrap（`POST /api/marketplace/crowdworks/bootstrap`）は既存 AgentIdentityService で不足する社員を作成する。独自のエージェント基盤は持たない。既存社員の最新revision、停止状態、組織・会社上限、Policyの承認方式・制限、現在の接続先は変更しない。過去のbootstrap revisionへの巻戻しや、権限の自動追加はしない。`status` / `bootstrap` の `runtime_readiness` と `specialist_readiness` は、現在のrevisionの権限交差とcanonical routeを検査し、設定済みでも実行不能なら `started=false` と具体的な `blocked_reason` を返す。

| Agent | slug | Team / Execution Profile | 主な担当 |
| --- | --- | --- | --- |
| CrowdWorks担当 | `crowdworks-marketplace` | `employee` / `manual` | 案件探索、採算評価、応募、契約監視、文章等の受注後作業 |
| Web制作担当 | `crowdworks-web-developer` | `app_development` / `manual` | LP・コーポレートサイト・HTML/CSS/JS・React/Next.js・WordPress・既存サイト修正・レスポンシブ・フォーム |
| アプリ開発担当 | `crowdworks-app-developer` | `app_development` / `manual` | Webアプリ・業務ツール・API・バックエンド・DB・デスクトップ/モバイル・自動化ツール |

新規専門担当の capability ceiling は既存Team catalogの `workspace_read / workspace_write / command_execute`、許可Subagentはcode_explorer / architecture_planner / code_implementer / code_reviewer。応募提案権限やemployee_operator、repo_mapは付与しない。既存Organization上限が不足する場合は明示した人間の権限変更が必要で、bootstrapは上限を拡張しない。WebとAppにまたがる案件は `app_development` を主担当にする。

モデル・実行方法は Agent画面の役割（Role）編集で新しい revision の Team / Execution Profile を選ぶと変わる。応募前評価は現在の専門Agentのrevisionを使う。受注後AgentWorkは開始時にpinしたAgentRevisionとExecution Profile route snapshotだけを使い、途中のSettings変更でprovider/modelが変わらない。

- Execution Profile の provider が `codex-cli` → Agent Harness の `CodexExecRunner`（model/effort を profile から設定）
- `claude-cli` → `ClaudeCodeRunner`
- `openai_compatible_local` / `ollama` → 既存 `LocalAgentTaskRunner` の scoped workspace/tool loop（選択した local model/effort を使い、Codex/Claude CLIへ変換しない）
- その他のproviderは開発runner未対応として停止する。`manual`から継承したproviderでも、既定Codex/Claudeへ暗黙に置き換えない

## Worker kind

`writing / research / data_entry / web_development / app_development / general`。以前の `code` は `app_development` として読み替える（`normalize_worker_kind`）。移行前に作られた `crowdworks_fulfillment_code` の AgentWork item も開発laneで処理される。DB schema変更は無い（worker kind は Opportunity metadata）。

## 応募前の専門評価

採算評価で上位候補になった Web制作・アプリ開発案件は、応募文を作る前に担当予定の専門Agentの観点で実現性を評価する（`crowdworks_specialist_assessment.py`）。

- 評価には AoiTalkホストの実際の開発環境probe（node/npm/python/git/php/docker/Xcode/Android SDK/ヘッドレスブラウザ/コードエージェントCLI等の有無）を事実として渡し、iOS実機・顧客サーバー・WordPress管理画面・API仕様・ストアアカウント等を「ある」と仮定させない。
- 結果は既存 OpportunityEvaluation の新しい version（fit=feasible/conditional/infeasible）として保存し、Opportunity metadata `specialist_assessment` にUI用投影を置く。保存項目: 作るもの、推奨担当、想定工数、技術スタック候補、必要な顧客提供物、未確定要件、開発/検証環境、実現可能性と理由、リスク、人間作業、人間作業時間、想定コスト、期待利益、利益/時、確度。
- 評価が無い・担当Agentが不在・実現困難・環境を用意できない・専門見積りで採算閾値未達の案件は応募を提案しない（screening_status: `specialist_review_pending` / `specialist_unavailable` / `specialist_declined` / `below_*`）。
- 応募文生成には専門評価を渡し、未確定要件と顧客提供物を応募文で確認させる。

## 受注後の実開発（Web制作 / アプリ開発）

Execution Profileの運用は Head（planner / reviewer）と Worker
（実装・workspace操作を担う `code_worker`）を分ける。Web制作・アプリ開発は、同じ開始時pinの
AgentRevision / Agent Team / Execution Profileを保ったまま
`planner → worker → 独立したdeterministic検証 → reviewer`を実行する。
reviewerのfix指示または必須検証の失敗に対するfixは最大2回までで、
workerが変更後に失敗した場合、または最大2回のfix後も未解決なら
`uncertain` / `blocked` として人間の
確認・エスカレーションへ回す。検証とreviewerが通った場合だけ
`READY_FOR_REVIEW`へ進み、人間の最終レビュー後の納品・deploy・pushは自動化しない。
Generalのbalanced/local presetはこの開発loopや重いreviewを強制しない。

1. 受注を検知すると、案件ごとに Project（`project_metadata.kind=client_engagement`）を作り、担当専門Agentへ AgentProjectGrant を付与する。
2. `CrowdWorksFulfillmentWorkSource` は開発案件を専門Agentに割り当てた AgentWork item（adapter `crowdworks_development_<kind>`、capabilities `workspace_read/workspace_write/command_execute`、`max_attempts=1`）として既存 coordinator へmaterializeする。
3. `ClientDevelopmentService` は、開始時にpinしたAgentRevision / Team / Execution Profileのrole routeからprovider/modelを解決し、provider/modelのハードコードや暗黙fallbackを行わない。既存Agent Harness runner（CLI）または `LocalAgentTaskRunner` / `run_openai_compatible_turn_loop`（local/OSS）で、同じscoped workspaceに `planner`（read-only）→ `code_worker`（mutate）→ deterministic検証 → `reviewer`（read-only）を実行し、reviewer fix後のWorker再実装は同じpinで最大2回まで行う。案件本文は「UNTRUSTED CLIENT LISTING」として区切り、指示としては扱わない。
4. コードエージェントの自己申告とは別に、AoiTalkがプロジェクト種別を判定して独立に検証する。
   - Node: `packageManager` / lockfileを判定し、npm・pnpm・Yarnの対応するinstall → build → lint → typecheck → test（scriptがあるもの）。複数lockfileで明示指定が無い場合は曖昧として停止する。
   - Python: 検証用venv → install → compileall → pytest（tests があれば）
   - PHP/WordPress: `php -l`（WordPress実環境での動作は未検証として残す）
   - Go: `go build ./...` → `go test ./...`
   - Rust: `cargo check` → `cargo test`（実行バイナリがあれば `cargo build`）
   - .NET: `dotnet restore` → `dotnet build` → test projectがあれば `dotnet test`
   - Maven / Gradle: wrapperを優先し、`test` / `build`（AndroidはSDKがある場合だけdebug build）
   - Flutter: `flutter pub get` → `flutter analyze` → testがあれば `flutter test`（Android targetはSDKがある場合だけ）
   - 静的サイト: ビルド工程なし
   - Web制作 / ローカルWebアプリ: 静的出力または `start/preview/dev` を127.0.0.1へ限定して起動し、既存QA laneのscoped PlaywrightでPC幅(1366px)・モバイル幅(390px)表示、スクリーンショット、横スクロール、内部リンク、フォーム構造、consoleエラーを確認する。明確な品質不良は必須検証失敗としてREADY_FOR_REVIEWにしない。外部リンクは訪問せず、フォームは送信しない。
   - README の有無と認証情報混入（秘密鍵・APIキー形式）を必ず確認する。
5. 成果物は既存 `OperationsService.create_artifact`（ArtifactVersion、provenanceにopportunity/agent/agent_run/work_item/workspaceを記録）で登録する: ソースZIP（`.git`/`node_modules`/`.aoitalk`/`.env` 等を除外）、ビルド成果物ZIP、README、検証レポート(md/json)、ビルド・テストログ、プレビュー画像。
6. すべての必須検証がPASSし、ソース成果物とREADMEが登録できた場合だけ READY_FOR_REVIEW にする。LLMがコードを書いたという申告だけでは READY_FOR_REVIEW にしない。

プロジェクト形式を判定できない app_development、または独立build/compile/testを一つも実行できない案件は `verification_failed` / `blocked` とし、READMEとsecret scanだけでREADY_FOR_REVIEWにはしない。顧客提供物・API仕様・既存テーマ・DB schemaなど応募前評価で必須とされた入力は `development_blockers` / `client_inputs_required` / `unresolved_requirements` として未解消ならコードエージェントを開始せず `awaiting_client_input` で停止する。`human_tasks`のレビュー・納品・本番公開・顧客確認だけでは開発を止めない。人間がOperations画面で必要情報を確認し、専用の要件確認操作が新しいrequirements revisionを発行した後にだけ新しいAgentWorkとして開発を開始する。

未検証の項目（ブラウザ無し、アプリ実行確認、WordPress実環境、iOS/Android実機など）は `development.unverified` に、失敗した任意検証やエージェント申告の問題は `development.known_issues` に残す。

## 顧客案件ワークスペース

- 既定の保存先は既存 workspace root（`AOITALK_WORKSPACES_DIR`、既定 `./workspaces`）配下の `_client_engagements/opportunity_<uuid>/`。`AOITALK_CLIENT_WORKSPACES_DIR` または config `client_development.workspace_root` で別ドライブ等へ移せる。
- 顧客開発案件ごとに `project_metadata.kind=client_engagement` / `project_metadata.opportunity_id` を持つ専用Projectを1つ作る。探索元・Connection Projectは `origin_project_id` として保持するだけで流用しない。同じOpportunityの再処理では同じ専用Projectを再利用し、担当専門AgentのAgentProjectGrantもそのProjectへ付与する。
- rootが AoiTalk リポジトリそのもの・その親・git管理下のパスを指す場合は起動しない（リポジトリ内に置く場合は git-ignored であることを確認する）。
- `source/` は Agent Harness の `WorkspaceManager(repo_root=None)` で作り、独自の `git init` をしたうえで `git rev-parse --show-toplevel` が自分自身であることを検証する。AoiTalk の worktree は作らない。リポジトリhookも実行しない。
- 作業ディレクトリ・HOME・資格情報を除いた環境変数はOS sandboxではない。現行の自動顧客開発laneではlocal workerのshellを無効化し、native mutate runnerと顧客管理のbuild/install/test/previewコマンドをホストへfallbackしない。安全な隔離実行が接続されていない場合は `verification_required`（AgentWorkはblocked）として停止する。ファイル生成・静的検査だけを実施した結果を検証済みとは扱わず、必須検証が残ればREADY_FOR_REVIEWにしない。一般の手動開発機能のコマンド設定は変更しない。
- Windowsのnpm/CLI `.cmd`・`.bat` は既存AgentRunnerのプロセスツリー管理下で起動し、複数行の顧客プロンプトは一時入力ファイルをstdinとして渡す。プロンプトをcmd引数へ埋め込んで改行・外部データを壊さない。
- AgentWork materialize時にpinされた `agent_revision_id` を受注後実行でも厳密に使用する。最新revisionへ暗黙に差し替えない。
- ワークスペースはレビューのため自動削除しない。`reports/` に検証レポート・ログ・スクリーンショットを残す。

## READY_FOR_REVIEW と納品

Operations → Opportunities の案件詳細の「開発結果・最終レビュー」で、作ったもの、担当Agent、実行runner/model、独立検証結果、プレビュー、成果物（`GET /api/operations/artifacts/{id}/content` でダウンロード）、未検証項目、既知の問題、顧客へ渡す予定のファイルを確認できる。

CrowdWorks納品ボタン、顧客本番サーバーへのdeploy、App Store / Google Play 公開、顧客GitHubへのpushは行わない。将来追加する場合も、READY_FOR_REVIEW 後の別の人間承認付き ExternalAction として実装する。

## 不確実状態

- コードエージェントがワークスペースを変更した後に失敗・切断・タイムアウトした場合、または検証コマンドがタイムアウトした場合は `uncertain` として停止し、自動再実行しない（開発laneは `max_attempts=1`、uncertain / running の案件は再materializeしない）。
- コードエージェントが何も変更せずに失敗した場合は `blocked`（Execution Profile を変更した新 revision が作られると新しいwork itemとして再実行対象になる）。
- 必須検証の失敗は `verification_failed`（blocked）で、READY_FOR_REVIEW にしない。
- 隔離実行を必要とする未実施のbuild/test/previewは `verification_required`（blocked、`crowdworks_development_verification_required`）。実行失敗と未実行を区別し、顧客scriptや公開コマンドをホストで自動実行しない。

## Policy constraints

engagement.submit_application のconstraintsは search_urls, scan_interval_minutes, max_pages_per_scan, max_opportunities_per_scan, max_applications_per_scan, min_expected_profit, min_profit_per_hour, currency を受ける。

llm_provider / llm_model / llm_effort は非推奨の互換フィールドで、既存Policy revisionを有効なまま残すためだけに受け付ける。これらは `manual` を含む全社員routeで無視し、provenanceに `legacy_policy_override_ignored` を残す。provider/model/effortは常にexact AgentRevisionのProfileが正本である。ワンクリックbootstrapはこれらを設定しない。

## モデルルーティング

モデルの正本は Agentの exact AgentRevision → Agent Team → Execution Profile → Subagent route で、共通resolverは `src/services/agent_model_routing.py`。CrowdWorks専用のモデル設定やBase Modelへの暗黙fallbackは持たない。

| フェーズ | work role | 優先Subagent（Teamのメンバーかつrevisionで許可されたもの） |
| --- | --- | --- |
| 採算評価・要件分析 | planner | architecture_planner → general_researcher → general_worker → employee_operator |
| 応募文作成 | planner | 同上 |
| 応募送信（ブラウザ操作） | operator | employee_operator → general_worker |
| 成果物: writing | writer | general_worker → employee_operator |
| 成果物: research | researcher | general_researcher → code_explorer → general_worker → employee_operator |
| 成果物: data_entry | data_worker | general_worker → workspace_operator → employee_operator |
| 成果物: code | code_worker | code_implementer → workspace_operator → general_worker → employee_operator |
| 成果物: general | general_worker | general_worker → employee_operator |
| AIレビュー | reviewer | code_reviewer → general_researcher → architecture_planner → employee_operator |

- Scan / fulfillmentのAgentWorkは WorkItem と AgentRun がpinしたrevisionを読み、最新revisionは参照しない。
- Team・Profile・Subagentが無効または該当なしなら `agent_route_*` のcodeで blocked にし、LLMを呼ばない。
- 実際に使った route（agent / revision / team / profile / acting_subagent / provider / model / effort / route_source）を Opportunity metadata、Artifact provenance、AgentRun の `provider` / `model` / `run_metadata.model_routes` に残す。credential、base URL、path、推論内容は保存しない。
- AIレビューのLLM呼出しが失敗した場合、成果物は保存し `ai_review_status=unavailable` を記録して人間の最終確認へ渡す。

実応募を人間承認にするかbounded autoにするかは既存 AgentActionPolicyRevision.authorization_mode が正本である。別の自動送信フラグは持たない。ワンクリックbootstrapの既定値は human_approval で、案件取得・評価・Draft生成までは自動、実応募だけ人間の最終決定後に進む。

## 生成予算とローカルruntime

Operationsの文章生成は共有の `ephemeral_llm.timeout_seconds` を使う。未指定は600秒、上限1800秒。モデルの準備、最初の通信、reasoning-only応答の再試行まで同じdeadlineを共有し、SDKには残時間を渡す。model/provider/effortは既存routeから変えない。別の呼出元が明示した短いtimeoutは維持する。

期限切れ・取消しは不正JSONと区別し、JSON repairが新しい予算で再要求しない。期限後の応答を成功として受理しない。同期HTTPはスレッドを強制停止できないため、実行中のrequestがある間はcleanupだけでgeneration leaseを解除せず、呼出終了時点の `worker_pending` を診断に残す。これはproviderの停止完了を意味しない。request数、応答model、thinking設定などは観測できた項目だけを安全なprovenanceへ記録する。

Strataの `ready`（モデルロード完了）と `serving`（期待したservice/status/modelのwrapperが要求を受け付ける状態）を区別する。正常なlazy-unload後はwrapperを維持し、次の要求でautoloadする。異なるmodel、異常な応答、他の外部port占有を単なるHTTP200で通さない。明示的なforce restartと起動待ち上限は別の制御として維持する。

## Browser / credential境界

CrowdWorksはAPI credentialをvaultへ複製しない。policy readinessは、対象ユーザーの選択PC・Edge接続・browser capabilityを検証し、そのdevice bindingを credential_state_hash 相当の実行authority evidenceへ固定する。選択PCが変わった場合は既存のaction integrity checkで実行を止める。

ブラウザ戦略は通常のStrategy LLMがbounded planを作り、各DOM stepの候補選択はJevを優先する（低confidence・未設定時だけ同じ操作用LLMへbounded fallback）。クリック・入力・遷移の実行と再観測は BrowserAgentService / RemoteControlBridge gateway を通り、PC Bridge -> Edge拡張 -> 選択PCのログイン済みEdgeへ送る。案件一覧・契約監視はdeterministicな `crowdworks_inspect` を最優先し、main documentを1回だけ機械抽出してカードごとのLLM操作を行わない。Edge Sleeping Tabは拡張が対象window/tabを自動でactiveにしてからDOM操作する。

## Idempotency / retry

- Opportunity: CrowdWorks connection + canonical job URLで重複取り込みを防ぐ。
- Application action: connection + opportunity IDでdedupeする。
- AgentWork: policy revision + Agent revision + scan time bucketのhashをsource revisionに使う。同一bucket内の社員revision変更も新しいpinを持つworkになる。
- 同一source/intent・revision・scope・capabilityが同じ理由で権限拒否中なら、共通coordinatorが現在の権限を再評価して既存blocked証跡を再利用する。intervalごとにblocked履歴を増殖させない。権限回復、新revision、scope/capability変更は新規workを妨げず、過去のblocked行・AgentRunは書き換えない。
- browser接続前の失敗は送信されていないためretry可能。
- submit dispatch後のtimeout/接続断/確認不能は uncertain とし、同じ応募を自動再送しない。
- retry時にEvaluationだけ保存済みでも、Draft未作成なら再度draft生成対象になる。

## 現在の境界

上記の開発検証一覧は要求される完了条件であり、ホスト実行の許可を意味しない。現行の自動顧客開発laneは隔離executor接続までfile-tool-onlyとし、必須の実行系検証が残る案件はverification_requiredで停止する。

検索一覧DOMから取得できる情報を優先するため、案件本文・添付・クライアント詳細が一覧に無い場合は missing_requirements として評価へ残り得る。受注後は文章・調査・データ入力・一般案件はレビュー用Markdown、Web制作・アプリ開発案件は独立ワークスペースでの実開発と独立検証に分岐する。いずれも外部サイトへの最終納品操作は実施しない。
