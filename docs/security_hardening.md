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

Linux/macOSでは `AOITALK_FIELD_CRYPTO_KEY_COMMAND` に keyring/KMS から base64 形式の32 byteキーを返すコマンドを設定する。macOSではkey commandが必須で、ローカル鍵ファイルへはフォールバックしない。Linuxのローカル検証用途だけ、`AOITALK_FIELD_CRYPTO_ALLOW_LOCAL_KEY_FILE=true` でユーザー権限ファイルのフォールバックを使える。このフォールバックは `/proc/self/fd` とPOSIXのfd相対操作を必要とし、利用できない環境ではfail closedする。

Linuxのローカル鍵ファイルでは、rootまたは実効ユーザーが所有しgroup/world writableでない全祖先だけを信頼する。sticky bit付きworld-writable祖先を通る場合は、直後のcomponentがrootまたは実効ユーザー所有かつgroup/world writableでないことも検証する。既存鍵は実効ユーザー所有の通常ファイルかつ `0400` または `0600` の場合だけ受理する。`0644` などを自動修復しないため、権限不正時は鍵ファイルをrotationしてから再試行する。

暗号化envelopeは `enc:v1:aes256gcm:local`、12 byte nonce、16 byte GCM tag、paddingなしcanonical base64urlに固定する。key commandやDPAPI helperが失敗した場合、標準出力・標準エラーは例外へ含めず、出力サイズも制限する。

## 対象

- `google_calendar_connections.access_token`
- `google_calendar_connections.refresh_token`
- `app_config_settings.value` の secret/API key/token/password 系 leaf
- `conversation_messages.content`
- `conversation_history.content`
- `conversation_archives.summary`
- `conversation_sessions.current_summary`
- `context_memories.content`
- `knowledge_nodes.body_text/body_json` の案件情報本文
- `record_rows.values`
- `record_rows.title`
- `record_rows.search_text`
- `task_comments.content`
- `knowledge_chunks.text`
- `user_x_cookie_credentials.encrypted_payload`（auth_token/ct0のcanonical最小構造）
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
