# SNS v6 の公開人格Project取込

SNSの単位は **1公開人格 = 1Project**。既存 `media_personas` / `media_platform_accounts` / EditorialProgram / ContentItem / GenerationPlan を使う。別Brandテーブル、別投稿queue、機械遷移ごとのTaskは追加しない。AoiTalkは判断・Memory、SocialOpsは投稿queueとraw metrics、MediaOpsは制作物・preview・明示publishの既存契約を使う。

担当ファイルは `src/services/media_brand_portfolio_service.py`、`src/api/media_brand_portfolio_routes.py`、`scripts/media_brand_portfolio_setup.py`、`resources/mediaops/sns_portfolio_v6.{md,json}`、`tests/test_media_brand_portfolio.py`。既存setup routerで登録し、research serviceの生成contextに採用済み人格原文を固定する。WebとNativeは同じFastAPI契約を使う。

## API契約

| API | operationId | 内容 |
|---|---|---|
| `GET /api/operations/media/brand-portfolio/definition` | `media_get_brand_portfolio_definition` | 8ブランド、原文識別情報、媒体候補、QA、参考、目的 |
| `GET /api/operations/media/brand-portfolio` | `media_get_brand_portfolio` | 認証owner限定のProject / Persona / channel / Program / Item読戻し |
| `POST /api/operations/media/brand-portfolio/setup` | `media_setup_brand_portfolio` | ownerと原文SHAを検証する原子的・再実行可能setup |

POSTは `Idempotency-Key` と下記bodyを受ける。

```json
{
  "expected_owner_user_id": "<認証ユーザーのUUID>",
  "expected_source_sha256": "8f3cade315b6866743748e0ff8e2567759793685b89a2cf7b5e1300e0ca30edd",
  "project_bindings": {}
}
```

`project_bindings` のキーは `headquarters`, `yopanisyaddo`, `genki_2`, `utsusen`, `kuchibashi`, `outdated`, `douseusodarouga`, `offby1`, `un_real`。値は既存Project UUID。各Projectは認証owner自身の所有かつ正式名・稼働状態が一致する必要がある。未指定は同ownerの正式名Projectを一意に採用し、なければ既存ProjectRepositoryとcanonical Docs初期化で作成する。曖昧な同名候補、別owner、完了/削除済み、別ブランドへの割当、再実行時の変更mappingは拒否する。

`00_SNS事業本部` は公開Personaを持たず、参加申請・他メンバーを許可しない管理Project。8公開ブランドの内部固定キャラクターは既存slot-free Characterとして保持できる。他ProjectのQA用Personaや原戦略metadataを削除・置換しない。既存公開Personaの未対応source移行は自動で重複作成せず409で止める。

## 制作から公開への引渡し

返却された `brands[].persona.current_revision.id`、`editorial_program_id`、`content_item_id` が制作側の入口。初期Itemは採用済みv6設計のhypothesis Findingとartifact SHAを持つ。新benchmarksや10/6 probeを成功結果として取り込まない。

Plannerが企画を具体化し、既存Research / Editorial APIでItemへ原典・採点条件・QAを持たせる。実際のStudio workspaceとCreativeRecipeを明示選択した後、既存 `POST /api/operations/media/generation-plans` に `persona_revision_id`, `creative_recipe_revision_id`, `workspace_id`, `content_item_id`, `request_spec` を渡す。setupはモデル・workspaceの接続やGeneration送信を行わない。

GenerationPlanは旧PersonaRevisionとItemを固定する。`request_spec.research_context.brand` は人格名、原文Creative Direction、QA・目的・参考・KPI・原文SHA・未確定事項を持ち、context hashの対象になる。後のPersona編集で既存Planを差し替えない。制作物から既存ContentVariant / QA / preview / explicit publishへ引き渡す。投稿queueとraw metricsはSocialOps正本、検証結果・結論はAoiTalkへ戻す。

媒体リストは `channels_exhaustive=false` の候補であり、追加媒体・同一媒体の複数アカウントを禁止する一覧ではない。実アカウントの追加・一覧・接続は既存PlatformAccount APIを使う。ニコニコは現行MediaPlatform未対応として `unsupported_platform`、Patreon系 / membership系とDL販売系は `service_selection_required` のまま保持する。DLsite等の具体的サービスや参考URLを推測確定しない。対応媒体の初期内部枠はpaused・credential unknown・remote URLなし。

OFFBY1のInstagram補助は、8/30前身戦略 `sns-account-strategy-current-2026-08-30.md`（`libfile_b6232d2776d481919e7ed52dff698c97`）の「X + YouTube が主力 / Instagram は補助」由来。channelの `source` にそのLibrary IDを保持する。10/7の新指示やv6本文の媒体記述として扱わない。

## source側の再実行

```bash
venv/bin/python scripts/media_brand_portfolio_setup.py plan \
  --owner <認証ユーザーのUUID> --output .local/sns-v6-plan.json

venv/bin/python scripts/media_brand_portfolio_setup.py apply \
  --plan .local/sns-v6-plan.json --base-url http://127.0.0.1:8000

venv/bin/python scripts/media_brand_portfolio_setup.py readback \
  --plan .local/sns-v6-plan.json --base-url http://127.0.0.1:8000
```

接続済みAPI tokenは既存 `AOITALK_API_TOKEN` を利用する。tokenやcredentialをplan・出力へ保存しない。`plan` はoffline、`apply` は明示コマンド。前後で認証ownerとreadbackを検証する。setupは一括commitし、途中失敗をrollbackするため、通信断後も同じplanで再実行できる。productionへの適用はこの開発で実施せず、最終local setupで行う。

## 原文と検証

正式Library原文は `libfile_12f761b543388191aa074bf44bf9751b`。正規Library readを1〜1000行と1001〜1016行まで読み、UTF-8本文を保持した。28474 bytes / 1016 linesがLibrary metadataと一致し、本文SHA-256は上記値。ZIP `libfile_5294d7d8391c819198c932a0048a2077` は識別済み。signed-byte downloadはnetwork proxyが403で拒否したためZIP SHAは未検証nullであり、本文SHAと混同しない。新benchmarks・旧仮名・probeによる置換は行わない。

検証はisolated SQLiteの実FastAPIで正常・owner/Project越境拒否・再実行dup0・fresh readback・一括rollback・内部Character保持・8brandのPlan contextを確認する。隔離PostgreSQL実APIでも新規9Projectとcanonical Docs、8Persona、15内部Account枠、8Item、越境403、再実行dup0、Task / GenerationRun / 投稿 / paid model呼出0を確認した。実装担当以外のread-onlyレビューを実施し、内部Character判定と変更mappingの再実行拒否を修正した。

targeted pytestは34件PASS。共有OpenAPIは正規generatorで再生成し、既存702ルートと既存schemaが同一で、追加は3ルート・9schemaのみと確認した。Web / Nativeの生成型はlockfileのopenapi-typescript 7.13.0から生成し、TypeScript 5.9.3で両ファイルを検証した。Mobile Product ContractもPASS。本担当はbackendと共有契約のsource実装までで、最終UI統合・APK配布・実アカウント接続は別担当の統合工程へ引き渡す。
