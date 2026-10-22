# チャットから実行する「いつもの手順」

既存のglobal YAML Skillを一度教え、aliases（例:「いつもの散髪」）で呼び出す。
チャットの `manage_skill` は登録・編集・一覧・削除・提案の修正/適用/却下を、
`run_skill` は今回だけの条件、実行、画面handoff後の再開、結果照合を扱う。
「いつものあれ」は実行可能な手順が1件の場合だけ解決する。複数なら一覧から選ぶ。
定期起動、PC offline時のcloud browser、実サイト固有の自動化scriptは追加しない。

## 正本と変更境界

- 正本は `config/skills/*.yaml`、既存SkillRegistry/loader、SkillLearningServiceのproposal。
  明示登録のLearningCaptureRouterが作ったproposalも `proposals` → `revise` → `apply` で使える。
- create/updateはproposalを作る。apply/deleteは既存HumanInteractionManagerの
  PLAN_APPROVALで確認し、proposal内容/現在ファイルのhashを再確認して適用する。
  applyの内容digestは既存DB row lock内でも比較し、承認後のrevision競合を拒否する。
  apply/rollback/direct YAML writerは既存cross-process lockをnamespace→targetの順に取得し、
  公開時にcanonical名前/aliasをNFKC・casefoldで再検査する。並行proposalやreviseで衝突した
  aliasは公開できない。Registryも衝突を拒否し、以前のaliasへの上書きを防ぐ。
- `src/skills/executor.py` はprompt展開を維持する。実行定義のあるSkillは `run_skill`
  へ案内する。SkillUsageReceiptのrender成功を予約成功として報告しない。
- 実操作は既存BrowserAgentService、CandidateDecisionRouter、`resolve_control_bridge`。
  Skill側adapterだけが確定承認・証拠・dispatch fenceを追加する。
  `src/tools/browser_agent.py`、BrowserAgentService、PC Bridge、新Jev専用browserの実装は変更しない。
- `skills.enabled` / `use_tools` が無効なら管理/実行をmodel toolsから除き、dispatchも拒否する。
  実行には追加で `browser_agent.enabled` が必要。system promptの案内も公開toolに合わせる。
  実行途中、承認後、Bridgeへ送る直前にもgateを再確認する。

## 既存parameterを使う実行定義

`parameters.__routine__.default` にversion 1の定義を保存する。
通常のparameter（例: date）は既存のdefaultを使い、`overrides` は今回だけ適用する。
project SKILL.mdにはparametersがないため、この初期対応はglobal YAMLのみ。

架空の受入条件（実サイトの予約には使わない）:

```json
{
  "name": "usual_haircut",
  "description": "架空の散髪",
  "aliases": ["いつもの散髪"],
  "prompt_template": "架空サロンのカットを{date}の10:00、4000円で予約する。{input}",
  "parameters": {
    "date": {"default": "2030-01-01"},
    "__routine__": {"default": {
      "version": 1,
      "intent": "commit",
      "start_url": "https://salon.test/fixture",
      "preparation_labels": ["日付"],
      "commit_label": "架空予約を確定",
      "confirmation": {"target": "架空サロンのカット", "datetime": "{date} 10:00", "price": "4000円"},
      "form_values": {"日付": "{date}", "メニュー": "カット", "アカウント": "架空ユーザー"},
      "completion_text": "架空予約が完了しました",
      "completion_url": "https://salon.test/complete",
      "result_fields": {"target": "架空サロンのカット", "datetime": "{date} 10:00", "price": "4000円"}
    }}
  }
}
```

`intent=check` は読取目的、`commit` は最終確定を含む。
completion_textとresult_fieldsは両方必要。commitではconfirmation/result_fieldsに
target・datetime・priceが必須。完了URLは任意だが、指定時はURLも照合する。
commitではconfirmation/result_fieldsのキー集合と正規化した値を一致させる。
confirmation_labels/result_labelsで表示ラベルを教えられる（既定は対象・日時・税込総額）。
観測本文と選択した確定controlのcontextの双方で `対象: ... | 日時: ... | 税込総額: ...`
のような明示ラベルから値全体を取り出す。DOMの改行が空白になっても次のラベルを境界にできる。
重複ラベル・複数の総額・切詰め・値の不足・追加料金/税抜表記・曖昧な日時は停止する。
金額はDecimalと明示通貨、日時は年/月/日/24時間と明示されたzoneで正規化し、部分一致を使わない。
`4000円` と `JPY 4,000` は一致し、`14000円`・`USD 4000`・通貨不明の `$4000` は一致しない。
価格ラベルは税込総額/税込合計/支払総額(税込)/grand total/total including tax/amount dueに限定する。
完了文は独立した文か、最初の明示factsラベルまでの先頭文として一致させる。
後続のreceiptや任意本文が最後の値と連結する場合は区切りが必要で、解釈を推測して確定しない。
テンプレートは登録parameterの単純な `{date}` 等のみ。属性参照/添字/format指定は禁止。

preparation_labelsは教えられた非確定の入力・選択・クリックcontrolだけを許す。
確定controlとの空白差による重複も拒否する。明らかな購入/同意/確定等のcontrolは
preparationであっても自動クリックしない。未知のcontrol/不足する条件はneeds_userで止める。
この初期adapterは一回の最終確定を扱う。複数の重要操作や未登録の契約同意があるサイトでは、
ユーザーによる画面確認や手順の見直しが必要になる。

## 承認と結果

最終操作の直前に、実ページに対象・日時・料金等があることを検査する。
確認dialogには今回の具体的な値を表示する。承認はuser/session/revisionだけでなく、
plan/action/preview digest、Skill hash、PC/tab、controlの意味、ページ本文、検証済みfacts、
実フォーム値/選択/accountのhashに結び付く。
承認後に再observeし、一意の同じcontrol/同じ内容だけを送る。内容が変われば再承認が必要。
保存確認のprojectionは既存managerの深さ制限内に収め、重要な条件を省略しない。
Webは既存dialogを使い、Nativeは既存permission timelineから同じbindingで応答する。
Native切断中の応答は承認済み表示に進めず、接続後の再応答を案内する。

| status | 意味 |
| --- | --- |
| running | 排他leaseを持つ実行が進行中 |
| succeeded | 最終操作後の新しい観測（checkでは読取観測）が完了文言/URL/全result_fieldsと一致 |
| failed | 確定が実行されなかった等の既知の失敗 |
| interrupted | ユーザー拒否・停止・送信前の中断 |
| needs_user | ログイン、承認待ちtimeout、条件変更、未知controlなど |
| unknown | 送信した可能性があり、完了証拠や結果の永続化を確定できない |

`steps_completed` は受け付けられた操作数。`success` / `completion_verified` はsucceededのみ。
証拠は観測から検証したpublic facts（承認card/actionと同一の値・facts digest）、完了ページURL、
本文hash等で、DOM全体や実フォームのraw値は保存しない。確認用の定義値を結果の証拠として転記しない。
query/fragmentを含むURLをpercent decodeして認証情報を検査し、OAuth token/codeやpassword/OTPが
あれば観測境界でhandoffする。encodedキー/値やnested URLも検査し、planner/trace/DBへ渡さない。
helper refsの解放を検証できなかった場合は、実目的の証拠と別に
`evidence.browser_cleanup_verified=false` を記録する。
見えている文字列の照合は登録された条件に対する証拠であり、未検証の実サイトの予約成立を保証しない。

## request_id / resume / 二重送信防止

SkillExecution（新migration `20261010_0002`）にuser/request_id一意の実行記録を保存する。
context/evidenceは既存field cryptoで暗号化し、Skill hash・override・PC/tabを凍結する。
初回ID省略時は信頼されたuser/Skill/client message IDから生成する。同じrequestは結果を返す。
resumeは同じuser/sessionのIDのみ。日付等を変更したresumeは拒否する。

最終送信前に `commit_dispatched=true` をDBへ保存する。保存できなければ送信しない。
接続epoch断/返信消失/停止/プロセス断後は、送信済みの可能性を保守的に保持する。
unknownのresumeは以前のPC/browser/tabだけをresolve/observe/verifyし、planner・navigate・type・確定を呼ばない。
履歴を使う場合もユーザーがその同じtabを開いてから照合する。

同時実行は既存target file lockに加え、DBのowner/expiry付きleaseで複数hostも排他する。
DB claim/saveはownerを条件に更新し、DB時計を共通の正本にする。期限切れの旧ownerはdispatchも保存もできない。
全体915秒以内、lease 960秒。クラッシュ直後にlive leaseを再開して操作することはない。
user/Skillの未解決実行と、user/正規化した対象/日時の予約fenceにより別request ID/別Skillでも再予約を止める。
日時表記や全角/空白差だけで同じ予約目的が別fenceになることを防ぐ。
unknownと確認済み予約のfenceは保持する。

`run_skill(request_id=..., resume=true)` で明示再開。
送信前の待機をやめて別条件でやり直す場合は `cancel=true`。
中止済みIDは再開不能でfenceを解放する。送信後のcancelはunknownとして照合を求め、
既存の実予約を取り消したことにはしない。

## Bridge担当への接続契約

Bridgeの専用browser優先選択、ログイン画面表示、idle timer、通常browser切替はBridge側が所有する。
確定仕様の「2分idle → 2択 → 10秒無応答で通常browser / 専用継続で更に2分」はSkill側に複製しない。
既存 `resolve_control_bridge(device_id)` の選択結果を使う。

Bridge command responseが `status=needs_user/interrupted/unknown` の場合はplannerへ渡さず停止し、
同じSkill request_idを維持する。`handoff` のstate/browser_backend/next_browser_backend/handoff_idだけを保持し、
password/OTP等の任意payloadは転送しない。専用/通常browser変更は送信前なら再開で受け入れる。
送信後はbrowser変更を証拠として混同せずunknownを維持する。
最終送信が `executed=false` と明確に拒否された場合だけfenceを解除してfailedにする。
接続断でresponseがない場合は `executed=false` と推定しない。

### 確定時の実フォーム状態と原子的な送信guard

Bridge実装は信頼された `supports_material_state_guard = True` capabilityを明示する。
非対応・取得不足は `needs_user` で停止し、承認card/確定dispatchへ進まない。
既存Bridge/extensionおよび新専用browser本体は別担当の境界であり、このbranchでは変更していない。
合成fixtureだけが以下の契約を実装済み。実backendが対応するまで実サイトの確定は停止する。

observeはprivate `material_states[target_ref]` を追加する（モデルへの返却前にadapterが除去する）。
snapshotは `version:1, complete:true, tab_id, document_id, target_stable_id, url, facts_text, controls` のみ。
urlは現在の公開URL、facts_textは対象controlに結び付く全公開確認factsを読み取った本文。
対象・日時・税込総額を含み、global観測/選択controlのcontextと同一factsでなければならない。
controlsは安定ID順で各 `stable_id, label, type, value, display_value` を持つ。
typeはtext/date/time/select/radio/checkbox/account。実DOMの非secret入力・選択肢・送信先account等、
操作の対象と条件に影響する全controlを含める。password/OTP/hidden tokenはsnapshotにも含めない。
対象と全controlを検査できないiframe/切詰め/未知controlはcompleteを宣言してはならない。
display_value全件が教示済みform_valuesと一致することが必要。未教示の値も黙って承認に含めない。

adapterは承認前と承認後のsnapshotを比較し、確定actに
`expected_material_state` とcanonical JSON SHA256の `expected_material_state_digest` を渡す。
Bridgeはdigest・document/target・現在URL/確認facts・全実フォーム状態を検証し、同じDOMタスク内で確定操作を
行う。再observeだけでguardしたり、この引数を無視して実行してはならない。
canonical JSONはオブジェクトのキーを再帰的にソートし、配列順を維持、UTF-8・空白なしで符号化する。
Bridgeは受信したsnapshot本文のSHA256を再計算してexpected_material_state_digestと比較する。
digestだけの改ざん、元digestのままsnapshot本文を変更したpacketは操作前に拒否する。
digest不整合は `ok:false, executed:false, error:material_state_digest_invalid` として返せる。
差分/未対応なら操作せず `ok:false, executed:false, error:material_state_changed` を返す。
フォーム値が同じでも送信直前の対象/日時/料金再計算/ページ移動があれば拒否する。
digest検証、focus/scroll、tick、可視性/identity検査などのawaitを全て終えてから実状態を再取得し、
最後の同期material検査とnative click/submitの間にawait/Promise/timer/別コマンドを挟まない。
async actを呼び出す前の比較だけでは、act内部のtick await中の変更を防げない。
最後の同期検査と実dispatchを接続できないbackendはcapabilityを宣言せず拒否する。
context/evidenceにはsnapshotのhashだけ保存する。raw値はprivate Bridge transport内に留める。

## 合成受入と未確認範囲

`tests/test_skill_routine_execution.py` は実Skill proposal/YAML、HumanInteractionManager、
BrowserAgentService、暗号化DB recordを接続する。opt-in試験は隔離Chromiumに既存extension content.jsを読み、
`salon.test`への全通信をroute fixtureに閉じ、予約POST=1/完了receiptを検証する。
モデル/実アカウント/実サイト予約/実サイト契約同意は使わない。
合成Bridgeの最終guardはtest専用のnode.click hookに置き、既存extensionのfocus/2 tick guard後に
同期再検査→native clickを行う。content.js自体は未変更であり、実backendの対応を意味しない。
HTTPSのroute fixtureで実WebCrypto SHA256を検証し、digest待機中、actのtick中のmicrotask/RAF変更、
digest単独/本文単独改ざんがexecuted=false・予約POST0となることを確認する。
試験は初期比較の成功、変更時点、最終hookの検査回数、native click回数を記録し、
既存helperの通常click到達を省いただけの偽の安全成功にならないことも検査する。

```sh
AOITALK_ROUTINE_FIXTURE_E2E=1 venv/bin/pytest -q tests/test_skill_routine_execution.py --basetemp=.local/skill-tests
```

Chromiumは合成試験で `/usr/bin/chromium` を使用する。試験DB/migration up-downはSQLiteであり、
運用PostgreSQLへのmigration適用はmerge/deploy時に必要。
独立AIの実Chromium component QAでは保存/確定/取消/不整合/狭幅/隣接回帰を確認した。
4件の安全レビューとawait競合修正後は合成87件（実DOM Chromium13条件・確定なしの読取照合を含む）、既存回帰143件、
変更Pythonのruff/mypy（target IOはLinux/Windows型境界も確認）を検証した。
独立QAは厳密facts/機密URL/実フォーム/原子的料金変更/正規化した重複防止66件、
実承認envelope生成1件、Web実dialogとWeb/Native実parserのChromium確認8件を通した。
UI確認のviewportは1366×768と390×844。Console/Network異常は0件。
await競合修正の独立実DOM確認は13条件すべてPASS。実Networkの予約POSTはnormalのみ1件、
他12条件は0件。正常時final_checks/native_clicksは1/1、act待機中変更は1/0、
digest待機中変更/改ざんではact開始前に停止した。これは合成fixtureの検証であり、
production Bridgeの最終dispatch guardの対応確認ではない。
証拠は恒久testと本契約に残し、task scratch/一時DB/起動processは終了時に除去する。
全アプリの認証→チャット→WebSocket、Native実画面/APK、新専用browserとの実接続、Hot Pepper Beautyは未確認。
