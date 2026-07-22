"""Alembic 導入前テーブルのベースライン.

このリビジョンは Alembic 管理より前から実DBに存在していたテーブル群
（projects / users / spaces / conversation_* など）を、後続マイグレーションの
ALTER が適用できる「導入前の形」で作成する。

背景:
    ルートリビジョン 20260319_0001 は tasks 等を作成する際に projects / users /
    local_tasks へ FK を張るが、これらの pre-Alembic テーブルはチェーンのどこでも
    作成されていなかった。既存の実DBは Alembic 導入前に手動構築済みだったため
    顕在化していなかったが、フレッシュ構築（CI の postgres サービスに
    `alembic upgrade head`）では projects 不在で UndefinedTable となり失敗する。

方針:
    - ここで作成するテーブルは「導入前の形」= 実DB現在の列から、後続マイグレーションが
      追加する列（projects.aliases / knowledge_node_id / deleted_at、
      conversation_sessions.is_group_chat / rp_settings、
      conversation_messages.sender_* / deleted_at / updated_at など）を除外したもの。
      除外しないと後続の add_column が DuplicateColumn で落ちる。
    - 列型・NULL 可否・default は実DB（aoitalk_memory）の information_schema を正本とする。
    - schema.ts（Drizzle）が定義する pre-Alembic テーブルを網羅し、フレッシュ構築後の
      スキーマドリフト検査が通るようにする。

Revision ID: 20260318_0000
Revises:
Create Date: 2026-03-18 00:00:00
"""

from __future__ import annotations

from alembic import op


revision = "20260318_0000"
down_revision = None
branch_labels = None
depends_on = None


CREATE_STATEMENTS = [
    """
    CREATE TABLE users (
        id UUID NOT NULL,
        username VARCHAR(100) NOT NULL,
        email VARCHAR(255),
        password_hash VARCHAR(255) NOT NULL,
        display_name VARCHAR(100),
        preferred_character VARCHAR(100),
        role VARCHAR(20),
        is_active BOOLEAN,
        is_password_reset_required BOOLEAN,
        created_at TIMESTAMP,
        updated_at TIMESTAMP,
        last_login TIMESTAMP,
        user_settings JSON,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE spaces (
        id UUID NOT NULL DEFAULT gen_random_uuid(),
        name VARCHAR NOT NULL,
        slug VARCHAR NOT NULL,
        description TEXT,
        color VARCHAR,
        owner_id UUID NOT NULL,
        sort_order DOUBLE PRECISION DEFAULT 0,
        created_at TIMESTAMP DEFAULT now(),
        updated_at TIMESTAMP DEFAULT now(),
        PRIMARY KEY (id),
        CONSTRAINT spaces_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES users (id)
    )
    """,
    """
    CREATE TABLE projects (
        id UUID NOT NULL,
        name VARCHAR(200) NOT NULL,
        description TEXT,
        slug VARCHAR(100) NOT NULL,
        owner_id UUID NOT NULL,
        allow_join_requests BOOLEAN,
        storage_quota_mb INTEGER,
        storage_used_mb DOUBLE PRECISION,
        created_at TIMESTAMP,
        updated_at TIMESTAMP,
        project_metadata JSON,
        estimated_hours DOUBLE PRECISION,
        space_id UUID,
        is_completed BOOLEAN NOT NULL DEFAULT false,
        PRIMARY KEY (id),
        CONSTRAINT projects_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES users (id),
        CONSTRAINT projects_space_id_fkey FOREIGN KEY (space_id) REFERENCES spaces (id)
    )
    """,
    """
    CREATE TABLE project_rag_collections (
        id UUID NOT NULL,
        project_id UUID NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT project_rag_collections_project_id_fkey FOREIGN KEY (project_id) REFERENCES projects (id)
    )
    """,
    """
    CREATE TABLE model_routing_configs (
        id UUID NOT NULL,
        user_id VARCHAR,
        project_id UUID,
        strategy VARCHAR(20),
        simple_model VARCHAR(100),
        standard_model VARCHAR(100),
        complex_model VARCHAR(100),
        routing_rules JSON,
        is_enabled BOOLEAN,
        created_at TIMESTAMP,
        updated_at TIMESTAMP,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE INDEX ix_model_routing_configs_user_id ON model_routing_configs (user_id)
    """,
    """
    CREATE TABLE local_tasks (
        id UUID NOT NULL,
        project_id UUID,
        clickup_task_id VARCHAR,
        title VARCHAR NOT NULL,
        description TEXT,
        status VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        due_at TIMESTAMP,
        priority VARCHAR,
        created_at TIMESTAMP,
        updated_at TIMESTAMP,
        task_metadata JSON,
        PRIMARY KEY (id),
        CONSTRAINT local_tasks_project_id_fkey FOREIGN KEY (project_id) REFERENCES projects (id)
    )
    """,
    """
    CREATE TABLE project_members (
        id UUID NOT NULL,
        project_id UUID NOT NULL,
        user_id UUID NOT NULL,
        role VARCHAR(20),
        permissions JSON,
        joined_at TIMESTAMP,
        invited_by UUID,
        PRIMARY KEY (id),
        CONSTRAINT unique_project_member UNIQUE (project_id, user_id),
        CONSTRAINT project_members_project_id_fkey FOREIGN KEY (project_id) REFERENCES projects (id),
        CONSTRAINT project_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES users (id),
        CONSTRAINT project_members_invited_by_fkey FOREIGN KEY (invited_by) REFERENCES users (id)
    )
    """,
    """
    CREATE TABLE conversation_sessions (
        id UUID NOT NULL,
        user_id VARCHAR NOT NULL,
        character_name VARCHAR NOT NULL,
        session_start TIMESTAMP,
        last_activity TIMESTAMP,
        message_count INTEGER,
        context JSON,
        current_summary TEXT,
        is_active BOOLEAN,
        title VARCHAR(200) DEFAULT ''::character varying,
        deleted_at TIMESTAMP,
        project_id UUID,
        PRIMARY KEY (id),
        CONSTRAINT conversation_sessions_project_id_fkey FOREIGN KEY (project_id) REFERENCES projects (id)
    )
    """,
    """
    CREATE TABLE conversation_messages (
        id UUID NOT NULL,
        session_id UUID NOT NULL,
        role VARCHAR NOT NULL,
        content TEXT NOT NULL,
        message_metadata JSON,
        created_at TIMESTAMP,
        token_count INTEGER,
        parent_message_id UUID,
        branch_index INTEGER DEFAULT 0,
        is_active_branch BOOLEAN DEFAULT true,
        PRIMARY KEY (id),
        CONSTRAINT conversation_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES conversation_sessions (id),
        CONSTRAINT fk_message_parent FOREIGN KEY (parent_message_id) REFERENCES conversation_messages (id)
    )
    """,
    """
    CREATE TABLE conversation_archives (
        id UUID NOT NULL,
        user_id VARCHAR NOT NULL,
        character_name VARCHAR NOT NULL,
        original_session_id VARCHAR,
        summary TEXT NOT NULL,
        message_count INTEGER,
        start_time TIMESTAMP,
        end_time TIMESTAMP,
        message_metadata JSON,
        archived_at TIMESTAMP,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE conversation_history (
        id UUID NOT NULL,
        user_id VARCHAR NOT NULL,
        session_id UUID,
        character_name VARCHAR NOT NULL,
        role VARCHAR NOT NULL,
        content TEXT NOT NULL,
        message_metadata JSON,
        created_at TIMESTAMP,
        token_count INTEGER,
        function_call_data JSON,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE feedback (
        id VARCHAR(50) NOT NULL,
        session_id VARCHAR(50),
        message TEXT NOT NULL,
        character VARCHAR(100),
        user_input TEXT,
        category VARCHAR(50) NOT NULL,
        comment TEXT,
        resolved BOOLEAN DEFAULT false,
        resolved_at TIMESTAMP,
        resolved_by VARCHAR(100),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        feedback_metadata JSONB DEFAULT '{}'::jsonb,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE webui_login_logs (
        id UUID NOT NULL,
        username VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        ip_address VARCHAR,
        user_agent TEXT,
        success BOOLEAN,
        failure_reason VARCHAR,
        session_duration_seconds INTEGER,
        created_at TIMESTAMP,
        login_metadata JSON,
        PRIMARY KEY (id)
    )
    """,
]


# downgrade は作成の逆順で DROP する（FK 依存を考慮）。
DROP_ORDER = [
    "webui_login_logs",
    "feedback",
    "conversation_history",
    "conversation_archives",
    "conversation_messages",
    "conversation_sessions",
    "project_members",
    "local_tasks",
    "model_routing_configs",
    "project_rag_collections",
    "projects",
    "spaces",
    "users",
]


def upgrade() -> None:
    for stmt in CREATE_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for table in DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
