"""
Document indexer using LlamaIndex for RAG.
"""

import logging
import asyncio
import hashlib
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime

from .config import ChunkingConfig, SourceConfig, IndexingConfig
from .embedding import BgeM3Embedding
from .qdrant_client import QdrantManager

logger = logging.getLogger(__name__)

# Lazy imports
_SimpleDirectoryReader = None
_SentenceSplitter = None


def _load_llama_index():
    """Lazy load LlamaIndex components."""
    global _SimpleDirectoryReader, _SentenceSplitter
    try:
        from llama_index.core import SimpleDirectoryReader
        from llama_index.core.node_parser import SentenceSplitter
        _SimpleDirectoryReader = SimpleDirectoryReader
        _SentenceSplitter = SentenceSplitter
        return True
    except ImportError:
        logger.warning("llama-index not installed. Indexing features will be disabled.")
        return False


@dataclass
class DocumentChunk:
    """A chunk of a document."""
    id: str
    text: str
    source_file: str
    chunk_index: int
    metadata: Dict[str, Any]


class DocumentIndexer:
    """Document indexer for RAG using LlamaIndex."""
    
    def __init__(
        self,
        qdrant: QdrantManager,
        embedding: BgeM3Embedding,
        chunking_config: ChunkingConfig,
        source_config: SourceConfig,
        indexing_config: Optional[IndexingConfig] = None
    ):
        """Initialize document indexer.
        
        Args:
            qdrant: Qdrant manager
            embedding: Embedding model
            chunking_config: Chunking configuration
            source_config: Source configuration
            indexing_config: Indexing performance configuration
        """
        self.qdrant = qdrant
        self.embedding = embedding
        self.chunking_config = chunking_config
        self.source_config = source_config
        self.indexing_config = indexing_config or IndexingConfig()
        self._llama_available = False
    
    async def initialize(self) -> bool:
        """Initialize the indexer.
        
        Returns:
            True if initialization successful
        """
        self._llama_available = _load_llama_index()
        return self._llama_available
    
    @staticmethod
    def _compute_file_hash(file_path: Path) -> str:
        """Compute SHA256 hash of file content.

        Args:
            file_path: Path to the file

        Returns:
            Hex digest of SHA256 hash
        """
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _generate_chunk_id(self, source_file: str, chunk_index: int) -> str:
        """Generate unique ID for a document chunk.
        
        Args:
            source_file: Source file path
            chunk_index: Index of the chunk within the document
            
        Returns:
            Unique chunk ID
        """
        content = f"{source_file}:{chunk_index}"
        return hashlib.md5(content.encode()).hexdigest()
    
    async def index_file(self, file_path: str) -> int:
        """Index a single file.
        
        Args:
            file_path: Path to the file to index
            
        Returns:
            Number of chunks indexed
        """
        if not self._llama_available:
            logger.error("LlamaIndex not available")
            return 0
        
        try:
            path = Path(file_path)
            if not path.exists():
                logger.error(f"File not found: {file_path}")
                return 0
            
            # Load document
            reader = _SimpleDirectoryReader(
                input_files=[str(path)]
            )
            documents = reader.load_data()
            
            if not documents:
                logger.warning(f"No content loaded from: {file_path}")
                return 0
            
            # Compute content hash for change detection
            content_hash = self._compute_file_hash(path)

            # Split into chunks
            splitter = _SentenceSplitter(
                chunk_size=self.chunking_config.chunk_size,
                chunk_overlap=self.chunking_config.chunk_overlap
            )

            chunks: List[DocumentChunk] = []
            for doc in documents:
                nodes = splitter.get_nodes_from_documents([doc])

                for i, node in enumerate(nodes):
                    chunk_id = self._generate_chunk_id(file_path, i)
                    chunks.append(DocumentChunk(
                        id=chunk_id,
                        text=node.text,
                        source_file=file_path,
                        chunk_index=i,
                        metadata={
                            "source_file": file_path,
                            "file_name": path.name,
                            "chunk_index": i,
                            "content_hash": content_hash,
                            "indexed_at": datetime.now().isoformat()
                        }
                    ))
            
            if not chunks:
                return 0
            
            # Generate embeddings
            texts = [chunk.text for chunk in chunks]
            embeddings = await self.embedding.embed(texts)
            
            if not embeddings:
                logger.error("Failed to generate embeddings")
                return 0
            
            # Store in Qdrant
            success = await self.qdrant.add_documents(
                ids=[chunk.id for chunk in chunks],
                embeddings=embeddings,
                texts=texts,
                metadata_list=[chunk.metadata for chunk in chunks]
            )
            
            if success:
                logger.info(f"Indexed {len(chunks)} chunks from {file_path}")
                return len(chunks)
            
            return 0
            
        except Exception as e:
            logger.error(f"Failed to index file {file_path}: {e}")
            return 0
    
    def _prepare_chunks_from_file(self, file_path: Path, content_hash: Optional[str] = None) -> List[DocumentChunk]:
        """Prepare chunks from a file without embedding (synchronous).

        Args:
            file_path: Path to the file
            content_hash: Pre-computed content hash (computed if None)

        Returns:
            List of DocumentChunk objects
        """
        if not self._llama_available:
            return []

        try:
            if not file_path.exists():
                logger.warning(f"File not found: {file_path}")
                return []

            if content_hash is None:
                content_hash = self._compute_file_hash(file_path)

            # Load document
            reader = _SimpleDirectoryReader(input_files=[str(file_path)])
            documents = reader.load_data()

            if not documents:
                logger.warning(f"No content loaded from: {file_path}")
                return []

            # Split into chunks
            splitter = _SentenceSplitter(
                chunk_size=self.chunking_config.chunk_size,
                chunk_overlap=self.chunking_config.chunk_overlap
            )

            chunks: List[DocumentChunk] = []
            for doc in documents:
                nodes = splitter.get_nodes_from_documents([doc])

                for i, node in enumerate(nodes):
                    chunk_id = self._generate_chunk_id(str(file_path), i)
                    chunks.append(DocumentChunk(
                        id=chunk_id,
                        text=node.text,
                        source_file=str(file_path),
                        chunk_index=i,
                        metadata={
                            "source_file": str(file_path),
                            "file_name": file_path.name,
                            "chunk_index": i,
                            "content_hash": content_hash,
                            "indexed_at": datetime.now().isoformat()
                        }
                    ))

            return chunks

        except Exception as e:
            logger.error(f"Failed to prepare chunks from {file_path}: {e}")
            return []
    
    async def index_directory(self, directory: str, recursive: bool = True) -> Dict[str, int]:
        """Index all matching files in a directory with incremental updates.

        Detects new, modified, and deleted files by comparing content hashes.

        Args:
            directory: Directory path
            recursive: Whether to search recursively

        Returns:
            Dict mapping file paths to number of chunks indexed
        """
        file_chunk_counts: Dict[str, int] = {}
        dir_path = Path(directory)

        if not dir_path.exists():
            logger.error(f"Directory not found: {directory}")
            return file_chunk_counts

        if not self._llama_available:
            logger.error("LlamaIndex not available")
            return file_chunk_counts

        # ── Phase 1: Discover files & compute hashes ──
        logger.info("Phase 1: Discovering files and computing hashes...")
        files_to_index: set[Path] = set()

        for pattern in self.source_config.include_patterns:
            if recursive:
                matches = list(dir_path.rglob(pattern))
            else:
                matches = list(dir_path.glob(pattern))

            for file_path in matches:
                relative_path = str(file_path.relative_to(dir_path))
                excluded = False
                for exclude in self.source_config.exclude_patterns:
                    if file_path.match(exclude) or relative_path.startswith(exclude.rstrip('*')):
                        excluded = True
                        break
                if not excluded and file_path.is_file():
                    files_to_index.add(file_path)

        files_list = list(files_to_index)
        total_files = len(files_list)
        logger.info(f"Found {total_files} files")

        # Compute hashes for all local files
        local_hashes: Dict[str, str] = {}
        for fp in files_list:
            local_hashes[str(fp)] = self._compute_file_hash(fp)

        # ── Phase 2: Diff detection ──
        # Scope: only compare against existing entries that belong to this directory.
        # This allows multiple directories to coexist in the same collection.
        logger.info("Phase 2: Checking for changes...")
        all_existing_hashes = await self.qdrant.scan_source_file_hashes()

        # Normalize directory prefix for comparison
        dir_prefix = str(dir_path.resolve())
        existing_hashes = {
            k: v for k, v in all_existing_hashes.items()
            if str(Path(k).resolve()).startswith(dir_prefix)
        }

        local_keys = set(local_hashes.keys())
        existing_keys = set(existing_hashes.keys())

        new_files = local_keys - existing_keys
        deleted_files = existing_keys - local_keys
        common_files = local_keys & existing_keys

        modified_files: set[str] = set()
        unchanged_files: set[str] = set()
        for sf in common_files:
            if existing_hashes[sf] is None or existing_hashes[sf] != local_hashes[sf]:
                modified_files.add(sf)
            else:
                unchanged_files.add(sf)

        logger.info(
            f"Diff result: {len(new_files)} new, {len(modified_files)} modified, "
            f"{len(unchanged_files)} unchanged, {len(deleted_files)} deleted"
        )

        # Delete chunks for modified and deleted files
        files_to_delete = modified_files | deleted_files
        if files_to_delete:
            logger.info(f"Deleting outdated chunks for {len(files_to_delete)} files...")
            for sf in files_to_delete:
                await self.qdrant.delete_by_source_file(sf)

        # Files that need (re-)indexing
        files_to_process = {Path(sf) for sf in (new_files | modified_files)}

        if not files_to_process:
            logger.info("All files up to date. Nothing to index.")
            return file_chunk_counts

        # ── Phase 3: Chunk preparation ──
        process_list = list(files_to_process)
        total_to_process = len(process_list)
        logger.info(f"Phase 3: Reading and chunking {total_to_process} files...")
        all_chunks: List[DocumentChunk] = []

        loop = asyncio.get_event_loop()
        max_concurrent = self.indexing_config.max_concurrent_files

        for batch_start in range(0, total_to_process, max_concurrent):
            batch_files = process_list[batch_start:batch_start + max_concurrent]

            tasks = [
                loop.run_in_executor(
                    None, self._prepare_chunks_from_file, f, local_hashes[str(f)]
                )
                for f in batch_files
            ]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for file_path, chunks_result in zip(batch_files, batch_results):
                if isinstance(chunks_result, Exception):
                    logger.error(f"Error processing {file_path}: {chunks_result}")
                    file_chunk_counts[str(file_path)] = 0
                else:
                    all_chunks.extend(chunks_result)
                    file_chunk_counts[str(file_path)] = len(chunks_result)

            processed = min(batch_start + max_concurrent, total_to_process)
            if processed % 100 == 0 or processed == total_to_process:
                logger.info(f"Read {processed}/{total_to_process} files ({len(all_chunks)} chunks)")

        if not all_chunks:
            logger.warning("No chunks generated from files.")
            return file_chunk_counts

        # ── Phase 4: Embed and store ──
        total_chunks = len(all_chunks)
        logger.info(f"Phase 4: Embedding and storing {total_chunks} chunks...")

        batch_size = self.indexing_config.batch_size
        progress_interval = self.indexing_config.progress_interval
        indexed_count = 0

        for batch_start in range(0, total_chunks, batch_size):
            batch_chunks = all_chunks[batch_start:batch_start + batch_size]
            texts = [chunk.text for chunk in batch_chunks]

            embeddings = await self.embedding.embed(texts)

            if not embeddings:
                logger.error(f"Failed to generate embeddings for batch starting at {batch_start}")
                continue

            success = await self.qdrant.add_documents(
                ids=[chunk.id for chunk in batch_chunks],
                embeddings=embeddings,
                texts=texts,
                metadata_list=[chunk.metadata for chunk in batch_chunks]
            )

            if success:
                indexed_count += len(batch_chunks)

            if indexed_count % progress_interval < batch_size or batch_start + batch_size >= total_chunks:
                logger.info(f"Indexed {indexed_count}/{total_chunks} chunks ({indexed_count * 100 // total_chunks}%)")

        logger.info(f"Complete! Processed {len(file_chunk_counts)} files with {indexed_count} chunks.")

        return file_chunk_counts
    
    async def index_text(
        self,
        text: str,
        source_name: str = "manual_input",
        metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        """Index raw text content.
        
        Args:
            text: Text content to index
            source_name: Name/identifier for the source
            metadata: Additional metadata
            
        Returns:
            Number of chunks indexed
        """
        if not text.strip():
            return 0
        
        try:
            # Split into chunks manually
            chunk_size = self.chunking_config.chunk_size
            overlap = self.chunking_config.chunk_overlap
            
            chunks: List[DocumentChunk] = []
            start = 0
            chunk_idx = 0
            
            while start < len(text):
                end = min(start + chunk_size, len(text))
                chunk_text = text[start:end]
                
                if chunk_text.strip():
                    chunk_id = self._generate_chunk_id(source_name, chunk_idx)
                    chunk_metadata = {
                        "source_name": source_name,
                        "chunk_index": chunk_idx,
                        "indexed_at": datetime.now().isoformat(),
                        **(metadata or {})
                    }
                    
                    chunks.append(DocumentChunk(
                        id=chunk_id,
                        text=chunk_text,
                        source_file=source_name,
                        chunk_index=chunk_idx,
                        metadata=chunk_metadata
                    ))
                    chunk_idx += 1
                
                start = end - overlap if end < len(text) else end
            
            if not chunks:
                return 0
            
            # Generate embeddings
            texts = [chunk.text for chunk in chunks]
            embeddings = await self.embedding.embed(texts)
            
            if not embeddings:
                return 0
            
            # Store in Qdrant
            success = await self.qdrant.add_documents(
                ids=[chunk.id for chunk in chunks],
                embeddings=embeddings,
                texts=texts,
                metadata_list=[chunk.metadata for chunk in chunks]
            )
            
            return len(chunks) if success else 0
            
        except Exception as e:
            logger.error(f"Failed to index text: {e}")
            return 0
