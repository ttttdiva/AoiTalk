"""Untrusted-output contract for Resolution Knowledge Capture.

The curator returns a proposal, never an authority.  This module keeps the
proposal boundary deliberately independent from candidate models and routes so
the worker, tests, and future API adapters can all validate the same payload.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


RESOLUTION_KNOWLEDGE_SCHEMA_VERSION = "resolution-knowledge-v1"
KNOWLEDGE_KINDS = frozenset(
    {"troubleshooting", "procedure", "setup", "runbook", "decision_playbook", "lesson"}
)
PUBLICATION_ACTIONS = frozenset({"create", "update", "no_change"})
QUESTION_OPTION_IDS = frozenset({"a", "b", "c", "d", "other", "dont_save"})

MAX_TITLE_CHARS = 240
MAX_TEXT_CHARS = 4_000
MAX_SHORT_TEXT_CHARS = 1_000
MAX_LIST_ITEMS = 32
MAX_PROCEDURE_ITEMS = 24
MAX_OPTIONS = 4
MAX_SEMANTIC_KEY_CHARS = 240
MAX_BLOCKING_FACT_CHARS = 120
MAX_QUESTION_ROUNDS = 2

TOP_LEVEL_KEYS = frozenset(
    {"value", "semantic_key", "research", "draft", "questions", "publication"}
)
VALUE_KEYS = frozenset(
    {"worth_capturing", "reuse_score", "confidence", "reason", "knowledge_kind"}
)
RESEARCH_KEYS = frozenset({"sufficient", "missing_facts", "blocking_unknowns"})
DRAFT_KEYS = frozenset(
    {
        "schema_version",
        "title",
        "semantic_key",
        "knowledge_kind",
        "problem",
        "preconditions",
        "symptoms",
        "root_cause",
        "resolution",
        "procedure",
        "verification",
        "pitfalls",
        "environment_constraints",
        "known_uncertainty",
        "source_evidence_ids",
        "publication",
    }
)
PROCEDURE_KEYS = frozenset({"step", "text", "evidence_ids"})
FACT_KEYS = frozenset({"text", "evidence_ids"})
QUESTION_KEYS = frozenset(
    {"title", "message", "blocking_fact_key", "options", "evidence_ids"}
)
QUESTION_OPTION_KEYS = frozenset({"id", "label"})
PUBLICATION_KEYS = frozenset({"action", "target_node_id", "target_revision_id"})

_UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_SECRET_VALUE_RE = re.compile(
    r"""(?ix)(?:
        (?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|secret|credential|cookie|authorization)
        \s*(?:[:=]|is)\s*[^\s,;]+|
        bearer\s+[A-Za-z0-9._~+/=-]{12,}|
        -----BEGIN\s+(?:RSA |EC |OPENSSH )?PRIVATE KEY-----|
        (?:sk|ghp|xox[baprs]-)[A-Za-z0-9_-]{12,}
    )"""
)
_SENSITIVE_VALUE_RE = re.compile(
    r"""(?ix)(?:
        \b\d{3}-\d{2}-\d{4}\b|
        \b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b|
        \b(?:password|passwd|api[_ -]?key|secret|token)\b\s*[:=]
    )"""
)


class ResolutionKnowledgeValidationError(ValueError):
    """The curator proposal is not safe to use."""

    def __init__(self, message: str, *, field: str = "output") -> None:
        self.field = field
        super().__init__(f"{field}: {message}")


class SensitiveKnowledgeError(ResolutionKnowledgeValidationError):
    """A proposal contains material that must not become reusable knowledge."""


def _text(value: Any, *, field: str, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        if required:
            raise ResolutionKnowledgeValidationError("must be text", field=field)
        return ""
    value = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").strip()
    if any(ord(char) < 0x20 and char not in "\n\t" for char in value):
        raise ResolutionKnowledgeValidationError("contains control characters", field=field)
    if len(value) > limit:
        raise ResolutionKnowledgeValidationError("is too long", field=field)
    if required and not value:
        raise ResolutionKnowledgeValidationError("must not be empty", field=field)
    return value


def contains_secret_like_text(value: Any) -> bool:
    """Return true only for secret-like values, not for generic words."""

    return isinstance(value, str) and bool(_SECRET_VALUE_RE.search(value))


def redact_sensitive_text(value: Any, *, limit: int = MAX_TEXT_CHARS) -> str:
    """Redact common credential values while keeping safe procedure prose."""

    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n")
    normalized = _SECRET_VALUE_RE.sub("[REDACTED_SECRET]", normalized)
    return normalized[:limit].strip()


def _safe_knowledge_text(value: Any, *, field: str, limit: int) -> str:
    text = _text(value, field=field, limit=limit)
    if _SENSITIVE_VALUE_RE.search(text) or contains_secret_like_text(text):
        redacted = redact_sensitive_text(text, limit=limit)
        if not redacted or _SENSITIVE_VALUE_RE.search(redacted):
            raise SensitiveKnowledgeError("contains secret or sensitive value", field=field)
        return redacted
    return text


def normalize_semantic_key(value: Any) -> str:
    """Normalize a reusable concept identity without incident-specific IDs."""

    text = _text(
        value,
        field="semantic_key",
        limit=MAX_SEMANTIC_KEY_CHARS,
        required=False,
    )
    if not text:
        return ""
    if contains_secret_like_text(text) or _UUID_RE.search(text):
        raise ResolutionKnowledgeValidationError("contains a secret or incident UUID", field="semantic_key")
    if any(char in text for char in "\\/\x00"):
        raise ResolutionKnowledgeValidationError("contains a path-like value", field="semantic_key")
    normalized = " ".join(text.casefold().split())
    if not normalized or len(normalized) > MAX_SEMANTIC_KEY_CHARS:
        raise ResolutionKnowledgeValidationError("is empty or too long", field="semantic_key")
    if len(normalized) < 3:
        raise ResolutionKnowledgeValidationError("is too short", field="semantic_key")
    return normalized


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], *, field: str) -> None:
    actual = set(value)
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        detail = f"missing={missing!r}" if missing else f"unknown={extra!r}"
        raise ResolutionKnowledgeValidationError(detail, field=field)


def _bounded_string_list(value: Any, *, field: str, limit: int = MAX_LIST_ITEMS) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ResolutionKnowledgeValidationError("must be a bounded list", field=field)
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_safe_knowledge_text(item, field=f"{field}[{index}]", limit=MAX_SHORT_TEXT_CHARS))
    return result


def _score(value: Any, *, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResolutionKnowledgeValidationError("must be a finite number", field=field)
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ResolutionKnowledgeValidationError("is outside the allowed range", field=field)
    return number


def _id_list(
    value: Any,
    *,
    field: str,
    evidence_ids: set[str],
    required: bool = True,
    limit: int = MAX_LIST_ITEMS,
) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ResolutionKnowledgeValidationError("must be a bounded list", field=field)
    if required and not value:
        raise ResolutionKnowledgeValidationError("must cite evidence", field=field)
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ResolutionKnowledgeValidationError("must contain evidence IDs", field=f"{field}[{index}]")
        identifier = item.strip()
        if identifier not in evidence_ids:
            raise ResolutionKnowledgeValidationError("unknown evidence ID", field=f"{field}[{index}]")
        if identifier in result:
            raise ResolutionKnowledgeValidationError("duplicate evidence ID", field=field)
        result.append(identifier)
    return result


def _validate_publication(
    value: Any,
    *,
    allowed_targets: Mapping[str, Any],
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResolutionKnowledgeValidationError("must be an object", field=field)
    _exact_keys(value, PUBLICATION_KEYS, field=field)
    action = value.get("action")
    if action not in PUBLICATION_ACTIONS:
        raise ResolutionKnowledgeValidationError("invalid action", field=f"{field}.action")
    node_id = value.get("target_node_id")
    revision_id = value.get("target_revision_id")
    if node_id is not None and (not isinstance(node_id, str) or not node_id.strip()):
        raise ResolutionKnowledgeValidationError("invalid target node", field=f"{field}.target_node_id")
    if revision_id is not None and (not isinstance(revision_id, str) or not revision_id.strip()):
        raise ResolutionKnowledgeValidationError("invalid target revision", field=f"{field}.target_revision_id")
    node_id = node_id.strip() if isinstance(node_id, str) else None
    revision_id = revision_id.strip() if isinstance(revision_id, str) else None
    allowed = {str(key): value for key, value in (allowed_targets or {}).items()}
    if action == "create" and (node_id is not None or revision_id is not None):
        raise ResolutionKnowledgeValidationError("create cannot nominate a target", field=field)
    # ``no_change`` is also the safe empty publication for a discarded/low-
    # value result.  A non-empty no-change proposal may still name an
    # allowlisted node, but it is not required to do so.
    if action == "update" and node_id is None:
        raise ResolutionKnowledgeValidationError("target is required", field=field)
    if node_id is not None:
        if node_id not in allowed:
            raise ResolutionKnowledgeValidationError("target is outside server allowlist", field=field)
        expected_revision = allowed[node_id]
        if isinstance(expected_revision, Mapping):
            expected_revision = expected_revision.get("revision_id")
        if revision_id is not None and expected_revision not in (None, revision_id):
            raise ResolutionKnowledgeValidationError("target revision is stale or not allowlisted", field=field)
    return {
        "action": action,
        "target_node_id": node_id,
        "target_revision_id": revision_id,
    }


def _validate_question(
    value: Any,
    *,
    evidence_ids: set[str],
    blocking_unknowns: set[str],
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResolutionKnowledgeValidationError("must be an object", field=field)
    _exact_keys(value, QUESTION_KEYS, field=field)
    title = _safe_knowledge_text(value.get("title"), field=f"{field}.title", limit=MAX_TITLE_CHARS)
    message = _safe_knowledge_text(value.get("message"), field=f"{field}.message", limit=MAX_SHORT_TEXT_CHARS)
    blocking_key = _safe_knowledge_text(
        value.get("blocking_fact_key"), field=f"{field}.blocking_fact_key", limit=MAX_BLOCKING_FACT_CHARS
    )
    if blocking_key not in blocking_unknowns:
        raise ResolutionKnowledgeValidationError("does not match a declared blocking unknown", field=field)
    options = value.get("options")
    if not isinstance(options, list) or not 2 <= len(options) <= MAX_OPTIONS:
        raise ResolutionKnowledgeValidationError("must contain two to four options", field=f"{field}.options")
    normalized_options: list[dict[str, str]] = []
    seen_option_ids: set[str] = set()
    for index, option in enumerate(options):
        if not isinstance(option, Mapping):
            raise ResolutionKnowledgeValidationError("must be an object", field=f"{field}.options[{index}]")
        _exact_keys(option, QUESTION_OPTION_KEYS, field=f"{field}.options[{index}]")
        option_id = _safe_knowledge_text(option.get("id"), field=f"{field}.options[{index}].id", limit=40).casefold()
        if option_id not in QUESTION_OPTION_IDS or option_id in seen_option_ids:
            raise ResolutionKnowledgeValidationError("invalid or duplicate option ID", field=field)
        seen_option_ids.add(option_id)
        normalized_options.append(
            {
                "id": option_id,
                "label": _safe_knowledge_text(
                    option.get("label"), field=f"{field}.options[{index}].label", limit=MAX_SHORT_TEXT_CHARS
                ),
            }
        )
    return {
        "title": title,
        "message": message,
        "blocking_fact_key": blocking_key,
        "options": normalized_options,
        "evidence_ids": _id_list(value.get("evidence_ids"), field=f"{field}.evidence_ids", evidence_ids=evidence_ids),
    }


def is_authoritative_local_success_item(item: Mapping[str, Any] | None) -> bool:
    """Return whether one evidence row can prove a concrete local success.

    A closed Task, every TaskActivity, assistant/external/web prose, workspace
    file, conversation session, project memory, system/canonical Docs, or
    weak/web/advisory strength cannot prove that a specific local change
    worked.  Per-claim success is limited to user confirmation with
    authoritative strength, a user/human task comment or conversation
    message, or a current user/human Docs node or revision.  Procedure and
    verification items are checked independently.
    """

    if not isinstance(item, Mapping):
        return False
    kind = str(item.get("kind") or "").casefold()
    authorship = str(item.get("authorship") or "").casefold()
    strength = str(item.get("strength") or "").casefold()
    if authorship in {"assistant", "external", "web", "system", "canonical"} or strength in {"web", "weak", "advisory"}:
        return False
    if kind == "user_confirmation":
        return strength == "authoritative"
    if kind in {"task_comment", "conversation_message"} and authorship in {"user", "human"}:
        return True
    if kind in {"docs_node", "docs_revision"} and authorship in {"user", "human"}:
        return True
    return False


def _authoritative_local_success(evidence_registry: Mapping[str, Any], evidence_ids: Sequence[str]) -> bool:
    """Return whether cited IDs include concrete local success evidence."""

    for identifier in evidence_ids:
        if is_authoritative_local_success_item(evidence_registry.get(identifier)):
            return True
    return False


def validate_resolution_knowledge_output(
    value: Any,
    *,
    evidence_registry: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    allowed_publication_targets: Mapping[str, Any] | Sequence[Mapping[str, Any]] = (),
    question_rounds: int = 0,
    research_exhausted: bool = True,
) -> dict[str, Any]:
    """Validate and normalize one exact curator result.

    The returned mapping contains only the contract fields.  The input is
    never mutated and unknown provider fields are rejected rather than copied.
    """

    if not isinstance(value, Mapping):
        raise ResolutionKnowledgeValidationError("must be an object")
    _exact_keys(value, TOP_LEVEL_KEYS, field="output")
    if isinstance(evidence_registry, Mapping):
        registry = {str(key): item for key, item in evidence_registry.items()}
    else:
        registry = {}
        for item in evidence_registry:
            if isinstance(item, Mapping) and item.get("id"):
                registry[str(item["id"])] = item
    evidence_ids = set(registry)
    if isinstance(allowed_publication_targets, Mapping):
        targets = dict(allowed_publication_targets)
    else:
        targets = {
            str(item.get("node_id")): item
            for item in allowed_publication_targets
            if isinstance(item, Mapping) and item.get("node_id")
        }

    value_part = value.get("value")
    if not isinstance(value_part, Mapping):
        raise ResolutionKnowledgeValidationError("must be an object", field="value")
    _exact_keys(value_part, VALUE_KEYS, field="value")
    if not isinstance(value_part.get("worth_capturing"), bool):
        raise ResolutionKnowledgeValidationError("must be boolean", field="value.worth_capturing")
    raw_score = value_part.get("reuse_score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, int):
        raise ResolutionKnowledgeValidationError("must be an integer", field="value.reuse_score")
    if not 0 <= raw_score <= 100:
        raise ResolutionKnowledgeValidationError("must be between 0 and 100", field="value.reuse_score")
    confidence = _score(value_part.get("confidence"), field="value.confidence", minimum=0.0, maximum=1.0)
    reason = _safe_knowledge_text(value_part.get("reason"), field="value.reason", limit=MAX_SHORT_TEXT_CHARS)
    kind = value_part.get("knowledge_kind")
    if kind not in KNOWLEDGE_KINDS:
        raise ResolutionKnowledgeValidationError("invalid knowledge kind", field="value.knowledge_kind")

    semantic_key = normalize_semantic_key(value.get("semantic_key"))
    research = value.get("research")
    if not isinstance(research, Mapping):
        raise ResolutionKnowledgeValidationError("must be an object", field="research")
    _exact_keys(research, RESEARCH_KEYS, field="research")
    if not isinstance(research.get("sufficient"), bool):
        raise ResolutionKnowledgeValidationError("must be boolean", field="research.sufficient")
    missing_facts = set(_bounded_string_list(research.get("missing_facts"), field="research.missing_facts"))
    blocking_unknowns_list = _bounded_string_list(research.get("blocking_unknowns"), field="research.blocking_unknowns")
    blocking_unknowns = set(blocking_unknowns_list)

    raw_questions = value.get("questions")
    if not isinstance(raw_questions, list) or len(raw_questions) > 1:
        raise ResolutionKnowledgeValidationError("at most one question is allowed", field="questions")
    try:
        rounds = int(question_rounds)
    except (TypeError, ValueError) as exc:
        raise ResolutionKnowledgeValidationError("invalid question round", field="question_rounds") from exc
    if rounds < 0 or rounds > MAX_QUESTION_ROUNDS:
        raise ResolutionKnowledgeValidationError("question round limit exceeded", field="question_rounds")
    questions = [
        _validate_question(
            raw_questions[0],
            evidence_ids=evidence_ids,
            blocking_unknowns=blocking_unknowns,
            field="questions[0]",
        )
    ] if raw_questions else []
    if questions and (rounds >= MAX_QUESTION_ROUNDS or not research_exhausted):
        raise ResolutionKnowledgeValidationError("question is not allowed in this research round", field="questions")
    if questions and not value_part.get("worth_capturing"):
        raise ResolutionKnowledgeValidationError("low-value work cannot ask a question", field="questions")

    publication = _validate_publication(
        value.get("publication"), allowed_targets=targets, field="publication"
    )

    raw_draft = value.get("draft")
    draft: dict[str, Any] | None = None
    if raw_draft is not None:
        if not isinstance(raw_draft, Mapping):
            raise ResolutionKnowledgeValidationError("must be an object or null", field="draft")
        _exact_keys(raw_draft, DRAFT_KEYS, field="draft")
        if raw_draft.get("schema_version") != RESOLUTION_KNOWLEDGE_SCHEMA_VERSION:
            raise ResolutionKnowledgeValidationError("invalid schema version", field="draft.schema_version")
        draft_key = normalize_semantic_key(raw_draft.get("semantic_key"))
        if draft_key != semantic_key:
            raise ResolutionKnowledgeValidationError("does not match top-level semantic key", field="draft.semantic_key")
        if raw_draft.get("knowledge_kind") != kind:
            raise ResolutionKnowledgeValidationError("does not match value knowledge kind", field="draft.knowledge_kind")
        title = _safe_knowledge_text(raw_draft.get("title"), field="draft.title", limit=MAX_TITLE_CHARS)
        problem = _safe_knowledge_text(raw_draft.get("problem"), field="draft.problem", limit=MAX_TEXT_CHARS)
        preconditions = _bounded_string_list(raw_draft.get("preconditions"), field="draft.preconditions")
        symptoms = _bounded_string_list(raw_draft.get("symptoms"), field="draft.symptoms")
        root_cause_raw = raw_draft.get("root_cause")
        root_cause = None if root_cause_raw is None else _safe_knowledge_text(root_cause_raw, field="draft.root_cause", limit=MAX_TEXT_CHARS)
        resolution = _safe_knowledge_text(raw_draft.get("resolution"), field="draft.resolution", limit=MAX_TEXT_CHARS)
        procedure_raw = raw_draft.get("procedure")
        if not isinstance(procedure_raw, list) or len(procedure_raw) > MAX_PROCEDURE_ITEMS:
            raise ResolutionKnowledgeValidationError("must be a bounded list", field="draft.procedure")
        procedure: list[dict[str, Any]] = []
        for index, item in enumerate(procedure_raw):
            if not isinstance(item, Mapping):
                raise ResolutionKnowledgeValidationError("must be an object", field=f"draft.procedure[{index}]")
            _exact_keys(item, PROCEDURE_KEYS, field=f"draft.procedure[{index}]")
            step = item.get("step")
            if isinstance(step, bool) or not isinstance(step, int) or step != index + 1:
                raise ResolutionKnowledgeValidationError("steps must be consecutive integers", field=f"draft.procedure[{index}].step")
            procedure.append(
                {
                    "step": step,
                    "text": _safe_knowledge_text(item.get("text"), field=f"draft.procedure[{index}].text", limit=MAX_TEXT_CHARS),
                    "evidence_ids": _id_list(item.get("evidence_ids"), field=f"draft.procedure[{index}].evidence_ids", evidence_ids=evidence_ids),
                }
            )
        def fact_list(key: str) -> list[dict[str, Any]]:
            raw_items = raw_draft.get(key)
            if not isinstance(raw_items, list) or len(raw_items) > MAX_LIST_ITEMS:
                raise ResolutionKnowledgeValidationError("must be a bounded list", field=f"draft.{key}")
            normalized: list[dict[str, Any]] = []
            for index, item in enumerate(raw_items):
                if not isinstance(item, Mapping):
                    raise ResolutionKnowledgeValidationError("must be an object", field=f"draft.{key}[{index}]")
                _exact_keys(item, FACT_KEYS, field=f"draft.{key}[{index}]")
                normalized.append(
                    {
                        "text": _safe_knowledge_text(item.get("text"), field=f"draft.{key}[{index}].text", limit=MAX_TEXT_CHARS),
                        "evidence_ids": _id_list(item.get("evidence_ids"), field=f"draft.{key}[{index}].evidence_ids", evidence_ids=evidence_ids),
                    }
                )
            return normalized
        verification = fact_list("verification")
        pitfalls = fact_list("pitfalls")
        source_evidence_ids = _id_list(
            raw_draft.get("source_evidence_ids"),
            field="draft.source_evidence_ids",
            evidence_ids=evidence_ids,
            limit=MAX_LIST_ITEMS,
        )
        if not procedure or not verification:
            raise ResolutionKnowledgeValidationError(
                "procedure and verification each need strong local evidence",
                field="draft.procedure" if not procedure else "draft.verification",
            )
        for index, item in enumerate(procedure):
            if not _authoritative_local_success(registry, item["evidence_ids"]):
                raise ResolutionKnowledgeValidationError(
                    "procedure step needs strong local evidence",
                    field=f"draft.procedure[{index}].evidence_ids",
                )
        for index, item in enumerate(verification):
            if not _authoritative_local_success(registry, item["evidence_ids"]):
                raise ResolutionKnowledgeValidationError(
                    "verification needs strong local evidence",
                    field=f"draft.verification[{index}].evidence_ids",
                )
        draft_publication = _validate_publication(
            raw_draft.get("publication"), allowed_targets=targets, field="draft.publication"
        )
        if draft_publication != publication:
            raise ResolutionKnowledgeValidationError("does not match top-level publication", field="draft.publication")
        draft = {
            "schema_version": RESOLUTION_KNOWLEDGE_SCHEMA_VERSION,
            "title": title,
            "semantic_key": draft_key,
            "knowledge_kind": kind,
            "problem": problem,
            "preconditions": preconditions,
            "symptoms": symptoms,
            "root_cause": root_cause,
            "resolution": resolution,
            "procedure": procedure,
            "verification": verification,
            "pitfalls": pitfalls,
            "environment_constraints": _bounded_string_list(raw_draft.get("environment_constraints"), field="draft.environment_constraints"),
            "known_uncertainty": _bounded_string_list(raw_draft.get("known_uncertainty"), field="draft.known_uncertainty"),
            "source_evidence_ids": source_evidence_ids,
            "publication": draft_publication,
        }

    if value_part.get("worth_capturing") and not semantic_key:
        raise ResolutionKnowledgeValidationError("valuable output needs a semantic key", field="semantic_key")
    if value_part.get("worth_capturing") and not questions and draft is None:
        raise ResolutionKnowledgeValidationError("valuable output needs a draft or blocking question", field="draft")
    if not value_part.get("worth_capturing"):
        if raw_draft is not None or questions or semantic_key or publication["action"] != "no_change":
            raise ResolutionKnowledgeValidationError("low-value output must be empty/no_change", field="output")

    return {
        "value": {
            "worth_capturing": value_part["worth_capturing"],
            "reuse_score": raw_score,
            "confidence": confidence,
            "reason": reason,
            "knowledge_kind": kind,
        },
        "semantic_key": semantic_key,
        "research": {
            "sufficient": research["sufficient"],
            "missing_facts": sorted(missing_facts),
            "blocking_unknowns": blocking_unknowns_list,
        },
        "draft": draft,
        "questions": questions,
        "publication": publication,
    }


def evidence_digest(registry: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> str:
    """Return a deterministic digest of bounded evidence metadata."""

    if isinstance(registry, Mapping):
        values = list(registry.values())
    else:
        values = list(registry)
    safe = []
    for item in values:
        if isinstance(item, Mapping):
            safe.append(
                {
                    key: item.get(key)
                    for key in ("id", "kind", "source_id", "source_path", "project_id", "version", "content_hash", "relation", "strength", "authorship")
                }
            )
    payload = json.dumps(sorted(safe, key=lambda row: str(row.get("id"))), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResolutionKnowledgeContract:
    """Small adapter for callers that prefer an object over free functions."""

    evidence_registry: Mapping[str, Any]
    allowed_publication_targets: Mapping[str, Any] = None  # type: ignore[assignment]
    question_rounds: int = 0
    research_exhausted: bool = True

    def validate(self, value: Any) -> dict[str, Any]:
        return validate_resolution_knowledge_output(
            value,
            evidence_registry=self.evidence_registry,
            allowed_publication_targets=self.allowed_publication_targets or {},
            question_rounds=self.question_rounds,
            research_exhausted=self.research_exhausted,
        )


# Stable aliases for worker/API adapters that name the version explicitly.
validate_resolution_knowledge_v1 = validate_resolution_knowledge_output
normalize_resolution_knowledge_output = validate_resolution_knowledge_output


__all__ = [
    "DRAFT_KEYS",
    "KNOWLEDGE_KINDS",
    "MAX_QUESTION_ROUNDS",
    "PUBLICATION_ACTIONS",
    "QUESTION_KEYS",
    "RESOLUTION_KNOWLEDGE_SCHEMA_VERSION",
    "ResolutionKnowledgeContract",
    "ResolutionKnowledgeValidationError",
    "SensitiveKnowledgeError",
    "contains_secret_like_text",
    "evidence_digest",
    "is_authoritative_local_success_item",
    "normalize_semantic_key",
    "normalize_resolution_knowledge_output",
    "redact_sensitive_text",
    "validate_resolution_knowledge_output",
    "validate_resolution_knowledge_v1",
]
