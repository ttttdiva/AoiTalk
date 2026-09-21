"""Deterministic, evidence-first consolidation for Dreaming Memory.

The normal turn extractor is intentionally permissive because it produces a
reviewable candidate.  Consolidation is a different boundary: it reads old
conversation turns and may create durable user memory.  This module keeps the
model call replaceable while making the final acceptance decision entirely
deterministic.  In particular, assistant text is context only, quotes must be
verbatim user text, and every accepted operation is corroborated by at least
two independent conversation sessions.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)


CONSOLIDATION_SYSTEM_PROMPT = """You maintain AoiTalk Dreaming Memory.
Return only a JSON array. Do not emit Markdown, a code fence, or commentary.
Only user messages are evidence. Assistant messages are context and must never
be quoted or used as evidence. Produce only durable, cross-session user facts,
preferences, constraints, workflows, relationships, or instructions.
Never emit delete/delete_all operations, secrets, sensitive personal data,
temporary observations, project/client/repository facts, or one-turn requests.
An item is valid only when every exact quote occurs in user messages. An item
seen in one session is a reviewable candidate; an item corroborated by at
least two different sessions may be active. Every item must contain:
operation (upsert), content (starting with 'The user '), memory_type, title,
confidence, importance, evidence, scope ('user'), and corroborated (derived
server-side). Return [] when no item is safe.
"""


CONSOLIDATION_PROMPT = """Consolidate durable user memories from the following turns.
Existing memories are shown only to avoid restating an identical item.

Existing memories:
{existing}

Turns (assistant text is context only; never use it as evidence):
{turns}

Return a JSON array with the exact schema described by the system message.
"""


_TRANSIENT_RE = re.compile(
    r"(?:\btoday\b|\btonight\b|\bcurrently\b|\bcurrent\b|\bnow\b|"
    r"\blatest\b|\bfor now\b|\bthis time\b|\bright now\b|"
    r"今日|今夜|現在|最新|いま|今の|今回だけ|この返答|この回答|"
    r"weather|forecast|news|headline|stock price|exchange rate|traffic|"
    r"天気|予報|ニュース|速報|株価|為替|交通情報)",
    re.IGNORECASE,
)
_PROJECT_RE = re.compile(
    r"(?:\bthis (?:project|repository|repo|client|incident|case)\b|"
    r"\bthe (?:project|repository|repo|client|incident)\b|"
    r"この(?:案件|プロジェクト|リポジトリ|レポジトリ|顧客|クライアント|障害)|"
    r"当該(?:案件|プロジェクト|障害))",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(?:password|passwd|secret|api[_ -]?key|bearer\s+[a-z0-9._~+/=-]{8,}|"
    r"authorization\s*[:=]|token\s*[:=]|秘密鍵|パスワード\s*[:：])",
    re.IGNORECASE,
)
_SENSITIVE_RE = re.compile(
    r"(?:\b\d{3}-\d{2}-\d{4}\b|\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b|"
    r"\b\d{10,}\b|住所|電話番号|生年月日|social security)",
    re.IGNORECASE,
)
_USER_PREFIX_RE = re.compile(r"^(?:the\s+user|user|ユーザー|依頼者)\b", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class DreamingSourceTurn:
    """A paired user turn and its optional assistant response.

    ``assistant_message_id`` is retained solely to prove parent pairing and to
    provide context to a provider.  It is never accepted as evidence.
    """

    session_id: str
    user_message_id: str
    user_text: str
    user_created_at: datetime | None = None
    user_updated_at: datetime | None = None
    project_id: str | None = None
    assistant_message_id: str | None = None
    assistant_text: str = ""
    assistant_created_at: datetime | None = None
    deleted_at: datetime | None = None
    is_active_branch: bool | None = True
    # Effective outbound policy captured at source-scan time.  These fields
    # are metadata only; they let the worker partition history batches
    # without persisting raw conversation text in the run ledger.
    privacy_mode: str = "direct"
    privacy_context: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = _text(value)
    return text or None


def _normalise_space(value: str) -> str:
    return _SPACE_RE.sub(" ", value.strip())


def _normalise_key(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"\bthe\s+user\b|\buser\b|ユーザー|依頼者", "", value)
    return re.sub(r"[^0-9a-zぁ-んァ-ン一-龥]+", "", value)


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _coerce_turn(value: Any) -> DreamingSourceTurn | None:
    if isinstance(value, DreamingSourceTurn):
        return value
    session_id = _text(_value(value, "session_id"))
    message_id = _text(
        _value(value, "user_message_id", _value(value, "message_id"))
    )
    user_text = _text(_value(value, "user_text", _value(value, "content")))
    if not session_id or not message_id or not user_text:
        return None
    return DreamingSourceTurn(
        session_id=session_id,
        user_message_id=message_id,
        user_text=user_text,
        user_created_at=_coerce_datetime(_value(value, "user_created_at", _value(value, "created_at"))),
        user_updated_at=_coerce_datetime(_value(value, "user_updated_at", _value(value, "updated_at"))),
        project_id=_text(_value(value, "project_id")) or None,
        assistant_message_id=_text(_value(value, "assistant_message_id")) or None,
        assistant_text=_text(_value(value, "assistant_text")),
        assistant_created_at=_coerce_datetime(_value(value, "assistant_created_at")),
        deleted_at=_coerce_datetime(_value(value, "deleted_at")),
        is_active_branch=_value(value, "is_active_branch", True),
        privacy_mode=_text(_value(value, "privacy_mode", "direct")).lower() or "direct",
        privacy_context=(
            dict(_value(value, "privacy_context"))
            if isinstance(_value(value, "privacy_context"), Mapping)
            else None
        ),
    )


class DreamingMemoryConsolidator:
    """Call an LLM for suggestions, then apply deterministic validation."""

    def __init__(
        self,
        *,
        system_prompt: str = CONSOLIDATION_SYSTEM_PROMPT,
        prompt_builder: Any | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.prompt_builder = prompt_builder
        self.last_parse_failed = False
        self.last_call_failed = False

    async def consolidate(
        self,
        *,
        user_id: str,
        source_turns: Sequence[DreamingSourceTurn | Mapping[str, Any]],
        existing_memories: Sequence[Mapping[str, Any] | Any] | None,
        llm_client: Any,
        privacy_context: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return only validated, corroborated user-memory operations.

        The method is deliberately side-effect free.  Persistence belongs to
        :class:`DreamingConsolidationService`, which routes every mutation
        through ``ScopedMemoryService``.
        """

        turns = [
            turn
            for raw in source_turns
            if (turn := _coerce_turn(raw))
            and turn.deleted_at is None
            and turn.is_active_branch is not False
        ]
        turns = self._dedupe_turns(turns)
        if not turns:
            return []
        if llm_client is None:
            return []

        # A single provider request must never mix privacy modes.  The
        # historical source loader is expected to partition batches; fail
        # closed if a caller bypasses that boundary.
        modes = {
            str(turn.privacy_mode or "direct").strip().lower() or "direct"
            for turn in turns
        }
        if len(modes) > 1:
            raise ValueError("dreaming privacy modes must be partitioned")

        prompt = self._build_prompt(turns, existing_memories or [])
        prompt = await self._protect_prompt(
            prompt,
            llm_client=llm_client,
            privacy_context=privacy_context,
            mode=next(iter(modes), "direct"),
        )
        self.last_call_failed = False
        output = await self._call_llm(llm_client, prompt)
        parsed = self._parse_json_only(output)
        if parsed is None:
            self.last_parse_failed = True
            return []
        self.last_parse_failed = False
        if isinstance(parsed, Mapping):
            operations = parsed.get("operations", parsed.get("memories", parsed.get("items")))
        else:
            operations = parsed
        if not isinstance(operations, list):
            return []

        accepted: list[dict[str, Any]] = []
        for item in operations:
            candidate = self._validate_operation(item, turns, user_id=str(user_id))
            if candidate is not None:
                accepted.append(candidate)
        # Stable ordering makes retries and database digests independent from
        # provider item ordering.
        accepted.sort(key=lambda item: (str(item["dedupe_key"]), str(item["id"])))
        return accepted

    async def _protect_prompt(
        self,
        prompt: str,
        *,
        llm_client: Any,
        privacy_context: Mapping[str, Any] | None,
        mode: str,
    ) -> str:
        """Apply the existing outbound gateway before sending history to LLM."""
        context = dict(privacy_context or {})
        gateway = context.get("privacy_gateway") or context.get("gateway")
        if gateway is None:
            try:
                from ..services.outbound_privacy_service import OutboundPrivacyGateway

                session_context = dict(context.get("session_context") or {})
                session_context.setdefault("privacy_mode", mode)
                gateway = OutboundPrivacyGateway(
                    context.get("config"),
                    user_id=context.get("user_id"),
                    session_context=session_context,
                    project_metadata=context.get("project_metadata"),
                )
            except Exception:
                gateway = None
        if gateway is None:
            if mode in {"local_only", "protected"}:
                raise RuntimeError("privacy gateway is unavailable")
            return prompt

        provider = str(
            context.get("provider")
            or getattr(llm_client, "provider", None)
            or getattr(llm_client, "provider_id", None)
            or "openai_compatible_local"
        )
        base_url = context.get("base_url") or getattr(llm_client, "base_url", None)
        protect = getattr(gateway, "protect", None)
        if not callable(protect):
            if mode in {"local_only", "protected"}:
                raise RuntimeError("privacy gateway cannot protect history")
            return prompt
        result = protect(
            {"prompt": prompt},
            provider=provider,
            base_url=base_url,
            source_kind="dreaming_history",
            model=context.get("model") or getattr(llm_client, "model", None),
        )
        if inspect.isawaitable(result):
            result = await result
        payload = getattr(result, "payload", result)
        if isinstance(payload, Mapping):
            value = payload.get("prompt")
            if isinstance(value, str):
                return value
        if isinstance(payload, str):
            return payload
        if mode in {"local_only", "protected"}:
            raise RuntimeError("privacy gateway returned no protected history")
        return prompt

    @staticmethod
    def _dedupe_turns(turns: Iterable[DreamingSourceTurn]) -> list[DreamingSourceTurn]:
        seen: set[tuple[str, str]] = set()
        result: list[DreamingSourceTurn] = []
        for turn in sorted(
            turns,
            key=lambda item: (
                _iso(item.user_created_at) or "",
                str(item.session_id),
                str(item.user_message_id),
            ),
        ):
            key = (str(turn.session_id), str(turn.user_message_id))
            if key in seen:
                continue
            seen.add(key)
            result.append(turn)
        return result

    def _build_prompt(
        self,
        turns: Sequence[DreamingSourceTurn],
        existing_memories: Sequence[Mapping[str, Any] | Any],
    ) -> str:
        existing_lines: list[str] = []
        for memory in existing_memories:
            content = _text(_value(memory, "content"))
            if content:
                existing_lines.append(f"- {content[:500]}")
        turn_lines: list[str] = []
        for index, turn in enumerate(turns, start=1):
            # Delimiters make it difficult for a provider to mistake context
            # for an instruction and visibly distinguish assistant text.
            turn_lines.append(
                "\n".join(
                    (
                        f"TURN {index} session={turn.session_id} user_message={turn.user_message_id}",
                        f"USER EVIDENCE: {turn.user_text}",
                        f"ASSISTANT CONTEXT ONLY: {turn.assistant_text[:800]}",
                    )
                )
            )
        builder = self.prompt_builder
        if callable(builder):
            try:
                return str(builder(turns, existing_memories))
            except TypeError:
                return str(builder(turns=turns, existing_memories=existing_memories))
        return CONSOLIDATION_PROMPT.format(
            existing="\n".join(existing_lines) if existing_lines else "(none)",
            turns="\n\n".join(turn_lines),
        )

    async def _call_llm(self, llm_client: Any, prompt: str) -> str | None:
        """Use a side-effect-free consolidation method when available."""

        methods = (
            "generate_memory_consolidation_async",
            "consolidate_memories_async",
            "consolidate_memory_async",
            "generate_memory_extraction_async",
        )
        for method_name in methods:
            method = getattr(llm_client, method_name, None)
            if not callable(method):
                continue
            try:
                kwargs = {"system_prompt": self.system_prompt}
                try:
                    parameters = inspect.signature(method).parameters
                    if "system_prompt" not in parameters and not any(
                        parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters.values()
                    ):
                        kwargs = {}
                except (TypeError, ValueError):
                    pass
                result = method(prompt, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return self._stringify_provider_result(result)
            except Exception as exc:  # pragma: no cover - provider defensive path
                self.last_call_failed = True
                logger.warning("Dreaming consolidation LLM failed: %s", type(exc).__name__)
                return None

        callable_client = llm_client if callable(llm_client) else getattr(llm_client, "chat", None)
        if not callable(callable_client):
            self.last_call_failed = True
            return None
        try:
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ]
            try:
                result = callable_client(messages, temperature=0.0, max_tokens=1600)
            except TypeError:
                result = callable_client(prompt)
            if inspect.isawaitable(result):
                result = await result
            return self._stringify_provider_result(result)
        except Exception as exc:  # pragma: no cover - provider defensive path
            self.last_call_failed = True
            logger.warning("Dreaming consolidation chat failed: %s", type(exc).__name__)
            return None

    @staticmethod
    def _stringify_provider_result(result: Any) -> str | None:
        if result is None:
            return None
        if isinstance(result, str):
            return result
        if isinstance(result, (list, tuple, dict)):
            # Fakes often return an already-decoded JSON value.  Converting it
            # here still subjects it to the same strict shape validation.
            return json.dumps(result, ensure_ascii=False)
        if inspect.isgenerator(result):
            return "".join(str(part) for part in result)
        return str(result)

    @staticmethod
    def _parse_json_only(output: str | None) -> Any | None:
        if not isinstance(output, str):
            return None
        text = output.strip()
        if not text or "```" in text:
            return None
        try:
            return json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _validate_operation(
        self,
        item: Any,
        turns: Sequence[DreamingSourceTurn],
        *,
        user_id: str,
    ) -> dict[str, Any] | None:
        if not isinstance(item, Mapping):
            return None
        operation = _text(item.get("operation", item.get("action", item.get("op", "upsert")))).lower()
        if operation not in {"upsert", "update", "replace"}:
            return None  # consolidation has no delete semantics
        scope = _text(item.get("scope", item.get("scope_intent", "user"))).lower()
        if scope not in {"user", "global", "user_memory"}:
            return None
        if item.get("delete") or item.get("deleted") or item.get("forget"):
            return None

        content = _normalise_space(_text(item.get("content")))
        if not content:
            return None
        if not _USER_PREFIX_RE.search(content):
            content = f"The user {content[0].lower() + content[1:] if content else content}"
        if _SECRET_RE.search(content) or _SENSITIVE_RE.search(content):
            return None
        quote = _text(
            item.get(
                "quote",
                item.get("evidence_quote", item.get("evidence_span", item.get("evidence"))),
            )
        )
        evidence_input = item.get("evidence")
        if not quote and not isinstance(evidence_input, list):
            return None
        if _SECRET_RE.search(quote) or _SENSITIVE_RE.search(quote):
            return None
        if str(item.get("sensitivity", "normal")).strip().lower() != "normal":
            return None
        if bool(item.get("transient")) or bool(item.get("is_transient")):
            return None
        if item.get("expires_at") is not None or _TRANSIENT_RE.search(f"{content}\n{quote}"):
            return None
        if _PROJECT_RE.search(f"{content}\n{quote}"):
            return None

        try:
            confidence = float(item.get("confidence", 0.0))
            importance = int(item.get("importance", 0))
        except (TypeError, ValueError):
            return None
        if confidence < 0.8 or importance < 6 or confidence > 1.0 or importance > 10:
            return None

        referenced_ids = item.get("source_message_ids", item.get("evidence_message_ids", []))
        if isinstance(referenced_ids, str):
            referenced_ids = [referenced_ids]
        if not isinstance(referenced_ids, Iterable):
            referenced_ids = []
        referenced = {str(value).strip() for value in referenced_ids if str(value).strip()}

        # A quote must be an exact substring of a user message.  We never
        # search assistant_text, even when a model supplies an assistant ID.
        evidence_refs: list[dict[str, Any]] = []
        if evidence_input is not None:
            if not isinstance(evidence_input, list) or not evidence_input:
                return None
            for raw_ref in evidence_input:
                if not isinstance(raw_ref, Mapping):
                    return None
                ref_message_id = _text(raw_ref.get("message_id", raw_ref.get("user_message_id")))
                ref_session_id = _text(raw_ref.get("session_id"))
                ref_quote = _text(raw_ref.get("quote", raw_ref.get("evidence_quote")))
                if not ref_message_id or not ref_session_id or not ref_quote:
                    return None
                match = next(
                    (
                        turn
                        for turn in turns
                        if turn.user_message_id == ref_message_id
                        and turn.session_id == ref_session_id
                        and ref_quote in turn.user_text
                    ),
                    None,
                )
                if match is None:
                    # In particular this rejects assistant IDs and quotes that
                    # only occur in assistant context.
                    return None
                evidence_refs.append(
                    {
                        "type": "conversation",
                        "session_id": match.session_id,
                        "message_id": match.user_message_id,
                        "quote": ref_quote,
                        "source_at": _iso(match.user_updated_at or match.user_created_at),
                        "created_at": _iso(match.user_created_at or match.user_updated_at),
                    }
                )
            matching_turns = [
                turn
                for turn in turns
                if any(
                    ref["session_id"] == turn.session_id
                    and ref["message_id"] == turn.user_message_id
                    for ref in evidence_refs
                )
            ]
            # The singular quote remains a compact public evidence hint.  The
            # list is authoritative when the model emits both fields.
            quote = _text(evidence_refs[0]["quote"])
        else:
            matching_turns = [turn for turn in turns if quote in turn.user_text]
            if referenced:
                matching_turns = [turn for turn in matching_turns if turn.user_message_id in referenced]
        if not matching_turns:
            return None
        # A project-associated turn cannot be silently downgraded into a
        # cross-project user memory.  Project auto-memory has its own ACL and
        # candidate path; the history consolidator remains user-only.
        if any(turn.project_id for turn in matching_turns):
            return None
        matching_sessions = sorted({turn.session_id for turn in matching_turns})
        corroborated = len(matching_sessions) >= 2
        # Provider flags can never promote a one-session item, but an explicit
        # false flag is useful for suppressing a provider's overconfident
        # multi-session claim during review.
        if item.get("corroborated") is False or item.get("verified") is False:
            corroborated = False

        source_ids = sorted({turn.user_message_id for turn in matching_turns})
        source_created_at = max(
            (
                turn.user_created_at
                for turn in matching_turns
                if turn.user_created_at
            ),
            default=None,
        )
        source_updated_at = max(
            (
                turn.user_updated_at or turn.user_created_at
                for turn in matching_turns
                if turn.user_updated_at or turn.user_created_at
            ),
            default=None,
        )
        memory_type = _text(item.get("memory_type", "fact")).lower()[:32] or "fact"
        title = _text(item.get("title"))[:200] or None
        dedupe_key = _stable_digest({"type": memory_type, "content": _normalise_key(content)})[:128]
        operation_id = _stable_digest(
            {
                "user_id": str(user_id),
                "dedupe_key": dedupe_key,
                "source_message_ids": source_ids,
            }
        )
        if not evidence_refs:
            evidence_refs = [
                {
                    "type": "conversation",
                    "session_id": turn.session_id,
                    "message_id": turn.user_message_id,
                    "quote": quote,
                    "source_at": _iso(turn.user_updated_at or turn.user_created_at),
                    "created_at": _iso(turn.user_created_at or turn.user_updated_at),
                }
                for turn in matching_turns
            ]
        active_eligible = corroborated and confidence >= 0.90 and importance >= 7
        status = "active" if active_eligible else "candidate"
        return {
            "id": operation_id,
            "operation_id": operation_id,
            "operation": operation,
            "action": operation,
            "memory_id": _text(item.get("memory_id")) or None,
            "content": content,
            "memory_type": memory_type,
            "title": title,
            "confidence": confidence,
            "importance": importance,
            "sensitivity": "normal",
            "transient": False,
            "scope": "user",
            "scope_intent": "user",
            "quote": quote,
            "evidence_span": quote,
            "source_message_ids": source_ids,
            "source_session_ids": matching_sessions,
            "evidence_refs": evidence_refs,
            "corroborated": corroborated,
            "verified": corroborated,
            "status": status,
            "source_at": _iso(source_created_at),
            "source_updated_at": _iso(source_updated_at),
            "evidence_max_created_at": _iso(source_created_at),
            "dedupe_key": dedupe_key,
        }


__all__ = [
    "CONSOLIDATION_PROMPT",
    "CONSOLIDATION_SYSTEM_PROMPT",
    "DreamingMemoryConsolidator",
    "DreamingSourceTurn",
]
