# PC BridgeのBrowser AgentとJev判断

一般Browser Agentは ポータブルPC Bridge で選択したPCの既存Edgeを操作する。サーバーPC固定のローカルPythonホスト方式は廃止した。

`browser_agent(goal, start_url?, tab_id?, device_id?, completion_text?, completion_url?)` は、接続中PCをユーザー設定から解決し、Bridge同梱のextension不要な `agent_browser` helperで明示選択した既存Edge targetへDOM観測と操作を送る。`start_url` は選択target上のnavigationであり、別tabを暗黙に作らない。Cookieや保存パスワードをコピーせず、普段のログイン済みセッションを使う。選択PCまたはtargetが利用できなければ停止し、別ブラウザやサーバーPCへfallbackしない。

Jevは現在の候補からの選択を担当する。通常の `agent_browser` 方式ではJevのtimeout・接続失敗・候補判断失敗時にfallbackせず、操作を停止する。専用ブラウザでは既存BrowserAgentServiceのJev／会話LLM判断を使う。判断providerの切替で操作するブラウザを変えない。同じ判断層をWindows Computer UseのUI Automation候補にも使用する。

内部QA BrowserとChatGPT Directorは別機能として維持する。専用ブラウザのログイン引継ぎと最終操作確認は、以下の契約に従う。

設定の `browser_agent` は enabled / jev_enabled / dedicated_enabled / max_steps / timeout_seconds / jev_model。`dedicated_enabled` の既定値はfalse。Jevキーはサーバーの既存 `.env` の `JEV_API_KEY` を使用し、Bridgeの接続設定やブラウザへ渡さない。

## 専用ブラウザと手動ログイン

`browser_agent_dedicated(goal?, resume_run_id?, start_url?, tab_id?, device_id?, completion_text?, completion_url?)` は選択PCのBridgeが所有する専用profileだけを使う。通常操作は `agent_browser`、X／YouTubeの素材準備と転送は `managed_profile`、専用操作は `dedicated_browser_` commandに固定する。失敗fallbackで別のbackendへ移らない。通常browserのreadinessとmedia profileのreadinessは独立している。

OFF時は新規tool登録・schema・promptから専用操作を除き、保持済みtool、API、Bridgeの自動操作も拒否する。OFF通知を受けたBridgeは専用browserを閉じる。ただし認証引継ぎで一時停止中のrunは、同じowner・接続epoch・tab/contextを保持して自動操作権だけを停止する。OFF中に許可するcommandは、その既存runの解放を確認する `end_run` / `cancel_login` だけで、新しいprofileを開かない。明示取消またはowner・接続epochの変更で保持runを閉じる。日常Skillの同request再開は保持backendを変更せず、OFF中は再開可能な停止状態を返す。ON復帰後も同runを再観測し、確定操作には新しい承認を必要とする。専用driverのprotocol、command一覧、checksumと固定Playwright runtimeが一致しない旧bundleは専用capabilityを広告しない。通常helperを同梱しない配布候補も通常browser対応とは扱わない。

ログイン画面を検出すると、観測や入力を停止し、同じPCの同じtabを表示して「この画面でログインだけお願いします」と案内する。パスワード等の入力値はモデル・チャット・ログへ渡さない。tab、context、可視状態を毎回確認し、ログイン完了を検出するか、利用者が明示的に再開するまで待つ。UIが利用できない場合は停止する。タスクの指示・plan・完了済みcursorはowner／session／runに結び付け、Windows DPAPIで暗号化して保存する。実行結果が不明な操作を自動で再実行しない。

2分操作がなければ「専用ブラウザで続ける／普段のブラウザへ切り替える」を提示する。操作があれば待機期限を延長し、専用で続ける選択は2分をリセットする。10秒回答がなければ切替を試みる。最初に専用browserの実行権を停止し、同じPC・接続の通常helperの既存readinessを確認する。普段のブラウザが未接続・利用拒否なら再接続せず停止する。対象アカウントと作業状態の別の利用者確認を通過してから通常操作を続ける。送信・購入・予約確定等は別の最終確認を必要とし、待機タイマーで確定しない。

`resume_run_id` は同じ専用tab／contextを保持したサーバー側service再作成からの再開に対応する。Bridgeのprocess再起動でcontextが変わった場合は、保存タスクを保持したまま停止する。新しいcontextでの作業復元、実Studio、実Jevの速度比較、今回の正式frozen配布は未検証である。隔離された合成画面の実DOM検証を、本番のログイン済みブラウザの成功として報告しない。

インストール、通信、操作先選択、exeのビルド、検証結果・未検証範囲は PC Bridgeガイド を正本とする。

## 2026-09-23: 対象選択と実行結果の強化

### 採用範囲

Stagehand型の文脈付き候補選択・候補分割、jev-ultrafast型のstable IDと観測IDの
分離・操作直前確認・必要な状態の待機、jev-browser型のproposed/executed/verification
の区別を、既存Edge拡張とBridge契約の内側に実装した。外部パッケージの実行時依存、
新しいbrowser所有モデル、replay cache、強制クリック、先頭候補への代用は追加しない。

候補routerは判断単位の `last_decision_fallback_reason` とrunの
`circuit_open_reason` を区別する。low-confidence/noneの後も次の判断でJevを使う。
429/timeout等はRetry-Afterを考慮する有限cooldown（最低10秒、3回失敗でrun内停止）。
認証・設定等の失敗はrun内停止を維持する。privacy拒否は別providerで回避しない。
`fallback_reason` は互換用に残す。

DOMの候補には、行/カードの文脈、見出し、role/label、enabled、document/frame、
stable IDを付与する。stable IDと新しい観測のtarget refは別物であり、
別観測のrefや同一nodeのラベル・href・行文脈変更は操作前に拒否する。
候補番号を並べ替えても元の実行対象への対応は維持する。

### 有限の観測・判断予算

| 段階 | 現在の上限 |
| --- | --- |
| 1 document走査 | 12,000 element nodes、2,048候補、約420KB候補データ |
| 全frame統合 | 4,096候補、約600KB候補データ |
| native select | 2,048表示optionまで。件数と省略フラグも返す |
| router | 4,096候補、最大16 shard、各200候補以下 |
| Jev向けshard | state/指示を含める24KB検証に収まるよう22KBから逆算 |

予算不足を `coverage.truncated` 等で明示し、不完全な観測の部分的な勝者を
そのまま操作しない。各shardを評価した上で元の文脈を保ったfinalistsを比較する。
小さい画面では通常1回の候補判断。大きい画面では判断回数が増え得る。
語彙shortlist/並列batchを含むさらなる最適化は未実装であり、高速化保証はない。

### 操作と結果

connected/disabled/aria-disabled/inert/readonly、scroll/focus後のgeometry、
viewport内の複数点hit-testを確認する。覆いが残れば待機上限後も操作しない。
open Shadow DOMを維持し、同一originの親iframeによる遮蔽も確認する。

**制限:** cross-originの親frameやtransform付きframeで親側hit-testの根拠が
得られない場合は `edge_parent_frame_unverified` として操作を止める。
この経路の完全な親frame attestationは未実装。閉じたShadow DOMの内部も観測不能。
観測できないものを成功とは扱わない。

type/select/check/uncheckは端末内で値・選択状態を読み戻す。生の値ではなく、
一致/不一致・検証種類・target ref・時刻を返す。contenteditableは選択範囲と
ブラウザ入力編集経路を使い、単純なtextContent置換はしない。
typeだけの操作にEnter/submitを付け加えない。optionが無い/曖昧なら停止する。

clickはdispatch成功と業務完了を分ける。同期SPA変化が見えれば不要なpopup待機を
省くが、その変化だけで `completion_verified` にしない。
`action_proposed` / `action_result` / 観測時間・件数・coverageをtraceに残す。
期待するtext/URLが無いstepの無条件再観測を省き、必要なstepと最終結果で観測する。

timeout/接続断/実行済みread-back失敗は、先に有限の再観測を行って停止し、
同じ送信や次点候補を自動実行しない。未実行が明示されたstaleだけ1回再観測・再選択する。
`success`、`steps_completed`、既存の完了条件による `completion_verified` は区別を維持する。
任意の業務目的をモデルのdoneだけで客観的に証明する機能ではない。

### 検証方法と境界

```powershell
$env:AOITALK_DOM_FIXTURE_E2E = '1'
.\venv\Scripts\python.exe -m pytest tests/test_edge_dom_contract.py tests/test_browser_action_outcomes.py tests/test_browser_agent_integration.py tests/test_candidate_decision_service.py tests/test_jev_decision_service.py tests/test_edge_browser_bridge.py -q --basetemp=.local/jev_pytest
node --test tests/edge_background_contract.cjs
.\venv\Scripts\python.exe tests/benchmark_edge_dom.py --baseline ec68c7e1bf1f21bd4d6b594bbb8008c11c315183 --trials 5
```

DOM試験は同PCのEdgeを**隔離したheadless fixture**として使う。ログイン済みの普段の
Edge/Bridge接続をこの試験で検証したとは主張しない。全frame統合/popup/document IDは
Chrome境界mockによるbackground.js試験、Jev判断はfakeと実validatorの契約試験である。

変更前後5回の320行fixtureでは、候補100→320、覆い背後の誤クリック5→0、
差し戻された入力の成功扱い5→0。最初の比較でDOM観測往復中央値は8.13→32.66ms。
範囲拡大と確認追加による観測コスト増であり、planner/Jev/LLM/通信を含めた
総所要時間の改善率・本番成功率ではない。少数試行からp95を主張しない。

### 追加実装と配布状態

観測ベースの残作業再計画、Jevのtarget/goal/stuck質問統合、生成入力値とURLの出典確認、
Windows PC Bridgeのidentity/coverage強化、41検索の独立opt-in rerankを追加した。
仕様・設定・実モデル検証・未完項目は [実行・検索の追加実装](jev_execution_search.md) を参照。

新PC Bridge 1.1.0の正式候補はonefile/runtime、self-test、固定helper、notice/hash gateを通過している。専用GUIの接続・切断・再接続、Nativeの日本語desktop操作、Chatの同じdeviceへのfocus確認も完了している。
実EdgeのCDP attach、JEVの候補選択、snapshot/ref、click/fill/select、Browser経路の本番WebUI完走は保存済みBrowser Use permission blockのため未検証である。候補の公開、稼働中exeや普段のEdge拡張の交換とは別に扱う。
cross-origin iframeの親側確認、広範囲の検索品質・性能評価は残る。
実行中の80/Gateway/Tunnelは再起動していない。ソースのpushとruntimeへの適用は区別する。

### Decision Receipt・shadow・完了レビュー

`CandidateDecisionRouter` は `decision_receipt_service.py` のReceiptを返す。
stateと候補内容はSHA-256 digestにし、本文・DOM・credentialをReceiptへ保存しない。
候補ID、Jevの分布・信頼度・閾値、engine、fallback理由、latencyを保持する。
分布の対象IDと `probability_scope` を併記し、shardのfinalist分布を全候補の分布と混同しない。
`selected_candidate` は提案、`host_candidate` / `host_action` / `outcome` はHostの実際の採用と結果である。
Tool Packの再検証とload、Browserの既存trace、Computer Use、Free Teamのlock・lease・使用確定へ接続する。
explicit commandによるTool PackのloadはHost判断のままで、Jev receiptを生成しない。

Free Teamは `decision_layer.jev_free_team_routing_enabled: false` でdisabled、
`true` でactiveになる。さらに `decision_layer.jev_free_team_shadow_mode: true` にすると、
Jevの選択を記録しながらdeterministicな順序を維持する。既定はactive（shadowはfalse）。
baselineとshadowの候補・一致を記録し、実際のleaseは従来のrow/quota lock下のHost検証で決める。
`local_only`、明示した単一候補、候補不足ではsemantic呼出しを行わない。

`decision_layer.jev_completion_verification_enabled: true` で既存agentic completion loopの
追加semantic reviewを有効にする（既定false）。同期・非同期ともdeterministic override後の
`done` にだけ適用し、0.85以上の確信で明確な不足を検出した場合はHostが`continue`へ下げる。
`continue`を`done`へ昇格させず、権威ある完了証拠によるfast-pathも維持する。
1 run最大2呼出し、network timeout 5秒、retry 0回で、既存continuation予算は増やさない。
同期loopが動作中のevent loopから直接呼ばれた場合は追加signalを省略し、privacy承認の配送を塞がない。
request/responseは秘密情報除去・UTF-8 byte制限後に既存OutboundPrivacyGatewayを通す。
会話履歴やtoolのraw arguments/resultsは送信しない。失敗・棄権・低確信・privacy拒否は既存review判断を維持する。

`recent_decision_receipts()` は同一プロセスの直近最大2048 decisionの最新状態を返し、
`summarize_decision_receipts()` で件数・kind・outcome・一致/不一致・一致率を取得できる。
ログの `decision_receipt` はdecision_idで更新を結び、集計では同じdecisionを重複計上しない。
永続DBやDashboardは追加していない。Receiptの確信度はモデルの出力値であり、校正済みの精度保証ではない。

### 日常Skillの実フォーム確定guard

専用 owned profile は、固定manifestでdriverとDOM controllerの実bytesを検証したbundleだけが
`dedicated_material_state_guard: true` と `dedicated_material_state_guard_version: 3` を広告する。
可視controlの送信対象判定にはfieldsetから継承するdisabled状態も含める。
最初のlegend内の例外はブラウザの`:disabled`判定に従う。旧version 2は対応済みとして扱わない。
versionがない旧bundleやversion 1は非対応として停止する。サーバー側の同じ接続に固定された
`RemoteDedicatedControlBridge.supports_material_state_guard` が日常Skillのprivate境界へ伝わる。
通常の extension不要 `agent_browser` helper は非対応のままで、確定前に `needs_user` で停止する。
別ブラウザへのfallbackやextension経路の復活は行わない。

private snapshotは日常Skill契約の対象・document・URL・公開facts・非秘密control値を持つ。
digestはUTF-8 canonical JSONのSHA256を受信側でも再計算する。focus/scroll/tickを終えた後に
実DOMから再取得し、同期比較からnative clickまでawaitを挟まない。値・日時・料金・送信先・
documentが変われば `executed:false` で拒否する。hidden項目のname/value/disabled/orderも、
isolated world内だけに原本を保持し、document-localの非公開salt付きSHA256をsnapshotへ含める。
hash計算待ちの間に原本が変わった場合や上限を超えた場合はcompleteを宣言しない。
最後の同期照合ではhidden原本の一致も確認し、CSRF値やhidden値をIPC・model・logへ返さない。
一般planner/traceへsnapshotを返さず、
Skill recordのcontext/evidenceにはhashのみを保存する。

一般専用ツールも最終確認前に同じprivate snapshotを固定し、公開factsと非秘密control値を
確認画面へ提示する。承認時のstate/digestをactへ渡し、guard非対応・不完全・通常helperへ
切替済みの確定操作は `browser_final_material_guard_required` で停止する。
ログイン中断時は未送信のdecision receiptを終端化してから再選択する。resumeではbackendが
切り替わっても保存cursorと実行済みreceiptを保持し、完了stepを再送しない。

現在completeを宣言できる対象は、top documentにある単一の検査可能なnative formのsubmit control。
iframe、shadow root、未知のcustom control、複数form、多選択、切詰め、秘密入力がある場合や、
全control/公開確認factsを取得できない場合はcompleteを宣言しない。通常helperでの確定、
独自SPAフォーム、実アカウント/実予約、正式frozen配布の受入は未確認・未対応範囲として残す。

`tests/test_dedicated_material_state_guard.py` は実owned Job/driver/controllerと隔離native formで、
一回の確定、digest改ざん拒否、待機中のtimer/microtask/RAFによる条件変更を検証する。
`tests/test_dedicated_routine_guard_integration.py` は固定日常Skill `cc35b5a` と結合した検証checkoutで、
専用bridgeを直接注入した実Skill adapter・HumanInteractionManager・Hub・Bridge dispatch・owned DOMを接続する。
このfixtureは本番Skill resolverやWebUI/DBの統合受入を確認するものではない。
合成ページ以外へ確定操作を送らない。単体/合成受入の成功を本番や正式配布の成功に置き換えない。

### 比較に使った一次ソースの固定版

直接コード移植ではなく、上記の原則に沿った独立実装。各repositoryのLICENSEはMITと確認。
閾値や外部作者の速度主張はこの環境の測定値として用いない。

- Stagehand PR2994: base `4c8f2381e058d0d9fb380d9e753541a7cd8864b8`、
  head `4298bb40fba16fff9d80310515cccd104cd37300`。`jevAct/pick.ts` の分割・文脈照合を参照。
- browser-use/jev-ultrafast: `1231850a0bf1a0c0341fe408ef1668dbbfdfac46`。
  `snapshot.js` のstable ID/観測の分離を参照。先頭250件制限は採用しない。
- jkudish/jev-browser: `f731c7aa773f55a30dad7175bbb358b2d8529e57`。
  `questions.ts` を参照。goal/stuck回答は客観的完了証拠としては採用しない。
- superagents-lab/jev-search: `67027d0185a9b22eb2a178f0eb15250d12ddabe6`。
  rank/merge/freshnessも追加で確認。41既存検索への選択的導入範囲は上記の追加実装文書を参照。
