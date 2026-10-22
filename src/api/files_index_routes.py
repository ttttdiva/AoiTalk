"""Status, settings and rescan for the User Files / Project Files semantic index.

Every response is limited to the caller's own User Files and Projects the
caller can currently read; other users' User Files are never reported, even
to admins.  Project overrides require ``manage_settings``; global settings
require an admin.
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .media_operations_routes import _actor, _principal_projection, _with_session
from ..services.search_sources import SourceUnavailable


IndexState = Literal['ready', 'pending', 'running', 'failed', 'pending_hash', 'excluded', 'unsupported']


class FilesIndexCounts(BaseModel):
    total: int = Field(0, description='Live inventory rows (excluding deleted).')
    indexable: int = Field(0, description='Rows whose bytes are verified and eligible for indexing.')
    ready: int = Field(0, description='Indexable rows with verified vectors for the current content hash.')
    pending: int = Field(0, description='Indexable rows waiting for (re)indexing.')
    running: int = 0
    failed: int = Field(0, description='Indexable rows whose indexing failed (attempt-capped; retry via rescan).')
    pending_hash: int = Field(0, description='New/changed files waiting for the scanner to hash them.')
    excluded: int = 0
    unsupported: int = 0


class FilesLocationModel(BaseModel):
    scope: Literal['user', 'project']
    project_id: str | None = None
    relative_path: str
    path: str


class FilesIndexRootStatus(BaseModel):
    scope: Literal['user', 'project']
    project_id: str | None = None
    project_name: str | None = None
    enabled: bool
    counts: FilesIndexCounts
    skipped_directories: dict[str, int] = Field(default_factory=dict, description=(
        'Directories not descended in the last full scan, by reason (hidden, pattern:<glob>, link).'))
    root_available: bool | None = Field(None, description='False when the root could not be listed (never treated as deletion).')
    last_full_scan_at: str | None = None


class FilesIndexStatusResponse(BaseModel):
    enabled: bool
    semantic_available: bool
    counts: FilesIndexCounts
    excluded_reasons: dict[str, int] = Field(default_factory=dict)
    queue_waiting: int = 0
    last_scan_at: str | None = None
    last_indexed_at: str | None = None
    scanner_running: bool = False
    scanner_error: str | None = None
    roots: list[FilesIndexRootStatus] = Field(default_factory=list)


class FilesIndexEntry(BaseModel):
    id: str
    scope: Literal['user', 'project']
    project_id: str | None = None
    rel_path: str
    name: str
    size_bytes: int
    status: str
    index_state: IndexState
    excluded_reason: str | None = None
    error: str | None = None
    last_scanned_at: str | None = None
    files_location: FilesLocationModel


class FilesIndexEntriesResponse(BaseModel):
    total: int
    offset: int
    limit: int
    entries: list[FilesIndexEntry]


class FilesIndexHeavyMediaPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    defer_bytes: int = Field(description='Files larger than this (and audio/video) are indexed after small files.')
    max_media_bytes: int = Field(description='Audio/video above this size are excluded (media_size_limit).')
    max_duration_seconds: int = Field(description='Audio/video longer than this are excluded at indexing time.')


class FilesIndexScopes(BaseModel):
    model_config = ConfigDict(extra='forbid')
    user: bool = True
    project: bool = True


class FilesIndexScopePatterns(BaseModel):
    model_config = ConfigDict(extra='forbid')
    user: list[str] = Field(default_factory=list)
    project: list[str] = Field(default_factory=list)


class FilesIndexGlobalSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool
    scopes: FilesIndexScopes
    include_roots: list[str] = Field(description='Relative folders to index; empty means the whole root.')
    exclude_patterns: list[str] = Field(description=(
        'Globs. "name/" excludes directories at any depth, "*.ext" matches file names, '
        'patterns containing "/" match the relative path.'))
    include_patterns: list[str] = Field(description='Globs that re-include otherwise excluded files/directories.')
    scope_exclude_patterns: FilesIndexScopePatterns
    max_file_bytes: int
    max_text_file_bytes: int
    max_concurrency: int = Field(description=(
        'Concurrent Files indexing jobs (1-4). Model inference stays serialized by the single '
        'EmbeddingGemma 2 IPC worker; extra lanes overlap hashing/decoding/Office conversion.'))
    scan_interval_seconds: int
    max_failed_attempts: int
    heavy_media_policy: FilesIndexHeavyMediaPolicy


class FilesIndexOwnerRules(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool | None = None
    include_roots: list[str] | None = Field(None, description='Replaces the global include_roots for this owner.')
    exclude_patterns: list[str] | None = Field(None, description='Added to the global exclude patterns.')
    include_patterns: list[str] | None = Field(None, description='Added to the global include patterns.')


class FilesIndexSettingsResponse(BaseModel):
    global_settings: FilesIndexGlobalSettings
    user_override: FilesIndexOwnerRules
    project_id: str | None = None
    project_override: FilesIndexOwnerRules | None = None
    can_edit_global: bool
    can_edit_project: bool = False


class FilesIndexSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: Literal['global', 'user', 'project']
    project_id: UUID | None = None
    settings: dict[str, Any] = Field(description=(
        'global: partial FilesIndexGlobalSettings keys; user/project: FilesIndexOwnerRules '
        '(replaces that owner override; {} clears it).'))


class FilesIndexRescanRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: Literal['all', 'user', 'project'] = 'all'
    project_id: UUID | None = None
    retry_failed: bool = Field(False, description='Also re-queue attempt-capped failed index jobs in the scope.')


class FilesIndexRescanResponse(BaseModel):
    roots: int
    retried: int
    scanner_running: bool


def _rules_model(rules: dict | None) -> FilesIndexOwnerRules:
    return FilesIndexOwnerRules(**(rules or {}))


def create_files_index_router(get_db_manager, get_user_from_request, require_auth_dependency, *,
                              queue_provider=None, ledger_path='data/search/embeddinggemma2.sqlite',
                              queue_path='data/search/media-index-queue.sqlite'):
    def no_store(response: Response):
        response.headers['Cache-Control'] = 'no-store'

    router = APIRouter(prefix='/api/search/files', tags=['search-files'], dependencies=[Depends(no_store)])

    async def call(request, operation):
        principal = _principal_projection(await _actor(get_user_from_request, request))
        try:
            user_id = UUID(str(principal['id'] or principal['user_id']))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(401, 'Not authenticated') from None

        async def invoke(session):
            from ..services.search_sources import SearchScope, SearchSourceResolver
            actor = await SearchSourceResolver(session, SearchScope(user_id))._actor()
            return await operation(session, actor)

        try:
            return await _with_session(get_db_manager, invoke)
        except HTTPException:
            raise
        except SourceUnavailable:
            raise HTTPException(404, 'Files scope unavailable') from None
        except PermissionError as exc:
            raise HTTPException(403, str(exc) or 'Permission denied') from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            raise HTTPException(503, 'Files index is temporarily unavailable') from None

    def scope_args(scope, project_id):
        if project_id is not None and scope == 'user':
            raise ValueError('project_id requires scope=project or all')
        return {'project_id': project_id, 'scope': None if scope in {None, 'all'} else scope,
                'include_user': project_id is None}

    @router.get('/status', response_model=FilesIndexStatusResponse, operation_id='files_index_status')
    async def status(request: Request, scope: Literal['all', 'user', 'project'] = 'all',
                     project_id: UUID | None = None, _: Any = Depends(require_auth_dependency)):
        async def operation(session, actor):
            from ..services.workspace_file_index import files_index_status
            result = await files_index_status(session, actor.id, ledger_path=ledger_path, queue_path=queue_path,
                                              **scope_args(scope, project_id))
            return FilesIndexStatusResponse.model_validate(result)
        return await call(request, operation)

    @router.get('/entries', response_model=FilesIndexEntriesResponse, operation_id='files_index_entries')
    async def entries(request: Request, state: Annotated[list[IndexState] | None, Query()] = None,
                      scope: Literal['all', 'user', 'project'] = 'all', project_id: UUID | None = None,
                      offset: int = 0, limit: int = 100, _: Any = Depends(require_auth_dependency)):
        if offset < 0 or not 1 <= limit <= 500:
            raise HTTPException(422, 'offset>=0 and 1<=limit<=500 are required')
        states = set(state or ['failed', 'excluded', 'unsupported'])

        async def operation(session, actor):
            from ..services.workspace_file_index import files_index_status
            result = await files_index_status(session, actor.id, ledger_path=ledger_path, queue_path=queue_path,
                                              entries_filter=states, offset=offset, limit=limit,
                                              **scope_args(scope, project_id))
            return FilesIndexEntriesResponse(total=result['entries_total'], offset=offset, limit=limit,
                                             entries=result['entries'])
        return await call(request, operation)

    async def settings_payload(session, actor, project_id):
        from ..services.workspace_file_index import load_files_index_settings
        from ..memory.project_repository import ProjectRepository
        from ..services.workspace_file_index import visible_roots

        settings = await asyncio.to_thread(load_files_index_settings)
        overrides = settings.pop('overrides', {})
        can_edit_project = False
        project_override = None
        if project_id is not None:
            await visible_roots(session, actor.id, project_id=project_id, scope='project')
            project_override = _rules_model(overrides.get('projects', {}).get(str(project_id)))
            can_edit_project = bool(await ProjectRepository.has_permission(
                session, project_id, actor.id, 'manage_settings'))
        return FilesIndexSettingsResponse(
            global_settings=FilesIndexGlobalSettings.model_validate(settings),
            user_override=_rules_model(overrides.get('users', {}).get(str(actor.id))),
            project_id=str(project_id) if project_id else None,
            project_override=project_override,
            can_edit_global=str(actor.role or '').lower() == 'admin',
            can_edit_project=can_edit_project,
        )

    @router.get('/settings', response_model=FilesIndexSettingsResponse, operation_id='files_index_settings')
    async def get_settings(request: Request, project_id: UUID | None = None,
                           _: Any = Depends(require_auth_dependency)):
        async def operation(session, actor):
            return await settings_payload(session, actor, project_id)
        return await call(request, operation)

    @router.put('/settings', response_model=FilesIndexSettingsResponse, operation_id='files_index_update_settings')
    async def put_settings(body: FilesIndexSettingsUpdate, request: Request,
                           _: Any = Depends(require_auth_dependency)):
        async def operation(session, actor):
            from ..memory.project_repository import ProjectRepository
            from ..services.workspace_file_index import (
                FilesRoot, _normalize_rules, get_active_files_index_service, load_files_index_settings,
                normalize_files_index_settings, save_files_index_changes)

            if body.target == 'global':
                if str(actor.role or '').lower() != 'admin':
                    raise PermissionError('Global Files index settings require an administrator')
                if 'overrides' in body.settings:
                    raise ValueError('Use target=user/project for overrides')
                current = await asyncio.to_thread(load_files_index_settings)
                merged = {**current, **body.settings}
                normalized = normalize_files_index_settings(merged, strict=True)
                changes = {f'search.files_index.{key}': normalized[key] for key in body.settings}
                roots = None
            else:
                rules = _normalize_rules(body.settings, strict=True, name='settings')
                if body.target == 'user':
                    if body.project_id is not None:
                        raise ValueError('project_id is only valid for target=project')
                    key = f'search.files_index.overrides.users.{actor.id}'
                    roots = [FilesRoot('user', actor.id)]
                else:
                    if body.project_id is None:
                        raise ValueError('target=project requires project_id')
                    if not await ProjectRepository.has_permission(
                            session, body.project_id, actor.id, 'manage_settings'):
                        raise PermissionError('Project Files index settings require manage_settings')
                    key = f'search.files_index.overrides.projects.{body.project_id}'
                    roots = [FilesRoot('project', body.project_id)]
                changes = {key: rules}
            if not await asyncio.to_thread(save_files_index_changes, changes):
                raise RuntimeError('settings_not_saved')
            service = get_active_files_index_service()
            if service is not None:
                if roots is None:
                    from ..services.workspace_file_index import discover_roots
                    roots = await asyncio.to_thread(discover_roots, service.workspace_root)
                service.request_rescan(roots)
            return await settings_payload(session, actor, body.project_id)
        return await call(request, operation)

    @router.post('/rescan', response_model=FilesIndexRescanResponse, operation_id='files_index_rescan')
    async def rescan(body: FilesIndexRescanRequest, request: Request, _: Any = Depends(require_auth_dependency)):
        async def operation(session, actor):
            from ..services.workspace_file_index import (
                FILES_KIND, files_index_status, get_active_files_index_service, visible_roots)

            args = scope_args(body.scope, body.project_id)
            roots, _ = await visible_roots(session, actor.id, project_id=args['project_id'], scope=args['scope'],
                                           include_user=args['include_user'])
            service = get_active_files_index_service()
            if service is not None:
                service.request_rescan(roots)
            retried = 0
            if body.retry_failed:
                status = await files_index_status(session, actor.id, ledger_path=ledger_path, queue_path=queue_path,
                                                  entries_filter={'failed'}, **args)
                ids = [entry['id'] for entry in status['entries']]
                if ids:
                    if queue_provider is not None:
                        queue = queue_provider()
                    else:
                        from ..services.media_index_worker import MediaIndexQueue
                        queue = MediaIndexQueue(queue_path)
                    retried = await asyncio.to_thread(queue.retry_failed, FILES_KIND, ids)
            return FilesIndexRescanResponse(roots=len(roots), retried=retried,
                                            scanner_running=bool(service and service.scanning))
        return await call(request, operation)

    return router
