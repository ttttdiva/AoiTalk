# Agent組織と委任workflow

Agentは既存 `Agent` と職務の固定版 `AgentRevision` を使い、直属上司は既存 `AgentOrganizationProfile.manager_agent_id` に保存する。Agent間の所属・指揮関係と、Subagent / Execution Profileから成る共有Execution Teamは別物である。同じExecution Teamを複数AgentやChatが参照できる。組織setupは既存AgentをIDまたはslugで再利用し、既存の一般Execution Team・Profile・Character・Project・Media Persona本文を変更しない。

正式名称は [feature_terminology.md](feature_terminology.md) を参照する。旧称「AI社員」に対応するidentityをAgentと呼ぶ。APIの `employee_*` / `employees`、source key `employee_delegation`、既存ファイル名は互換性のため維持する。会話CharacterとMedia Personaは別のidentityである。

## API契約

全ルートは認証済みhuman admin、`virtual_company` と `autonomous_agent_runtime` を要求する。DBのUserがactive/adminであることも確認する。委任の実行にはOrganizationの `allow_agent_runtime=true`、3つのAgentの現行雇用状態・Project grant・Space membership・任意Persona assignmentが必要になる。

全段階の既存read capability `workspace_read` を、受付時の3つのAgentの中央authority検証、Workの必須capability、adapterの必須capabilityに指定する。直属関係でもOrganization/company/Spaceの明示read denyとcapability ceilingを迂回しない。空のrevision/Persona capability ceilingは実行拒否となり、setupがこれを補充することはない。旧sourceで作られた必須capability空の未実行Workは `capability_not_declared` で止まり、過去manifestを改変せず新しい委任として明示的に出し直す。

| API | operationId | body / response |
|---|---|---|
| `GET /api/agent-organization` | `employee_get_organization` | `{success,nodes,edges}` |
| `POST /api/agent-organization/setup` | `employee_setup_organization` | `{employees:[...]}` → `{success,employees,blockers,ready}` |
| `POST /api/agent-delegations` | `employee_create_delegation` | 下記body → `{success,delegation}` |
| `GET /api/agent-delegations` | `employee_list_delegations` | 任意`agent_id`/`limit` → `{success,delegations}` |
| `GET /api/agent-delegations/{delegation_id}` | `employee_get_delegation` | `{success,delegation}` |
| `POST /api/agent-delegations/{delegation_id}/retry` | `employee_retry_delegation` | `{expected_work_item_id}` → `{success,delegation}` |
| `POST /api/agent-delegations/{delegation_id}/cancel` | `employee_cancel_delegation` | `{success,delegation}` |
| `GET /api/agent-delegations/{delegation_id}/artifacts/{artifact_id}` | `employee_get_delegation_artifact` | `{success,artifact}` |

setupの各Agentは `key,display_name,agent_id|slug` を持つ。任意の `manager_key,job_title,responsibility_summary,revision,activate` は明示された項目だけ適用する。`revision` は既存 `CreateRevisionRequest` と同じで、既存Execution Team/Profileを参照する。`activate=true` はdraftAgentだけをactiveにする。paused/retiredAgentは再開しない。manager_keyを省略した既存Agentの上司は保持する。循環した上司関係は拒否する。

委任bodyは次の形になる。

```json
{
  "delegator_agent_id": "<上司UUID>",
  "delegator_revision_id": "<上司revisionUUID>",
  "worker_agent_id": "<担当AgentUUID>",
  "worker_revision_id": "<担当revisionUUID>",
  "reviewer_agent_id": "<独立reviewAgentUUID>",
  "reviewer_revision_id": "<reviewer revisionUUID>",
  "project_id": "<既存Project UUID>",
  "title": "許可済み資料の整理",
  "instruction": "提供資料を整理し、根拠と不明点を返す。",
  "work_role": "writer",
  "idempotency_key": "<呼出元で固定した一意key>"
}
```

任意の `persona_id,parent_delegation_id,linked_work_item_id` を指定できる。work_roleは `researcher,writer,data_worker,general_worker,code_worker`。3つのAgentは互いに異なる必要がある。worker/reviewerは上司の直属、または上司が対象Projectの有効な既存 `manage_members=true` grantを持つ必要がある。後者はOrganization・職務・Spaceのpermission ceilingと交差する。共有専門Agentは直属上司を変更せず、複数ブランドの責任者からこの権限で委任を受けられる。setupからgrant・credential・policyの追加は行わない。

## 実行と再開

既存の共有 `AgentWorkCoordinator` にsource/adapter `employee_delegation` を登録する。通常のdispatcherまたは既存 `POST /api/agent-work/tick` が `work → review → return` を実行する。各段階は別Agentの固定revisionと、そのrevisionが参照する既存Execution Team/Profileの固定model routeを使用する。モデルは固定revisionの職務指示と委任内容、前段の採用済み成果物を受け取る。ChatとExecution Team定義を共有してもrunnerは同じではなく、この汎用委任ではSubagentを主にroute選択に使い、その指示・tools一式を継承しない。Persona本文が全Agentのpromptへ自動注入されるという契約もない。

一般のAgentの仕事はtool-freeの実model生成であり、提供された資料の整理・原稿・評価・分析を返す。新しいWeb調査、build、test、外部投稿を実施したとは扱わない。ツール無効化を保証できないCLI routeはblockする。個人経験を偽るアンケート回答、応募送信、公開、購入、account作成はこのadapterに含めない。

Web/App開発は `code_worker` と既存CW fulfillmentの `linked_work_item_id` を指定する。source/担当/revision/Project/Persona pinsを固定し、現行の受諾案件・要件source revisionを再確認する。開発実行自体は既存native adapterとscope issuerが担う。共有委任adapterはsource ZIP・validation reportの実体hash、work/run/revision/sourceの結合、required checksのpass、独立reviewのpassを確認してからAgentreviewへ進める。

`delegation.state` は永続Workの状態から導出する。`queued,awaiting_review,awaiting_return,returned,changes_requested,evidence_missing,cancelled`、段階別の `work:pending/work:retry_wait/review:blocked` 等を返す。子委任が残る場合は `awaiting_children`、子の取消・block・失敗・差戻しは `child_blocked` と `children:[{delegation_id,state}]` に返す。readinessには現行権限や子待ちのerror codeを返す。

providerの一時失敗は共有queueのbackoffに従って再試行する。blocked/failed/dead_letterの明示retryは同じWorkを再queueし、元のAgentRunのroute snapshotと試行履歴を保持する。reviewの差戻しはroundを増やし、旧review成果を次の仕事へ渡す。linked native開発の差戻し・再開はnative側で明示的に行い、新しい開発Workを別の委任に結び直す。uncertainや取消済み委任は自動再送しない。

instructionと成果本文は暗号化保存する。一覧には本文を含めない。artifact取得は採用済みWork/result/runの結合とhashを再確認する。返却artifactには前段の証拠pin、子の返却pin、入力context SHAを保持する。leaseを失った試行の候補は後続段階へ採用しない。

## 上司による限定計画と実委任

委任作成の任意 `planning` を指定すると、`plan → 子のwork/review/return → 親のwork/review/return` を同じ共有dispatcherが実行する。plan担当はrootでは上司、`can_delegate=true` の子ではその担当Agentになる。モデルは許可済みrosterのkey、scope key、input keyと小さくする方向の予算だけを選び、Agent・Project・grantを生成できない。子はAgent自身の固定Execution Team/Profileで動き、各成果を別Agentがreviewして上司へ返す。

```json
{
  "roster": [
    {"key":"writer","agent_id":"<既存AgentUUID>","agent_revision_id":"<固定revisionUUID>","allowed_work_roles":["writer"]},
    {"key":"researcher","agent_id":"<既存AgentUUID>","agent_revision_id":"<固定revisionUUID>","allowed_work_roles":["researcher"]},
    {"key":"reviewer","agent_id":"<既存AgentUUID>","agent_revision_id":"<固定revisionUUID>","allowed_work_roles":[],"can_review":true}
  ],
  "max_children":2,
  "max_total_children":16,
  "max_depth":3,
  "max_output_tokens_per_call":2048,
  "max_total_output_tokens":65536,
  "max_attempts_per_phase":2,
  "max_rework_rounds":1
}
```

このJSONを上記委任bodyの `planning` に入れる。rosterの `scope_keys` は既定 `["current"]`。SNS本部からブランドへ委任する場合は `brand_keys` とrosterの許可scope keyを指定し、owner-bound正式v6 portfolioの既存Project/Persona/revision/source SHAを固定する。dispatch時にも正本を再照合する。部署・ブランド担当の `can_delegate=true` は同じ許可scopeと狭めたrosterを引き継ぐ。直属関係または既存scoped management ACL、全受領AgentのProject/Persona readを子生成前と実行時に再確認する。計画配下へ通常APIで手動の子を追加して予算を迂回する操作は409で拒否する。自動計画のroleは `researcher,writer,data_worker,general_worker` に限定し、native Workを要する開発は従来の明示 `code_worker` 経路を使う。

子のplan/work/review/returnには、計画の割当文に加えて親の委任instruction(brief本文を含む)を `context.source_instructions.assignment` として渡す。孫には上位計画の委任instructionも `source:<委任ID>` で渡す。これらはhash pinを実行時に再照合した信頼できない入力データで、権限や指示の上書きにはならない。割当文がbriefを要約・転記していなくても担当は本文を使える。reviewは入力に無い事実の捏造と、入力にある情報を確認不能・要定義とした漏れの両方を差し戻す。保存artifactの入力pinはhashのみで本文を複製しない。

任意 `input_artifacts:{"reference":"<採用済みartifact UUID>"}` は同じownerの採用成果だけを参照できる。artifact ID・Work・AgentRun・SHAを保存し、元のProject/Persona権限を現在の受領Agentについて再確認する。過去の集約成果に含まれる子返却や入力元も保存時の証拠から辿り、生成時のprivacyは全入力元で最も厳しい設定を適用する。失効した資料を別案件へコピーして権限やprivacyを薄めない。

plan採用marker・SHA・子生成・累計子数はroot→orderの行ロック下で同一transactionに保存する。再discoverは採用済みの同じ計画を増殖させない。取消やlease失効の未採用出力は子を作れない。採用直前の権限失効等は `planning_blocked` と `planning.error_code` に永続化し、復旧後に採用plan Work IDを指定した明示retryで再検証する。

`max_total_output_tokens` は根の全Agent・plan/work/review/return・再作業・失敗attemptに跨る累計出力枠である。各AgentRunの呼出し前に最大出力分を永続予約し、失敗・取消・retryでも戻さない。対応providerは実送信の上限を設定し、1 attemptにつき生成wireを最大1回、SDK retryなしとする。native/plain helperとローカルOpenAI互換経路はprivacy確認前に追加bodyの標準引数との衝突を除外し、SDKが最後にmergeするJSONでも選択した上限・model・入力・tool条件を保持する。provider固有の追加オプションと元設定は保持する。上限を保証できないCLIは計画treeでは `delegation_bounded_route_required` で止まる。既存一般Profileや課金quotaは変更しない。review差戻しはrootと子の双方を `max_rework_rounds` まで自動で再作業させ、旧reviewと採用済み子返却を保持する。予算・attempt・rework上限に達した後のretryは409となる。

一覧・詳細の `planning` は `enabled,root_id,adopted_plan_artifact_id,reserved_output_tokens,max_total_output_tokens,created_children,error_code` を返し、phaseには `plan` が加わる。`awaiting_plan,planning_blocked` を含む状態と既存子状態を表示できる。plan artifactの `content.tasks` で採用した割当を確認する。

## 再実行可能setup

`scripts/setup_employee_organization.py` は既定でpreviewし、`--apply` でcanonical serviceを通して適用する。`--actor-user-id` は既存human adminのUUID。構造上の `ready=true` はAgent・revision・直属関係が揃ったことを表し、Project/Persona権限やprovider接続の準備完了ではない。

- `--manifest <JSON>`: APIと同じemployees manifestを適用する。
- `--crowdworks`: 既存CrowdWorks/Web/AppAgentをslugで再利用し、調査評価・ライター・正当な軽作業・品質reviewの欠けた4つのAgentを作る。`--worker-team-id/--worker-profile-id/--reviewer-team-id/--reviewer-profile-id` は明示選択した既存設定を指定する。
- `--sns`: 既存 `--director-agent-id` をSNS事業統括として再利用する。正式v6のowner-bound portfolioをcanonical serviceでreadbackし、8ブランド担当と共有の調査・制作・運用・分析・review職を作る。上記worker/reviewer設定と `--brand-team-id/--brand-profile-id` が必要。既存MediaDirectorのdraft状態やrevisionを勝手に変更せず、必要な明示manifestをblockerとして返す。`brand_bindings` には既存Project/Persona/revision/Program/Itemを返し、別ブランドProjectを作らない。

実DBへのsetup、Project/Persona grantの設定、provider接続、実投稿はこのsource開発の隔離受入と別工程である。受入は実HTTP・DB・共有lease dispatchを使い、provider transport/モデル出力だけをfixtureに置換する。native開発の受入は既存adapterの出力契約をfixtureで生成し、実artifact保存・読戻し・hashとrun pinsの検証までを通す。
