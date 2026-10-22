"""Search multimodal originals and inspect authorized page/time/text evidence."""

from __future__ import annotations
import base64
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import uuid
import wave

from ..core import tool
from .knowledge_tools import (
    _run_async_in_thread,
    _user_context,
    _project_scope,
    _active_turn,
)

logger = logging.getLogger(__name__)


class MediaUserRequired(PermissionError):
    """The tool call has no authenticated AoiTalk user."""


class ConversationContextRequired(PermissionError):
    """``current_context`` was requested outside a saved conversation."""


class InvalidReadArguments(ValueError):
    """media_read arguments are malformed (not an ACL or storage failure)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


# Model-facing result budget. A 32k-context local model cannot absorb the full
# API projection (15 hits were ~6.8k tokens); the full list stays available
# through the UI/API search endpoint.
MODEL_RESULT_LIMIT = 8
MODEL_SNIPPET_CHARS = 300

_READ_TARGET_KINDS = frozenset({'media', 'files', 'docs', 'knowledge', 'conversation'})
_DEFAULT_TARGETS = ['docs', 'knowledge', 'conversation', 'media', 'files']
_FILES_FALLBACK_NOTE = (
    "User Files/Project Filesの意味索引が未完了または無効のため、Filesの候補が欠けている可能性があります。"
    "Filesで候補が無い・少なくても該当なしと断定せず、ファイル名・パス・正確な語句が分かるなら"
    "search_files（名前・内容の文字列検索）やbm25_search（語句一致のランキング検索）でも確認してください。"
)
_FILES_FALLBACK_NOTE_WITHOUT_BM25 = _FILES_FALLBACK_NOTE.replace(
    "やbm25_search（語句一致のランキング検索）", ""
)


def _files_fallback_note() -> str:
    """Fallback note that names only tools the current turn exposes."""

    from ...llm.tool_policy import structured_task_hides_tool

    if structured_task_hides_tool("bm25_search"):
        return _FILES_FALLBACK_NOTE_WITHOUT_BM25
    return _FILES_FALLBACK_NOTE
_LOCATOR_KEYS = frozenset({'kind', 'id', 'session_id', 'docs_library_id'})
_LOCATOR_KINDS = {'docs': 'docs_node', 'knowledge': 'knowledge_document',
                  'conversation': 'conversation_message'}
_POSITION_KEYS = ('page_number', 'start_seconds', 'end_seconds', 'text_start', 'text_end', 'text_length')
_SEARCH_NEXT_STEP = (
    "原本確認は results[].read_args の各キーをそのまま media_read の引数に渡す"
    "（locator/citationはobjectのまま。文字列化・改変・推測しない）。"
    "回答に用いる関連度の高い候補をまず数件選び、原本を確認してから回答する。"
    "media_readが成功して内容が依頼に該当しなければ該当資料は見つからなかったと正直に伝え、"
    "読込失敗・権限拒否・原本不在なら原本を確認できなかったことと返された理由を伝え、"
    "該当なしとは断定しない。"
    "weak_match=falseでも全件が関連する意味ではない。"
    "matched_query_clues は検索に使った手がかり側の識別子で、media_readには渡さない。"
)
_SEARCH_EMPTY_NEXT_STEP = (
    "resultsは返却候補がないため、存在しないread_argsでmedia_readを呼び出さない。"
    "searched_scope・complete・warnings・lane_status・query_sourcesを確認し、"
    "検索範囲外、未完了、エラーによる候補なしと、完了した範囲で候補がなかったことを区別する。"
    "完了した検索範囲で候補がなくても、原本を確認できる候補が返らなかったと正直に伝える。"
    "必要ならscope='all_readable'を提案する（strict project scopeでは広げない）。"
)


def _failure(code: str, message: str) -> dict:
    return {"success": False, "complete": False, "error": code, "error_code": code, "message": message}


def _media_search_failure(exc: Exception) -> dict:
    """Map a failure to a reason the model can relay without guessing."""

    from ...services.chat_search_sources import QuerySourceBudgetExceeded

    if isinstance(exc, QuerySourceBudgetExceeded):
        return _failure("query_source_budget_exceeded",
                        "手がかりのsourceが件数または容量の上限を超えています。対象を減らしてください。")
    if isinstance(exc, MediaUserRequired):
        return _failure("authenticated_user_required",
                        "ログイン中のユーザーを特定できないためローカル検索を実行できません。")
    if isinstance(exc, ConversationContextRequired):
        return _failure("conversation_context_required",
                        "現在の会話を特定できません。scope='project'/'space'/'all_readable'を指定してください。")
    if isinstance(exc, PermissionError):
        return _failure("search_scope_not_permitted",
                        "指定した検索範囲またはsourceは現在の権限・Project制約では検索できません。")
    if isinstance(exc, ValueError):
        return _failure("invalid_search_request",
                        "検索条件が不正です（query/source/scope/mode/targetsを確認してください。"
                        "targetsとsourcesは文字列ではなく配列で渡す）。")
    logger.exception("media_search failed")
    return _failure("media_search_internal_error",
                    "ローカル検索の内部エラーで検索できませんでした。0件という意味ではありません。")


def _media_read_failure(exc: Exception) -> dict:
    """Distinguish malformed arguments, scope, missing/denied originals and bugs."""

    from ...services.chat_search_sources import EvidenceChanged
    from ...services.search_sources import SourceUnavailable

    if isinstance(exc, InvalidReadArguments):
        logger.warning("media_read rejected arguments: %s", exc.code)
        return _failure(exc.code, exc.message)
    if isinstance(exc, EvidenceChanged):
        logger.info("media_read evidence changed: %s", exc.reason)
        payload = _failure(exc.reason, "検索後に原本が更新されました。media_searchで検索し直してください。")
        payload["warnings"] = ["search_again_for_current_evidence"]
        return payload
    if isinstance(exc, MediaUserRequired):
        logger.warning("media_read without authenticated user")
        return _failure("authenticated_user_required",
                        "ログイン中のユーザーを特定できないため原本を読めません。")
    if isinstance(exc, ConversationContextRequired):
        logger.info("media_read without current conversation context")
        return _failure("conversation_context_required",
                        "現在の会話を特定できないため原本を読めません。scope='project'/'space'/'all_readable'を明示してください。")
    if isinstance(exc, SourceUnavailable):
        # Missing and unauthorized originals intentionally share one
        # non-disclosing outcome; only the fixed resolver reason is logged.
        logger.info("media_read original unavailable: %s", exc.reason)
        return _failure("media_original_unavailable",
                        "原本が存在しないか、現在の権限・指定scopeでは読めません。"
                        "検索時と同じscope（read_args）で呼んだか確認し、それでも失敗する場合は原本を確認できなかったと伝える。")
    if isinstance(exc, PermissionError):
        logger.info("media_read scope not permitted: %s", type(exc).__name__)
        return _failure("read_scope_not_permitted",
                        "指定したscope/project/spaceでは読めません（現在のProject制約を超えています）。")
    if isinstance(exc, ValueError):
        logger.warning("media_read invalid request: %s", type(exc).__name__)
        return _failure("invalid_read_request",
                        "読込条件が不正です（scope/space_id/project_idの組み合わせを確認してください）。")
    logger.exception("media_read failed")
    return _failure("media_read_internal_error",
                    "原本読込の内部エラーです。原本が無いという意味ではありません。")


def _coerce_object(value, *, code: str, name: str) -> dict | None:
    """Accept an object or the same object serialized as a JSON string."""

    if value is None or isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    raise InvalidReadArguments(
        code, f"{name}は検索結果のread_argsにあるobjectをそのまま渡してください（独自形式の文字列は不可）。")


def _validated_read_arguments(target_kind, unit_id, locator, citation):
    if target_kind not in _READ_TARGET_KINDS:
        raise InvalidReadArguments(
            'invalid_read_request', "target_kindはmedia/files/docs/knowledge/conversationのいずれかです。")
    if target_kind in {'media', 'files'}:
        try:
            return str(uuid.UUID(str(unit_id))), None, None
        except ValueError:
            raise InvalidReadArguments(
                'invalid_read_request',
                "target_kind='media'には検索結果read_argsのunit_id（UUID）が必要です。") from None
    locator = _coerce_object(locator, code='invalid_locator', name='locator')
    citation = _coerce_object(citation, code='invalid_locator', name='citation')
    if (not locator or set(locator) - _LOCATOR_KEYS
            or locator.get('kind') != _LOCATOR_KINDS[target_kind]):
        raise InvalidReadArguments(
            'invalid_locator',
            f"target_kind='{target_kind}'のlocatorは検索結果read_argsのobject"
            f"（kind='{_LOCATOR_KINDS[target_kind]}'）をそのまま渡してください。")
    try:
        uuid.UUID(str(locator.get('id')))
    except ValueError:
        raise InvalidReadArguments('invalid_locator', "locator.idが不正です。検索結果のlocatorをそのまま渡してください。") from None
    return unit_id, locator, citation or {}


def _read_args(hit: dict, scope: str, space_id: str) -> dict:
    target = hit.get('target_kind')
    # Files units live in the shared media index: media_read reads them by unit_id.
    args: dict = {'target_kind': 'media' if target == 'files' else target, 'scope': scope}
    if target in {'media', 'files'}:
        args['unit_id'] = hit.get('unit_id')
    else:
        args['locator'] = hit.get('locator')
        args['citation'] = hit.get('citation') or {}
    if scope == 'space' and space_id:
        args['space_id'] = space_id
    return args


def _scope_description(scope: str, project_name: str | None, character_name: str | None) -> str:
    project = f"プロジェクト「{project_name}」" if project_name else "プロジェクト未選択の個人範囲"
    if scope == 'current_context':
        who = f"・キャラクター「{character_name}」" if character_name else ""
        return f"現在の会話の{project}{who}"
    if scope == 'project':
        return f"{project}内の全キャラクター"
    if scope == 'space':
        return "指定Space内の閲覧可能な全プロジェクト"
    return "閲覧権限のある全プロジェクトと個人資料"


def _model_search_payload(projected: dict, raw: dict, *, scope: str, space_id: str,
                          searched_scope: dict, strict_project_scope: bool,
                          files_index: dict | None = None) -> dict:
    """Compact the API projection for a small model context window.

    ``projected`` and ``raw['results']`` are the same ordered hits; the raw
    engine response additionally carries the optional ``similarity`` and
    ``weak_match`` signals which are passed through verbatim when present.
    """

    hits = list(projected.get('results') or [])
    raw_hits = list(raw.get('results') or [])
    results = []
    for index, hit in enumerate(hits[:MODEL_RESULT_LIMIT]):
        raw_hit = raw_hits[index] if index < len(raw_hits) and isinstance(raw_hits[index], dict) else {}
        text = str(hit.get('text') or '')
        item = {
            'rank': index + 1,
            'target_kind': hit.get('target_kind'),
            'title': hit.get('title'),
            'snippet': text[:MODEL_SNIPPET_CHARS],
            'score': round(float(hit.get('score') or 0), 4),
        }
        if len(text) > MODEL_SNIPPET_CHARS:
            item['snippet_truncated'] = True
        similarity = raw_hit.get('similarity', hit.get('similarity'))
        if similarity is not None:
            item['similarity'] = similarity
        if hit.get('modality') and hit.get('modality') != 'text':
            item['modality'] = hit['modality']
        for key in ('source_kind', 'source_id', *_POSITION_KEYS):
            if hit.get(key) is not None:
                item[key] = hit[key]
        if hit.get('files_location'):
            item['files_location'] = hit['files_location']
        if hit.get('other_locations'):
            item['other_locations'] = [
                {key: other.get(key) for key in ('target_kind', 'source_kind', 'title') if other.get(key)}
                for other in hit['other_locations'][:5] if isinstance(other, dict)]
        item['read_args'] = _read_args(hit, scope, space_id)
        clues = []
        for match in hit.get('matched_query_sources') or []:
            clue = match.get('source') if isinstance(match, dict) else None
            if isinstance(clue, str) and clue not in clues:
                clues.append(clue)
        if clues:
            item['matched_query_clues'] = clues
        results.append(item)
    total = len(hits)
    # Only the engine's own signal is relayed; the API model's default for a
    # response without the signal is not evidence of a weak match.
    weak_match = raw.get('weak_match')
    raw_lanes = raw.get('lane_status') if isinstance(raw.get('lane_status'), dict) else {}
    payload = {
        'success': True,
        'complete': projected.get('complete', False),
        'searched_scope': searched_scope,
        'next_step': _SEARCH_NEXT_STEP if results else _SEARCH_EMPTY_NEXT_STEP,
        'warnings': projected.get('warnings') or [],
        'total_count': total,
        'returned_count': len(results),
        'truncated': total > len(results),
        'results': results,
        'lane_status': [
            {'target_kind': lane.get('target_kind'), 'status': lane.get('status'),
             'partitions': len(lane.get('partitions') or []),
             **({'weak_match': raw_lanes[lane.get('target_kind')]['weak_match']}
                if isinstance(raw_lanes.get(lane.get('target_kind')), dict)
                and 'weak_match' in raw_lanes[lane.get('target_kind')] else {}),
             **({'failed_reasons': sorted({p['reason'] for p in lane.get('partitions') or []
                                            if isinstance(p, dict) and p.get('reason')})}
                if any(isinstance(p, dict) and p.get('reason') for p in lane.get('partitions') or []) else {})}
            for lane in projected.get('lane_status') or []],
        'query_sources': [
            {key: source.get(key) for key in ('source', 'status', 'reason', 'total_units', 'processed_units')
             if source.get(key) is not None}
            for source in projected.get('query_sources') or []],
    }
    if weak_match is not None:
        payload['weak_match'] = weak_match
    if files_index is not None:
        payload['files_index'] = files_index
        if not files_index.get('enabled', True):
            payload['warnings'] = [*payload['warnings'], 'files_index_disabled']
            payload['files_fallback_note'] = _files_fallback_note()
        elif not files_index.get('complete', False):
            payload['warnings'] = [*payload['warnings'], 'files_index_incomplete']
            payload['files_fallback_note'] = _files_fallback_note()
    if payload['truncated']:
        payload['truncated_note'] = (
            f"上位{len(results)}件のみ表示（全{total}件、snippetは各{MODEL_SNIPPET_CHARS}字まで）。"
            "全件はUIの検索画面で確認できる。")
    if not results or weak_match:
        if not results:
            empty_note = (
                f"検索範囲（{searched_scope['description']}）では返却候補がありません。"
                "read_argsを持つ候補がないためmedia_readは呼び出さず、"
                "complete・warnings・lane_status・query_sourcesを確認してください。"
                "検索が未完了またはエラーなら該当なしと断定せず、その理由を伝えてください。"
            )
        else:
            empty_note = ""
        verification = (
            "回答に用いる関連度の高い候補をまず数件選び、各results[].read_argsをそのままmedia_readへ渡して"
            "原本を確認してから回答してください。media_readが成功して内容が依頼に該当しなければ、"
            "該当資料は見つからなかったと正直に伝えてください。読込失敗・権限拒否・原本不在なら、"
            "原本を確認できなかったことと返された理由を伝え、該当なしとは断定しないでください。"
            "weak_match=falseでも全件が関連するとは限りません。"
        )
        if strict_project_scope:
            payload['scope_note'] = (
                (empty_note or verification) +
                ("" if empty_note else f"検索範囲（{searched_scope['description']}）では候補の確信度が低いです。") +
                "この会話は現在のプロジェクトに限定されているため範囲は広げられません。"
            )
        elif scope != 'all_readable':
            payload['scope_note'] = (
                (empty_note or verification) +
                ("" if empty_note else f"検索範囲（{searched_scope['description']}）では候補の確信度が低いです。") +
                "他のプロジェクトのDocs等も探すには scope='all_readable' で再検索できるとユーザーに提案してください。"
            )
        else:
            payload['scope_note'] = (
                (empty_note or verification) +
                ("" if empty_note else "閲覧可能な全範囲でも候補の確信度が低いです。")
            )
    return payload


async def _files_coverage(resolver, scope: str, space_id: str) -> dict:
    """Coverage of the Files roots this search could reach (never other owners)."""
    from ...services.workspace_file_index import coverage_summary, files_index_status

    try:
        if scope in {'current_context', 'project'}:
            project = resolver.scope.project_id
            status = await files_index_status(resolver.session, resolver.scope.user_id, project_id=project,
                                              include_user=project is None)
        elif scope == 'space':
            status = await files_index_status(resolver.session, resolver.scope.user_id, scope='project',
                                              space_id=uuid.UUID(space_id) if space_id else None)
        else:
            status = await files_index_status(resolver.session, resolver.scope.user_id)
        return coverage_summary(status)
    except Exception:  # noqa: BLE001 - coverage is advisory, never a search failure
        logger.debug("media_search files coverage unavailable", exc_info=True)
        return {'enabled': True, 'complete': False, 'status': 'coverage_unavailable'}


async def _with_resolver(operation, space_id=None, *, read_scope=None, project_id=None):
    from ...memory.database import get_database_manager
    from ...services.search_sources import SearchSourceResolver, SearchScope, SourceUnavailable

    try:
        user_id, _ = _user_context()
    except PermissionError as exc:
        raise MediaUserRequired(str(exc)) from exc
    if user_id is None:
        raise MediaUserRequired("Authenticated media user required")
    turn = _active_turn()
    # ``current_context`` is bound below from the persisted conversation. The
    # turn's UI project is metadata only and may be stale; using it before the
    # saved row is loaded would let a project override widen a personal scope.
    current_project = (None if read_scope == 'current_context'
                       else _project_scope(turn.project_id if turn else None))
    requested_project = uuid.UUID(project_id) if project_id else None
    selected_space = uuid.UUID(space_id) if space_id else None
    selected_project = current_project
    if read_scope is not None:
        if read_scope not in {'current_context', 'project', 'space', 'all_readable'}:
            raise ValueError('Invalid result read scope')
        if read_scope == 'current_context' and (turn is None or not turn.session_id):
            raise ConversationContextRequired('Current context requires an authenticated conversation')
        if turn and turn.strict_project_scope:
            if read_scope not in {'current_context', 'project'}:
                raise PermissionError('Result read exceeds strict project scope')
            if read_scope != 'current_context' and requested_project not in {None, current_project}:
                raise PermissionError('Result read exceeds strict project scope')
        if read_scope == 'all_readable':
            if requested_project or selected_space:
                raise ValueError('All-readable result reads have no partition override')
            selected_project = None
        elif read_scope == 'space':
            if not selected_space or requested_project:
                raise ValueError('Space result reads require only a Space')
            selected_project = None
        elif read_scope == 'project':
            selected_project = requested_project or current_project
            if selected_project is None:
                raise ValueError('Project result reads require a Project')
        elif selected_space is not None:
            raise PermissionError('Current-context result read cannot change Space')
    db = get_database_manager()
    session = await db.get_session()
    try:
        current_character = None
        if read_scope == 'current_context':
            try:
                session_uuid = uuid.UUID(str(turn.session_id))
            except (ValueError, TypeError, AttributeError):
                raise ConversationContextRequired('Current context requires a valid saved conversation') from None
            # Resolve the saved conversation under the authenticated actor
            # before constructing the restricted scope. This rechecks the
            # live Project/participant ACL and makes the persisted row the
            # source of truth when the turn's selected Project is stale.
            saved_resolver = SearchSourceResolver(session, SearchScope(user_id))
            conversation = await saved_resolver._conversation(session_uuid)
            try:
                selected_project = (uuid.UUID(str(conversation.project_id))
                                    if conversation.project_id is not None else None)
            except (ValueError, TypeError, AttributeError):
                raise SourceUnavailable('Conversation unavailable') from None
            current_character = str(getattr(conversation, 'character_name', '') or '').strip()
            if not current_character:
                raise SourceUnavailable('Conversation unavailable')
            if requested_project is not None and requested_project != selected_project:
                raise PermissionError('Current-context result read cannot change Project')
        resolver = SearchSourceResolver(
                session,
                SearchScope(
                    user_id, selected_project, selected_space,
                    project_scope_restricted=read_scope == 'current_context',
                    character_name=current_character,
                    character_scope_restricted=read_scope == 'current_context',
                ),
            )
        actor = await resolver._actor()
        if selected_project is not None:
            await resolver._project(selected_project)
        elif selected_space is not None:
            from ...services.space_access import get_readable_space
            if await get_readable_space(session, space_id=selected_space, user_id=user_id,
                                        user_info={'role': actor.role}) is None:
                raise PermissionError('Result read Space is unavailable')
        return await operation(resolver)
    finally:
        await session.close()


@tool
def media_search(
    query: str = "",
    query_source_kind: str = "",
    query_source_id: str = "",
    query_page: int = 1,
    top_n: int = 10,
    space_id: str = "",
    use_current_attachments: bool = False,
    attachment_indices: list[int] | None = None,
    sources: list[dict] | None = None,
    mode: str = "combined",
    scope: str = "current_context",
    targets: list[str] | None = None,
) -> str:
    """テキストのqueryでローカルのDocs・Knowledge・過去の会話・添付media・User/Project Filesを意味検索する。

    「ローカル資料/過去チャット/Docs/ファイルの内容を調べて」はまずこれを使う（Web検索ではない）。
    内容・意味で探すときはこれ（targetsにfilesでUser Files/Project Filesの本文・画像・PDF・音声・動画・メール・Office）。
    ファイル名・パス・正確な語句が分かるときはsearch_files/bm25_searchの文字列検索を使う。
    files_index.complete=falseやfiles_fallback_noteがあればFiles索引が未完了なので、0件でも該当なしと断定せず
    search_files/bm25_searchでも確認する。filesの結果はfiles_location（scope/project_id/path）でFiles画面の場所を示す。
    原本確認は各結果のread_argsをそのままmedia_readへ渡してから回答する。
    targets/sources/attachment_indicesは配列で渡す。targets省略時はdocs/knowledge/conversation/media/filesの全部。
    error時はmessageの理由をそのまま伝え、0件と説明しない。
    resultsは関連しそうな候補であり、weak_matchの値にかかわらず回答に用いる関連度の高い数件のread_argsを
    そのままmedia_readへ渡して原本を確認してから回答する。media_readが成功して内容が依頼に該当しなければ
    「該当資料は見つからなかった」と正直に伝える。読込失敗・権限拒否・原本不在なら原本を確認できなかったことと
    理由を伝え、該当なしとは断定せず、候補snippetや無関係な結果だけを根拠にしない。
    既定scope=current_contextは認可済みの現在会話のプロジェクト/キャラクターだけ（プロジェクト会話では自分の
    User Filesを含まない）。searched_scopeを確認し、個人のUser Filesや他プロジェクトの資料が必要なら
    scope='all_readable'での再検索を提案する（scope_note参照）。
    モデル向け結果は上位8件・snippet300字まで（total_count/truncated参照）。

    「この画像と音声を手がかりに探して」にはuse_current_attachments=Trueを使う。
    attachment_indices未指定は保存済み添付の全件。選択indexはchat_search_sourcesで確認。
    UUID/pathを推測せず、既存source参照はsourcesへ順序付きで渡す。
    combinedは手がかりの統合であり論理ANDではない。separateは個別に検索する。
    projectは現在プロジェクト内の合法な全キャラクター、space/all_readableは各範囲の合法な全partition。
    query_sourcesの処理範囲/状態とlane_status/complete/warningsを確認して回答する。
    未検索・部分検索・失敗を0件成功と説明しない。

    画像queryは既存の原本source_kind/source_idで指定。任意path/URLは受け付けない。
    source_kindはgeneration_output/generated_media/chat_attachment/task_attachment/docs_attachment/knowledge_document/meeting_audio/workspace_file。
    メールのtext_start/text_endは正規化した検索用ヘッダー・本文全体の0始まりUnicodeコードポイント範囲[start,end)。
    text_lengthはその全体の文字数。原MSG/EMLのbyte位置や検索snippetの長さではない。位置がない資料はnull。
    """

    async def search(resolver):
        from ...api.media_search_routes import MediaQueryPlan, MediaQuerySource, MediaUnifiedSearchResponse
        from ...services.chat_search_sources import (saved_attachment_sources, execute_query_plan,
            MAX_QUERY_ATTACHMENTS, QuerySourceBudgetExceeded)

        selected_sources = list(sources or [])
        current_refs = []
        if bool(query_source_kind) != bool(query_source_id):
            raise ValueError('Source requires both kind and id')
        if query_source_kind:
            selected_sources.append({'source_kind': query_source_kind, 'source_id': query_source_id,
                                     'selector': {'pages': [query_page]}})
        if attachment_indices is not None and not use_current_attachments:
            raise ValueError('Attachment selection requires the current saved message')
        if use_current_attachments:
            turn = _active_turn()
            values = await saved_attachment_sources(resolver,
                session_id=turn.session_id if turn else None,
                message_id=turn.message_id if turn else None, indices=attachment_indices)
            selected_sources.extend({'source_kind': s['ref'].kind, 'source_id': s['ref'].id} for s in values)
            current_refs = [s['ref'] for s in values]
        if len(selected_sources) > MAX_QUERY_ATTACHMENTS:
            raise QuerySourceBudgetExceeded('添付検索は12件までです。対象を選び直してください。')
        plan = MediaQueryPlan(instruction=query, sources=[MediaQuerySource.model_validate(s) for s in selected_sources],
            mode=mode, scope=scope, targets=targets or list(_DEFAULT_TARGETS), limit=top_n)
        if not query.strip() and not plan.sources:
            raise ValueError('A query or saved source is required')
        turn = _active_turn()
        if scope == 'current_context' and (turn is None or not turn.session_id):
            raise ConversationContextRequired('Current context requires an authenticated conversation')
        if turn and turn.strict_project_scope and scope not in {'project', 'current_context'}:
            raise PermissionError('Strict search requires project scope')
        conversation = await resolver._conversation(uuid.UUID(turn.session_id)) if turn and turn.session_id else None
        from ...services.chat_search_sources import project_unified_response
        strict = bool(turn and turn.strict_project_scope)
        character_name = conversation.character_name if conversation else None
        result = await execute_query_plan(resolver, plan.model_dump(mode='json'),
            current_session_id=turn.session_id if turn else None,
            character_name=character_name,
            current_source_refs=current_refs, selected_space_id=space_id,
            strict_project_scope=strict)
        projected = MediaUnifiedSearchResponse.model_validate(
            project_unified_response(result)).model_dump(mode='json')
        project_id = resolver.scope.project_id if scope in {'current_context', 'project'} else None
        project_name = None
        if project_id is not None:
            from ...memory.models import Project
            try:
                project = await resolver.session.get(Project, project_id)
            except Exception:  # noqa: BLE001 - the label is descriptive only
                logger.debug("media_search project label unavailable", exc_info=True)
                project = None
            project_name = getattr(project, 'name', None)
        searched_scope = {
            'scope': scope,
            'description': _scope_description(scope, project_name,
                                              character_name if scope == 'current_context' else None),
            'project_id': str(project_id) if project_id else None,
            'project_name': project_name,
            'space_id': (space_id or None) if scope == 'space' else None,
            'targets': list(plan.targets),
        }
        files_index = (await _files_coverage(resolver, scope, space_id)
                       if 'files' in plan.targets else None)
        return json.dumps(_model_search_payload(projected, result, scope=scope, space_id=space_id,
                                                searched_scope=searched_scope, strict_project_scope=strict,
                                                files_index=files_index),
                          ensure_ascii=False, default=str)

    try:
        return _run_async_in_thread(_with_resolver(search))
    except Exception as exc:
        return json.dumps(_media_search_failure(exc), ensure_ascii=False)


@tool
def chat_search_sources() -> str:
    """現在ターンの保存済み添付をindex/filename/kind/sizeで列挙。検索用UUID/pathを推測しない。"""
    async def enumerate_sources(resolver):
        from ...services.chat_search_sources import saved_attachment_sources
        turn = _active_turn()
        values = await saved_attachment_sources(resolver,
            session_id=turn.session_id if turn else None,
            message_id=turn.message_id if turn else None)
        return json.dumps({'success': True, 'attachments': [
            {k: s[k] for k in ['index', 'filename', 'kind', 'size_bytes']} for s in values]}, ensure_ascii=False)
    try:
        return _run_async_in_thread(_with_resolver(enumerate_sources))
    except Exception:
        return json.dumps({'success': False, 'error': 'saved_attachments_unavailable'}, ensure_ascii=False)


@tool
def media_read(unit_id: str = "", question: str = "", space_id: str = "",
               target_kind: str = "media", locator: dict | None = None,
               citation: dict | None = None, scope: str = "current_context",
               project_id: str = "") -> str:
    """media_search結果の原本をACL再検証して読む。引数はその結果のread_argsをそのまま渡す。

    media/files hitはtarget_kind='media'とunit_id（UUID）、Docs/Knowledge/会話は
    target_kind・locator・citation（object）とscopeを検索結果から改変せずに渡す。
    matched_query_cluesや'docs_node:<id>'のような独自文字列はlocatorにならない。
    失敗時はerror（invalid_locator/read_scope_not_permitted/media_original_unavailable等）とmessageを確認する。
    引用用OCR/ASRを保持。questionがあれば既存の画像/音声/動画認識経路へ
    認証済み原本区間を渡す（既存privacy設定を適用）。本文添付は直接読む。
    メールのtext_start/text_endは正規化した検索用ヘッダー・本文全体の0始まりUnicodeコードポイント範囲[start,end)。
    text_lengthはその全体の文字数。原MSG/EMLのbyte位置ではない。位置がない資料はnull。
    現在のreaderと認可で原本の引用範囲を取得。complete/warningsも確認する。
    scopeには検索で選んだ範囲を明示する。all_readableは現在の合法Project全体、
    spaceはspace_id内、projectはproject_id（省略は現在Project）内のhitを読む。
    strictな現在Projectの制約は広げない。今回添付のquery認可とは別のhit読込範囲。
    """

    try:
        valid_unit_id, valid_locator, valid_citation = _validated_read_arguments(
            target_kind, unit_id, locator, citation)
    except InvalidReadArguments as exc:
        return json.dumps(_media_read_failure(exc), ensure_ascii=False)

    async def read(resolver):
        if target_kind not in {'media', 'files'}:
            from ...services.chat_search_sources import read_canonical_evidence
            evidence = await read_canonical_evidence(resolver, target_kind, valid_locator, valid_citation)
            return json.dumps({'success': True, 'target_kind': target_kind, **evidence}, ensure_ascii=False)
        from ...rag.media_index import get_media_index_service

        source, chunk = await get_media_index_service().read_original(
            resolver, valid_unit_id
        )
        result = {
            "success": True,
            "unit_id": unit_id,
            "source_kind": source.ref.kind,
            "source_id": source.ref.id,
            "title": source.title,
            "text": chunk.lexical_text,
            "original_url": source.metadata.get("original_url"),
            "page_number": chunk.metadata.get("page_number"),
            "start_seconds": chunk.metadata.get("start_seconds"),
            "end_seconds": chunk.metadata.get("end_seconds"),
            "text_start": chunk.metadata.get("text_start"),
            "text_end": chunk.metadata.get("text_end"),
            "text_length": chunk.metadata.get("text_length"),
            "session_id": source.metadata.get("session_id"),
            "message_id": source.metadata.get("message_id"),
            "modality": chunk.modality,
        }
        if source.metadata.get("files_location"):
            result["files_location"] = source.metadata["files_location"]
        if question and chunk.modality != "text":
            # Resolve again immediately before consuming original media bytes.
            await resolver.resolve(source.ref)
            from ...config import Config
            from ...services.media_recognition_service import MediaRecognitionService

            turn = _active_turn()
            recognizer = MediaRecognitionService(
                Config().config,
                usage_context=SimpleNamespace(
                    user_id=str(resolver.scope.user_id),
                    session_id=turn.session_id if turn else None,
                ),
            )
            if chunk.modality == "image":
                buffer = io.BytesIO()
                chunk.model_input["image"].save(buffer, format="PNG")
                raw = buffer.getvalue()
                mime = "image/png"
            elif chunk.modality == "audio":
                import numpy as np

                samples = np.asarray(chunk.model_input["audio"], dtype=np.float32)
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as handle:
                    handle.setnchannels(1)
                    handle.setsampwidth(2)
                    handle.setframerate(16000)
                    handle.writeframes(
                        (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
                    )
                raw = buffer.getvalue()
                mime = "audio/wav"
            else:
                raw = Path(chunk.model_input["video"]).read_bytes()
                mime = "video/x-matroska"
            media = {
                "name": source.title,
                "data": f"data:{mime};base64," + base64.b64encode(raw).decode(),
                "mime_type": mime,
            }
            if chunk.modality == "image":
                recognition = (await recognizer.recognize_images(question, [media]))[0]
            elif chunk.modality == "audio":
                recognition = await recognizer.recognize_audio(question, media)
            else:
                recognition = await recognizer.recognize_video(question, media)
            result.update(
                analysis=recognition.result, analysis_error=recognition.error or None
            )
        return json.dumps(result, ensure_ascii=False)

    try:
        return _run_async_in_thread(_with_resolver(read, space_id, read_scope=scope, project_id=project_id))
    except Exception as exc:
        return json.dumps(_media_read_failure(exc), ensure_ascii=False)
