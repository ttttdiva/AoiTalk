# AoiTalk セキュリティ hardening

## 方針

AoiTalk はDB認証だけに依存せず、保存データをアプリ層で暗号化する。

- DBには暗号文を保存する
- AoiTalk サーバだけがOS保護キーからデータキーを取得して復号する
- `.env` に暗号化キーを置かない
- Qdrant payload も平文本文を保存しない
- AI/検索/UIはAoiTalkサーバ内で復号済み本文を使う

## 鍵

Windows では `scripts/field_crypto_key.ps1` が DPAPI CurrentUser で保護した32 byteデータキーを作成・取得する。既定の保存先は `%APPDATA%\AoiTalk\keys\field-crypto-key.dpapi`。

Linux/macOSでは `AOITALK_FIELD_CRYPTO_KEY_COMMAND` に keyring/KMS から base64 形式の32 byteキーを返すコマンドを設定する。ローカル検証用途だけ、`AOITALK_FIELD_CRYPTO_ALLOW_LOCAL_KEY_FILE=true` でユーザー権限ファイルのフォールバックを使える。

## 対象

- `google_calendar_connections.access_token`
- `google_calendar_connections.refresh_token`
- `app_config_settings.value` の secret/API key/token/password 系 leaf
- `conversation_messages.content`
- `conversation_history.content`
- `conversation_archives.summary`
- `conversation_sessions.current_summary`
- `context_memories.content`
- `project_facts.content`
- `record_rows.values`
- `record_rows.title`
- `record_rows.search_text`
- `task_comments.content`
- `knowledge_chunks.text`
- Qdrant payload `text`

## 移行

```powershell
venv\Scripts\python.exe scripts\encrypt_sensitive_data.py
venv\Scripts\python.exe scripts\encrypt_sensitive_data.py --apply
venv\Scripts\python.exe scripts\audit_plaintext_sensitive_data.py
```

`encrypt_sensitive_data.py` は dry-run が既定。`--apply` のときだけDBを書き換える。

## ローカル外周

```powershell
.\scripts\harden_local_security.ps1
.\scripts\harden_local_security.ps1 -Apply
```

`-Apply` は `.env` ACL と PostgreSQL `listen_addresses` を修正する。Firewall の広い許可を無効化する場合は、影響確認後に `-ApplyFirewall` を明示する。

## 参考

- [OWASP Cryptographic Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html)
- [OWASP Key Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Key_Management_Cheat_Sheet.html)
- [Microsoft DPAPI CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)
- [PostgreSQL listen_addresses](https://www.postgresql.org/docs/current/runtime-config-connection.html)
