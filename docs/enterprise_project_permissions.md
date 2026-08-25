# Enterprise プロジェクト権限・移行手順

## 権限ポリシー

プロジェクト所有者 (`projects.owner_id`) と global admin は、membership の有無や
保存済み ACL にかかわらず、5 権限すべてを持ちます。それ以外の主体は
`project_members.permissions` に明示された `true` だけが有効です。legacy 互換のため
JSON object を表す文字列は object として正規化しますが、`NULL`、JSON object でない
文字列、配列、壊れた値、未知キー、または boolean 以外の値を含む ACL、membership
なしは deny-all です。未知キーを含む ACL は既知キーだけを残すことはせず、ACL 全体を
deny-all として扱います。

| 操作 | 必要な権限 |
| --- | --- |
| project GET、task read、project file read、Agent read | `read` |
| task create | `read` + `write` |
| task write、project file write、Agent write | `write` |
| project file delete、project delete | `delete` |
| member 一覧・追加・更新・削除 | `manage_members` |
| project PATCH（name、description、space、完了、aliases、color、metadata 等） | `manage_settings` |

`read`、`write`、`delete`、`manage_members`、`manage_settings` は互いに代用しません。
Next.js Cookie 経路は `hasEffectiveProjectPermission`、FastAPI・Repository・Task・Agent
経路は `has_effective_project_permission` を正本として、上表の同じ権限を渡します。
task create は不可視作成を防ぐため `read` と `write` の両方を必須とします。write-only
membership は作成前に拒否し、read 権限を自動付与したり membership を書き換えたり
しません。

## role の安全な既定値

permissions が未設定だった旧行だけ、次の値へ移行します。新規作成・更新時の未知、
空、誤記 role は例外にし、member への fallback は行いません。

| role | read | write | delete | manage_members | manage_settings |
| --- | --- | --- | --- | --- | --- |
| owner | true | true | true | true | true |
| admin | true | true | true | true | true |
| member | true | false | false | false | false |
| viewer | true | false | false | false | false |
| 未知・NULL・未対応 | false | false | false | false | false |

`projects.owner_id` を所有権の正本とします。owner membership が欠落していれば作成し、
同じ user の旧 role は `owner` へ直します。既存の明示 permissions は保持し、NULL の
場合だけ owner の全権限を保存します。実効権限は保存値にかかわらず owner 全権限です。

## migration の適用区分

新規 DB、または `20260804_0002` をまだ適用していない DB では、修正版
`20260804_0002` が上表を直接 materialize します。その後
`20260807_0001` が owner 関係を再確認し、Enterprise bootstrap 状態表を作成します。

旧版 `20260804_0002` を適用済みの DB では、`20260807_0001` は次だけを自動補正します。

- `projects.owner_id` から一意に判断できる owner membership の欠落・role 不一致
- permissions が現在も NULL で、未設定と断定できる行

旧版が生成した admin の `manage_settings=false`、member の `write=true` は、管理者が
同じ値を明示設定した行と DB 上で区別できません。そのため全件を自動上書きしません。

## 適用済み DB の監査と確認補正

最初に DB backup を取得し、read-only の監査を実行します。

```bash
psql "$DATABASE_URL" -X -v ON_ERROR_STOP=1 \
  -f scripts/audit_project_member_permissions.sql
```

最初の結果は「旧版の誤 backfill と同じ JSON」の候補であり、誤設定の証明では
ありません。membership の作成時刻、管理者操作ログ、対象者の業務上の役割を確認し、
補正してよい `project_members.id` と現在値・補正値を管理者が明示承認します。

承認済み ID だけを次のように transaction 内で補正します。例の UUID や JSON のまま
実行せず、監査結果と承認記録から入力してください。`expected_current` の一致条件により、
監査後に変更された行は更新されません。

```sql
BEGIN;

CREATE TEMP TABLE confirmed_acl_corrections (
    project_member_id uuid PRIMARY KEY,
    expected_current jsonb NOT NULL,
    reviewed_target jsonb NOT NULL
) ON COMMIT DROP;

INSERT INTO confirmed_acl_corrections VALUES
    ('00000000-0000-0000-0000-000000000000',
     '{"read":true,"write":true,"delete":false,"manage_members":false,"manage_settings":false}',
     '{"read":true,"write":false,"delete":false,"manage_members":false,"manage_settings":false}');

SELECT pm.id, pm.project_id, pm.user_id, pm.role, pm.permissions
FROM project_members AS pm
JOIN confirmed_acl_corrections AS confirmed
  ON confirmed.project_member_id = pm.id
FOR UPDATE;

UPDATE project_members AS pm
SET permissions = confirmed.reviewed_target::json
FROM confirmed_acl_corrections AS confirmed
WHERE pm.id = confirmed.project_member_id
  AND pm.permissions::jsonb = confirmed.expected_current
RETURNING pm.id, pm.project_id, pm.user_id, pm.role, pm.permissions;

-- RETURNING 件数と値が承認一覧に完全一致した場合だけ COMMIT する。
-- 不一致、0件、余分な行があれば ROLLBACK する。
COMMIT;
```

## rollback

`alembic downgrade 20260806_0007` は `enterprise_bootstrap_state` だけを削除します。
owner repair や NULL ACL materialization は、既存の明示値と区別できなくなるため自動で
戻しません。ACL 補正の rollback は、backup または承認 transaction ごとに保存した
`project_members.id` と補正前 JSON を使い、同じ optimistic equality 条件付き transaction
で行います。migration downgrade 後に再 upgrade すると bootstrap は未完了状態から
再初期化されるため、運用時間を確保して実施してください。

## Enterprise bootstrap 状態

`enterprise_bootstrap_state` は singleton 行に bootstrap user の安定 UUID と
`completed_at` を保存します。初回 password reset が必要な間は LAN gate を閉じ、変更が
commit された後は完了を永続化します。username 変更や別の reset-required admin 作成では
再ロックしません。DB 読み取り失敗は 503、gate key の欠落・不一致・重複は 403 です。
完了状態の取消しは通常のユーザー更新では行わず、将来専用の管理操作を設ける場合だけ
singleton 状態を明示的に変更してください。
