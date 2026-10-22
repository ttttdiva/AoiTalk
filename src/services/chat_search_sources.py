"""Translate saved turn attachments into authorized native query references.

Client IDs, prompt paths and recognition output are never source authority.
Index readiness is deliberately irrelevant to resolving a query original.
"""
from __future__ import annotations

import hashlib
import re
from uuid import UUID

from ..memory.models import ConversationMessage
from .search_sources import SourceRef, SourceUnavailable
from .chat_attachment_limits import MAX_CHAT_ATTACHMENTS, MAX_CHAT_ATTACHMENT_BYTES

MAX_QUERY_ATTACHMENTS = MAX_CHAT_ATTACHMENTS
MAX_QUERY_ATTACHMENT_BYTES = MAX_CHAT_ATTACHMENT_BYTES


class QuerySourceBudgetExceeded(ValueError):
    """A public selection limit, distinct from a missing/unauthorized source."""


class EvidenceChanged(SourceUnavailable):
    """A citation no longer addresses the same current canonical body."""


def verify_evidence_body(text, citation):
    expected = citation.get('body_sha256')
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise EvidenceChanged(reason='evidence_version_required')
    if hashlib.sha256(text.encode('utf-8')).hexdigest() != expected:
        raise EvidenceChanged(reason='evidence_changed')


async def saved_attachment_sources(resolver, *, session_id, message_id, indices=None):
    """Resolve every selected DB attachment in order, or reject the request."""
    try:
        session_uuid, message_uuid = UUID(str(session_id)), UUID(str(message_id))
    except (ValueError, TypeError, AttributeError):
        raise SourceUnavailable() from None
    await resolver._conversation(session_uuid)
    row = await resolver.session.get(ConversationMessage, message_uuid, populate_existing=True)
    if (row is None or row.session_id != session_uuid or row.deleted_at is not None
            or row.is_active_branch is False or row.role != 'user'):
        raise SourceUnavailable()
    attachments = (row.message_metadata or {}).get('attachments')
    if not isinstance(attachments, list) or not attachments:
        raise SourceUnavailable()
    selected = list(range(len(attachments))) if indices is None else indices
    if (not isinstance(selected, list) or not selected
            or any(type(i) is not int or i < 0 or i >= len(attachments) for i in selected)
            or len(set(selected)) != len(selected)):
        raise SourceUnavailable()
    if len(selected) > MAX_QUERY_ATTACHMENTS:
        raise QuerySourceBudgetExceeded('添付検索は12件までです。対象を選び直してください。')
    total_bytes = 0
    result = []
    for index in selected:
        item = attachments[index]
        if not isinstance(item, dict) or item.get('upload_failed') or not item.get('path'):
            raise SourceUnavailable()
        ref = SourceRef('chat_attachment', f'{message_uuid}:{index}')
        # Canonical ACL/storage/hash resolver, shared with explicit references.
        source = await resolver.resolve(ref)
        size = source.path.stat().st_size if source.path is not None else 0
        total_bytes += size
        if total_bytes > MAX_QUERY_ATTACHMENT_BYTES:
            raise QuerySourceBudgetExceeded('添付検索は合計100 MBまでです。対象を選び直してください。')
        mime = str(item.get('mime_type') or '')
        kind = mime.split('/')[0] if mime.startswith(('image/', 'audio/', 'video/')) else 'text'
        filename = str(item.get('name') or '添付').replace('\\', '/').rsplit('/', 1)[-1] or '添付'
        result.append({'ref': ref, 'index': index, 'kind': kind,
                       'filename': re.sub(r'[\x00-\x1f\x7f]', ' ', filename)[:240],
                       'size_bytes': size})
    return result


async def authorize_query_sources(resolver, sources):
    """Check native, server-classified sources together under one budget."""
    from ..rag.media_query import resolve_query_source
    if len(sources) > MAX_QUERY_ATTACHMENTS:
        raise QuerySourceBudgetExceeded('添付検索は12件までです。対象を選び直してください。')
    total = 0
    for source in sources:
        if source.ref is not None:
            current = await resolve_query_source(resolver, source)
            total += current.path.stat().st_size if current.path is not None else 0
            if total > MAX_QUERY_ATTACHMENT_BYTES:
                raise QuerySourceBudgetExceeded('添付検索は合計100 MBまでです。対象を選び直してください。')


def unified_search_service():
    """Load the independently owned engine only when a unified call is made."""
    try:
        from .unified_search import get_unified_search_service
    except ImportError:
        raise RuntimeError('Unified search is unavailable') from None
    return get_unified_search_service()


async def execute_query_plan(resolver, plan, *, service=None, current_session_id=None,
                             character_name=None, docs_library_id=None,
                             current_source_refs=(), selected_space_id=None, strict_project_scope=False):
    """Pass the wire plan to the shared engine without implementing a lane."""
    from ..rag.media_query import QueryPlan, QuerySource, QuerySelector, QueryBudget
    from .unified_search import UnifiedSearchContext, query_source_resolvers
    if len(plan['sources']) > MAX_QUERY_ATTACHMENTS:
        raise QuerySourceBudgetExceeded('添付検索は12件までです。対象を選び直してください。')
    engine = service if service is not None else unified_search_service()
    sources = []
    current_source_refs = frozenset(current_source_refs)
    for source in plan['sources']:
        # Authority is not part of the wire schema. Only the connection layer
        # may identify DB-expanded current attachments via current_source_refs.
        if 'authority' in source:
            raise ValueError('query_source_authority_is_server_owned')
        selector = source.get('selector') or {}
        ref = SourceRef(source['source_kind'], source['source_id']) if source.get('source_kind') else None
        sources.append(QuerySource(
            ref=ref, authority='current_context' if ref in current_source_refs else 'selected_scope',
            text=source.get('text'), selector=QuerySelector(
                pages=tuple(selector.get('pages') or ()), seconds=tuple(selector['seconds']) if selector.get('seconds') else None,
                characters=tuple(selector['characters']) if selector.get('characters') else None,
                frames=tuple(selector.get('frames') or ()))))
    native_plan = QueryPlan(instruction=plan['instruction'], sources=tuple(sources), mode=plan['mode'],
        scope=plan['scope'], targets=tuple(plan['targets']), limit=plan['limit'], version=plan['version'],
        budget=QueryBudget(max_sources=MAX_QUERY_ATTACHMENTS, max_source_bytes=MAX_QUERY_ATTACHMENT_BYTES))
    context = UnifiedSearchContext(user_id=resolver.scope.user_id, project_id=resolver.scope.project_id,
        space_id=UUID(str(selected_space_id)) if selected_space_id else resolver.scope.space_id,
        docs_library_id=UUID(str(docs_library_id)) if docs_library_id else None,
        current_session_id=UUID(str(current_session_id)) if current_session_id else None, character_name=character_name,
        # Preserve the resolver's explicit saved-context restrictions. In
        # particular, a restricted personal scope has ``project_id=None`` but
        # must not become the legacy all-readable wildcard here.
        current_source_scope=resolver.scope,
        source_workspace_root=str(resolver.workspace_root), strict_project_scope=strict_project_scope)
    await authorize_query_sources(query_source_resolvers(native_plan, resolver.session, context), native_plan.sources)
    return await engine.search(native_plan, session=resolver.session, context=context)


def _safe_reason(value):
    reason = value.get('reason') if isinstance(value, dict) else value
    return reason if isinstance(reason, str) and re.fullmatch(r'[a-zA-Z0-9_:.-]{1,100}', reason) else 'search_incomplete'


def project_unified_response(response):
    """Stable API projection of native result citations and explicit coverage."""
    results = []
    for hit in response['results']:
        target = hit['target_kind']
        locator = hit['locator']
        citation = hit.get('citation') or {}
        document, chunk = hit.get('document') or {}, hit.get('chunk') or {}
        results.append({**hit, 'title': hit.get('title') or document.get('title') or document.get('path') or
            hit.get('character_name') or '会話',
            'text': str(hit.get('text') or chunk.get('text') or hit.get('content') or '')[:2000],
            'source_kind': hit.get('source_kind') or (locator['kind'] if locator['kind'] != 'docs_node' else None),
            'source_id': hit.get('source_id') or (locator['id'] if locator['kind'] != 'docs_node' else None),
            **{key: hit.get(key, citation.get(key)) for key in ['unit_id', 'page_number', 'start_seconds',
                'end_seconds', 'text_start', 'text_end', 'text_length']}})
    lanes = response['lane_status']
    return {'version': response.get('version', 1), 'results': results, 'query_sources': response['source_status'],
        'lane_status': [{'target_kind': target, **state, 'partitions': [
            {**p, **({'reason': _safe_reason(p['reason'])} if p.get('reason') else {})} for p in state.get('partitions', [])]}
            for target, state in lanes.items()],
        'complete': response['complete'], 'warnings': [_safe_reason(w) for w in response['warnings']]}


async def read_canonical_evidence(resolver, target_kind, locator, citation=None):
    """Delegate canonical hits to existing current readers and live ACLs."""
    def project_in_scope(project_id):
        # Keep an explicit personal partition distinct from the legacy
        # all-readable ``SearchScope(user_id)`` wildcard. The resolver's
        # final Project check remains authoritative for live ACL/Space state.
        if bool(getattr(resolver.scope, 'project_scope_restricted', False)):
            return project_id == resolver.scope.project_id
        if project_id is None:
            return resolver.scope.project_id is None and resolver.scope.space_id is None
        return resolver.scope.project_id is None or project_id == resolver.scope.project_id

    if not isinstance(locator, dict) or set(locator) - {'kind', 'id', 'session_id', 'docs_library_id'}:
        raise SourceUnavailable()
    identifier = UUID(str(locator.get('id')))
    citation = citation or {}
    if not isinstance(citation, dict) or set(citation) - {
        'derived_text_start', 'derived_text_end', 'text_start', 'text_end', 'text_length',
        'chunk_id', 'chunk_index', 'heading_path', 'page_number', 'start_seconds', 'end_seconds', 'unit_id', 'body_sha256'}:
        raise SourceUnavailable()
    if target_kind == 'docs' and locator.get('kind') == 'docs_node':
        from ..memory.models import KnowledgeNode
        from .docs_acl import can_read_node, is_docs_node_renderable
        from ..rag.docs_index import get_docs_index_service
        node = await resolver.session.get(KnowledgeNode, identifier, populate_existing=True)
        if node is None or not is_docs_node_renderable(node) or not await can_read_node(resolver.session, node, resolver.scope.user_id):
            raise SourceUnavailable()
        if locator.get('docs_library_id') and UUID(str(locator['docs_library_id'])) != node.docs_library_id:
            raise SourceUnavailable()
        if not project_in_scope(node.project_id):
            raise SourceUnavailable()
        await resolver._project(node.project_id)
        # This existing reader rebuilds exactly the normalized own-record
        # text addressed by the search citation, including typed body/Fields.
        current = await get_docs_index_service()._prepared_originals(resolver.session, node.docs_library_id, [identifier], resolver.scope.user_id)
        if identifier not in current or not await can_read_node(resolver.session, current[identifier][0], resolver.scope.user_id):
            raise SourceUnavailable()
        current_node = current[identifier][0]
        if not project_in_scope(current_node.project_id):
            raise SourceUnavailable()
        await resolver._project(current_node.project_id)
        text = current[identifier][1]
        verify_evidence_body(text, citation)
        original = f'/docs?node_id={identifier}'
        start, end = citation.get('derived_text_start'), citation.get('derived_text_end')
    elif target_kind == 'knowledge' and locator.get('kind') == 'knowledge_document':
        from ..knowledge.service import KnowledgeService
        async def verify_current_knowledge():
            try:
                await resolver.resolve(SourceRef('knowledge_document', str(identifier)), open_file=False)
            except SourceUnavailable as exc:
                # Canonical Knowledge can read retained remote text; the
                # native-media resolver denies it only because no file exists.
                # That reason occurs after its live source/Project ACL checks.
                if exc.reason != 'remote_growi_has_no_local_media_original':
                    raise
        await verify_current_knowledge()
        actor = await resolver._actor()
        payload = await KnowledgeService.read_document(resolver.session, actor_user_id=resolver.scope.user_id,
            is_admin=actor.role == 'admin', document_id=identifier)
        if payload.get('error'):
            raise SourceUnavailable()
        await verify_current_knowledge()
        text = str(payload.get('content') or '')
        if citation.get('chunk_id'):
            from ..memory.models import KnowledgeChunk
            chunk = await resolver.session.get(KnowledgeChunk, UUID(str(citation['chunk_id'])), populate_existing=True)
            if chunk is None or chunk.document_id != identifier or not chunk.text or chunk.text not in text:
                raise SourceUnavailable()
            # Exact currently readable chunk, without inventing an offset for
            # repeated text when the Knowledge reader supplies no char locator.
            text = chunk.text
        original = f'/api/knowledge/documents/{identifier}'
        start, end = citation.get('text_start'), citation.get('text_end')
    elif target_kind == 'conversation' and locator.get('kind') == 'conversation_message':
        source = await resolver.resolve(SourceRef('conversation_message', str(identifier)), open_file=False)
        if UUID(str(locator.get('session_id'))) != UUID(source.metadata['session_id']):
            raise SourceUnavailable()
        text = source.text
        verify_evidence_body(text, citation)
        original = f"/chat?s={source.metadata['session_id']}&message={identifier}"
        start, end = citation.get('text_start'), citation.get('text_end')
    else:
        raise SourceUnavailable()
    if start is not None or end is not None:
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise SourceUnavailable()
        text = text[start:end]
    complete = len(text) <= 12000
    return {'text': text[:12000], 'citation': citation, 'complete': complete,
        'warnings': [] if complete else ['evidence_text_truncated'],
        'original_url': original if target_kind != 'conversation' else None,
        'conversation_url': original if target_kind == 'conversation' else None}
