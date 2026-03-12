"""RAGインデクサーの差分更新ロジックのテスト"""
import hashlib
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.rag.indexer import DocumentIndexer, DocumentChunk
from src.rag.config import ChunkingConfig, SourceConfig, IndexingConfig


class TestComputeFileHash:
    def test_returns_sha256_hex(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("hello world", encoding="utf-8")

        result = DocumentIndexer._compute_file_hash(f)

        expected = hashlib.sha256(b"hello world").hexdigest()
        assert result == expected

    def test_different_content_different_hash(self, tmp_path):
        f1 = tmp_path / "a.md"
        f2 = tmp_path / "b.md"
        f1.write_text("content A", encoding="utf-8")
        f2.write_text("content B", encoding="utf-8")

        assert DocumentIndexer._compute_file_hash(f1) != DocumentIndexer._compute_file_hash(f2)

    def test_same_content_same_hash(self, tmp_path):
        f1 = tmp_path / "a.md"
        f2 = tmp_path / "b.md"
        f1.write_text("same", encoding="utf-8")
        f2.write_text("same", encoding="utf-8")

        assert DocumentIndexer._compute_file_hash(f1) == DocumentIndexer._compute_file_hash(f2)

    def test_binary_file(self, tmp_path):
        f = tmp_path / "binary.bin"
        data = bytes(range(256))
        f.write_bytes(data)

        result = DocumentIndexer._compute_file_hash(f)
        expected = hashlib.sha256(data).hexdigest()
        assert result == expected


class TestGenerateChunkId:
    def setup_method(self):
        self.indexer = DocumentIndexer(
            qdrant=MagicMock(),
            embedding=MagicMock(),
            chunking_config=ChunkingConfig(),
            source_config=SourceConfig(),
        )

    def test_deterministic(self):
        id1 = self.indexer._generate_chunk_id("file.md", 0)
        id2 = self.indexer._generate_chunk_id("file.md", 0)
        assert id1 == id2

    def test_different_for_different_index(self):
        id1 = self.indexer._generate_chunk_id("file.md", 0)
        id2 = self.indexer._generate_chunk_id("file.md", 1)
        assert id1 != id2

    def test_different_for_different_file(self):
        id1 = self.indexer._generate_chunk_id("a.md", 0)
        id2 = self.indexer._generate_chunk_id("b.md", 0)
        assert id1 != id2


class TestIndexDirectoryDiffDetection:
    """index_directoryの差分判定ロジック（Phase 1-2）のテスト。

    LlamaIndex依存を避けるため、_prepare_chunks_from_fileをモックし、
    scan_source_file_hashesとdelete_by_source_fileの呼び出しを検証する。
    """

    def _make_indexer(self):
        qdrant = AsyncMock()
        embedding = AsyncMock()
        indexer = DocumentIndexer(
            qdrant=qdrant,
            embedding=embedding,
            chunking_config=ChunkingConfig(),
            source_config=SourceConfig(include_patterns=["*.md"]),
            indexing_config=IndexingConfig(),
        )
        indexer._llama_available = True
        return indexer, qdrant, embedding

    @pytest.mark.asyncio
    async def test_new_files_indexed(self, tmp_path):
        """新規ファイルが正しくインデックスされる"""
        indexer, qdrant, embedding = self._make_indexer()

        # 新規ファイルを作成
        f = tmp_path / "new.md"
        f.write_text("new content", encoding="utf-8")
        content_hash = DocumentIndexer._compute_file_hash(f)

        # DB上にはまだ何もない
        qdrant.scan_source_file_hashes.return_value = {}

        chunk = DocumentChunk(
            id="abc", text="new content", source_file=str(f),
            chunk_index=0, metadata={"source_file": str(f), "content_hash": content_hash}
        )
        embedding.embed.return_value = [[0.1] * 1024]
        qdrant.add_documents.return_value = True

        with patch.object(indexer, "_prepare_chunks_from_file", return_value=[chunk]):
            result = await indexer.index_directory(str(tmp_path))

        assert str(f) in result
        assert result[str(f)] == 1
        qdrant.delete_by_source_file.assert_not_called()
        qdrant.add_documents.assert_called_once()

    @pytest.mark.asyncio
    async def test_unchanged_files_skipped(self, tmp_path):
        """未変更ファイルはスキップされる"""
        indexer, qdrant, embedding = self._make_indexer()

        f = tmp_path / "existing.md"
        f.write_text("same content", encoding="utf-8")
        content_hash = DocumentIndexer._compute_file_hash(f)

        # DB上に同じハッシュが存在
        qdrant.scan_source_file_hashes.return_value = {str(f): content_hash}

        result = await indexer.index_directory(str(tmp_path))

        assert result == {}
        qdrant.delete_by_source_file.assert_not_called()
        embedding.embed.assert_not_called()

    @pytest.mark.asyncio
    async def test_modified_files_reindexed(self, tmp_path):
        """変更されたファイルは旧チャンク削除→再インデックスされる"""
        indexer, qdrant, embedding = self._make_indexer()

        f = tmp_path / "modified.md"
        f.write_text("updated content", encoding="utf-8")
        new_hash = DocumentIndexer._compute_file_hash(f)

        # DB上には古いハッシュ
        qdrant.scan_source_file_hashes.return_value = {str(f): "old_hash_value"}

        chunk = DocumentChunk(
            id="xyz", text="updated content", source_file=str(f),
            chunk_index=0, metadata={"source_file": str(f), "content_hash": new_hash}
        )
        embedding.embed.return_value = [[0.2] * 1024]
        qdrant.add_documents.return_value = True
        qdrant.delete_by_source_file.return_value = True

        with patch.object(indexer, "_prepare_chunks_from_file", return_value=[chunk]):
            result = await indexer.index_directory(str(tmp_path))

        assert str(f) in result
        qdrant.delete_by_source_file.assert_called_once_with(str(f))
        qdrant.add_documents.assert_called_once()

    @pytest.mark.asyncio
    async def test_deleted_files_cleaned(self, tmp_path):
        """ディレクトリから消えたファイルのチャンクがDBから削除される"""
        indexer, qdrant, embedding = self._make_indexer()

        # DB上にはこのディレクトリ配下のファイルが存在するが、実ファイルは消えている
        deleted_file = str(tmp_path / "deleted.md")
        qdrant.scan_source_file_hashes.return_value = {
            deleted_file: "some_hash"
        }

        result = await indexer.index_directory(str(tmp_path))

        qdrant.delete_by_source_file.assert_called_once_with(deleted_file)
        assert result == {}

    @pytest.mark.asyncio
    async def test_other_directory_not_affected(self, tmp_path):
        """別ディレクトリのデータは差分更新で削除されない"""
        indexer, qdrant, embedding = self._make_indexer()

        # 今回のディレクトリに新規ファイルを作成
        f = tmp_path / "new.md"
        f.write_text("new content", encoding="utf-8")
        content_hash = DocumentIndexer._compute_file_hash(f)

        # DB上には別ディレクトリのファイルが存在
        qdrant.scan_source_file_hashes.return_value = {
            "C:/other_project/data.md": "other_hash"
        }

        chunk = DocumentChunk(
            id="abc", text="new content", source_file=str(f),
            chunk_index=0, metadata={"source_file": str(f), "content_hash": content_hash}
        )
        embedding.embed.return_value = [[0.1] * 1024]
        qdrant.add_documents.return_value = True

        with patch.object(indexer, "_prepare_chunks_from_file", return_value=[chunk]):
            result = await indexer.index_directory(str(tmp_path))

        # 別ディレクトリのファイルは削除されない
        qdrant.delete_by_source_file.assert_not_called()
        assert str(f) in result

    @pytest.mark.asyncio
    async def test_legacy_data_without_hash_reindexed(self, tmp_path):
        """content_hashがNoneのレガシーデータは変更扱いで再インデックスされる"""
        indexer, qdrant, embedding = self._make_indexer()

        f = tmp_path / "legacy.md"
        f.write_text("legacy content", encoding="utf-8")
        content_hash = DocumentIndexer._compute_file_hash(f)

        # DB上にはハッシュなし（レガシー）
        qdrant.scan_source_file_hashes.return_value = {str(f): None}

        chunk = DocumentChunk(
            id="leg", text="legacy content", source_file=str(f),
            chunk_index=0, metadata={"source_file": str(f), "content_hash": content_hash}
        )
        embedding.embed.return_value = [[0.3] * 1024]
        qdrant.add_documents.return_value = True
        qdrant.delete_by_source_file.return_value = True

        with patch.object(indexer, "_prepare_chunks_from_file", return_value=[chunk]):
            result = await indexer.index_directory(str(tmp_path))

        assert str(f) in result
        qdrant.delete_by_source_file.assert_called_once_with(str(f))
