# AoiTalk 個人エージェント: 下書き backend milestone（未登録・未配備）

2026-10-22。`f1ade965043594b492a68001779dd01fde27cb6b` の個人台帳と設計契約を土台に、期限・在庫の候補保存から本人受理までを実装した。実データ、外部送信、モデル呼出し、grant/credential作成、稼働DBのmigration、配備は行わない。

## 実装した範囲

- `src/services/personal_agent_tools.py`: 優先順に行政手続の期限整理、弾薬の過去事実の記帳、家計整理、キャリア整理の4つの不変・休眠profileを定義。全profileは `disabled_design_only` / autostartなし。最初の2つだけに内部 `private.ledger.state` / `private.draft.propose` adapterがある。金融・キャリアの実行ツールは空。
- 実際の実行時registryは `src/llm/runtime_tool_registry.py` の `_register_tool_definition` / `_build_runtime_tool_registry` / `build_runtime_tool_registry`。`src/tools/registry.py` のglobal registryはlegacy。**どちらにも今回のadapter/profileを登録しない。** 設計JSONの6ツールは将来契約であり、6個とも実装・稼働した意味ではない。
- `src/services/personal_draft_service.py`: 本人の明示テキスト根拠、型付き期限/在庫候補、受理・破棄・再送receiptを暗号化追記する。新しい `personal_draft_events` と対応する `20261022_0002` migrationを追加した。
- `src/api/routes/personal_draft_routes.py`: 本人の署名済みWebセッション専用HTTP境界。Webの個人台帳ページから `personal-draft-panel.tsx` で根拠と候補を確認・受理・破棄できる。Mobileの受理UIは未実装。共有OpenAPIとWeb/Mobile型は生成するが、MobileのBearer認証でこの本人Web操作を代行できるようにはしない。

## 入力と根拠

最初の入力は **本人が選んだ明示テキストと、その値を指定する型付きproposal** に限定する。OCR、PDF/画像/添付取込、URL/任意パスの読込み、モデルによる抽出は未実装。テキストは16,384文字以下。

期限は `subject_key` / `due_date` と任意 `label`。在庫は `subject_key` / `entry_type`（`opening_balance`・`purchase`・`consumption`）/ 整数 `quantity` / `unit` / `occurred_on` と任意 `note`。初期残数のみ0を許す。未知項目、偽装したowner/actor/schema、無効日付、真偽値・文字列の数量は拒否する。subjectは中立なaliasとし、許可番号・銃の識別番号・口座番号を入れない。

`POST /api/personal-drafts/evidence` は本人操作のみ。指定された各値（中立subject以外）が元テキスト中に明記されていることを厳密な文字範囲で照合し、元テキスト・SHA-256・field spans・`owner_explicit_text_v1`・型付きproposalを暗号化保存する。これは意味の解釈・公的書類の真正性・本人確認済み期限を保証する抽出器ではない。例の合成在庫テキストは `consumption 2 pcs 2026-10-20`。書類中の命令文を実行する機構はない。

`private.draft.propose` または本人の `POST /api/personal-drafts` はownerのevidenceと型付き内容が完全一致した候補だけ保存する。同じevidenceから別の下書きを作ることは拒否する。違うevidenceとして再入力された同じ実世界の出来事の意味的重複検出は未実装で、本人レビューの対象となる。

## 保存と受理

イベントは `evidence` → `proposed` → `accepted` または `discarded`。別キーで既存の受理/破棄を照合した場合は `receipt` を追記する。原記録を上書きしない。訂正は元の未受理候補を破棄し、新しい根拠/候補を作る。受理済み台帳の訂正は既存の本人向け台帳操作に従う。

全イベントのpayload（source hash、範囲、出典版、proposal、digest、操作結果を含む）は既存のfield cryptoで暗号化する。DB triggerはUPDATE・直接DELETEを拒否する。owner＋idの複合FKは別ownerの根拠・下書き・台帳targetへの関連付けを拒否する。通常のowner削除によるcascadeだけを許す。現在値、共有Task/Record、通知scheduleには未受理候補を投影しない。

本人のHTTPは次の操作を提供する。

| 操作 | endpoint |
|---|---|
| 明示テキスト根拠保存 | `POST /api/personal-drafts/evidence` |
| 根拠と一致した候補保存 | `POST /api/personal-drafts` |
| 最新候補一覧（最大50件） | `GET /api/personal-drafts?limit=50` |
| 元テキスト・範囲・全候補・digest・結果の再照合 | `GET /api/personal-drafts/{draft_id}` |
| 本人受理 | `POST /api/personal-drafts/{draft_id}/accept` |
| 未受理候補の破棄 | `POST /api/personal-drafts/{draft_id}/discard` |

受理/破棄本文は `{"confirmed_digest":"<表示全内容のSHA-256>","idempotency_key":"<固定した不透明UUID>"}`。このmilestoneの操作キーはUUIDに限定し、広い将来設計schemaより厳しくする。digestはdraft ID・revision=1・schema・型付き全内容・proposal主体・evidence ID・元テキストと出典hash/範囲/版を束縛する。未知の結果では同じdraft IDと操作キーで再照合・再送する。

- 在庫受理時だけ `user_action` として既存台帳へ反映し、`accepted` を返す。Agentから受理できず、提案だけで残数は変わらない。負数・複数単位・初期残数重複の既存警告は維持する。
- 期限受理は `agent` の `deadline_candidate` として材料化し、`materialized_requires_verification` を返す。既存の本人用 `/api/personal-ledger/entries/{entry_id}/verify` で別に確認するまで未確認。受理を確認済み・完了・通知予定作成済みと扱わない。
- 既存 `append_entry` はtransactionを所有したまま。検証と追記をprivateなshared-session coreへ分け、受理はそのcoreを **同じtransaction** 内で呼ぶ。台帳追記と受理target対応の両方がcommitまたはrollbackされる。台帳キーはdraft IDから決定する。
- owner単位のtransaction advisory lockとDBの単一terminal/単一target制約で同時受理を直列化する。同じキーの別内容は競合。異なるキー・再接続・新しいservice instanceで同じ候補を受理しても同じtargetを再送するだけ。別キーでの再照合もreceiptで予約し、そのキーを別候補へ転用できない。

## 認証とローカル実行境界

HTTPのownerは既存 `require_interactive_owner` が署名済みNext Cookieを再検証して解決する。Bearer、Agent、service、内部転送markerだけの要求、認証無効、主体不一致を拒否する。Next proxyは `personal-ledger` と同じ同一Origin/Referer mutation検証とcookie限定転送を `personal-drafts` にも適用する。backendはJSON以外のmutationとcross-site fetchを追加拒否し、候補応答は `Cache-Control: no-store`。既存台帳の認証を緩和しない。

内部adapterは既定で無効。テスト/将来のserver controllerが明示的に有効化した場合も、実際の `TurnContext` のowner・session、projectなし、同じuser/sessionに束縛した実 `OutboundPrivacyGateway`、明示 `local_only`、解決済みloopback endpoint、外部fallback禁止を必須とする。constructorのuser辞書やモデル引数でowner/profile/routeを代入できない。DB読取前とawait後の返却前に再検証する。SQLでownerとprofileのledger/subjectを絞ってから復号し、既存の5,000行上限超過は部分成功にしない。

この境界はadapterを本番実行へ有効化したことを意味しない。将来のcontrollerはモデル入力・tool結果・ログ/trace・埋め込み・通知まで全runのegress抑止を接続/検証する必要がある。**受理・verify・completeをAgentツールへ公開しない。** profile定義もgrantも自動起動も今回登録しない。

## 明示的な未実装範囲

- Mobile UI、OCR/添付source resolver、外部モデル、定期実行、通知/取消/配信管理、5,000行を超える完全state projection、候補一覧のpagination。
- 金融・キャリアのprivate dataset、取込/重複照合/集計・context adapter。設計JSONのfinance/career/reminder出力例は稼働機能ではない。
- **Moneytree/MF MEの直接API連携は依頼済みの未完了要件。手動ファイルで代替完了にしない。** 別途引継がれた公式調査では、Moneytree LINKはOAuth APIだが提供元発行のcredentialsと本番契約が必要で、個人self-service利用可否は未確認。状態は `needs_provider_onboarding`。MF MEは公式ChatGPT Apps連携があるがAoiTalk/一般clientの公式API利用権は未確認、`official_client_access_unverified`。両adapterは無効。ChatGPT token流用・非公開endpoint推測は行わない。参照: [Moneytree開始条件](https://docs.link.getmoneytree.com/docs/getting-started)、[test環境](https://faq.getmoneytree.com/test-environment)、[MF ME公式案内](https://support.me.moneyforward.com/hc/ja/articles/61272522990361)。本実装作業からAPI接続や認可は行っていない。
- 法定期限/更新周期の自動計算。管轄に合う公式規則のレビューと必要な本人日付が揃うまで無効。武器使用・改造・調達の支援は含まない。
- 稼働migration、本番Agent登録、追加grant/credential、配備、実データ投入は未実施。

## 合成検証

```bash
venv/bin/python docs/design/personal-agents-20261022/validate_design.py
# 下記はloopback・aoitalk_test_* の使い捨てPostgreSQLだけを許す。
AOITALK_TEST_POSTGRES_DSN=<isolated-test-dsn> venv/bin/python -m pytest \
  tests/test_personal_drafts.py tests/test_personal_draft_routes.py \
  tests/test_personal_ledger_service.py tests/test_personal_ledger_routes.py \
  --basetemp=.local/pytest-personal-drafts
```

`synthetic-cases.json` / `tool-contracts.json` / `agent-definitions.json` は将来の全体設計を保持する。実装済みsubsetとUUID操作キーの制約は本書・実装・生成OpenAPIを正本とする。移送元の作業用相対pathをこのrepository内のpathへ修正した。今回の合成検証は実モデル/実法令/実運用の確認を代替しない。

今回の実測: Pythonの対象4ファイルは **129 PASS / skipなし**。実PostgreSQLには使い捨てのloopback DBだけを使用し、最小users表・共有投影の空sentinel表と、実際の台帳/下書き2本のmigrationを適用した。全migration履歴や稼働環境へ適用した検証ではない。並行受理（同一/異なるキー）、accept/discard競合、保存途中のrollback、service再作成後の再送、別キーreceiptの再利用競合、別ownerへの復号前拒否、DB複合FK・追記専用trigger、実serviceを通すHTTP lifecycle、外部route/未知route/fallback・途中のpolicy変更拒否を含む。

backend時点ではNext proxy **15 PASS**、対象ESLint/Ruff・Web全体型検査・Mobile Product Contract **PASS**。正規OpenAPI生成とWeb/Mobile型生成を実施し、既存の全path/schemaが不変で5pathだけ追加されたことを比較した。別担当によるサービス・認証境界の読取りレビューも実施した。続くWeb UIでは表示digestの明示確認と送信直前の再照合、対象/digest変更ごとの確認解除、曖昧な応答後の同一payload/key再送、終端状態の保持を追加。保留中の要求は画面メモリ内で保持し、ページを再起動した場合もサーバーの単一受理制約と取得した終端状態で二重計上を防ぐ。UIの対象Vitestは53 PASS、対象ESLintとWeb全体の型検査もPASS。独立した合成APIの実Chromium操作10シナリオと既存記帳の回帰確認1件、375px/1280pxの実ブラウザ操作と375px画像の目視確認を実施した。さらに、旧台帳読取りの遅延応答が受理後の残数を上書きしないPlaywright回帰1件も合成APIの隔離ハーネスでPASS。モデル/通知/本番登録は引き続き未実施。
