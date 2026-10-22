"""Measure the real conversation search path on disposable SQL/Qdrant rows.

Requires only localhost:55432/aoitalk_test_* PostgreSQL. Vectors/query input
are synthetic, not model-quality evidence; local Qdrant ranks the vectors.
Owns one random schema and in-memory collection, deletes neither originals
nor existing indexes. No credentials or plaintext user data are recorded.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import uuid

import numpy as np
from qdrant_client import QdrantClient, models
from sqlalchemy import Column, Index, MetaData, Table, event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def run(args):
    from src.memory.models import User, ConversationMessage, ConversationSession, ConversationParticipant
    from src.memory import database
    from src.memory.cross_session_memory import CrossSessionMemoryService
    from src.rag.config import QdrantConfig
    from src.rag.embedding_identity import EMBEDDING_FINGERPRINT
    from src.rag.qdrant_client import QdrantManager

    url = make_url(args.database_url)
    if not (url.host == '127.0.0.1' and url.port == 55432 and url.database.startswith('aoitalk_test_')):
        raise ValueError('Only disposable localhost:55432 aoitalk_test_* is allowed')
    schema = 'eg2_memory_fixture_' + uuid.uuid4().hex
    engine = create_async_engine(url)
    scoped = engine.execution_options(schema_translate_map={None: schema})
    metadata = MetaData()
    tables = {m: Table(m.__tablename__, metadata, *[Column(c.name, c.type, primary_key=c.primary_key)
               for c in m.__table__.columns]) for m in (User, ConversationMessage, ConversationSession, ConversationParticipant)}
    Index('fixture_messages_session', tables[ConversationMessage].c.session_id)
    Index('fixture_participant_actor', tables[ConversationParticipant].c.session_id,
          tables[ConversationParticipant].c.participant_id)
    actor, session_id = uuid.uuid4(), uuid.uuid4()
    counts = {'sql': 0, 'vector': 0}
    def sql_count(*_):
        counts['sql'] += 1
    event.listen(engine.sync_engine, 'before_cursor_execute', sql_count)
    client = QdrantClient(':memory:')
    manager = QdrantManager(QdrantConfig(collection_name='fixture_memory'))
    manager.client, manager._is_local_mode, manager._initialized = client, True, True
    client.create_collection(manager.config.collection_name, vectors_config=models.VectorParams(size=768, distance=models.Distance.COSINE))
    search = manager.search
    async def counted_search(**kwargs):
        counts['vector'] += 1
        return await search(**kwargs)
    manager.search = counted_search
    class SyntheticQuery:
        async def embed_query(self, query):
            return [1.] + [0.] * 767
    service = CrossSessionMemoryService()
    service._initialized, service.embedding, service.qdrant = True, SyntheticQuery(), manager
    original_factory = database.get_db_session
    async def factory():
        return AsyncSession(scoped, expire_on_commit=False)
    database.get_db_session = factory
    report = {'status': 'FAIL', 'synthetic_only': True,
              'backend': 'PostgreSQL 16 + QdrantClient local in-memory',
              'includes': 'real candidate SQL + one local vector query + fresh bulk SQL original read',
              'excludes': 'native query encoding, disk-persisted local Qdrant overhead, production corpus quality',
              'python_fetches_or_ranks_vectors': False, 'cases': []}
    created = False
    try:
        async with engine.begin() as db:
            await db.execute(CreateSchema(schema))
        created = True
        async with scoped.begin() as db:
            await db.run_sync(metadata.create_all)
            await db.execute(tables[User].insert(), {'id': actor, 'username': 'fixture', 'role': 'user', 'is_active': True})
            await db.execute(tables[ConversationSession].insert(), {'id': session_id, 'user_id': str(actor), 'character_name': 'aoi'})
        previous = 0
        started = time.perf_counter()
        for size in args.sizes:
            for offset in range(previous, size, 1000):
                stop = min(size, offset + 1000)
                identifiers = [uuid.uuid5(uuid.NAMESPACE_URL, f'eg2-memory-capacity:{i}') for i in range(offset, stop)]
                async with scoped.begin() as db:
                    await db.execute(tables[ConversationMessage].insert(), [
                        {'id': identifier, 'session_id': session_id, 'role': 'user', 'content': 'Synthetic 日本語 RTX-830 192.168.1.1',
                         'is_active_branch': True} for identifier in identifiers])
                # Score varies independently of insertion and ID order.
                points = []
                for i, identifier in enumerate(identifiers, offset):
                    vector = [0.] * 768
                    vector[0], vector[1] = .5 + (i % 1000) / 2500, .5
                    points.append(models.PointStruct(id=str(identifier), vector=vector,
                        payload={'doc_id': str(identifier), 'embedding_fingerprint': EMBEDDING_FINGERPRINT,
                                 'project_id': '__aoitalk_no_project__', 'text': ''}))
                client.upsert(manager.config.collection_name, points)
            previous = size
            from sqlalchemy import text
            async with scoped.begin() as db:
                await db.execute(text(f'ANALYZE "{schema}".conversation_messages'))
            values, sql_calls, vector_calls = [], [], []
            for _ in range(args.observations):
                counts.update(sql=0, vector=0)
                begin = time.perf_counter()
                hits = await service.search_relevant_conversations(str(actor), '日本語の資料', character_name='aoi', limit=5)
                values.append(time.perf_counter() - begin)
                sql_calls.append(counts['sql'])
                vector_calls.append(counts['vector'])
                assert len(hits) == 5 and all('Synthetic' in h['content'] for h in hits)
                assert [h['relevance_score'] for h in hits] == sorted([h['relevance_score'] for h in hits], reverse=True)
            case = {'allowed_messages': size, 'observations': args.observations,
                    'p50_seconds': float(np.percentile(values, 50)), 'p95_seconds': float(np.percentile(values, 95)),
                    'sql_statements_min': min(sql_calls), 'sql_statements_max': max(sql_calls),
                    'vector_queries_min': min(vector_calls), 'vector_queries_max': max(vector_calls),
                    'result_count': len(hits), 'original_candidates_max': 20}
            report['cases'].append(case)
            print(json.dumps(case), flush=True)
        report.update(status='PASS', total_fixture_and_query_seconds=time.perf_counter() - started)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    finally:
        database.get_db_session = original_factory
        client.close()
        if created:
            async with engine.begin() as db:
                await db.execute(DropSchema(schema, cascade=True))
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', required=True)
    parser.add_argument('--sizes', nargs='+', type=int, default=[100, 500, 145922])
    parser.add_argument('--observations', type=int, default=30)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
