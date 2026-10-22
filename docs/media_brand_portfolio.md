# Operations メディアブランドポートフォリオ配置表・簡易設定手順

本ドキュメントは、Operations（MediaOps WS1 / WS2）におけるブランドポートフォリオ定義（`resources/mediaops/sns_portfolio_v6.json` および `resources/mediaops/sns_portfolio_v6.md`）を整理した配置表および簡易設定手順である。47件は既存設計文書16件と計画資料の定義31件、15件は媒体構成の定義数を示す。これらは定義上の件数であり、実環境での素材生成・Persona下書き・アカウント・管理参照の作成や登録済み状態は確認していない。

素材再生成、外部アカウント作成、外部投稿は行わず、既存の実装・契約（`MediaBrandPortfolioService`, `MediaOperationsSetupService`, `MediaOperationsService`）に準拠して構成する。

---

## 1. 構成要素の概要と内訳

| 区分 | 件数 | 内訳・対象 |
|---|---|---|
| **管理参照の定義** | 9件 | 本部（`00_SNS事業本部`） + 8公開ブランド（`01_よぱにしゃっど！`〜`08_UN_REAL`）。実環境の作成状態は未確認 |
| **ペルソナ下書き対象の定義** | 8件 | `PersonaBulkDraftSlot` のスロット1〜8に対応する候補。下書きレコードの作成状態は未確認（スロット9は予備/未割当） |
| **プラットフォームアカウント構成の定義** | 15件 | 各ブランドの主媒体構成（X: 5, pixiv: 3, YouTube: 5, ニコニコ: 1, Instagram: 1）。実アカウントの作成・登録状態は未確認 |
| **設計文書・計画資料の定義** | 47件 | 既存設計文書 16件（CI 8件 + Brief 8件） + 計画資料の定義 31件。31件は生成済み素材を意味しない |

---

## 2. ブランドポートフォリオ定義の配置表

以下はポートフォリオファイルに記載された定義であり、DB上のPersona・Project・アカウント・PersonaResourceの実在や登録を示すものではない。

### 2.1 全体構造

```text
00_SNS事業本部 (Strategy / Operations Director) [本部管理参照]
├── Slot 1: 01_よぱにしゃっど！ (成人向けAIイラスト・短尺動画)
│   ├── アカウント (2): X, pixiv (※Patreon/DL販売はサービス選定待ち)
│   └── 素材 (8): CI, Brief + 6計画資料
├── Slot 2: 02_元気が2番！ (VOICEROID絵・短編漫画・劇場動画)
│   ├── アカウント (4): X, pixiv, ニコニコ動画, YouTube
│   └── 素材 (7): CI, Brief + 5計画資料
├── Slot 3: 03_鬱戦 (ほの暗いオリジナルIP資産)
│   ├── アカウント (3): X, Instagram, pixiv
│   └── 素材 (7): CI, Brief + 5計画資料
├── Slot 4: 04_刺されても死なない大きなクチバシ (トレンド二次創作・時事ミーム)
│   ├── アカウント (1): X
│   └── 素材 (6): CI, Brief + 4計画資料
├── Slot 5: 05_OUTDATED™ (不穏知的解説 / NotebookLM)
│   ├── アカウント (1): YouTube
│   └── 素材 (7): CI, Brief + 5計画資料
├── Slot 6: 06_どうせ嘘だろうが (最新ネタDeep Research解説)
│   ├── アカウント (1): YouTube
│   └── 素材 (8): CI, Brief + 6計画資料
├── Slot 7: 07_OFFBY1 (AI/SaaS実測・実務メディア)
│   ├── アカウント (2): X, YouTube (※Instagramは補助/前身由来)
│   └── 素材 (2): CI, Brief
└── Slot 8: 08_UN_REAL (高速クラブミュージック・リスナー資産)
    ├── アカウント (1): YouTube
    └── 素材 (2): CI, Brief
```

---

### 2.2 詳細配置マトリクス

| Slot | ブランド名 | Project名 | 主ターゲット | 媒体構成定義 (15件) | 設計文書・計画資料定義 (47件) |
|:---:|---|---|---|---|---|
| **-** | **SNS事業本部** | `00_SNS事業本部` | 全体統括・戦略・調査 | - | `CUSTOM_INSTRUCTIONS.md`<br>`PROJECT_BRIEF.md`<br>*(本部将来成果物: 5件)* |
| **1** | **よぱにしゃっど！** | `01_よぱにしゃっど！` | 成人向け直接収益 | `x`<br>`pixiv` | 1. `CUSTOM_INSTRUCTIONS.md`<br>2. `PROJECT_BRIEF.md`<br>3. `PERSONA_BIBLE.md`<br>4. `CHARACTER_BIBLE.md`<br>5. `VISUAL_BIBLE.md`<br>6. `FETISH_TOPIC_MATRIX.md`<br>7. `PRODUCT_BIBLE.md`<br>8. `ALGORITHM_EXPERIMENTS.md` |
| **2** | **元気が2番！** | `02_元気が2番！` | VOICEROIDファン形成 | `x`<br>`pixiv`<br>`niconico`<br>`youtube` | 1. `CUSTOM_INSTRUCTIONS.md`<br>2. `PROJECT_BRIEF.md`<br>3. `PERSONA_BIBLE.md`<br>4. `CHARACTER_INTERPRETATION_DB.md`<br>5. `DIALOGUE_BIBLE.md`<br>6. `THEATER_FORMATS.md`<br>7. `PLATFORM_EDITING_RULES.md` |
| **3** | **鬱戦** | `03_鬱戦` | オリジナルIP資産 | `x`<br>`instagram`<br>`pixiv` | 1. `CUSTOM_INSTRUCTIONS.md`<br>2. `PROJECT_BRIEF.md`<br>3. `WORLD_BIBLE.md`<br>4. `CHARACTER_BIBLE.md`<br>5. `VISUAL_BIBLE.md`<br>6. `TIMELINE.md`<br>7. `VOICE_GUIDE.md` |
| **4** | **刺されても死なない大きなクチバシ** | `04_刺されても死なない大きなクチバシ` | Attention / Trend | `x` | 1. `CUSTOM_INSTRUCTIONS.md`<br>2. `PROJECT_BRIEF.md`<br>3. `PERSONA_BIBLE.md`<br>4. `FANDOM_KNOWLEDGE.md`<br>5. `NEWS_MEME_PLAYBOOK.md`<br>6. `TREND_SOURCE_LIST.md` |
| **5** | **OUTDATED™** | `05_OUTDATED™` | 高回転知的YouTube | `youtube` | 1. `CUSTOM_INSTRUCTIONS.md`<br>2. `PROJECT_BRIEF.md`<br>3. `TOPIC_BACKLOG.md`<br>4. `RESEARCH_STANDARD.md`<br>5. `TITLE_THUMBNAIL_BIBLE.md`<br>6. `SCRIPT_STYLE.md`<br>7. `NOTEBOOKLM_WORKFLOW.md` |
| **6** | **どうせ嘘だろうが** | `06_どうせ嘘だろうが` | 高品質・最新ネタYouTube | `youtube` | 1. `CUSTOM_INSTRUCTIONS.md`<br>2. `PROJECT_BRIEF.md`<br>3. `TOPIC_RADAR.md`<br>4. `SOURCE_QUALITY_RULES.md`<br>5. `DEEP_RESEARCH_TEMPLATE.md`<br>6. `VIDEO_PRODUCTION_PIPELINE.md`<br>7. `MOTION_GRAPHICS_BIBLE.md`<br>8. `VOICE_BIBLE.md` |
| **7** | **OFFBY1** | `07_OFFBY1` | 実務・購買意図 | `x`<br>`youtube` | 1. `CUSTOM_INSTRUCTIONS.md`<br>2. `PROJECT_BRIEF.md` |
| **8** | **UN//REAL** | `08_UN_REAL` | 音楽カタログ / リスナー資産 | `youtube` | 1. `CUSTOM_INSTRUCTIONS.md`<br>2. `PROJECT_BRIEF.md` |
| **9** | *(空きスロット)* | - | - | - | *(未割当)* |

---

## 3. 簡易設定手順 (Setup Runbook)

本節は、`MediaBrandPortfolioService`（`src/services/media_brand_portfolio_service.py`）および `MediaOperationsSetupService`（`src/services/media_operations_setup_service.py`）のAPI契約に沿った設定手順を示す。ここに記載する定義数は実環境の登録済み状態を保証しない。

### Step 1: ポートフォリオ定義の照合
1. `GET /api/operations/media/brand-portfolio/definition` を呼び出し、応答の `source.sha256` が原文（`resources/mediaops/sns_portfolio_v6.md` のファイルバイト / SHA-256: `8f3cade315b6866743748e0ff8e2567759793685b89a2cf7b5e1300e0ca30edd`）と一致することを確認する。この値は `resources/mediaops/sns_portfolio_v6.json` 自体のSHA-256ではない。

### Step 2: プロジェクトの準備と紐付け
1. 認証済みユーザーのUUIDと Step 1 で確認した `source.sha256` を用意する。
2. `POST /api/operations/media/brand-portfolio/setup` を呼び出す。JSON本文には `expected_owner_user_id` と `expected_source_sha256` が必須で、`Idempotency-Key` ヘッダーも必須。
   ```json
   {
     "expected_owner_user_id": "<認証済みユーザーのUUID>",
     "expected_source_sha256": "8f3cade315b6866743748e0ff8e2567759793685b89a2cf7b5e1300e0ca30edd"
   }
   ```
   山括弧内は実際のUUIDに置き換える。
3. `project_bindings` は任意。既存Projectへの明示的な紐付けが必要な場合だけ、実在を確認したProject UUIDを指定する。省略時の解決・作成はAPIの実装に従う。
4. setupは各ブランドのPersonaへ、v6原文、`custom_instructions`、`project_brief` の計3件を `resource_kind="supporting_document"` と `provenance.type="artifact"` のSHA-256参照として自動添付する。これら3件を手動で再添付する必要はない。

### Step 3: ペルソナ下書き（Bulk Draft）の確認と反映
1. 8ブランドの基本属性（名前、概要、トーン、対象媒体、コンテンツ柱）は `PersonaBulkDraft` のスロット1〜8に対応する。スロット9は予備である。
2. `POST /api/operations/media/persona-drafts` でプレビューを作る。`source` はファイルパスではなく、JSON/Markdown等の本文テキストを値として渡す。例ではJSON文書そのものを文字列にしている（実データに置き換える）。これは入力形式を示す1スロットの例で、完全な8ブランド分の下書きを作るには対象スロットの実データを含める。`Idempotency-Key` ヘッダーが必須。
   ```json
   {
     "source_format": "json",
     "source": "{\"slots\":[{\"slot\":1,\"display_name\":\"<表示名>\",\"summary\":\"<概要>\",\"voice\":\"<口調>\",\"audience\":\"<対象読者>\",\"platforms\":[\"x\"],\"content_pillars\":[\"<コンテンツ柱>\"]}]}"
   }
   ```
3. 応答の `applyable: true` と `version` を確認する。適用する場合は、応答の `id` を `{draft_id}` に使って `POST /api/operations/media/persona-drafts/{draft_id}/apply` を呼び、本文に `{"expected_version": <応答のversion>}`、必須ヘッダーに `Idempotency-Key` を指定する。`applyable` はAPIのレスポンスフィールド名である。

### Step 4: プラットフォームアカウントの登録
1. 媒体構成を実アカウントとして登録する場合は、`POST /api/operations/media/platform-accounts` に `Idempotency-Key` ヘッダーと、少なくとも次の必須フィールドを送る。`project_id` と `persona_id` は任意。
   ```json
   {
     "platform": "x",
     "account_ref": "<確認済みのアカウント識別子>",
     "display_name": "<確認済みの表示名>",
     "project_id": "<Project UUID>",
     "persona_id": "<Persona UUID>"
   }
   ```
   山括弧内は確認済みの実値に置き換える。`platform` に指定できる値は `x`, `pixiv`, `dlsite`, `patreon`, `youtube`, `instagram`, `fanza`, `tiktok`。ポートフォリオにある `niconico` は現行 `MediaPlatform` enum にないため、このAPIでは登録できない。リクエストに `publish_capability` や `credential_status` は含めない。
2. 資格情報の登録はアカウント作成とは別操作で、`POST /api/operations/media/platform-accounts/{account_id}/credential` を使う。API契約上の必須multipart項目は `connection_type` と `package`、加えて `Idempotency-Key` ヘッダーである。任意のトークン／Cookie項目を作らず、資格情報パッケージの契約に従う。

### Step 5: 素材・文書（PersonaResource）の配置
1. 実在するファイルや参照先を該当Personaへ添付する場合は、`POST /api/operations/media/personas/{persona_id}/resources` に `Idempotency-Key` ヘッダーを付ける。本文フィールドは `resource_kind`、任意の `platform` / `label`、および `provenance`。ファイルパスや `kind` はこのAPIのフィールドではない。
   ```json
   {
     "resource_kind": "supporting_document",
     "label": "PROJECT_BRIEF.md",
     "provenance": {
       "type": "artifact",
       "sha256": "<対象文書の64桁SHA-256>",
       "mime_type": "text/markdown"
     }
   }
   ```
   山括弧内は対象文書の実際のSHA-256に置き換える。`resource_kind` の例は `persona_bible` と `supporting_document`。`supporting_document` には `url`（`type` と `url`）または `artifact`（`type`, 64桁SHA-256, `mime_type`）を使う。`stored_artifact`（`type` とArtifact UUID）は `reference_image` 専用であり、`supporting_document` と組み合わせると拒否される。
2. 計画資料の定義31件は、実ファイルや登録済みArtifactの存在を意味しない。実体を用意し、APIが受け付ける `provenance` を確認した後にのみPersonaResourceとして登録する。

---

## 4. 保守と不変条件

- **不変条件 1**: 1つの公開Personaは1つの専用Projectを占有する（本部プロジェクトには公開Personaを配置しない）。
- **不変条件 2**: 原本チェックサム（`SOURCE_SHA256`）と文書改変の検証を常に通す。
- **不変条件 3**: パスワード・トークン・APIキー等の秘匿情報をメタデータやURL、平文ログに含めない。
- **不変条件 4**: 未確定サービス（Patreon系・DL販売系等の具体プロバイダ）は無理に確定させず、`unknown` として保持する。
