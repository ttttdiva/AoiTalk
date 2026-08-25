"""Ranking and short-lived trusted bindings for existing Inbox items."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from uuid import UUID

from itsdangerous import BadData, URLSafeTimedSerializer

from ..security_secret import auth_secret_required, resolve_auth_secret_env


INBOX_RESOLUTION_MAX_AGE_SECONDS = 15 * 60
_TOKEN_SALT = "aoitalk-inbox-item-resolution-v1"
_PROCESS_LOCAL_TOKEN_SECRET = secrets.token_urlsafe(32)
_NOISE = (
    "以前",
    "前に",
    "この前",
    "inbox",
    "追加していた",
    "追加した",
    "記載していた",
    "記載した",
    "保存していた",
    "保存した",
    "みたいな奴",
    "みたいなやつ",
    "について",
    "の件",
    "新しく",
    "こういう",
    "情報が分かって",
    "情報がわかって",
    "追記して",
    "更新して",
)
_TOKEN_RE = re.compile(
    r"[a-zA-Z0-9][a-zA-Z0-9._/-]*|"
    r"[\u30a0-\u30ffー]+|"
    r"[\u4e00-\u9fff々]+|"
    r"[\u3040-\u309f]+"
)
_STOP_TERMS = {
    "これ",
    "それ",
    "あれ",
    "こと",
    "もの",
    "やつ",
    "奴",
    "情報",
    "件",
    "追加",
    "記載",
    "保存",
    "更新",
    "追記",
    "分かって",
    "わかって",
    "いる",
    "いた",
    "して",
    "した",
}


@dataclass(frozen=True)
class InboxSearchCandidate:
    node_id: UUID
    project_id: UUID
    project_name: str
    title: str
    searchable_text: str
    updated_at: datetime | None = None


@dataclass(frozen=True)
class RankedInboxCandidate:
    candidate: InboxSearchCandidate
    score: int
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class InboxResolution:
    item_id: UUID
    project_id: UUID


def extract_inbox_query_terms(query: str) -> tuple[str, ...]:
    cleaned = str(query or "").casefold()
    for noise in _NOISE:
        cleaned = cleaned.replace(noise.casefold(), " ")
    terms: list[str] = []
    for value in _TOKEN_RE.findall(cleaned):
        term = value.strip("._/- ")
        if len(term) < 2 or term in _STOP_TERMS:
            continue
        if term not in terms:
            terms.append(term)
    return tuple(terms[:12])


def rank_inbox_candidates(
    query: str,
    candidates: Iterable[InboxSearchCandidate],
) -> list[RankedInboxCandidate]:
    terms = extract_inbox_query_terms(query)
    if not terms:
        return []
    ranked: list[RankedInboxCandidate] = []
    for candidate in candidates:
        title = candidate.title.casefold()
        haystack = f"{candidate.title}\n{candidate.searchable_text}".casefold()
        matched = tuple(term for term in terms if term in haystack)
        if not matched:
            continue
        score = sum(18 if term in title else 8 for term in matched)
        score += 6 * max(0, len(matched) - 1)
        if len(matched) == len(terms):
            score += 20
        ranked.append(
            RankedInboxCandidate(
                candidate=candidate,
                score=score,
                matched_terms=matched,
            )
        )
    return sorted(
        ranked,
        key=lambda item: (
            item.score,
            item.candidate.updated_at or datetime.min,
        ),
        reverse=True,
    )


def has_unique_inbox_match(
    ranked: list[RankedInboxCandidate],
    *,
    query: str,
) -> bool:
    if not ranked:
        return False
    term_count = len(extract_inbox_query_terms(query))
    minimum = max(18, term_count * 8)
    if ranked[0].score < minimum:
        return False
    return len(ranked) == 1 or ranked[0].score - ranked[1].score >= 12


def inbox_update_entry_key(
    *,
    item_id: UUID,
    update_text: str,
    message_ref: str | None,
    session_id: str | None,
) -> str:
    material = "\n".join(
        [
            str(item_id),
            str(message_ref or ""),
            str(session_id or ""),
            str(update_text or "").strip(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _serializer() -> URLSafeTimedSerializer:
    secret = resolve_auth_secret_env(("AOITALK_JWT_SECRET", "AUTH_SECRET"))
    if secret is None:
        if auth_secret_required():
            raise RuntimeError(
                "AOITALK_JWT_SECRET (or AUTH_SECRET) is required in Enterprise"
            )
        secret = _PROCESS_LOCAL_TOKEN_SECRET
    return URLSafeTimedSerializer(secret_key=secret, salt=_TOKEN_SALT)


def issue_inbox_resolution_token(
    *,
    user_id: UUID,
    item_id: UUID,
    project_id: UUID,
    session_id: str | None,
    message_ref: str | None,
) -> str:
    return _serializer().dumps(
        {
            "user_id": str(user_id),
            "item_id": str(item_id),
            "project_id": str(project_id),
            "session_id": str(session_id or ""),
            "message_ref": str(message_ref or ""),
        }
    )


def verify_inbox_resolution_token(
    token: str,
    *,
    user_id: UUID,
    session_id: str | None,
    message_ref: str | None,
) -> InboxResolution:
    try:
        payload = _serializer().loads(
            str(token or ""),
            max_age=INBOX_RESOLUTION_MAX_AGE_SECONDS,
        )
        if not isinstance(payload, dict):
            raise ValueError
        if payload.get("user_id") != str(user_id):
            raise ValueError
        if payload.get("session_id", "") != str(session_id or ""):
            raise ValueError
        if payload.get("message_ref", "") != str(message_ref or ""):
            raise ValueError
        return InboxResolution(
            item_id=UUID(str(payload["item_id"])),
            project_id=UUID(str(payload["project_id"])),
        )
    except (BadData, KeyError, TypeError, ValueError) as exc:
        raise PermissionError(
            "Inbox検索結果のresolution tokenが無効または期限切れです。"
            "もう一度inbox_search_itemsで対象を検索してください。"
        ) from exc
