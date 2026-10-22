# 人の制作briefと調査の入力契約

`POST /api/operations/media/content-items` は既存の `editorial_program_id`、
`title`、`brief` と `Idempotency-Key` を使い、`input_kind` を追加する。

- `research`（省略時の既定値）: 従来どおり `finding_ids` または
  `candidate_ids` が1件以上必要。出典・採用状態・ACL・Project・Personaの検証を維持する。
- `user_authored`: 人が書いた創作依頼を `brief` に指定する。研究参照なしで保存できる。
  空白だけの入力は禁止し、原文を空白・改行も含めて最大8000文字で保存する。
  実際の調査を併記する場合、参照の検証は `research` と同じである。
  finding、candidate、evidenceを自動作成せず、事実調査や確認済みの出典には扱わない。

```json
{
  "editorial_program_id": "<existing program id>",
  "input_kind": "user_authored",
  "title": "オリジナルの集計レッスン",
  "brief": "架空の20行を使う。製品A/Bを各10行、画像と練習ファイルを照合する。",
  "persona_revision_id": "<this program's immutable persona revision id>",
  "content_type": "image"
}
```

作成者 `created_by` と所有者 `owner_user_id` は認証principalから決まり、Projectと
Personaは既存制作計画とimmutable Persona revisionから決まる。クライアントから
所有者・作成者を上書きできない。応答・一覧・再読込には `input_kind` が含まれる。
保存済み `brief`、`version`、`content_hash` と `persona_revision_id` が正規の制作入力であり、
user_authoredのhashは種別も含む。同一キー・同一payloadは同じ記録、変更は409になる。
Persona revisionを省略した同一キーの再送は最初のpinを保ち、新規キーは最新revisionをpinする。
既存researchのhashは変更しない。修正依頼は別のimmutable ContentItemとして保存する。

Generation Planのserver-owned `request_spec.research_context.brief` に原文・種別・作成者・
Persona revisionをsnapshotする。外側には所有者・Project・ContentItem version/hashを含む。
研究参照がない場合、`research.findings` と `research.candidates` は空配列である。
人の要求を出典付き事実に昇格させず、権利・受入試験・収益の未確認状態を維持する。
既存Planは再計算せず、新規Planは既存の8000文字prompt上限を超えたら保存前に422となる。

DB migration `20261007_0024` は既存行を `research` として保持する。
user_authored行がある場合、種別を失うdowngradeは拒否する。
