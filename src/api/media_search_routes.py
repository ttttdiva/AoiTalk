"""Authenticated search and page/time/text evidence in the common media index."""
from __future__ import annotations

import io
import math
import hashlib
import mimetypes
import os
import re
from typing import Any, Literal
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from starlette.background import BackgroundTask

from .media_operations_routes import _actor, _principal_projection, _with_session
from ..memory.models import Space
from ..rag.index_io import run_index_io
from ..rag.media_index import get_media_index_service
from ..services.search_sources import SearchScope, SearchSourceResolver, SourceRef, SourceUnavailable
from ..services.chat_search_sources import QuerySourceBudgetExceeded, EvidenceChanged
from ..services.media_asset_storage import open_asset
from ..services.storage_roots import StorageRoot
from ..services.storage_io import StorageError
from .routes.storage_stream import byte_range


SourceKind = Literal['generation_output', 'generated_media', 'chat_attachment', 'conversation_message',
                     'task_attachment', 'docs_attachment', 'knowledge_document', 'meeting_audio',
                     'workspace_file']
TargetKind = Literal['docs', 'knowledge', 'conversation', 'media', 'files']


class FilesLocation(BaseModel):
    """Where a User Files / Project Files hit lives, for navigating the Files UI."""
    scope: Literal['user', 'project']
    project_id: str | None = None
    relative_path: str = Field(description='POSIX path relative to the User Files / Project Files root.')
    path: str = Field(description='Workspace-relative explorer path (_users/user_<id>/... or _projects/project_<id>/...).')


class OtherLocation(BaseModel):
    """The same original bytes found through another source (secondary display)."""
    target_kind: TargetKind
    locator: dict[str, Any] = Field(default_factory=dict)
    source_kind: SourceKind | None = None
    source_id: str | None = None
    title: str | None = None
    unit_id: UUID | None = None
    score: float | None = None


class MediaImageQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_kind: SourceKind
    source_id: str = Field(min_length=1, max_length=120)
    page_number: int = Field(default=1, ge=1, le=100000)

    @model_validator(mode='after')
    def valid_locator(self):
        parts = self.source_id.split(':')
        UUID(parts[0])
        if self.source_kind == 'chat_attachment':
            if len(parts) != 2 or int(parts[1]) < 0:
                raise ValueError('Invalid image locator')
        elif len(parts) != 1:
            raise ValueError('Invalid image locator')
        return self


class MediaQuerySelector(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pages: list[StrictInt] = Field(default_factory=list, max_length=256)
    seconds: tuple[float, float] | None = None
    characters: tuple[StrictInt, StrictInt] | None = None
    frames: list[StrictInt] = Field(default_factory=list, max_length=256)

    @model_validator(mode='after')
    def valid_selection(self):
        from ..rag.media_query import QuerySelector
        QuerySelector(pages=tuple(self.pages), seconds=self.seconds,
                      characters=self.characters, frames=tuple(self.frames))
        return self


class MediaQuerySource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_kind: SourceKind | None = None
    source_id: str | None = Field(default=None, min_length=1, max_length=120)
    text: str | None = Field(default=None, min_length=1, max_length=32000)
    selector: MediaQuerySelector = Field(default_factory=MediaQuerySelector)

    @model_validator(mode='after')
    def valid_source(self):
        if (self.source_kind is None) != (self.source_id is None):
            raise ValueError('Source requires kind and id')
        from ..rag.media_query import QuerySource, QuerySelector
        QuerySource(ref=SourceRef(self.source_kind, self.source_id) if self.source_kind else None,
                    text=self.text, selector=QuerySelector(**{
                        'pages': tuple(self.selector.pages), 'seconds': self.selector.seconds,
                        'characters': self.selector.characters, 'frames': tuple(self.selector.frames)}))
        return self


class MediaQueryPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal[1] = 1
    instruction: str = Field(default='', max_length=4000)
    sources: list[MediaQuerySource] = Field(default_factory=list, max_length=12)
    mode: Literal['combined', 'separate'] = 'combined'
    scope: Literal['current_context', 'project', 'space', 'all_readable'] = 'all_readable'
    targets: list[TargetKind] = Field(
        default_factory=lambda: ['docs', 'knowledge', 'conversation', 'media', 'files'], min_length=1, max_length=5)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode='after')
    def valid_plan(self):
        from ..rag.media_query import QueryPlan, QuerySource, QuerySelector
        sources = tuple(QuerySource(
            ref=SourceRef(s.source_kind, s.source_id) if s.source_kind else None, text=s.text,
            selector=QuerySelector(pages=tuple(s.selector.pages), seconds=s.selector.seconds,
                characters=s.selector.characters, frames=tuple(s.selector.frames))) for s in self.sources)
        # Empty saved-attachment plans are expanded only after DB authorization.
        QueryPlan(instruction=self.instruction, sources=sources or (QuerySource(text='pending_saved_sources'),), mode=self.mode, scope=self.scope,
                  targets=tuple(self.targets), limit=self.limit, version=self.version)
        return self


class ChatSearchAttachment(BaseModel):
    index: int
    filename: str
    kind: Literal['text', 'image', 'audio', 'video']
    size_bytes: int


class MediaSearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(default='', max_length=4000)
    image: MediaImageQuery | None = None
    project_id: UUID | None = None
    space_id: UUID | None = None
    limit: int = Field(default=20, ge=1, le=100)
    plan: MediaQueryPlan | None = None
    use_current_attachments: bool = False
    session_id: UUID | None = None
    message_id: UUID | None = None
    attachment_indices: list[StrictInt] | None = Field(default=None, min_length=1, max_length=12)

    @model_validator(mode='after')
    def requires_query(self):
        if self.use_current_attachments and (self.session_id is None or self.message_id is None):
            raise ValueError('Saved message and conversation are required')
        if self.attachment_indices is not None and not self.use_current_attachments:
            raise ValueError('Attachment selection requires the current saved message')
        if self.plan is not None and (self.query.strip() or self.image is not None):
            raise ValueError('Use one query plan')
        if self.plan is not None:
            if not self.plan.instruction.strip() and not self.plan.sources and not self.use_current_attachments:
                raise ValueError('A query or saved source is required')
            if self.plan.scope == 'project' and self.project_id is None:
                raise ValueError('Project scope requires a project')
            if self.plan.scope == 'space' and self.space_id is None:
                raise ValueError('Space scope requires a space')
            if self.plan.scope == 'current_context' and self.session_id is None:
                # Standalone User Files has an authenticated personal partition
                # but no saved conversation. Keep every other current-context
                # query bound to its authenticated conversation.
                personal_files_query = (
                    self.plan.targets == ['files'] and not self.plan.sources
                    and self.project_id is None and self.space_id is None
                    and not self.use_current_attachments and self.message_id is None
                )
                if not personal_files_query:
                    raise ValueError('Current context requires an authenticated conversation')
        if not self.query.strip() and self.image is None and self.plan is None and not self.use_current_attachments:
            raise ValueError('Enter a query or select an image')
        return self


class MediaSearchResult(BaseModel):
    unit_id: UUID
    source_kind: SourceKind
    source_id: str
    title: str
    modality: Literal['text', 'image', 'audio', 'video']
    text: str = ''
    score: float = 0
    page_number: int | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None
    text_start: int | None = Field(default=None, ge=0, description=(
        '0-based inclusive Unicode code point offset in the normalized searchable '
        'header/body evidence. Not a raw MSG/EML byte offset; null when unavailable.'))
    text_end: int | None = Field(default=None, ge=0, description=(
        '0-based exclusive Unicode code point offset in the normalized searchable '
        'header/body evidence. Addresses the full unit before response text truncation, '
        'not raw MSG/EML bytes; null when unavailable.'))
    text_length: int | None = Field(default=None, ge=0, description=(
        'Total Unicode code point count of the normalized searchable header/body '
        'evidence across all units, not the response snippet length or raw MSG/EML '
        'byte length; null when unavailable.'))
    original_url: str | None = None
    conversation_url: str | None = None
    preview_url: str | None = None
    files_location: FilesLocation | None = None


class MediaSearchResponse(BaseModel):
    results: list[MediaSearchResult]
    semantic_available: bool = True


class MediaOriginalResponse(BaseModel):
    result: MediaSearchResult


class MediaUnifiedHit(BaseModel):
    target_kind: TargetKind
    locator: dict[str, Any]
    title: str
    text: str = ''
    score: float = Field(default=0, description=(
        'Canonical EmbeddingGemma 2 pure dense cosine used for cross-lane ordering. '
        'It is the same value as similarity and is not a partition-local RRF or keyword score.'))
    similarity: float | None = Field(default=None, description=(
        'Canonical EmbeddingGemma 2 pure dense cosine of the query and this hit. '
        'It duplicates score for client compatibility and is null only for legacy/non-comparable payloads.'))
    unit_id: UUID | None = None
    source_kind: SourceKind | None = None
    source_id: str | None = None
    modality: Literal['text', 'image', 'audio', 'video'] = 'text'
    page_number: int | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None
    text_start: int | None = None
    text_end: int | None = None
    text_length: int | None = None
    citation: dict[str, Any] = Field(default_factory=dict)
    matched_query_sources: list[dict[str, Any]] = Field(default_factory=list)
    files_location: FilesLocation | None = Field(default=None, description=(
        'Present for target_kind=files: the User/Project Files location to open in the Files UI.'))
    other_locations: list[OtherLocation] = Field(default_factory=list, description=(
        'Other indexed sources holding byte-identical originals (for example a chat attachment of the same '
        'file). The Files location is the primary result; these are secondary.'))


class MediaQuerySourceStatus(BaseModel):
    source: str
    status: str
    reason: str | None = None
    total_units: int | None = None
    processed_units: int = 0
    processed_ranges: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MediaSearchLaneStatus(BaseModel):
    target_kind: TargetKind
    status: str
    partitions: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    weak_match: bool = Field(default=True, description=(
        'True when this lane found nothing or its best valid pure dense cosine is below the '
        'lane threshold; keyword overlap is not used as match confidence.'))


class MediaUnifiedSearchResponse(BaseModel):
    version: Literal[1] = 1
    results: list[MediaUnifiedHit]
    query_sources: list[MediaQuerySourceStatus]
    lane_status: list[MediaSearchLaneStatus]
    complete: bool
    warnings: list[str]
    weak_match: bool = Field(default=True, description=(
        'True when every searched lane is weak_match or has no valid pure dense cosine. '
        'Treat the results as low-confidence candidates requiring original-read confirmation.'))

    @model_validator(mode='before')
    @classmethod
    def _derive_weak_match(cls, data):
        # The overall flag is defined by the lanes; derive it when a projection
        # carries only the per-lane flags.
        if isinstance(data, dict) and 'weak_match' not in data and isinstance(data.get('lane_status'), list):
            from ..services.unified_search import overall_weak_match
            lanes = [lane if isinstance(lane, dict) else lane.model_dump() for lane in data['lane_status']]
            data = {**data, 'weak_match': overall_weak_match(lanes)}
        return data


class MediaEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_kind: Literal['docs', 'knowledge', 'conversation']
    locator: dict[str, Any]
    citation: dict[str, Any] = Field(default_factory=dict,
        description='Docs/conversation require the search citation body_sha256 of the full canonical UTF-8 text. Changed/missing versions return 409 before offsets are applied.')
    project_id: UUID | None = None
    space_id: UUID | None = None


class MediaEvidenceResponse(BaseModel):
    text: str
    citation: dict[str, Any]
    complete: bool
    warnings: list[str]
    original_url: str | None = None
    conversation_url: str | None = None


class MediaIndexProgressResponse(BaseModel):
    indexing: dict[str, int]
    reconciliation: dict[str, int]
    warnings: list[Literal['index_retry_pending', 'inventory_scan_failed']] = Field(default_factory=list)


def safe_local_url(value):
    """Keep links on this origin; reject schemes, authority and control bytes."""
    if not isinstance(value, str) or not value.startswith('/') or value.startswith('//'):
        return None
    decoded = unquote(value)
    if decoded.startswith('//') or '\\' in decoded or any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    return value


def result_dto(hit, project_id=None, space_id=None):
    original = safe_local_url(hit.get('original_url'))
    page = hit.get('page_number')
    start = hit.get('start_seconds')
    fragment = urlsplit(original).fragment if original else ''
    if isinstance(page, int) and page > 0:
        fragment = f'page={page}'
    elif isinstance(start, (float, int)) and math.isfinite(start) and start >= 0:
        end = hit.get('end_seconds')
        fragment = f't={start:g}' + (f',{end:g}' if isinstance(end, (float, int)) and math.isfinite(end) and end > start else '')
    if original:
        parts = urlsplit(original)
        original = urlunsplit((*parts[:4], fragment))
    conversation = None
    try:
        session = UUID(str(hit.get('session_id')))
        params = {'s': str(session)}
        if hit.get('message_id'):
            params['message'] = str(UUID(str(hit['message_id'])))
        conversation = '/chat?' + urlencode(params)
    except (ValueError, TypeError, AttributeError):
        pass
    unit_id = UUID(str(hit['unit_id']))
    params = [(name, str(value)) for name, value in [('project_id', project_id), ('space_id', space_id)] if value]
    preview = f'/api/search/media/{unit_id}/preview' + ('?' + urlencode(params) if params else '')
    if hit['source_kind'] != 'conversation_message':
        # Use this ACL-checked byte route for every native original. Some
        # legacy metadata links describe a source but do not deliver bytes.
        original = f'/api/search/media/{unit_id}/content' + ('?' + urlencode(params) if params else '') + ('#' + fragment if fragment else '')
    return MediaSearchResult(
        **{key: hit[key] for key in ['unit_id', 'source_kind', 'source_id', 'title', 'modality']},
        score=hit.get('score', 0), text=str(hit.get('text') or '')[:2000],
        page_number=page, start_seconds=start, end_seconds=hit.get('end_seconds'),
        text_start=hit.get('text_start'), text_end=hit.get('text_end'),
        text_length=hit.get('text_length'),
        original_url=original, conversation_url=conversation,
        preview_url=preview if hit['modality'] == 'image' else None,
        files_location=hit.get('files_location') if hit['source_kind'] == 'workspace_file' else None,
    )


def create_media_search_router(get_db_manager, get_user_from_request, require_auth_dependency,
                               *, workspace_root=None, service_provider=None, queue_provider=None,
                               unified_service_provider=None):
    def no_store(response: Response):
        response.headers['Cache-Control'] = 'no-store'
    router = APIRouter(prefix='/api/search/media', tags=['search-media'], dependencies=[Depends(no_store)])
    get_service = service_provider or get_media_index_service

    async def call(request, project, space, operation):
        principal = _principal_projection(await _actor(get_user_from_request, request))
        try:
            user_id = UUID(str(principal['id'] or principal['user_id']))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(401, 'Not authenticated') from None

        async def invoke(session):
            resolver = SearchSourceResolver(session, SearchScope(user_id, project, space), workspace_root=workspace_root)
            actor = await resolver._actor()
            if project is not None:
                await resolver._project(project)
            elif space is not None:
                from ..services.space_access import get_readable_space
                if await session.get(Space, space, populate_existing=True) is None or await get_readable_space(
                    session, space_id=space, user_id=user_id, user_info={'role': actor.role}
                ) is None:
                    raise SourceUnavailable()
            return await operation(resolver)

        try:
            return await _with_session(get_db_manager, invoke)
        except QuerySourceBudgetExceeded:
            raise HTTPException(413, 'Query attachments exceed the 12-source / 100 MB budget') from None
        except EvidenceChanged as exc:
            raise HTTPException(409, exc.reason) from None
        except SourceUnavailable:
            raise HTTPException(404, 'Source unavailable') from None
        except (ValueError, OSError):
            raise HTTPException(404, 'Source unavailable') from None
        except RuntimeError:
            raise HTTPException(503, 'Media search is temporarily unavailable') from None
        except StorageError:
            raise HTTPException(404, 'Source unavailable') from None
        except Exception:
            raise HTTPException(503, 'Media search is temporarily unavailable') from None

    @router.post('', response_model=MediaSearchResponse | MediaUnifiedSearchResponse, operation_id='media_search')
    async def search(body: MediaSearchRequest, request: Request, _: Any = Depends(require_auth_dependency)):
        async def operation(resolver):
            if body.plan is not None or body.use_current_attachments:
                from ..services.chat_search_sources import saved_attachment_sources, execute_query_plan
                # A search Space is a corpus filter, not authority for the
                # current saved conversation or its query attachment bytes.
                resolver = SearchSourceResolver(resolver.session,
                    SearchScope(resolver.scope.user_id, body.project_id), workspace_root=workspace_root)
                conversation = await resolver._conversation(body.session_id) if body.session_id else None
                if conversation is not None:
                    # The saved conversation, rather than a client character
                    # or omitted project, supplies current query authority.
                    resolver = SearchSourceResolver(resolver.session,
                        SearchScope(resolver.scope.user_id, conversation.project_id),
                        workspace_root=workspace_root)
                plan = (body.plan or MediaQueryPlan(instruction=body.query)).model_dump(mode='json')
                current_refs = []
                if body.use_current_attachments:
                    sources = await saved_attachment_sources(resolver, session_id=body.session_id,
                        message_id=body.message_id, indices=body.attachment_indices)
                    plan['sources'].extend({'source_kind': s['ref'].kind, 'source_id': s['ref'].id} for s in sources)
                    current_refs = [s['ref'] for s in sources]
                result = await execute_query_plan(resolver, plan,
                    service=unified_service_provider() if unified_service_provider else None,
                    current_session_id=body.session_id,
                    character_name=conversation.character_name if conversation else None,
                    current_source_refs=current_refs, selected_space_id=body.space_id)
                from ..services.chat_search_sources import project_unified_response
                return MediaUnifiedSearchResponse.model_validate(project_unified_response(result))
            image_ref = SourceRef(body.image.source_kind, body.image.source_id) if body.image else None
            if image_ref:
                # Deny before index/vector lookup even when the actor has no
                # currently ready candidates in this scope.
                await resolver.resolve(image_ref, open_file=False)
            service = get_service()
            hits = await service.search(resolver, query=body.query.strip(), image_ref=image_ref,
                query_page=body.image.page_number if body.image else 1, limit=body.limit)
            return MediaSearchResponse(results=[result_dto(h, body.project_id, body.space_id) for h in hits],
                                       semantic_available=not bool(service.embedding.last_error))
        if body.plan is not None or body.use_current_attachments:
            target_space = body.space_id if body.plan is not None and body.plan.scope == 'space' else None
            return await call(request, None, target_space, operation)
        return await call(request, body.project_id, body.space_id, operation)

    @router.get('/attachments', response_model=list[ChatSearchAttachment], operation_id='media_search_attachments')
    async def attachments(request: Request, session_id: UUID, message_id: UUID,
                          project_id: UUID | None = None, space_id: UUID | None = None,
                          _: Any = Depends(require_auth_dependency)):
        async def operation(resolver):
            from ..services.chat_search_sources import saved_attachment_sources
            values = await saved_attachment_sources(resolver, session_id=session_id, message_id=message_id)
            return [ChatSearchAttachment(**{k: s[k] for k in ['index', 'filename', 'kind', 'size_bytes']}) for s in values]
        return await call(request, project_id, space_id, operation)

    @router.post('/evidence', response_model=MediaEvidenceResponse, operation_id='media_search_evidence')
    async def unified_evidence(body: MediaEvidenceRequest, request: Request, _: Any = Depends(require_auth_dependency)):
        async def operation(resolver):
            from ..services.chat_search_sources import read_canonical_evidence
            return MediaEvidenceResponse.model_validate(await read_canonical_evidence(
                resolver, body.target_kind, body.locator, body.citation))
        return await call(request, body.project_id, body.space_id, operation)

    @router.get('/progress', response_model=MediaIndexProgressResponse, operation_id='media_search_progress')
    async def progress(request: Request, project_id: UUID | None = None, space_id: UUID | None = None,
                       _: Any = Depends(require_auth_dependency)):
        async def operation(resolver):
            if queue_provider:
                queue = queue_provider()
            else:
                from ..services.media_index_worker import MediaIndexQueue
                queue = MediaIndexQueue()
            return MediaIndexProgressResponse(**await queue.scoped_status(resolver))
        return await call(request, project_id, space_id, operation)

    async def evidence(resolver, unit_id):
        current, chunk = await get_service().read_original(resolver, str(unit_id))
        hit = {**chunk.metadata, 'unit_id': str(unit_id), 'source_kind': current.ref.kind,
               'source_id': current.ref.id, 'title': current.title, 'text': chunk.lexical_text,
               'modality': chunk.modality, **{key: current.metadata.get(key) for key in
                   ['original_url', 'session_id', 'message_id', 'files_location']}}
        return current, chunk, result_dto(hit, resolver.scope.project_id, resolver.scope.space_id)

    @router.get('/{unit_id}', response_model=MediaOriginalResponse, operation_id='media_read_original')
    async def read(unit_id: UUID, request: Request, project_id: UUID | None = None,
                   space_id: UUID | None = None, _: Any = Depends(require_auth_dependency)):
        async def operation(resolver):
            _, _, result = await evidence(resolver, unit_id)
            return MediaOriginalResponse(result=result)
        return await call(request, project_id, space_id, operation)

    @router.get('/{unit_id}/preview', response_class=Response, operation_id='media_read_preview',
                responses={200: {'content': {'image/png': {'schema': {'type': 'string', 'format': 'binary'}}}}})
    async def preview(unit_id: UUID, request: Request, project_id: UUID | None = None,
                      space_id: UUID | None = None, _: Any = Depends(require_auth_dependency)):
        async def operation(resolver):
            current, chunk, _ = await evidence(resolver, unit_id)
            if chunk.modality != 'image' or 'image' not in chunk.model_input:
                raise SourceUnavailable()
            def encode():
                image = chunk.model_input['image'].copy()
                image.thumbnail((768, 768))
                buffer = io.BytesIO()
                image.save(buffer, format='PNG')
                return buffer.getvalue()
            payload = await run_index_io(encode)
            latest = await resolver.resolve(current.ref)
            if await run_index_io(get_service()._hash, latest.path) != chunk.metadata.get('content_hash'):
                raise SourceUnavailable()
            return Response(payload, media_type='image/png', headers={'Cache-Control': 'no-store',
                'X-Content-Type-Options': 'nosniff', 'Content-Disposition': 'inline; filename="search-preview.png"'})
        return await call(request, project_id, space_id, operation)

    @router.get('/{unit_id}/content', response_class=Response, operation_id='media_read_content',
                responses={200: {'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}},
                           206: {'description': 'Authorized original byte range'}})
    async def content(unit_id: UUID, request: Request, project_id: UUID | None = None,
                      space_id: UUID | None = None, _: Any = Depends(require_auth_dependency)):
        async def operation(resolver):
            current, chunk, _ = await evidence(resolver, unit_id)
            if current.path is None:
                raise SourceUnavailable()
            root = StorageRoot('search-original', 'Search original', str(current.path.parent), read_only=True, external=False)
            pinned = open_asset(root, current.path.name)
            handle = None
            entered = [False]
            def close():
                if entered[0]:
                    entered[0] = False
                    pinned.__exit__(None, None, None)
            try:
                def prepare():
                    opened, info = pinned.__enter__()
                    entered[0] = True
                    try:
                        digest = hashlib.sha256()
                        while block := opened.read(1024 * 1024):
                            digest.update(block)
                        if digest.hexdigest() != chunk.metadata.get('content_hash'):
                            raise SourceUnavailable()
                        return opened, info
                    except BaseException:
                        close()
                        raise
                handle, info = await run_index_io(prepare)
                latest = await resolver.resolve(current.ref)
                if await run_index_io(get_service()._hash, latest.path) != chunk.metadata.get('content_hash'):
                    raise SourceUnavailable()
                start, end, partial = byte_range(request.headers.get('range'), info.st_size)
                mime = current.metadata.get('mime_type') or mimetypes.guess_type(current.title)[0] or 'application/octet-stream'
                mime = str(mime).split(';', 1)[0].strip().lower()
                if not re.fullmatch(r'[a-z0-9.+_-]+/[a-z0-9.+_-]+', mime):
                    mime = 'application/octet-stream'
                inline = mime == 'application/pdf' or mime.startswith(('image/png', 'image/jpeg', 'image/webp', 'image/gif', 'audio/', 'video/'))
                name = current.title.replace('\\', '/').rsplit('/', 1)[-1]
                disposition = ('inline' if inline else 'attachment') + "; filename*=UTF-8''" + quote(name, safe='')
                headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                           'Content-Disposition': disposition, 'Content-Length': str(max(0, end - start + 1)), 'Accept-Ranges': 'bytes'}
                if partial:
                    headers['Content-Range'] = f'bytes {start}-{end}/{info.st_size}'
                async def stream():
                    # Retain current._resource until the descriptor is closed;
                    # managed asset snapshots must outlive the HTTP response.
                    retained = current
                    try:
                        cursor = start
                        while cursor <= end:
                            def read_block():
                                now = os.fstat(handle.fileno())
                                if (now.st_size, now.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                                    raise SourceUnavailable()
                                handle.seek(cursor)
                                return handle.read(min(1024 * 1024, end - cursor + 1))
                            block = await run_index_io(read_block)
                            if not block:
                                raise SourceUnavailable()
                            cursor += len(block)
                            yield block
                    finally:
                        await run_index_io(close)
                        del retained
                return StreamingResponse(stream(), media_type=mime, status_code=206 if partial else 200, headers=headers,
                                         background=BackgroundTask(run_index_io, close))
            except BaseException:
                await run_index_io(close)
                raise
        return await call(request, project_id, space_id, operation)

    return router
