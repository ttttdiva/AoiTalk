# Staged personal migrations

通常autoload (`alembic/versions`) の現在のheadは `20261023_0001` (task API operation receipts)。このディレクトリは通常の Alembic `version_locations` と起動時の対象に含まれず、ここにあるrevisionはlive DBへ適用されていない。DBへの適用は別途承認された手順が必要。

元のpersonal chainは変更せず、`20261022_0002` -> `0003` (reminder drafts/selections) -> `0102` (career) -> `0201` (finance datasets) -> `0104` (receipt candidates) -> `0105` (private reminder lifecycle) の順に続く。

staged-only merge `20261024_0106` は、このchainの `20261022_0105` と現autoload head `20261023_0001` を結ぶno-op revision。staged directoryを明示して読み込んだ場合にcombined graphを単一headにするためのsource-only migrationであり、通常autoloadや自動起動からは読み込まれない。

- 0201 は `personal_finance_entries` の唯一の作成者。
- 旧 `20261022_0103_personal_finance_entries` は 0201 に **superseded**。取り込まない。
- 0105 adds owner-bound reminder lifecycle references and service/trigger transition checks. A delivered event is the owner's encrypted in-app result; no external notification channel is created.
- 既適用revision(0001/0002)は変更しない。
