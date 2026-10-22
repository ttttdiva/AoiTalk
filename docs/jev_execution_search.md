# Browser再計画・PC Bridge対象確認・検索再ランキング

2026-09-23の追加実装。既存Edge/PC Bridge、選択中PC、Jevのprivacy gatewayを維持し、外部エージェントや別の検索アプリで置換しない。

## Browser Agent

`run(plan)` は呼出側が作成した型付きプランの入口として維持する。`run_goal` は、現在の観測をuntrusted dataとして既存の会話LLMへ渡し、残作業を計画する。型付きプランに比べ、生成プランには入力値・URLの出典検証を追加する。type/selectの値は元のgoal中の正確な部分文字列、navigateのURLは開始URLまたはgoalに明示されたURLに限る。空文字への変更は明示的な消去指示に限る。この版は会話添付など別の許可済み値ソースを自動解決しない。

対象なし、対象の変化、未実行が明示されたguard拒否等でのみ、再観測して残作業を更新する。最大2回の再計画、同一失敗・同一状態での打切り、run全体のstep/time予算を守る。再計画を毎step行う方式ではない。実行済みのtarget/action/valueはrun内のsalt付き識別情報で追跡し、生成プランから同じ操作を繰り返さない。誤った繰り返しを防ぐため、同じstable nodeの意図的な再操作も保守的に停止することがある。明示的な型付きプランでの繰り返しは維持する。

送信後のtimeout・接続断・read-back失敗は、再観測して停止する。結果不明な書込みは再計画でも再送しない。PC Bridgeの実WebSocketエラー型も同様に扱う。明示的な「入力だけ・送信しない」要求は生成プランでの送信操作を拒否する。

入力値は再観測・候補判断・返却観測でマスクする。Jevのtarget選択とgoal/stuck評価は、同じ状態から独立に尋ねられる場合に1回のリクエストへまとめる。大きな候補群ではshardの絞込みを先に行い、最終段階だけで進捗を尋ねる。goal_doneはtrace上の参考信号で、完了証拠ではない。実際の完了条件を満たさないdoneはsteps_completedである。

trace/resultにplanner回数、再計画数、step数、Jev/LLM候補判断回数、Jev使用token、判断payload bytes、総時間を追加した。planner・会話LLMのtoken使用量はこの局所metricでは取得していない。各providerの既存利用量記録と混同しない。

通常HTTPページではcrypto.randomUUIDが使えない場合があるため、拡張の観測IDはgetRandomValuesから生成する。HTTP/HTTPSとも同一document内のstable IDと観測refは区別する。

## Windows PC Bridge 1.1.0

Windows実行コードは41内で完結する。80のフォルダをimportしない。Jevはサーバー側の判断だけで、Bridgeへキーを配布しない。

観測にobservation_id、foreground HWND/PID/process作成時刻、UIA runtime ID、role/name/automation ID、geometry、monitor配置を持たせ、入力前に照合する。pointerは対象または子要素へのhit-testを確認し、SendInputのまま実行する。明示的なcanvas座標操作は維持する。

UIA走査は子・隣接要素を逐次取得し、4,000 node/depth32、controlsのUTF-8 payload 512KBを上限にcoverageを返す。既存の先頭190候補への切捨てを削除し、サーバーの有限shard評価へ渡す。上限やproviderエラーは観測不足として扱う。個々のCOM provider呼出し自体を別workerで強制timeoutする機構までは追加していない。

対象名は入力値を返すEditWrapper.window_textではなく、UIAのCurrentNameを優先する。名前がnullの要素も処理する。Value patternが示すreadonlyを尊重し、Valueを持たないText/Document editorは既存の物理入力経路を維持する。既にfocusされた入力欄を再focusせず、Ctrl+A後の選択を不用意に解除しない。キー同時押しは対象layoutのscan codeを付けた1回のSendInputで送信する。

新しいexeのビルドとself-testは成功したが、実行中のルートexeは交換していない。Windows GUIの実機試験は未完了。専用fixtureへの日本語入力、UIA名の取得、対象外foregroundでの拒否は確認したが、全選択後の置換が成立しない試行があった。最後のWS/WSS試行では、fixtureに試験以外のキー入力が入り、前面も変化して完走できなかった。これはPASSではなく、実機再検証が必要な状態である。試験用ウィンドウと試験用exeプロセスは終了する。

`tests/test_pc_bridge_native.py` はビルド済みexeを一時WebSocket/WSSサーバーに接続して実行する。`AOITALK_PC_BRIDGE_LIVE=1`、`AOITALK_PC_BRIDGE_DESKTOP_ONLY=1`、`AOITALK_PC_BRIDGE_TEST_EXE`を指定できる。既存Edge接続の再起動は不要だが、共有デスクトップ上で他の入力・GUI作業を並行させないこと。

## 41だけの検索再ランキング

既存の構造化検索結果を、表示件数で切る前に評価する。OpenAI Hosted Searchの完成回答を解析し直さず、SearXNG等の既存検索経路を維持する。取得上限は表示件数の2倍・最大10件/検索元、評価は公開結果の最大24件/1回、既存Jev質問数/bytes制約の範囲で行う。network timeoutはprivacy reviewの後に5秒、retryなし。評価失敗時は元の結果・順位を返す。

設定は既存の管理者settings APIで操作できる。どちらも既定falseで、production設定は勝手に変更しない。

```json
{"key":"search.jev_rerank_enabled","value":true}
```

これは既存の `PATCH /api/settings` のbody。認証済み管理者が明示的に有効化する。`browser_agent.jev_enabled` は流用しない。JEV_API_KEYは既存のサーバー環境を使う。

SearXNGにはintranet検索を含む構成があるため、公開Webだけを返すインスタンスと確認できる場合に限り、別の `search.jev_rerank_searxng_public` もtrueにする。未宣言のSearXNG、local knowledge、private/project文書、非公開scope、未知のengine、ローカルIP/host、認証情報を含むURLは送らない。公開URLのquery parameterも評価payloadへ渡さない。公開候補同士の位置だけを並べ替え、private/unscored候補の位置は変えない。

元のquery・引用符・コード記号・全角文字・否定条件・引用内の空白を保持する。明示site:/after:/before:と今日/昨日をAsia/Tokyoで解釈し、モデルの関連性値より優先する。取得日を公開日とみなさず、Wikipediaの更新時刻も公開日とは区別する。日付のみ・timezone不明では東京日付を満たす根拠が不足すると表示する。条件外・日時不明の候補は参考として残し、満たしたと偽らない。任意の自然言語期間や新しい検索providerの自動選択は追加していない。

URLのpath/queryの大文字小文字や意味のあるparameterを変えずに重複をまとめ、検索元・元順位・title/URL/公開日をprovenanceとして保持する。local/private結果と公開結果は同一URLでも混ぜない。検索元の部分障害をwarningで伝え、全失敗を単なる0件と表示しない。Jevの関連性値は真偽の保証や校正済みの確率ではない。

## 検証範囲

- unit/契約・既存回帰: `tests/test_browser_goal_recovery.py`, `test_pc_bridge_identity.py`, `test_search_result_ranking.py` と既存browser/search/PC Bridge関連テスト。
- `tests/test_edge_dom_contract.py`: 実Edgeの隔離fixtureによるDOM試験。通常HTTPと遅延/遮蔽/入力差戻し等。普段のログイン済み拡張の更新確認とは別。
- `AOITALK_JEV_REMAINING_LIVE=1` の `tests/test_jev_remaining_live.py`: 現在のlocal modelと実Edgeで通常経路/想定外dialogの2ケースに成功。想定外dialogでは再計画1回、送信1回のまま、総時間9.321秒。通常経路8.058秒。各1回の測定で成功率や速度向上率は主張しない。
- 実Jevのtarget/goal/stuckの3質問も1リクエストで実行し、候補IDと3回答を検証した。入力496/output76 token。
- 実Jevの合成公開検索4件: 関連する候補が3位から1位へ移動、出典集合を保持。1呼出し368ms、入力1,003/output72 token。実検索のランキング品質を大規模に検証した結果ではない。
- production WebUI/更新後の選択PC・普段のログイン済みEdgeを通す一貫したQA、Windows GUI試験の完走、cross-origin/transform iframeの親側確認は残る。サーバー・Bridge・Gateway/Tunnelはこの作業中に再起動しない。

## 参照した実装

Stagehand/Jev Browserの比較SHAは [Browser Agent文書](jev_browser_agent.md) に記載。検索はsuperagents-lab/jev-searchの `67027d0185a9b22eb2a178f0eb15250d12ddabe6` にあるrank/merge/freshnessも読み、独立実装した。外部の重み付け、URL parameter削除、推測日付、追加providerはそのまま採用していない。
