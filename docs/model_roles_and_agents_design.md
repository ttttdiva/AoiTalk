# 役割 → モデル表と、単一 LLM の Agent への移行（設計）

2026-10-30。現状の事実は [model_settings_inventory.md](model_settings_inventory.md) を参照。

## 0. 目的と、今の問題
- 1 つの Agent の中に、Director、Team、Subagent、ExecutionProfile が重なっている。どのモデルで動くかは、`agent_revisions.agent_team_id` と `execution_profile_id` の文字列を、アプリ設定の中で毎回引いて解決している（inventory Part 2 §2.2）。
- 「用途 → モデル」の選択が約 20 か所に分かれ、同じ意味のものが 3 系統ある（Team の execution_profiles、free-team の llm_profiles、旧来の全体 execution_profiles）。fallback の順番を持つのは free-team の pool だけ。
- 画像認識、音声認識、動画、クリップ取り込み、Project Automation、Cloud Advisor が、それぞれ別のタブになっている。画像生成のモデルは、コードに直書きされている。
- 保存済みの設定には、電話受付の説明が残っている（`agent_team.subagents.employee_operator.description`）。

## 1. 新しい形（結論）
1. **役割 → モデルの表**（全体で 1 つ）。役割ごとに、モデルの順序付きの列（先頭が主、残りが fallback）を持つ。
2. **Agent = 1 つの LLM + tool の組**。Agent の版（`agent_revisions`）には、任務、指示、権限、予算、tool、モデルの上書き（役割ごと、任意）を持つ。上書きが無い役割は、全体の表をそのまま使う。
3. **別の作業者には、tool call で委任する**。`delegate(agent=…)` で他の Agent に、`delegate_worker(kind=…)` で外部の作業者（codex、antigravity、claude、grok、muse）に任せる。委任先の権限と予算は、呼び出し側の範囲内に収める。
4. Director、Team、Subagent、ExecutionProfile は廃止する。普通の Chat は、既定の Agent「Aoi」として同じ経路で動かす。

## 2. データの形

### 2.1 役割 → モデル表（アプリ設定 `model_roles`、schema 1）
```yaml
model_roles:
  schema_version: 1
  roles:
    chat:            { routes: [R...], allow_paid_fallback: false }
    summarize:       { routes: [] }        # 空なら chat を継承する
    code:            { routes: [R...] }
    review:          { routes: [] }
    research:        { routes: [] }
    vision:          { routes: [R...], image_mode: auto|always|off }
    audio:           { routes: [], engine: speech_recognition|llm|off }
    video:           { routes: [{provider: mage_vl, model: ...}], video_mode: auto|off }
    site_analysis:   { routes: [R...] }    # 旧 clip_ingest
    project_automation: { routes: [] }
    image_generation:   { routes: [{provider: generation_studio, model: <selection>}] }
    advisor:         { routes: [R...], mode: disabled|manual|automatic }   # 旧 cloud_advisor
R = { kind: llm,  provider, model, effort?, connection_ref? }   # 通常の LLM client
  | { kind: pool, pool_id }                                     # free-team の pool（中の候補順・quota・max_fallbacks に従う）
  | { kind: chatgpt_web }                                       # Advisor 用の ChatGPT Web facade
  | { kind: mage_vl, model }                                    # 動画（専用 runtime）
  | { kind: stt_engine, engine, model? }                        # ローカル STT
  | { kind: generation_selection, selection_id }                # 画像・動画生成（Generation Studio の selection）
```
- `routes` が空の役割は、`chat` を継承する（今の「Main を継承」と同じ）。`chat` だけは空にできない。
- **P1 の保存方式（write-through facade）**: 各役割の**先頭の route** は、今までどおり旧キー（`llm_provider`/`llm_model`、`model_routing.classes.*`、`cloud_advisor.*`、`mage_vl.*`、`speech_recognition.*`）に書き込む。読み手（runtime）は P1 では変えない。`model_roles` に置くのは、fallback の列、新しい役割（code、review、research、summarize、image_generation）、`allow_paid_fallback` だけ。旧 writer（`POST /llm/engine`、`PATCH /settings` の旧キー、`/free-team/settings`）が旧キーを書いた場合は、表の先頭として読み直す（表示は常に旧キーが正）。
- **fallback を始める条件（P1）**: **provider への要求を送る前**の失敗だけ。具体的には、client を作れない、admission や quota の予約で拒否された、CLI の実行ファイルが無い。要求を送った後の失敗（タイムアウトを含む）では、次へ進まない。要求を送った後の fallback は、試行の台帳（試行ごとの credential、課金の種類、予約、確定した usage）と、free-team の「外側で再試行しない」を表す型付きのエラーができてから入れる（P3 以降）。free-team の pool の中では、pool 自身の再試行に任せ、外側では重ねない。
- **課金の境界**: 課金の種類は、provider からではなく **credential profile の `billing_mode`** で決める（complimentary、free_tier、promo_credit は free、subscription_cli は subscription、それ以外の API は paid、ローカルは local）。決められないものは paid として扱う。列の中で free から paid に変わる位置では、`allow_paid_fallback: true` のときだけ次へ進む。既定は false。
- 秘密値（`base_url`、`api_key`）は表に入れない。route ごとの接続先は、`connection_ref` で既存のキー（例: `model_routing.classes.vision`）を指す。
- `model_roles` に version を持たせ、保存は CAS（`expected_version`）で行う。初回の作成は一意の migration id で 1 回だけ行い、旧キーは消さない。`model_roles` を消せば、P1 の前と同じ動きに戻る。

### 2.2 Agent の版（`agent_revisions` に列を足す。版は今と同じく不変）
| 列 | 型 | 内容 |
|---|---|---|
| `model_overrides` | JSON `{role: {routes:[R...], allow_paid_fallback?}}` | 全体の表との差分だけを持つ。空なら全体の表を使う |
| `tool_ids` | JSON list | Agent に**見せる** tool（または tool group）。権限の上限は、今までどおり `capability_ceiling` と既存の authority guard（owner、Project の ACL、ActionPolicy を含む）が決める。tool group は、サーバー側の catalog で capability と resource scope に展開してから、guard を通す（`capability_ceiling` は置き換えない） |
| `delegate_policy` | JSON | 委任してよい Agent の id（または `any_owned`）と、作業者の種類の allowlist |
| `budget_policy`（既存） | JSON | `max_tokens`、`max_tool_calls`、`max_cost_micros`、`currency`。AoiDots の run budget（`runtime.agent_run_max_*`）の既定を、Agent ごとに上書きできるようにする |

- `agent_team_id`、`execution_profile_id`、`allowed_subagent_ids` は、P2 では**列を足すだけ**（expand-only）で、旧列にはこれまでどおり値を書く。すべての reader と writer（API、Operations UI、Mobile の生成型を含む）を移したあとで、最後の段階で nullable にしてから削除する。
- retired の Agent（4 件）には新しい版を作らない（履歴専用）。
- `conversation_agent_bindings` と `agent_runs` の `route_snapshot` はそのまま残す（実行したときの記録）。

### 2.3 廃止するアプリ設定
`agent_team.*`（teams、subagents、execution_profiles、orchestration_mode、delegation_enabled）、トップレベルの `execution_profiles`、`routing_profiles.free-team.llm_profiles`。
- 退避先は `legacy_snapshots.<migration_id>` とする。保存時の暗号化と、API で返すときの秘匿処理（redaction）はそのまま適用する。
- 退避と同じ transaction で app config の schema version を上げる。そのうえで、旧 Team の migration と既定値の seed（`_migrate_agent_team_v2_config`）を、その version 以降では実行しないようにする（そうしないと、次の起動で既定の Team が復活する）。
- `model_routing.classes.*`、`cloud_advisor.*`、`chatgpt_web.*`、`mage_vl.*`、`speech_recognition.*` は**廃止しない**。これらは各役割の先頭の route の実体として残す。`chatgpt_web.*` は、Advisor の ChatGPT Web facade も使っている。

## 3. 委任の tool
### 3.1 `delegate`（Agent に任せる）
- 既存の `delegate_agent_work`（AgentDialogue、`tools/agent_dialogue.py`）を、この名前に一本化する。状態確認、追加の指示、停止、再開の tool 群もそのまま残す。
- 引数: `agent_id`、`instruction`、`title`、`project_id?`、`task_id?`、`budgets{max_turns,max_tokens,max_cost_micros}`、`deadline?`。
- 制限:
  - 呼び出し側の `delegate_policy` に、委任先の Agent が含まれていること。
  - 委任先の tool は、委任先の版の `tool_ids` と、呼び出し側の `tool_ids` の**共通部分**に絞る（権限を広げない）。
  - 予算は、呼び出し側の run の残り予算から確保する。AoiDots の run budget の ledger と、同じ予約の仕組みを使う。
- `agent_team_delegate`、`load_agent_team`、`AgentDelegation` の API だけの発注（tool からは呼べない）は、`delegate` に統合する。過去の 16 行は読み取りで残す。

### 3.2 `delegate_worker`（外部の作業者に任せる）
- 引数: `kind`（`codex|antigravity|claude|grok|muse`）、`instruction`、`workspace_scope`（read か write と、path の allowlist）、`model?`、`effort?`、`timeout_seconds?`、`budgets{max_tokens,max_cost_micros,max_attempts}`。
- 実行の範囲: 実行時に、OS の sandbox や worktree の範囲と、呼び出し側の Agent の実効の scope との共通部分を、path ごとに強制する。作業者からの再委任はできない（depth 1）。停止と再開は、既存の AgentDialogue の仕組みに載せ、監査の id を付ける。
- `delegate` の引数も、既存の契約（`max_depth`、`max_attempts`、`cost_currency`、`predecessor_dialogue_id`）を落とさない。
- 実体は既存のものを使う。`kind=muse` は muse_remote_execution、それ以外は cli_llm_client と CLI session。provider としてではなく、tool の結果（要約、diff、生成物の参照、usage）として返す。
- 制限:
  - 呼び出し側の `tool_ids` に `delegate_worker:<kind>` があること。
  - write を指定するには、呼び出し側に `workspace_write` と `command_execute` があること。
  - 外部の作業者の内部 tool は、`allow_cli_native_tools` 相当の既存の判定で絞る。
- 今の「CLI Development」用の Agent（slug `cli-development-*`）は、そのまま「その CLI を model_overrides の chat に持つ Agent」に移す。会話の挙動は変えない。

### 3.3 Jev のブラウザ操作
すでに普通の tool（`browser_agent`、`browser_agent_dedicated`）なので、`tool_ids` に `browser` を入れるだけでよい。tool の実装（Edge の担当が LocalBrowserBridge に差し替え中）には触らない。

## 4. 画面（設定 › 言語モデル → 「モデル」の 1 画面）
```
┌ 無料枠と課金 ───────────────────────────────────────────────┐
│ openai-luna 残 812k/1M ・ gemini-free-flash 残 1,320 req ・ openrouter 残 $0.42 │
│ 課金になる経路: code（codex-cli はサブスク）, advisor（OpenAI API・従量）      │
└──────────────────────────────────────────────────────────────┘
┌ モデル一覧（ドラッグ元） ┐ ┌ 役割 → モデル（ドロップ先・並べ替え） ────────────┐
│ 検索 [          ]       │ │ 会話      [1 openrouter deepseek-v4.1] [2 pool:coordinator] [+] │
│ ▸ 無料枠  (残量つき)    │ │ 要約      （会話を継承）                                  [+] │
│ ▸ サブスク CLI          │ │ コード    [1 codex-cli gpt-6.1-sol xhigh]                [+] │
│ ▸ API（従量）           │ │ 画像認識  [1 会話を継承]   送信: auto                    [+] │
│ ▸ ローカル              │ │ 動画      [1 mage_vl Mage-VL] 動画: auto                 [+] │
│ ▸ 外部作業者（Muse など）│ │ 画像生成  [1 generation_studio …]                        [+] │
└─────────────────────────┘ └──────────────────────────────────────────────┘
```
- チップの表示: provider、model、課金の種類のバッジ（`free`（残量つき）、`subscription`、`paid`、`local`）、effort の選択、削除ボタン。
- 操作:
  - 一覧から役割の行へドラッグして追加する。行の中では、ドラッグで順番を入れ替える。
  - ドラッグできない環境（キーボード、タッチ）のために、各チップに「上へ」「下へ」「削除」のボタンを付け、行には「追加」メニューを付ける。
  - 実装は、コードベースのほかの D&D と同じく、ネイティブの HTML5 DnD を使う。
- 課金の種類が free から paid に変わる位置に、区切りを表示する。`allow_paid_fallback` が false なら、その先のチップを薄く表示し、「ここで停止」と添える。
- 役割ごとの付随の設定は、行の右端の小さな歯車に入れる（画像の送信方法、動画の auto か off、音声のエンジン、advisor のモード）。
- 残量と課金の情報は、無料Team の既存 API を使う（`GET /free-team/usage`、`/free-team/settings` の `credentials[].billing_mode`、`/free-team/openrouter/quota`）。課金の種類は `billing_mode` と provider の対応から決める。
- 残すタブ: 「接続」（provider の Base URL、API key、ローカル runtime、Ollama）、「プライバシー」（外部送信とマスキング用のローカルモデル）、「無料Team」（pool と候補の詳細）。消すタブ: Base Model のモデル選択、Routing、Execution Team、Cloud Advisor。
- Agent の画面（Operations › Agent › 職務・モデル）では、同じ行の部品を使って「全体の表から継承」と「この Agent だけ上書き」を表示する。tool はチェック一覧で選ぶ。

## 5. 移行
### 5.1 全体の表（P1。自動、何度実行しても同じ結果、元のキーは残す）
| 役割 | 移行元 |
|---|---|
| chat | `llm_selection_kind=routing_profile` なら `[{pool: main_pool_id}]`。それ以外は `[{llm_provider, llm_model, <provider>.reasoning_effort}]` |
| vision、audio、site_analysis、project_automation | `model_routing.classes.<旧名>`。inherit なら `[]`。`image_mode`、`audio.engine` は付随の設定へ |
| video | `model_routing.classes.video.model` か `mage_vl.model` と、`video_mode` |
| advisor | `cloud_advisor.provider/model/reasoning_effort/mode` |
| image_generation | 今の直書きの値 `gemini-3-pro-image-preview` |
| code、review、research、summarize | 空（会話を継承）。P2 で Agent の上書きから入れる |

### 5.2 Agent の版（P2。各 Agent に新しい版を 1 つ追加し、古い版は不変のまま残す）
各 Agent の最新の版について、次のように決める。
- **model_overrides**: (team, profile) の default_route と、`allowed_subagent_ids` に対応する役割ごとの overrides を解決する。作業の役割から新しい役割への対応は次のとおり。
  - planner、researcher → research
  - writer、data_worker、general_worker、operator → chat
  - code_worker、explorer → code
  - reviewer → review

  そのうえで、全体の表と違う役割だけを書き込む。`manual` は上書きなし、`free-team` は `{pool: coordinator}` にする。
- **tool_ids**: `capability_ceiling` に、許可されていた Subagent の capability のうち `capability_ceiling` 以内のものを足す。さらに、Team 経由で使っていた委任を `delegate` と `delegate_worker:<kind>` に置き換える（muse-remote の profile → `delegate_worker:muse`、CLI の provider で Subagent を使っていたもの → その CLI の `delegate_worker`）。
- 移行の前と後で、各 Agent の役割ごとの route が一致することを確かめる。移行スクリプトは dry-run の差分表を出し、一致しない Agent があれば止まる。
- 実データの例:
  - CrowdWorks担当: review = openai gpt-5.6-sol high。tool は external_action_propose と delegate。
  - アプリ開発担当: code と chat = codex-cli gpt-6.1-sol xhigh（code_explorer と code_implementer は high）。tool は workspace と command と delegate_worker:muse。
  - AoiDots: 上書きなし。tool は aoi_tools、web_read、delegate。
- 既存のデータの不整合（品質review担当の employee_operator が config の Team にいない、personal_private の Team が builtin にしかない）は、移行のときに「今の実行時と同じ結果」を正として解決する。

### 5.3 実行中のものと、固定された参照
- **セッションごとのモデル指定**（チャット画面のモデル選択、`/llm/session-settings`、`/llm/new-chat-defaults`）: 残す。その会話の `chat` の列の先頭に重ねる（fallback は全体の列を使う）。
- **会話と Agent の紐付け**（`conversation_agent_bindings` は版の id を固定している）: P2 では、新しい版を作ったら、次の turn で active な紐付けを新しい版に付け替える。進行中の turn は古い版のまま終わらせる。付け替えの仕組みがあるか、版の更新が今どう伝わるかは、P2 の最初に Codex が確かめる。
- **進行中の作業**（`agent_work_items` と `agent_runs` は版と route_snapshot を固定している）: 何もしない。古い版のまま、旧 resolver で最後まで動かす。P5 は、旧版を参照する未完了の作業が 0 件になってから行う。

### 5.4 戻し方
- P1: `model_roles` を消すと、旧キーの解決に戻る。
- P2: 旧 resolver を P5 まで残すので、新しい版を退役させて古い版を active に戻せる。
- P3: `legacy_snapshots` から `agent_team` を書き戻せる。

## 6. 段階分け（レビュー反映後。段階ごとに merge して「再起動要」を報告する）
| 段階 | 内容 |
|---|---|
| **P1** | `model_roles`（version と CAS）と型付きの GET/PUT API。旧キーからの一回だけの作成（migration id 付き）。write-through facade（各役割の先頭は旧キーに書き、runtime の reader は変えない）。要求を送る前だけの fallback。課金の種類は credential の `billing_mode` で決める。設定の D&D 画面。電話受付の説明の移行（旧 builtin の文と完全に一致する場合だけ） |
| P2 | `agent_revisions` に列を足すだけ（`model_overrides`、`tool_ids`、`delegate_policy`）。版の移行（旧 work role ごとに実効の route を展開し、新しい役割にまとめたときに値が衝突したら止めて、手で解決する表を出す。legacy の heavy/light/coding、`main_pool_id`、`llm_profiles` も正規化する。retired は除外）。Agent の実行時は「版の上書き → 全体の表」で解決する |
| P3 | `delegate` と `delegate_worker` を、旧 tool と**並べて**入れる（sandbox、depth、予算、監査）。試行の台帳を作り、要求を送った後の fallback を入れる。AoiDots の run budget と統合する |
| P4 | 普通の Chat を、ユーザーごとの Agent「Aoi」で動かす（AoiDots と同じ private root の方式。作れない場合は今の Chat の経路に戻す） |
| P5 | Team、Subagent、ExecutionProfile、Director を撤去する（退避と schema version を同じ transaction で行う）。旧列は、すべての consumer の移行記録を確認してから nullable にし、削除する。OpenAPI と Web、Mobile の型を再生成する |

## 7. 電話受付の名残
- 保存済みの `agent_team.subagents.employee_operator.description` が、旧 builtin の文「管理者が設定した自動化と電話受付を担当する。」と**完全に一致する場合だけ**、新しい builtin の文に置き換える（ユーザーが編集した文には触れない）。P1 の設定の migration で行う。
- P3 で Subagent 自体を廃止したときに、この項目も消える。

## 8. 他の担当との重なり
- AoiDots（run の上限）: `agent_run_budget.py` と `runtime.agent_run_max_*` を、P3 の委任の予算の土台にする。P3 に入る前に、その担当と、ledger の予約の API と、版の `budget_policy` との優先順位を決める。
- Edge（内蔵ブラウザ）: `browser_agent` の実装には触らない。tool の id（`browser`）を参照するだけ。
- free-team: pool と候補はそのまま使う。`llm_profiles` だけを役割の表へ移す。
