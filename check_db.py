"""
PostgreSQL会話履歴確認スクリプト

使用方法:
    venv\Scripts\python.exe scripts\check_db.py

説明:
    データベース内の会話セッションを確認します。
    project_idの関連付けを確認するのに便利です。
"""
import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

async def check_sessions():
    # .envから接続情報を取得
    conn = await asyncpg.connect(
        host=os.getenv('POSTGRES_HOST', '127.0.0.1'),
        port=int(os.getenv('POSTGRES_PORT', 5432)),
        user=os.getenv('POSTGRES_USER', 'aoitalk'),
        password=os.getenv('POSTGRES_PASSWORD'),
        database=os.getenv('POSTGRES_DB', 'aoitalk_memory')
    )
    
    try:
        rows = await conn.fetch("""
            SELECT id, title, project_id, message_count, is_active, 
                   character_name, last_activity
            FROM conversation_sessions
            ORDER BY last_activity DESC
            LIMIT 20
        """)
        
        print("\n=== 会話セッション一覧 ===\n")
        for row in rows:
            print(f"ID: {row['id']}")
            print(f"  タイトル: {row['title']}")
            print(f"  プロジェクトID: {row['project_id']}")
            print(f"  メッセージ数: {row['message_count']}")
            print(f"  アクティブ: {row['is_active']}")
            print(f"  キャラクター: {row['character_name']}")
            print(f"  最終更新: {row['last_activity']}")
            print()
            
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(check_sessions())
