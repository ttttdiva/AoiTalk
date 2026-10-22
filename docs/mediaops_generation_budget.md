# MediaOps 子生成の親費用予約

AgentWork からの Automation / GenerationPlan submit は、DB の WorkItem source と
固定 AgentRevision を照合して、親 root の既存 `max_run_cost_micros` /
`max_daily_cost_micros` と `currency` を子生成へ適用する。上限額の新しい既定値は設けない。
親子の project と parent/root chain が一致しない場合は submit しない。

見積は所有者が固定した RecipeRevision の `cost_policy`、Automation revision の
`generation_action.cost_policy`、または WorkItem の既存 `budget_reservation` から読む。
金額契約は `estimated_cost_micros`（非負の整数、bool 不可）と `currency`（大文字3文字）。
Recipe / Action に金額があればそちらを使う。親費用上限がある場合、見積・通貨の欠落、
不一致、不正値は submit 前に拒否する。quota の units / amount を金額へ換算しない。
明示された 0 は許可するが、非従量フラグから 0 を推定しない。
既存の従量生成承認は費用予約とは別に必要。

金銭予約は root WorkItem の server-owned `metadata.media_generation_costs` に保持する。
AutomationRun の image slot / GenerationPlan ごとに request hash、外部 idempotency key hash、
origin WorkItem、親 Agent/revision、通貨、見積、予約UTC日を固定する。同一 slot の再試行で
加算しない。domain の submit intent と同じ transaction で予約を commit し、commit 前の
拒否・CAS敗北・rollback では金銭予約を残さない。generic quota 台帳は金銭集計に含めない。
台帳は Work 再探索、候補再生成、承認待ち、再試行、reconcile を跨いで保持する。

PostgreSQL では同 Agent の transaction advisory lock と root 行ロックにより、別 root を
跨ぐ同日予約を直列化する。日次は予約時の UTC 日に固定し、reconcile で翌日へ移さない。
同日・同 Agent の異通貨での非ゼロ予約は換算せず拒否する。run上限は root 全体の累積値。
reserved と spent はどちらも上限に計上する。安全なJSON投影の上限64 slot、または台帳の
不整合に達した場合も新規 submit を拒否し、古い予約を切り捨てない。

成功した固定runのreceiptで spent とする。transport例外、不正receipt、失敗、取消、
quarantined、unavailable は費用が0の証拠にならず reserved を保持する。73の公開画像／
Preset APIには実請求の金額hardcapや未実行・返金の証拠契約がないため、この変更は
41の**見積予約による admission**であり、73の実請求額上限を保証しない。未実行と確定
できる41内の submit 前拒否だけが予約を残さず終了する。未知の73 fieldは追加送信しない。

public API / OpenAPI schema の変更はない。内部 ExecutionAdapter が origin WorkItem ID
だけを service に渡す。従来の直接 human submit、quotaだけのsession、旧adapter signature
は維持する。費用上限のない AgentRevision へ新しい金額を自動設定しない。
