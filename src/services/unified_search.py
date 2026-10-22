"""One server-only query encoding and four live-authorized search lanes.

API/tools supply QueryPlan and authenticated context, never prepared vectors.
Prepared lanes expose one canonical EmbeddingGemma 2 dense cosine; that value
drives cross-lane ranking and is returned as both ``score`` and ``similarity``.
Missing or invalid cosine candidates fail closed, and result spans collapse
before evidence-source union.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import uuid

from sqlalchemy import select, and_, or_, cast, String, literal, func

from ..rag.media_query import MediaQueryPreparer, QueryPlan, QueryPreparationError, QuerySourceResolvers
from .search_sources import SearchSourceResolver, SearchScope, SourceUnavailable, _acl_batches

# Weak-match judgement on the real EmbeddingGemma 2 cosine.  Measured top-1:
# conversation matches 0.79-0.85, non-matches 0.63-0.70; media shares the
# embedding space and threshold.
SEMANTIC_WEAK_TOP_SIMILARITY = 0.74
CONVERSATION_WEAK_TOP_SIMILARITY = SEMANTIC_WEAK_TOP_SIMILARITY
MEDIA_WEAK_TOP_SIMILARITY = SEMANTIC_WEAK_TOP_SIMILARITY
FILES_WEAK_TOP_SIMILARITY = SEMANTIC_WEAK_TOP_SIMILARITY
FILES_KINDS = frozenset({"workspace_file"})
KNOWLEDGE_WEAK_TOP_SIMILARITY = SEMANTIC_WEAK_TOP_SIMILARITY
# Docs top-1 cosines of matches (0.76-0.82) and non-matches (0.64-0.73) are
# close, and keyword overlap does not separate them (common Japanese n-grams
# give non-matches 0.3-0.46).  Docs therefore also needs a clear lead of the
# top-1 over the median candidate (matches 0.14-0.27, non-matches 0.09-0.15).
DOCS_WEAK_TOP_SIMILARITY = 0.74
DOCS_WEAK_TOP_MARGIN = 0.12
# The median lead needs a candidate distribution; tiny scopes use the floor only.
DOCS_WEAK_MIN_DISTRIBUTION = 8


def _finite(value):
    if isinstance(value, (bool, str, bytes)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def result_similarity(target, hit):
    """Canonical pure dense cosine of a prepared lane hit, else ``None``."""
    field = {
        "docs": "_query_semantic_score",
        "knowledge": "_query_semantic_score",
        "conversation": "_query_semantic_score",
        "media": "_query_semantic_score",
        "files": "_query_semantic_score",
    }.get(target)
    return _finite(hit.get(field)) if field else None


def lane_weak_match(target, hits):
    """True when a lane's best hits are too weak to count as a real match.

    ``hits`` are the lane's deduplicated candidates (internal fields intact).
    An empty lane is weak.  Lanes without a comparable cosine (knowledge) are
    judged only by emptiness.
    """
    if not hits:
        return True
    similarities = sorted((value for value in (result_similarity(target, hit) for hit in hits)
                           if value is not None), reverse=True)
    if target in {"conversation", "media", "knowledge", "files"}:
        threshold = {
            "conversation": CONVERSATION_WEAK_TOP_SIMILARITY,
            "media": MEDIA_WEAK_TOP_SIMILARITY,
            "files": FILES_WEAK_TOP_SIMILARITY,
            "knowledge": KNOWLEDGE_WEAK_TOP_SIMILARITY,
        }[target]
        return not similarities or similarities[0] < threshold
    if target != "docs":
        return False
    if not similarities or similarities[0] < DOCS_WEAK_TOP_SIMILARITY:
        return True
    if len(similarities) < DOCS_WEAK_MIN_DISTRIBUTION:
        return False
    median = similarities[len(similarities) // 2]
    return similarities[0] - median < DOCS_WEAK_TOP_MARGIN


def overall_weak_match(lanes):
    """The whole search is weak only when every lane is weak."""
    flags = [bool(state.get("weak_match", True)) for state in lanes]
    return all(flags)


@dataclass(frozen=True)
class UnifiedSearchContext:
    user_id: uuid.UUID
    project_id: uuid.UUID | None = None
    space_id: uuid.UUID | None = None
    docs_library_id: uuid.UUID | None = None
    current_session_id: uuid.UUID | None = None
    character_name: str | None = None
    current_source_scope: SearchScope | None = None
    source_workspace_root: str | None = None
    strict_project_scope: bool = False

    def __post_init__(self):
        for name in ("user_id", "project_id", "space_id", "docs_library_id", "current_session_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, uuid.UUID(str(value)))
        if self.current_source_scope is not None and (
                not isinstance(self.current_source_scope, SearchScope)
                or self.current_source_scope.user_id != self.user_id):
            raise ValueError("query_source_actor_mismatch")


class _PartitionResolver(SearchSourceResolver):
    """A null Project is an explicit partition, never the wildcard scope."""
    def __init__(self, *args, non_project=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.non_project = non_project

    async def _project(self, project_id):
        if self.non_project and project_id is not None:
            raise SourceUnavailable()
        return await super()._project(project_id)

    async def _allowed_refs(self, unique, actor, *, require_local_original):
        allowed = set(await super()._allowed_refs(unique, actor, require_local_original=require_local_original))
        if not self.non_project or not allowed:
            return list(allowed)
        from ..memory.models import (ConversationMessage, ConversationSession, KnowledgeAttachment,
            KnowledgeNode, KnowledgeDocument, GeneratedMedia, GenerationOutput, MeetingProcessingJob,
            WorkspaceFileEntry)
        from sqlalchemy import case
        from ..security.field_crypto import decrypt_json_value_if_needed
        kept = set()
        refs = [unique[key] for key in allowed]
        specs = {
            "chat_attachment": (ConversationMessage, ConversationMessage.id, ConversationSession.project_id,
                [(ConversationSession, ConversationSession.id == ConversationMessage.session_id)]),
            "conversation_message": (ConversationMessage, ConversationMessage.id, ConversationSession.project_id,
                [(ConversationSession, ConversationSession.id == ConversationMessage.session_id)]),
            "docs_attachment": (KnowledgeAttachment, KnowledgeAttachment.id, KnowledgeNode.project_id,
                [(KnowledgeNode, KnowledgeNode.id == KnowledgeAttachment.node_id)]),
            "knowledge_document": (KnowledgeDocument, KnowledgeDocument.id, KnowledgeDocument.project_refs, []),
            "generation_output": (GenerationOutput, GenerationOutput.id, GenerationOutput.project_id, []),
            "generated_media": (GeneratedMedia, GeneratedMedia.id, ConversationSession.project_id,
                [(ConversationSession, and_(GeneratedMedia.context_type == "conversation",
                                            cast(ConversationSession.id, String) == GeneratedMedia.context_id))]),
            "meeting_audio": (MeetingProcessingJob, MeetingProcessingJob.id, MeetingProcessingJob._request_json, []),
            # User Files belong to the personal partition, Project Files to theirs.
            "workspace_file": (WorkspaceFileEntry, WorkspaceFileEntry.id, case(
                (WorkspaceFileEntry.scope == "project", WorkspaceFileEntry.owner_id), else_=None), []),
        }
        for kind, (model, id_column, project_column, joins) in specs.items():
            by_id = {}
            for ref in refs:
                if ref.kind == kind:
                    by_id.setdefault(uuid.UUID(ref.id.split(":")[0]), []).append(ref.key)
            for batch in _acl_batches(by_id):
                stmt = select(id_column, project_column).select_from(model)
                for related, condition in joins:
                    stmt = stmt.outerjoin(related, condition)
                for identifier, project in await self.session.execute(stmt.where(id_column.in_(batch))):
                    if kind == "meeting_audio":
                        request = decrypt_json_value_if_needed(project, aad="meeting_processing_jobs.request_json") or {}
                        project = request.get("project_id")
                    if not project:
                        kept.update(by_id[identifier])
        return list(allowed & kept)

    async def resolve(self, ref, *, open_file=True):
        source = await super().resolve(ref, open_file=open_file)
        if self.non_project and (source.metadata.get("project_id") or source.metadata.get("project_refs")):
            raise SourceUnavailable()
        return source


def query_source_resolvers(plan, session, context):
    """Bind both source authorities once; corpus Space filters stay separate."""
    current = context.current_source_scope or SearchScope(context.user_id, context.project_id)
    if context.strict_project_scope and (
            plan.scope not in {"current_context", "project"}
            or context.project_id != current.project_id
            or current.project_id is None and context.space_id is not None):
        raise SourceUnavailable(reason="strict_project_scope_required")
    if plan.scope == "project" and context.project_id is None:
        raise ValueError("project_scope_requires_project")
    if plan.scope == "space" and context.space_id is None:
        raise ValueError("space_scope_requires_space")
    if plan.scope == "all_readable":
        selected = SearchScope(context.user_id)
    elif plan.scope == "space":
        selected = SearchScope(context.user_id, space_id=context.space_id)
    else:
        selected = SearchScope(context.user_id, context.project_id, context.space_id)
    return QuerySourceResolvers(
        current_context=_PartitionResolver(session, current, workspace_root=context.source_workspace_root,
            non_project=current.project_id is None and current.space_id is None),
        selected_scope=_PartitionResolver(session, selected, workspace_root=context.source_workspace_root,
            non_project=plan.scope != "all_readable" and selected.project_id is None and selected.space_id is None))


class UnifiedSearchService:
    def __init__(self, *, embedding=None, preparation=None):
        if embedding is None:
            from ..rag.config import get_rag_config
            from ..rag.embedding import get_embedding
            embedding = get_embedding(get_rag_config().embedding)
        self.preparer = MediaQueryPreparer(embedding, preparation)

    @staticmethod
    def _resolver(session, context, *, project=None, space=None, non_project=False):
        return _PartitionResolver(session, SearchScope(uuid.UUID(str(context.user_id)), project, space),
                                  non_project=non_project)

    async def _partitions(self, plan, session, context):
        """Enumerate legal current partitions; bounded and fail explicitly."""
        from ..memory.models import DocsLibrary, Project, ConversationSession
        from ..memory.cross_session_memory import _memory_inventory_statement
        actor = uuid.UUID(str(context.user_id))
        project = uuid.UUID(str(context.project_id)) if context.project_id else None
        space = uuid.UUID(str(context.space_id)) if context.space_id else None
        if plan.scope == "project" and project is None:
            raise ValueError("project_scope_requires_project")
        if plan.scope == "space" and space is None:
            raise ValueError("space_scope_requires_space")
        space_scoped = plan.scope == "space" or (plan.scope == "current_context" and project is None and space is not None)
        broad = space_scoped or plan.scope == "all_readable"
        target_space = space if plan.scope != "all_readable" else None
        resolver = self._resolver(session, context, space=target_space)
        actor_row = await resolver._actor()
        if broad:
            ids = list((await session.execute(select(Project.id).where(Project.deleted_at.is_(None)))).scalars())
            permissions = await resolver._acl_projects(ids, actor_row)
            projects = [pid for pid, policy in permissions.items() if policy.scoped_read]
            if plan.scope == "all_readable":
                projects.insert(0, None)
        else:
            await self._resolver(session, context, project=project, space=space)._project(project)
            projects = [project]
        if len(projects) > plan.budget.max_partitions:
            raise ValueError("partition_budget_exceeded")
        lanes = {target: [] for target in plan.targets}
        if "docs" in lanes:
            from ..memory.models import KnowledgeNode, KnowledgeNodeShare
            from .docs_graph_service import DocsGraphService
            from .docs_acl import apply_docs_search_visibility
            # Candidate library IDs reveal no foreign private partitions.
            project_library = select(literal(1)).select_from(KnowledgeNode).where(
                KnowledgeNode.docs_library_id == DocsLibrary.id,
                KnowledgeNode.project_id.in_([pid for pid in projects if pid is not None])).exists()
            shared_library = select(literal(1)).select_from(KnowledgeNodeShare).join(KnowledgeNode,
                KnowledgeNode.id == KnowledgeNodeShare.node_id).where(KnowledgeNode.docs_library_id == DocsLibrary.id,
                KnowledgeNodeShare.user_id == actor, func.lower(KnowledgeNodeShare.permission).in_(["read", "write"])).exists()
            library_stmt = select(DocsLibrary.id, DocsLibrary.owner_user_id).where(
                or_(DocsLibrary.owner_user_id == actor, project_library, shared_library))
            if context.docs_library_id and not broad:
                library_stmt = library_stmt.where(DocsLibrary.id == context.docs_library_id)
            libraries = (await session.execute(library_stmt)).all()
            if len(libraries) > plan.budget.max_partitions:
                raise ValueError("partition_budget_exceeded")
            for library, owner in libraries:
                for partition in projects:
                    statement, _ = await DocsGraphService(session)._build_structured_query_statement(
                        docs_library_id=library, user_id=actor, project_id=partition, turn_project_id=partition)
                    statement = apply_docs_search_visibility(statement, user_id=actor)
                    if partition is None:
                        statement = statement.where(KnowledgeNode.project_id.is_(None))
                    visible = (await session.execute(statement.with_only_columns(literal(1),
                        maintain_column_froms=True).limit(1))).first() is not None
                    if visible or (owner == actor and partition is None):
                        lanes["docs"].append({"library": library, "project": partition, "space": target_space})
        if "knowledge" in lanes:
            # A source grant can authorize a document whose Project refs are
            # no longer readable. Knowledge's canonical source ACL is the
            # authority for all_readable, including those residual documents.
            lanes["knowledge"] = ([{"wildcard": True}] if plan.scope == "all_readable" else
                [{"project": pid, "space": target_space} for pid in projects])
        if "conversation" in lanes:
            stmt = _memory_inventory_statement(actor).with_only_columns(ConversationSession.project_id,
                ConversationSession.character_name, maintain_column_froms=True).distinct()
            from ..memory.cross_session_memory import _character_slug_candidates
            if not broad:
                stmt = stmt.where(ConversationSession.project_id == project)
            character = context.character_name
            if plan.scope == "current_context" and not character and context.current_session_id:
                # Resolve through the canonical owner/joined relation. A
                # caller-supplied session ID alone never supplies authority.
                from ..memory.cross_session_memory import _canonical_character_slug
                try:
                    current = await self._resolver(session, context, project=project, space=space)._conversation(
                        context.current_session_id)
                    if current.project_id == project and current.character_name:
                        character = _canonical_character_slug(current.character_name)
                except SourceUnavailable:
                    pass
            if plan.scope == "current_context" and character:
                stmt = stmt.where(ConversationSession.character_name.in_(_character_slug_candidates(character)))
            rows = (await session.execute(stmt)).all()
            allowed = set(projects)
            lanes["conversation"] = [{"project": pid, "character": character,
                "space": target_space} for pid, character in rows if pid in allowed]
            if plan.scope == "current_context" and not character:
                lanes["conversation"] = [{"unsupported_reason": "current_character_unresolved"}]
        for target in ("media", "files"):
            if target in lanes:
                # Wildcard media uses its existing full current ACL filter. Other
                # scopes enumerate Project partitions, including explicit NULL
                # (the personal partition holding the actor's own User Files).
                lanes[target] = ([{"wildcard": True}] if plan.scope == "all_readable" else
                    [{"project": pid, "space": target_space} for pid in projects])
        if sum(map(len, lanes.values())) > plan.budget.max_partitions:
            raise ValueError("partition_budget_exceeded")
        return lanes

    async def _search_lane(self, target, partition, unit, plan, session, context):
        if partition.get("unsupported_reason"):
            raise RuntimeError(partition["unsupported_reason"])
        project, space = partition.get("project"), partition.get("space")
        if target == "docs":
            from ..rag.docs_index import get_docs_index_service
            resolver = self._resolver(session, context, project=project, space=space, non_project=project is None)
            await resolver._actor()
            await resolver._project(project)
            result = await get_docs_index_service().search_prepared(unit, session=session,
                docs_library_id=partition["library"], user_id=context.user_id, project_id=project,
                turn_project_id=project, _non_project=project is None, limit=plan.limit)
            reason = result.telemetry.fallback_reason
            if reason not in {None, "no_eligible_nodes", "empty_scope"}:
                raise RuntimeError(f"docs:{reason}")
            await resolver._actor()
            await resolver._project(project)
            return [{**hit, "target_kind": target, "locator": {"kind": "docs_node", "id": hit["node_id"],
                "docs_library_id": hit["docs_library_id"]}} for hit in result.hits]
        if target == "knowledge":
            from ..knowledge.service import KnowledgeService, KnowledgeSearchFilters
            resolver = self._resolver(session, context, project=project, space=space,
                                      non_project=project is None and not partition.get("wildcard"))
            await resolver._actor()
            await resolver._project(project)
            hits = await KnowledgeService.search_prepared(session, unit, actor_user_id=context.user_id,
                filters=KnowledgeSearchFilters(project_id=project,
                    non_project=project is None and not partition.get("wildcard")), limit=plan.limit * 3)
            await resolver._actor()
            await resolver._project(project)
            return [{**hit, "target_kind": target, "locator": {"kind": "knowledge_document", "id": hit["document"]["id"]},
                "citation": {"chunk_id": hit["chunk"]["id"], "chunk_index": hit["chunk"]["chunk_index"],
                             "heading_path": hit["chunk"]["heading_path"]}} for hit in hits]
        if target == "conversation":
            from ..memory.cross_session_memory import get_cross_session_memory
            hits = await get_cross_session_memory().search_prepared(unit, session=session, user_id=context.user_id,
                project_id=project, space_id=space, character_name=partition["character"],
                current_session_id=context.current_session_id, limit=plan.limit)
            return [{**hit, "target_kind": target, "_query_semantic_score": hit.get("relevance_score"),
                "locator": {"kind": "conversation_message", "id": hit["id"],
                            "session_id": hit["session_id"]}} for hit in hits]
        from ..rag.media_index import get_media_index_service
        resolver = self._resolver(session, context, project=project, space=space,
                                  non_project=project is None and not partition.get("wildcard"))
        await resolver._actor()
        await resolver._project(project)
        # The Files lane owns workspace_file units; the media lane keeps the
        # attachment/generated kinds so one unit never appears in both lanes.
        hits = await get_media_index_service().search_prepared(
            resolver, unit, limit=plan.limit * 3,
            **({"kinds": FILES_KINDS} if target == "files" else {"exclude_kinds": FILES_KINDS}))
        await resolver._actor()
        await resolver._project(project)
        return [{**hit, "target_kind": target, "locator": {"kind": hit["source_kind"], "id": hit["source_id"]},
            "citation": {key: hit[key] for key in ("unit_id", "page_number", "start_seconds", "end_seconds",
                         "text_start", "text_end", "text_length") if hit.get(key) is not None}} for hit in hits]

    async def _authorize_lane(self, target, partition, hits, session, context):
        """Last publish gate; no vector inference or vector store lookup."""
        project, space = partition.get("project"), partition.get("space")
        resolver = self._resolver(session, context, project=project, space=space,
            non_project=project is None and not partition.get("wildcard"))
        await resolver._actor()
        await resolver._project(project)
        if not hits:
            return []
        if target == "docs":
            from ..rag.docs_index import get_docs_index_service
            current = await get_docs_index_service().authorize_prepared_hits(hits, session=session,
                docs_library_id=partition["library"], user_id=context.user_id,
                project_id=project, non_project=project is None)
        elif target == "knowledge":
            from ..knowledge.service import KnowledgeService, KnowledgeSearchFilters
            current = await KnowledgeService.authorize_prepared_hits(session, hits,
                actor_user_id=context.user_id, filters=KnowledgeSearchFilters(project_id=project,
                non_project=project is None and not partition.get("wildcard")))
        elif target == "conversation":
            from ..memory.cross_session_memory import get_cross_session_memory
            current = await get_cross_session_memory().authorize_prepared_hits(hits, session=session,
                user_id=context.user_id, character_name=partition["character"],
                project_id=project, current_session_id=context.current_session_id)
        else:
            from ..rag.media_index import get_media_index_service
            current = await get_media_index_service().authorize_prepared_hits(resolver, hits)
        await resolver._actor()
        await resolver._project(project)
        return current

    @staticmethod
    def _lane_failure(exc):
        # Driver errors can contain connection strings, SQL and private text.
        import httpx
        from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
        from sqlalchemy.exc import SQLAlchemyError
        if isinstance(exc, SourceUnavailable):
            return "source_unavailable"
        if isinstance(exc, TimeoutError):
            return "lane_timeout"
        if isinstance(exc, (ConnectionError, OSError, httpx.TransportError)):
            return "lane_transport_failed"
        if isinstance(exc, ResponseHandlingException):
            return UnifiedSearchService._lane_failure(exc.source_exception)
        if isinstance(exc, UnexpectedResponse):
            return "lane_remote_failed"
        if isinstance(exc, SQLAlchemyError):
            return "lane_storage_failed"
        if isinstance(exc, RuntimeError):
            value = str(exc)
            allowed = {"unsupported_partition", "current_character_unresolved", "conversation_index_unavailable",
                       "knowledge_index_unavailable", "media_index_unavailable", "files_index_unavailable"}
            docs_reasons = {"index_unavailable", "search_failed", "acl_filter_failed", "no_vectors"}
            if value in allowed or value.startswith("docs:") and value[5:] in docs_reasons:
                return value
        return "lane_search_failed"

    @staticmethod
    def _fusion_relevance(target, hit):
        """Return the canonical prepared dense cosine, if valid."""
        return result_similarity(target, hit)

    @staticmethod
    def _lane_candidates(runs):
        """Deduplicated valid-cosine hits per lane, internals intact."""
        import json
        lanes = {}
        for target, _unit, hits in runs:
            lane = lanes.setdefault(target, {})
            for hit in hits:
                key = json.dumps(hit["locator"], sort_keys=True, default=str)
                value = UnifiedSearchService._fusion_relevance(target, hit)
                if value is None:
                    continue
                current = lane.get(key)
                current_value = (UnifiedSearchService._fusion_relevance(target, current)
                                 if current is not None else None)
                if current is None or (
                    value is not None
                    and (current_value is None or value > current_value)
                ):
                    lane[key] = hit
        return {target: list(values.values()) for target, values in lanes.items()}

    @staticmethod
    def _fuse(runs, limit):
        """Merge prepared candidates by one canonical dense cosine.

        Query units and evidence sources contribute provenance only. They do
        not add votes or inflate a result's score. A locator is represented by
        the valid hit with the highest cosine, with deterministic tie-breaking.
        """
        import json
        records, evidence = {}, {}

        def tie_key(hit):
            # Partition/unit metadata is deliberately excluded: equal-cosine
            # duplicates must not depend on enumeration order.
            return json.dumps([hit.get("citation"), hit.get("_query_content_hash"),
                               hit.get("content_hash"), hit.get("text"), hit.get("content"),
                               hit.get("title")],
                              sort_keys=True, default=str)

        def prefer(candidate, current):
            candidate_score = UnifiedSearchService._fusion_relevance(candidate[0], candidate[1])
            current_score = UnifiedSearchService._fusion_relevance(current[0], current[1])
            if candidate_score is not None and current_score is None:
                return True
            if candidate_score is None and current_score is not None:
                return False
            if candidate_score != current_score:
                return candidate_score is not None and candidate_score > current_score
            return tie_key(candidate[1]) < tie_key(current[1])

        for target, unit, hits in runs:
            for hit in hits:
                # Missing/non-finite dense cosine is fail-closed and never
                # becomes an unranked result through a legacy score field.
                if UnifiedSearchService._fusion_relevance(target, hit) is None:
                    continue
                locator_key = json.dumps([target, hit["locator"]], sort_keys=True, default=str)
                current = records.get(locator_key)
                if current is None or prefer((target, hit), (target, current)):
                    records[locator_key] = hit
                evidence.setdefault(locator_key, {})
                for match in unit.matched_query_sources:
                    evidence[locator_key][json.dumps(match, sort_keys=True, default=str)] = match
        out = []
        for key, hit in records.items():
            score = UnifiedSearchService._fusion_relevance(hit.get("target_kind"), hit)
            if score is None:
                continue
            dedupe_hash = UnifiedSearchService._duplicate_key(hit)
            citation = dict(hit.get("citation") or {})
            content_hash = citation.get("content_hash") or hit.get("content_hash")
            body_sha256 = citation.get("body_sha256") or hit.get("body_sha256")
            # Offset validation binds to the exact full UTF-8 body. File or
            # scope-bound hashes and Docs snippets are not body digests.
            if hit.get("target_kind") == "conversation" and isinstance(hit.get("content"), str):
                body_sha256 = hashlib.sha256(hit["content"].encode("utf-8")).hexdigest()
            if hit.get("target_kind") in {"docs", "conversation"}:
                citation.pop("content_hash", None)
            elif content_hash:
                citation["content_hash"] = content_hash
            if body_sha256:
                citation["body_sha256"] = body_sha256
            out.append({**{k: v for k, v in hit.items() if not k.startswith("_query_")},
                        "citation": citation, "score": score,
                        "similarity": score,
                        "matched_query_sources": sorted(
                            evidence[key].values(),
                            key=lambda match: json.dumps(match, sort_keys=True, default=str),
                        ),
                        **({"_duplicate_key": dedupe_hash} if dedupe_hash else {})})
        ordered = sorted(out, key=lambda hit: (-hit["score"], json.dumps(hit["locator"], sort_keys=True)))
        return UnifiedSearchService._collapse_duplicates(ordered)[:limit]

    @staticmethod
    def _duplicate_key(hit):
        """Original-bytes sha256 shared by Files, media originals and Knowledge."""
        target = hit.get("target_kind")
        if target in {"files", "media"}:
            value = hit.get("_query_content_hash")
        elif target == "knowledge":
            value = (hit.get("document") or {}).get("content_hash") or hit.get("content_hash")
        else:
            return None
        if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value):
            return value
        return None

    @staticmethod
    def _collapse_duplicates(ordered):
        """One result per identical original; the Files location is primary.

        Other kinds holding the same bytes (chat/task/Docs attachments,
        Knowledge documents) remain reachable as ``other_locations``.
        """
        groups = {}
        for hit in ordered:
            key = hit.get("_duplicate_key")
            if key:
                groups.setdefault(key, []).append(hit)
        primary_ids = {}
        for key, members in groups.items():
            files = [hit for hit in members if hit.get("target_kind") == "files"]
            primary_ids[key] = id((files or members)[0])
        result = []
        for hit in ordered:
            key = hit.pop("_duplicate_key", None)
            if key is None:
                result.append(hit)
                continue
            if id(hit) != primary_ids[key]:
                continue
            others = [member for member in groups[key] if member is not hit]
            if others:
                hit = {**hit, "other_locations": [{
                    "target_kind": other.get("target_kind"),
                    "locator": other.get("locator"),
                    "source_kind": other.get("source_kind"),
                    "source_id": other.get("source_id"),
                    "title": other.get("title") or (other.get("document") or {}).get("title"),
                    "unit_id": other.get("unit_id"),
                    "score": other.get("score"),
                } for other in others]}
            result.append(hit)
        for hit in ordered:
            hit.pop("_duplicate_key", None)
        return result

    async def search(self, plan: QueryPlan, *, session, context: UnifiedSearchContext):
        if not isinstance(plan, QueryPlan) or not isinstance(context, UnifiedSearchContext):
            raise TypeError("query_plan_and_authenticated_context_required")
        source_status, lane_status = [], {target: {"status": "not_processed", "partitions": []} for target in plan.targets}
        response = {"version": 1, "results": [], "source_status": source_status,
                    "lane_status": lane_status, "complete": False, "warnings": [], "weak_match": True}
        try:
            resolver = query_source_resolvers(plan, session, context)
            partitions = await self._partitions(plan, session, context)
            prepared = await self.preparer.prepare(plan, resolver)
            response["source_status"] = prepared.source_status
            calls = len(prepared.units) * sum(map(len, partitions.values()))
            if calls > plan.budget.max_search_calls:
                raise ValueError("search_call_budget_exceeded")
            runs = []
            for target in plan.targets:
                state = lane_status[target]
                for partition in partitions[target]:
                    entry = {"partition": {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in partition.items()},
                             "status": "pending"}
                    state["partitions"].append(entry)
                    try:
                        for unit in prepared.units:
                            hits = await self._search_lane(target, partition, unit, plan, session, context)
                            runs.append((target, unit, [{**hit, "_query_partition": partition} for hit in hits]))
                        entry["status"] = "complete"
                    except Exception as exc:
                        # DB/driver exceptions can include SQL parameters.
                        reason = self._lane_failure(exc)
                        entry.update(status="failed", reason=reason)
                state["status"] = "complete" if all(p["status"] == "complete" for p in state["partitions"]) else "failed"
            # All runs finish before any result is published. An earlier
            # unit's hits may have lost ACL/privacy/hash authority meanwhile.
            await self.preparer.reauthorize(prepared, resolver)
            import json
            groups = {(target, json.dumps(partition, sort_keys=True, default=str)): (partition, {})
                for target, values in partitions.items() if lane_status[target]["status"] == "complete"
                for partition in values}
            for target, _unit, hits in runs:
                if lane_status[target]["status"] != "complete":
                    continue
                for hit in hits:
                    partition = hit["_query_partition"]
                    key = (target, json.dumps(partition, sort_keys=True, default=str))
                    identity = json.dumps([hit["locator"], hit.get("citation"), hit.get("_query_content_hash")],
                                          sort_keys=True, default=str)
                    groups.setdefault(key, (partition, {}))[1][identity] = hit
            legal = set()
            for (target, _key), (partition, candidates) in groups.items():
                try:
                    current = await self._authorize_lane(target, partition, list(candidates.values()), session, context)
                    for hit in current:
                        legal.add(json.dumps([target, partition, hit["locator"], hit.get("citation"),
                            hit.get("_query_content_hash")], sort_keys=True, default=str))
                except Exception as exc:
                    lane_status[target]["status"] = "failed"
                    for entry in lane_status[target]["partitions"]:
                        if entry["partition"] == {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in partition.items()}:
                            entry.update(status="failed", reason=self._lane_failure(exc))
            runs = [(target, unit, [hit for hit in hits if json.dumps([target,
                hit["_query_partition"], hit["locator"], hit.get("citation"), hit.get("_query_content_hash")],
                sort_keys=True, default=str) in legal]) for target, unit, hits in runs]
            # Lane authorization itself awaits live DB reads. Query originals
            # must still have the same authority/body after those awaits.
            await self.preparer.reauthorize(prepared, resolver)
            response["complete"] = all(state["status"] == "complete" for state in lane_status.values())
            for target, state in lane_status.items():
                if state["status"] != "complete":
                    response["warnings"].append({"target_kind": target, "reason": "lane_incomplete"})
            # A failed partition cannot publish an apparently complete prefix.
            complete_runs = [run for run in runs if lane_status[run[0]]["status"] == "complete"]
            response["results"] = self._fuse(complete_runs, plan.limit)
            candidates = self._lane_candidates(complete_runs)
            for target, state in lane_status.items():
                state["weak_match"] = lane_weak_match(target, candidates.get(target, []))
            response["weak_match"] = overall_weak_match(lane_status.values())
        except QueryPreparationError as exc:
            response["source_status"] = exc.source_status
            response["warnings"].append({"reason": exc.reason})
        except (ValueError, SourceUnavailable) as exc:
            reason = getattr(exc, "reason", str(exc))
            response["warnings"].append({"reason": reason})
            if isinstance(exc, SourceUnavailable):
                for source in response["source_status"]:
                    source.update(status="failed", reason=reason)
        except Exception:
            response["warnings"].append({"reason": "query_scope_unavailable"})
        return response


_unified_search = None


def get_unified_search_service():
    global _unified_search
    if _unified_search is None:
        _unified_search = UnifiedSearchService()
    return _unified_search
