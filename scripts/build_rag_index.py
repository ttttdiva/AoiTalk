#!/usr/bin/env python
"""
RAG Index Builder Script

Usage:
    python scripts/build_rag_index.py <directory_or_file_path> [--clear] [--project <project_id>]
    python scripts/build_rag_index.py --collection <collection_id> [--clear]

Arguments:
    path: Path to the directory or file to index
    --clear: Optional. Clear existing index before processing
    --project: Optional. Project ID (UUID) to index documents for
    --collection: Optional. RAG collection ID (UUID) from DB. Uses collection's
                  configured source_directory, collection_name, and patterns.
                  When specified, 'path' argument is not required.

Example:
    # Index to default collection
    python scripts/build_rag_index.py "C:/my/documents"

    # Index to project-specific collection
    python scripts/build_rag_index.py "C:/my/project_docs" --project abc123-def456-...

    # Index using a DB-registered collection (uses its configured source_directory)
    python scripts/build_rag_index.py --collection abc123-def456-...

    # Clear and rebuild project index
    python scripts/build_rag_index.py "C:/my/docs" --project abc123 --clear
"""

import sys
import os
import asyncio
import argparse
import logging
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from src.rag import RagManager
from src.rag.config import get_project_collection_name

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def _get_collection_from_db(collection_id: str):
    """Fetch a RagCollection record from the database."""
    from src.memory.database import get_database_manager
    from src.memory.rag_collection_repository import RagCollectionRepository
    from uuid import UUID

    db = get_database_manager()
    if db is None:
        logger.error("Database not available. Make sure the app has been run at least once.")
        return None

    async with db.get_session() as session:
        return await RagCollectionRepository.get_by_id(session, UUID(collection_id))


async def _update_collection_status(collection_id: str, status: str,
                                     points_count: int = None,
                                     error_message: str = None):
    """Update collection status in the database."""
    from src.memory.database import get_database_manager
    from src.memory.rag_collection_repository import RagCollectionRepository
    from uuid import UUID

    db = get_database_manager()
    if db is None:
        return
    try:
        async with db.get_session() as session:
            await RagCollectionRepository.update_status(
                session, UUID(collection_id), status,
                points_count=points_count, error_message=error_message
            )
    except Exception as e:
        logger.warning(f"Failed to update DB status: {e}")


async def main():
    parser = argparse.ArgumentParser(description="Build RAG index from documents")
    parser.add_argument("path", nargs="?", default=None,
                        help="Path to directory or file to index (not required with --collection)")
    parser.add_argument("--clear", action="store_true", help="Clear existing index before processing")
    parser.add_argument("--project", "-p", type=str, default=None,
                        help="Project ID (UUID) to index documents for. If not specified, uses default collection.")
    parser.add_argument("--collection", "-c", type=str, default=None,
                        help="RAG collection ID (UUID) from DB. Uses collection's configured settings.")

    args = parser.parse_args()

    # ── Collection mode: look up from DB ──
    if args.collection:
        collection = await _get_collection_from_db(args.collection)
        if not collection:
            logger.error(f"Collection not found: {args.collection}")
            return

        collection_name = collection.collection_name
        target_path = Path(args.path) if args.path else Path(collection.source_directory)

        if not target_path.exists():
            logger.error(f"Path not found: {target_path}")
            await _update_collection_status(args.collection, "error",
                                             error_message=f"Path not found: {target_path}")
            return

        logger.info(f"Target: DB collection '{collection.name}' ({collection_name})")
        logger.info(f"Source: {target_path}")

        from src.rag.manager import get_rag_manager_for_collection
        manager = get_rag_manager_for_collection(collection_name)

        # Apply patterns from DB
        if collection.include_patterns:
            manager.config.source.include_patterns = collection.include_patterns
        if collection.exclude_patterns:
            manager.config.source.exclude_patterns = collection.exclude_patterns

        if not await manager.initialize():
            logger.error("Failed to initialize RAG manager")
            await _update_collection_status(args.collection, "error",
                                             error_message="Failed to initialize RAG manager")
            return

        await _update_collection_status(args.collection, "indexing")

    # ── Legacy mode: project or default ──
    else:
        if not args.path:
            parser.error("path is required when --collection is not specified")

        target_path = Path(args.path)
        project_id = args.project

        if not target_path.exists():
            logger.error(f"Path not found: {target_path}")
            return

        collection_name = get_project_collection_name(project_id)
        if project_id:
            logger.info(f"Target: Project-specific collection '{collection_name}'")
        else:
            logger.info(f"Target: Default collection '{collection_name}'")

        manager = RagManager(project_id=project_id)
        if not await manager.initialize():
            logger.error("Failed to initialize RAG manager")
            return

    # Show current status
    info = await manager.get_collection_info()
    logger.info(f"Current collection status: {info}")

    # Clear index if requested
    if args.clear:
        logger.info(f"Clearing existing index for collection: {collection_name}...")
        await manager.clear_index()
        # Re-initialize collection
        manager._initialized = False
        await manager.initialize()
        logger.info("Index cleared")

    # Process
    try:
        if target_path.is_file():
            logger.info(f"Indexing file: {target_path}")
            count = await manager.index_file(str(target_path))
            logger.info(f"Complete. Indexed {count} chunks.")

        elif target_path.is_dir():
            logger.info(f"Indexing directory: {target_path}")
            results = await manager.index_directory(str(target_path), recursive=True)
            total_chunks = sum(results.values())
            logger.info(f"Complete. Indexed {len(results)} files with {total_chunks} total chunks.")

        else:
            logger.error("Invalid path type")

    except KeyboardInterrupt:
        logger.warning("\nOperation cancelled by user")
        if args.collection:
            await _update_collection_status(args.collection, "error",
                                             error_message="Cancelled by user")
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        if args.collection:
            await _update_collection_status(args.collection, "error",
                                             error_message=str(e))
    else:
        # Update DB status on success
        if args.collection:
            final_info = await manager.get_collection_info()
            points_count = final_info.get("points_count", 0) if final_info else 0
            await _update_collection_status(args.collection, "ready",
                                             points_count=points_count)
    finally:
        # Show final status
        info = await manager.get_collection_info()
        logger.info(f"Final collection status: {info}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
