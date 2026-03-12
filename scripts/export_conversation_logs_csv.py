#!/usr/bin/env python3
"""
Export conversation logs from PostgreSQL to CSV format
"""

import os
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
import argparse
from dotenv import load_dotenv
import json
from sqlite_export_utils import export_to_sqlite, convert_datetime_to_iso, convert_json_fields

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.memory.models import ConversationSession, ConversationMessage
from src.memory.config import MemoryConfig


def export_conversation_logs(output_dir: Path, user_id: Optional[str] = None, 
                           start_date: Optional[datetime] = None, 
                           end_date: Optional[datetime] = None):
    """Export conversation messages from PostgreSQL to SQLite database"""
    
    # Load environment variables
    load_dotenv()
    
    # Connect to PostgreSQL
    config = MemoryConfig()
    database_url = (
        f"postgresql://{config.postgres_user}:"
        f"{config.postgres_password}@{config.postgres_host}:"
        f"{config.postgres_port}/{config.postgres_db}"
    )
    pg_engine = create_engine(
        database_url,
        connect_args={"client_encoding": "utf8"}
    )
    Session = sessionmaker(bind=pg_engine)
    pg_session = Session()
    
    try:
        # Export ConversationMessages only
        print("Exporting conversation messages...")
        
        query = pg_session.query(ConversationMessage)
        if user_id:
            # Get session IDs for the user first
            session_query = pg_session.query(ConversationSession).filter(ConversationSession.user_id == user_id)
            sessions = session_query.all()
            session_ids = [s.id for s in sessions]
            if session_ids:
                query = query.filter(ConversationMessage.session_id.in_(session_ids))
            else:
                # No sessions found for user
                messages = []
        else:
            if start_date:
                query = query.filter(ConversationMessage.created_at >= start_date)
            if end_date:
                query = query.filter(ConversationMessage.created_at <= end_date)
            messages = query.all()
        
        if user_id and session_ids:
            if start_date:
                query = query.filter(ConversationMessage.created_at >= start_date)
            if end_date:
                query = query.filter(ConversationMessage.created_at <= end_date)
            messages = query.all()
        elif not user_id:
            messages = query.all()
        else:
            messages = []
        
        # Convert messages to dictionary format
        data = []
        for message in messages:
            row = {
                'id': str(message.id),
                'session_id': str(message.session_id),
                'role': message.role,
                'content': message.content,
                'message_metadata': json.dumps(message.message_metadata, ensure_ascii=False) if message.message_metadata else None,
                'created_at': convert_datetime_to_iso(message.created_at),
                'token_count': message.token_count or 0
            }
            data.append(row)
        
        # Define table schema
        columns = [
            {'name': 'id', 'type': 'TEXT PRIMARY KEY'},
            {'name': 'session_id', 'type': 'TEXT'},
            {'name': 'role', 'type': 'TEXT'},
            {'name': 'content', 'type': 'TEXT'},
            {'name': 'message_metadata', 'type': 'TEXT'},
            {'name': 'created_at', 'type': 'TEXT'},
            {'name': 'token_count', 'type': 'INTEGER'}
        ]
        
        # Define indexes
        indexes = [
            {'name': 'idx_messages_session_id', 'columns': ['session_id']},
            {'name': 'idx_messages_created_at', 'columns': ['created_at']},
            {'name': 'idx_messages_role', 'columns': ['role']}
        ]
        
        # Export to SQLite
        messages_file = output_dir / "conversation_messages.db"
        tables_data = {
            'conversation_messages': {
                'columns': columns,
                'data': data,
                'indexes': indexes
            }
        }
        
        export_to_sqlite(messages_file, tables_data)
        
        print(f"✅ Export completed!")
        print(f"📁 Database: {messages_file}")
        print(f"📊 Records: {len(messages)}")
        
    except Exception as e:
        print(f"Error during export: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        pg_session.close()


def main():
    """Main function with command line argument parsing"""
    parser = argparse.ArgumentParser(
        description="Export conversation logs from PostgreSQL to SQLite database"
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        help="Output directory path (default: logs/)"
    )
    parser.add_argument(
        "-u", "--user",
        type=str,
        help="Filter by user ID"
    )
    parser.add_argument(
        "--start-date",
        type=str,
        help="Start date for filtering (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end-date",
        type=str,
        help="End date for filtering (YYYY-MM-DD)"
    )
    
    args = parser.parse_args()
    
    # Set default output path if not provided
    if args.output:
        output_dir = Path(args.output)
    else:
        output_dir = Path("logs")
    
    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Parse dates if provided
    start_date = None
    end_date = None
    if args.start_date:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
    if args.end_date:
        end_date = datetime.strptime(args.end_date, "%Y-%m-%d")
    
    # Run export
    export_conversation_logs(
        output_dir=output_dir,
        user_id=args.user,
        start_date=start_date,
        end_date=end_date
    )


if __name__ == "__main__":
    main()