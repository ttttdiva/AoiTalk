#!/usr/bin/env python3
"""
PostgreSQLデータベース強制リセットスクリプト
トランザクションエラーを回避してすべてのデータを確実に削除
"""

import asyncio
import os
import sys
from pathlib import Path

# プロジェクトのルートディレクトリをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.memory.database import DatabaseManager
from src.memory.config import MemoryConfig
from sqlalchemy import text


async def force_reset_database():
    """データベースを強制的にリセット"""
    print("=== PostgreSQL強制データベースリセット ===\n")
    
    # 設定を読み込み
    config = MemoryConfig()
    
    # データベースマネージャーを初期化
    db_manager = DatabaseManager(config)
    
    try:
        # データベースに接続
        await db_manager.initialize()
        print("✅ データベースに接続しました")
        
        # 各テーブルを個別のトランザクションで削除
        tables_to_clear = [
            "conversation_messages",
            "conversation_sessions", 
            "conversation_history",
            "mem0_semantic_facts"
        ]
        
        print("\n🗑️  テーブルデータを個別に削除中...")
        
        for table in tables_to_clear:
            try:
                async with db_manager.SessionLocal() as session:
                    await session.execute(text(f"DELETE FROM {table}"))
                    await session.commit()
                    print(f"✅ {table} テーブルをクリア")
            except Exception as e:
                print(f"⚠️  {table} テーブルのクリアでエラー: {e}")
        
        # 追加のMem0関連テーブルも削除
        print("\n🔍 追加テーブルの確認と削除...")
        additional_tables = [
            "embeddings",
            "vector_store",
            "memory_store", 
            "semantic_memory",
            "pgvector_embeddings"
        ]
        
        for table in additional_tables:
            try:
                async with db_manager.SessionLocal() as session:
                    await session.execute(text(f"DELETE FROM {table}"))
                    await session.commit()
                    print(f"✅ {table} テーブルをクリア")
            except Exception as e:
                print(f"⚠️  {table} テーブルは存在しないか、削除できませんでした")
        
        # データベースの状態を最終確認
        print("\n📊 最終確認:")
        async with db_manager.SessionLocal() as session:
            for table in tables_to_clear:
                try:
                    result = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
                    count = result.scalar()
                    if count == 0:
                        print(f"✅ {table}: {count}行（空）")
                    else:
                        print(f"⚠️  {table}: {count}行（まだデータが残っています）")
                except Exception as e:
                    print(f"❌ {table}: エラー ({e})")
        
        print("\n🎉 強制データベースリセット完了！")
        
    except Exception as e:
        print(f"❌ エラーが発生しました: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        # リソースをクリーンアップ
        if hasattr(db_manager, 'engine') and db_manager.engine:
            await db_manager.engine.dispose()
        print("\n🔄 データベース接続をクローズしました")


if __name__ == "__main__":
    asyncio.run(force_reset_database())