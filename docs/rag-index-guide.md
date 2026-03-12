# RAGインデックスの作成ガイド

AoiTalkでは、ドキュメントをベクトルデータベースにインデックスしてRAG検索を利用できます。

## 基本的な使い方

```bash
# 仮想環境を有効化
cd AoiTalk
.\venv\Scripts\activate

# ディレクトリ全体をインデックス
python scripts/build_rag_index.py "C:/path/to/documents"

# 単一ファイルをインデックス
python scripts/build_rag_index.py "C:/path/to/file.pdf"
```

## オプション

| オプション | 説明 |
|-----------|------|
| `--clear` | 既存インデックスをクリアしてから再構築 |
| `--project <UUID>` | 特定プロジェクト用のインデックスを作成 |

## プロジェクト別インデックス

プロジェクトを指定すると、そのプロジェクト専用のコレクションにインデックスされます。

```bash
# プロジェクト用インデックス作成
python scripts/build_rag_index.py "C:/project_docs" --project abc123-def456-...

# プロジェクト用インデックスをクリア＆再構築
python scripts/build_rag_index.py "C:/project_docs" --project abc123-def456-... --clear
```

> **ヒント**: プロジェクトUUIDはWebUIのプロジェクト設定画面で確認できます。

## 対応ファイル形式

- Markdown (`.md`)
- テキスト (`.txt`)
- PDF (`.pdf`)
- その他LlamaIndexがサポートする形式

## WebUIでの利用

1. WebUIでプロジェクトを選択
2. 通常通りメッセージを送信
3. RAG検索が自動的にそのプロジェクトのコレクションを使用

プロジェクト未選択時はデフォルトのコレクション（`aoitalk_documents`）が検索されます。
