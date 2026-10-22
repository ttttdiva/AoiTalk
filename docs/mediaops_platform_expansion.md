# MediaOps: FANZA / TikTok と調査の実行境界

## 追加された契約

既存 X / pixiv / DLsite / Patreon / YouTube / Instagram を維持し、FANZA と TikTok を共通の型・DB制約・OpenAPI・Web/Mobile生成型・Persona/Account/Research/Content/Metrics/Operationsへ追加した。

- FANZA は fanza_release。売場（同人/商業）、販売者参照、商品パッケージ、サンプル、価格と通貨、AI利用区分を保存する。AI生成、一部AI利用、未確認を区別する
- TikTok は tiktok_video。単一動画、caption、cover、音源権利、AI表示、明示選択したprivacyを保存する。privacyの自動既定値を与えない
- どちらも実provider稼働を宣言しない。手動引継ぎの準備と外部の実行結果は別で、公開・販売開始をdraft payloadから主張できない
- FANZAのAI利用未確認、TikTokの音源権利未確認はContent readinessと既存Operations Kernelの双方で公開を止める。既存の人間承認・account/revision/hash・QA/権利検証を維持する

公式能力の根拠:
- https://support.dmm.co.jp/doujin/article/48426
- https://developers.tiktok.com/docs/en/content-sharing-guidelines
- https://developers.tiktok.com/docs/en/content-posting-api-get-started

## 調査の実行

Routineのaccount/tag条件は公開検索queryへ変換する。これは認証済みnative timelineの追跡を意味しない。domain許可、除外語、取得できた公開日の鮮度、URL重複排除、設定された候補数上限を実際の結果へ適用する。未取得の公開日は鮮度未検証として残す。20 queryの実行上限と未実行分を記録する。native paginationは未使用である。

実行前に正規サービスから固定されたRoutine revisionを再読込し、hashとversionを確認する。WorkItemの短縮projectionだけを実行条件の正本にしない。

検索snippetはsignalとして保存し、factへ自動昇格しない。Automation briefと画面も検証済み主張・未検証情報・空結果・一部失敗を区別する。manual/fallbackのテーマだけを調査実績にしない。

YouTubeの公開video IDだけは証拠URLに保持する。他のquery値・credential・trackingは引き続き破棄する。source idempotency keyは結果の並び順に依存せず、case-sensitiveなvideo IDを保持する。

## 適用と検証

migration: 20261005_0001（前版20261004_0001）。既存revisionの新媒体flagはfalse、unknownなbulk intakeはnullを維持する。新媒体を利用した記録があればdowngradeを拒否する。DBへ適用する際は正規Alembic経路を使い、Agent/Team等の業務データは正式API/UIで登録する。

主な検証:
- tests/test_media_fanza_tiktok.py
- tests/test_media_platform_expansion_migration.py
- tests/test_media_research_scope.py
- tests/test_media_common_runtime.py
- tests/test_media_operations_automation_service.py
- frontend/src/components/operations/media/content-variant-panel.test.tsx

これらのfixture成功は、本番DB適用・Agent実登録・外部投稿・実モデル推論・収益の証明ではない。それぞれ別の実行証跡とreadbackで確認する。
