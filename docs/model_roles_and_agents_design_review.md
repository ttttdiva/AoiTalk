# model roles / Agent 移行設計 — 反対側からのレビュー

対象: 設計書と棚卸しを、現在の checkout のコード・API・生成契約と照合した read-only レビュー。秘密値は読んでいない。実DBへの接続・件数の再集計はしていないため、41 Agents / 79 revisions 等の実データ件数は棚卸し記載を前提条件として扱う。

## 指摘事項

1. **高 — P2 の役割対応が一対一でなく、既存の経路差を1つの override に潰す**
   - 設計は `planner/researcher → research`、`writer/data_worker/general_worker/operator → chat`、`code_worker/explorer → code` と複数の旧経路を同一 role に畳む（設計:120-124）。実装は work role ごとに異なる Subagent 候補を選び、Team profile の `overrides[subagent_id]` を default より優先する（`src/services/agent_model_routing.py:89-113,294-334,492-535`）。
   - 例えば同一 Agent の `planner` と `researcher`、`code_worker` と `explorer` が別 Subagent override を引き、provider/model/effort のいずれかが違えば `model_overrides[research|code]` だけでは両方を再現できない。dry-run の「route 一致」は旧 work role ごとに比較しないと衝突を見逃す。
   - **修正:** 旧 work role の実効 route を全て展開し、新 role へ集約する際に複数値が出たら自動移行を中止する。role を細分化するか、明示的な手動解決表を出す。

2. **高 — `manual` 以外の旧 system profile と free-team の overlay / pool 設定が移行表から抜けている**
   - 設計の P2 は `manual` と固定値 `{pool: coordinator}` だけを記述し、legacy `heavy/light/coding` の扱いがない（設計:126-127）。実装ではこれらは有効な legacy profile で、Main 継承に加えて `light` は `effort_policy=lower` を適用する（`src/services/agent_model_routing.py:318-327`、`src/services/agent_team_v3.py:174-176`）。空 override へ移すと effort が変わる。
   - `free-team` の pool は `routing_profiles.free-team.main_pool_id` から解決され、既定値が常に固定される保証はない（`src/services/free_team_service.py:369-379`、`src/services/agent_team_v3.py:1304-1308`）。`{pool: coordinator}` の固定はカスタム `main_pool_id` を変更する。さらに `llm_profiles` は `free_team_service.py:325-346,388-435` と `specialist_delegate.py:740-769` で pool、effort、tool mode の解決に使われる。
   - **修正:** P2 に全 system/legacy profile の正規化表を追加し、実効 `main_pool_id` と各 `llm_profile_id` の pool/effort/tool_mode を保存する。旧 reader が残る間はその設定を消さない。

3. **高 — 新しい route 型では既存の特殊 provider と per-route 接続設定を表せない**
   - `R` は provider/model/effort または pool だけだが、既存の vision / clip_ingest / project_automation は route ごとの `base_url` と `api_key` を設定できる（設計:36-37、`src/api/routes/config_routes.py:778-785`）。新設定へ写すと接続先・資格情報が失われる。
   - `cloud_advisor` は `chatgpt-web` を有効な provider として受け、model を `web-default` に補い、専用 `ChatGPTWebCloudAdvisorAdapter` を使う（`src/api/routes/config_routes.py:645-650`、`src/services/cloud_advisor_service.py:832-837,1298-1310`）。`chatgpt_web.*` を Director 用として廃止すると、この Advisor も使えなくなる。通常Chatには `grok-bot` と `muse-remote+muse-chat` の専用clientもあり、Agent用の `muse-remote` (modelなし) とは別経路である（`src/llm/manager.py:757-763,1065-1073`）。`mage_vl` と `speech_recognition` も通常のLLM client factoryとは別の実行経路。画像生成も現状は通常toolが固定Gemini modelを使い（`src/tools/image_generation.py:198-204`）、Operations MediaOpsは `ims_/imd_/ien_/wsl_` opaque selection IDを要求する（`src/services/media_operations_generation_service.py:1098-1102`）。§5.1の「固定model → generation_studio selection」は値の型が一致せず、同じ用途の別実行経路もカバーしない。
   - **修正:** API client route、ChatGPT Web facade、Mage-VL、ローカルSTT、資格情報参照を含む route variant を定義する。secret は provider 接続 profile ID で参照し、旧 per-route secret を移すまで削除しない。Advisor の `max_consultations_per_turn` も別設定として保持する。

4. **高 — P1 の切替一覧から audio/video reader が漏れ、P3 の削除で壊れる**
   - P1 の reader 切替列挙には audio/video がない（設計:148）が、P3 は `model_routing.classes.*` 全体を廃止対象にする（設計:57）。現在 audio は `src/services/media_recognition_service.py:787-856` が `model_routing.classes.audio.engine` と `speech_recognition.*` を読み、video は `src/services/mage_vl_service.py:339` と `src/services/media_recognition_service.py:920-942` が video route と mode を読む。同じく新 role `site_analysis` の reader は存在せず、実 reader は `src/services/docs_ingest_service.py:469-540` の `clip_ingest` route。resolverを `site_analysis` に切替えても現状の clip importer は新表を読まない。
   - P3 で旧キーを除くと、再起動後に音声エンジンが既定へ戻る／off 扱いになる、Mage-VL が選択モデルや接続先を失う可能性がある。audio の実体はローカルSTT engine でもあり、空 routes を chat 継承にするだけでは同値ではない。
   - **修正:** audio/video の全 runtime reader と UI/API を先に新 schema へ切替え、既存 engine、mode、Mage-VL runtime 設定を含む round-trip を確認してから旧キーを退役する。

5. **高 — fallback の「最初の token」は副作用境界にならず、Free Team では二重 retry になる**
   - 設計は最初の token より前の timeout/接続エラーを retry 可能とする（設計:40）。tool call が最初の応答イベントになる場合、token がなくても tool が実行済みのことがあり、CLI native tools はモデル本文の token を返す前に workspace を変更し得る。
   - Free Team client は既に独自の候補 retry を持つ一方、timeout、side effect 開始、tool 実行の可能性を検出した場合は retry を停止する（`src/llm/free_team_client.py:610-635,813-824`）。外側の新 client が同じ timeout を別 route へ再試行すると、内部 policy を迂回し、課金や副作用が重複する。
   - **修正:** token ではなく provider request / tool dispatch / 外部副作用の確定状態で retry 可否を決める。Free Team の「no outer retry」情報をtyped errorとして伝播し、同一 idempotency key と attempt log を使う。streaming、tool call、CLI native execution の各境界を明示する。

6. **高 — fallback の課金種別・quota reservation・失敗 attempt の会計が未定義**
   - `billing_mode` は provider 一律の属性ではなく credential profile にあり、Free Team は complimentary/free tier/promo credit/subscription CLI 等を同じ候補列に持つ。pool の quota reservation は candidate・credential と連動する（`src/services/free_team_service.py:300-307,369-379`、`src/llm/free_team_client.py:628-635,750-756`）。provider/model だけで free/paid 境界を決める設計では、OpenRouter の無料/有料 credential や CLI subscription を誤分類し得る。
   - pre-token timeout でも provider request は受理・課金済みの場合がある。成功 route だけを親 run の cost に加えると、fallback を重ねて `max_cost_micros` を越えられる。反対に quota reservation を毎 route で独立取得すると同じ run が二重確保し得る。
   - **修正:** 各 attempt の credential、billing class、予約額、確定 usage/cost、失敗時の消費扱いを記録する。親予算・Free Team quota・subscription 利用を一つの attempt ledger で settle し、分類不能なら fallback を fail-closed にする。

7. **高 — `tool_ids` が現行 capability authority の代わりになる仕様がない**
   - 設計は `tool_ids` で `capability_ceiling` を置換し、tool 名・group と capability ID を混在させている（設計:48-50）。現行 authority guard は `Subagent.capability_ids ∩ AgentRevision.capability_ceiling ∩ catalog` を検証し、read/write mode と Subagent の write 許可も別に確認する（`src/services/agent_authority_guard.py:1083-1125`）。AgentDialogue readiness も `aoi_tools` capability を直接要求する（`src/services/agent_dialogue_service.py:1316-1328`）。
   - 単なる親子の `tool_ids` 共通部分では、親が持つ Agent/project/resource ACL、`browser_qa` の親発行 capability、`external_action_propose` の ActionPolicy、個人データ capability を再現できない。`workspace` 等の group と concrete tool 名の比較規則もない。
   - **修正:** capability ceiling と tool exposure を分離する。tool/group はサーバー側 catalog で capability・resource scope に展開し、現行 guard と owner/Project ACL を必ず通す。新字段を `AgentDialogueService`、authority guard、binding snapshot、API schema の全 reader に反映してから旧列を退役する。

8. **高 — `delegate_worker` の write/native authority と実行量を制限できない**
   - `workspace_scope` の path allowlist と `workspace_write + command_execute` の boolean だけでは、CLI/Muse process が実際に触れる filesystem・shell scope を拘束しない（設計:69-75）。現行 CLI provider は Subagent の通常の model route として `create_llm_client_for_target` から呼ばれ、その native tools は `allow_cli_native_tools` と capability/workspace access で別途制限される（`src/services/agent_team_v3.py:1264-1314,1317-1424`）。それを `delegate_worker:<kind>` だけに置換すると、AgentRevisionの指示・ACL・child run lifecycleを通る実行からCLI sessionへの外部委任へ変わる。Muse の既存 Agent capability も `remote_agent_delegate` で明示分離される（同:47-51）ため、`delegate_worker:muse` と通常Chatの `muse-chat` を混同できない。
   - 新 tool に `budgets` がなく、timeout 以外の token/cost/attempt/depth 上限もない。AgentDialogue の現行 contract には `max_depth`、`max_attempts`、`cost_currency`、`predecessor_dialogue_id` があるが設計引数から落ちている（`src/tools/agent_dialogue.py:274-288`）。許可 worker が自分自身や別 worker を再委任する場合の depth/cycle 制御もない。
   - **修正:** OS sandbox/worktree と実効 Agent scope の積集合を executor に渡し、path ごとに強制する。kind 別 capability、max depth/attempts、累積 tokens/cost、再帰 cycle、停止/再開・監査 ID を定義し、Muse native gateway も同じ制約を強制する。

9. **高 — P2 の nullable 化は旧 resolver・API・Operations UI と矛盾し、retired Agent も通常移行できない**
   - DB は現状 `agent_team_id` / `execution_profile_id` / `allowed_subagent_ids` を NOT NULL とする（`src/memory/models/agent_identity.py:372-375`）。作成 API も Team/Profile 必須（`src/api/agent_identity_routes.py:165-180`）、selection/pinning は空 Team/Profile を invalid として拒否する（`src/services/conversation_agent_service.py:1115-1120`）。`employee-role.tsx:172-220` はこれらを表示・送信する。
   - P2 で null にしてから古い resolver が読み続けるという設計（設計:53,138）は、新revisionを読む旧 adapter/rolling processで失敗する。さらに revision creation は retired Agent を拒否する（`src/services/agent_identity_service.py:1589`）。全 Agent に新 revision を作る場合、retired 4件（棚卸し:248）はサービス経由で移行できない。
   - **修正:** P2 を additive expand-only migration にし、旧readerを全置換した後に nullable/drop する。新旧 payload を段階対応させ、retired Agent は履歴専用として明示的に除外するか、監査済みの別移行経路を定義する。

10. **高 — P3 の `agent_team` 削除は次回起動時の schema migration で復活する**
    - 現在の config load は毎回 `_migrate_agent_team_v2_config` を呼ぶ（`src/app_config_store.py:708-726`）。`agent_team` が無く topology も空と判定されると `_migrate_section` が標準 Team/Subagent を再生成し（`src/services/agent_team_v2_migration.py:425-432`）、最後に canonical `agent_team` として戻す（同:448-471）。
    - よって P3 が `agent_team.*` を snapshot へ移して削除しても、移行コードを同時に改めなければ再起動時に既定 Team が DB に再保存される。UIを外しただけでは Director/Team runtime が再出現し得る。
    - **修正:** P3 前に新 app-config schema/version を設け、旧 Team migration/default seed をその version 以降では実行しないようにする。legacy snapshot を読ませる互換期間と新 runtime の開始条件を明示する。

11. **高 — P4 の system-owned Aoi は通常の非Project会話から選択できない**
    - 新規 system Agent の owner が user creator でない場合、ConversationAgentService は admin/creator 以外に Project を必須とし、Project がなければ `agent_project_grant_missing` を返す（`src/services/conversation_agent_service.py:529-545`）。Project があっても AgentProjectGrant の有効な read 権限を要求する（同:547-570）。
    - したがって「既定 Agent Aoi」を既存の通常 Chat に自動適用するだけでは、Project 未設定会話で bind/run admission が拒否される。ユーザー別コピーを作る場合も private root/owner lifecycle をどう付与するかが未定義。
    - **修正:** Aoi を user-scoped Agent として provision するか、system Agent 用の限定された共有選択・Project ACL contract を実装する。既存会話・新規会話のどちらへ適用するか、失敗時の通常Chat fallbackを明示する。

12. **高 — P3 からP4まで、通常Chatの委任経路が途切れる可能性がある**
    - P3 で Team delegate / load team / Director を撤去し、P4 で初めて通常Chatを Agent「Aoi」にする順序（設計:150-151）。P3中の unbound Chat はまだAgent turn経路ではない一方、新 `delegate` の登録対象と権限取得経路が設計されていない。
    - 既定の現在設定が standard でも Chat は `agent_team_delegate` を使う通常経路を持ち、Director は別分岐である（`src/llm/runtime_tool_registry.py:3510-3523`、`src/assistant/modes/terminal_mode.py:3381-3385`）。段階間の再起動で古い tool が消えて新 tool が使えない期間が発生する。
    - **修正:** 新しいChat/Aoi実行経路を先行して裏で稼働・検証し、旧 tool と新 tool の共存期間を設けて段階的に切り替える。Director の Cloud Advisor 利用は別 route として残す。

13. **高 — P1 の「旧キー read-only」と既存の読み書き API が一致しない**
    - `model_roles` は現行 `GET /api/settings` response に含まれず（`src/api/routes/config_routes.py:834-866,950-977`）、`PATCH /api/settings` も未登録 key を400で拒否する（同:1620-1625）。一方旧 `model_routing.*` / `cloud_advisor.*` は現状 `ALLOWED_SETTINGS` にあり、`POST /api/llm/engine` や `/api/agent-team/config`、`/api/free-team/settings` も `routing_profiles.free-team` を保存し、`llm_profiles` を含む入力を受ける（同:728-810,1128-1155,1474; `src/api/routes/free_team_routes.py:525-534,644-648`; `src/api/routes/llm_routes.py:3051`）。
    - UIを差し替えても古いWeb/API callerは旧値を書ける。resolverが新 `model_roles` を優先すれば、その保存は成功表示なのに実行へ反映されない。P1 の「元キー readonly」と新しい表の保存・読込経路が未設計。
    - **修正:** `model_roles` のtyped GET/PUT/PATCH、schema version、validation、CAS/ETagを追加する。旧 writer は新表へ変換して書くか、明示エラーで拒否する。Free Team pools/candidatesを保持しつつ `llm_profiles` だけを移すAPIも同時に対応する。

14. **中 — global config migration の同時実行と rollback は現状の契約だけでは安全でない**
    - `app_config_settings` は1行 (`GLOBAL_CONFIG_KEY`) に全設定を格納する。dotted patch helperはrow lockと任意の `expected_values` を持つ（`src/app_config_store.py:914-925,963-1000`）が、起動時 migration は変更後のglobal JSONをrow lock/CASなしで保存する（同:708-726）。`model_roles` 全体を一つの値として複数画面が保存すると、row lockだけでは古い map による上書きを防げない。
    - `legacy_snapshots.<日付>` の衝突規則、同日の再実行、復元後にP3/P5コードが再削除しない保証がない（設計:57,140-143）。snapshotに旧 route `api_key` 等を生値のまま保存する案なら、DBでは暗号化されても、閲覧/復元時のredaction・admin gate・保持期限を定義する必要がある。rollback は migration script だけでは元のrevision columnsや外部 run を戻せない。
    - **修正:** 一意な migration ID と同一transactionの snapshot+versioned switch、`expected_values`/row version CAS、排他制御を使う。snapshotは暗号化/秘匿を維持し、restore APIも権限検証する。rollback対象と不可逆なP5以降の境界を明記する。

15. **高 — P1以降の再起動で fallback 列と in-flight work の pin が一致しない**
    - 既存 `agent_runs` は開始時の route snapshot を固定し、retry時にも設定変更でrouteを変えない設計である（`src/services/agent_model_routing.py:985-1021,1134-1163`）。新routeは複数候補列だが、設計は `route_snapshot` を「記録のまま」とするだけ（設計:54,135-138）。P1中にfallback先や `model_roles` が変わった状態で再起動・再試行すると、初回実行と次attemptのrouteが食い違う可能性がある。plain Chatのstreamはrestartで中断し、再送時の同じturn/side effect処理も定義されていない。
    - P5の待機条件は旧版を参照する未完了作業だけだが、未claim work item、retryable/paused AgentDialogue、CLI/Muse session、過去binding/APIが含まれるか不明（設計:138,152）。run snapshotのない旧runは既存実装でもfail-closedとなる（棚卸し:319）。
    - **修正:** Run開始時に順序・billing class・allow_paid_fallback・role schema digestを含む全 fallback plan を凍結し、各 attemptを永続化する。P5 gate は `agent_work_items` だけでなく resumable dialogues/session/run と外部参照を列挙して確認する。ConversationAgentServiceは各turnで最新revisionの新snapshotを作るため、既存audit bindingを更新せずこの仕組みを使う（`src/services/conversation_agent_service.py:1039-1042,1109-1126`）。

16. **高 — P5 の削除対象と shared API / Operations / 生成型の整理が不足**
    - Agent revision API は現状 `agent_team_id`, `execution_profile_id`, `allowed_subagent_ids`, `capability_ceiling` を契約に含み（`src/api/agent_identity_routes.py:160-183`）、Operations Agent画面も送信・編集する（`frontend/src/components/operations/agents/employee-role.tsx:172-220`）。FastAPI OpenAPI はWeb/Mobileの生成型の元で、既存の実行-profile / session-settings APIも公開されている（`contracts/openapi/fastapi.json:77458-77490,77772-77854`、mobile生成型 `mobile/src/types/api-types.gen.ts:6472-6490,6643-6679`）。
    - 115 tests / 49 frontend files の名前検索だけでは、`capability_ceiling` の authority consumers、`/api/agent-team/config`、free-team overlay writer、chat session override、Operationsの一括組織設定や Mobile生成契約の移行完了を示さない。P5で列/APIを先に消すと、古いWeb/mobileが必須fieldなしで失敗する。
    - **修正:** P5前に全API/DB/FE/Mobile reader-writerをsymbol/endpoint単位で棚卸しし、新旧payloadの互換期間を設ける。OpenAPIとWeb/Mobile生成型を再生成し、runtime consumers全移行後にschema dropを行う。テスト数を完了条件にせず、各consumerの移行記録をgateにする。

## 確認して問題なし

- Free Team client は candidate lease の取得・usage精算を行い、timeoutやside effect/tool実行の可能性がある失敗では内部retryを止める（`src/llm/free_team_client.py:628-635,750-756,813-824`）。新fallback clientがこの境界を壊さない設計が必要。
- Agent turnは開始時にrevision/binding/route snapshotを固定し、既存runの再開で最新revisionを取り直さない。新しいChat turnは最新revisionから別snapshotを作る（`src/services/agent_run_service.py:604-665`、`src/services/conversation_agent_service.py:1039-1042,1109-1126`）。
- アプリ設定のsecret leafはDB保存時に暗号化され、`settings_public.py` にnested redaction helperがある（`src/app_config_store.py:897-900`、`src/security/settings_public.py:89-126`）。ただし新legacy snapshotをAPIで返す場合にも同じ秘匿処理を適用する必要がある。
- Project Automationのdedicated route不正は現在明示エラーになるため、汎用fallbackで「内容/設定エラー」を隠さない方針は現状のfail-closed挙動に合う（`src/services/project_automation_model.py:261,461-473`）。


