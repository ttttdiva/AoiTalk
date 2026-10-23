# Staged personal migrations

autoload (`alembic/versions`) head は `20261022_0002` のまま。本ディレクトリは alembic の
version_locations に含まれず、DBへ自動適用されない。適用は別承認の手順で行う。

単一chain: `20261022_0002` -> `0003` (reminder drafts/selections) -> `0102` (career) -> `0201` (finance datasets) -> `0104` (receipt candidates) -> `0105` (private reminder lifecycle)

- 0201 は `personal_finance_entries` の唯一の作成者。
- 旧 `20261022_0103_personal_finance_entries` は 0201 に **superseded**。取り込まない。
- 0105 adds owner-bound reminder lifecycle references and service/trigger transition checks. A delivered event is the owner's encrypted in-app result; no external notification channel is created.
- 既適用revision(0001/0002)は変更しない。
- 0105 remains staged and is not loaded by normal autoload startup.
