from __future__ import annotations

import inspect
import uuid
from datetime import datetime


def test_file_explorer_bookmark_model_columns() -> None:
    from src.memory.models import FileExplorerBookmark

    columns = {column.name for column in FileExplorerBookmark.__table__.columns}
    assert columns == {
        "id",
        "user_id",
        "name",
        "path",
        "icon",
        "sort_order",
        "created_at",
        "updated_at",
    }


def test_file_explorer_bookmark_model_is_user_scoped_unique() -> None:
    from src.memory.models import FileExplorerBookmark

    constraints = [
        constraint
        for constraint in FileExplorerBookmark.__table__.constraints
        if getattr(constraint, "name", None) == "unique_file_explorer_bookmark_path"
    ]
    assert len(constraints) == 1

    constraint_columns = {column.name for column in constraints[0].columns}
    assert constraint_columns == {"user_id", "path"}


def test_file_explorer_bookmark_to_dict() -> None:
    from src.memory.models import FileExplorerBookmark

    bookmark_id = uuid.uuid4()
    user_id = uuid.uuid4()
    now = datetime(2026, 5, 7, 12, 0, 0)

    bookmark = FileExplorerBookmark.__new__(FileExplorerBookmark)
    bookmark.id = bookmark_id
    bookmark.user_id = user_id
    bookmark.name = "Collections"
    bookmark.path = "G:/Collections"
    bookmark.icon = "folder"
    bookmark.sort_order = 2.0
    bookmark.created_at = now
    bookmark.updated_at = now

    result = bookmark.to_dict()

    assert result == {
        "id": str(bookmark_id),
        "user_id": str(user_id),
        "name": "Collections",
        "path": "G:/Collections",
        "icon": "folder",
        "sort_order": 2.0,
        "created_at": "2026-05-07T12:00:00",
        "updated_at": "2026-05-07T12:00:00",
    }


def test_file_explorer_bookmark_repository_methods_are_async() -> None:
    from src.memory.file_explorer_bookmark_repository import (
        FileExplorerBookmarkRepository,
    )

    for method_name in [
        "list_for_user",
        "get_by_path",
        "add",
        "remove_by_path",
        "update",
    ]:
        assert inspect.iscoroutinefunction(
            getattr(FileExplorerBookmarkRepository, method_name)
        )
