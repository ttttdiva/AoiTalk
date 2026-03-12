"""RAGコレクション アクセス制御のテスト"""
import uuid
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


class TestUserRagCollectionModel:
    """UserRagCollectionモデルのテスト"""

    def test_model_tablename(self):
        from src.memory.models import UserRagCollection
        assert UserRagCollection.__tablename__ == 'user_rag_collections'

    def test_model_columns(self):
        from src.memory.models import UserRagCollection
        columns = {c.name for c in UserRagCollection.__table__.columns}
        expected = {'id', 'user_id', 'collection_id', 'permission', 'linked_at', 'linked_by'}
        assert expected == columns

    def test_model_unique_constraint(self):
        from src.memory.models import UserRagCollection
        constraints = [
            c for c in UserRagCollection.__table__.constraints
            if hasattr(c, 'name') and c.name == 'unique_user_collection'
        ]
        assert len(constraints) == 1

    def test_permission_default(self):
        from src.memory.models import UserRagCollection
        col = UserRagCollection.__table__.columns['permission']
        assert col.default.arg == 'read'

    def test_to_dict(self):
        from src.memory.models import UserRagCollection
        now = datetime(2026, 1, 1, 12, 0, 0)
        user_id = uuid.uuid4()
        col_id = uuid.uuid4()
        link_id = uuid.uuid4()
        linked_by_id = uuid.uuid4()

        obj = UserRagCollection.__new__(UserRagCollection)
        obj.id = link_id
        obj.user_id = user_id
        obj.collection_id = col_id
        obj.permission = 'write'
        obj.linked_at = now
        obj.linked_by = linked_by_id

        result = obj.to_dict()
        assert result['id'] == str(link_id)
        assert result['user_id'] == str(user_id)
        assert result['collection_id'] == str(col_id)
        assert result['permission'] == 'write'
        assert result['linked_at'] == '2026-01-01T12:00:00'
        assert result['linked_by'] == str(linked_by_id)

    def test_to_dict_none_linked_by(self):
        from src.memory.models import UserRagCollection
        obj = UserRagCollection.__new__(UserRagCollection)
        obj.id = uuid.uuid4()
        obj.user_id = uuid.uuid4()
        obj.collection_id = uuid.uuid4()
        obj.permission = 'read'
        obj.linked_at = None
        obj.linked_by = None

        result = obj.to_dict()
        assert result['linked_at'] is None
        assert result['linked_by'] is None


class TestRagCollectionRepositoryImports:
    """リポジトリの新メソッドが正しくインポートできるかの確認"""

    def test_repository_has_access_control_methods(self):
        from src.memory.rag_collection_repository import RagCollectionRepository

        assert hasattr(RagCollectionRepository, 'list_accessible_collections')
        assert hasattr(RagCollectionRepository, 'can_user_access_collection')
        assert hasattr(RagCollectionRepository, 'has_write_permission')
        assert hasattr(RagCollectionRepository, 'link_to_user')
        assert hasattr(RagCollectionRepository, 'unlink_from_user')
        assert hasattr(RagCollectionRepository, 'get_collection_user_links')
        assert hasattr(RagCollectionRepository, 'get_user_linked_collections')

    def test_methods_are_static(self):
        from src.memory.rag_collection_repository import RagCollectionRepository
        import inspect

        for method_name in [
            'list_accessible_collections',
            'can_user_access_collection',
            'has_write_permission',
            'link_to_user',
            'unlink_from_user',
            'get_collection_user_links',
            'get_user_linked_collections',
        ]:
            method = getattr(RagCollectionRepository, method_name)
            assert inspect.iscoroutinefunction(method), f"{method_name} should be async"


class TestRagCollectionRoutesImports:
    """APIルートの新モデルとルーターファクトリのテスト"""

    def test_link_user_payload(self):
        from src.api.rag_collection_routes import LinkUserPayload

        payload = LinkUserPayload(user_id="test-uuid", permission="write")
        assert payload.user_id == "test-uuid"
        assert payload.permission == "write"

    def test_link_user_payload_default_permission(self):
        from src.api.rag_collection_routes import LinkUserPayload

        payload = LinkUserPayload(user_id="test-uuid")
        assert payload.permission == "read"

    def test_router_factory_exists(self):
        from src.api.rag_collection_routes import create_rag_collection_router
        import inspect

        assert callable(create_rag_collection_router)
        sig = inspect.signature(create_rag_collection_router)
        params = list(sig.parameters.keys())
        assert 'get_db_manager' in params
        assert 'get_user_from_request' in params
        assert 'require_auth_dependency' in params


class TestModelRelationships:
    """モデル間のリレーション定義テスト"""

    def test_rag_collection_has_user_links_backref(self):
        from src.memory.models import RagCollection
        mapper = RagCollection.__mapper__
        rel_names = {r.key for r in mapper.relationships}
        assert 'user_links' in rel_names

    def test_user_has_rag_collection_links_backref(self):
        from src.memory.models import User
        mapper = User.__mapper__
        rel_names = {r.key for r in mapper.relationships}
        assert 'rag_collection_links' in rel_names

    def test_user_rag_collection_foreign_keys(self):
        from src.memory.models import UserRagCollection
        fk_targets = set()
        for col in UserRagCollection.__table__.columns:
            for fk in col.foreign_keys:
                fk_targets.add(fk.target_fullname)
        assert 'users.id' in fk_targets
        assert 'rag_collections.id' in fk_targets
