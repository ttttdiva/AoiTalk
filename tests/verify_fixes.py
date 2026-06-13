import os
import sys
from unittest.mock import MagicMock

sys.path.append(os.getcwd())


def test_imports():
    print("Testing imports...")
    try:
        from src.services.task_management_service import TaskManagementService
        from src.llm.cli_backends.base import CLIBackendBase
        print("Imports successful!")
    except Exception as e:
        print(f"Import failed: {e}")
        sys.exit(1)


def test_task_service_instantiation():
    print("Testing task service import and setup...")
    from src.services.task_management_service import TaskManagementService

    mock_db_manager = MagicMock()
    mock_db_manager.is_initialized.return_value = True

    _ = TaskManagementService()
    print("TaskManagementService initialized successfully")


if __name__ == "__main__":
    test_imports()
    test_task_service_instantiation()
