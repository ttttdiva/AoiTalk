# Mobile Docs同期 scale harness

## 目的

本番形状（Docs node 約160,000件、edge 約108,000件）を個人データなしで再現し、
同期中にJS heapへ全snapshotを展開しないことを回帰検証する。

`mobile/src/sync/docs-scale-fixture.ts` の `createDocsScaleFixture` は、既定で本番形状の
1%（node 1,600 / edge 1,080）を生成する。行とページはiteratorで遅延生成され、
`pageSize` 件を超える配列を保持しない。

## 同期のbounded境界

- APIページのstaging書き込みは `DOCS_STAGING_WRITE_BATCH_SIZE=256` 件ずつ別transactionへ
  commitする。途中失敗時はrun cursorを進めず、次回pullで同じpageをupsert再実行できる。
- atomic promotionはlive行・membership・scope/digest・run完了を従来どおり1 transactionで確定する。
- production pathではstaging全行をengineからJS配列へ渡さず、table/entityKeyのkeyset cursorで
  `DOCS_STAGED_PROMOTION_BATCH_SIZE=256` 件ずつ読み取る。
- promotionが返す `DocsSyncPromotionTelemetry`（source / rowsRead / batches / maxBatchSize）は、
  `DocsResyncProgress` の任意フィールドへ転送される。

## targeted検証

```powershell
cd mobile
npx jest --runInBand src/sync/__tests__/docs-scale.test.ts `
  src/repositories/__tests__/docs-scope-membership.test.ts
npx jest --runInBand src/sync/__tests__/engine-staged-sync.test.ts
```

`docs-scope-membership.test.ts` は513 staged rowsを投入し、256/256/1の3 batchと
`maxBatchSize <= 256`、live rows全件を確認する。

## 実測上の注意

この変更でstaging payloadの全配列化は除去したが、atomic ACL reconciliationの正本である
`authoritative_ids` と既存membership/ref集合は現状 `Set` としてpromotion transaction前に
読み込む。160k級での実heap、SQLite transaction時間、Android process death/ANRは、親workstreamの
emulator/実機QAで telemetry と logcat を採取して判定する。必要なら次段でmembershipの
set-based SQL化を別設計として検討する（今回の変更ではschema migrationを行わない）。
