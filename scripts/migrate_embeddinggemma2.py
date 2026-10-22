"""Reindex canonical AoiTalk sources into the versioned EmbeddingGemma 2 space.

Default shadow mode reads the original DB in a forced read-only transaction,
writes a separate local Qdrant destination and generation-specific SQLite
checkpoints, and never acknowledges the live DB's index metadata. Live mode
requires an explicit stopped-runtime assertion; it does not stop services,
switch runtime configuration, or delete old collections/originals.
"""

from __future__ import annotations
import argparse
import asyncio
from collections.abc import Mapping
from dataclasses import replace
import json
import hashlib
from pathlib import Path
import re
import sqlite3
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contextlib import asynccontextmanager
from sqlalchemy import select
from src.memory.database import DatabaseManager
from src.memory.models import (
    DocsLibrary,
    KnowledgeSource,
    ConversationMessage,
)
from src.rag.docs_index import DocsIndexService
from src.knowledge.index_service import KnowledgeIndexService
from src.rag.media_index import MediaIndexService, MigrationLedger
from src.rag.qdrant_client import QdrantManager
from src.rag.embedding import get_embedding
from src.memory.cross_session_memory import _memory_inventory_statement
from src.services.privacy_masking_sql import privacy_masking_source_sql
from src.memory.conversation_index import migrate_memory_unit, purge_private_memory_points
from src.rag.embedding_identity import (
    EMBEDDING_IDENTITY,
    EMBEDDING_FINGERPRINT,
)
from src.rag.shadow_migration import (
    DocsShadowIndex, KnowledgeShadowIndex, ShadowCheckpoint, read_only_source_session,
)
from src.services.search_sources import (
    SearchSourceResolver,
    SearchScope,
    SourceRef,
    SourceUnavailable,
)


def migration_paths(args):
    """Validate operator intent before opening any DB/index/checkpoint store."""
    mode = getattr(args, "mode", "shadow")
    generation = getattr(args, "generation", None)
    if mode != "candidate" and getattr(args, "shadow_manifest", None):
        raise ValueError("--shadow-manifest requires --mode candidate")
    if mode in {"shadow", "candidate"}:
        if not generation or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", generation) is None:
            raise ValueError("Shadow mode requires --generation (1-64 ASCII letters/digits/_/-)")
        if not args.status and not args.local_path:
            raise ValueError("Shadow mode requires a separate --local-path destination")
        base = Path("data/search") / mode / generation
        manifest = args.manifest or base / "migration.sqlite"
        media_manifest = args.media_manifest or base / "media.sqlite"
        live_paths = {Path("data/search/embeddinggemma2-migration.sqlite").resolve(),
                      Path("data/search/embeddinggemma2.sqlite").resolve()}
        if manifest.resolve() in live_paths or media_manifest.resolve() in live_paths:
            raise ValueError("Shadow checkpoints must not replace runtime migration/media metadata")
        if mode == "candidate":
            if getattr(args, "user_id", None):
                raise ValueError("Candidate uses the canonical shared inventory; omit --user-id")
            if getattr(args, "domain", "all") == "files" or getattr(args, "files_collection", None):
                raise ValueError("Candidate covers registered canonical sources; legacy generic files require separate review")
            if getattr(args, "media_manifest", None):
                raise ValueError("Candidate uses one independent --manifest; omit --media-manifest")
    elif mode == "live":
        if not args.status and not getattr(args, "confirm_runtime_stopped", False):
            raise ValueError("Live migration requires --confirm-runtime-stopped; stop every index writer first")
        manifest = args.manifest or Path("data/search/embeddinggemma2-migration.sqlite")
        media_manifest = args.media_manifest or Path("data/search/embeddinggemma2.sqlite")
    else:
        raise ValueError("Unknown migration mode")
    if manifest.resolve() == media_manifest.resolve():
        raise ValueError("Domain and media checkpoints require distinct SQLite files")
    return mode, manifest, media_manifest


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_rag_snapshot(value):
    """Reject malformed stored sections before RagConfig can apply defaults."""
    if not isinstance(value, Mapping):
        raise RuntimeError("Existing DB RAG configuration is malformed; migration stopped")
    for section in ("qdrant", "embedding"):
        if section not in value or not isinstance(value[section], Mapping):
            raise RuntimeError(f"Existing DB RAG configuration section is missing or malformed: {section}")
    qdrant = value["qdrant"]
    local_path = qdrant.get("local_path")
    if local_path is not None and (not isinstance(local_path, str) or not local_path.strip()):
        raise RuntimeError("Existing DB RAG configuration local_path is malformed")
    if "host" in qdrant and (not isinstance(qdrant["host"], str) or not qdrant["host"].strip()):
        raise RuntimeError("Existing DB RAG configuration host is malformed")
    if "port" in qdrant and (not _is_int(qdrant["port"]) or not 1 <= qdrant["port"] <= 65535):
        raise RuntimeError("Existing DB RAG configuration port is malformed")
    if "mode" in qdrant and qdrant["mode"] not in {"external", "local-managed"}:
        raise RuntimeError("Existing DB RAG configuration mode is malformed")
    if local_path is None:
        required = ("host", "port", "mode")
        if any(key not in qdrant for key in required):
            raise RuntimeError("Existing DB RAG configuration endpoint is incomplete")
    embedding = value["embedding"]
    from src.rag.embedding_identity import MODEL_ID
    if "model" in embedding and embedding["model"] != MODEL_ID:
        raise RuntimeError("Existing DB RAG configuration embedding model is malformed or mismatched")
    if "batch_size" in embedding and (not _is_int(embedding["batch_size"]) or embedding["batch_size"] <= 0):
        raise RuntimeError("Existing DB RAG configuration embedding batch_size is malformed")


async def shadow_config(session):
    # Config()/get_rag_config() perform first-use/default/encryption writeback.
    # Read only the RAG subtree; provider credentials are not projected.
    from src.memory.models import AppConfigSetting
    from src.rag.config import RagConfig
    from src.security.field_crypto import decrypt_json_secret_leaves
    value = (await session.execute(select(AppConfigSetting.value["rag"]).where(
        AppConfigSetting.key == "global"))).scalar_one_or_none()
    if not isinstance(value, Mapping):
        raise RuntimeError("Existing DB RAG configuration unavailable; shadow migration did not run")
    try:
        rag = decrypt_json_secret_leaves({"rag": value}, aad_prefix="app_config_settings.value")["rag"]
        _validate_rag_snapshot(rag)
        return RagConfig.from_dict(rag)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("Existing DB RAG configuration is malformed; migration stopped") from exc


@asynccontextmanager
async def source_session(db, mode):
    # Neither mode initializes/migrates the application DB. Operators must
    # provision the schema before this separate derived-index command runs.
    if mode in {"shadow", "candidate"}:
        async with read_only_source_session(db.SessionLocal) as session:
            yield session
    else:
        session = db.SessionLocal()
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()


def validate_shadow_destination(source_config, target):
    if source_config.qdrant.local_path:
        source = Path(source_config.qdrant.local_path).resolve()
        target = target.resolve()
        if source == target or source in target.parents or target in source.parents:
            raise ValueError("Shadow Qdrant destination must be separate from the runtime store")


def validate_shadow_manifest(path):
    """Inspect existing SQLite before its constructor could create tables."""
    if not path.exists():
        return
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as store:
        names = {row[0] for row in store.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "shadow_generation" in names:
            return
        if names - {"embedding_sources", "embedding_attempts"}:
            raise ValueError("Shadow manifest is an existing non-shadow database")
        if "embedding_sources" in names and store.execute("SELECT 1 FROM embedding_sources LIMIT 1").fetchone():
            raise ValueError("Existing non-shadow migration metadata cannot be overwritten")


def read_ledger_progress(path):
    """Operator status does not create/migrate a checkpoint database."""
    if not path.exists():
        return {"identity": EMBEDDING_FINGERPRINT, "states": {}, "failures": [], "attempts": [], "not_started": True}
    ledger = object.__new__(MigrationLedger)
    ledger.path = path
    ledger.connect = lambda: sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    return ledger.progress()


async def docs_library_ids(session, resolver, *, shadow):
    """Enumerate complete actor-visible shadow libraries, without widening live.

    Own empty libraries still need a ready-zero checkpoint. Foreign libraries
    enter only when the canonical Docs ACL/render relation has a current row;
    no encoder is initialized merely to discover an invisible library.
    """
    from src.memory.models import KnowledgeNode
    from src.services.docs_graph_service import DocsGraphService

    cursor = None
    while True:
        statement = select(DocsLibrary.id, DocsLibrary.owner_user_id).order_by(DocsLibrary.id).limit(1000)
        if not shadow:
            statement = statement.where(DocsLibrary.owner_user_id == resolver.scope.user_id)
        if cursor is not None:
            statement = statement.where(DocsLibrary.id > cursor)
        rows = (await session.execute(statement)).all()
        if not rows:
            break
        for row in rows:
            if row.owner_user_id == resolver.scope.user_id:
                yield row.id
                continue
            visible, _ = await DocsGraphService(session)._build_structured_query_statement(
                docs_library_id=row.id, user_id=resolver.scope.user_id,
                project_id=resolver.scope.project_id)
            from src.services.docs_acl import apply_docs_search_visibility

            visible = apply_docs_search_visibility(visible, user_id=resolver.scope.user_id)
            eligible = visible.with_only_columns(KnowledgeNode.id, maintain_column_froms=True).limit(1)
            if (await session.execute(eligible)).first() is not None:
                yield row.id
        cursor = rows[-1].id


async def run(args):
    mode, manifest, media_manifest = migration_paths(args)
    if args.status:
        if mode == "candidate":
            ready = False
            if manifest.exists():
                with sqlite3.connect(manifest.resolve().as_uri() + "?mode=ro", uri=True) as store:
                    tables = {r[0] for r in store.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if "candidate_generation" not in tables:
                        raise ValueError("Candidate status requires a candidate manifest")
                    binding = store.execute("SELECT binding FROM candidate_generation WHERE singleton=1").fetchone()
                    if binding is None or json.loads(binding[0])[1] != args.generation:
                        raise ValueError("Candidate status generation binding mismatch")
                    domain = json.loads(binding[0])[6]
                    ready = store.execute("SELECT 1 FROM embedding_sources WHERE source_key=? AND status='ready' AND identity=?",
                                          ("candidate:" + domain, EMBEDDING_FINGERPRINT)).fetchone() is not None
                    columns = {r[1] for r in store.execute("PRAGMA table_info(candidate_units)")}
                    if not {"input_digest", "dense_digest"}.issubset(columns):
                        ready = False
                    elif store.execute("SELECT 1 FROM candidate_units WHERE input_digest IS NULL OR dense_digest IS NULL LIMIT 1").fetchone():
                        ready = False
                    if store.execute("SELECT 1 FROM embedding_sources WHERE identity=? AND kind!='candidate' AND status IN ('failed','running','interrupted') LIMIT 1",
                                     (EMBEDDING_FINGERPRINT,)).fetchone():
                        ready = False
            return {"mode": mode, "generation": args.generation, "progress": read_ledger_progress(manifest),
                    "candidate_ready": ready,
                    "production_cutover": "not_performed", "live_metadata": "not_acknowledged"}
        from src.services.media_index_worker import MediaIndexQueue
        report = read_ledger_progress(manifest)
        return {"mode": mode, "generation": getattr(args, "generation", None), "progress": report,
                "reconciliation": MediaIndexQueue.read_reconciliation_overview()}
    db = DatabaseManager()
    try:
        async with source_session(db, mode) as session:
            return await migrate_session(args, session, db, mode, manifest, media_manifest)
    finally:
        await db.close()


async def migrate_session(args, session, db, mode, manifest, media_manifest):
    source_config = await shadow_config(session)
    config = source_config
    if mode != "candidate":
        from src.rag.runtime_candidate import CandidateCheckpoint
        target = args.local_path or source_config.qdrant.local_path
        if target and (Path(target) / CandidateCheckpoint.marker_name).exists():
            raise ValueError("A shared candidate cannot be adopted by actor shadow/live mode; shared cutover requires separate review")
    if mode in {"shadow", "candidate"}:
        validate_shadow_destination(source_config, args.local_path)
    if args.local_path:
        config = replace(
            config, qdrant=replace(config.qdrant, local_path=str(args.local_path))
        )
    if args.device:
        config = replace(
            config, embedding=replace(config.embedding, device=args.device)
        )
    if mode == "candidate":
        from contextlib import ExitStack
        from src.rag.runtime_candidate import CandidateCheckpoint, ReadOnlyShadow, RuntimeCandidate, _separate
        source_binding = hashlib.sha256(json.dumps([
            db.config.postgres_host, db.config.postgres_port, db.config.postgres_db,
            db.config.postgres_schema], separators=(",", ":")).encode()).hexdigest()
        if source_config.qdrant.local_path:
            _separate(manifest, source_config.qdrant.local_path)
        with ExitStack() as stack:
            shadows = []
            for path in getattr(args, "shadow_manifest", None) or []:
                provider = ReadOnlyShadow(path, source_binding, shadows)
                stack.callback(provider.close)
                shadows.append(provider)
            checkpoint = CandidateCheckpoint(manifest, args.local_path, generation=args.generation,
                source_binding=source_binding, domain=args.domain, shadows=shadows,
                format_version=getattr(args, "candidate_format", 1))
            with checkpoint.writer():
                return await RuntimeCandidate(config, checkpoint, shadows).run(session, args.domain)
    resolver = SearchSourceResolver(session, SearchScope(uuid.UUID(args.user_id)))
    await resolver._actor()
    if mode == "shadow":
        validate_shadow_manifest(manifest)
        validate_shadow_manifest(media_manifest)
    ledger = MigrationLedger(manifest)
    checkpoint = None
    if mode == "shadow":
        source_binding = hashlib.sha256(json.dumps([
            db.config.postgres_host, db.config.postgres_port, db.config.postgres_db,
            db.config.postgres_schema], separators=(",", ":")).encode()).hexdigest()
        checkpoint = ShadowCheckpoint(ledger, generation=args.generation,
            target_path=args.local_path.resolve(), actor=resolver.scope.user_id, source_binding=source_binding)
        checkpoint.assert_scope(resolver.scope)
        # Media readiness is also generation/actor/source/destination bound;
        # an explicitly supplied existing live ledger cannot be repurposed.
        ShadowCheckpoint(MigrationLedger(media_manifest), generation=args.generation,
            target_path=args.local_path.resolve(), actor=resolver.scope.user_id, source_binding=source_binding)
    try:
        if args.domain in {"all", "files"}:
            from src.rag.files_migration import migrate_legacy_files
            result = await migrate_legacy_files(session, resolver, config, ledger,
                collections=getattr(args, 'files_collection', None), read_config=source_config)
            print(json.dumps({"domain": "files", **result}, ensure_ascii=False), flush=True)
        if args.domain in {"all", "media"}:
            media = MediaIndexService(config, ledger_path=media_manifest)
            async for ref in resolver.inventory():
                result = await media.sync_source(resolver, ref)
                print(
                    json.dumps(
                        {"domain": "media", "source": ref.key, **result},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            progress = media.ledger.progress()
            ledger.mark(
                SourceRef("domain", "media"),
                "failed" if progress["states"].get("failed") else "ready",
                error=json.dumps(progress["states"]),
            )
        if args.domain in {"all", "knowledge"}:
            knowledge = KnowledgeShadowIndex(config, checkpoint) if checkpoint else KnowledgeIndexService(config)
            for source_id in (
                await session.execute(
                    select(KnowledgeSource.id).order_by(KnowledgeSource.id)
                )
            ).scalars():
                ref = SourceRef("knowledge_source", str(source_id))
                if source_id not in await resolver._acl_knowledge_sources([source_id], await resolver._actor()):
                    continue
                ledger.mark(ref, "running")
                try:
                    if checkpoint:
                        result = await knowledge.sync_shadow(session, resolver, source_id)
                        print(json.dumps({"domain": "knowledge", "source": ref.key, **result}), flush=True)
                        continue
                    result = await knowledge.sync_source(session, source_id)
                    if result.status != "synced":
                        raise RuntimeError(result.error or result.status)
                    await session.commit()
                    ledger.mark(
                        ref,
                        "ready",
                        completed=result.indexed_chunks,
                        total=result.indexed_chunks,
                    )
                except Exception as exc:
                    if mode == "live":
                        await session.rollback()
                    ledger.mark(ref, "failed", error=str(exc))
        if args.domain in {"all", "docs"}:
            docs = DocsShadowIndex(config, checkpoint) if checkpoint else DocsIndexService(config)
            current_libraries = set()
            async for library_id in docs_library_ids(session, resolver, shadow=checkpoint is not None):
                ref = SourceRef("docs_library", str(library_id))
                current_libraries.add(ref.key)
                ledger.mark(ref, "running")
                try:
                    if checkpoint:
                        result = await docs.sync_shadow(session, resolver, library_id)
                        print(json.dumps({"domain": "docs", "source": ref.key, **result}), flush=True)
                        continue
                    result = await docs.reconcile_library(session, library_id)
                    if result.get("status") in {"disabled", "error"}:
                        raise RuntimeError(str(result))
                    await session.commit()
                    ledger.mark(
                        ref,
                        "ready",
                        completed=int(result.get("total", 0)),
                        total=int(result.get("total", 0)),
                    )
                except Exception as exc:
                    if mode == "live":
                        await session.rollback()
                    ledger.mark(ref, "failed", error=str(exc))
            if checkpoint:
                # A foreign library can disappear from fresh enumeration
                # after revocation. Its former READY flag is not current proof.
                for previous in ledger.ready():
                    if previous.kind == "docs_library" and previous.key not in current_libraries:
                        ledger.mark(previous, "excluded", error="docs_no_current_readable_records")
        if args.domain in {"all", "memory"}:
            encoder = get_embedding(config.embedding)
            qdrant = QdrantManager(
                replace(config.qdrant, collection_name="aoitalk_conversation_memory"),
                vector_size=encoder.dimension,
            )
            if not await qdrant.initialize():
                raise RuntimeError("Memory migration unavailable")
            await purge_private_memory_points(qdrant, resolver, ledger)
            if checkpoint:
                # READY is scoped evidence, not a persisted permission grant.
                # A departed participant may no longer appear in inventory.
                # Preserve shared points for other current legal readers.
                previous = [ref for ref in ledger.ready() if ref.kind == "conversation_message"]
                for start in range(0, len(previous), 1000):
                    batch = previous[start:start + 1000]
                    allowed = set(await resolver.allowed_refs(batch, require_local_original=False))
                    for ref in batch:
                        if ref.key not in allowed:
                            ledger.mark(ref, "excluded", error="memory_no_current_readable_source")
            masked = _memory_inventory_statement(resolver.scope.user_id, include_masked=True).where(
                privacy_masking_source_sql(ConversationMessage.message_metadata))
            masked_cursor = None
            while True:
                stmt = masked.with_only_columns(ConversationMessage.id).order_by(ConversationMessage.id).limit(1000)
                if masked_cursor:
                    stmt = stmt.where(ConversationMessage.id > masked_cursor)
                identifiers = list((await session.execute(stmt)).scalars())
                if not identifiers:
                    break
                for identifier in identifiers:
                    ledger.mark(SourceRef("conversation_message", str(identifier)), "excluded", error="privacy_masking_source")
                masked_cursor = identifiers[-1]
            cursor = None
            while True:
                await resolver._actor()
                stmt = _memory_inventory_statement(resolver.scope.user_id).with_only_columns(
                    ConversationMessage.id).order_by(ConversationMessage.id).limit(100)
                if cursor:
                    stmt = stmt.where(ConversationMessage.id > cursor)
                ids = list((await session.execute(stmt)).scalars())
                if not ids:
                    break
                for message_id in ids:
                    ref = SourceRef("conversation_message", str(message_id))
                    ledger.mark(ref, "running")
                    try:
                        await migrate_memory_unit(encoder, qdrant, resolver, ref, ledger)
                    except SourceUnavailable as exc:
                        ledger.mark(ref, "excluded", error=exc.reason)
                        ledger.attempt(ref, resolver.scope.user_id, "excluded", exc.reason)
                    except Exception as exc:
                        ledger.mark(ref, "failed", error=str(exc))
                cursor = ids[-1]
        return {
            "mode": mode,
            "generation": getattr(args, "generation", None),
            "source_database": "read_only_rollback" if checkpoint else "live_metadata_updates",
            "identity": EMBEDDING_IDENTITY,
            "progress": ledger.progress(),
            "production_cutover": "not_performed",
            "old_collections": "retained_pending_individual_review",
        }
    except BaseException:
        # Source DB rollback is owned by source_session; SQLite checkpoints
        # and already persisted destination batches remain resumable.
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user-id", help="Authenticated source owner UUID; no admin broadening"
    )
    parser.add_argument(
        "--domain",
        choices=["all", "files", "media", "knowledge", "docs", "memory"],
        default="all",
    )
    parser.add_argument('--files-collection', action='append',
                        help='Exact legacy generic collection; only existing source metadata and current canonical ACL are used')
    parser.add_argument(
        "--manifest",
        type=Path,
    )
    parser.add_argument(
        "--media-manifest",
        type=Path,
    )
    parser.add_argument("--local-path", type=Path)
    parser.add_argument("--mode", choices=["shadow", "candidate", "live"], default="shadow",
                        help="shadow/candidate never commit source DB metadata; live requires all writers stopped")
    parser.add_argument("--generation", help="Isolated generation token; reruns require the same bound inputs/destination")
    parser.add_argument("--candidate-format", type=int, choices=[1, 2], default=1,
                        help="1 preserves the four-collection contract; 2 also verifies ContextMemory")
    parser.add_argument("--shadow-manifest", type=Path, action="append",
                        help="Candidate dense provider: stopped, bound shadow migration/media manifest (repeatable)")
    parser.add_argument("--confirm-runtime-stopped", action="store_true",
                        help="Explicit operator assertion: every application/index writer is stopped (does not stop services)")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if not args.status and args.mode != "candidate" and not args.user_id:
        parser.error("--user-id is required for current ACL evaluation")
    try:
        migration_paths(args)
    except ValueError as exc:
        parser.error(str(exc))
    report = asyncio.run(run(args))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if report.get("candidate_ready") is False or report.get("progress", report).get("states", {}).get("failed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
