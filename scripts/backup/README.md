# AoiTalk バックアップシステム

PostgreSQLとMem0の会話ログをHugging Face Model Repositoryにバックアップ・復元するシステムです。

## 特徴

- **プライバシー保護**: APIキーや個人情報を自動的にマスキング
- **圧縮保存**: gzip圧縮により効率的なストレージ利用
- **差分バックアップ**: 既存データのチェックによる重複回避
- **Model Repository形式**: Datasetよりもアクセスしやすい形式で保存

## 必要な設定

`.env`ファイルに以下を設定:
```
HUGGINGFACE_API_KEY=your_token_here
```

## 使い方

### バックアップ

```bash
# 通常のバックアップ（Hugging Faceへアップロード）
python scripts/backup/backup_to_huggingface.py

# ドライラン（エクスポートのみ、アップロードなし）
python scripts/backup/backup_to_huggingface.py --dry-run

# 出力先を指定してエクスポート
python scripts/backup/backup_to_huggingface.py --output-dir ./backup_test
```

### リストア（復元）

```bash
# 通常のリストア
python scripts/backup/restore_from_huggingface.py

# ドライラン（ダウンロードのみ）
python scripts/backup/restore_from_huggingface.py --dry-run

# PostgreSQLのみ復元
python scripts/backup/restore_from_huggingface.py --skip-mem0

# Mem0のみ復元
python scripts/backup/restore_from_huggingface.py --skip-postgres
```

## バックアップ内容

- `conversations.db.gz`: PostgreSQL会話履歴（SQLite形式）
- `mem0_memories.json.gz`: Mem0セマンティックメモリ
- `metadata.json`: バックアップメタデータ
- `README.md`: バックアップ説明

## セキュリティ

- すべてのバックアップはプライベートリポジトリに保存
- APIキー、メールアドレス、電話番号などは自動マスキング
- マスクされたデータ: `[MASKED_API_KEY]`, `[MASKED_PERSONAL_INFO]`, `[MASKED]`

## 自動バックアップ

cronで定期実行する場合の例:

```bash
# 週次バックアップ（毎週日曜日午前3時）
0 3 * * 0 cd /home/indulge/projects/AoiTalk && python scripts/backup/backup_to_huggingface.py
```
