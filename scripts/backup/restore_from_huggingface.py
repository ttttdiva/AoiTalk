#!/usr/bin/env python3
"""
Hugging Face Model Repositoryからのリストア（復元）スクリプト
バックアップされたデータをダウンロードして復元
"""

import os
import sys
import json
import sqlite3
import logging
import argparse
import gzip
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

# プロジェクトルートをパスに追加
sys.path.append(str(Path(__file__).parent.parent.parent))

from huggingface_hub import login, hf_hub_download, snapshot_download
from dotenv import load_dotenv

# 環境変数を読み込み
load_dotenv()

# ロギングの設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class HuggingFaceDownloader:
    """Hugging Face Model Repositoryからのダウンロード"""
    
    def __init__(self, repo_id: str, token: str):
        self.repo_id = repo_id
        self.token = token
        
    def login_and_download(self, download_dir: Path) -> bool:
        """ログインしてバックアップをダウンロード"""
        try:
            # ログイン
            login(token=self.token)
            logger.info(f"Hugging Faceにログインしました")
            
            # スナップショットをダウンロード
            logger.info(f"リポジトリ {self.repo_id} からダウンロード中...")
            snapshot_path = snapshot_download(
                repo_id=self.repo_id,
                repo_type="model",
                token=self.token,
                local_dir=str(download_dir),
                local_dir_use_symlinks=False
            )
            
            logger.info(f"ダウンロード完了: {snapshot_path}")
            return True
            
        except Exception as e:
            logger.error(f"ダウンロードエラー: {e}")
            return False


class PostgreSQLRestorer:
    """SQLiteからPostgreSQLへの復元"""
    
    def restore_from_sqlite(self, sqlite_path: Path) -> Dict[str, int]:
        """SQLiteからPostgreSQLに復元"""
        import psycopg2
        from src.memory.database import DatabaseManager
        from src.memory.config import MemoryConfig
        
        stats = {'sessions': 0, 'messages': 0, 'archives': 0}
        
        # SQLiteに接続
        conn_sqlite = sqlite3.connect(str(sqlite_path))
        cursor_sqlite = conn_sqlite.cursor()
        
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
        
        try:
            # セッションデータを復元
            cursor_sqlite.execute("SELECT * FROM conversation_sessions")
            sessions = cursor_sqlite.fetchall()
            
            for session in sessions:
                # 既存のセッションをチェック
                cursor_pg.execute(
                    "SELECT id FROM conversation_sessions WHERE id = %s",
                    (session[0],)
                )
                if not cursor_pg.fetchone():
                    cursor_pg.execute(
                        """INSERT INTO conversation_sessions 
                           (id, user_id, character_name, session_start, last_activity,
                            message_count, context, current_summary, is_active) 
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        session
                    )
                    stats['sessions'] += 1
            
            # メッセージデータを復元
            cursor_sqlite.execute("SELECT * FROM conversation_messages")
            messages = cursor_sqlite.fetchall()
            
            for message in messages:
                # 既存のメッセージをチェック
                cursor_pg.execute(
                    "SELECT id FROM conversation_messages WHERE id = %s",
                    (message[0],)
                )
                if not cursor_pg.fetchone():
                    cursor_pg.execute(
                        """INSERT INTO conversation_messages 
                           (id, session_id, role, content, message_metadata, 
                            created_at, token_count, embedding) 
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        message
                    )
                    stats['messages'] += 1
            
            # アーカイブデータを復元
            cursor_sqlite.execute("SELECT * FROM conversation_archives")
            archives = cursor_sqlite.fetchall()
            
            for archive in archives:
                # 既存のアーカイブをチェック
                cursor_pg.execute(
                    "SELECT id FROM conversation_archives WHERE id = %s",
                    (archive[0],)
                )
                if not cursor_pg.fetchone():
                    cursor_pg.execute(
                        """INSERT INTO conversation_archives 
                           (id, user_id, character_name, original_session_id, summary,
                            message_count, start_time, end_time, message_metadata, 
                            archived_at, summary_embedding) 
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        archive
                    )
                    stats['archives'] += 1
            
            conn_pg.commit()
            logger.info(f"PostgreSQL復元完了: {stats}")
            
        except Exception as e:
            conn_pg.rollback()
            logger.error(f"復元エラー: {e}")
            raise
            
        finally:
            cursor_sqlite.close()
            conn_sqlite.close()
            cursor_pg.close()
            conn_pg.close()
        
        return stats


class Mem0Restorer:
    """Mem0メモリの復元"""
    
    def restore_from_json(self, json_path: Path) -> Dict[str, int]:
        """JSONからMem0メモリを復元"""
        try:
            # Conditionally import semantic memory to avoid SQLite issues
            try:
                from src.memory.semantic_memory import SemanticMemoryManager
                SEMANTIC_MEMORY_AVAILABLE = True
            except (ImportError, Exception) as e:
                logger.warning(f"Semantic memory not available: {e}")
                SEMANTIC_MEMORY_AVAILABLE = False
                SemanticMemoryManager = None
            
            stats = {'memories': 0, 'facts': 0}
            
            # JSONを読み込み
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            memories = data.get('memories', [])
            
            # Mem0マネージャーを初期化
            if not SEMANTIC_MEMORY_AVAILABLE or SemanticMemoryManager is None:
                logger.warning("Semantic memory is not available. Skipping Mem0 restoration.")
                return stats
            
            try:
                manager = SemanticMemoryManager()
            except Exception as e:
                logger.error(f"Failed to initialize semantic memory manager: {e}")
                return stats
            
            # メモリを復元
            for memory in memories:
                # マスクされたデータは復元しない（[MASKED]を含むものはスキップ）
                if self._contains_masked_data(memory):
                    logger.warning(f"マスクされたメモリをスキップ: {memory.get('id', 'unknown')}")
                    continue
                
                try:
                    # メモリを追加
                    manager.add_memory(
                        text=memory.get('text', ''),
                        metadata=memory.get('metadata', {})
                    )
                    stats['memories'] += 1
                    
                    if 'facts' in memory:
                        stats['facts'] += len(memory.get('facts', []))
                        
                except Exception as e:
                    logger.warning(f"メモリ復元エラー（スキップ）: {e}")
            
            logger.info(f"Mem0復元完了: {stats}")
            return stats
            
        except Exception as e:
            logger.warning(f"Mem0復元エラー（全体をスキップ）: {e}")
            return {'memories': 0, 'facts': 0}
    
    def _contains_masked_data(self, data: Any) -> bool:
        """データにマスクされた情報が含まれているかチェック"""
        if isinstance(data, str):
            return '[MASKED' in data
        elif isinstance(data, dict):
            return any(self._contains_masked_data(v) for v in data.values())
        elif isinstance(data, list):
            return any(self._contains_masked_data(item) for item in data)
        return False


def decompress_file(input_path: Path, output_path: Path):
    """gzip圧縮されたファイルを解凍"""
    with gzip.open(input_path, 'rb') as f_in:
        with open(output_path, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
    logger.info(f"解凍完了: {input_path} -> {output_path}")


def main():
    parser = argparse.ArgumentParser(description='Hugging Faceからバックアップを復元')
    parser.add_argument('--dry-run', action='store_true', help='ダウンロードのみ実行（復元しない）')
    parser.add_argument('--download-dir', type=str, help='ダウンロード先ディレクトリ')
    parser.add_argument('--skip-postgres', action='store_true', help='PostgreSQL復元をスキップ')
    parser.add_argument('--skip-mem0', action='store_true', help='Mem0復元をスキップ')
    args = parser.parse_args()
    
    # 環境変数から設定を読み込み
    hf_token = os.getenv('HUGGINGFACE_API_KEY')
    if not hf_token:
        logger.error("HUGGINGFACE_API_KEYが設定されていません")
        return 1
    
    repo_id = os.getenv('HUGGINGFACE_REPO_ID')
    if not repo_id:
        logger.error("HUGGINGFACE_REPO_IDが設定されていません")
        return 1
    
    # ダウンロードディレクトリの準備
    if args.download_dir:
        download_dir = Path(args.download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)
    else:
        download_dir = Path(tempfile.mkdtemp(prefix="aoitalk_restore_"))
    
    try:
        # Hugging Faceからダウンロード
        downloader = HuggingFaceDownloader(repo_id, hf_token)
        if not downloader.login_and_download(download_dir):
            return 1
        
        # メタデータを確認
        metadata_path = download_dir / "metadata.json"
        if metadata_path.exists():
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)
            logger.info(f"バックアップ情報: {metadata}")
        
        if args.dry_run:
            logger.info(f"ドライラン完了。ダウンロード先: {download_dir}")
            return 0
        
        # PostgreSQL復元
        if not args.skip_postgres:
            db_gz_path = download_dir / "conversations.db.gz"
            if db_gz_path.exists():
                logger.info("PostgreSQLデータの復元を開始...")
                db_path = download_dir / "conversations.db"
                decompress_file(db_gz_path, db_path)
                
                restorer = PostgreSQLRestorer()
                pg_stats = restorer.restore_from_sqlite(db_path)
                logger.info(f"PostgreSQL復元結果: {pg_stats}")
            else:
                logger.warning("conversations.db.gz が見つかりません")
        
        # Mem0復元
        if not args.skip_mem0:
            mem0_gz_path = download_dir / "mem0_memories.json.gz"
            if mem0_gz_path.exists():
                logger.info("Mem0メモリの復元を開始...")
                mem0_path = download_dir / "mem0_memories.json"
                decompress_file(mem0_gz_path, mem0_path)
                
                restorer = Mem0Restorer()
                mem0_stats = restorer.restore_from_json(mem0_path)
                logger.info(f"Mem0復元結果: {mem0_stats}")
            else:
                logger.warning("mem0_memories.json.gz が見つかりません")
        
        logger.info("復元処理が完了しました")
        return 0
        
    except Exception as e:
        logger.error(f"復元エラー: {e}", exc_info=True)
        return 1
        
    finally:
        # 一時ディレクトリの場合は削除
        if not args.download_dir and download_dir.exists():
            shutil.rmtree(download_dir)
            logger.info("一時ファイルを削除しました")


if __name__ == "__main__":
    sys.exit(main())