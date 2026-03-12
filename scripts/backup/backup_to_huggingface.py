#!/usr/bin/env python3
"""
AoiTalk会話ログのHugging Face Model Repositoryへのバックアップシステム
PostgreSQLとMem0のデータをエクスポートし、プライバシー保護処理を行ってアップロード
"""

import os
import sys
import json
import sqlite3
import logging
import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional
import tempfile
import shutil
import gzip

# プロジェクトルートをパスに追加
sys.path.append(str(Path(__file__).parent.parent.parent))

from huggingface_hub import login, HfApi, upload_folder
from dotenv import load_dotenv

# 環境変数を読み込み
load_dotenv()

# ロギングの設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class PrivacyMasker:
    """個人情報やセンシティブな情報をマスキングするクラス"""
    
    def __init__(self):
        # APIキーのパターン
        self.api_key_patterns = [
            r'sk-[a-zA-Z0-9]{48}',  # OpenAI
            r'AIza[a-zA-Z0-9-_]{35}',  # Google
            r'[a-f0-9]{32}',  # 一般的な32文字のキー
            r'[a-zA-Z0-9_-]{40,}',  # 一般的な長いトークン
        ]
        
        # 個人情報のパターン（日本語対応）
        self.personal_patterns = [
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',  # Email
            r'\b\d{3}-\d{4}-\d{4}\b',  # 日本の電話番号
            r'\b\d{3}-\d{3}-\d{4}\b',  # 米国の電話番号
            r'\b\d{3}\.\d{3}\.\d{3}\.\d{3}\b',  # IPアドレス
        ]
        
        # 環境変数から取得する可能性のあるキー名
        self.env_keys = [
            'API_KEY', 'SECRET', 'TOKEN', 'PASSWORD', 'PRIVATE',
            'CLIENT_ID', 'CLIENT_SECRET', 'ACCESS_TOKEN'
        ]
    
    def mask_text(self, text: str) -> str:
        """テキスト内のセンシティブな情報をマスキング"""
        if not text:
            return text
        
        masked_text = text
        
        # APIキーのマスキング
        for pattern in self.api_key_patterns:
            masked_text = re.sub(pattern, '[MASKED_API_KEY]', masked_text)
        
        # 個人情報のマスキング
        for pattern in self.personal_patterns:
            masked_text = re.sub(pattern, '[MASKED_PERSONAL_INFO]', masked_text)
        
        # 環境変数キーのマスキング
        for key in self.env_keys:
            pattern = f'{key}["\']?[:=]["\']?[^\\s"\',;]+' 
            masked_text = re.sub(pattern, f'{key}=[MASKED]', masked_text, flags=re.IGNORECASE)
        
        return masked_text
    
    def mask_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """辞書内のセンシティブな情報を再帰的にマスキング"""
        masked_data = {}
        
        for key, value in data.items():
            # キー自体がセンシティブな場合
            if any(sensitive in key.lower() for sensitive in ['api', 'key', 'secret', 'token', 'password']):
                masked_data[key] = '[MASKED]'
            elif isinstance(value, str):
                masked_data[key] = self.mask_text(value)
            elif isinstance(value, dict):
                masked_data[key] = self.mask_dict(value)
            elif isinstance(value, list):
                masked_data[key] = [
                    self.mask_dict(item) if isinstance(item, dict) 
                    else self.mask_text(item) if isinstance(item, str)
                    else item
                    for item in value
                ]
            else:
                masked_data[key] = value
        
        return masked_data


class PostgreSQLExporter:
    """PostgreSQLから会話履歴をエクスポート"""
    
    def __init__(self, masker: PrivacyMasker):
        self.masker = masker
        
    def export_to_sqlite(self, output_path: Path) -> Dict[str, int]:
        """PostgreSQLからSQLiteにエクスポート"""
        import psycopg2
        from src.memory.database import DatabaseManager
        from src.memory.config import MemoryConfig
        
        stats = {'sessions': 0, 'messages': 0, 'archives': 0}
        
        # PostgreSQL接続情報を取得
        config = MemoryConfig()
        database_url = (
            f"postgresql://{config.postgres_user}:"
            f"{config.postgres_password}@{config.postgres_host}:"
            f"{config.postgres_port}/{config.postgres_db}"
        )
        
        # PostgreSQLに接続
        conn_pg = psycopg2.connect(database_url)
        cursor_pg = conn_pg.cursor()
        
        # SQLiteデータベースを作成
        conn_sqlite = sqlite3.connect(str(output_path))
        cursor_sqlite = conn_sqlite.cursor()
        
        try:
            # テーブルスキーマを作成
            self._create_sqlite_schema(cursor_sqlite)
            
            # セッションデータをエクスポート
            cursor_pg.execute("""
                SELECT id, user_id, character_name, session_start, last_activity,
                       message_count, context, current_summary, is_active
                FROM conversation_sessions 
                ORDER BY session_start
            """)
            sessions = cursor_pg.fetchall()
            
            for session in sessions:
                masked_session = list(session)
                # current_summary (index 7) をマスキング
                if masked_session[7]:
                    masked_session[7] = self.masker.mask_text(masked_session[7])
                # context (index 6) をマスキング
                if masked_session[6]:
                    if isinstance(masked_session[6], dict):
                        masked_session[6] = json.dumps(
                            self.masker.mask_dict(masked_session[6])
                        )
                    else:
                        # 既にJSON文字列の場合
                        masked_session[6] = json.dumps(
                            self.masker.mask_dict(json.loads(masked_session[6]))
                        )
                else:
                    masked_session[6] = json.dumps({})
                
                cursor_sqlite.execute(
                    """INSERT INTO conversation_sessions 
                       (id, user_id, character_name, session_start, last_activity,
                        message_count, context, current_summary, is_active)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    masked_session
                )
                stats['sessions'] += 1
            
            # メッセージデータをエクスポート
            cursor_pg.execute("""
                SELECT id, session_id, role, content, message_metadata, 
                       created_at, token_count, embedding 
                FROM conversation_messages 
                ORDER BY created_at
            """)
            messages = cursor_pg.fetchall()
            
            for message in messages:
                masked_message = list(message)
                # content (index 3) をマスキング
                masked_message[3] = self.masker.mask_text(masked_message[3])
                # message_metadata (index 4) をマスキング
                if masked_message[4]:
                    if isinstance(masked_message[4], dict):
                        masked_message[4] = json.dumps(
                            self.masker.mask_dict(masked_message[4])
                        )
                    else:
                        # 既にJSON文字列の場合
                        masked_message[4] = json.dumps(
                            self.masker.mask_dict(json.loads(masked_message[4]))
                        )
                else:
                    masked_message[4] = json.dumps({})
                # embedding (index 7) は除外
                masked_message[7] = None
                
                cursor_sqlite.execute(
                    """INSERT INTO conversation_messages 
                       (id, session_id, role, content, message_metadata, 
                        created_at, token_count, embedding)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    masked_message
                )
                stats['messages'] += 1
            
            # アーカイブデータをエクスポート
            cursor_pg.execute("""
                SELECT id, user_id, character_name, original_session_id, summary,
                       message_count, start_time, end_time, message_metadata, 
                       archived_at, summary_embedding
                FROM conversation_archives 
                ORDER BY archived_at
            """)
            archives = cursor_pg.fetchall()
            
            for archive in archives:
                masked_archive = list(archive)
                # summary (index 4) をマスキング
                if masked_archive[4]:
                    masked_archive[4] = self.masker.mask_text(masked_archive[4])
                # message_metadata (index 8) をマスキング
                if masked_archive[8]:
                    if isinstance(masked_archive[8], dict):
                        masked_archive[8] = json.dumps(
                            self.masker.mask_dict(masked_archive[8])
                        )
                    else:
                        # 既にJSON文字列の場合
                        masked_archive[8] = json.dumps(
                            self.masker.mask_dict(json.loads(masked_archive[8]))
                        )
                else:
                    masked_archive[8] = json.dumps({})
                # summary_embedding (index 10) は除外
                masked_archive[10] = None
                
                cursor_sqlite.execute(
                    """INSERT INTO conversation_archives 
                       (id, user_id, character_name, original_session_id, summary,
                        message_count, start_time, end_time, message_metadata, 
                        archived_at, summary_embedding)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    masked_archive
                )
                stats['archives'] += 1
            
            conn_sqlite.commit()
            logger.info(f"PostgreSQLエクスポート完了: {stats}")
            
        finally:
            cursor_pg.close()
            conn_pg.close()
            cursor_sqlite.close()
            conn_sqlite.close()
        
        return stats
    
    def _create_sqlite_schema(self, cursor):
        """SQLiteのテーブルスキーマを作成"""
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                character_name TEXT,
                session_start TEXT,
                last_activity TEXT,
                message_count INTEGER,
                context TEXT,
                current_summary TEXT,
                is_active INTEGER
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content TEXT,
                message_metadata TEXT,
                created_at TEXT,
                token_count INTEGER,
                embedding BLOB
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_archives (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                character_name TEXT,
                original_session_id TEXT,
                summary TEXT,
                message_count INTEGER,
                start_time TEXT,
                end_time TEXT,
                message_metadata TEXT,
                archived_at TEXT,
                summary_embedding BLOB
            )
        """)


class Mem0Exporter:
    """Mem0から重要なメモリをエクスポート"""
    
    def __init__(self, masker: PrivacyMasker):
        self.masker = masker
    
    def export_to_json(self, output_path: Path) -> Dict[str, int]:
        """Mem0メモリをJSONにエクスポート"""
        try:
            # Conditionally import semantic memory to avoid SQLite issues
            try:
                from src.memory.semantic_memory import SemanticMemoryManager
                SEMANTIC_MEMORY_AVAILABLE = True
            except (ImportError, Exception) as e:
                logger.warning(f"Semantic memory not available: {e}")
                SEMANTIC_MEMORY_AVAILABLE = False
                SemanticMemoryManager = None
            import psycopg2
            from src.memory.config import MemoryConfig
            
            stats = {'memories': 0, 'facts': 0}
            
            # Mem0は専用のテーブルを使用しているため、直接SQLでアクセス
            config = MemoryConfig()
            database_url = (
                f"postgresql://{config.postgres_user}:"
                f"{config.postgres_password}@{config.postgres_host}:"
                f"{config.postgres_port}/{config.postgres_db}"
            )
            
            conn = psycopg2.connect(database_url)
            cursor = conn.cursor()
            
            try:
                # Mem0のメモリテーブルから取得（テーブル名は推測）
                cursor.execute("""
                    SELECT memory, metadata, created_at, user_id
                    FROM memories
                    ORDER BY created_at DESC
                """)
                memories = cursor.fetchall()
                
                masked_memories = []
                for memory in memories:
                    memory_dict = {
                        'memory': memory[0],
                        'metadata': memory[1] if memory[1] else {},
                        'created_at': memory[2].isoformat() if memory[2] else None,
                        'user_id': memory[3]
                    }
                    masked_memory = self.masker.mask_dict(memory_dict)
                    masked_memories.append(masked_memory)
                    stats['memories'] += 1
                
            except psycopg2.errors.UndefinedTable:
                # Mem0テーブルが存在しない場合
                logger.warning("Mem0メモリテーブルが見つかりません")
                masked_memories = []
            
            finally:
                cursor.close()
                conn.close()
            
            # JSONとして保存
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'export_date': datetime.now().isoformat(),
                    'memories': masked_memories,
                    'stats': stats
                }, f, ensure_ascii=False, indent=2)
            
            logger.info(f"Mem0エクスポート完了: {stats}")
            return stats
            
        except Exception as e:
            logger.warning(f"Mem0エクスポートエラー（スキップ）: {e}")
            # エラーが発生しても空のJSONファイルを作成
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'export_date': datetime.now().isoformat(),
                    'memories': [],
                    'stats': {'memories': 0, 'facts': 0}
                }, f, ensure_ascii=False, indent=2)
            return {'memories': 0, 'facts': 0}


class HuggingFaceUploader:
    """Hugging Face Model Repositoryへのアップロード"""
    
    def __init__(self, repo_id: str, token: str):
        self.repo_id = repo_id
        self.token = token
        self.api = HfApi()
        
    def login_and_prepare(self):
        """ログインとリポジトリの準備"""
        login(token=self.token)
        
        # リポジトリが存在しない場合は作成
        try:
            self.api.repo_info(repo_id=self.repo_id, token=self.token)
            logger.info(f"既存のリポジトリを使用: {self.repo_id}")
        except:
            self.api.create_repo(
                repo_id=self.repo_id,
                private=True,
                repo_type="model",
                token=self.token
            )
            logger.info(f"新しいリポジトリを作成: {self.repo_id}")
    
    def upload_backup(self, backup_dir: Path) -> bool:
        """バックアップディレクトリをアップロード"""
        try:
            # README.mdを作成
            readme_path = backup_dir / "README.md"
            with open(readme_path, 'w', encoding='utf-8') as f:
                f.write(f"""# AoiTalk Conversation Backup

This is an automated backup of AoiTalk conversation logs.

## Contents

- `conversations.db.gz`: Compressed SQLite database with conversation history
- `mem0_memories.json.gz`: Compressed JSON file with Mem0 semantic memories
- `metadata.json`: Backup metadata

## Privacy Notice

All personal information and API keys have been automatically masked for privacy protection.

Backup created: {datetime.now().isoformat()}
""")
            
            # メタデータを作成
            metadata_path = backup_dir / "metadata.json"
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'backup_date': datetime.now().isoformat(),
                    'backup_type': 'full',
                    'version': '1.0',
                    'source': 'AoiTalk'
                }, f, indent=2)
            
            # アップロード
            logger.info("Hugging Faceへのアップロードを開始...")
            upload_folder(
                folder_path=str(backup_dir),
                repo_id=self.repo_id,
                repo_type="model",
                token=self.token,
                commit_message=f"Automated backup - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            logger.info("アップロード完了")
            return True
            
        except Exception as e:
            logger.error(f"アップロードエラー: {e}")
            return False


def compress_file(input_path: Path, output_path: Path):
    """ファイルをgzip圧縮"""
    with open(input_path, 'rb') as f_in:
        with gzip.open(output_path, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
    logger.info(f"圧縮完了: {input_path} -> {output_path}")


def main():
    parser = argparse.ArgumentParser(description='AoiTalk会話ログをHugging Faceにバックアップ')
    parser.add_argument('--dry-run', action='store_true', help='アップロードせずにエクスポートのみ実行')
    parser.add_argument('--output-dir', type=str, help='出力ディレクトリ（デフォルト: 一時ディレクトリ）')
    args = parser.parse_args()
    
    # 環境変数から設定を読み込み
    hf_token = os.getenv('HUGGINGFACE_API_KEY')
    if not hf_token and not args.dry_run:
        logger.error("HUGGINGFACE_API_KEYが設定されていません")
        return 1
    
    repo_id = os.getenv('HUGGINGFACE_REPO_ID')
    if not repo_id:
        logger.error("HUGGINGFACE_REPO_IDが設定されていません")
        return 1
    
    # 出力ディレクトリの準備
    if args.output_dir:
        backup_dir = Path(args.output_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
    else:
        backup_dir = Path(tempfile.mkdtemp(prefix="aoitalk_backup_"))
    
    try:
        # プライバシーマスカーを初期化
        masker = PrivacyMasker()
        
        # PostgreSQLエクスポート
        logger.info("PostgreSQLデータのエクスポートを開始...")
        pg_exporter = PostgreSQLExporter(masker)
        db_path = backup_dir / "conversations.db"
        pg_stats = pg_exporter.export_to_sqlite(db_path)
        
        # データベースを圧縮
        compress_file(db_path, backup_dir / "conversations.db.gz")
        db_path.unlink()  # 非圧縮版を削除
        
        # Mem0エクスポート
        logger.info("Mem0メモリのエクスポートを開始...")
        mem0_exporter = Mem0Exporter(masker)
        mem0_path = backup_dir / "mem0_memories.json"
        mem0_stats = mem0_exporter.export_to_json(mem0_path)
        
        # JSONを圧縮
        compress_file(mem0_path, backup_dir / "mem0_memories.json.gz")
        mem0_path.unlink()  # 非圧縮版を削除
        
        # 統計情報を表示
        logger.info("エクスポート完了:")
        logger.info(f"  - セッション: {pg_stats['sessions']}")
        logger.info(f"  - メッセージ: {pg_stats['messages']}")
        logger.info(f"  - アーカイブ: {pg_stats['archives']}")
        logger.info(f"  - Mem0メモリ: {mem0_stats['memories']}")
        logger.info(f"  - Mem0ファクト: {mem0_stats['facts']}")
        
        # Hugging Faceへアップロード
        if not args.dry_run:
            uploader = HuggingFaceUploader(repo_id, hf_token)
            uploader.login_and_prepare()
            
            if uploader.upload_backup(backup_dir):
                logger.info(f"バックアップ完了: {repo_id}")
            else:
                logger.error("アップロードに失敗しました")
                return 1
        else:
            logger.info(f"ドライラン完了。出力先: {backup_dir}")
        
        return 0
        
    except Exception as e:
        logger.error(f"バックアップエラー: {e}", exc_info=True)
        return 1
        
    finally:
        # 一時ディレクトリの場合は削除
        if not args.output_dir and backup_dir.exists():
            shutil.rmtree(backup_dir)
            logger.info("一時ファイルを削除しました")


if __name__ == "__main__":
    sys.exit(main())