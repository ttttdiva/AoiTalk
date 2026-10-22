# Operations の scoped Codex CLI 配備

Windows の通常チャット用 Codex と、Operations の repository scope 内で動く Codex は実行環境が異なる。
Windows npm wrapper を WSL/bubblewrap 内へ渡しても、Windows home、node.exe、認証設定へは到達できない。
この制約を host subprocess への fallback や /mnt 全体の mount で回避しない。

## Linux 実行ファイルの契約

- 公式 OpenAI Codex の Linux x64 artifact を、固定版・registry integrity・実行ファイル SHA-256 を確認して repository の cache/agent-runtime/codex/ 配下へ配置する
- deployment 側の AOITALK_CODEX_LINUX_EXECUTABLE に、その実体の絶対 WSL path を指定する
- これは request、顧客 workspace、AgentRunScope の payload から設定する値ではない
- artifact と親directoryはdeployment側が管理し、実行中に置換しない。spawn前にWSL canonical pathを解決し、write/delete/scratch scopeへの包含を拒否する。cacheを含むrepository全体をwrite scopeにする場合は別の承認済みdeployment領域が必要
- backend は指定された実行ファイル1本だけを /opt/aoitalk/providers/codex に read-only mountする。host home、npm package全体、認証情報は mountしない
- scoped CodexCLIBackend と CodexExecRunner は同じ guest aliasを使う。通常の Windows CODEX_BIN と OS PATH は変更しない
- runtime readiness は networkなしの同じnamespaceで --version を実行する。Node toolchainを指定した場合はNodeとCodexを両方検査する
- 未設定では従来のguest commandを維持し、host CLIへのfallbackを行わない。誤ったpath・未配備・起動失敗は利用可能扱いにしない

認証・モデル疎通・CLI native tools の全動作を --version 成功から推定しない。
公式packageの追加resource（code-mode host、rg、内側sandboxなど）が必要な機能は、実行契約と外側sandboxの制約を別途確認する。
この設定で追加resourceや権限が自動的に公開されることはない。

## 認証と検証の分離

scoped guestはHOMEと環境を隔離する。既存WindowsのOAuth token、auth.json、API keyを暗黙にコピー・mount・exportしない。
既存の承認済みbrokerがない場合、provider認証の設計・権限承認が必要。
新しいcredential、grant、persistent accessはコード修正の承認に含めない。

検証結果は次の4項目に分ける。

1. 公式artifactの版・integrity・SHA-256とnamespace内のCLI起動
2. 指定provider/model/effortの認証付き実モデル疎通
3. repository scopeのread/write/command境界と実build/test
4. Strataのhealth、model ID完全一致、および実応答の終了理由

Strataは既存のopenai_compatible_local経路を使う。CLIが未準備でも勝手にAPIや別モデルへ切り替えない。
GPUを共有する実検証は既存のgeneration lease・担当者の実行と調整する。healthだけを実推論PASSとしない。

## 認証を追加する場合の事前条件

公式のheadless認証flowは [codex login --device-auth](https://developers.openai.com/codex/auth/)。
本人が公式ページでsign-inとdevice code承認を行う。既存Windowsのcacheをコピーして代用しない。
保存方式はkeyring（利用不能なら失敗）、file（専用CODEX_HOME/auth.json）、ephemeral（同一processのメモリ内のみ）から明示的に選ぶ。
ChatGPT認証は利用中にtokenを自動refreshするため、この手順に固定日数の失効保証はない。認証の停止・失効方法も承認時に確認する。

現行scoped HOMEは一時領域で、永続credential storeとその安全なbroker接続は提供していない。
新規ログインだけでOperations利用が完了するとは案内しない。新しい保存先・利用process・共有範囲・失効方法を確定し、persistent accessの承認を得てから実装／本人ログインへ進む。
参考: [認証保存方式とrefresh](https://developers.openai.com/codex/auth/#credential-storage)。

## Strata の Operations タスク

`openai_compatible_local` の `qwen3.8-flash-next-iq2_xs` は、タスクの backend に `effort="thinking"` を明示し、通常の `openai_compatible_local.context_budget.response_reserve_tokens` を 2048 とした経路で、実際の file tool 結果から最終 JSON report まで完了を確認した。モードは明示的に選択し、失敗時に別モデルや別モードへ暗黙に切り替えない。

- `read_only` タスクの system prompt は変更作業や未要求の README／成果物を要求しない。明示された goal と completion criteria が終了条件になる
- Strata が tools なし／空、または `tool_choice="none"` の要求に tool_calls を返した場合は、`strata_tool_calls_when_disabled` として停止する。これは provider の契約違反の検出であり、最終回答への変換やタスク成功扱いはしない
- 単発応答、実 tool 使用、最終 report、ファイル不変性を別々に確認する。`finish_reason="stop"`、report の解析成功、実際の goal 達成を揃えて task 完了を判定する
- backend の inline 検証だけでは、アプリの task 永続保存・usage 記録・Web／Mobile 表示の動作は確認できない

2026-10-06 UTC の現行 adapter/保存 runner では、Strata task `00c35e8acc5543b2aca014e18a46b324` の完了、同 owner/model/時間窓の中央 usage 2 行（合計 4,119 tokens）、独立実 Web 確認を実施した。保存 receipt は parsed report/最終終了と file tool 1 回を示すが、raw `finish_reason` は保存されておらず、usage 行には task ID・provider-reported provenance stamp がない。この限界を既存の別 CLI/Strata 読取成功で補完しない。検証用 Strata は正規停止済み。

host 認証を使う現行 inference adapter には、既定の host 指示拒否を維持した一回の明示レビュー経路がある。scoped guest への credential 配備を追加する仕組みではない。本文・OpenAI 送信・trusted-host 前提を本人が具体的に採用するまで実 Codex task を再送しない。API/UI の契約、観測できる source/hash 変更の検出と消費 bytes attestation の違いは [task adapter の記録](operations_codex_task_adapter.md#global-instruction-review-boundary-2026-10-06-utc) を参照する。

2026-10-07 UTC の専用 app-profile 経路は既定無効の source-only 境界であり、上記 scoped guest の認証 broker ではない。固定公式 0.160.0 package/checksum・安全展開・実行前全file照合、Windows私有DACL・non-reparse保存先、digestに結び付いた本人4項目承認と一回限りreceipt、所有stdioでの公式login ID成功通知と同account/workspace照合を具体実装した。通常serverにはowner/root/artifact/connectorを設定しておらず、任意verifier・公開completion・token import・個人認証流用は認めない。起動前のKnown Folder system policy/祖先project configはmetadataで不在を要求し、判定不能・存在時は起動を拒否する。公式proxy/RPCの成功をdaemon所有・起動時storeの証明に代用しない。

配備された場合のWeb接続は、保存先・保持・全6scope・用途/usage・切断効果の明示確認、本人承認、公式リンク、手動完了確認の順に進む。所有者切替は親task表示も失効させる。接続直後は `model_ready=false` であり、同じ所有stdioのモデル・thread・turn契約とowner leaseを検証した後だけtrueになる。taskは認証済みchildを再起動せず、単一readerが制御RPCとtask RPCを分配する。正常終了はthread解除応答を確認して次taskに接続を保ち、取消・期限切れ・protocol異常は所有子を閉じてusage重複を防ぐ。Webでの専用profile使用は、接続確認後の明示選択を要する。選択済みprofileは再確認開始・失敗・切断・取消・失効後も保持し、接続未確認の送信を停止する。所有者別の接続先選択だけを再読込後に復元し、接続の確認結果は引き継がない。hostへの切替は本人の明示選択に限り、profile taskのPOST前にも接続状態と所有者を再確認する。実package/native ACL/provision、配備owner/root/accountの確定と具体条件への本人承認・公式ログイン、その後の実モデル／保存task／usage／独立Web受入が残る。既存host認証・通常runtime・Bridge・Pixelの操作は行わず、source checkpointと接続受入・runtime反映を分ける。[専用profileの契約](operations_codex_task_adapter.md#app-owned-profile-boundary-2026-10-07-utc) と [所有transportのTaskRunner統合](operations_codex_task_adapter.md#owned-transport-taskrunner-integration-2026-10-07-utc) を参照する。
