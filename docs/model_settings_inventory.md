# モデル設定と Agent 実行経路の棚卸し（2026-10-30）

調査は read-only（API は GET、DB は read-only の SELECT）。設計は [model_roles_and_agents_design.md](model_roles_and_agents_design.md) を参照。

---

- FE の `pyFetch(path)` は `/api/python-proxy${path}` を叩き、BE の `/api${path}` に届く（`components/settings/llm-model-section-types.ts:1877`）。
- 汎用保存は `PATCH /api/settings {key,value}`（`api/routes/config_routes.py:1486`）。許可キーは `ALLOWED_SETTINGS`（同 `:583-812`）。読込は `GET /api/settings`（`:834`）。
- 秘密値（`*.api_key` 等）は設定名だけを記載し、値には触れていない。

---
## 1. 画面上のセクション／タブ構成

| 階層 | 画面の表示名 | 実装 file:line |
|---|---|---|
| 設定カテゴリ | 会話・AI応答 › **言語モデル** | `app/(app)/settings/page.tsx:1266`、`settings-target-registry.ts:199`（targetId `llm-model`） |
| 上段タブ（自前の tablist） | Base Model / Routing / Execution Team / Privacy & Advanced / Cloud Advisor | `settings/llm-model-section.tsx:161-167`（state `activeSection` `:58`） |
| Base Model › 内側タブ | 基本モデル（`language`） | `llm-model-section.tsx:361, 399-521` |
| 　Base 内のパネル | 接続設定 / Effort（LlmModelGroupsPanel） | `llm-model-section.tsx:648-672` → `llm-model-groups-panel.tsx:232-290` |
| 　Base 内のダイアログ | 「日次予算を設定」（FreeTeamSettingsPanel quotaOnly） | `llm-model-section.tsx:446-460` |
| 　Base 内のパネル | Strata / Managed local runtime | `llm-model-section.tsx:500-519` |
| 　Base 内のパネル | 候補一覧・Ollama Pull・削除 | `llm-model-section.tsx:674-868` |
| Routing › カードと内側タブ | 画像認識 / 音声認識 / 動画認識 / クリップ取り込み / Project Automation | カード `llm-model-section.tsx:313-358`、タブ `:362-366` |
| 　画像認識 | `vision` | `llm-media-model-tabs.tsx:114-179` |
| 　音声認識 | `audio` | `llm-media-model-tabs.tsx:181-224` |
| 　動画認識 | `video`（Mage-VL） | `llm-media-model-tabs.tsx:226-348` |
| 　クリップ取り込み | `clip_ingest` | `llm-media-model-tabs.tsx:350-449` |
| 　Project Automation | `project_automation` | `llm-project-automation-routing.tsx:90-237` |
| Execution Team | 有効トグルと保存 + 内側タブ Teams / Subagents / Director | `llm-model-section.tsx:872-888` → `llm-agent-team-routing.tsx:458-701`（タブ `:489-491`） |
| 　Teams | Team一覧・詳細・所属Subagent・Topology・Execution Profiles | `llm-agent-team-routing.tsx:494-651` |
| 　Subagents | 役割・指示・Capability など | `llm-agent-team-routing.tsx:652-675` |
| 　Director | Web版ChatGPTをDirectorにする | `llm-agent-team-routing.tsx:676-697` |
| Privacy & Advanced | Embedding runtime / Provider表示 / OpenRouter上流 / llama.cpp model root と runtime / 無料Team（全体） / 外部送信 | `llm-model-section.tsx:524-625` |
| Cloud Advisor | Cloud Advisor | `llm-model-section.tsx:627-646` → `llm-cloud-advisor-settings.tsx` |
| 別カテゴリ | 会話・AI応答 › キャラクター › 音声エンジン（Gemini TTS / Irodori TTS） | `page.tsx:1265`、`characters-section.tsx:1159, 1210, 1219`、`gemini-voice-settings.tsx`、`irodori-voice-settings.tsx` |
| 別カテゴリ | ナレッジ・検索 › 検索プロバイダ（`search.openai_model`） | `page.tsx:1492`、`search-settings-section.tsx:123,148` |
| 別カテゴリ | ナレッジ・検索 › クリップ取り込み（取り込み先だけでモデル項目なし） | `page.tsx:1495`、`clip-ingest-targets-section.tsx` |
| 別カテゴリ | 外部連携 › ComfyUI（画像生成ワークフロー） | `page.tsx:1571`、`comfyui-section.tsx:287+`、`lib/comfyui-api.ts:26` |
| なし | 画像生成モデル、動画生成、STTエンジン、Realtime音声のモデルを選ぶ UI は存在しない | §3 を参照 |

補足: `LlmModelGroupsPanel` の `llm-model-groups-panel.tsx:72-230` は `className="hidden"` で常に非表示になっている。中身は画像認識・音声認識の **重複コントロール**（同じ `classDrafts` を編集する）で、デッドUIになっている。

---
## 2. フィールド一覧（設定値 → 保存キー → API → BE の読み手）

### 2.1 Base Model（基本モデル）
保存 API: `POST /api/llm/engine`（`api/routes/llm_routes.py:3051`、FE `llm-model-section-state.ts:680-720`）。読込 API: `GET /api/llm/models[?refresh&provider]`（`llm_routes.py:2795`、FE `state.ts:318`）。

| UI (file:line) | 設定内容 | 書込キー（engine route 内の `_apply_config`、`llm_routes.py:3570-3700`） | BE runtime の読み手 | 備考 |
|---|---|---|---|---|
| プロバイダー `llm-model-section.tsx:402-415` | Main chat の provider | `llm_provider`、`llm_selection_kind`（static/routing_profile）、`routing_profile_id`（`free-team`/空） | `llm/manager.py:1057,1086`、`services/session_llm_runtime.py:198` | `routing-profile` を選ぶと無料Teamになる |
| モデル `:417-442`、候補リストのクリック `:820`、カスタムID `:463-497` | Main chat の model | `llm_model` と `<provider>.model`（`openai.model`、`codex_cli.model`、`ollama.model`、`openrouter.model` …） | `manager.py`、`session_llm_runtime.py:283`、`execution_profile_service.py:335`（resolve_execution_main_route） | **重複**: 同じ値が `llm_model` と `<provider>.model` の2か所に入る |
| Base URL `llm-model-groups-panel.tsx:235` | 接続先 | `<provider>.base_url`、`sglang_base_url`、`openai_compatible_local.base_url` | 各 engine（`llm/openai_compatible_local_engine.py`、`llm/sglang_url.py`） | 表示は `CONNECTION_SETTINGS_PROVIDERS` に含まれる provider のときだけ |
| API key `groups-panel.tsx:245` | 認証情報 | `openrouter_api_key`、`kimi_api_key`、`deepseek_api_key`、`deepinfra_api_key`、`ollama.api_key`、`sglang_api_key` | 各 engine | 空欄なら既存値を維持 |
| Effort `groups-panel.tsx:262-280`（保存 `:283`） | 推論の強さ | `codex_cli.reasoning_effort`、`claude_cli.reasoning_effort`、`<provider>.reasoning_effort` | `cli_llm_client.py`、各 engine | 表示は `REASONING_EFFORT_PROVIDERS` のとき |
| llama.cpp runtime `llama-cpp-runtime-panel.tsx`（`llm-model-section.tsx:600`） | ローカル起動パラメータ | `openai_compatible_local.llama_cpp.*`（engine route の `llama_cpp`） | `llm/openai_compatible_local_profiles.py` | 配置は Privacy タブ |
| 「選択中の候補を更新」`:676` | カタログの再取得 | なし（`GET /llm/models?refresh=true`） | — | |
| Ollama Pull `:707-758`、削除 `:846` | ローカルモデルの導入と削除 | なし（`POST /ollama/pull`、`DELETE /ollama/models`） | — | |
| Strata / Managed runtime `:500-519` | ローカル runtime の準備 | なし（`/llm/local-runtime/status|prepare|tasks/*`、`llm_routes.py:1912-2408`） | — | |
| 日次予算ダイアログ `:446-460` | QuotaPool の停止上限 | DB `free_team_quota_pools`（`PATCH /free-team/quota-pools/{id}`） | `services/free_team_service.py` | **重複**: Privacy の FreeTeamSettingsPanel にも同じ QuotaPool 編集がある |

### 2.2 Routing（用途別）
読込: `GET /api/settings` の `settings.model_routing`、`settings.mage_vl`、`settings.speech_recognition`、`schema`（`state.ts:1253-1350`）。
保存: `PATCH /api/settings` をキーごとに順番に呼ぶ（`saveRoutingSettings(scope)`、`state.ts:1422-1580`）。

| UI (file:line) | 設定内容 | 書込キー | BE の読み手 | 備考 |
|---|---|---|---|---|
| 画像 プロバイダー `media-tabs.tsx:123-142` | vision の provider（`inherit` = 言語モデルと同じ） | `model_routing.classes.vision.inherit`、`.provider` | `services/media_recognition_service.py:563,1074`、`api/server_parts/chat_message_mixin.py:778` | 候補は `media.image=true` のモデルを持つ provider。BE の enum は `MODEL_ROUTING_PROVIDERS` から claude-cli/grok-cli/deepseek を除いたもの（`config_routes.py:770`） |
| 画像 モデル `:145-153` | vision の model | `model_routing.classes.vision.model` | 同上 | カスタム入力は表示されない（非表示の重複UIにだけある） |
| 画像の送信方法 `:173-176` | auto/always/off | `model_routing.media.image_mode` | `chat_message_mixin.py:774` | |
| （UIなし・暗黙に保存） | effort | `model_routing.classes.vision.reasoning_effort` と `.mode`（同じ値）、`.base_url`、`.api_key` | `reasoning_effort` は deepinfra の場合だけ `media_recognition_service.py:1373` が読む。**`.mode` は未使用** | |
| 音声 プロバイダー `:190-210` | local-stt / inherit / provider / off | `model_routing.classes.audio.engine`（speech_recognition/llm/off）、`.inherit`、`.provider` | `media_recognition_service.py:786` | provider の enum は固定リスト（`config_routes.py:806-811`） |
| 音声 モデル `:213-219` | audio の model | `model_routing.classes.audio.model` | 同上 | local-stt のときは `speech_recognition.current_engine` と `engines.<e>.model` を **表示するだけで編集UIはない**（読み手 `media_recognition_service.py:575-576`） |
| （暗黙） | | `model_routing.classes.audio.reasoning_effort` と `.mode`、`.base_url`、`.api_key` | **`.mode` は未使用** | |
| 動画認識 `:235-244` | auto(Mage-VL)/off | `model_routing.media.video_mode` | `media_recognition_service.py:942` | provider は `mage_vl` に固定され、表示だけ（`:247-251`） |
| 動画 モデルID `:255` | Mage-VL のモデル | `model_routing.classes.video.model` **と** `mage_vl.model` | `services/mage_vl_service.py:335-339`、`media_recognition_service.py:920` | **重複キー**（同じ値を2キーへ保存） |
| 動画 SGLang Base URL `:267`、APIキー `:278` | 接続先 | `model_routing.classes.video.base_url` と `mage_vl.base_url`、`model_routing.classes.video.api_key` | 同上 | Base URL も **重複キー** |
| Mage-VL有効 / SGLang管理 / 事前ロード `:289,307,311` | runtime | `mage_vl.enabled`、`.managed`、`.preload_on_start` | `mage_vl_service.py` | |
| フレーム数・最大サイズ・最大長・起動待ち `:317-330`、起動コマンド `:334` | runtime | `mage_vl.num_frames`、`.max_video_bytes`、`.max_video_duration_seconds`、`.startup_timeout_seconds`、`.server_command` | `mage_vl_service.py` | `max_pixels`、`max_new_tokens`、`codec_engine` は UI がなく、既定値で上書き保存される（`state.ts:1540-1552`） |
| （暗黙） | | `model_routing.classes.video.mode`、`.inherit=false` | **`video.mode` は未使用** | |
| クリップ プロバイダー `:359-398` | inherit / provider | `model_routing.classes.clip_ingest.inherit`、`.provider` | `services/docs_ingest_service.py:466,512-540` | 候補は routing_profile と claude/grok を除いたもの（`state.ts:32,250`）。BE の enum は `AGENT_TEAM_PROVIDERS` |
| クリップ モデル `:401-432` | model | `model_routing.classes.clip_ingest.model` | 同上 | |
| クリップ Effort `:437-445` | effort | `model_routing.classes.clip_ingest.reasoning_effort` と `.mode` | `docs_ingest_service.py:771`（`reasoning_effort` を見て、無ければ `mode`） | |
| PA Route `project-automation-routing.tsx:101-123` | inherit / dedicated | `model_routing.classes.project_automation.inherit`、`.mode`（`""`/inherit/dedicated） | `services/project_automation_model.py:13,133,176`、`project_overview_service.py`、`project_steward_service.py:2218`、`knowledge_capture_research_service.py:67` | **キー名の衝突**: PA の `.mode` は inherit/dedicated を表すが、他の class の `.mode` は effort を表す |
| PA プロバイダー / モデル / Base URL / APIキー / Effort `:128,146,171,186,206` | 専用ルート | `...project_automation.provider`、`.model`、`.base_url`、`.api_key`、`.reasoning_effort` | 同上 | 候補は `TARGET_CLIENT_PROVIDERS`（`llm/manager.py:509-524`）∩ schema。provider を変えるときは一度 inherit に戻してから順に保存する（`state.ts:1464-1508`） |

### 2.3 Execution Team
読込: `GET /api/agent-team/config`（`config_routes.py:1071`。capability_catalog、各 Team/Subagent の effective_route、main_effective_route を返す）。`state.ts:1255` と `llm-agent-team-routing.tsx:258` が **2回取得している**。
保存: `PUT /api/agent-team/config {agent_team}`（`config_routes.py:1128`。`save_to_file("agent_team", …)` で文書全体を保存、`:1460`前後）。その後 `saveRoutingSettings("agent")` が `chatgpt_web.*` を PATCH する（`routing.tsx:285-315`）。

| UI (`llm-agent-team-routing.tsx`:line) | 書込キー | BE の読み手 | 備考 |
|---|---|---|---|
| Execution Team topologyを有効にする `:478` | `agent_team.delegation_enabled` | `agent_team_v3.py:903`（`agent_team_v3_delegation_enabled`）→ `llm/runtime_tool_registry.py:580,2129`、`llm/native_runtime.py:678`、`llm/prompts.py:250` | **重複経路**: PATCH 用キー `agent_team.delegation_enabled` も許可されている（`config_routes.py:590`） |
| Team名・説明 `:504,507` | `agent_team.teams.<id>.name`、`.description` | 表示とプロンプトのみ | |
| Team 有効 `:509` | `agent_team.teams.<id>.enabled` | `agent_team_v3.py:1238`（無効なら Main を継承）、`agent_team_scope_active :1474` | |
| 利用タイミング `:510`、利用できる場面 `:512` | `.activation.mode`（always/contextual/manual）、`.activation.contexts`（`app_development` のみ） | `agent_team_scope_active`（`agent_team_v3.py:1474`）→ `llm/director_controller.py:296` | |
| 所属Subagent チェック `:520` | `agent_team.teams.<id>.subagent_ids[]` | `resolve_team_execution_route`（`agent_team_v3.py:1240-1246`） | チェックを外すと、全 profile の `overrides` からその Subagent を削除する（`routing.tsx:316-330`） |
| Topology preview `:524` | なし（読み取り専用） | — | `agent-team-topology-preview.tsx:52-186`（Main → Team → Subagent） |
| Execution Profile 名・有効 `:557,569` | `agent_team.teams.<id>.execution_profiles.<pid>.name`、`.enabled` | `agent_team_v3.py:1247-1249` | |
| default_route `:578`（ExecutionRouteEditor） | `...execution_profiles.<pid>.default_route` = `{inherit_model, provider, model, effort_policy(same/lower/explicit/default), effort}` | `agent_team_v3.py:1250-1256`、`_apply_execution_route :1153` | editor は `llm-agent-team-execution-route.tsx:336`（preset）、`:352`（provider、`muse-remote` を含む）、`:371`（model）、`:388`（effort） |
| Subagent別 override `:594-624` | `...execution_profiles.<pid>.overrides.<subagent_id>` | `agent_team_v3.py:1251-1252` | |
| Subagent 名・説明・指示 `:661-663` | `agent_team.subagents.<sid>.name`、`.description`、`.instructions` | `llm/specialist_delegate.py`、`runtime_tool_registry.py` | |
| Subagent 有効 `:664` | `.enabled` | `agent_team_v3_subagents(include_disabled=False)`（`:982`） | |
| Capabilities `:665` | `.capability_ids[]` | `filter_agent_team_capabilities`（`agent_team_v3.py:1395`） | |
| Workspaceアクセス / CLI native `:668` | `.max_workspace_access`、`.allow_cli_native_tools` | `agent_team_v3.py:1357-1394` | |
| 拡張可能・既定/最大インスタンス `:669` | `.scalable`、`.default_instances`、`.max_instances` | `runtime_tool_registry.py` | |
| Web版ChatGPTをDirectorに `:680` | `agent_team.orchestration_mode`（standard/director） | `services/agent_team_service.py:802` → `llm_routes.py:3360`、`assistant/modes/terminal_mode.py:3384` | **重複経路**: PATCH キー `agent_team.orchestration_mode`（`config_routes.py:586,1739`） |
| Director詳細 `:693`（保存先・待ち秒・往復回数） | `chatgpt_web.profile_dir`、`.response_timeout_seconds`、`.max_rounds_per_turn` | `llm/chatgpt_web_provider.py:102,405`、`llm/director_controller.py:480` | 接続確認は `GET /api/chatgpt-web/status`、`POST /settings-browser`、`POST /check-login`（`chatgpt_web_routes.py:35,39,62`） |

### 2.4 Privacy & Advanced
| UI (file:line) | 書込キー / API | BE の読み手 | 備考 |
|---|---|---|---|
| Embedding runtime `llm-model-section.tsx:531` | `search.embedding_enabled`（PATCH、`lib/embedding-master-setting.ts:5`） | search/RAG 系 | モデル選択ではない |
| Provider表示 `llm-provider-visibility-settings.tsx:159`（保存 `:101`） | `llm_provider_visibility.hidden_provider_ids`（PATCH） | BE は GET で返すだけ（`config_routes.py:887`） | **BE runtime では未使用**。FE の `lib/llm-provider-visibility.ts` がヘッダー表示を絞り込むためだけに使う |
| OpenRouter上流 `openrouter-provider-routing.tsx:216-345`（provider=openrouter のときだけ） | `GET/PATCH /api/llm/openrouter/provider-routing`（`llm_routes.py:2927,2953`）→ `openrouter.model_provider_options.<model>`（order/only、allow_fallbacks、require_parameters、zdr） | `llm/openrouter_provider_routing.py:142,151` | |
| llama.cpp model root `llm-model-section.tsx:545-597` | `PUT /api/llm/llama-cpp/model-root`（`llm_routes.py:2448`） | llama.cpp launcher | |
| 無料Team（全体） `:615`（Base provider が routing_profile のときだけ） | §5 を参照 | | |
| 外部送信 `llm-external-privacy-settings.tsx:37-131` | `external_model_privacy.mode`、`.review_policy`、`.raw_media_policy`、`.semantic_redaction_enabled`、`.notify`、**`.local_provider`、`.local_model`**（マスキング用モデル）、`.redaction_terms`、`.trusted_local_hosts`（各フィールドを PATCH、`state.ts:1378`） | `llm/native_runtime.py:2514,2548`、`services/outbound_privacy_service.py`、`services/deep_research_service.py:3535`、`services/app_workflow.py:1768` | local model の候補は ollama/sglang/openai_compatible_local のモデル（`llm-model-section.tsx:117-130`） |

### 2.5 Cloud Advisor
保存: PATCH ×4（`state.ts:1398-1420`）。

| UI (`llm-cloud-advisor-settings.tsx`:line) | 書込キー | BE の読み手 | 備考 |
|---|---|---|---|
| モード `:114-129` | `cloud_advisor.mode`（disabled/manual/automatic） | `llm/prompts.py:199`、`llm/tool_policy.py:2888`、`services/app_workflow.py:1773`、`services/cloud_advisor_service.py:751` | |
| Provider `:134-152` | `cloud_advisor.provider` | `cloud_advisor_service.py:792` | enum は openai / deepinfra / chatgpt-web（`config_routes.py:645-650`） |
| モデル（空欄なら既定） `:157-166` | `cloud_advisor.model` | `cloud_advisor_service.py:804` | 自由入力 |
| Reasoning effort `:168` | `cloud_advisor.reasoning_effort` | `cloud_advisor_service.py:813,830-863` | |

---
## 3. 「用途 → モデル」一覧（モデルを選んでいる箇所すべて）

| 用途 | config key | UI での編集場所 | 候補 | fallback / 順序 |
|---|---|---|---|---|
| Chat Main（全体の既定） | `llm_provider`、`llm_model`、`<provider>.model`、`<provider>.reasoning_effort` | Base Model › 基本モデル | `GET /llm/models` のカタログ（deployment で絞り込み、`filterAvailableProviders`） | deployment の固定値が優先（`lib/llm-provider-visibility.ts`） |
| Chat Main（セッション別） | セッション DB（`/api/llm/session-settings`、`/api/llm/new-chat-defaults`、`llm_routes.py:4186-4268`） | チャット画面（`lib/chat-llm-settings.ts:163-452`）。Settings には無い | 同じカタログ | 未指定ならグローバル Main を使う（`execution_profile_service.py:266`） |
| 無料Team（Main を routing profile にする場合） | `llm_provider=routing-profile`、`llm_model=free-team`、`routing_profiles.free-team.*`、DB の candidates | Base で「無料Team」を選ぶ → Privacy の無料Team パネル | DB の候補モデル（openai-luna/sol、gemini-free-*、openrouter-free-*、codex-spark、antigravity、grok-build …） | pool の `candidate_ids` 順に `max_fallbacks` 回まで。超過したら停止（有料には移らない）。`services/free_team_defaults.py:102-107` |
| 履歴要約 | 専用キーなし | なし | — | Main agent の model を使う（`llm/manager_parts/memory_integration.py:321-360`） |
| コンテキスト圧縮 | `context_compression.*`（閾値だけでモデル指定なし） | なし | — | Main を使う |
| 執筆（writing） | `model_routing.classes.writing.*` | **なし**（ALLOWED_SETTINGS にも無い） | — | **未使用**: migration で補完するだけ（`app_config_store.py:113-131`）で、runtime の読み手は無い |
| 画像認識（vision） | `model_routing.classes.vision.*`、`model_routing.media.image_mode` | Routing › 画像認識 | image 対応モデルを持つ provider | inherit なら Main。auto のとき Main が画像非対応なら送らない（`chat_message_mixin.py:774-800`） |
| 音声認識（audio） | `model_routing.classes.audio.*`、`speech_recognition.current_engine`、`speech_recognition.engines.<e>.model` | Routing › 音声認識（STT エンジンは表示だけ） | audio 対応 provider ＋ ローカルSTT | engine が speech_recognition ならローカルSTT、llm なら inherit か専用ルート |
| 動画理解 | `model_routing.classes.video.*` ＋ `mage_vl.*`（重複） | Routing › 動画認識 | mage_vl 固定 | video_mode=off なら使わない |
| クリップ取り込み | `model_routing.classes.clip_ingest.*` | Routing › クリップ取り込み | AGENT_TEAM_PROVIDERS（claude/grok を除く） | inherit なら request client、次に Main。専用 factory が失敗したとき `_aoitalk_clip_ingest_fallback` で Main に戻る（`docs_ingest_service.py:512-540`） |
| Project Automation | `model_routing.classes.project_automation.*` | Routing › Project Automation | TARGET_CLIENT_PROVIDERS | 既定は inherit（Main）。`_resolve_mode`（`project_automation_model.py:176-201`） |
| Cloud Advisor | `cloud_advisor.provider`、`.model`、`.reasoning_effort` | Cloud Advisor | openai / deepinfra / chatgpt-web | model が空欄なら provider の既定 |
| Director | `agent_team.orchestration_mode=director`、`chatgpt_web.*` | Execution Team › Director | Web版ChatGPT のみ | OFF なら standard（AoiTalk 自身が実行） |
| Subagent（docs_operator、general_researcher、employee_operator、general_worker、project_operator、workspace_operator、code_explorer、architecture_planner、code_implementer、code_reviewer、personal_private_adapter、ui_qa_worker） | `agent_team.teams.<tid>.execution_profiles.<pid>.default_route` と `.overrides.<sid>` | Execution Team › Teams › Execution Profiles | カタログ provider から routing_profile と JEV を除き、`muse-remote` を追加 | Team と profile はセッション選択（`mode=fixed` ＋ `execution_profile_id`）で決まる。未選択・Team無効・非メンバー・profile無効なら Main を継承（effort same）（`agent_team_v3.py:1185-1300`） |
| Subagent（無料Team 経由） | `routing_profiles.free-team.llm_profiles.<id>`（target_type inherit/static/pool、pool_id、effort_policy） | Privacy › 無料Team › LLM Profile | pool | `specialist_delegate.py:745-760` で重ねる。ただし v3 route は `llm_profile_id`/`profile_id` を削除するため（`agent_team_v3.py:1280-1284`）**到達しにくい（要確認）**。**重複**: Team execution_profiles と同じ「Subagent → モデル」を別の仕組みで持っている |
| 外部送信マスキング | `external_model_privacy.local_provider`、`.local_model` | Privacy › 外部送信 | ローカル provider（ollama/sglang/openai_compatible_local）のモデル | 空欄なら provider 設定から自動選択（`native_runtime.py:2514-2553`） |
| Web検索 | `search.provider`、`search.openai_model` | ナレッジ・検索 › 検索プロバイダ | schema の値 | `tools/basic/web_search.py:62` |
| TTS（キャラクター別） | キャラクターの `voice_engine`、`voice_id`、`voice_parameters.model`（`POST/PUT /characters/manage[/{id}]`） | キャラクター › 音声（GeminiVoiceSettings） | `GET /tts/gemini/catalog` | 未指定なら `tts_settings` の Gemini model（`tts/engines/gemini_tts_engine.py:541,595`） |
| 画像生成 | **キー無し**（`gemini-3-pro-image-preview` を直書き） | なし。ComfyUI は URL と default_workflow だけ（`/api/comfyui/config`、`api/comfyui_routes.py:165-178`） | — | `tools/image_generation.py:200` |
| 動画生成 | なし（見つからなかった） | — | — | — |
| Realtime音声と文字起こし | `voice_sessions.realtime.model`、`.input_transcription.model` | なし | — | `api/routes/live_voice_routes.py` |
| その他 UI が無いもの | `keyword_detection.llm_model`、`discord.vision_model`、`agent_harness.codex.model`、`agent_harness.claude.model`、`crowdworks.evaluation_fallback.model`（Operations UI 側） | なし | — | 各 service が直接読む（`bot/modes/discord_mode.py:1988` など） |
| 旧 global Execution Profile | `execution_profiles.*` ＋ active id（`/api/llm/execution-profiles`、`llm_routes.py:4150-4163`） | チャット画面（`chat-llm-settings.ts:203,452`） | — | **legacy**: 「Main を上書きしない」と明記（`execution_profile_service.py:1-4`） |

---
## 4. Teams / Subagents / Director のデータモデル

**保存形式**（`config_defaults.py:319~`、型は `llm-model-section-types.ts:1340-1460`、正規化は `agent_team_v3.py:836 normalize_agent_team_v3`）:
```
agent_team:
  schema_version: 3
  delegation_enabled: bool
  orchestration_mode: standard|director
  preset_seed_version: int            # 組み込み profile の seed marker（agent_team_v3.py:185-186）
  teams:
    <team_id>: {team_id,name,description,enabled,sort_order,
                activation:{mode: always|contextual|manual, contexts:[app_development]},
                subagent_ids:[<sid>...],
                execution_profiles:{ <pid>: {profile_id,name,enabled,
                    default_route:{inherit_model,provider,model,effort_policy,effort},
                    overrides:{ <sid>: <ExecutionRoute> } } } }
  subagents:
    <sid>: {subagent_id,name,description,instructions,enabled,capability_ids[],
            scalable,default_instances,max_instances,max_workspace_access:none|read|write,
            allow_cli_native_tools}
```
- 既定の Team（`agent_team_v3.py:158-171`）: general（always。general_worker、general_researcher、docs_operator、project_operator、workspace_operator）、app_development（contextual。code_explorer、architecture_planner、code_implementer、code_reviewer、employee_operator）、employee（manual。employee_operator）、personal_private（manual。personal_private_adapter）。ui_qa_worker はどの Team にも属さない（opt-in）。
- 既定の Subagent カタログは `agent_team_v3.py:120-156`。組み込み Execution Profile の preset は `:205-345`（general-balanced/api-luna/local/quality、dev-codex-local/claude-local/codex-luna/api-luna/local-only/premium。例: `codex-cli gpt-5.6-sol high`、`claude-cli claude-opus-5-5 high`、`openai_compatible_local bonsai-2-27b-ternary default`）。
- 旧形式の `AGENT_TEAM_DEFAULT_LLM_PROFILES`（heavy/light/coding）は内部 migration 専用（`:173-177`）。

**所属チェックボックスと config の対応**: Team 詳細の Subagent チェック（`routing.tsx:520`）は `teams.<tid>.subagent_ids` に直接追加・削除する。外したときは、その Team の全 execution_profile の `overrides.<sid>` も削除する（`routing.tsx:316-330`）。同じ Subagent が複数 Team に所属できる（ID の参照だけを持つ）。Subagent を削除すると全 Team から外れる（`deleteSubagent`）。

**runtime での利用**:
- 経路の解決: `resolve_agent_team_v3_route`（`agent_team_v3.py:1258`）と `resolve_team_execution_route`（`:1210`）。呼び出し元は `llm/specialist_delegate.py:720`、`llm/runtime_tool_registry.py:2381`、`assistant/modes/agent_run_events.py:205,274`、`api/routes/config_routes.py:1097`（GET の preview）。
- Team/profile の選択はセッション側にある（`session_llm_runtime_context.session_agent_team_selection()` が `mode=fixed` で、`session_execution_profile_id()` がある場合だけ）。Settings 画面には「どの profile を使うか」を選ぶ UI が無い。
- 有効/無効とスコープ: `agent_team_v3_delegation_enabled`（`:903`）、`agent_team_scope_active`（`:1474` → `director_controller.py:296`）。
- Director: `agent_team_service.agent_team_orchestration_mode`（`agent_team_service.py:802`）。
- `services/execution_profile_service.py` には Main route の解決（`resolve_execution_main_route :335`）、Team profile の検証（`validate_team_execution_profile :581`）、旧 global EP の互換処理がある。

**電話受付（phone）の残り文言**: settings の UI（`components/settings/*`、`app/(app)/settings/*`）、`agent_team_v3.py`、`agent_team_service.py`、`agent_team_v2_migration.py`、`execution_profile_service.py`、`config_defaults.py` を検索して **該当なし**。リポジトリ全体でも `components/operations/agents/employee-management.test.tsx:419` の否定アサーション（`queryByText(/…電話受付…/)` が存在しないことを確認するもの）だけ。

---
## 5. 無料Team パネル（free-team-settings-panel.tsx）

| 表示・操作 (line) | API | データの出所 |
|---|---|---|
| 初回読込 `:136-137` | `GET /api/free-team/settings`（profile、credentials、candidates、`free_team_routes.py:509`）＋ `GET /api/free-team/usage`（quota_pools、verification、`:520`） | profile は config の `routing_profiles.free-team`、残りは DB（`memory/models/free_team.py:74 CredentialProfile`、`:139 QuotaPool`、`:262 CandidateModel`、`:341 Reservation`） |
| 無料Teamを有効化 / メインプール / 最大フォールバック回数 / 超過時の動作「停止（有料移行なし）」 `:299-309` | 「ルーティングを保存」で `PUT /api/free-team/settings`（`:216`、BE `:525` → `routing_profiles.free-team` に保存、`:640`前後） | config |
| OpenAI無料枠の確認状態と停止理由 `:311-316` | usage の `verification.status`、`.stop_reason`（ラベル `:90-120`） | free_team_service |
| OpenRouter 残量観測 `openrouter-quota-observation.tsx` | `GET /api/free-team/openrouter/quota`、`POST .../quota/refresh`（`free_team_routes.py:1117,1121`） | local {configured_limit, safety_margin, effective_cap, used, reserved}、provider_observed {status, used, limit, remaining, observed_at} |
| OpenAI 確認パネル `openai-confirmation-panel.tsx` | `GET/PUT/DELETE /api/free-team/openai-confirmation`（`openai_confirmation_routes.py:110-133`） | |
| LLM Profile overlay `:326-345` | profile の PUT に含める | `routing_profiles.free-team.llm_profiles` |
| CredentialProfile（有効化、APIキー） `:347-358` | `PUT /api/free-team/credentials/{id}`（`:156`） | 各 credential の `billing_mode`: complimentary（openai）/ free_tier（gemini、openrouter）/ promo_credit（gemini）/ subscription_cli（codex-cli、antigravity-cli、grok-cli）（`free_team_defaults.py:18-80`） |
| 候補モデル（有効、優先度、max_output_tokens、cooldown） `:360-371` | `PATCH /api/free-team/candidates/{id}`（`:174`） | DB |
| QuotaPool（残量 `available`、limit、consumed、reserved、実効/設定の日次cap、安全率・安全量、外部観測差分、stop_reason、window_end、last_provider_sync_at） `:374-398` | `PATCH /api/free-team/quota-pools/{id}`（`:195`） | DB |
| OpenRouter更新 `:238` | `POST /api/free-team/openrouter/refresh`（`free_team_routes.py:927`） | |
| （パネル外） | `POST /api/free-team/usage/sync`（`:1129`）、`GET /api/free-team/ledger`（`:1163`） | 台帳 |

「無料枠残量 / 課金経路」の表示に転用する場合:
- 残量: `GET /free-team/usage` の `quota_pools[].available`、`effective_daily_cap`、`stop_reason`、`model_group`、`verification`。
- 課金経路: `GET /free-team/settings` の `credentials[].billing_mode` と `candidates[].provider/model`。
- OpenRouter の実測値: `/free-team/openrouter/quota` の `provider_observed.remaining`。
- `quotaOnly` prop を使えば QuotaPool と verification だけを表示できる（Base の日次予算ダイアログで実際にそうしている）。

---
## 重複・未使用のまとめ
- **重複（同じ値を2キーに保存）**: `llm_model` と `<provider>.model`。`model_routing.classes.video.model/base_url` と `mage_vl.model/base_url`。vision/audio/clip_ingest の `reasoning_effort` と `.mode`。
- **重複（同じキーを2か所で編集）**: QuotaPool（Base の日次予算ダイアログと Privacy の無料Team）。`agent_team.delegation_enabled` と `orchestration_mode`（PUT の文書全体と PATCH キー）。画像認識・音声認識（Routing タブと `llm-model-groups-panel.tsx:72-230` の非表示UI）。
- **意味の重複（Subagent → モデル）**: Team の `execution_profiles` と `routing_profiles.free-team.llm_profiles`、さらに legacy の global `execution_profiles`。
- **未使用・デッド**: `model_routing.classes.{vision,audio,video}.mode`、`model_routing.classes.writing`（UI も読み手も無い）、`llm_provider_visibility.hidden_provider_ids`（BE runtime では未使用で FE 表示専用）、非表示の重複UI。
- **キー名の衝突**: `project_automation.mode` は inherit/dedicated を表すが、他 class の `.mode` は effort を表す。
- **テストだけが読むキー**: 明確に該当するものは見つからなかった（上記の未使用キーは、テストでも設定・保存の検証に使われる程度）。

---

# Part 2. backend の実行経路とデータ
---

## 1. データ（agents / agent_revisions）

### 1.1 スキーマ（`src/memory/models/agent_identity.py`）
- `agents`（270-305）: id, display_name, slug(unique), state(`draft|active|paused|retired`), character_id, thumbnail_artifact_id, create_hash, idempotency_key, created_by, created_at, updated_at。**model/tool/routing の列は無い**。
- `agent_revisions`（360-385。不変、AgentRun が pin する）:
  - id, agent_id, version, display_name, mission, responsibility_summary, operational_instructions
  - **agent_team_id** varchar(100) NOT NULL — app config `agent_team.teams.<id>` か builtin `AGENT_TEAM_DEFAULT_TEAMS` を指す
  - **execution_profile_id** varchar(100) NOT NULL — 指定先は Team 内 `execution_profiles.<id>`、または system の `manual`/`free-team`、または legacy の `heavy|light|coding`
  - **allowed_subagent_ids** JSON list
  - **capability_ceiling**（JSON list。capability id の上限。例 workspace_read, workspace_write, command_execute, aoi_tools, web_read, media, external_action_propose, remote_agent_delegate, personal_deadline_*）
  - wake_policy（JSON）, **budget_policy**（JSON）, **concurrency_policy**（JSON。max_parallel_runs/max_parallel_tasks/queue_class）
  - content_hash, idempotency_key, created_by, created_at
  - **provider / model / effort の列は無い**。model は `execution_profile_id` を経由して app config から毎回解決している。
- ほかに pin をコピーして持つテーブル: `conversation_agent_bindings`（agent_revision_id, agent_team_id, execution_profile_id, allowed_subagent_ids, route_snapshot）、`agent_runs`（agent_revision_id, acting_subagent_id。resolved_execution_manifest.route_snapshot を持つ）、`agent_work_items.agent_revision_id`。
- revision 作成時の検証は `src/services/agent_identity_service.py:1300 _validate_team_refs`。team が既知か、profile が team 内にあるか system/legacy か、subagent が enabled かを確認する。呼び出し元は create_revision（1516）と create_agent（1350/1499）。route 警告は 855。
- 件数: agents 41（active 33 / paused 4 / retired 4）、revisions 79、agent_runs 2914、agent_work_items 1783、conversation_agent_bindings 731、agent_dialogues 11、agent_delegations 16。

### 1.2 実データ: 各 agent の最新 revision を (team, profile) でまとめたもの
profile の右に書いた「→」は、app config 上でその profile の default_route が指す先。

| team / execution_profile（→ route） | Agent（state, ver） | allowed_subagent_ids | capability_ceiling / その他 |
|---|---|---|---|
| app_development / crowdworks-codex-61（→ codex-cli gpt-6.1-sol xhigh。override: code_explorer, code_implementer は high） | Web制作担当(active v2), アプリ開発担当(active v4) | code_explorer, architecture_planner, code_implementer, code_reviewer | ws_read/ws_write/command_execute。アプリ開発担当だけ +remote_agent_delegate、conc 5/5。両者とも queue_class=client_development |
| app_development / manual（→ Main） | 品質review担当(active v2) | code_reviewer, architecture_planner, employee_operator ※ | ws_read |
| general / cli-consultation-claude-opus55-high（→ claude-cli claude-opus-5-5） | 設計相談(active v2) | general_worker | ws_read、conc 1/1 |
| general / cli-development-agy-gemini-38-flash-high（→ antigravity-cli） | CLI Development antigravity-cli R3(active v3。slug 側は "...flash-medium"), 同 R1(retired) | workspace_operator | ws r/w, cmd |
| general / cli-development-claude-sonnet-low（→ claude-cli sonnet） | CLI Development claude-cli R2(active) | workspace_operator | ws r/w, cmd |
| general / cli-development-codex-luna-max（→ codex-cli gpt-6-luna） | CLI Development codex-cli R2(active) | workspace_operator | ws r/w, cmd |
| general / cli-development-grok-47-build-fast（→ grok-cli grok-4.7-build-fast） | CLI Development grok-cli R2(active) | workspace_operator | ws r/w, cmd |
| general / ep_muy1vark「社員判断_API_Sol_high」（→ openai gpt-5.6-sol） | OFFBY1, OUTDATED™, UN//REAL, どうせ嘘だろうが, よぱにしゃっど！(v2 +ws_read), 元気が2番！, 刺されても死なない…, 鬱戦 の各ブランド担当, SNS共有調査担当, SNS共有分析担当, 案件調査・評価担当（以上11件 active）, QA Operations Parallel 1/2（paused） | general_researcher, general_worker | 大半が [] |
| general / ep_muy1x1l1「社員作業_API_Luna_max」（→ openai gpt-5.6-luna） | SNS共有運用担当, ライター担当, 軽作業担当(+workspace_operator)（active）, QA Core prod C worker(paused v13) | general_worker | QA Core のみ aoi_tools, ws_read |
| general / gateway-strata-local（→ openrouter deepseek-v4.1-flash-1017） | Gateway Strata(active v2), Session A Bounded Acceptance…(active), QA Gateway A4 ×3(retired) | general_worker (+workspace_operator) | ws_read。または r/w/cmd |
| general / gateway-strata-qwen-local（→ openai_compatible_local qwen3.8-flash-next-iq3_xxs） | Gateway Strata Qwen(active) | general_worker, workspace_operator | ws r/w, cmd |
| general / manual（→ Main） | **AoiDots ×3**（active。ユーザーごとに1件。v4,v1,v1） | general_researcher, general_worker | aoi_tools, web_read（v4 は +ws_read）、conc 1/1 |
| operations_review / ep_muy1z0wy「社員統括_review_API_Sol_high」（→ openai gpt-5.6-sol） | CrowdWorks担当(active v5), Media Director / 統括(active v2), SNS共有品質review担当(active v2) | architecture_planner, code_reviewer, employee_operator | CrowdWorks: external_action_propose, budget {"currency":"JPY"}, wake interval 1800s・max 48/day, queue marketplace。他2件は ws_read |
| personal_private / manual | 個人期限台帳(active) | personal_private_adapter | personal_deadline_read/draft（team は builtin のみ。config には無い） |
| qa_lfm25_acceptance_20261015 / qa-lfm25-acceptance（→ openai_compatible_local lfm2.5-24b-a2b-q6_k） | LFM2.5 Q6_K Isolated QA Reader(paused v3) | general_researcher | ws_read |
| team_muwr2gkg「MediaOps」 / **free-team**（→ routing-profile free-team） | SNS共有制作担当(active v2) | media_operator | media |

- wake_policy は CrowdWorks担当（interval）以外すべて manual か {}。budget_policy は CrowdWorks の {"currency":"JPY"} 以外すべて {}。**revision の budget に token / cost の上限はまだ入っていない**。
- ※ config の app_development team の subagent_ids に employee_operator が無い（builtin default にはある）。このため 品質review担当 の employee_operator は route 不可になる。
- 現在の revision からは参照されていない profile: ep_mtl7qqmb（claude-cli opus。override general_worker→ollama gpt-oss:20b）、cli-development-agy-gemini-38-flash-medium、quality-local、appdev-muse-remote（provider muse-remote）。
- `conversation_agent_bindings` の (team, profile) 分布: general/cli-development-claude-sonnet-low 223, general/gateway-strata-local 181, app_development/crowdworks-codex-61 95, general/cli-development-codex-luna-max 71, general/manual 42, general/gateway-strata-qwen-local 37, general/cli-development-agy-…-medium 29, qa-lfm25 24, cli-consultation 11, ep_muy1vark 7, ep_muy1x1l1 6, agy-high 4, personal_private/manual 1。
- `agent_runs.run_type`: chat_turn 1116, autonomous_work 948, cli_session_turn 683, remote_agent_turn 101（Muse）, agent_team_delegate 60, director_operator 4, operations_codex_parent 2。acting_subagent_id は general_worker 206, code_implementer 9, workspace_operator 2、それ以外は NULL。

---

## 2. Teams / Subagents / ExecutionProfiles / Director の保存場所と解決チェーン

### 2.1 保存先
- **DB の `app_config_settings` テーブル**。key=`global` の行（JSON、約66KB）に保存される。秘密値の leaf だけ AES 暗号化（aad_prefix `app_config_settings.value`）。
  - load: `src/app_config_store.py:648 load_app_config_sync`（migrate と default 埋めを DB に書き戻す）
  - snapshot: `app_config_store.py:749`（job 実行時用。書き戻さない）
  - save: `app_config_store.py:841`、key 単位の更新: 908/914
  - 旧 `config.yaml` は初回 seed（`_load_legacy_yaml`, 134）にしか使わない
  - migration: `_migrate_agent_team_v2_config`（592）。中身は `agent_team_v2_migration.migrate_agent_team_config`。`orchestration_mode` の正規化は 618
  - プリセットの seed: `config_defaults.py:1150` → `agent_team_v3.seed_agent_team_execution_profile_presets`（641）
- 既定値: `src/config_defaults.py`
  - `agent_team:`（319-）: schema_version 3, orchestration_mode, delegation_enabled, teams.general / app_development, subagents ×10
  - `routing_profiles.free-team`（528-534）
  - `model_routing`（255-）
- 実 DB 値:
  - `agent_team`: orchestration_mode=`standard`, **delegation_enabled=true**
  - teams: general(activation manual), app_development(contextual:app_development), story(contextual), team_msyreqxa「Local_General」(disabled), operations_review(manual), team_muwr2gkg「MediaOps」(always), qa_lfm25…(manual)
  - subagents 13: 既定10 + media_operator, story_import, story_writer。code_implementer に remote_agent_delegate が追加されている
  - ExecutionProfile は **Team ごと**の `agent_team.teams.<team>.execution_profiles.<pid> = {profile_id,name,enabled,default_route{inherit_model,provider,model,effort_policy,effort},overrides{<subagent_id>: route}}`
  - トップレベルの `execution_profiles = {profiles:{}, active_profile_id: manual}` と `llm.active_execution_profile_id = manual` は旧来の名残（`execution_profile_service.py:234 apply_main_execution_profile` は no-op）
- builtin（config に無くても有効なもの。`src/services/agent_team_v3.py`）:
  - capability catalog 26-74（21種）
  - subagent catalog 91-156（ui_qa_worker, personal_private_adapter を含む）
  - `AGENT_TEAM_DEFAULT_TEAMS` 158-168（personal_private, employee, general, app_development）
  - legacy LLM profile heavy/light/coding 174-
  - route 可能な provider 358（`_AGENT_TEAM_ROUTE_PROVIDERS`）
  - system profile `manual` / `free-team`: `execution_profile_service.py:108 default_execution_profiles`
- 設定 API:
  - `GET/PUT /api/agent-team/config`（`src/api/routes/config_routes.py:1071/1128`）
  - `GET/PUT /api/llm/execution-profiles`、`POST …/{id}/activate`（`src/api/routes/llm_routes.py:4150-4163`）
  - UI: `frontend/src/components/settings/llm-agent-team-routing.tsx`, `llm-agent-team-execution-route.tsx`, `agent-team-topology-preview.tsx`
- その他、model を持つ global key:
  - Main: `llm_provider`=openrouter, `llm_model`=deepseek/deepseek-v4.1-flash-1017
  - `llm_runtime_mode`, `llm_selection_kind`=static, `routing_profile_id`=""（free-team は未選択）
  - `agents.provider/model`（gemini-3-flash-preview。旧 domain agent 用）
  - `model_delegation` / `cloud_delegation`（gpt-4o。旧）

### 2.2 Agent work item 開始 → provider/model 決定
1. 登録: `src/services/ai_employee_platform.py:55 register_ai_employee_work`。AutomationRule / ExternalAction / CrowdWorks / Delegation の各 source と adapter を共通 coordinator に登録する（delegation は `agent_delegation_runtime.py:715`）。
2. claim → `src/services/agent_work_runtime.py:4897 create_agent_run`。work item の revision を読み、`AgentRoutingPins.from_revision`（`agent_model_routing.py:135-186`。agent_team_id / execution_profile_id / allowed_subagent_ids を読む）を作る。続けて **`build_agent_routing_snapshot`（`agent_model_routing.py:985`）で全 work role 分の route を Run 開始時に凍結**し、run の manifest.route_snapshot に保存する（4933-4940）。旧 run に snapshot が無ければ `legacy_route_snapshot_required` で fail-closed。
3. 各 role の解決（`src/services/agent_model_routing.py`）:
   - `OPERATIONS_WORK_ROLES`（88）: role ごとに Subagent の優先順を持つ。planner, explorer, researcher, writer, data_worker, code_worker, general_worker, reviewer, operator
   - `select_role_subagent`（492）: Team のメンバーで、かつ revision の allowed_subagent_ids に含まれ、enabled な最初の候補を選ぶ
   - `resolve_agent_subagent_route`（409）→ `_route_spec`（294）。manual なら Main、free-team なら `{routing-profile, free-team}`。Team profile なら overrides[sid] があればそれ、無ければ default_route。legacy heavy/light は Main を継承
   - `agent_team_v3._apply_execution_route`（1153）/ `_profile_route`（1072）: inherit/static と effort_policy（same/lower/explicit/default）を適用する
   - Main は `execution_profile_service.resolve_execution_main_route`（335）→ `agent_team_service._main_route`（174。`llm_provider`/`llm_model`、無ければ `<provider>.model`、retired なら canonical）。Operations work は session の Main override を継承しない（`agent_model_routing._main_route` 279）
   - 実行ごとの profile 切替: `resolve_agent_execution_selection`（626）。同じ Team の別 profile を explicit に選べる
4. adapter 側で pins と snapshot を再ロードする（`load_pinned_agent_pins` 1124）。例: `agent_delegation_runtime.py:564-567`, `marketplace/client_development.py:3141`, `crowdworks_work_runtime.py:340`, `local_agent_task_adapter.py:67`, `agent_task_invocation.py:206`。そこから `resolve_agent_role_route(...)` → `src/llm/manager.py:743 create_llm_client_for_target(provider, model, effort)`（例: `agent_delegation_runtime.py:55`, `client_development.py:2538`）。
5. Agent に bind された Chat の turn（run_type chat_turn）の場合:
   - `src/services/chat_execution_adapter.py:1872 resolve_chat_agent_execution`。role を順に試す: worker なら general_worker が先頭（2040-2085）、そうでなければ operator → general_worker → researcher → …
   - `build_agent_chat_turn_client`（2382）→ `create_llm_client_for_target`（2532）
   - binding の作成: `conversation_agent_service.py:278-305`（bind 時に route_snapshot を作る）
6. classifier（AutomationRule の意味判定）は別系統。`src/services/agent_automation_invoker.py:102 resolve_route(config, team_id, profile_id)` が profile の default_route（manual なら Main）を使う。free-team は拒否（`semantic_free_team_unsupported`）。evaluate 内で team / profile / main-override を ContextVar に bind する（445-460）。

### 2.3 普通の Chat メッセージ → model 決定
- 起動時の常駐 client: `manager.py:1038 create_llm_client(config)`。`llm_provider` / `llm_model`（= Main。"llm.main" というキーは無い）を使う。
- turn ごと:
  - `src/services/session_llm_runtime.py:123 bind_session_llm_runtime` が session settings から ContextVar を bind する。対象は `agent_team_selection`（auto / fixed + team_id + loaded_team_ids）、`execution_profile_id`（team が fixed のときだけ）、Main override（`special_routing` / model picker）
  - `session_llm_generation.py:538-551`: session の route が常駐 client と違えば `build_session_turn_client`（session_llm_runtime.py:522）→ `resolve_execution_main_route` → `create_llm_client_for_target`
- `src/assistant/modes/terminal_mode.py`
  - 2029: 会話に Agent binding（durable AgentRun）がある場合は `resolve_chat_agent_execution` → 上の 2.2-5 の経路。`orchestration_mode` は強制で standard（3381-3385）
  - binding が無い場合、`agent_team_orchestration_mode`（`agent_team_service.py:802`）が `director` なら **Director = Web版 ChatGPT**（`src/llm/director_controller.py`。ChatGPTWebProvider。返答中の「【AoiTalkへの作業依頼】」marker で operator run_type `director_operator` を起動）（3515-3736）
  - 現在の設定は standard なので Director は不使用。runs は 4 件だけ
- standard の場合: Chat の LLM = Main（または session override）。Subagent は tool `agent_team_delegate` 経由で使う（§3）。Team を fixed + execution_profile_id にした場合だけ、`agent_team_v3._session_uses_team_execution_profile`（1185）→ `resolve_team_execution_route`（1210）で Subagent の route がその Team profile に従う。それ以外は `resolve_agent_team_v3_route`（1258）で Main を継承する。
- 特殊な Chat provider: `muse-remote` + model `muse-chat`（manager.py:758 → muse_chat_service）、`grok-bot`（manager.py:755 → grok_bot_chat_service）、`routing-profile`/`free-team`（free_team_client）。

---

## 3. Director / Team / Subagent の実行時挙動と外部 worker

### 3.1 Subagent の起動（Chat root 側の tool）
- tool **`agent_team_delegate(team, subagent, task, instances=1, work_mode=read|write, scopes[])`**: `src/llm/runtime_tool_registry.py:2085-2140`。
  - 登録条件: `agent_team.delegation_enabled` かつ、active / manual / contextual な Team がある（3514-3516 付近）
  - 説明文の roster は Team / Subagent のグラフから動的に作る（`_agent_team_delegate_roster` 1908）
  - 入れ子禁止: child は `get_current_agent_team_role()` が立っていると拒否される（2160）
  - Agent bind 済み turn の制限（2363-2380）: `agent_execution_binding.agent_team_id` と一致しない Team →「outside the pinned AgentRevision」。`allowed_subagent_ids` に無い Subagent → 拒否。route は `resolve_agent_subagent_route(require_allowed=True)` で決め、`_config_with_pinned_subagent_route`（1987）で child 用 config に overlay する
  - 非 bind turn: `resolve_agent_team_v3_route`。Team の active 判定は `agent_team_scope_active`（agent_team_v3.py:1474）
  - capability: `filter_agent_team_capabilities`（agent_team_v3.py:1395）。CLI backend は `allow_cli_native_tools` が必要（`resolve_agent_execution_backend` 1317）
  - 実行: `src/llm/specialist_delegate.py:3994 AgentTeamSubagentDelegationRunner`（基底 `SpecialistDelegationRunner` 673。client 生成 1211 `create_llm_client_for_target`）。child AgentRun の run_type は `agent_team_delegate`
- tool `load_agent_team(team_id)`（runtime_tool_registry.py:560）: manual Team を session に読み込む。
- tool `media_assistant`（3567, `_register_delegation_tool` 1843 → `MediaDelegationRunner` specialist_delegate.py:4108）: Media 用の固定 delegate。
- tool `consult_cloud_advisor`（3305）: 別 model への相談（§5）。

### 3.2 Agent → Agent（DB 永続）
- **AgentDialogue（AoiDots root から worker Agent へ）**: `src/tools/agent_dialogue.py:254-354`
  - tool 群: `list_agent_workers`, **`delegate_agent_work(title, instruction, agent_id, project_id, task_id, budgets{max_turns,max_depth,max_attempts,max_tokens,max_cost_micros,cost_currency}, deadline, predecessor_dialogue_id)`**, `get_agent_work_status`, `send_agent_work_message`, `answer_agent_work_question`, `escalate_agent_work_question`, `stop_agent_work`, `resume_agent_work`
  - 登録: `runtime_tool_registry.py:3363 _register_agent_dialogue_tools`（常に登録）
  - backend: `services/agent_dialogue_service.py`。worker の route は `resolve_agent_role_route(..., "general_worker")`（1323-1366）
  - テーブル: agent_dialogues ほか（usage, budget_roots, reservations, deliveries, inputs）。CLI provider で max_tokens / cost の上限がある場合は拒否（`chat_execution_adapter.py:2382-` AgentDialogueBudgetUnsupported）
- **AgentDelegation（社員間の発注。work → review → return）**:
  - API: `POST /api/...delegations`（`src/api/agent_delegation_routes.py:205`）。**tool からは呼べない**
  - service: `services/agent_delegation_service.py:18-20`。PHASES=(work, review, return)、WORK_ROLES=(researcher, writer, data_worker, general_worker, code_worker)
  - runtime: `agent_delegation_runtime.py:546` adapter `employee_delegation`。phase role の route は 564-567、reviewer は 407
  - 実データ 16 行: Media Director→よぱにしゃっど！(data_worker, reviewer SNS共有品質review) 6件（完了2）、Web制作担当→アプリ開発担当(code_worker, reviewer 品質review担当) 10件（完了0、cancel 1）。すべて 2026-10-08、round 1。

### 3.3 外部 worker の現状（tool ではなく「provider」として扱われている）
- **codex-cli / claude-cli / antigravity-cli / grok-cli**:
  - LLM provider id（ROUTABLE_PROVIDERS `agent_model_routing.py:58`）。execution profile の default_route / override に `provider: codex-cli` のように書く。backend 判定は `provider.endswith("-cli")` → `"cli"`（agent_team_v3.py:1151）
  - 実体は `src/llm/cli_llm_client.py`（CLILLMClient）。Main にも Subagent にも free-team candidate にもなり得る
  - 別レーンとして「CLI Development」session（`services/cli_development_sessions.py` + `cli_session_work_adapter.py:169 CLISessionWorkAdapter`, register 2161、run_type cli_session_turn 683件）。専用 Agent（slug cli-development-*）に bind し、Agent の profile が CLI を指す
- **Muse**: provider `muse-remote`（model なし、backend remote。`agent_team_v3._apply_execution_route` 1159-1167）。capability は `remote_agent_delegate`（`muse_remote_execution.py:72`）。
  - 実行: `cli_session_work_adapter.py:1316 MuseRemoteTurnWorkAdapter` / `register_muse_remote_session_work` 2173 → `muse_remote_execution.py:574 MuseRemoteClient`, 850 Adapter（muse-cli 0.3.2）。run_type remote_agent_turn 101件
  - profile: app_development/appdev-muse-remote
  - Chat 用には別経路で `muse-remote`+`muse-chat`
- **Codex（Operations の code_worker）**: `services/codex_development_runtime.py`（run_type operations_codex_parent）。`agent_harness/runner.py`（WSL bwrap 上の codex）
- **local LLM の agent task**: `services/local_agent_tasks.py`（Home MCP Gateway からの委譲。openai_compatible_local）
- **Director**: ChatGPT Web（`director_controller.py`、`chatgpt_web.*` 設定）
- まとめ: 既に「別 Agent / 外部 worker へ委譲する tool」として存在するのは `agent_team_delegate`, `load_agent_team`, `delegate_agent_work`（とその制御 tool 群）, `media_assistant`, `consult_cloud_advisor`, `browser_agent` / `browser_agent_dedicated`。codex / antigravity / grok / claude / muse を直接呼ぶ tool は無い（profile の provider としてだけ選べる）。

---

## 4. Jev ブラウザ操作の公開方法
- tool **`browser_agent(goal, start_url, tab_id, completion_text, completion_url, device_id)`**: `src/tools/browser_agent.py:6-`（定義の name は 54 付近）。owner `browser_agent`、side_effect writes、timeout 930s。
  - 処理: `pc_bridge_service.resolve_control_bridge(device_id)` → `services/browser_agent_service.py BrowserAgentService.run_goal`
  - plan は**呼び出し元の Chat client**で作る（`browser_agent_planner.build_browser_plan`。`client.generate_plain_text_async`）
  - 候補判断は `CandidateDecisionRouter`（llm_choose = 同じ client、jev = `JevDecisionService`）（browser_agent_service.py:171, 282）
- tool **`browser_agent_dedicated(..., resume_run_id)`**（browser_agent.py:137 付近）→ `services/dedicated_jev_runtime.py DedicatedJevRuntime`
- Jev 本体: `services/jev_decision_service.py:412 JevDecisionService`。remote API（typesafe.ai。649 付近）、model `jev-1.13.0`（27 `JEV_DEFAULT_MODEL`）、資格情報は env `JEV_API_KEY`、provider 名 "jev"（528/646）。Jev は free-team の candidate 順序付けにも使う（§5）。
- 設定: `browser_agent.{enabled, jev_enabled, dedicated_enabled, max_steps, timeout_seconds}`（`browser_agent_models.py:72-79 BrowserAgentSettings`、jev_model も持つ）。
- 公開範囲:
  - deferred tool pack `browser`（`src/llm/tool_packs.py:177-183`。browser_agent, browser_agent_dedicated, computer_observe, computer_use）
  - policy（`src/llm/tool_policy.py:633`, 712/755, 3025-3031）
  - 登録（runtime_tool_registry.py:3541-3545）
  - Subagent capability `browser_qa`（ui_qa_worker 用。QA browser は `specialist_delegate.py:393-449` の qa_navigate など）
- → 既に普通の tool。Agent の tool set に入れれば足りる。capability id としては `browser_qa` だけが別にある。

---

## 5. Chat 以外の model routing（key / reader / fallback）
| 用途 | config key | reader | fallback / 継承 |
|---|---|---|---|
| vision（画像） | `model_routing.classes.vision`（inherit,provider,model,base_url,api_key,reasoning_effort）+ `model_routing.media.image_mode`(auto/always/off) | `src/api/server_parts/chat_message_mixin.py:773-795`（明示 route、または Main が vision 非対応なら MediaRecognitionService に委譲）、`services/media_recognition_service.py:563-572 _class_route` | inherit か空なら Main（llm_provider/llm_model）。request の parameter fallback は `_create_chat_completion_with_fallback` 1461（provider 切替ではない）。DB値: inherit=true |
| audio | `model_routing.classes.audio`（engine=speech_recognition） | media_recognition_service（`speech_recognition.current_engine` 576） | 音声認識 engine 設定を使う |
| video | `model_routing.classes.video`（mage_vl, microsoft/Mage-VL, 127.0.0.1:30000）+ `model_routing.media.video_mode` + `mage_vl.*` | `services/mage_vl_service.py:339`、media_recognition_service.py:942 | 継承なし（inherit=false）。off なら無効 |
| clip_ingest | `model_routing.classes.clip_ingest`（DB: openai gpt-5.6-luna, effort max, inherit=false） | `services/docs_ingest_service.py:466-470 clip_ingest_route`, 520 | dedicated 優先。`allow_main_fallback` / `fallback_to_main` / `allow_fallback` があれば Main client へ（494-505、marker `_aoitalk_clip_ingest_fallback`） |
| writing | `model_routing.classes.writing` | migration だけ（`app_config_store.py:113`）。実際の reader は見つからない | inherit |
| heavy / light（classes） | `model_routing.classes.heavy/light`（DB に空の値がある） | reader は見つからない（旧 LLM profile の名残。agent_team_v3.py:174-175） | — |
| project_automation | `model_routing.classes.project_automation`（mode inherit/dedicated） | `services/project_automation_model.py:13,133,176,261 resolve_project_automation_route`、`project_steward_service.py:2218` | inherit なら Main。dedicated は検証に失敗すると ProjectAutomationRouteError（自動 fallback なし） |
| cloud_advisor | `cloud_advisor.{mode(disabled),provider(openai),model(''),reasoning_effort(high),max_consultations_per_turn}` | `services/cloud_advisor_service.py:780-840`、tool 登録 runtime_tool_registry.py:3305 | model が空なら `canonical_model_for_provider`（839）。provider 間の fallback はしない（921） |
| 要約（会話 summary） | 専用 key なし | `src/memory/manager.py:556-660 _trigger_summarization`（渡された chat の `llm_client` を使う） | = その Chat の client |
| タイトル生成 | 専用 key なし | `services/conversation_title_llm.py:37 _title_generation_client` | = Chat client |
| context_compression | `context_compression.*`（strategies、within_turn、history_compaction） | 機械的圧縮（model route の key は無い。LLM 不使用かどうかは未検証） | — |
| 画像 / 動画生成（MediaOps） | app config の model routing は**使わない**。request_spec の不透明な `model_selection_id`（`ims_` / `imd_` / `ien_` が画像、`wsl_` が動画） | `services/media_operations_generation_service.py:74 GenerationStudioAdapter`, 749-750 regex、`api/media_operations_generation_routes.py:310-313`、adapter `media_execution_adapters.py:1014`（`media.generation`）。外部 Generation Studio（env `AOITALK_GENERATION_STUDIO_BEARER_TOKEN` など。generation_studio_auth.py:191）。ComfyUI は `comfyui.*`（comfyui_service.py:395） | selection 単位。fallback なし |
| Web 検索 | `search.provider`, `search.openai_model` | `tools/basic/web_search.py:62`, `quick_search_service.py:102` | — |
| CrowdWorks 評価 | `crowdworks.evaluation_fallback.{enabled,model=gpt-5.6-luna}` | `services/marketplace/crowdworks_workflow.py:191-200, 1549` | pin された sol route が quota で拒否されたときだけ luna で再試行 |
| deep research | `llm_provider` / `<provider>.model` | `services/deep_research_service.py:600,1606-1622`（`create_llm_client(config)`） | Main |
| site analysis | 専用 role / key なし（"site_analysis" は grep で 0件） | — | — |
| **free-team** | `routing_profiles.free-team.{enabled, main_pool_id=coordinator, max_fallbacks=6, credential_profiles(7), quota_pools, candidates(9), pools(6), llm_profiles(heavy,light,coding,tool-executor), openrouter_credential_source, agent_team_enabled}` + `decision_layer.jev_free_team_routing_enabled/shadow_mode` | `services/free_team_service.py:310 free_team_profile`, 369 main_route_intent, 388 pool_route_intent, 1008 `_bounded_routing_enabled`, 1025 `_semantic_candidate_order`（Jev）。`src/llm/free_team_client.py:576`（max_fallbacks を 0-10 に clamp）, 611 retry loop, 708 `create_llm_client_for_target`, 822 打ち切り。API `free_team_routes.py:539,641` | pool ごとに candidate_ids の順（priority と quota による除外、Jev 順序）で最大 max_fallbacks 回 fallback。quota reset / cooldown 待ちは専用の例外になる |

- free-team pools（DB）:
  - coordinator（tool auto）: openai-luna, openai-sol, gemini-free-flash, openrouter-free-*, gemini-promo-flash, codex-spark, antigravity, grok-build
  - heavy（tool disabled）: openai-sol, openai-luna, gemini-free-flash, openrouter-free-*, gemini-promo-flash, codex-spark
  - light（tool required）: gemini-free-lite, gemini-free-flash, openrouter-free-*, openai-luna, codex-spark, antigravity, grok-build
  - coding（tool required）: openrouter-free-*, gemini-free-flash, codex-spark, antigravity, grok-build
  - tool-executor（tool required）: gemini-free-flash, openrouter-free-*, gemini-promo-flash, codex-spark, antigravity, grok-build
  - vision（tool required）: gemini-free-flash, gemini-promo-flash
- free-team candidates（provider / model, priority）: openai gpt-5.6-luna(10) / gpt-5.6-sol(11), gemini-2.5-flash(20) / -lite(21), openrouter/free(30), gemini-promo flash(40), codex-cli gpt-5.3-codex-spark(50), antigravity-cli default(60), grok-cli grok-build(70)。
- → 新設計の「role → model + fallbacks」に近い既存の仕組みは free-team の pool（role 別の candidate 列）だけ。ほかの class は「明示 route か、Main を継承」の 1 段で、fallback は持たない。

---

## 6. 現行の形を固定している tests
`tests/` で `agent_team_id|execution_profile_id|allowed_subagent_ids|agent_team_v3|orchestration_mode` のどれかに当たるファイルは **115 本**。

| パターン | ファイル数 | hit 数 |
|---|---|---|
| agent_team_id | 59 | 207 |
| execution_profile_id | 82 | 440 |
| allowed_subagent_ids | 52 | 169 |
| agent_team_v3 | 32 | 101 |
| orchestration_mode | 16 | 34 |
| agent_team_delegate（参考） | 21 | 92 |

5パターン合計の hit 数が多い順（上位）:
- test_execution_profile_admission_wiring 87, test_execution_profile_service 50, test_agent_task_invocation 37, test_agent_execution_selection 32
- test_agent_automation_invoker 31, test_chat_agent_execution_adapter 30, test_agent_model_routing 30, test_crowdworks_client_development 29
- test_personal_agent_controller 26, test_agent_identity_foundation 24, test_agent_private_provisioning 22, test_prepare_crowdworks_muse_remote_revision 21
- test_same_role_execution_profile_selection 18, test_crowdworks_detail_quality 18, test_crowdworks_bootstrap 18, test_agent_root_authority_guard 17
- test_aoidots_coordinator_cpu_acceptance 13, test_muse_chat_service 12, test_agent_dialogue_workspace 12, test_agent_delegation_workflow 12
- test_config_routes 11, test_character_and_agent_team_routing 11, test_agent_team_service 10, test_personal_deadline_chat_draft 9, test_agent_dialogue_service 9
- test_tool_failure_circuit_terminal 8, test_cli_development_sessions_persistence 8, test_agent_dialogue_bridge 8, test_runtime_tool_registry 7, test_runtime_prompt_terminology 7
- test_specialist_agent_team 6, test_personal_agent_session_candidates 6, test_local_agent_task_adapter_orm 6, test_docs_task_search_index_consistency 6, test_crowdworks_fulfillment_usage 6, test_agent_work_recurrence_blocking 6
- test_terminal_mode_stream_gate 5, test_muse_authorized_scope_store 5, test_conversation_agent_service 5, test_agent_work_chat_ledger 5, test_agent_team_muse_remote_route 5
- test_personal_agent_owner_identity 4, test_llm_model_catalog 4, test_enterprise_cli_scope_boundary 4, test_agent_task_provider_terminal 4
- 残り約70本は各 1-3 hit

名前から見て形そのものを固定しているファイル:
- test_agent_team_legacy_context_roundtrip, test_agent_team_muse_remote_route, test_agent_team_qa_browser, test_agent_team_service, test_agent_team_session_selection, test_agent_team_v2_runtime, test_terminal_agent_team_metadata, test_specialist_agent_team
- test_execution_profile_runtime_api, test_execution_profile_service, test_execution_profile_admission_wiring, test_same_role_execution_profile_selection
- Director 系: test_director_controller, test_chatgpt_web_director_cli, test_director_browser_env_isolation（`director_controller` を参照するのは 5 本）

frontend で agent_team_id / execution_profile_id / allowed_subagent_ids（camel 形も含む）に当たるのは 49 ファイル:
- components/chat 7, lib/__tests__ 5, components/operations 5, hooks/__tests__ 2, personal-agents 2
- lib: operations-api, conversation-agent-api, chat-llm-settings, new-chat-llm-settings-store, chat-send-outbox, chat-session-route-handoff, employee-organization-setup-api, mediaops-bootstrap（いずれも .ts）
- 生成型 `frontend/src/lib/api-types.gen.ts`

---

## 7. 近くのコードを触っている他 session
- **AoiDots run caps**（`.worktrees/aoidots-run-caps`、feature/aoidots-run-caps）: 未 commit の変更 18ファイル、+2485/-187。
  - 新規 `src/llm/agent_run_budget.py`: AgentRun id ごとの process-local ledger。既定 200,000 tokens / 24 tool calls、code `run_budget_exceeded:tokens|tool_calls`
  - 新規 config `runtime.agent_run_max_total_tokens` / `runtime.agent_run_max_tool_calls`（config_defaults）
  - 変更: `runtime_tool_registry.py`（child run budget の settle / discard）, `specialist_delegate.py`, `chat_execution_adapter.py`, `agent_task_invocation.py`, `agent_work_runtime.py`, `tools/agent_dialogue.py`, `unified_turn_runtime.py`, `native_runtime.py`, `agent_runtime.py`, 各 engine（cli / gemini / ollama / openai_compatible_local / sglang）, `agent_run_events.py`
  - 新規 test: test_agent_run_budget, test_agent_run_tool_failures, test_specialist_run_budget
  - → delegate tool（agent_team_delegate / delegate_agent_work）と chat_execution_adapter の改修とまともにぶつかる。revision の `budget_policy` に寄せる候補（現状 DB では空）
  - 関連してすでに main にある変更: AoiDots の生成出力を 8192 tokens に制限（d84ee75c7）、Root proof 後の cap（c60185005。`chat_execution_adapter.py:1339 _project_initial_aoidots_output_cap`, 1369 `bind_initial_aoidots_human_run`）
- **内蔵ブラウザ（Edge）**: main は model-assign より 9 commit 先にある。model-assign に未取り込みの commit:
  - S1 cfb71b59c: `src/services/local_browser/{broker,runtime,status}.py`、`service_manager/_browser_broker.py`、`pc_bridge/edge_broker_control.py`、`/health` の browser status
  - S2 534592a50: `local_browser/local_bridge.py` LocalBrowserBridge（RemoteControlBridge 互換の面、origin で tab を選ぶ）
  - 設計: `docs/local_browser_architecture.md`（f490c5bc5）、0cd980acc（local-first targets、Bridge は remote PC 用の optional 部品、S5/S6 計画）
  - → `browser_agent` tool は現在 `pc_bridge_service.resolve_control_bridge` を使っているので、今後 LocalBrowserBridge に差し替わる見込み。Jev / browser tool を「普通の tool」にする作業はこの系列と重なる
  - ユーザー方針: Edge への接続は edge_cdp_broker 経由だけ
- **free-team-phase2**（`.worktrees/free-team-phase2`）: main に merge 済み（d0aed0641「Free Team不明予約をUTC窓終了後に解放」。`free_team_service.py` +302）。role → model の fallback 設計は free_team_service / free_team_client と重なる。
- Muse native gateway（b2f510133、`agent_link/muse_native/gateway.py`）も main 側だけにある。
