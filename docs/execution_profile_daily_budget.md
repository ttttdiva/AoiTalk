# ExecutionProfile と OpenAI 日次予算

## 設定の正本と UI

社員の内部モデル割当は既存 AgentTeam schema v3 の
`agent_team.teams.<team_id>.execution_profiles.<profile_id>` を使う。
社員 revision の `agent_team_id` / `execution_profile_id` がその定義を参照し、
Subagent ごとの `overrides[subagent_id]` が `default_route` より優先される。
planner / worker / reviewer を別の quota や別の Team 定義として複製しない。
共有した同じ profile を編集すると、その profile を選ぶ他の社員・session にも適用される。

- Settings → Language Model → Agent Team → Teams → Execution Profiles で
  provider、model、effort、各役割の override を変更する。
- Operations → Agent → 職務・モデル で既存 Team / ExecutionProfile を選ぶ。
- 管理者は Settings → 言語モデル → Base Model → 日次予算を設定 から共有 QuotaPool を開ける。
  Main の provider/model を切り替えず、各行の「予算を保存」で保存・再取得する。
  この入口は既存の quota 保存 API と同じ台帳を使い、権限なしの PATCH は認証・管理者判定で拒否する。
- 同じ Language Model 設定の無料Teamパネルで候補 model / effort、候補有効状態、
  credential/provider の有効状態、日次停止 cap、出力上限を変更する。
  無料Teamの「ルーティングを保存」は routing 用であり、quota の行保存とは独立する。
- AntiGravity は候補の無効化で pool から除外できる。credential の無効化は
  同 provider の明示 Team CLI route も次の実行開始時に停止する。

この仕組みは社員人数、CW/SNS、指揮系統を固定しない。業務組織の設計・社員追加は別の契約であり、
将来の社員も同じ一般の profile と共有 quota を使う。通常 Chat の Main 割当は変更しない。
Standalone CLI embedder が DB 設定を持たない場合は従来の CLI 実行を維持し、設定台帳を遅延初期化しない。

## 上限と送信境界

OpenAI の無料枠は既存 pricing catalog の exact model group と既存 `FreeTeamQuotaPool` に統合する。
新しい社員別・model 別の無料 quota は作らない。

| 既存 quota ID | 共有 model group | 確認済み 1M / 10M 枠の初期停止 cap |
| --- | --- | --- |
| `openai-sol-daily` | `1m`（gpt-5.6-sol 等） | 970,000 token / UTC日 |
| `openai-luna-daily` | `10m`（gpt-5.6-luna 等） | 9,970,000 token / UTC日 |

安全量は最低 30,000。利用 Tier が小さい場合は既存 `FREE_TIER_LIMITS` でさらに上限を下げる。
新規 seed のみ初期値を変えるため、既存 DB の独自 margin、provider、Team/profile は上書きしない。
従来の Luna 1% margin が残る DB では実効 cap が 9,900,000 のままなので、必要なら既存 UI の
日次停止 cap を 9,970,000 に保存する。この操作は安全率を 0、安全量を固定 30,000 にする。

設定欄は既存 quota 行の limit / margin から導く `configured_daily_cap` を再読込する。
現在の利用 Tier を反映した `effective_daily_cap` は表示・予約判定用であり、設定値を上書きしない。
usage API の従来の `daily_cap` は実効値のまま維持し、編集時には使わない。
例えば `tier_1_2` でも設定値 970,000 / 9,970,000 を保存・再読込できるが、
実効値は 220,000 / 2,470,000 に制限され、UI は利用 Tier による制限理由を併記する。
確認済み 1M / 10M 条件の `tier_3_plus` では設定値と実効値が一致する。
アカウントのプラン名から API Tier を変更・推測する処理は追加しない。
外部観測分は設定 cap や実効 cap の書換えではなく残量からの控除として表示する。
これらは既存 DB 設定からの派生応答であり、新しい quota 設定や DB 列ではない。

予約判定は `確認済み実効cap - guard消費 - 未相関の中央usage - 固定external_delta - 予約中` を使う。
provider 観測以降の local 消費を観測値の下へ隠さず、UI の残量も同じ計算を使う。
input / output は cached input や reasoning を含む総量とし、内訳をもう一度足さない。
トークンの事前見積りは全文 UTF-8 byte 数に framing と出力 hard limit を足す保守的な上限である。
privacy の redaction/review 後、SDK に渡す最終 payload の instructions、履歴、tool schema も含める。

明示 OpenAI Team route も同じ guard を通る。出力設定のない既知モデル群には既存候補 seed の
4096 を privacy 前に設定する。既存 `runtime.target_max_output_tokens` があればそれを優先し、
FreeTeam pool は DB 候補の出力上限を渡す。Responses は `max_output_tokens`、
Chat は reasoning を含む `max_completion_tokens` を使う。
title/plain text/memory extraction の直接 helper も実 SDK identity を渡して同じ guard と上限を使う。

候補 → quota の行 lock と比較更新を同じ transaction で実施し、並列 worker の残量競合を止める。
外側の pool 選択 lease は OpenAI token を二重予約せず、実送信ごとに内側の lease を作る。
明示的な retry / fallback も別予約となり、SDK の暗黙 retry は既存設定の `max_retries=0` を維持する。

usage が完全な非負の整数で返れば実績へ精算する。既知 0 は解放できるが、失敗、キャンセル、
接続断、不正/部分 usage、crash の予約は `uncertain` として最大量を保持する。
timeout は provider の停止証明にならない。reaper と 00:00 UTC reset は不明量を解放しない。
日を跨ぐ進行中の予約も新日へ持ち越し、古い finalizer が新しい予約を差し引かない。
同 reservation の再精算は一度だけで、実績が見積りを超えた場合も clamp せず記録する。

CLI の成功・キャンセルとも private な lease 固有の証跡を使う。実同期 thread の終了と、owned process の
exit、および POSIX process group 消滅または Windows owned Job の ActiveProcesses=0 が確認できた
場合だけ concurrency 予約を解放する。async Task 完了、WSL publication finalization、cleanup 完了は
プロセス停止の証明にしない。二重 cancel も thread を shield して drain する。
未確認の custom/scoped tree と API token の不明量は保持する。uncertain の cleanup 再精算では
残存 quota map と settlement version を原子的に更新し、他実行の予約・精算済み metric は触らない。
receipt を使う host Windows lane は CREATE_SUSPENDED → Job 割当 → ResumeThread の順に起動する。
起動後の後付け Job は、割当前に生成された子の停止を証明できないため解放証跡に使わない。
Job の breakaway は許可しない。再開確認に失敗した owned process は停止し、停止が不明なら保持する。
Job と suspended launch の根拠は [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)、
[Process Creation Flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)、
[ResumeThread](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-resumethread) を参照。
POSIX の証跡は owned session group 内を対象とする。group を離れて daemon 化する custom backend や
別 system service の処理、scoped WSL の未確認 tree を停止したと推測しない。

未知の model、provider-managed 過去 response / conversation / item 参照、stream、media/file、
有料組込み tool、output 無制限、headers/query/extra-body の上書きは無料送信を止める。
catalog にない bare alias を snapshot と同じ無料枠だと推測しない。

## 本人確認の受入れと実行資格情報の照合

利用者本人による既存キーの適用資格・組織所属・Project 登録の確認を受け入れる。
追加の資格調査、モデル試行、auth grant、data-sharing opt-in は行わない。
管理者は Base Model → 日次予算を設定 → 確認済み無料枠の記録から、既存キー参照、
確認済み請求組織scope、無料枠（1M / 10M または250K / 2.5M）、本人申告の出典メモを保存する。
これは既に確認した対象資格・参加状態の記録であり、新しい資格調査やprovider操作ではない。
キー本体を入力する欄はなく、現在の資格情報の非secret SHA-256 fingerprint、SDK Project、接続先を表示する。
同じキー・無料枠の事実を保存するだけで、資格情報の利用目的・権限を拡張しない。

正本は既存 `app_config_settings.global` 内の `openai.complimentary_verification`。
新規quota・credential・設定tableは作らない。`/api/free-team/openai-confirmation` の GET / PUT / DELETE は
署名付きNext Web Cookieだけを既存AuthMixinで再検証し、現在のactive管理者・session世代・reset状態と安定UUIDを確認する。
FastAPI Cookieが混在してもNext本人のsubjectを優先し、記録actorとの一致を必須にする。
無効・重複Next Cookieから別Cookieへfallbackしない。
内部キー・転送marker・BearerやFastAPI Cookieだけ、agent principal、auth無効モードでは記録できない。
Mobileにもcanonical APIの型を生成するが、Bearerからの自己承認は許可しない。この管理操作は本人Webセッションで行う。
グローバル資格情報に既存owner列がないため、最初に記録した管理者UUIDを記録ownerとして固定する。
以後は他の管理者も記録を閲覧・上書き・失効できず、失効してもownerを保持する。

`owner_user_id`、操作actor、記録UTC時刻、record UUID、`source_kind=owner_declaration` とfingerprintはserverが決める。
clientはactor・時刻・provider attestationを指定できない。本人申告はprovider自動attestationではなく、
自動確認adapterは実装していない。記録時刻は事実をローカル保存した時刻で、providerが確認した時刻ではない。
署名付き本人セッションで行われたHTTP操作から、物理的な手操作と同じCookieを使う自動操作は識別できない。
Cookieを持たない内部/agent/token経路による自己承認を拒否する境界である。

保存はglobal row lock内で現在のキーfingerprintと読込時record UUIDを比較する。別操作によるキー/記録変更は409。
scope、tier、ローカル確認flag、確認記録を一つのtransactionで保存し、秘密値・Main/provider/Teamを保持する。
保存時点のSDK organization/projectと公式API endpointを束縛し、実送信時にもSDK identityを照合する。
通常設定APIでキー・接続先・scope・tier・確認flagを変更すると同じ設定行lock内で失効する。
無料Teamキーのrotationもglobal config → credential順でlockし、同じtransactionで記録を失効する。
元の値に戻しても復活せず、同じownerが新しい事実を明示保存する必要がある。
環境変数によるキー/Project/org変更は毎回の現在binding照合で停止する（環境変数の変更履歴は保存しない）。

予約はcallerが古いlegacy snapshotでも最新global行を先にlockし、現在のmanaged確認記録と失効を優先する。
古いSDK・worker設定が失効記録を回避できない。記録の失効/変更は既存消費や不明予約を解放しない。
確認記録は任意の再確認期限を追加せず、未確認・設定不一致を有料実行許可へ変換しない。
中央usage・全組織消費の集計と既存privacy/ACLは従来どおりで、外部同組織アプリの同時消費は観測できない。

## 中央 usage・制限・適用

既存 TokenUsage の owner / user / session / project と ACL はそのまま維持する。
`quota_reservation_id` を付けて guard の精算済み消費と中央 usage を相関し、同じ使用量を二度引かない。
当日の同一組織・model group の中央 usage は owner を跨いで合算する。
当日 `default` / 空 / null scope の usage がある場合は、組織帰属を推測せず停止する。
quota の scope が別組織だった場合や、既存消費の帰属が未確定の場合も停止する。

外部の同一組織アプリによる同時消費はこのローカル台帳から見えない。
手動 sync の numeric 観測は coverage の証明がないため全量を external_delta として保守的に加算する。
`{"usage": 900000, "included_through": "2026-10-07T12:00:00Z"}` の object は provider がその時点まで
取り込んだことを確認できる場合だけ使う。取得時刻を lagging snapshot の coverage として送らない。
その UTC 日・coverage 内の監査済み guard 実績と中央の未相関 usage だけを初回観測から引き、
重複差分を固定する。同日 external_delta は減らさず、重複・逆順の低い観測を無視する。
旧 DB 観測や証跡のない古い local 実績の重複は推測せず、その UTC 日は保守的に両方数える。
UTC reset は観測 state をリセットするが不明な予約を保持する。同期と予約は version と行 lock を共有する。
これはリアルタイムの全組織保証にはならない。
この限界と不明量の予約保持を UI に表示する。

公式条件の正本は [OpenAI のデータ共有・無料 token FAQ](https://help.openai.com/en/articles/10306912-sharing-feedback-evaluation-and-fine-tuning-data-and-api-inputs-and-outputs-with-openai)。
対象の組織/Project、利用 Tier、00:00 UTC、共有 model group、上限を跨ぐ request 全体の課金、
tools 等の除外は運用側で確認した条件を使う。

source の適用には Alembic `20261007_0001`（中央usage相関の nullable UUID/index）、
`20261007_0003`（観測 watermark/version・精算 version）を通常の起動時 migration へ渡す。
並行開発の社員 delegation migration `20261007_0002` の後へ接続し、revision ID の重複を避ける。
この開発作業では live/local DB migration、worker 設定変更、runtime restart/取り込みは実施しない。
移行前や中央 usage DB が未準備なら API は送らない。

## 検証範囲

確認記録は専用 SQLite / 隔離 PostgreSQL fixture で、通常保存・非secret readback・owner固定・
stale record競合・scope/key変更の失効・旧worker snapshotへの反映・直接API profile予約・cap拒否を確認する。
失効しても不明usageの予約と既存消費は保持される。署名Cookieの検証は現物AuthMixin / UserRepositoryと
fixtureユーザーで、内部偽装・Bearer・別owner・member・agent・active/reset/session世代を対象にする。

確認記録UIは独立 Chromium + PostgreSQL 17 + 現物署名Cookieの fixture でも PASS。
1M / 10M本人申告の保存 → hard reload → API profile予約成功 → cap到達拒否 → scope変更失効を確認した。
無料Teamキー参照の保存・再読込と、実効環境キーのfingerprint変化による停止も確認した。
other-admin / member の GET / PUT / DELETE 403、匿名・無効Cookie偽装401ではDB状態が変わらなかった。
Main / Team / Routingを保持し、Console / failed network / 外部送信は0。
画面shellの周辺APIはfixtureで、本番Next→Python転送境界は専用proxy単体テストで別に検証する。
検証の全DB入口はfixtureへ束縛し、通常runtime managerの取得をfail-fastで拒否する。
既存runtime・利用者DB・credential configの適用、provider操作・実モデル呼出しは行っていない。

専用 SQLite / fake SDK fixture で並列境界、共有 owner、retry/cancel/crash、不明 usage、
日跨ぎ、UTC offset、資格情報変更、API cap/model/provider 更新、privacy 後の wire、直接 native route を確認する。
既存 ExecutionProfile、privacy、中央 usage、native/specialist、pricing の回帰も対象とする。

quota 保存・再読込の API fixture は PostgreSQL でも実行できる。
明示した `AOITALK_TEST_QUOTA_POSTGRES_URL` の `aoitalk_test_` database だけを許可し、
テストごとの新規 schema を成功・失敗にかかわらず削除する。通常 DB や `.env` へ接続しない。
両 Tier で設定値と実効値を分けて再読込し、実効境界を超える予約が引き続き拒否されることも確認する。

設定値・実効値の分離は新品 PostgreSQL 17 と source の quota API / ORM に接続した
独立 Chromium でも保存 → 画面再読込が PASS。
両 Tier の設定値 970,000 / 9,970,000、tier_1_2 の実効値 220,000 / 2,470,000、
tier_3_plus の実効値 970,000 / 9,970,000、安全率 0 / 安全量 30,000 を確認した。
再読込後に cap を編集せず保存しても同じ設定値・実効値・残量が保たれた。
Main / provider / Team / routing profile を保持し、member の PATCH 403 / anonymous 401 も確認した。
画面の周辺 API・認証は fixture で、既存 runtime と利用者 DB は対象外。実モデル呼出しは 0。

独立 Chromium QA は実 FreeTeamSettingsPanel + mock fetch/localStorage の 7 フローで PASS。
モデル/effort・AntiGravity無効・cap の保存/再読込、422 復帰、残量/予約/UTC reset/停止理由、
隣接 routing と確認済み表示を検証した。page error / request failed は 0、console は fixture の favicon 404 のみ。
Next shell・本番認証・実 provider はこの fixture の対象外。
mock での model 保存成功は、実 API の exact catalog/group 許可を証明しない。

観測後の消費修正は独立 Chromium QA の追加 3 フローでも PASS。
provider 観測 900,000 + local 50,000 を使用済み 950,000 / 残量 20,000 と表示し、
cap 960,000 保存・再読込で残量 10,000、予約 500 の別表示で残量 9,500 と確認した。
Provider 無効の停止理由・UTC reset も表示された。再検証では page / console / failed network は 0。
Windows の suspended / Job / resume / Query は fake WinAPI fixture で確認し、実 Windows 実行は未実施。
