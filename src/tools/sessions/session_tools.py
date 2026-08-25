"""チャットセッション横断参照ツール。

現在進行中の会話だけでは足りない時に、同じユーザーの他のチャット
セッションを一覧・読み出し・全文検索するための読み取り専用ツールを提供する。

``search_past_chats`` が過去会話の検索（意味検索と語句検索）を担い、
``list_chat_sessions`` / ``read_chat_session`` が「どんな会話が存在するか」
「その会話の実際の本文」を直接扱う。

メッセージ本文はアプリ層で暗号化されているため、必ず ORM の
``ConversationMessage.content`` プロパティ経由で読む（生 SQL では読めない）。
"""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..core import tool
from src.memory.conversation_repository import get_conversation_repository


# 一度に読み込むメッセージ・セッション数の安全上限
MAX_SESSION_LIST_LIMIT = 200
MAX_MESSAGE_LIMIT = 1000
MAX_SEARCH_RESULT_LIMIT = 200
MAX_SESSIONS_SCANNED = 200

# search_past_chats の mode 別の既定件数（統合前の各ツールの既定値を維持する）。
DEFAULT_SEMANTIC_SEARCH_LIMIT = 10
DEFAULT_TEXT_SEARCH_LIMIT = 30
EXCERPT_CONTEXT_CHARS = 200


def is_explicit_chat_session_reference(session_id: str) -> bool:
    """Return whether ``session_id`` was server-validated for this turn.

    The helper intentionally reads only the immutable TurnContext binding;
    client-provided labels/titles are never consulted.  Runtime registration
    uses it when Memory Search is disabled to keep the explicit-read path
    narrow while preserving ``read_chat_session``'s normal ACL check.
    """

    try:
        from src.services.turn_context import is_explicit_reference_in_turn

        return is_explicit_reference_in_turn("chat_session", session_id)
    except Exception:
        return False


def build_explicit_read_chat_session_tool():
    """Build a ``read_chat_session`` ToolDefinition for Memory Search OFF.

    The public tool name/schema remains unchanged.  Only the runtime wrapper
    adds the requirement that the requested ID is one of the ACL-validated
    ``@chat_session`` references bound to the current turn; the wrapped reader
    still performs the repository's user/session ACL check.
    """

    async def read_explicit_chat_session(
        session_id: str,
        limit: int = 200,
        offset: int = 0,
        order: str = "asc",
        include_metadata: bool = False,
    ) -> Dict[str, Any]:
        if not is_explicit_chat_session_reference(session_id):
            return {
                "success": False,
                "error": "Memory Search 無効時は、このturnでサーバー検証済みのチャット参照だけ読み出せます",
                "messages": [],
            }
        return await read_chat_session.execute_async(
            session_id=session_id,
            limit=limit,
            offset=offset,
            order=order,
            include_metadata=include_metadata,
        )

    read_explicit_chat_session.__name__ = "read_chat_session"
    read_explicit_chat_session.__doc__ = (
        "指定したチャットセッションの本文を読む読み取り専用ツール。"
        "Memory Search 無効時は、現在turnでサーバー検証済みの明示参照に限る。"
    )
    return replace(
        tool(read_explicit_chat_session),
        name="read_chat_session",
        owner="sessions",
        side_effect="none",
        risk="low",
        supports_parallel=False,
    )


# ─── ユーザーコンテキスト解決 ────────────────────────────────────────────


def _current_user_context() -> Tuple[Optional[str], bool]:
    """実行中ターンの (user_id, is_admin) を解決する。"""
    user_id: Optional[str] = None
    is_admin = False

    try:
        from src.tools.os_operations.tools import get_current_user_context

        context = get_current_user_context() or {}
        raw_user_id = context.get("user_id")
        if raw_user_id:
            user_id = str(raw_user_id)
        is_admin = bool(context.get("is_admin", False))
    except Exception:
        pass

    if not user_id:
        try:
            from src.services.turn_context import get_turn_context

            turn_user_id = getattr(get_turn_context(), "user_id", None)
            if turn_user_id:
                user_id = str(turn_user_id)
        except Exception:
            pass

    return user_id, is_admin


def _resolve_target_user(
    explicit_user_id: Optional[str] = None,
) -> Tuple[str, bool, Optional[str]]:
    """参照対象ユーザーを解決する。

    Returns:
        (target_user_id, is_admin, error_message)
    """
    current_user_id, is_admin = _current_user_context()
    if not current_user_id:
        current_user_id = "default_user"

    requested = str(explicit_user_id).strip() if explicit_user_id else ""
    if requested and requested != current_user_id:
        if not is_admin:
            return (
                current_user_id,
                is_admin,
                "他ユーザーのチャットセッションを参照する権限がありません",
            )
        return requested, is_admin, None

    return current_user_id, is_admin, None


# ─── 整形ヘルパー ────────────────────────────────────────────────────────


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


def _to_datetime(value: Any) -> Optional[datetime]:
    return value if isinstance(value, datetime) else None


def _session_summary(session: Any) -> Dict[str, Any]:
    """list_chat_sessions / read_chat_session 共通のセッション要約。"""
    project_id = getattr(session, "project_id", None)
    return {
        "session_id": str(getattr(session, "id", "") or ""),
        "title": getattr(session, "title", "") or "",
        "character_name": getattr(session, "character_name", None),
        "project_id": str(project_id) if project_id else None,
        "message_count": int(getattr(session, "message_count", 0) or 0),
        "session_start": _iso(getattr(session, "session_start", None)),
        "last_activity": _iso(getattr(session, "last_activity", None)),
        "is_active": bool(getattr(session, "is_active", False)),
        "is_group_chat": bool(getattr(session, "is_group_chat", False)),
    }


def _message_content(message: Any) -> str:
    """暗号化カラムを ORM プロパティ経由で復号して読む。"""
    try:
        return str(getattr(message, "content", "") or "")
    except Exception:
        return ""


def _message_payload(message: Any, include_metadata: bool = False) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "role": getattr(message, "role", None),
        "content": _message_content(message),
        "sender_display_name": getattr(message, "sender_display_name", None),
        "created_at": _iso(getattr(message, "created_at", None)),
    }
    if include_metadata:
        payload["message_id"] = str(getattr(message, "id", "") or "") or None
        payload["metadata"] = _public_metadata(
            getattr(message, "message_metadata", None) or {}
        )
        payload["token_count"] = getattr(message, "token_count", None)
    return payload


def _public_metadata(value: Any) -> Any:
    """provider 内部の推論内容を落としたメタデータを返す。"""
    try:
        from src.memory.models.conversations import _public_message_metadata

        return _public_message_metadata(value)
    except Exception:
        return value


def _query_terms(query: str) -> List[str]:
    """クエリを空白で分割し、全語 AND 一致に使う小文字語リストを返す。"""
    return [term for term in str(query or "").casefold().split() if term]


def _build_excerpt(content: str, terms: List[str]) -> str:
    """最初にヒットした語の前後を切り出した抜粋を返す。"""
    folded = content.casefold()
    positions = [folded.find(term) for term in terms]
    positions = [pos for pos in positions if pos >= 0]
    hit = min(positions) if positions else 0

    start = max(0, hit - EXCERPT_CONTEXT_CHARS)
    end = min(len(content), hit + EXCERPT_CONTEXT_CHARS)
    excerpt = content[start:end]
    if start > 0:
        excerpt = "…" + excerpt
    if end < len(content):
        excerpt = excerpt + "…"
    return excerpt


def _clamp(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


# ─── ツール本体 ──────────────────────────────────────────────────────────


@tool
async def list_chat_sessions(
    limit: int = 30,
    query: Optional[str] = None,
    project_id: Optional[str] = None,
    include_inactive: bool = True,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """今の会話以外のチャットセッションを新しい順に一覧する読み取り専用ツール。

    「前に別のチャットで話した件」「昨日の会話の続き」「あのセッションどれだっけ」
    のように、現在の会話コンテキストに無い過去のやり取りを探す必要が出たら、
    自分の判断でいつでも呼んでよい。まずこのツールで対象セッションを特定し、
    本文が必要なら read_chat_session、語句や話題で絞りたいなら
    search_past_chats に繋げる。search_past_chats が会話の断片を返すのに対し、
    こちらはセッションそのものの一覧を返す。

    Args:
        limit: 返すセッション数の上限（既定 30、最大 200）。
        query: タイトルの部分一致フィルタ（大文字小文字を区別しない）。
        project_id: 案件でフィルタする場合の案件ID。"" を渡すと案件未紐付けのみ。
        include_inactive: 終了済みセッションも含めるか（既定 True）。
        user_id: 管理者のみ指定可。他ユーザーのセッションを対象にする。
    """
    target_user_id, _is_admin, error = _resolve_target_user(user_id)
    if error:
        return {
            "success": False,
            "error": error,
            "sessions": [],
            "count": 0,
        }

    limit = _clamp(limit, 30, 1, MAX_SESSION_LIST_LIMIT)
    title_filter = str(query).strip().casefold() if query else ""
    # タイトル絞り込みは Python 側で行うため、多めに取得してから切り詰める。
    fetch_limit = min(max(limit * 5, 100), 500) if title_filter else limit

    try:
        repository = get_conversation_repository()
        sessions = await repository.get_user_sessions(
            target_user_id,
            limit=fetch_limit,
            include_inactive=bool(include_inactive),
            project_id=project_id,
        )
    except Exception as exc:
        return {
            "success": False,
            "error": f"チャットセッション一覧の取得に失敗しました: {exc}",
            "sessions": [],
            "count": 0,
            "resolved_user_id": target_user_id,
        }

    summaries = [_session_summary(session) for session in sessions or []]
    if title_filter:
        summaries = [
            summary
            for summary in summaries
            if title_filter in str(summary.get("title") or "").casefold()
        ]
    summaries = summaries[:limit]

    total_session_count: Optional[int] = None
    try:
        total_session_count = int(
            await get_conversation_repository().count_user_sessions(target_user_id)
        )
    except Exception:
        total_session_count = None

    return {
        "success": True,
        "message": f"{len(summaries)}件のチャットセッションが見つかりました",
        "sessions": summaries,
        "count": len(summaries),
        "resolved_user_id": target_user_id,
        "total_session_count": total_session_count,
    }


@tool
async def read_chat_session(
    session_id: str,
    limit: int = 200,
    offset: int = 0,
    order: str = "asc",
    include_metadata: bool = False,
) -> Dict[str, Any]:
    """指定したチャットセッションの本文を読む読み取り専用ツール。

    list_chat_sessions や search_past_chats で見つけた session_id を渡して、
    その会話で実際に何が話されたかを確認する時に使う。現在の会話では分からない
    経緯・決定事項・約束を正確に引用したい場合は、要約に頼らずこのツールで
    原文を読むこと。返すのは現在のアクティブブランチのメッセージのみ。

    Args:
        session_id: 読み出すセッションのID。
        limit: 返すメッセージ数の上限（既定 200、最大 1000）。
        offset: 読み飛ばすメッセージ数（ページング用）。
        order: 並び順。"asc"（古い順・既定）または "desc"（新しい順）。
        include_metadata: True でメッセージIDやメタデータも返す。
    """
    resolved_session_id = str(session_id).strip() if session_id else ""
    if not resolved_session_id:
        return {
            "success": False,
            "error": "session_id が空です",
            "messages": [],
        }

    current_user_id, is_admin, error = _resolve_target_user(None)
    if error:
        return {"success": False, "error": error, "messages": []}

    limit = _clamp(limit, 200, 1, MAX_MESSAGE_LIMIT)
    offset = _clamp(offset, 0, 0, 10**6)
    descending = str(order or "asc").strip().casefold() == "desc"

    repository = get_conversation_repository()

    try:
        if not is_admin:
            allowed = await repository.user_has_session_access(
                resolved_session_id, current_user_id
            )
            if not allowed:
                return {
                    "success": False,
                    "error": "このチャットセッションを参照する権限がありません",
                    "messages": [],
                }

        session = await repository.get_session_by_id(resolved_session_id)
        if session is None:
            return {
                "success": False,
                "error": "チャットセッションが見つかりません",
                "messages": [],
            }

        messages = list(
            await repository.get_active_branch_messages(resolved_session_id) or []
        )
    except Exception as exc:
        return {
            "success": False,
            "error": f"チャットセッションの読み出しに失敗しました: {exc}",
            "messages": [],
        }

    total_message_count = len(messages)
    if descending:
        messages = list(reversed(messages))
    window = messages[offset : offset + limit]

    return {
        "success": True,
        "session": _session_summary(session),
        "messages": [
            _message_payload(message, include_metadata=include_metadata)
            for message in window
        ],
        "total_message_count": total_message_count,
        "returned_message_count": len(window),
        "offset": offset,
        "order": "desc" if descending else "asc",
        "has_more": offset + len(window) < total_message_count,
    }


async def _search_chat_history_text(
    query: str,
    limit: int = 30,
    session_id: Optional[str] = None,
    days: Optional[int] = None,
    max_sessions_scanned: int = 50,
) -> Dict[str, Any]:
    """過去チャット本文を語句で全文検索する実装（search_past_chats の text モード）。

    空白区切りの全ての語を含むメッセージだけを返す（大文字小文字は区別しない）。
    """
    terms = _query_terms(query)
    if not terms:
        return {
            "success": False,
            "error": "検索クエリが空です",
            "results": [],
            "count": 0,
        }

    current_user_id, is_admin, error = _resolve_target_user(None)
    if error:
        return {"success": False, "error": error, "results": [], "count": 0}

    limit = _clamp(limit, 30, 1, MAX_SEARCH_RESULT_LIMIT)
    max_sessions_scanned = _clamp(max_sessions_scanned, 50, 1, MAX_SESSIONS_SCANNED)
    cutoff: Optional[datetime] = None
    if days is not None:
        try:
            day_count = max(1, int(days))
            cutoff = datetime.utcnow() - timedelta(days=day_count)
        except (TypeError, ValueError):
            cutoff = None

    repository = get_conversation_repository()

    try:
        if session_id:
            resolved_session_id = str(session_id).strip()
            if not is_admin:
                allowed = await repository.user_has_session_access(
                    resolved_session_id, current_user_id
                )
                if not allowed:
                    return {
                        "success": False,
                        "error": "このチャットセッションを参照する権限がありません",
                        "results": [],
                        "count": 0,
                    }
            session = await repository.get_session_by_id(resolved_session_id)
            sessions = [session] if session is not None else []
        else:
            sessions = list(
                await repository.get_user_sessions(
                    current_user_id,
                    limit=max_sessions_scanned,
                    include_inactive=True,
                )
                or []
            )
    except Exception as exc:
        return {
            "success": False,
            "error": f"検索対象セッションの取得に失敗しました: {exc}",
            "results": [],
            "count": 0,
        }

    results: List[Dict[str, Any]] = []
    sessions_scanned = 0
    truncated = False

    for session in sessions[:max_sessions_scanned]:
        if truncated:
            break

        last_activity = _to_datetime(getattr(session, "last_activity", None))
        if cutoff is not None and last_activity is not None and last_activity < cutoff:
            continue

        current_session_id = str(getattr(session, "id", "") or "")
        if not current_session_id:
            continue

        try:
            messages = (
                await repository.get_active_branch_messages(current_session_id) or []
            )
        except Exception:
            continue

        sessions_scanned += 1
        session_title = getattr(session, "title", "") or ""

        for message in messages:
            created_at = _to_datetime(getattr(message, "created_at", None))
            if cutoff is not None and created_at is not None and created_at < cutoff:
                continue

            content = _message_content(message)
            if not content:
                continue
            folded = content.casefold()
            if not all(term in folded for term in terms):
                continue

            results.append(
                {
                    "session_id": current_session_id,
                    "session_title": session_title,
                    "role": getattr(message, "role", None),
                    "created_at": _iso(getattr(message, "created_at", None)),
                    "excerpt": _build_excerpt(content, terms),
                }
            )
            if len(results) >= limit:
                truncated = True
                break

    return {
        "success": True,
        "message": f"{len(results)}件のメッセージが一致しました",
        "results": results,
        "count": len(results),
        "sessions_scanned": sessions_scanned,
        "truncated": truncated,
        "resolved_user_id": current_user_id,
    }


@tool
async def search_past_chats(
    query: str,
    mode: str = "semantic",
    limit: Optional[int] = None,
    session_id: Optional[str] = None,
    days: Optional[int] = None,
    time_range: str = "all",
    max_sessions_scanned: int = 50,
) -> Dict[str, Any]:
    """過去の会話を検索する読み取り専用ツール。

    mode="semantic"（既定）は意味検索で、関連する過去会話やメモリの断片を返す。
    キーワードが一致しなくても話題・人物・決定事項から関連会話を拾えるので、
    ユーザーの好み・名前・以前の作業内容など現在の会話に無い文脈が必要になったら
    自分の判断でいつでも呼んでよい。毎ターン自動で添えられる抜粋で足りない場合の
    深掘りにも使う。
    mode="text" は会話本文の語句全文検索で、空白区切りの全ての語を含む
    メッセージだけを返す（大文字小文字は区別しない）。「以前この単語を含む
    会話があったはず」と語句がはっきりしている時に使う。
    どちらのモードもヒットに `session_id` が付く。断片では足りない場合は
    その session_id を `read_chat_session` に渡して会話本文を読むこと。
    存在するセッションを眺めたい場合は `list_chat_sessions` を使う。

    Args:
        query: 検索クエリ。semanticは話題を簡潔に、textは検索語を空白区切りで。
        mode: "semantic"（既定・意味検索）または "text"（語句の全文検索）。
        limit: 返す件数の上限（未指定なら semantic は 10、text は 30）。
        session_id: textモードのみ。指定したセッション内だけを検索する。
        days: textモードのみ。直近何日分に絞るか。未指定なら全期間。
        time_range: semanticモードのみ。"all"（既定）/"today"/"week"/"month" 等。
        max_sessions_scanned: textモードのみ。走査するセッション数の上限（既定 50）。
    """
    selected_mode = str(mode or "semantic").strip().casefold()
    if selected_mode not in {"semantic", "text"}:
        return {
            "success": False,
            "error": 'mode は "semantic" または "text" を指定してください',
            "results": [],
            "count": 0,
        }

    if selected_mode == "text":
        result = await _search_chat_history_text(
            query=query,
            limit=DEFAULT_TEXT_SEARCH_LIMIT if limit is None else limit,
            session_id=session_id,
            days=days,
            max_sessions_scanned=max_sessions_scanned,
        )
        result["mode"] = "text"
        return result

    from src.tools.memory.memory_tools import semantic_memory_search

    result = await semantic_memory_search(
        query=query,
        time_range=time_range,
        max_results=DEFAULT_SEMANTIC_SEARCH_LIMIT if limit is None else limit,
    )
    result["mode"] = "semantic"
    result["count"] = len(result.get("results") or [])
    return result
