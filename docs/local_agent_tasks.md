# Local Agent Tasks（Home MCP Gateway 委譲）

ChatGPT から目的・workspace・権限・完了条件を1回渡し、AoiTalk の正準 Agent が既存の共通 tool loop で複数ステップの作業を進める。呼び出し側は正準 `/api/agent-tasks` の `GET /{task_id}`（状態）/ `tail` / `cancel` と、中断作業の `resume`（照合）で受付後の状態を扱う。正準経路に `/status` や一覧のendpointはない。受付成功は作業完了ではない。

Qwen の対象 route は provider=`openai_compatible_local`、catalog model / served alias=`qwen3.8-flash-next-iq3_xxs`、runtime=`strata`。ChatGPT 自体のモデル差し替えではなく、通常 Chat と同じ Agent identity、revision、権限、モデル route を使う委譲経路である。最初の疎通は read-only ファイル作業で行う。構造化編集と、追加の権限・既存sandboxの準備を満たした場合の隔離コマンドに対応する。無制限のhost shell・実デスクトップ・外部補助 LLM は含めない。

主な実装:
- 正準の受付・参照（HTTP prefix `/api/agent-tasks`）: `src/api/agent_task_routes.py`
- 正準 Agent 受付サービス + Work adapter（adapter key `api_agent_task`）: `src/services/agent_task_invocation.py`
  （`AgentTaskInvocationService` と `AgentTaskExecutionAdapter` は同一ファイルに定義する。別ファイルの `agent_task_execution_adapter.py` はない）
- 旧 `/api/local-agent` の互換 machinery（正準経路とは別）: `src/api/local_agent_task_routes.py`、`src/services/local_agent_task_adapter.py`（adapter key `local_agent_task`）
- 旧managerの永続 receipt・runner / tool guard、および正準経路が共用する制限・検証・redactor: `src/services/local_agent_tasks.py`
- run 専用 MCP: `src/services/scoped_local_mcp.py`、`src/tools/external/mcp_plugin.py`
- Gateway 側: `80_HomeMCPGateway` の既存 `aoitalk_local_task_start/status/tail/cancel/list`。各ツールが41のどのendpointを呼ぶかは80の実装で確認する（41の正準 router に `/status` や一覧endpointはない）

## 共通workspace基盤の統合状況（2026-10-13時点の履歴）

以下は2026-10-13（JST）時点の履歴記録で、現在の指示ではない。当時の公開HEAD監査では、必要な `agent_dialogue_workspace` / `tool_mutation_resource_guard` とtool-policyのworkspace scope APIが別作業の未commit変更にのみ存在し、旧 `/api/local-agent` 互換machineryは基盤の統合まで新規受付を `409 canonical_runtime_unavailable` で拒否する実装だった（このコードは現在も旧adapterにのみ存在し、正準 `/api/agent-tasks` のsourceには現れない）。当時の実行・sandboxテストは基盤を含む開発checkoutでの結果である。

その後、正準経路は `src/api/agent_task_routes.py` / `src/services/agent_task_invocation.py` に統合され、main（e01c774時点）で正準の実タスクが実行されている。ただしこれは当該経路の稼働確認であり、ハーネス全体の完了、全テストのPASS、モデル出力の正しさを示すものではない。タスクの `completed` も出力が正しいことの証明ではなく、有効な成功toolレシートなどの完了証拠を満たした状態を表す。共有lease・Agent権限・Run所有・path guardは実行時に毎回再検証する。実Strata/Qwen受入は、対象環境で別に確認する。

## Architecture

```text
ChatGPT → Home MCP Gateway の既存5ツール
  → 41 /api/agent-tasks（`src/api/agent_task_routes.py`、既存cookie認証。Router は `require_auth_dependency` 配下）
      … 旧 `/api/local-agent` は互換専用の別 machinery であり、正準経路とは混同しない
  → AgentTaskInvocationService（受付・冪等性・Agent/revision固定・Work/Run対応付け）
  → 共通AgentWorkCoordinator / AgentWorkItem + AgentRun + 正準ConversationSession
  → AgentTaskExecutionAdapter（adapter key `api_agent_task`、同じく `agent_task_invocation.py`）
  → 共通tool loop（worker thread）
      ├─ 固定済みAgent/revision/route/TurnContextと実行checkpoint
      ├─ 固定 pinned client `OpenAICompatibleLocalClient`（`build_agent_chat_turn_client`）+ 既存共有local runtime
      │    → Strata（推論のみ。native AgentTurnRunner ではない）
      ├─ 共通tool loop / 共通tool router・policy
      ├─ 既存os_operations + task scope guard + 共有workspace lease
      ├─ 許可時のみ発行済みHarnessExecutionScope
      │    Windows: `native_windows`（`filesystem_isolation=false`。WSL/bubblewrap ではない）
      │    非Windows: WSL/bubblewrap
      └─ 必要なら既存MCPPlugin → 80のrun専用read-only stdio子
```

正準 Agent の依頼は旧 `LocalAgentTask` manager の独立 daemon queue に投入しない。旧 `/api/local-agent` の互換 machinery は別に存在し、正準経路と同一視しない。共通 coordinator が claim、lease、取消、settlement を所有する。`task_id`、`agent_run_id`、`work_item_id` は別のIDとして対応付け、正準の投影はこの3つとAgent/revision IDだけを公開する（内部の session ID は投影しない）。依頼ごとの session を作り、既存の別 Chat へ履歴・権限を混ぜない。

既存の明示的な Codex CLI / app-profile 経路は独自の承認・認証契約を維持する互換対象で、この Qwen 委譲の代替には使わない。正準受付が利用できない場合も、その経路や別モデルへ暗黙に切り替えない。Strata 内蔵の別 MCP tool loop も使用しない。

## API

すべて `require_auth_dependency`（既存cookie認証）配下で、Router 全体に一律の `require_admin` は掛けない。認証済みowner本人に限定し、選択 Agent の ACL / capability は共通サービスで照合する。旧 `/api/local-agent/*` は別machinery（Router は `require_admin` 配下）で、正準受付・参照は下表の `/api/agent-tasks` を使う。両者を同一経路として記述しない。正準受付には通常の本人 Web session で検証された安定した user UUID が必要で、owner / 承認主体をリクエスト本文から受け取らない。Gateway 接続者の管理者ロールは Agent 選択 ACL の照合に使い、選択 Agent の権限を管理者へ昇格させない。Enterprise では無効（`409`）。

| Method / Path | 用途 |
| --- | --- |
| `POST /api/agent-tasks` | タスク受付（`Idempotency-Key` ヘッダー必須）。新規は `202 {task, created}`、同一依頼の再取得は `200` |
| `GET /api/agent-tasks/{task_id}` | 状態・進捗・正準ID・route・結果 |
| `GET /api/agent-tasks/{task_id}/tail?limit=&max_bytes=` | サイズ制限付きイベント末尾 |
| `POST /api/agent-tasks/{task_id}/cancel` | 取消要求。実処理の終了とは分けて表示 |
| `POST /api/agent-tasks/{task_id}/resume` | 再起動で結果不明になった操作の照合後の継続（下記「中断作業の照合と resume」）。盲目的な再試行ではない |

上記が `src/api/agent_task_routes.py` の全routeで、`/status` と一覧endpointは存在しない。

受付・状態取得・tail ではモデルをロードしない。旧 `/api/local-agent/status` 診断の `model` / `default_route` は互換情報で、選択 Agent の実行モデルの根拠にはしない。タスクの `requested_route` / `resolved_route` と pinned revision を確認する。

### リクエスト

正準 `POST /api/agent-tasks` の本文（`AgentTaskCreateRequest`、未知のフィールドは `extra=forbid` で422）。`Idempotency-Key` は必須ヘッダーで、本文では受け付けない。

```json
{
  "agent_id": "<検証済みの正準Agent UUID>",
  "goal": "READMEとpyproject.tomlを読み、Gatewayの起動手順と依存条件を要約する",
  "workspace": "D:\\Dev\\80_HomeMCPGateway",
  "constraints": ["ファイルを変更しない", "secretを出力しない"],
  "completion_criteria": ["参照したファイル名を示す", "確認できない事項を区別する"],
  "mode": "read_only",
  "allow_commands": false,
  "provider": "openai_compatible_local",
  "model": "qwen3.8-flash-next-iq3_xxs"
}
```

| Field | 正準委譲での意味 |
| --- | --- |
| `agent_id` | 必須（UUID）。正準routerは省略を受け付けない。`local_agent_tasks.default_agent_id` は旧 `/api/local-agent` 側の設定で、正準routerのsourceでは参照しない |
| `goal` / `workspace` | 必須。`goal` は最大8000文字。workspaceは既存の絶対ディレクトリ。通常は `local_agent_tasks.allowed_roots` 内。`command_workspace_unconfined` で解除されたadminと、許可されたAgent呼出は、そのallowlistの外でも使える |
| `provider` / `model` / `effort` | 任意の期待値。Agentの固定routeと照合し、不一致は `agent_route_mismatch`。上書きやfallback指定ではない |
| `required_capabilities` | 任意（最大20）。要求するcapability。Agentの現在のgrantで検証し、未知のcapabilityは拒否 |
| `constraints` / `completion_criteria` | 各20項目以内。指示・完了確認用であり、権限を与える自然文ではない |
| `requires_command_verification` | 任意のboolean。最後のファイルtool編集より後の成功したコマンド証跡を完了条件にする。省略時は目的・完了条件の文面から推定する（実行を求める動詞＋テスト/build/lint、または「テストが通る」等の成功要件だけが対象。「verification」等の語だけ、否定・静的レビュー・読み戻しでは推定しない）。検証が必要（明示または推定）で `allow_commands=false` の場合は矛盾として受付時に `422`。コマンド側のファイル変更は追跡しない |
| `mode` | `read_only` / `mutate`。省略時はAgentのcapability（`workspace_write` / `command_execute`）から決まる。`mutate` の構造化編集には `workspace_read` / `workspace_write` 権限と共有workspace leaseが必要 |
| `allow_commands` | `true` は `mode=mutate`、`command_execute` 権限、コマンドruntimeの準備を満たす場合だけ許可（read_onlyでは403）。通常は既存harness rootの厳密な子が必要で、解除された主体だけはその外の既存ディレクトリも使える |
| `allow_git_commit` / `allow_git_push` | 省略・nullは `false` として扱い、そのgit操作（commit / push）を許可しない（リクエスト自体は拒否されない）。許可するには `true` に加えて `mode=mutate` と `allow_commands=true` が必要（commitは明示pathのadd＋task範囲の通常commit、pushは通常のnon-force branch push） |

次は旧 `/api/local-agent` の `local_agent_task_create` 側の項目で、正準の本文には存在しない（送ると422）。`allow_adb`、`expected_branch`、`max_tool_rounds`、`timeout_sec`、`command_timeout_sec`、`codex_connection`、本文の `idempotency_key`。正準経路のタスク期限は固定3600秒（`_TASK_TIMEOUT_SECONDS`）で、リクエストから変更できない。

`goal` / `constraints` / `completion_criteria` の自然文は、加工・破棄せずAgentへの指示（プロンプト）としてそのまま渡す。書込み・コマンドの権限は `mode`・`allow_commands`・Agent権限・workspace scope などの構造化された値だけで決まる。

- ファイルを名指しする保護指定（例: `src_a.py は変更しないこと`）は、そのファイルへの書込みだけを拒否する。他のファイルは `mode=mutate` のまま編集できる。
- ファイルを名指ししない制約（例: `設定ファイルは変更しないこと`、`TTL/認証/DB禁止`）は、権限を広げる根拠にも狭める根拠にもしない。守るべき指示としてモデルへ渡すだけである。
- `mode=mutate` のタスクは、依頼文にファイル名がなくても書込みを必須として扱う。成功した書込みの記録（receipt）が残るまで、作業の予告だけでは完了にしない。

任意の `user_id` / owner / project / session を本文へ追加することもできない。

### `required_outputs`（正準 `/api/agent-tasks` の必須成果物）

`POST /api/agent-tasks` は任意項目 `required_outputs`（workspace相対のファイル名、最大20件、`mode=mutate` 必須）を受け付ける。これだけが完了判定に加わる構造化契約で、`goal` や最終応答の文言からは成果物も「後で書く」という約束も推論しない。

- **検証される内容**: 各ファイルは、リンク/reparseを含まない非空の通常ファイルで、モデルの最初のターンの前に取得した内容digest（ストリームsha256）と**異なり**、かつ書込みの来歴を持つこと。来歴は対象パスへのファイルツールの成功receipt（`file_tool`）、または許可された `allow_commands` のコマンドが正常終了していること（`command_correlated`）。後者は相関であり、コマンドが何を書いたかの証明ではない（コマンドreceiptにはファイルシステム差分がない）。出力ごとの来歴は内部のAgentWork結果（`result_summary_json.required_output_provenance`）にだけ記録され、公開の `result` は従来どおり `response` のみで、この値を返さない。
- **満たされないもの**: 変更のない既存ファイル、touchのみ、同一内容での書き直し、読み取りのみ、バックグラウンド起動のみ、拒否・失敗・不確実なコマンド。`task_required_output_missing` で失敗し、`failure.missing_required_outputs` に未達のファイルが並ぶ。
- **baseline**: admissionではなく初回実行時（workspace占有中、モデルとツールの前）に1回だけ取得し、Runイベント `api_agent_task.required_output_baseline` に永続化する。再起動後も同じ値を再利用する。書込み・読み戻しに失敗したら実行を止める（`audit_write_failed`）。
- **名前の制約**: Windowsでも安全な相対名のみ。絶対パス、ドライブ、UNC、`..`、予約名（`CON` 等）、ADS（`:`）、末尾のドット/空白、リンク/reparseは `422`。
- **補正ターン**: 欠落が `required_outputs` で明示されている場合に限り、最大2回、同じattemptのライブ履歴（元のツール結果は無加工）に補正ノートだけを送る。履歴を継続できなければ盲目的に再生成せず未完了で止める。補正で欠落が減らず、ファイルも変更されなければ（読み取りは進捗ではない）止まる。未解決の失敗・拒否・policy denial・コマンド検証の欠落は補正の対象外で、最終ゲートの優先順位も変わらない。
- **範囲**: 契約があるのは正準 `/api/agent-tasks` のみ。Gateway経由のClaude Code job等、別のライフサイクルを持つ経路には効かない。成果物をコマンドの副作用だけで作る場合も、上記の相関までしか確認できない。

実行ごとの能力ガイダンスは、発行済みscopeの `write_roots` / `delete_roots` / `scratch_roots` / `command_roots` から生成され、そのattemptの最初のユーザー入力の末尾に1回だけ付く（system promptやツールschemaは変えない）。書込み権は削除権を含まないこと、使い捨てのファイルは発行済みscratchに置くこと、コマンドで `>` / `>>` のリダイレクトを使わないことを述べるが、情報提供であり、全てのpolicy・権限・失敗の記録は従来どおり。編集対象が固定scope（`edit_targets` または文章由来の限定）で絞られている場合は、書込み・削除の案内を対象ファイルに限り、scratch/probeの案内は出さない。

### `edit_targets`（正準 `/api/agent-tasks` の編集対象scope）

`POST /api/agent-tasks` の任意項目 `edit_targets`（workspace相対の**ファイル**のみ、最大20件、重複不可、`required_outputs` と同じWindows安全名・リンク拒否、`mode=mutate` 必須）は、`mode=mutate` タスクで file tool が書き換えてよいファイルを明示する。`required_outputs` は完了契約であって権限ではなく、書込み権限を与えない。

- **完全一致**: 明示した対象は、その相対パスのファイル**ただ1つ**を指す。`ACCEPTANCE.md` は `sub/ACCEPTANCE.md` に一致しない（admissionの `required_outputs` 検査も、実行時のfile tool判定も同じ）。パス比較は大文字小文字を区別せず（実行時比較と同じ。Windows以外でも `A.md` と `a.md` は同一扱いで、より厳しい側）。文章から推論されたscope（`edit_targets` 省略時）の裸のファイル名は、従来どおりworkspace内の同名ファイルに一致する（互換動作）。
- **未指定（null）と空配列は別**: 省略は従来どおり `goal` / `constraints` / `completion_criteria` の文章から導いたscope。`[]` は「file toolで書けるファイルなし」で、無制限ではない。mutateタスクの完了には変更系receiptが必要なので、`[]` は `allow_commands` を伴う場合（コマンドだけで作業するタスク）にだけ受け付け、コマンドもない場合は `422`（何も書かないなら `mode=read_only`）。
- **admissionで確定し永続化**: 呼出し側の元の文章と `edit_targets` から `FixedEditScope`（editable / protected / 正規化済み / sha256 fingerprint）を計算し、AgentWork metadata `edit_scope` に保存する。実行時のpolicy（`check_tool_call_allowed` の構造化mutate判定）と完了判定（`filesystem_outcome_unmet`）は、束縛されたこの固定値だけを使い、そのターンの入力文は読み直さない。実行ごとの能力ガイダンス・補正ノート・再起動の継続ノートは権限に影響しない。同じRunの補正ターンと再起動後も同じfingerprintで動く。
- **明示指定が置き換えるのは推論された編集可能リストだけ**: 文章に書かれた読み取り専用ファイル（protected）は残り、`edit_targets` / `required_outputs` がそれに当たれば `422`。「他のファイルは読取のみ」という限定（carve-out）は、**編集してよいファイルが文章中に名指しされている場合に限り**その名指しを上限として扱い、`edit_targets` がその外なら黙って広げず `422`。carve-outが編集ファイルを名指ししていない場合は上限にならず、`edit_targets` がそのまま上限になる。文章中の「X だけ」は推論された編集可能リストとして `edit_targets` に置き換えられる（本件の「…節だけを確認し」を編集限定と誤読する問題の回避）。両者が食い違うことを検出して `422` にはしない。
- **`422`（Work/Runの作成前）**: `required_outputs` が最終scopeの外／protectedに当たる、`edit_targets` が不正・重複・21件以上、`read_only` で非空、`[]` でコマンドなし、全体読み取り専用の文章。`edit_targets` を省略した場合も、文章から導いたscope外の `required_outputs` は `422`。
- **永続metadataの検証**: 保存された `edit_scope` は型・version（真偽値は不可）・リストの正規形（整列・重複なし）・パスの安全性・不変条件（明示scopeは必ず限定、等）まで検査し、fingerprintが自己整合していても壊れた値は実行前に `execution_binding_mismatch` で失敗する（fail closed）。このfieldがない旧taskは、保存済みの**元の依頼**（Goal / Constraints / Completion criteria）から**実行のたびに決定的に再導出**する。Run objectiveは不変で、フレームワークが後から加えた文は読まない。導出値の書き戻しはしない。
- **ガイダンス**: scopeが対象ファイルに限定されている場合、許可された対象ファイルを明示し、scratch/probeファイルの作成を案内しない（scratchは自動で追加しない）。

**制約（限界）**: これはfile toolの対象パスに対するpolicyである。OSのsandboxではなく、許可された `allow_commands` のコマンドが何を書くかは検査しない（コマンドは対象外のファイルも書き得る。コマンドreceiptにファイルシステム差分はない）。コマンドでの書込みを禁じたい場合は `allow_commands` を与えない。policy denialは従来どおり失敗receiptとして残り、タスクは `blocked` で終わる。フレームワークは再試行・消去・権限拡大をしない。

### 冪等性と受付切断

- 同じ認証済みowner・同じキー・同じ正規化requestは同じreceiptへ戻す。Agent設定を後から変更しても、そのreceiptの選択済みAgent/revisionは変えない。
- Agent、model、workspace、mode、権限などrequest内容が異なる同一キーは `idempotency_conflict`。同じ作業の再送で新しいキーを発行しない。キーは `Idempotency-Key` ヘッダーで渡し、不正な値は `422 invalid_idempotency_key`。
- 受付中断・応答消失・キー索引だけの欠落でも、作成済みreceiptを再取得し、二重のWork/Runを盲目的に作らない。
- 受付途中の永続化が完了していないtaskは、状態取得で `interrupted` として投影され、安全な再実行を仮定しない。旧 `/api/local-agent` の `admission_interrupted` は旧adapter側のコードで、正準sourceには現れない。タスク作成の冪等性は外部副作用のexactly-once保証ではない。

## 状態と結果

`GET /api/agent-tasks/{task_id}` の `task`（`AgentTaskProjection`）が返す項目は次の11個だけで、`create` / `cancel` / `resume` も同じ形である。

| 項目 | 意味 |
| --- | --- |
| `task_id` | 32桁hex。受付の識別子 |
| `status` | `queued` / `running` / `waiting` / `waiting_approval` / `interrupted` / `completed` / `failed` / `cancelled` |
| `work_item_id` / `agent_run_id` / `agent_id` / `agent_revision_id` | 共通Work・Run・pinned Agent/revisionへの対応（`agent_run_id` は未作成なら `null`） |
| `cancellation_requested` | 取消要求済みで、まだ `running` の間だけ `true` |
| `reconciliation_required` | 結果不明の操作（`tool` / `operation_id` / `path`）。再起動照合待ちのときだけ。無ければ `null` |
| `result` | `completed` のときだけ `{response}`（secret除去済みの最終回答のみ）。内部traceや任意のRun結果は出さない |
| `failure` | `failed` のときだけ。`reason`、未解決tool、成功tool、残した最終回答（完了結果ではない） |
| `tool_failures` | `completed` / `failed` のときだけ。回復済み・未解決・診断（コマンド非0終了）の失敗tool |

`tail` は `task_id` / `status` / `events`（Work event列）/ `truncated` / `event_count` を返す。

次の項目は旧 `/api/local-agent` のレガシー専用で、正準の応答には存在しない。`session_id`、`requested_route` / `resolved_route`、`execution_state`（`loading_model` / `cancelling` を含む）、`current_step` / `latest_activity`、`counters`、`worker_alive` / `stalled`、`result.git`、`result.changed_files`（`changed_files_complete` / `changed_files_source`）、`result.checks` / `criteria_verification` / `verified_tool_results`、`unfinished_background_jobs`。これらをcanonical投影の項目として扱わず、また旧facadeの項目が存在することをバックエンドの実状態の証拠にもしない。実行状態の確認は上表の項目、`tail` のWork event、共通Work/AgentRunの状態で行う。

| `status` | 共通Workの状態・意味 |
| --- | --- |
| `queued` | `pending`（受付が `ready` でない `pending` は `interrupted`） |
| `running` | `claimed` / `running` |
| `waiting` | `retry_wait`。待機理由はeventで確認する |
| `waiting_approval` | `awaiting_approval`。承認待ちをfailedやフリーズへ潰さない。再起動照合待ちなら `reconciliation_required` が付く |
| `interrupted` | `uncertain`。再起動等で実行継続を安全に証明できない |
| `completed` | `succeeded` |
| `failed` | `failed` / `dead_letter` / `blocked` |
| `cancelled` | `cancelled`。取消処理中は `running` のまま `cancellation_requested=true` になる場合がある |

完了判定は、成功したガード付きtoolレシートがあること、未解決のtool失敗がないこと、`mutate` ならファイル変更toolの成功レシートがあること、`requires_command_verification` なら最後の変更点より後に成功したコマンド証跡があること、で行う（`_completion_evidence_satisfied`）。これは成功toolの証跡の確認であり、モデルの最終回答や任意の完了条件全体の第三者検証ではない。`completed` を出力が全て正しい証拠として扱わない。変更点はファイルtoolの編集に加え、実行前後のworkspace内容digest（観測範囲の全通常ファイルの内容ハッシュ。範囲はworkspaceがGitのtop levelなら「tracked + 未ignoreのuntracked」と明示 `required_outputs`（ignore済みでも必ず観測）の和集合、それ以外は `.git` を除くworkspace全体の有界走査）が「変更なし」と完全に証明できなかった全コマンドレシート（変更あり・不明・上限超過・読取不能・シンボリックリンク/junction/特殊ファイルあり・background起動・証跡なし。成否は問わない）である。検証は最後の変更点より後の、変更なしと証明された成功コマンド（またはlaunch〜完了readの間にworkspaceが不変だったbackground完了read）でなければならず、未知のコマンド自身は検証として採用されない。コマンド文字列・終了コード・stdoutから安全性は推定せず、コマンドの自動再実行もしない。限界: 1つのコマンドが書込みと検証を兼ねる場合は変更点として扱い、検証にならない（別コマンドでの再検証が必要）。初回のテスト実行がキャッシュ等を生成すると「変更あり」となり再実行が要る。ディレクトリ走査・Git列挙・ファイル読取はいずれも20000エントリ・64MiB・5秒で有界で、超過・読取不能・観測不能な必須sourceがあれば不明となり、fail closedで再検証を要求する。Git workspaceではリポジトリ自身がignoreする依存/cache（`node_modules`、`venv`、`.local` 等）は範囲外で、そこへの書込みは検証を失効させない（ディレクトリ名やcaller指定の除外リストは持たない）。範囲外への書込みは観測されず、OS全体の隔離は主張しない。「変更なし」は、実行前後ともcompleteな観測状態と同一の正しい形式のdigestを伴う場合だけ信頼し、ラベルだけの証跡・partial・digest欠落/不一致は不明として扱う。前後の観測は原子的でなく、最終成果物は検証時のdigestに束縛されていないため、観測窓と競合する書込みや範囲外の書込みは検知できず、最終成果物の鮮度や遅延書込みの防止は保証しない。workspace外・`.git` 配下への書込みは観測しない。コマンド由来の変更一覧は保証しない。Git状態（clean / 変更なし）も正準の応答には含まれない。

### 中断作業の照合と resume

再起動で実行が中断したタスクは、まず `restart_reconciliation_required` として `waiting_approval` に留まる場合がある。このとき status の `reconciliation_required` に、結果が不明な操作（`tool` / `operation_id` / `path`）が並ぶ。`POST /api/agent-tasks/{task_id}/resume` は次の照合・継続だけを行う。

- Work が `awaiting_approval` かつ `restart_reconciliation_required` の場合だけ受け付ける。それ以外は `409 not_awaiting_reconciliation`。
- 呼出しごとにAgent権限・workspace・Runのdurable receipt・tool_start記録を再評価する。読取は再実行しても安全として再試行、編集/作成は記録済みfile digestで成否が確定すれば確定させる。確定できない操作は `unknown` のまま残る。
- `unknown` の操作は、ownerが workspace を確認したうえで `confirmed_operation_ids` にその `operation_id` を明示した場合だけ継続できる。継続してもモデルへは結果不明として伝え、成功として記録しない。
- 取消要求済み・拒否されたtool呼出し・失われた権限は resume で回避できない。再queueされた実行は source refresh・権限・task fence を改めて通る。継続できない場合は `blocked` / `cancelled` / `uncertain` に確定する。
- 同じ操作を無確認で再送する、新しいWork/Runを自動で作る、といった盲目的な再実行ではない。

### 期限・取消・進捗停止

- 共通Work/AgentRunの現在状態・owner・lease・期限とAgent権限を実行checkpointで再確認する。tool実行とモデル呼出しを跨いで取消を無視しない。
- task deadline（正準は固定3600秒）とtool呼出し期限を分ける。`max_tool_rounds` はレガシー専用のリクエスト項目で、正準リクエストには存在しない。MCP初期化待ちと通常toolの30秒期限も別扱い。
- 同一呼出しの反復・共通tool loopの重複抑止・連続失敗上限を適用する。scope追加toolにもcounter、redaction、失敗追跡を適用する。
- 取消APIは共通Workのleaseを無効化して取消要求を記録する。workerと生成が止まる前に成功・完全終了を宣言せず、`running`（`cancellation_requested=true`）のまま当該runのcleanupを待つ。
- Gateway/Tunnelの再接続は取消ではない。副作用の結果が不明なtimeoutを理由に同じ操作を自動再送しない。

## Security boundary

- **Agent authority**: 本人・選択ACL・固定revision・capability ceiling・現在のgrantを共通サービスで照合する。モデル名やGateway管理者接続だけでは権限を与えない。
- **workspace**: 既存 `AgentRunScope`、`AgentDialogueWorkspaceManager` の共有lease、共通tool policyと構造化tool guardを使用。workspace外、symlink/junction、資格情報ファイル等を拒否する。構造化編集だけのscopeは `native_allowed=false`。`mode=mutate` だけでコマンド権限を付与しない。共有leaseは所有workerとMCP contextの終了後に解放する。
- **コマンド**: 明示許可と現在のAgent権限を検証してサーバーが発行した有限 `HarnessExecutionScope` に限り、backendが提供する隔離・COW・secret除外・network NONE（WSL/bubblewrapではファイル隔離を含む）を使用する。Windowsの正準経路は `native_windows`（`filesystem_isolation=false`）で、コマンドはAoiTalkを動かすOSユーザー権限で動き、cwdの外も読み書きできる。strictなcwd・ファイルtool scopeはOSのファイルシステム隔離ではない。scopeの自己申告やworkspaceの親を管理rootとして採用しない。通常host shellへfallbackせず、既存の危険操作・上書きリダイレクト等のtool policyも維持する。ADB / Git commit / `expected_branch` は引き続き未対応で、要求時に拒否する。
- **secret**: 不要な資格情報を子へ渡さず、結果・イベント・要約に既存redactorを適用する。API key、cookie、passwordをMCP引数やモデルcontextへ置かない。
- **承認**: `AUTONOMOUS_WORK` という生成ポリシー名を権限や承認の代替にしない。共通Agent authorityと実際のtool policyを通し、待機が必要なら `waiting_approval` として扱う。

## 設定

設定正本はDBのアプリ設定。既存の設定保存経路で更新する。default定義や `config.yaml` の編集だけでは稼働設定は変わらない。

| Key | 用途 |
| --- | --- |
| `local_agent_tasks.enabled` | 既定true。Enterpriseでは無効 |
| `local_agent_tasks.default_agent_id` | 旧 `/api/local-agent` の省略時委譲先。正準 `/api/agent-tasks` は `agent_id` が必須で参照しない |
| `local_agent_tasks.allowed_roots` | 許可workspace。未設定時の既存既定はcheckoutの親だが、運用では必要なrootに絞る |
| `local_agent_tasks.command_workspace_unconfined` | 作業場所 allowlist と harness root の厳密子孫制約を外すadmin専用の例外。未設定・空は従来どおり両方を維持。`user_ids` は有効なadminのユーザーIDだけ。`user_ids` に入れたadminは、そのadminが受け付ける全タスク（どのAgentを呼んでも）が解除対象になる。`agent_calls` はそのadminが呼ぶAgentとrevisionの組で、revision固定のため、Agentを編集して新しいrevisionになると解除対象から外れる（再度登録が必要）。ユーザー名では指定できない。解除後もリンク/reparseとfilesystem rootは拒否する。ファイルツールの読取/書込根はworkspace自身に留まるが、コマンドの到達範囲はbackendに依存する（下記「条件付き隔離コマンド」の2.） |
| `local_agent_tasks.storage_dir` | 既定 `data/local_agent_tasks`。旧managerのreceipt/event保存（レガシー） |
| `local_agent_tasks.default_*` / `max_*` / `repeat_limit` / `failure_streak_limit` / `tool_result_chars` | 旧managerの `LocalAgentTaskLimits` による制限（レガシー。正準リクエストの `max_tool_rounds` 等は存在しない） |
| `local_agent_tasks.mcp` | 下記のrun専用read-only MCP設定。既定無効 |

共通 `AgentWorkCoordinator` が有効で、正準Work adapter（key `api_agent_task`、`AgentTaskExecutionAdapter`）が登録済みであることが必要。旧 `/api/local-agent/status` 診断の `common_runtime_connected` は旧machinery（adapter key `local_agent_task`）の接続状態であり、正準経路の可用性とは別に確認する。正準実行のclaim/lease/同時実行制御は共通coordinatorが所有し、既存managerのdaemon queue件数や `max_concurrency` を正準Work全体の状態とみなさない。

`local_agent_tasks.model` やAoiTalk全体の既定モデルを変更してAgent選択の代わりにしない。既存のAgent管理境界で、対象Agentのoperator routeとrevisionを設定する。他Chat/Agentの選択モデルは変更しない。

## 条件付き隔離コマンド

初回は `read_only` / `allow_commands=false` で確認する。コマンドが必要な依頼だけ `mode=mutate` / `allow_commands=true` を明示し、次の条件をすべて満たす。

1. 認証済みowner、正準Agent/revision、現在のRun/Work/leaseが一致し、`workspace_read`、`workspace_write`、`command_execute` が有効。
2. workspaceは `local_agent_tasks.allowed_roots` 内で、既存の信頼済みharness workspace rootの厳密な子ディレクトリ。root自身やリンク/reparse経由は不可。adminが `local_agent_tasks.command_workspace_unconfined` で明示した有効なadminユーザー、またはそのユーザーが呼ぶ登録済みAgent revisionだけは、allowlist と harness root の外でも、存在する絶対パスのディレクトリを workspace に使える。`read_only` / ファイルのみ / `allow_commands` で同じ。filesystem root、存在しないパス、ファイル、相対パス、リンク/reparseは解除後も拒否。read_file/edit_file等のファイルツールの read/write 根と、コマンドの開始ディレクトリ（cwd）は workspace（と既存scratch）に留め、親ディスクを許可根に足さない。ただしこれで保証されるのはファイルツールの到達範囲だけである。コマンドの実行場所は経路で異なる。(a) 互換の `/api/local-agent`（`src/services/local_agent_task_adapter.py`）は `backend_kind` を `wsl_bwrap` 既定のまま使い、Windowsでも WSL/bubblewrap 内で実行し、この経路は admin 解除を参照しない（常に allowlist と harness 厳密子孫を要求）。(b) ネイティブAPI Agent経路（`/api/agent-tasks`、`src/services/agent_task_invocation.py`）は `os.name == "nt"` のとき `native_windows` を選び、WSL readiness を要求せず、admin 解除はこの経路で有効になる。`native_windows` は `filesystem_isolation: false`（`src/agent_harness/runner.py`）で、コマンドはAoiTalkを動かすOSユーザーの権限で動き、cwdの外（親ディレクトリ、他ドライブ、ユーザープロファイル等）も読み書きできる。ファイルシステム隔離があるのはWSL/bubblewrap backendだけ。`user_ids` に入れたadminは当該adminの全タスクが解除される。`agent_calls` はAgentとrevisionの組で照合するため、Agentを編集してrevisionが変わると解除が外れる。それ以外のユーザーは allowlist と harness 厳密子孫の両方を維持する。capability、所有者分離、コマンドsandbox（backendが提供する範囲）は残る。
3. WSL/bubblewrap backend を使う経路（上記 (a)、および非Windowsの (b)）では、既存のファイルscope対応backendが利用可能で、有限のreadiness smokeに合格する。Windowsの (b) は `native_windows` のため免除される。受付のWork実行gateを解放する前と、providerを作る前の両方で再確認する。

管理rootは既存の `harness_execution.workspace_root` 等の実効設定から解決する。requestの親をrootに推測したり、setup/install・WSL distribution切替・永続設定変更を自動で行ったりしない。未準備なら `409 command_runtime_unavailable` として推論・command実行前に停止する。権限やroot不一致は `permission_denied`。

コマンドは41の共通tool routerから既存のscoped job機構へ渡す。80のrun専用MCPは引き続きread-onlyの4ツールであり、コマンド許可でそのprofileを拡張しない。隔離scopeのprocess数、出力、disk、実行時間には有限上限を設け、ネットワークはNONE、変更は既存COW反映・監査契約に従う。反映件数0も正常な読取/出力コマンドでは有効な結果で、反映件数だけを成功・失敗判定にしない。

既存policyが拒否するコマンドは、sandboxが準備済みでも拒否する。拒否を弱めて再送せず、構造化toolで必要なファイルを用意する等、既存の許可範囲で進める。自分の背景jobだけを追跡・取消し、終端結果の観測なしに完了扱いしない。

## Run専用 Home MCP 接続

DBの `local_agent_tasks.mcp` の例。実在する80の既存Python・script・cwdを確認する。

```json
{
  "enabled": true,
  "command": "D:\\Dev\\80_HomeMCPGateway\\.venv\\Scripts\\python.exe",
  "script": "D:\\Dev\\80_HomeMCPGateway\\scripts\\gateway_stdio.py",
  "cwd": "D:\\Dev\\80_HomeMCPGateway"
}
```

`mcp_enabled` とAgentの `workspace_read` 権限も必要。起動設定は管理設定だけから取得し、モデルでは変更できない。41の `venv` と80の `.venv` は別々に使用し、80のPythonモジュールを41へimportしない。

ChatGPT向けGatewayは既存の `HOME_MCP_AOITALK_ENABLED` で委譲入口を有効化する。更新後は通常のMCP接続でツールメタデータを再取得する。認証・接続先・公開base pathは既存設定から解決し、パスを推測して追加しない。

ローカル子にだけ次の固定scopeを渡す。通常Gatewayの環境は変更しない。

- `HOME_MCP_PROFILE=local_agent_read_only`
- `HOME_MCP_WORKSPACE=<検証済みworkspace>`
- `HOME_MCP_AGENT_RUN_ID=<正準AgentRun ID>`
- `HOME_MCP_OWNER_ID=<認証済みowner>`
- `HOME_MCP_AOITALK_ENABLED=0`

OS起動用の限定環境とscopeだけを継承し、API key、cookie、DB接続文字列、41の `PYTHONPATH` / `VIRTUAL_ENV` を渡さない。

### 公開する4ツールと制限

- `list_directory`: 最大500件
- `path_info`: scope内のファイル/ディレクトリ情報
- `read_text`: UTF-8、最大24,000文字
- `search_text`: 最大100結果、500列挙項目、10秒。省略は返却値で確認する

41は送信直前にrun/owner/Agent/revision/session、現在の権限、workspace、引数を検証する。80も固定scopeと同じallowlistで実行する。workspace外、symlink/junction/reparse、hardlink、資格情報ファイルを拒否し、直接 `tools/call` / 汎用MCP helperでも禁止操作を実行できない。

任意shell、PowerShell/Python、デスクトップ、ブラウザ、HTTP、Jev/外部補助LLM、AoiTalk再委譲、clip ingestは公開しない。ローカルstdioという理由で一般MCPの外部送信検査を免除するものではない。新しいホスト操作には別途権限境界が必要。

元のnested/nullable/array/$defs/ローカル$ref schema、`structuredContent`、`isError`を保持する。外部schema参照をHTTP取得せず、解決不能なら拒否する。`ok=false` / `success=false`も共通tool runtimeへ操作失敗として返す。画像・音声・resourceはテキスト専用経路では未対応とし、base64やSDK objectを文字列へ潰さない。Qwenの画像・音声対応をこの接続試験から推定しない。

接続とsessionの開始・終了は同じowner loop/taskで行い、workerやnative adapterの呼出しを元loopへ戻す。終了・取消・起動失敗・timeoutで当該runの `AsyncResourceScope` を閉じる。同名serverへの再接続中も旧呼出しを新sessionへ移さず、旧listの失敗で新schemaを消さない。他runや無関係プロセスを停止しない。

### OFFへ戻す

DBの `local_agent_tasks.mcp.enabled=false` で新規run専用接続を止める。稼働runも次回toolの設定・権限確認で拒否し、終了時に自分の子を閉じる。Gatewayから新規委譲も止める場合に限り、そのGatewayの既存連携フラグを無効化する。モデル・Agentの既定設定や他runのleaseは変更しない。

## ローカルモデルの文脈予算（Strata含む）

委譲タスクはclientの `chat()` を通らず `_tool_loop_completion` を直接使うため、ターン単位の文脈予算が保存されない。
`_fit_tool_loop_messages_to_budget` はこの場合に実モデルの予算（Strataは `context_size`、既定32768）を都度解決して送信直前の履歴を縮める。
縮め方は決定的で、送信用コピーだけを変更する（永続transcriptは不変）。(1) 全toolの結果を段階的にclip、(2) なお超過なら古い結果をstub化（直近2件は保持）、(3) 古いcall引数の長い文字列を短縮（JSONは維持）。
`tool_calls` と `tool` 結果の対応、system・最初のuser・最新の結果は落とさない。ツール結果の1件上限（`tool_result_chars`）とは別に累積を抑える。

Strataのtool_call形式は公式serverがOpenAI互換の `tool_calls` で返す。`<parameter=…>` やJSON-in-`<tool_call>` などtemplate外の形はserver configの
`"tool_call_recovery": true`（opt-in、公式測定で読取率92.9%→98.3%）で回収できる。既存configの値は変更せず、運用者が `strata-<model>.json` へ追加する。

## 共有Strataと実行期間

既存LLM client、profile registry、共有runtime coordinator、consumer/generation leaseを再利用する。coordinatorを推論proxyとして使わず、lease/resolverの実効base URLを使用する。Agentのprovider/model/effortは受付と実行で照合し、client/wireのmodel不一致も失敗にする。

MCP子プロセスをrunごとに起動することと、同じモデルの二重ロードは別問題。モデルは既存起動identityが一致する場合に共有し、競合workerの強制再起動、別Strataのfallback起動、外部model/CLIへの切替を行わない。tool roundごとにclientを作り直さず、既存のwarm保持・最終lease解放方針を維持する。最終consumer解放後や既存warm期限後の次回実行はcold loadになり得る。

モデル取得、再配置、GPU設定変更、native最大contextへの増量、稼働サービス再起動は接続設定や診断の副作用として行わない。詳細は [共有runtime](shared_local_runtime.md)、[Strata](strata_runtime.md)、[モデル保存先](local_llm_storage_policy.md) の契約に従う。

## 永続化・再起動

正準経路の状態は DB 上の AgentWorkItem / AgentRun / ConversationSession と Run の durable receipt が保持し、`GET /api/agent-tasks/{task_id}` はその現在状態の投影である。専用の AgentTask ORM receipt クラスはない。`data/local_agent_tasks/<task_id>/task.json` / `events.jsonl` は旧 `/api/local-agent` 互換 manager の保存形式で、正準経路の正本ではない（旧receiptの `running` だけで最終結果を判断しない）。

正準receiptは旧managerの再投入対象にしない。安全にadmit済みのpending Workと実行途中のclaimを区別し、実行途中の処理を復元できなければ `uncertain` / `interrupted_by_restart` に確定する。受付途中の旧boot receiptも自動再admitしない。失われた応答は同じ `Idempotency-Key` での再取得や `GET /{task_id}` で確認し、最後の操作を盲目的に再実行しない。

## 検証と適用

まずfake LLM、synthetic worker、隔離したstdio子で確認する。実GPU試験と稼働環境への設定反映・MCP接続更新は別の手順である。

```powershell
$env:AOITALK_TEST_HOME_MCP_PYTHON = 'D:\Dev\80_HomeMCPGateway\.venv\Scripts\python.exe'
$env:AOITALK_TEST_HOME_MCP_SCRIPT = 'D:\Dev\80_HomeMCPGateway\scripts\gateway_stdio.py'
.\venv\Scripts\python.exe -m pytest tests/test_scoped_local_mcp.py tests/test_mcp_plugin.py tests/test_mcp_lifecycle.py tests/test_mcp_tools.py --basetemp .local\mcp-scoped-smoke -q
```

両環境を指定しなければ別venv試験のみskip。試験用TEMP/TMPや `--basetemp` は41の `.local/` に置き、終了後は自分が作ったscratch・子・試験用環境変数だけを片付ける。

主な対象テストは上記MCP群に加え、`test_local_agent_task_adapter.py`、`test_local_agent_task_adapter_orm.py`、`test_local_agent_tasks.py`、`test_local_agent_task_routes_selection.py`、`test_local_agent_task_transport.py`、既存のChat Agent binding/authority、共有runtime/launcher関連。API変更時は [正規のOpenAPI型生成](openapi_typegen.md) でWeb/Mobile両方の契約を更新し、生成物を手編集しない。

2026-10-13（JST）のWindows検証では、41の既存MCP v1環境と80の既存MCP v2環境で、モデルなしのstdio初期化・4ツール一覧・構造化読取・禁止呼出し・切断を確認した。変更対象MCPの47テストがPASSし、syntheticな正常終了・起動失敗・timeout・取消、無関係sentinelの生存、所有loop/context、schema/結果、権限失効とsession置換raceを確認した。

同日の正準task実装ではunit/ORMの76テストがPASSした。さらに既存の実効WSL/toolchain設定を使用したWindows上の実WSL/bubblewrap smokeが1/1 PASS（11.55秒）。正準Work/Runから、構造化 `create_file` による無害な `printf` scriptの作成、実際の隔離コマンド実行、stdout・COW終了処理・監査、実ファイル読取、owner-bound Runの `succeeded` まで確認した。この試験で置き換えたのはモデル応答であり、実Strata/Qwenの推論品質を確認したものではない。

実sandbox試験は `test_local_agent_task_adapter_orm.py::test_opt_in_real_sandbox_command_with_scripted_model` を `AOITALK_TEST_REAL_SCOPED_COMMANDS=1` で明示的に有効化する。アプリが既に使用しているWSL distribution / bubblewrap等の実効設定を試験プロセスにも反映し、隔離したtest workspaceで実行する。未設定の単独Pythonプロセスの既定distributionだけを見て本番backendの準備状況を断定しない。readiness確認を通すための新規install・永続設定変更やpolicyの緩和は不要であり、行わない。

GPUモデルの起動、稼働AoiTalk/Gateway/既存LLMの再起動、永続設定変更は行っていない。検証時の共有runtimeにはprofile/runtime登録がなく、別用途の既存llama GPUプロセスも自動adoptしない。DBの実運用設定反映、通常ChatGPT接続のメタデータ更新、実Qwenの日本語回答・コード読解・tool call、cold/warm性能は別途受入が必要で、実モデル込みの利用可能性は未確認。速度は過去のtok/s値をSLAにせず、queue/cold load/prefill/最初のtoken・可視回答/decode/tool/全体を実測できる項目ごとに記録する。

## deterministic jobとの使い分け

手順が決まった許可済みコマンドは通常Gatewayの既存job機構、途中結果で次の操作が変わるファイル作業や、上記条件を満たした隔離コマンド作業は正準Agent委譲を使い分ける。run専用read-only MCPにjob/browser ID操作を追加したり、未許可のhostコマンドをAgent経由で迂回させたりしない。

## Troubleshooting

| 症状 | 確認 |
| --- | --- |
| `409 local agent tasks are disabled`（旧machinery側） | Enterprise、`local_agent_tasks.enabled`、managerの接続状態 |
| `agent_not_configured`（レガシー） | 旧 `/api/local-agent` で `agent_id` もDBの `default_agent_id` も無い場合。正準は `agent_id` 必須で、欠落は422 |
| `agent_inactive`（レガシー）/ `agent_revision_missing` | 選択Agentの有効状態・公開revision・binding |
| `agent_route_mismatch` | 期待した `provider` / `model` / `effort` とpinned revisionのroute。別モデルで回避しない |
| `canonical_runtime_unavailable`（レガシー） | 旧adapterのみが返すコード（2026-10-13時点の履歴参照）。正準routerは共通runtimeが無いとき `503` / `agent_work_unavailable` を返す |
| `permission_denied` / `authority_revoked`（後者はレガシー） | 本人session、Agent grant、work lease、mode、host操作要求の有無 |
| `command_runtime_unavailable` | WSL/bubblewrap経路では現在のファイルscope対応readinessと実効toolchain設定（Windowsの正準経路は `native_windows` でWSL readinessを要求しない）。host shellへのfallbackや自動installを行わない |
| `background_completion_unverified`（レガシー） | 所有背景jobの終端結果未観測。cleanup結果とjob監査を確認し、受付成功だけで完了にしない |
| `workspace_not_allowed` | 実効 `allowed_roots`。リンク・資格情報領域を使って回避しない |
| `waiting_approval` | 共通の承認状態を確認。独自に自動承認・新規タスク再送しない |
| `interrupted` / `uncertain` | 最後のイベントと副作用を確認。同じ操作の無確認の再送を仮定しない。`reconciliation_required` がある場合だけ、workspaceを確認して `resume` に `operation_id` を渡す |
| `loading_model`（レガシー） | 旧 `/api/local-agent` の `execution_state`。正準の応答には無い。状態取得で新しいモデルを起動しない |
| `Local MCP connection failed` / profile mismatch | 実在する80のPython/script/cwd、`mcp_enabled`、子profile、4ツール、SDK試験 |
| `completion_unverified`（レガシー） | 旧経路の完了ゲート。正準は成功toolレシート等の完了証拠が揃わない場合に `failed` と `failure.reason`（例: `task_no_successful_tool_receipt`）で示す。任意の完了条件の確認とは区別する |


## 2026-10-13 実クライアント接続の追加修正

共通Agentから実ローカルLLMへ進む追加試験で、`set_session_context` に未対応の `session_id` keywordを渡す不具合を確認した。OpenAI互換・Ollamaの既存APIに合わせて `metadata.session_id` を渡し、クライアントの `current_session_id` も正準TurnContextから設定する。キャッシュ済みクライアントに正準セッションがない場合は両方を明示的にクリアする。owner不一致は引き続き拒否する。実クライアントのsetterを含む回帰試験を `tests/test_local_agent_task_session_metadata.py` に追加した。

この引数修正の合格と、実モデルが最後まで仕事を完了したことは別に判定する。追加の実GPU試験は `.local/real-strata-qa/` の隔離SQLite/workspaceと既存共有coordinatorで実施し、live DBへの書込・外部ネットワーク・モデル切替を成功条件のために許可しない。共通基盤の未統合差分が残るcheckoutでの試験を、配布mainの受入完了と扱わない。


09:07 JSTまでの追加受入では、隔離SQLiteとread-only workspaceから既存共有coordinatorを通し、実Strata 0.1.41 / Qwen IQ3_XXSでランダムなmarkerを実ファイルから読み取る仕事が完了した。実LLM 3呼出し・tool 2呼出し、Run/Work成功、元ファイル不変、同一キー再送の非重複を確認し、pytest 1件PASS（808.58秒）。consumer/generation leaseは実行中各1、終了後runtime 0、所有モデルプロセスの消滅を確認した。初回はcold loadを含むため、呼出し686.265/32.750/82.735秒を通常の応答速度保証には使わない。

この実GPU試験は引数修正の初版で実行し、その後のcurrent_session_id帰属・クリア追加は両実provider setterを含む最終146 PASS/20 skipとRuffで検証した。共通基盤の未コミット差分を含む開発checkoutでの結果であり、通常Gateway認証、配布mainの基盤統合、実GPUの最終版再実行は未確認。証跡は `.local/real-strata-qa/receipt.json` と `.local/agent-delegation-20261013/receipt.json`。
