# ZIP転送の応答ストリーム寿命検証（2026-10-15）

## 対象と結果

Apps archive GETとFiles download GET／POSTで、上流応答ヘッダー到着後は本文側がキャンセルを所有するよう変更した。既存のmedia preview向け処理を共有し、認証、URL、Range、Content-Disposition、Filesのフォーム→JSON変換を維持する。

- VM上のVitest 2.1.8: **50件成功**（2026-10-15 07:15 UTCに再実行）。
  - `python-api-proxy.test.ts`: 28件
  - `explorer-download-proxy.test.ts`: 14件
  - `download-route-streaming.test.ts`: 4件
  - `downstream-response-lifetime.test.ts`: 4件
- 変更前のproxy／routeソースへ一時的に戻した比較では、同じrouteテストのApps GET・Files GET・Files POSTの**3件が失敗**。既存media previewの1件は成功。変更後は4件とも成功。
- 変更sourceのstrict TypeScript 5.9.3チェックは成功。ただしNext／authの型境界は検証用stubを使用した。

テストは実際のroute、proxy、共有utilityを呼び出し、handler return後のincoming request abortを模擬する。全チャンク保持、準備中の中断、downstream consumerによるキャンセル、上流本文エラーを確認した。使用したバイト列は転送検証用であり、実ZIPの生成・展開を検証したものではない。

## 再実行

VMの作業コピーのrootで実行する。一時harnessは`.local/`に置き、配布対象に含めない。

```sh
node /workspace/shared/73-character-catalog-20261013/node_modules/.pnpm/vitest@2.1.8_jsdom@25.0.1/node_modules/vitest/vitest.mjs run --config .local/vitest-archive-stream.config.mjs
```

当該harnessの構成:

- `.local/vitest-archive-stream.config.mjs`: Node環境、単一fork、上記4テストだけを実行。`@`は`frontend/src`へ解決する。
- `.local/archive-stream-next-stub.ts`: `NextRequest`を標準`Request`＋`nextUrl`、`NextResponse`を標準`Response`＋JSON factoryで代替する。
- `.local/archive-stream-auth-stub.ts`: テスト用の固定userを返す。実際の認証、DB、秘密情報にはアクセスしない。

完全なfrontend依存がある環境では、実Nextを使って以下も再実行する必要がある。

```sh
cd frontend
npm test -- src/lib/server/__tests__/python-api-proxy.test.ts src/lib/server/__tests__/explorer-download-proxy.test.ts src/lib/server/__tests__/download-route-streaming.test.ts src/lib/server/__tests__/downstream-response-lifetime.test.ts
```

## 未確認と適用範囲

実Next.js runtime、実ブラウザ、AVDからの公開経路、実ZIPのcentral directory／展開内容、production配備はこの検証では未確認。stubでの成功を実環境の成功と扱わない。

この検証はキャンセル寿命の回帰を再現・修正したものであり、報告された空ZIPや`archive.htm`の原因がこれだけだったことは証明していない。特定Excelのupload HTTP 503は別の未解決問題であり、このdownload修正による解消を主張しない。
