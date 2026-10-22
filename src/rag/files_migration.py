"""Resume generic files from legacy source metadata and current canonical ACL.

Never discovers files by walking a disk or reuses old vectors/index text. Only
source_file metadata in the configured legacy generic collection is read.
An original needs a currently readable canonical KnowledgeSource root; orphaned
manual inputs, missing files and roots without ownership remain explicit gaps.
"""
from __future__ import annotations

from dataclasses import replace
import fnmatch
import hashlib
from pathlib import Path
import shutil
import tempfile

from sqlalchemy import select

from ..memory.models import KnowledgeSource
from ..services.media_asset_storage import open_asset
from ..services.search_sources import SourceRef
from ..services.storage_roots import StorageRoot, lexical_path
from .config import DEFAULT_COLLECTION_NAME
from .embedding import get_embedding
from .embedding_identity import document_input, EMBEDDING_FINGERPRINT
from .indexer import DocumentIndexer
from .index_io import run_index_io
from .qdrant_client import QdrantManager, SharedQdrantClient, _LOCAL_INDEX_IO_LOCK


async def migrate_legacy_files(session, resolver, config, ledger, *, collections=None, read_config=None):
    from qdrant_client import QdrantClient, models

    actor = await resolver._actor()
    source_config = read_config or config
    lock = _LOCAL_INDEX_IO_LOCK if config.qdrant.local_path or source_config.qdrant.local_path else None
    async def io(function, *args, **kwargs):
        return await run_index_io(function, *args, lock=lock, **kwargs)
    names = list(dict.fromkeys(collections or [DEFAULT_COLLECTION_NAME, source_config.qdrant.collection_name]))
    sources = list((await session.execute(select(KnowledgeSource.id, KnowledgeSource.root_path,
                                                KnowledgeSource.include_patterns, KnowledgeSource.exclude_patterns,
                                                KnowledgeSource.source_type))).all())
    roots = []
    readable = await resolver._acl_knowledge_sources([source.id for source in sources], actor)
    for source in sources:
        if source.source_type != 'growi' and source.id in readable:
            roots.append(source)
    report = {"collections": {}, "ready": 0, "excluded": 0, "failed": 0}
    source = source_config.qdrant
    shared_source = bool(source.local_path and config.qdrant.local_path and
                         lexical_path(source.local_path) == lexical_path(config.qdrant.local_path))
    try:
        # Different source/target stores must not share the singleton, which
        # closes its previous client when switching paths. Never bypass a live
        # local store's exclusive lock or copy its files while it is running.
        if source.local_path and not shared_source and not lexical_path(source.local_path).is_dir():
            raise FileNotFoundError("Registered legacy local store is missing")
        client = (await io(SharedQdrantClient.get_client, source.local_path) if shared_source else
                  await io(QdrantClient, path=source.local_path) if source.local_path else
                  QdrantClient(host=source.host, port=source.port, api_key=source.api_key))
    except Exception:
        report.update(failed=1, source_metadata='legacy_source_metadata_unavailable')
        ledger.mark(SourceRef('domain', 'files'), 'failed', error='legacy_source_metadata_unavailable')
        return report
    try:
        available = {c.name for c in (await io(client.get_collections)).collections}
        for name in names:
            if name not in available:
                report['collections'][name] = 'legacy_collection_absent'
                continue
            info = await io(client.get_collection, name)
            if isinstance(info.config.params.vectors, dict) or '__eg2_' in name:
                report['collections'][name] = 'not_a_legacy_generic_collection'
                continue
            files, offset, orphan_ids = set(), None, []
            while True:
                records, offset = await io(client.scroll, collection_name=name, limit=200,
                                           offset=offset, with_payload=['source_file'], with_vectors=False)
                for record in records:
                    path = (record.payload or {}).get('source_file')
                    if isinstance(path, str) and path:
                        files.add(path)
                    else:
                        orphan_ids.append(str(record.id))
                if len(files) + len(orphan_ids) > 100000:
                    raise RuntimeError('Legacy files inventory exceeds 100000; select a collection partition')
                if offset is None:
                    break
            for identifier in orphan_ids:
                ref = SourceRef('rag_file_orphan', name + ':' + identifier)
                ledger.mark(ref, 'excluded', error='legacy_index_has_no_original_source_metadata')
                report['excluded'] += 1
            encoder, target, indexer = None, None, None
            for file_name in sorted(files):
                key = hashlib.sha256((name + '\0' + file_name).encode()).hexdigest()
                ref = SourceRef('rag_file', key)
                ledger.mark(ref, 'running')
                try:
                    path = lexical_path(file_name)
                    match = None
                    for source in roots:
                        root = lexical_path(source.root_path)
                        try:
                            relative = path.relative_to(root).as_posix()
                        except ValueError:
                            continue
                        included = any(fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(relative, pattern)
                                       for pattern in (source.include_patterns or ['*.md', '*.txt', '*.pdf']))
                        excluded = any(fnmatch.fnmatch(part, pattern) or fnmatch.fnmatch(relative, pattern)
                                       for pattern in (source.exclude_patterns or []) for part in Path(relative).parts)
                        if included and not excluded:
                            match = (source, root, relative)
                            break
                    if match is None:
                        ledger.mark(ref, 'excluded', error='legacy_source_without_current_canonical_acl_or_policy')
                        report['excluded'] += 1
                        continue
                    source, root, relative = match
                    if not path.is_file():
                        ledger.mark(ref, 'excluded', error='original_file_missing')
                        report['excluded'] += 1
                        continue
                    storage = StorageRoot('eg2-files', 'Registered source', str(root), external=False, read_only=True)
                    scratch = Path(__file__).resolve().parents[2] / '.local'
                    scratch.mkdir(exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix='eg2-files-', dir=scratch) as directory:
                        snapshot = Path(directory) / path.name
                        def copy_original():
                            with open_asset(storage, relative) as (handle, _):
                                with snapshot.open('wb') as output:
                                    shutil.copyfileobj(handle, output)
                        await io(copy_original)
                        original_hash = await io(DocumentIndexer._compute_file_hash, snapshot)
                        if encoder is None:
                            encoder = get_embedding(config.embedding)
                            target = QdrantManager(replace(config.qdrant, collection_name=name + '__files'))
                            if not await encoder.initialize() or not await target.initialize():
                                raise RuntimeError('files_encoder_or_index_unavailable')
                            indexer = DocumentIndexer(target, encoder, config.chunking, config.source, config.indexing)
                            if not await indexer.initialize():
                                raise RuntimeError('files_document_reader_unavailable')
                        chunks = await io(indexer._prepare_chunks_from_file, snapshot, original_hash)
                        if not chunks:
                            raise RuntimeError('original_has_no_extractable_chunks')
                        for number, chunk in enumerate(chunks):
                            chunk.id = indexer._generate_chunk_id(file_name, number)
                            chunk.metadata.update(source_file=file_name, file_name=path.name, chunk_index=number,
                                                  knowledge_source_id=str(source.id), content_hash=original_hash,
                                                  embedding_input_hash=hashlib.sha256(document_input(chunk.text, path.name).encode()).hexdigest())
                        verified = 0
                        for offset in range(0, len(chunks), 32):
                            batch = chunks[offset:offset + 32]
                            pending = [chunk for chunk in batch if not await target.has_verified_document(
                                chunk.id, chunk.metadata['embedding_input_hash'])]
                            if pending:
                                vectors = await encoder.embed([c.text for c in pending], titles=[path.name] * len(pending))
                                if len(vectors) != len(pending) or not await target.add_documents(
                                    [c.id for c in pending], vectors, [c.text for c in pending], [c.metadata for c in pending]):
                                    raise RuntimeError('files_embedding_or_write_failed')
                            for chunk in batch:
                                if not await target.has_verified_document(chunk.id, chunk.metadata['embedding_input_hash']):
                                    raise RuntimeError('files_persisted_vector_verification_failed')
                            verified += len(batch)
                            ledger.mark(ref, 'running', completed=verified, total=len(chunks))
                        fresh = (await session.execute(select(KnowledgeSource.root_path, KnowledgeSource.include_patterns,
                            KnowledgeSource.exclude_patterns, KnowledgeSource.source_type).where(KnowledgeSource.id == source.id))).one_or_none()
                        current_acl = await resolver._acl_knowledge_sources([source.id], await resolver._actor())
                        if fresh is None or tuple(fresh) != (source.root_path, source.include_patterns, source.exclude_patterns,
                                                            source.source_type) or source.id not in current_acl:
                            raise RuntimeError('files_source_revoked_during_migration')
                        def verify_original():
                            with open_asset(storage, relative) as (handle, _):
                                digest = hashlib.sha256()
                                for block in iter(lambda: handle.read(1024 * 1024), b''):
                                    digest.update(block)
                                return digest.hexdigest()
                        if await io(verify_original) != original_hash:
                            raise RuntimeError('original_changed_during_migration')
                        current_ids = {chunk.id for chunk in chunks}
                        stale, point_cursor = [], None
                        while True:
                            points, point_cursor = await target._io(target.client.scroll,
                                collection_name=target.config.collection_name,
                                scroll_filter=models.Filter(must=[models.FieldCondition(key='source_file',
                                    match=models.MatchValue(value=file_name))]), offset=point_cursor,
                                limit=200, with_payload=['doc_id'], with_vectors=False)
                            stale.extend(point.id for point in points if (point.payload or {}).get('doc_id') not in current_ids)
                            if point_cursor is None:
                                break
                        for offset in range(0, len(stale), 200):
                            # Only verified-new-space derivatives, never legacy
                            # collections or originals, are replaced here.
                            await target._io(target.client.delete, collection_name=target.config.collection_name,
                                             points_selector=models.PointIdsList(points=stale[offset:offset + 200]))
                        ledger.mark(ref, 'ready', completed=verified, total=len(chunks), verified_hash=original_hash)
                        report['ready'] += 1
                except Exception as exc:
                    ledger.mark(ref, 'failed', error=str(exc))
                    report['failed'] += 1
            report['collections'][name] = {'registered_files': len(files), 'orphan_points': len(orphan_ids),
                                          'target': name + '__files__eg2_' + EMBEDDING_FINGERPRINT[:16]}
        ledger.mark(SourceRef('domain', 'files'), 'failed' if report['failed'] else 'excluded' if report['excluded'] else 'ready',
                    error=str(report))
        return report
    finally:
        if not shared_source:
            await io(client.close)
