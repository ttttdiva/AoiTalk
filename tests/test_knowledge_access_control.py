"""Knowledge Workspace model and route tests."""

import inspect
import uuid
from datetime import datetime


class TestKnowledgeSourcePermissionModel:
    def test_model_tablename(self):
        from src.memory.models import KnowledgeSourcePermission

        assert KnowledgeSourcePermission.__tablename__ == "knowledge_source_permissions"

    def test_model_columns(self):
        from src.memory.models import KnowledgeSourcePermission

        columns = {column.name for column in KnowledgeSourcePermission.__table__.columns}
        expected = {
            "id",
            "source_id",
            "user_id",
            "project_id",
            "permission",
            "created_at",
            "created_by",
        }
        assert expected == columns

    def test_permission_default(self):
        from src.memory.models import KnowledgeSourcePermission

        col = KnowledgeSourcePermission.__table__.columns["permission"]
        assert col.default.arg == "read"

    def test_to_dict(self):
        from src.memory.models import KnowledgeSourcePermission

        now = datetime(2026, 1, 1, 12, 0, 0)
        obj = KnowledgeSourcePermission.__new__(KnowledgeSourcePermission)
        obj.id = uuid.uuid4()
        obj.source_id = uuid.uuid4()
        obj.user_id = uuid.uuid4()
        obj.project_id = None
        obj.permission = "write"
        obj.created_at = now
        obj.created_by = uuid.uuid4()

        result = obj.to_dict()
        assert result["permission"] == "write"
        assert result["project_id"] is None
        assert result["created_at"] == "2026-01-01T12:00:00"


class TestKnowledgeModels:
    def test_source_to_dict_uses_workspace_fields(self):
        from src.memory.models import KnowledgeSource

        obj = KnowledgeSource.__new__(KnowledgeSource)
        obj.id = uuid.uuid4()
        obj.name = "Foam Notes"
        obj.description = None
        obj.root_path = "D:/Notes/Foam"
        obj.source_type = "local_dir"
        obj.owner_user_id = uuid.uuid4()
        obj.access_policy = {"default": "private"}
        obj.include_patterns = ["*.md"]
        obj.exclude_patterns = [".git"]
        obj.sync_mode = "manual"
        obj.write_policy = "propose_patch"
        obj.status = "synced"
        obj.document_count = 10
        obj.chunk_count = 42
        obj.last_synced_at = None
        obj.error_message = None
        obj.created_at = None
        obj.updated_at = None

        result = obj.to_dict()
        assert result["root_path"] == "D:/Notes/Foam"
        assert result["write_policy"] == "propose_patch"
        assert result["document_count"] == 10

    def test_document_source_path_unique_constraint(self):
        from src.memory.models import KnowledgeDocument

        constraints = {
            constraint.name
            for constraint in KnowledgeDocument.__table__.constraints
            if hasattr(constraint, "name")
        }
        assert "uq_knowledge_document_source_path" in constraints

    def test_legacy_rag_tables_are_not_in_metadata(self):
        from src.memory.models import Base

        assert "rag_collections" not in Base.metadata.tables
        assert "project_rag_collections" not in Base.metadata.tables
        assert "user_rag_collections" not in Base.metadata.tables


class TestKnowledgeRoutes:
    def test_router_factory_exists(self):
        from src.api.knowledge_routes import create_knowledge_router

        assert callable(create_knowledge_router)
        sig = inspect.signature(create_knowledge_router)
        params = list(sig.parameters.keys())
        assert "get_db_manager" in params
        assert "get_user_from_request" in params
        assert "require_auth_dependency" in params

    def test_create_source_payload_keeps_legacy_fields_for_compatibility(self):
        from src.api.knowledge_routes import CreateKnowledgeSourcePayload

        payload = CreateKnowledgeSourcePayload(
            name="Foam",
            root_path="D:/Notes/Foam",
        )
        assert payload.source_type == "local_dir"
        assert payload.write_policy == "propose_patch"

    def test_create_project_workspace_payload_does_not_require_root_path(self):
        from src.api.knowledge_routes import CreateKnowledgeSourcePayload

        payload = CreateKnowledgeSourcePayload(
            name="案件Workspace",
            project_id=str(uuid.uuid4()),
        )
        assert payload.root_path is None
        assert payload.project_id is not None
