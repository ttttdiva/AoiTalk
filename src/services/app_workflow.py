"""Privacy-aware App/Macro build workflow.

This module is intentionally a small workflow controller, not another App
service or model provider.  It keeps a turn-scoped protected context for the
problem projection sent to :class:`CloudAdvisorCoordinator`, then lets the
local implementation callback (or the deterministic fallback below) write to
the existing App workspace.  Cloud output is advisory data only; it is never
treated as a command or granted file/tool authority.

The service is useful in two modes:

* ``workspace=Path(...)`` is a pure filesystem mode used by tests and local
  preview tools.
* ``app_service`` + ``session`` + ``owner_user_id`` (or a supplied persistence
  callback) creates/uses a persistent App workspace.  Existing App APIs remain
  the authority for permissions, DB transactions, Git and job execution.

No raw source is included in the Cloud query.  Raw evidence and alias maps are
  held only by the short-lived ``AppProblemIR`` instance and are excluded from
  all serialisation methods.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

import yaml

from .app_storage import (
    AppStorageError,
    ensure_app_workspace,
    is_private_app_path,
    normalize_app_relative_path,
    resolve_app_file,
    resolve_workspace_file,
)

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 120_000
MAX_QUERY_CHARS = 24_000
MAX_ADVISORY_CHARS = 30_000
MAX_GENERATED_FILE_CHARS = 256_000
MAX_GENERATED_FILE_BYTES = 1_048_576
MAX_LIST_ITEMS = 32
MAX_ITEM_CHARS = 1_000
MAX_REPAIR_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Intent routing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppWorkflowIntent:
    """Result of deterministic App/Macro intent detection."""

    kind: str
    command: str | None = None
    confidence: float = 0.0
    reason: str = ""

    @property
    def is_macro(self) -> bool:
        return self.kind == "macro"

    @property
    def is_app(self) -> bool:
        return self.kind == "app"

    def __bool__(self) -> bool:
        return bool(self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "command": self.command,
            "confidence": self.confidence,
            "reason": self.reason,
        }


def parse_app_command(value: str | None) -> AppWorkflowIntent | None:
    """Parse the explicit ``/app`` and ``/macro`` commands.

    The command parser deliberately does not execute or expand the remainder
    of the command.  It is returned as metadata for the workflow controller.
    """

    text = str(value or "").strip()
    match = re.match(r"^/(app|macro)(?:\s|$)", text, flags=re.I)
    if not match:
        return None
    kind = match.group(1).casefold()
    return AppWorkflowIntent(
        kind=kind,
        # Keep only the canonical system token in public/audit metadata; the
        # free-form argument may contain credentials or customer facts.
        command=f"/{kind}",
        confidence=1.0,
        reason="explicit_slash_command",
    )


def detect_app_workflow_intent(
    text: str | None,
    *,
    attachments: Iterable[str] | Mapping[str, Any] | None = None,
) -> AppWorkflowIntent | None:
    """Detect representative natural-language App/Macro requests.

    This is intentionally conservative and deterministic.  It is a routing
    hint only; the selected backend is always :class:`AppBuildWorkflow`.
    """

    raw = str(text or "").strip()
    explicit = parse_app_command(raw)
    if explicit:
        return explicit
    lowered = raw.casefold()
    attachment_text = ""
    if isinstance(attachments, Mapping):
        attachment_text = " ".join(str(key) for key in attachments.keys())
    elif attachments is not None:
        attachment_text = " ".join(str(item) for item in attachments)
    corpus = f"{lowered} {attachment_text.casefold()}"

    macro_words = ("マクロ", "macro", "vba", "判定するマクロ")
    app_words = ("app", "アプリ", "アプリケーション", "業務アプリ")
    creation_words = (
        "作って",
        "作成",
        "生成",
        "実装",
        "create",
        "build",
        "make",
        "作りたい",
    )
    has_creation = any(word in corpus for word in creation_words)
    has_data = any(
        word in corpus
        for word in ("config", "設定", "log", "ログ", "判定", "正常", "異常", "ファイル")
    )
    def contains_term(word: str) -> bool:
        token = str(word).casefold()
        if re.fullmatch(r"[a-z0-9]+", token):
            return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", corpus) is not None
        return token in corpus

    if any(contains_term(word) for word in macro_words) and (has_creation or has_data):
        return AppWorkflowIntent(
            kind="macro",
            confidence=0.96 if has_creation else 0.84,
            reason="macro_request",
        )
    if any(contains_term(word) for word in app_words) and has_creation:
        return AppWorkflowIntent(
            kind="app",
            confidence=0.9,
            reason="app_creation_request",
        )
    # An attached config/log with an explicit classification request is a
    # useful macro hint even when the user omits the word "macro".
    if has_data and any(word in corpus for word in ("ok", "ng", "正常なら", "異常なら", "ステータス")):
        return AppWorkflowIntent(
            kind="macro",
            confidence=0.78,
            reason="classification_request",
        )
    return None


def detect_app_intent(text: str | None, *, attachments: Iterable[str] | Mapping[str, Any] | None = None) -> str | None:
    """Compatibility helper returning only ``"app"``/``"macro"``."""

    result = detect_app_workflow_intent(text, attachments=attachments)
    return result.kind if result else None


def automatic_app_intent(text: str | None, *, attachments: Iterable[str] | Mapping[str, Any] | None = None) -> AppWorkflowIntent | None:
    """Alias used by Chat routing code."""

    return detect_app_workflow_intent(text, attachments=attachments)


def build_app_problem_ir(**kwargs: Any) -> "AppProblemIR":
    """Functional adapter for callers that prefer a module-level builder."""

    return AppProblemIR.from_inputs(**kwargs)


def parse_app_problem(value: Mapping[str, Any]) -> "AppProblemIR":
    return AppProblemIR.from_dict(value)


# ---------------------------------------------------------------------------
# Local protected context and masking
# ---------------------------------------------------------------------------


_PRIVATE_IP_RE = re.compile(
    r"(?<![\w.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|127(?:\.\d{1,3}){3})(?![\w.])"
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_INTERNAL_URL_RE = re.compile(
    r"(?i)\bhttps?://(?:[^\s/@:]+(?::[^\s/@]*)?@)?(?:[A-Za-z0-9_-]+\.)*(?:internal|local|localhost|intranet|corp|lan)(?::\d+)?(?:/[^\s]*)?"
)
_WINDOWS_PATH_RE = re.compile(r"(?<![\w])(?:[A-Za-z]:\\|\\\\)[^\r\n\t\"']+")
_UNIX_PATH_RE = re.compile(r"(?<![\w])/(?:home|Users|var|tmp|opt|srv|mnt|workspace|work)/[^\r\n\t\"']+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:password|passwd|api[_ -]?key|access[_ -]?token|auth[_ -]?token|secret|credential|token)\s*[:=]\s*)([^\s,;]+)"
)
# Cloud advisory prose is untrusted data just like source evidence.  Keep a
# small, deliberately conservative detector for values that should never be
# copied into the public design/result projection even when the provider
# invents a new field/value that was not present in the input.
_ADVISORY_SENSITIVE_RE = re.compile(
    r"(?i)(?:secret|password|passwd|token|credential|api[_ -]?key|"
    r"customer|client|internal|private|confidential|fict|local[_ -]?path|"
    r"機密|極秘|秘密|顧客|社内|非公開)"
)
_ADVISORY_ID_RE = re.compile(r"(?i)\b[A-Z0-9]{3,}(?:[-_][A-Z0-9]{2,})+\b")
_EVIDENCE_TOKEN_RE = re.compile(r"[^\s,;|{}\[\]()<>\"']+")
_PROTECTED_MARKER_RE = re.compile(
    r"^(?:"
    r"<(?:VALUE|EVIDENCE|CUSTOMER|SECRET|INTERNAL_URL|EMAIL|PRIVATE_IP|LOCAL_PATH|INTERNAL_HOST|PROJECT_ID)_\d{1,6}>|"
    r"<(?:LOCAL_VALUE|ADVISORY_REDACTED|formula|secret|private-ip|internal-url|local-path|email)>|"
    r"\[AOI_(?:SECRET|EMAIL|INTERNAL_HOST|INTERNAL_URL|LOCAL_PATH|PRIVATE_IP|CONFIDENTIAL_TERM)_[0-9]+\]|"
    r"\[WF_VALUE_[A-F0-9]{8,64}\]"
    r")$",
    re.IGNORECASE,
)
_EVIDENCE_SAFE_TOKENS = frozenset(
    {
        "ok",
        "ng",
        "error",
        "critical",
        "fatal",
        "fail",
        "failed",
        "failure",
        "success",
        "successful",
        "healthy",
        "up",
        "down",
        "reachable",
        "unreachable",
        "info",
        "warn",
        "warning",
        "debug",
        "status",
        "state",
        "config",
        "log",
        "input",
        "output",
        "interface",
        "peer",
        "retry",
        "latency",
        "timeout",
        "true",
        "false",
        "none",
        "null",
        "正常",
        "異常",
        "成功",
        "失敗",
        "接続",
        "切断",
        "確認",
        "開始",
        "終了",
        "警告",
        "情報",
    }
)
_HOST_RE = re.compile(
    r"(?i)\b(?:[a-z0-9][a-z0-9-]{2,63}\.)+(?:corp|internal|local|lan|intranet)(?:\b|/)"
)
_CORP_NAME_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fffA-Za-z0-9・ー]{2,80}(?:株式会社|有限会社|合同会社)")
_CUSTOMER_LABEL_RE = re.compile(
    r"(?im)(?P<prefix>(?:顧客名|顧客|会社名|customer(?:\s+name)?|client(?:\s+name)?)[ \t]*[:：=][ \t]*)(?P<value>[^\r\n,;]+)"
)
_DEVICE_LABEL_RE = re.compile(
    r"(?im)(?P<prefix>(?:装置名|ホスト名|hostname|host(?:name)?|device(?:\s+name)?)[ \t]*[:：=][ \t]*)(?P<value>[^\r\n,;]+)"
)
_DEVICE_TOKEN_RE = re.compile(r"(?i)\b(?:fw|sw|router|switch|host|srv|db)-[a-z0-9][a-z0-9-]{3,63}\b")
_IDENTIFIER_LABEL_RE = re.compile(
    r"(?im)(?P<prefix>(?:顧客コード|案件コード|環境(?:名|ID)?|プロジェクト(?:名|ID)?|customer[_ -]?id|project[_ -]?id|environment[_ -]?id)[ \t]*[:：=][ \t]*)(?P<value>[^\r\n,;]+)"
)


def _looks_sensitive(value: str) -> bool:
    """Identify likely confidential literals for final Cloud assertions."""

    text = str(value or "")
    if not text:
        return False
    if any(pattern.search(text) for pattern in (_PRIVATE_IP_RE, _EMAIL_RE, _INTERNAL_URL_RE, _WINDOWS_PATH_RE, _UNIX_PATH_RE, _SECRET_ASSIGNMENT_RE, _HOST_RE, _CUSTOMER_LABEL_RE, _DEVICE_LABEL_RE, _DEVICE_TOKEN_RE, _IDENTIFIER_LABEL_RE)):
        return True
    lowered = text.casefold()
    return bool(_CORP_NAME_RE.search(text)) or any(token in lowered for token in ("password=", "api_key=", "access_token=", "fakeonly", "confidential", "機密", "顧客名:"))


class _ProtectedContext:
    """Ephemeral raw-to-alias map scoped to one workflow execution."""

    __slots__ = ("workflow_id", "_raw_to_alias", "_alias_to_raw", "_counts")

    def __init__(self, workflow_id: str):
        self.workflow_id = workflow_id
        self._raw_to_alias: dict[str, str] = {}
        self._alias_to_raw: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    @property
    def aliases(self) -> Mapping[str, str]:
        # Never expose raw strings as mapping keys.  This property is intended
        # only for safe diagnostics and therefore returns ordinal labels plus
        # opaque aliases; local rebinding uses ``restore`` internally.
        return {
            f"value_{index}": alias
            for index, alias in enumerate(self._alias_to_raw, start=1)
        }

    def alias_for(self, raw: str, category: str = "VALUE") -> str:
        value = str(raw)
        if not value:
            return value
        existing = self._raw_to_alias.get(value)
        if existing:
            return existing
        key = re.sub(r"[^A-Z0-9]+", "_", str(category).upper()).strip("_") or "VALUE"
        self._counts[key] = self._counts.get(key, 0) + 1
        alias = f"<{key}_{self._counts[key]:03d}>"
        self._raw_to_alias[value] = alias
        self._alias_to_raw[alias] = value
        return alias

    def mask(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.mask_text(value)
        if isinstance(value, Mapping):
            return {str(key): self.mask(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.mask(item) for item in value]
        if isinstance(value, tuple):
            return [self.mask(item) for item in value]
        return value

    def mask_text(self, text: str) -> str:
        result = str(text)
        # Known values first.  This catches synthetic customer names and
        # project identifiers which cannot be inferred reliably by regex.
        for raw in sorted(self._raw_to_alias, key=len, reverse=True):
            if raw and len(raw) >= 3:
                result = result.replace(raw, self._raw_to_alias[raw])

        def replace_secret(match: re.Match[str]) -> str:
            return f"{match.group(1)}{self.alias_for(match.group(2), 'SECRET')}"

        result = _SECRET_ASSIGNMENT_RE.sub(replace_secret, result)
        result = _INTERNAL_URL_RE.sub(lambda m: self.alias_for(m.group(0), "INTERNAL_URL"), result)
        result = _EMAIL_RE.sub(lambda m: self.alias_for(m.group(0), "EMAIL"), result)
        result = _PRIVATE_IP_RE.sub(lambda m: self.alias_for(m.group(0), "PRIVATE_IP"), result)
        result = _WINDOWS_PATH_RE.sub(lambda m: self.alias_for(m.group(0), "LOCAL_PATH"), result)
        result = _UNIX_PATH_RE.sub(lambda m: self.alias_for(m.group(0), "LOCAL_PATH"), result)
        result = _CUSTOMER_LABEL_RE.sub(
            lambda m: f"{m.group('prefix')}{self.alias_for(m.group('value').strip(), 'CUSTOMER')}",
            result,
        )
        result = _DEVICE_LABEL_RE.sub(
            lambda m: f"{m.group('prefix')}{self.alias_for(m.group('value').strip(), 'INTERNAL_HOST')}",
            result,
        )
        result = _IDENTIFIER_LABEL_RE.sub(
            lambda m: f"{m.group('prefix')}{self.alias_for(m.group('value').strip(), 'PROJECT_ID')}",
            result,
        )
        result = _DEVICE_TOKEN_RE.sub(lambda m: self.alias_for(m.group(0), "INTERNAL_HOST"), result)
        result = _CORP_NAME_RE.sub(lambda m: self.alias_for(m.group(0), "CUSTOMER"), result)
        result = _HOST_RE.sub(lambda m: self.alias_for(m.group(0), "INTERNAL_HOST"), result)
        return result

    def mask_evidence_text(self, text: str) -> str:
        """Mask unlabelled evidence while retaining safe status vocabulary.

        Regex-only masking cannot identify an arbitrary customer phrase or a
        proprietary token in a log.  Evidence is therefore tokenized after
        the known deterministic substitutions above; anything outside a
        small structural vocabulary is bound to a request-local opaque alias.
        This is intentionally lossy for unknown text, but it is fail-closed
        and keeps markers such as ``OK``/``ERROR`` useful to the advisor.
        """

        result = self.mask_text(text)
        if not result:
            return result
        result = re.sub(
            r"<([^<>]{1,96})>",
            lambda match: (
                match.group(0)
                if _PROTECTED_MARKER_RE.fullmatch(match.group(0))
                else self.alias_for(match.group(1), "EVIDENCE")
            ),
            result,
        )
        pieces: list[str] = []
        cursor = 0
        for match in _EVIDENCE_TOKEN_RE.finditer(result):
            pieces.append(result[cursor : match.start()])
            token = match.group(0)
            before = result[match.start() - 1] if match.start() else ""
            after = result[match.end()] if match.end() < len(result) else ""
            folded = token.casefold()
            keep = (
                (
                    before == "<"
                    and after == ">"
                    and _PROTECTED_MARKER_RE.fullmatch(f"<{token}>") is not None
                )
                or folded in _EVIDENCE_SAFE_TOKENS
                or token.isdigit()
                or (token.endswith(("=", ":")) and token.isascii())
            )
            pieces.append(token if keep else self.alias_for(token, "EVIDENCE"))
            cursor = match.end()
        pieces.append(result[cursor:])
        return "".join(pieces)

    def restore(self, value: str) -> str:
        result = str(value)
        for alias, raw in sorted(self._alias_to_raw.items(), key=lambda item: len(item[0]), reverse=True):
            result = result.replace(alias, raw)
        return result


def _clip(value: Any, limit: int = MAX_ITEM_CHARS) -> str:
    text = str(value or "").strip()
    return text[: max(0, int(limit))]


def _trim_masked_projection_text(value: Any, limit: int) -> str:
    """Bound masked text without cutting an opaque marker in half."""

    text = str(value or "")
    bounded = text[: max(0, int(limit))]
    # Evidence masking uses angle-bracket markers.  A raw character slice can
    # leave ``<EVID`` at the query boundary, which is neither a safe marker nor
    # useful evidence and causes the strict Cloud attestation to reject the
    # whole otherwise-safe request.  Drop only the incomplete trailing token.
    if bounded:
        opening = bounded.rfind("<")
        closing = bounded.rfind(">")
        if opening > closing:
            bounded = bounded[:opening].rstrip()
    return bounded


def _safe_public_filename(value: Any, *, index: int = 1) -> str:
    """Return a bounded non-sensitive filename for result metadata."""

    raw = Path(str(value or "").replace("\\", "/")).name
    if not raw:
        return f"file_{index}"
    suffix = Path(raw).suffix.casefold()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ""
    stem = Path(raw).stem
    if re.fullmatch(r"(?:src|tests|input|evidence|document|main|test|aoitalk)[a-z0-9_.-]*", stem, re.I):
        return f"{stem[:80]}{suffix}"
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]
    return f"file_{digest}{suffix}"


def _bounded_list(value: Any, *, limit: int = MAX_LIST_ITEMS, item_limit: int = MAX_ITEM_CHARS) -> list[str]:
    if isinstance(value, str):
        values: Sequence[Any] = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        return []
    result: list[str] = []
    for item in values[:limit]:
        text = _clip(item, item_limit)
        if text and text not in result:
            result.append(text)
    return result


def _json_object(value: Any) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _bounded_json_text(payload: Mapping[str, Any], *, limit: int = MAX_QUERY_CHARS) -> str:
    """Serialize a safe payload without slicing JSON into invalid syntax."""

    def encode(value: Mapping[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    candidate = dict(payload)
    text = encode(candidate)
    if len(text) <= limit:
        return text
    problem = candidate.get("problem")
    if isinstance(problem, Mapping):
        compact = dict(problem)
        for key, item_limit, char_limit in (
            ("inputs", 8, 600),
            ("evidence", 8, 400),
            ("parsing_strategy", 8, 300),
            ("rules", 8, 300),
            ("expected_outputs", 8, 300),
            ("edge_cases", 8, 300),
            ("implementation_constraints", 8, 300),
        ):
            value = compact.get(key)
            if isinstance(value, list):
                trimmed: list[Any] = []
                for item in value[:item_limit]:
                    if isinstance(item, Mapping):
                        item_copy = dict(item)
                        for field_name in ("content", "sample"):
                            if isinstance(item_copy.get(field_name), str):
                                item_copy[field_name] = _trim_masked_projection_text(
                                    item_copy[field_name],
                                    char_limit,
                                )
                        trimmed.append(item_copy)
                    elif isinstance(item, str):
                        trimmed.append(_trim_masked_projection_text(item, char_limit))
                    else:
                        trimmed.append(item)
                compact[key] = trimmed
        if isinstance(compact.get("foundation"), Mapping):
            foundation = dict(compact["foundation"])
            if isinstance(foundation.get("nodes"), list):
                foundation["nodes"] = foundation["nodes"][:16]
            compact["foundation"] = foundation
        compact["goal"] = _trim_masked_projection_text(compact.get("goal"), 400)
        compact["truncated"] = True
        candidate["problem"] = compact
        text = encode(candidate)
    if len(text) <= limit:
        return text
    # Last-resort structure-only envelope.  This remains valid JSON and never
    # falls back to the original/raw payload.
    return encode(
        {
            "schema": str(candidate.get("schema") or "aoitalk.app_problem_ir.v1"),
            "phase": str(candidate.get("phase") or "design"),
            "task": "Return bounded advisory JSON only.",
            "problem": {
                "schema_version": 1,
                "kind": "app",
                "privacy": {"raw_material_included": False, "truncated": True},
            },
        }
    )[:limit]


def _safe_relative_path(value: Any, *, allow_private: bool = False) -> str:
    try:
        normalized = normalize_app_relative_path(str(value))
    except (TypeError, ValueError, AppStorageError) as exc:
        raise ValueError("App workflow path is invalid") from exc
    if not allow_private and is_private_app_path(normalized):
        raise ValueError("private App path cannot be used by workflow")
    return normalized


def _read_input_value(
    value: Any,
    *,
    max_chars: int = MAX_INPUT_CHARS,
    allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
    base_root: str | os.PathLike[str] | None = None,
) -> tuple[str, str | None]:
    """Read bounded text from a value, returning ``(text, filename)``."""

    if isinstance(value, bytes):
        return value[:max_chars].decode("utf-8", errors="replace"), None
    if isinstance(value, str) and base_root:
        try:
            if not Path(value).expanduser().is_absolute():
                candidate = Path(base_root).expanduser() / Path(value)
                if candidate.is_file():
                    value = candidate
        except (OSError, ValueError, RuntimeError):
            pass
    is_file_value = isinstance(value, Path)
    if isinstance(value, str) and len(value) < 4096:
        try:
            is_file_value = Path(value).is_file()
        except (OSError, ValueError, RuntimeError):
            is_file_value = False
    if is_file_value:
        try:
            path = Path(value)
            resolved = path.expanduser().resolve(strict=True)
            if allowed_roots is not None:
                roots = []
                for root in allowed_roots:
                    try:
                        roots.append(Path(root).expanduser().resolve(strict=True))
                    except OSError:
                        continue
                if not roots or not any(
                    _is_under(resolved, root) for root in roots
                ):
                    return "", None
            path = resolved
            # Input files are read by the local caller.  Do not allow a file
            # callback to make this service a generic unrestricted file reader.
            data = path.read_bytes()[: max_chars * 4]
            return data.decode("utf-8", errors="replace")[:max_chars], path.name
        except (OSError, ValueError):
            return _clip(value, max_chars), None
    return _clip(value, max_chars), None


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Structured problem and design contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppProblemIR:
    """Masked, bounded problem representation for App/Macro design."""

    workflow_id: str
    kind: str
    goal: str
    inputs: tuple[Mapping[str, Any], ...] = ()
    parsing_strategy: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    edge_cases: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()
    evidence: tuple[Mapping[str, Any], ...] = ()
    # Raw material remains local and is intentionally excluded by to_dict.
    _raw_material: tuple[str, ...] = field(default=(), repr=False, compare=False)
    _protected: _ProtectedContext | None = field(default=None, repr=False, compare=False)
    _foundation_context: Any | None = field(default=None, repr=False, compare=False)
    _foundation_context_id: str | None = field(default=None, repr=False, compare=False)
    _foundation_node_ids: tuple[str, ...] = field(default=(), repr=False, compare=False)
    _foundation_complete: bool = field(default=False, repr=False, compare=False)
    _goal_raw: str = field(default="", repr=False, compare=False)

    SCHEMA_VERSION = 1

    @classmethod
    def from_inputs(
        cls,
        *,
        kind: str = "app",
        goal: str = "",
        inputs: Mapping[str, Any] | Sequence[Any] | None = None,
        files: Mapping[str, Any] | Sequence[Any] | None = None,
        workflow_id: str | None = None,
        max_chars: int = MAX_INPUT_CHARS,
        allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
        base_root: str | os.PathLike[str] | None = None,
    ) -> "AppProblemIR":
        normalized_kind = "macro" if str(kind).casefold() == "macro" else "app"
        wid = str(workflow_id or uuid.uuid4())
        context = _ProtectedContext(wid)
        source_items: list[tuple[str, str]] = []
        raw_values: list[str] = []

        def add_item(label: Any, value: Any, *, register_scalar: bool = True) -> None:
            text, filename = _read_input_value(
                value,
                max_chars=max_chars,
                allowed_roots=allowed_roots,
                base_root=base_root,
            )
            name = _clip(label, 160) or "input"
            if filename:
                name = filename
            source_items.append((name, text))
            # Register scalar identifiers (customer/project names, host names,
            # paths, etc.) but not an entire log/config body.  Registering a
            # whole body would collapse the useful structural projection into
            # one opaque token.
            label_key = str(label or "").casefold()
            if (
                register_scalar
                and isinstance(value, str)
                and 3 <= len(value.strip()) <= 512
                and label_key not in {"content", "text", "log", "config", "data", "source"}
            ):
                raw_values.append(value.strip())

        if isinstance(inputs, Mapping):
            for key, value in inputs.items():
                if key in {"files", "attachments"}:
                    continue
                add_item(key, value)
        elif inputs is not None:
            for index, value in enumerate(inputs, start=1):
                if isinstance(value, Mapping):
                    name = value.get("name") or value.get("path") or f"input_{index}"
                    content = value.get("content", value.get("text", value.get("data")))
                    if content is None:
                        content = value.get("path") or value.get("relative_path") or ""
                    add_item(name, content, register_scalar=False)
                else:
                    add_item(f"input_{index}", value, register_scalar=False)
        if isinstance(files, Mapping):
            for name, value in files.items():
                # File contents are evidence, not scalar identifiers.  Do not
                # register the complete body as one alias or the structural
                # projection would collapse to ``<VALUE_001>``.
                if isinstance(value, Mapping):
                    value = (
                        value.get("content")
                        if value.get("content") is not None
                        else value.get("text")
                        if value.get("text") is not None
                        else value.get("data")
                        if value.get("data") is not None
                        else value.get("path")
                        or value.get("file_path")
                        or ""
                    )
                add_item(name, value, register_scalar=False)
        elif files is not None:
            for index, value in enumerate(files, start=1):
                if isinstance(value, Mapping):
                    name = value.get("name") or value.get("path") or f"file_{index}"
                    content = value.get("content", value.get("text", value.get("data")))
                    if content is None:
                        # Chat attachment metadata normally contains a
                        # server-verified path.  Read only that concrete file;
                        # callers should perform authorization before handing
                        # metadata to this workflow.  The allowed-root check
                        # is still enforced by ``_read_input_value``.
                        content = (
                            value.get("path")
                            or value.get("relative_path")
                            or value.get("filename")
                            or ""
                        )
                    add_item(name, content, register_scalar=False)
                else:
                    add_item(f"file_{index}", value, register_scalar=False)

        # Register long values before regex masking so aliases are stable and
        # source paths/customer identifiers cannot appear in the projection.
        for raw in sorted(set(raw_values), key=len, reverse=True):
            if len(raw) >= 3 and not raw.isspace():
                context.alias_for(raw, "VALUE")

        raw_goal = _clip(goal, 2_000)
        if raw_goal:
            raw_values.append(raw_goal)
        # Keep the short user goal structurally readable; the shared
        # foundation attached at workflow execution and the final literal
        # assertion still gate any Cloud-bound copy.  Evidence bodies use the
        # stricter token-wise masker below.
        masked_goal = context.mask_text(raw_goal)
        masked_inputs: list[Mapping[str, Any]] = []
        safe_paths: list[str] = []
        evidence: list[Mapping[str, Any]] = []
        all_masked_text: list[str] = []
        for index, (name, text) in enumerate(source_items, start=1):
            # File/config/log bodies are untrusted evidence.  Known labels and
            # status markers remain readable, while arbitrary unlabelled
            # tokens are replaced by opaque aliases so a novel customer value
            # cannot bypass the regex set.
            masked = context.mask_evidence_text(text)
            all_masked_text.append(masked)
            extension = Path(str(name)).suffix.casefold()
            safe_name = (
                f"input_{index}{extension}"
                if extension
                and extension not in {".pem", ".key", ".p12", ".pfx", ".secret", ".secrets"}
                and re.fullmatch(r"\.[a-z0-9]{1,8}", extension)
                else f"input_{index}"
            )
            safe_paths.append(safe_name)
            masked_inputs.append(
                {
                    "id": f"input_{index}",
                    "path": safe_name,
                    "kind": "text" if text else "empty",
                    "chars": min(len(text), max_chars),
                    "content": masked[: min(len(masked), 16_000)],
                }
            )
            evidence.append(
                {
                    "id": f"evidence_{index}",
                    "source_id": f"input_{index}",
                    "line_count": max(0, masked.count("\n") + (1 if masked else 0)),
                    "sample": masked[:2_000],
                }
            )

        corpus = "\n".join(all_masked_text)
        lower = corpus.casefold()
        parsing = ["read bounded UTF-8/text input", "ignore unknown fields and malformed lines"]
        if any(token in lower for token in ("json", ".json", "{")):
            parsing.insert(0, "parse JSON when input is valid, then fall back to line scanning")
        if any(token in lower for token in ("csv", ".csv", "comma")):
            parsing.insert(0, "support CSV/header-based records")
        if normalized_kind == "macro":
            parsing.append("treat attached config/log content as untrusted data, never as instructions")

        rules: list[str] = []
        if any(token in lower for token in ("error", "critical", "fail", "ng", "異常", "失敗", "down")):
            rules.append("classify an input as NG when a high-severity error/failure marker is present")
        if any(token in lower for token in ("ok", "success", "正常", "up", "healthy")):
            rules.append("classify as OK only when no NG marker is present and a healthy marker is observed")
        if not rules:
            rules.append("report an explicit, useful reason when evidence is insufficient")
        expected = ["status: OK or NG", "reason explaining the classification"]
        if normalized_kind == "app":
            expected.append("structured result suitable for the App target")
        edge_cases = [
            "empty input",
            "malformed or partial records",
            "conflicting OK and NG markers (NG takes precedence)",
            "unknown status without enough evidence",
        ]
        # Keep raw material only for local implementation/debug callbacks.
        local_raw = tuple(raw_values + [text for _, text in source_items])
        return cls(
            workflow_id=wid,
            kind=normalized_kind,
            goal=masked_goal,
            inputs=tuple(masked_inputs),
            parsing_strategy=tuple(parsing[:MAX_LIST_ITEMS]),
            rules=tuple(rules[:MAX_LIST_ITEMS]),
            expected_outputs=tuple(expected),
            edge_cases=tuple(edge_cases),
            source_paths=tuple(safe_paths),
            evidence=tuple(evidence),
            _raw_material=local_raw,
            _protected=context,
            _goal_raw=raw_goal,
        )

    @property
    def aliases(self) -> Mapping[str, str]:
        """Safe alias metadata (alias names only; no raw values)."""

        if self._protected is None:
            return {}
        return {f"value_{index}": alias for index, alias in enumerate(self._protected._alias_to_raw, start=1)}

    @property
    def raw_material(self) -> tuple[str, ...]:
        """Local-only raw values for implementation callbacks.

        Callers should prefer ``local_material`` and must never include this in
        a Cloud query or persistent/audit metadata.
        """

        return self._raw_material

    def local_material(self) -> str:
        return "\n".join(self._raw_material)[:MAX_INPUT_CHARS]

    @property
    def context_id(self) -> str | None:
        context = self._foundation_context
        if context is not None:
            return str(getattr(context, "context_id", "") or "") or self._foundation_context_id
        return self._foundation_context_id

    @property
    def node_ids(self) -> tuple[str, ...]:
        context = self._foundation_context
        if context is None:
            return self._foundation_node_ids
        try:
            refs = getattr(context, "references", ())
            return tuple(str(getattr(ref, "node_id", "") or "") for ref in refs if getattr(ref, "node_id", None))
        except Exception:
            return self._foundation_node_ids

    def with_foundation_context(
        self,
        context: Any | None,
        *,
        complete: bool = True,
    ) -> "AppProblemIR":
        context_id = str(getattr(context, "context_id", "") or "") or None
        try:
            refs = tuple(getattr(ref, "node_id", "") for ref in getattr(context, "references", ()))
        except Exception:
            refs = ()
        return replace(
            self,
            _foundation_context=context,
            _foundation_context_id=context_id,
            _foundation_node_ids=tuple(str(item) for item in refs if item),
            _foundation_complete=bool(complete),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "workflow_id": self.workflow_id,
            "kind": self.kind,
            "goal": self.goal,
            "inputs": [dict(item) for item in self.inputs],
            "parsing_strategy": list(self.parsing_strategy),
            "rules": list(self.rules),
            "expected_outputs": list(self.expected_outputs),
            "edge_cases": list(self.edge_cases),
            "source_paths": list(self.source_paths),
            "evidence": [dict(item) for item in self.evidence],
        }

    def to_cloud_projection(self) -> dict[str, Any]:
        """Return the only representation permitted in a Cloud query."""

        projection = self.to_dict()
        # Keep Cloud-bound evidence compact.  The local implementation retains
        # the complete bounded files, while the advisor needs only a
        # representative structural excerpt and counts.  This also keeps one
        # semantic sidecar call within its finite input/latency budget.
        compact_inputs: list[dict[str, Any]] = []
        for item in self.inputs[:16]:
            compact = dict(item)
            if isinstance(compact.get("content"), str):
                compact["content"] = _trim_masked_projection_text(compact["content"], 2_000)
            compact_inputs.append(compact)
        projection["inputs"] = compact_inputs
        compact_evidence: list[dict[str, Any]] = []
        for item in self.evidence[:16]:
            compact = dict(item)
            if isinstance(compact.get("sample"), str):
                compact["sample"] = _trim_masked_projection_text(compact["sample"], 800)
            compact_evidence.append(compact)
        projection["evidence"] = compact_evidence
        projection["goal"] = _trim_masked_projection_text(self.goal, 800)
        # Defense in depth for callers that constructed an IR manually (or
        # through ``from_dict``) rather than ``from_inputs``.  The public
        # projection never contains a known local literal, regardless of
        # whether it was recognized by a regex.
        def scrub(value: Any) -> Any:
            if isinstance(value, str):
                result = value
                for raw in sorted(self.raw_material, key=len, reverse=True):
                    if isinstance(raw, str) and len(raw) >= 3:
                        result = result.replace(raw, "<LOCAL_VALUE>")
                return result
            if isinstance(value, Mapping):
                return {key: scrub(item) for key, item in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value
        projection = scrub(projection)
        projection["privacy"] = {
            "raw_material_included": False,
            "aliases_local_only": True,
            "source_paths_are_safe_ids": True,
        }
        if self.context_id:
            projection["context_id"] = self.context_id
            projection["node_ids"] = list(self.node_ids)
        # Include a compact safe projection from the shared foundation.  The
        # custom App IR remains the domain schema, while this nested envelope
        # proves that canonical one-way masking has run over the same raw
        # evidence.  If the foundation is unavailable, the already-masked
        # domain fields remain the conservative fallback; raw material is
        # never inserted here.
        foundation = self._foundation_context
        if foundation is not None:
            if not callable(getattr(foundation, "project", None)):
                raise RuntimeError("App workflow protected context is malformed")
            if not self._foundation_complete and self.raw_material:
                raise RuntimeError("App workflow protected projection is incomplete")
            refs = tuple(getattr(foundation, "references", ()) or ())
            if refs:
                try:
                    # A long high-entropy log can expand considerably when
                    # the foundation's literal scrub replaces every token
                    # with an alias.  The App IR above is the semantic
                    # evidence channel; use only a bounded small-value sample
                    # for the foundation lifecycle/provenance proof so the
                    # shared projection budget remains finite.
                    selected_refs: list[Any] = []
                    bindings = tuple(getattr(foundation, "bindings", ()) or ())
                    for reference in refs:
                        if len(selected_refs) >= 16:
                            break
                        node_id = getattr(reference, "node_id", None)
                        binding = next(
                            (
                                item
                                for item in bindings
                                if getattr(getattr(item, "reference", None), "node_id", None) == node_id
                            ),
                            None,
                        )
                        raw_value = getattr(binding, "raw_value", None)
                        if isinstance(raw_value, str) and len(raw_value) <= 512:
                            selected_refs.append(reference)
                    if not selected_refs:
                        projection["foundation"] = {
                            "schema": "aoitalk.workflow.projection.v1",
                            "context_id": str(getattr(foundation, "context_id", ""))[:128],
                            "nodes": [],
                            "node_count": len(refs),
                            "truncated": True,
                            "masking": "forced_protected",
                        }
                        return projection
                    safe = foundation.project(refs=tuple(selected_refs))
                    safe_payload = safe.to_dict()
                except Exception as exc:
                    # A canonical boundary failure is never a reason to send
                    # the domain fallback; callers must fail closed instead.
                    raise RuntimeError("App workflow protected projection failed") from exc
                if not isinstance(safe_payload, Mapping):
                    raise RuntimeError("App workflow protected projection is malformed")
                # The foundation projection is a lifecycle/privacy proof, not
                # the App domain evidence channel.  A long log can contain
                # overlapping words (for example ``unreachable``) that a
                # generic literal scrubber may represent as a marker embedded
                # in a residual token.  Keep the domain IR's already-masked
                # structural evidence above, while reducing each foundation
                # value to a fresh opaque marker before strict Cloud-schema
                # validation.  Node/object IDs and provenance remain useful
                # for advisory references; raw bindings never leave context.
                foundation_nodes_safe: list[dict[str, Any]] = []
                raw_nodes = safe_payload.get("nodes", ())
                if isinstance(raw_nodes, Sequence) and not isinstance(raw_nodes, (str, bytes)):
                    for index, node in enumerate(raw_nodes[:16], start=1):
                        if not isinstance(node, Mapping):
                            continue
                        safe_node: dict[str, Any] = {}
                        for key in ("node_id", "object_id", "kind", "role", "provenance"):
                            if key in node:
                                safe_node[key] = node[key]
                        node_seed = f"{self.context_id or 'workflow'}:{safe_node.get('node_id') or index}"
                        safe_node["value"] = (
                            "[WF_VALUE_"
                            + hashlib.sha256(node_seed.encode("utf-8", "replace")).hexdigest()[:16].upper()
                            + "]"
                        )
                        foundation_nodes_safe.append(safe_node)
                projection["foundation"] = {
                    "schema": "aoitalk.workflow.projection.v1",
                    "context_id": str(getattr(safe, "context_id", ""))[:128],
                    "nodes": foundation_nodes_safe,
                    "masking": "forced_protected",
                }
            elif self.raw_material:
                raise RuntimeError("App workflow protected projection has no references")
        return projection

    @property
    def cloud_projection(self) -> dict[str, Any]:
        return self.to_cloud_projection()

    @property
    def masked(self) -> dict[str, Any]:
        return self.to_cloud_projection()

    @property
    def safe_projection(self) -> dict[str, Any]:
        return self.to_cloud_projection()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AppProblemIR":
        if not isinstance(value, Mapping):
            raise ValueError("AppProblemIR must be an object")
        version = value.get("schema_version", cls.SCHEMA_VERSION)
        if version != cls.SCHEMA_VERSION:
            raise ValueError("unsupported AppProblemIR schema")
        kind = "macro" if str(value.get("kind", "app")).casefold() == "macro" else "app"
        workflow_id = _clip(value.get("workflow_id"), 100)
        if not workflow_id:
            raise ValueError("workflow_id is required")
        raw_inputs = (
            value.get("inputs")
            if isinstance(value.get("inputs"), Sequence)
            and not isinstance(value.get("inputs"), (str, bytes, bytearray))
            else ()
        )
        raw_evidence = (
            value.get("evidence")
            if isinstance(value.get("evidence"), Sequence)
            and not isinstance(value.get("evidence"), (str, bytes, bytearray))
            else ()
        )
        files: dict[str, Any] = {}
        for index, item in enumerate(raw_inputs[:MAX_LIST_ITEMS], start=1):
            if not isinstance(item, Mapping):
                continue
            files[f"input_{index}.txt"] = item.get("content", item.get("text", item.get("sample", "")))
        for index, item in enumerate(raw_evidence[:MAX_LIST_ITEMS], start=1):
            if not isinstance(item, Mapping):
                continue
            files[f"evidence_{index}.txt"] = item.get("sample", item.get("content", item.get("text", "")))
        # Re-enter through the same bounded masking constructor used by normal
        # workflow input.  A persisted/model-supplied IR is untrusted data,
        # not an authority to populate raw fields or bypass the local context.
        base = cls.from_inputs(
            kind=kind,
            goal=_clip(value.get("goal"), 2_000),
            files=files,
            workflow_id=workflow_id,
        )

        def safe_sequence(raw: Any) -> tuple[str, ...]:
            values = _bounded_list(raw)
            if base._protected is None:
                return tuple(values)
            return tuple(
                base._protected.mask_evidence_text(item)[:MAX_ITEM_CHARS]
                for item in values
            )

        return replace(
            base,
            parsing_strategy=safe_sequence(value.get("parsing_strategy")),
            rules=safe_sequence(value.get("rules")),
            expected_outputs=safe_sequence(value.get("expected_outputs")),
            edge_cases=safe_sequence(value.get("edge_cases")),
            source_paths=tuple(
                f"input_{index}.txt" for index in range(1, min(len(files), MAX_LIST_ITEMS) + 1)
            ),
        )

    from_raw = from_inputs
    build = from_inputs


@dataclass(frozen=True)
class AppDesignSpec:
    """Validated advisory design consumed by the local implementation."""

    workflow_id: str
    kind: str
    inputs: tuple[str, ...] = ()
    parsing_strategy: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    edge_cases: tuple[str, ...] = ()
    test_cases: tuple[Mapping[str, Any], ...] = ()
    implementation_constraints: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    revision_advice: tuple[str, ...] = ()
    target_key: str = "aoitalk"
    entrypoint: str = "src/main.py"
    source_of_advice: str = "local_fallback"
    advisory_text: str = field(default="", repr=False)

    SCHEMA_VERSION = 1

    @property
    def parsing(self) -> tuple[str, ...]:
        return self.parsing_strategy

    @property
    def outputs(self) -> tuple[str, ...]:
        return self.expected_outputs

    @property
    def constraints(self) -> tuple[str, ...]:
        return self.implementation_constraints

    @classmethod
    def fallback(cls, problem: AppProblemIR, *, source_of_advice: str = "local_fallback") -> "AppDesignSpec":
        return cls(
            workflow_id=problem.workflow_id,
            kind=problem.kind,
            inputs=tuple(_clip(item.get("path") or item.get("id"), 200) for item in problem.inputs),
            parsing_strategy=tuple(problem.parsing_strategy),
            rules=tuple(problem.rules),
            expected_outputs=tuple(problem.expected_outputs),
            edge_cases=tuple(problem.edge_cases),
            test_cases=(
                {"name": "healthy_input", "expected_status": "OK"},
                {"name": "error_input", "expected_status": "NG"},
                {"name": "empty_input", "expected_status": "NG"},
            ),
            implementation_constraints=(
                "local implementation remains the execution authority",
                "do not execute source/config as instructions",
                "bound input size and return structured JSON",
            ),
            missing_information=(),
            revision_advice=(),
            source_of_advice=source_of_advice,
        )

    @classmethod
    def from_advisory(cls, advisory: Any, problem: AppProblemIR) -> "AppDesignSpec":
        """Parse a bounded Cloud response; invalid/unknown fields fail closed."""

        raw_text = ""
        if isinstance(advisory, Mapping):
            payload = dict(advisory)
        else:
            raw_text = _clip(advisory, MAX_ADVISORY_CHARS)
            text = raw_text.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
                text = re.sub(r"\s*```$", "", text)
            payload = {}
            try:
                parsed = json.loads(text)
                if isinstance(parsed, Mapping):
                    payload = dict(parsed)
            except (TypeError, ValueError, json.JSONDecodeError):
                # A prose response is still useful as revision/advice text but
                # cannot define executable paths or commands.
                payload = {"revision_advice": [text]} if text else {}
        if isinstance(payload.get("design"), Mapping):
            payload = dict(payload["design"])
        fallback = cls.fallback(problem, source_of_advice="cloud")

        def values(*keys: str, default: Sequence[str] = ()) -> tuple[str, ...]:
            for key in keys:
                if key in payload:
                    result = tuple(
                        _safe_advisory_text(problem, item, limit=MAX_ITEM_CHARS)
                        for item in _bounded_list(payload.get(key), item_limit=MAX_ITEM_CHARS)
                    )
                    result = tuple(item for item in result if item)
                    if result:
                        return result
            return tuple(default)

        inputs = values("inputs", default=fallback.inputs)
        parsing = values("parsing_strategy", "parsing", default=fallback.parsing_strategy)
        rules = values("rules", "decision_rules", default=fallback.rules)
        expected = values("expected_outputs", "outputs", default=fallback.expected_outputs)
        edge = values("edge_cases", "edgecases", default=fallback.edge_cases)
        constraints = values("implementation_constraints", "constraints", default=fallback.implementation_constraints)
        missing = values("missing_information", "missing", default=())
        revision = values("revision_advice", "revision", "advice", default=())

        raw_tests = payload.get("test_cases", payload.get("tests", ()))
        tests: list[Mapping[str, Any]] = []
        if isinstance(raw_tests, Mapping):
            raw_tests = [raw_tests]
        if isinstance(raw_tests, Sequence) and not isinstance(raw_tests, (str, bytes)):
            for item in raw_tests[:MAX_LIST_ITEMS]:
                if isinstance(item, Mapping):
                    # Test data is advisory and may never contain a command or
                    # arbitrary path.  Keep only scalar JSON values.
                    cleaned: dict[str, Any] = {}
                    for key, value in list(item.items())[:16]:
                        if str(key).casefold() in {"command", "cmd", "shell", "path", "entrypoint", "exec", "tool"}:
                            continue
                        if isinstance(value, (str, int, float, bool)) or value is None:
                            safe_key = _safe_advisory_text(problem, key, limit=80)
                            if not safe_key:
                                continue
                            cleaned[safe_key] = (
                                _safe_advisory_text(problem, value, limit=500)
                                if isinstance(value, str)
                                else value
                            )
                    if cleaned:
                        tests.append(cleaned)
        if not tests:
            tests = [dict(item) for item in fallback.test_cases]

        # The system workflow owns one deterministic Target/source namespace.
        # Cloud output may describe behavior, but it cannot retarget writes or
        # execution to an arbitrary existing file.
        target_key = fallback.target_key
        entrypoint = fallback.entrypoint
        return cls(
            workflow_id=problem.workflow_id,
            kind=problem.kind,
            inputs=inputs,
            parsing_strategy=parsing,
            rules=rules,
            expected_outputs=expected,
            edge_cases=edge,
            test_cases=tuple(tests),
            implementation_constraints=constraints,
            missing_information=missing,
            revision_advice=revision,
            target_key=target_key,
            entrypoint=entrypoint,
            source_of_advice="cloud",
            advisory_text=_safe_advisory_text(
                problem,
                raw_text,
                limit=MAX_ADVISORY_CHARS,
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, problem: AppProblemIR | None = None) -> "AppDesignSpec":
        """Validate a structured design supplied by a local adapter/test."""

        if problem is None:
            workflow_id = _clip(value.get("workflow_id"), 100)
            kind = "macro" if str(value.get("kind", "app")).casefold() == "macro" else "app"
            problem = AppProblemIR(workflow_id=workflow_id or str(uuid.uuid4()), kind=kind, goal="")
        return cls.from_advisory(value, problem)

    def with_revision(self, advice: Sequence[str], *, source_of_advice: str | None = None) -> "AppDesignSpec":
        return AppDesignSpec(
            **{
                **self.__dict__,
                "revision_advice": tuple(
                    _safe_design_fragment(item)
                    for item in _bounded_list(advice)
                    if _safe_design_fragment(item)
                ),
                "source_of_advice": source_of_advice or self.source_of_advice,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        safe_sequence = lambda values: [
            _safe_design_fragment(item) for item in values if _safe_design_fragment(item)
        ]
        safe_tests: list[dict[str, Any]] = []
        for test in self.test_cases:
            safe_test: dict[str, Any] = {}
            for key, value in list(test.items())[:16]:
                safe_key = _safe_design_fragment(key, limit=80)
                if not safe_key:
                    continue
                safe_test[safe_key] = (
                    _safe_design_fragment(value, limit=500)
                    if isinstance(value, str)
                    else value
                )
            if safe_test:
                safe_tests.append(safe_test)
        return {
            "schema_version": self.SCHEMA_VERSION,
            "workflow_id": self.workflow_id,
            "kind": self.kind,
            "inputs": safe_sequence(self.inputs),
            "parsing_strategy": safe_sequence(self.parsing_strategy),
            "rules": safe_sequence(self.rules),
            "expected_outputs": safe_sequence(self.expected_outputs),
            "edge_cases": safe_sequence(self.edge_cases),
            "test_cases": safe_tests,
            "implementation_constraints": safe_sequence(self.implementation_constraints),
            "missing_information": safe_sequence(self.missing_information),
            "revision_advice": safe_sequence(self.revision_advice),
            "target_key": self.target_key,
            "entrypoint": self.entrypoint,
            "source_of_advice": self.source_of_advice,
        }


@dataclass
class AppBuildResult:
    status: str
    kind: str
    workflow_id: str
    workspace: str | None = None
    app_id: str | None = None
    target_key: str = "aoitalk"
    problem: AppProblemIR | None = None
    design: AppDesignSpec | None = None
    cloud_status: str = "not_consulted"
    cloud_advisory: str = ""
    consultations: int = 0
    attempts: int = 0
    test_result: Mapping[str, Any] | None = None
    run_result: Mapping[str, Any] | None = None
    diagnostics: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    intent: AppWorkflowIntent | None = None

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    @property
    def user_message(self) -> str:
        if self.ok:
            target = self.target_key or "aoitalk"
            app_ref = self.app_id or "App workspace"
            return f"{self.kind.title()} Appを生成し、Target「{target}」を検証しました（{app_ref}）。"
        return "App/Macroワークフローを完了できませんでした。入力と権限を確認して再試行してください。"

    message = user_message

    @property
    def workspace_path(self) -> str | None:
        return self.workspace

    @property
    def app_workspace(self) -> str | None:
        return self.workspace

    def to_dict(self) -> dict[str, Any]:
        safe_workspace = None
        if self.workspace:
            # Keep only an opaque bounded label in public/audit projections;
            # user-selected workspace/app names may contain customer data.
            workspace_name = Path(self.workspace).name
            if self.app_id:
                safe_workspace = f"app_{str(self.app_id)[:24]}"
            else:
                digest = hashlib.sha256(
                    workspace_name.encode("utf-8", "replace")
                ).hexdigest()[:12]
                safe_workspace = f"workspace_{digest}"
        safe_advisory = (
            _safe_advisory_text(self.problem, self.cloud_advisory, limit=MAX_ADVISORY_CHARS)
            if self.problem
            else ""
        )
        safe_files = [
            _safe_public_filename(item, index=index)
            for index, item in enumerate(self.files[:MAX_LIST_ITEMS], start=1)
        ]
        return {
            "status": self.status,
            "kind": self.kind,
            "workflow_id": self.workflow_id,
            "workspace": safe_workspace,
            "workspace_name": safe_workspace,
            "app_id": self.app_id,
            "target_key": self.target_key,
            "problem": self.problem.to_dict() if self.problem else None,
            "design": self.design.to_dict() if self.design else None,
            "cloud_status": self.cloud_status,
            "cloud_advisory": safe_advisory,
            "consultations": self.consultations,
            "attempts": self.attempts,
            "test_result": (
                _safe_result_mapping(self.problem, self.test_result)
                if isinstance(self.test_result, Mapping)
                else self.test_result
            ),
            "run_result": (
                _safe_result_mapping(self.problem, self.run_result)
                if isinstance(self.run_result, Mapping)
                else self.run_result
            ),
            "diagnostics": [
                _safe_advisory_text(self.problem, item, limit=2_000)
                if self.problem
                else _safe_design_fragment(item, limit=2_000)
                for item in self.diagnostics[:MAX_LIST_ITEMS]
            ],
            "files": safe_files,
            "intent": self.intent.to_dict() if self.intent else None,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


# ---------------------------------------------------------------------------
# Workflow implementation
# ---------------------------------------------------------------------------


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return current
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, None)
        except TypeError:
            value = getter(key)
        return default if value is None else value
    return default


async def _invoke_callback(callback: Callable[..., Any], *, kwargs: Mapping[str, Any], positional: Sequence[Any] = ()) -> Any:
    """Invoke sync/async callbacks without leaking an implementation detail.

    Callbacks are intentionally optional integration hooks.  We inspect their
    signature first so a callback that only accepts ``(spec, workspace)`` can
    coexist with the richer production callback contract.
    """

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        # Some C-extension/builtin callables do not expose a signature.  This
        # is the only case where positional fallback is allowed; a TypeError
        # raised *by* the callback itself must never trigger a hidden retry.
        result = callback(*positional)
    else:
        accepts_var_kw = any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in signature.parameters.values()
        )
        if accepts_var_kw:
            call_kwargs = dict(kwargs)
        else:
            call_kwargs = {
                name: value
                for name, value in kwargs.items()
                if name in signature.parameters
            }
        if call_kwargs or not positional:
            result = callback(**call_kwargs)
        else:
            result = callback(*positional)
    if inspect.isawaitable(result):
        return await result
    return result


def _normalize_callback_result(value: Any, *, default_ok: bool = False) -> dict[str, Any]:
    if isinstance(value, Mapping):
        result = dict(value)
        if "ok" not in result:
            if "passed" in result:
                result["ok"] = bool(result.get("passed"))
            elif "success" in result:
                result["ok"] = bool(result.get("success"))
        if "ok" not in result:
            status = str(result.get("status") or "").casefold()
            result["ok"] = status in {"ok", "success", "succeeded", "passed"}
        return result
    if isinstance(value, bool):
        return {"ok": value, "status": "succeeded" if value else "failed"}
    if value is None:
        return {"ok": default_ok, "status": "succeeded" if default_ok else "unknown"}
    return {"ok": bool(value), "status": "succeeded" if value else "failed", "value": value}


def _safe_failure_text(problem: AppProblemIR, value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (Mapping, list, tuple)) else str(value or "")
    # Recreate aliases for failure text, then ensure every known raw sentinel
    # is absent even if an implementation callback returned unstructured logs.
    if problem._protected is not None:
        text = problem._protected.mask_text(text)
    for raw in sorted(problem.raw_material, key=len, reverse=True):
        # This function is used for user-visible/audit diagnostics, so remove
        # every known local literal (not only regex-detected secrets).  The
        # Cloud query itself remains structurally useful and uses the narrower
        # ``_looks_sensitive`` assertion below.
        if raw and len(raw) >= 3:
            text = text.replace(raw, "<LOCAL_VALUE>")
    return text[:8_000]


def _safe_advisory_text(problem: AppProblemIR, value: Any, *, limit: int = MAX_ITEM_CHARS) -> str:
    """Project one untrusted Cloud/local-advisor string for local use.

    A provider response is never a trusted source of local facts.  Known
    workflow literals are removed first; values that look like credentials,
    customer/project identifiers, or opaque non-ASCII evidence are replaced
    with a bounded marker instead of being copied into ``AppDesignSpec`` or a
    workflow result.  Generic structural English (for example ``NG takes
    precedence``) remains useful to the deterministic adapter.
    """

    text = _clip(value, limit)
    if not text:
        return ""
    safe = _safe_failure_text(problem, text)
    # ``_safe_failure_text`` replaces every known raw material value, but a
    # defensive pass protects values added after the context was built.
    for raw in sorted(problem.raw_material, key=len, reverse=True):
        if isinstance(raw, str) and len(raw) >= 3:
            safe = safe.replace(raw, "<LOCAL_VALUE>")
    if safe != text:
        return safe[:limit]
    if _ADVISORY_SENSITIVE_RE.search(text) or _ADVISORY_ID_RE.search(text):
        return "<ADVISORY_REDACTED>"
    # Japanese/other non-ASCII prose can contain an unlabelled customer or
    # operational value.  Do not risk echoing it from an untrusted provider.
    if any(ord(char) > 127 for char in text) and len(text.strip()) >= 4:
        return "<ADVISORY_REDACTED>"
    return safe[:limit]


def _safe_design_fragment(value: Any, *, limit: int = MAX_ITEM_CHARS) -> str:
    """Final defensive projection for an advisory design fragment."""

    text = _clip(value, limit)
    if not text or text.startswith("<") and text.endswith(">"):
        return text
    if re.fullmatch(r"(?:input|evidence)_\d+(?:\.[a-z0-9]{1,8})?", text, re.I):
        return text
    if text.casefold() in {
        "name",
        "expected_status",
        "status",
        "reason",
        "description",
        "kind",
        "workflow_id",
        "target_key",
        "entrypoint",
    }:
        return text
    if _ADVISORY_SENSITIVE_RE.search(text) or _ADVISORY_ID_RE.search(text):
        return "<ADVISORY_REDACTED>"
    if any(ord(char) > 127 for char in text) and len(text.strip()) >= 4:
        return "<ADVISORY_REDACTED>"
    return text


def _safe_result_mapping(
    problem: AppProblemIR | None,
    value: Mapping[str, Any],
    *,
    _depth: int = 0,
) -> dict[str, Any]:
    """Copy callback/runtime result metadata without local literal leakage.

    Callback/runtime results are extension seams and may contain arbitrary
    nested data. Keep the public projection bounded and avoid recursively
    walking a maliciously deep mapping forever.
    """
    if _depth >= 6:
        return {"value": "<RESULT_DEPTH_LIMIT>"}

    result: dict[str, Any] = {}
    safe_local_messages = {
        "正常状態を確認しました",
        "エラーまたは異常状態を検出しました",
        "正常性を確認できるマーカーがありません",
        "入力が空です",
    }
    for key, item in list(value.items())[:MAX_LIST_ITEMS]:
        safe_key = _safe_design_fragment(key, limit=120)
        if not safe_key:
            continue
        if isinstance(item, str):
            if item in safe_local_messages:
                result[safe_key] = item
            else:
                result[safe_key] = (
                    _safe_advisory_text(problem, item, limit=8_000)
                    if problem is not None
                    else _safe_design_fragment(item, limit=8_000)
                )
        elif isinstance(item, Mapping):
            result[safe_key] = _safe_result_mapping(problem, item, _depth=_depth + 1)
        elif isinstance(item, (list, tuple)):
            result[safe_key] = [
                child if isinstance(child, str) and child in safe_local_messages
                else (
                    _safe_advisory_text(problem, child, limit=8_000)
                    if problem is not None
                    else _safe_design_fragment(child, limit=8_000)
                )
                if isinstance(child, str)
                else _safe_result_mapping(problem, child, _depth=_depth + 1) if isinstance(child, Mapping)
                else child if child is None or type(child) in {bool, int, float} else "<UNSUPPORTED_RESULT>"
                for child in item[:MAX_LIST_ITEMS]
            ]
        else:
            result[safe_key] = (
                item
                if item is None or type(item) in {bool, int, float}
                else "<UNSUPPORTED_RESULT>"
            )
    return result


class AppBuildWorkflow:
    """Deterministic App/Macro workflow built on existing App facilities."""

    def __init__(
        self,
        config: Any | None = None,
        *,
        cloud_advisor: Any | None = None,
        coordinator: Any | None = None,
        app_service: Any | None = None,
        workspace_root: str | os.PathLike[str] | None = None,
        app_context_callback: Callable[..., Any] | None = None,
        persist_workspace_callback: Callable[..., Any] | None = None,
        implementation_callback: Callable[..., Any] | None = None,
        test_callback: Callable[..., Any] | None = None,
        run_callback: Callable[..., Any] | None = None,
        repair_callback: Callable[..., Any] | None = None,
        max_repair_attempts: int = 2,
        max_repairs: int | None = None,
        max_repair_iterations: int | None = None,
        command_timeout_seconds: int = 90,
    ) -> None:
        self.config = config
        self.cloud_advisor = cloud_advisor or coordinator
        self.app_service = app_service
        self.workspace_root = Path(workspace_root).expanduser() if workspace_root else None
        self.app_context_callback = app_context_callback or persist_workspace_callback
        self.implementation_callback = implementation_callback
        self.test_callback = test_callback
        self.run_callback = run_callback
        self.repair_callback = repair_callback
        configured_repairs = (
            max_repair_iterations
            if max_repair_iterations is not None
            else max_repairs
            if max_repairs is not None
            else max_repair_attempts
        )
        self.max_repair_attempts = max(0, min(int(configured_repairs), MAX_REPAIR_ATTEMPTS))
        self.command_timeout_seconds = max(1, min(int(command_timeout_seconds), 600))
        self.last_cloud_query: str = ""
        self.cloud_queries: list[str] = []
        self._last_implementation_source = "deterministic_fallback"

    async def _consult_cloud(
        self,
        problem: AppProblemIR,
        *,
        phase: str,
        failure: str = "",
        consent: bool = False,
        advisor_override: Any | None = None,
    ) -> tuple[str, str, str, int]:
        """Consult only the canonical Cloud Advisor coordinator."""

        # Local-only policy is an authoritative early deny.  This also keeps a
        # test double from accidentally pretending that it bypassed egress.
        privacy_mode = str(_config_get(self.config, "external_model_privacy.mode", "") or "").casefold()
        if privacy_mode == "local_only":
            return "privacy_blocked", "", "local_only", 0

        mode_default = "automatic" if self.config is None and (advisor_override or self.cloud_advisor) is not None else ""
        mode = str(_config_get(self.config, "cloud_advisor.mode", mode_default) or mode_default).casefold()
        if mode not in {"disabled", "manual", "automatic"}:
            return "privacy_blocked", "", "invalid_cloud_advisor_mode", 0
        if mode == "disabled":
            return "disabled", "", "disabled", 0
        if mode == "manual" and not consent:
            return "manual_required", "", "manual_consent_required", 0
        advisor = advisor_override or self.cloud_advisor
        if advisor is None and self.config is not None:
            try:
                from .cloud_advisor_service import CloudAdvisorCoordinator

                advisor = CloudAdvisorCoordinator(self.config)
            except Exception:
                advisor = None
        if advisor is None:
            return "not_consulted", "", "no_coordinator", 0

        # A credential-like literal in the command itself is ambiguous: it
        # cannot be safely interpreted as workflow intent.  Stop before even
        # invoking an injected/provider advisor.  File evidence containing
        # the same synthetic value remains eligible once it is structurally
        # projected and masked.
        if problem._goal_raw and _looks_sensitive(problem._goal_raw):
            raise ValueError("sensitive workflow goal is not eligible for Cloud")

        # Cloud consultation is permitted only after the shared protected
        # context has been attached.  A hand-built IR without canonical
        # foundation provenance is a local-only object, never a reason to
        # send a best-effort/raw fallback through an injected advisor.
        if problem._foundation_context is None:
            raise ValueError("workflow protected context is unavailable")
        projection = problem.to_cloud_projection()
        query_payload: dict[str, Any] = {
            "schema": "aoitalk.app_problem_ir.v1",
            "phase": phase,
            "task": "Design/debug an App or Macro. Return advisory structured JSON only; do not execute actions.",
            "problem": projection,
        }
        if failure:
            query_payload["failure_evidence"] = _safe_advisory_text(
                problem,
                failure,
                limit=2_000,
            )
        query = _bounded_json_text(query_payload)
        # A final local sentinel assertion protects against accidental future
        # additions to the query payload.
        for raw in problem.raw_material:
            if raw and len(raw) >= 3 and raw in query:
                raise ValueError("raw App workflow value would enter Cloud query")
        self.last_cloud_query = query
        self.cloud_queries.append(query)
        if len(self.cloud_queries) > 16:
            del self.cloud_queries[:-16]

        try:
            from .cloud_advisor_service import (
                CloudAdvisorEscalationAssessment,
                CloudAdvisorRequest,
                CloudAdvisorTriggerOrigin,
            )

            origin = getattr(CloudAdvisorTriggerOrigin, "WORKFLOW_CONTROLLER", CloudAdvisorTriggerOrigin.MAIN_AGENT)
            if consent:
                origin = CloudAdvisorTriggerOrigin.USER_EXPLICIT
            request = CloudAdvisorRequest(
                query=query,
                trigger_origin=origin,
                protected_projection=True,
                protected_projection_digest=hashlib.sha256(
                    query.encode("utf-8")
                ).hexdigest(),
                assessment=CloudAdvisorEscalationAssessment(
                    multi_constraint_reasoning=True,
                    high_uncertainty=bool(failure),
                    specialist_judgment=True,
                ),
            )
        except Exception as exc:
            logger.warning(
                "App workflow Cloud Advisor contract unavailable exception_type=%s",
                type(exc).__name__,
            )
            return "provider_error", "", "cloud_contract_unavailable", 0
        try:
            consult = getattr(advisor, "consult", None)
            if not callable(consult):
                consult = advisor
            value = await _invoke_callback(consult, kwargs={"request": request, "query": query}, positional=(request,))
        except Exception as exc:
            logger.warning("App workflow Cloud Advisor consultation failed: %s", type(exc).__name__)
            return "provider_error", "", "provider_request_failed", 0

        status = "ok"
        advisory = ""
        detail = ""
        if hasattr(value, "status"):
            status_value = getattr(value, "status", "")
            status = getattr(status_value, "value", None) or str(status_value or "")
            advisory = str(getattr(value, "advisory_text", "") or "")
            if not advisory and isinstance(getattr(value, "design", None), Mapping):
                advisory = json.dumps(getattr(value, "design"), ensure_ascii=False)
            detail = str(getattr(value, "detail_code", "") or "")
            used = int(getattr(value, "consultations_used", 1) or 0)
        elif isinstance(value, Mapping):
            status = str(value.get("status") or "ok")
            advisory = str(value.get("advisory_text", value.get("advice", value.get("text", ""))) or "")
            if not advisory and isinstance(value.get("design"), Mapping):
                advisory = json.dumps(value["design"], ensure_ascii=False)
            detail = str(value.get("detail_code", value.get("detail", "")) or "")
            used = int(value.get("consultations_used", value.get("consultations", 1)) or 0)
        else:
            advisory = str(value or "")
            used = 1 if advisory else 0
        return status, advisory[:MAX_ADVISORY_CHARS], detail, used

    async def _prepare_workspace(
        self,
        *,
        workspace: str | os.PathLike[str] | None,
        workspace_root: str | os.PathLike[str] | None = None,
        app_id: str | None,
        session: Any,
        owner_user_id: Any,
        app_name: str,
        slug: str,
        description: str,
        project_id: Any = None,
    ) -> tuple[Path, str | None, Any | None, bool]:
        created_persistent = False
        app_obj: Any | None = None
        resolved_app_id = str(app_id) if app_id else None
        if workspace is not None:
            selected = Path(workspace).expanduser().resolve()
            if workspace_root is not None:
                try:
                    selected.relative_to(Path(workspace_root).expanduser().resolve())
                except ValueError as exc:
                    raise ValueError("App workflow workspace is outside workspace_root") from exc
            if app_id:
                try:
                    managed = ensure_app_workspace(
                        str(app_id),
                        name=app_name,
                        description=description,
                        workspace_root=workspace_root or self.workspace_root,
                    ).resolve()
                except Exception as exc:
                    raise ValueError("persistent App workspace is invalid") from exc
                if selected != managed:
                    raise ValueError("persistent App workspace does not match App identity")
            selected.mkdir(parents=True, exist_ok=True)
            return selected, resolved_app_id, app_obj, created_persistent

        if self.app_context_callback is not None:
            value = await _invoke_callback(
                self.app_context_callback,
                kwargs={
                    "app_id": resolved_app_id,
                    "name": app_name,
                    "slug": slug,
                    "description": description,
                    "session": session,
                    "owner_user_id": owner_user_id,
                    "project_id": project_id,
                },
                positional=(),
            )
            if isinstance(value, Mapping):
                selected_value = value.get("workspace") or value.get("workspace_path")
                resolved_app_id = str(value.get("app_id") or resolved_app_id or "") or None
                app_obj = value.get("app")
            else:
                selected_value = getattr(value, "workspace", None) or value
                resolved_app_id = str(getattr(value, "app_id", None) or resolved_app_id or "") or None
                app_obj = getattr(value, "app", None)
            if selected_value is None:
                raise ValueError("App context callback did not return a workspace")
            selected = Path(selected_value).expanduser().resolve()
            if workspace_root is not None:
                try:
                    selected.relative_to(Path(workspace_root).expanduser().resolve())
                except ValueError as exc:
                    raise ValueError("App context workspace is outside workspace_root") from exc
            if resolved_app_id:
                try:
                    managed = ensure_app_workspace(
                        resolved_app_id,
                        name=app_name,
                        description=description,
                        workspace_root=workspace_root or self.workspace_root,
                    ).resolve()
                except Exception as exc:
                    raise ValueError("persistent App workspace is invalid") from exc
                if selected != managed:
                    raise ValueError("persistent App context workspace does not match App identity")
            selected.mkdir(parents=True, exist_ok=True)
            return selected, resolved_app_id, app_obj, True

        if self.app_service is not None and session is not None and owner_user_id is not None and not resolved_app_id:
            creator = getattr(self.app_service, "create_app", None)
            if callable(creator):
                app_obj = await _invoke_callback(
                    creator,
                    kwargs={
                        "session": session,
                        "owner_user_id": owner_user_id,
                        "name": app_name,
                        "slug": slug,
                        "description": description,
                        "origin_project_id": project_id,
                        "visibility": "private",
                    },
                    positional=(),
                )
                resolved_app_id = str(getattr(app_obj, "id", None) or "") or None
                created_persistent = True
                if resolved_app_id and project_id is not None:
                    # The generic AppService creator owns App/Manifest/Git
                    # setup but intentionally does not guess Project bindings.
                    # This workflow has an authenticated project scope, so add
                    # the same development binding as the controller callback
                    # before persistent source writes begin.
                    try:
                        from uuid import UUID
                        from sqlalchemy import select
                        from ..memory.models import ProjectApp

                        project_uuid = UUID(str(project_id))
                        existing_binding = await session.scalar(
                            select(ProjectApp)
                            .where(
                                ProjectApp.project_id == project_uuid,
                                ProjectApp.app_id == UUID(resolved_app_id),
                            )
                            .limit(1)
                        )
                        if existing_binding is None:
                            session.add(
                                ProjectApp(
                                    project_id=project_uuid,
                                    app_id=UUID(resolved_app_id),
                                    binding_mode="development",
                                    created_by=owner_user_id,
                                )
                            )
                            await session.flush()
                    except Exception as exc:
                        raise PermissionError("persistent App Project binding could not be created") from exc

        if resolved_app_id:
            effective_root = workspace_root if workspace_root is not None else self.workspace_root
            if app_obj is None and self.app_service is not None and session is not None:
                getter = getattr(self.app_service, "get_app", None)
                if callable(getter):
                    try:
                        app_obj = await _invoke_callback(
                            getter,
                            kwargs={"session": session, "app_id": uuid.UUID(resolved_app_id)},
                            positional=(session, uuid.UUID(resolved_app_id)),
                        )
                    except Exception:
                        # A pure workspace caller may pass an opaque app_id;
                        # inability to load an optional DB row must not grant
                        # it any extra authority.
                        app_obj = None
            try:
                selected = ensure_app_workspace(
                    resolved_app_id,
                    name=app_name,
                    description=description,
                    workspace_root=effective_root,
                )
            except (ValueError, AppStorageError) as exc:
                raise ValueError("App workspace could not be resolved") from exc
            return selected, resolved_app_id, app_obj, created_persistent

        # Pure workspace mode without an explicit path.  This branch is useful
        # for CLI callers but remains bounded and clearly disposable.
        root = Path(workspace_root).expanduser() if workspace_root is not None else self.workspace_root
        root = root or Path(tempfile.mkdtemp(prefix="aoitalk-app-workflow-"))
        root.mkdir(parents=True, exist_ok=True)
        safe_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug).strip("-")[:80] or f"app-{uuid.uuid4().hex[:8]}"
        selected = (root / safe_slug).resolve()
        selected.mkdir(parents=True, exist_ok=True)
        return selected, resolved_app_id, app_obj, created_persistent

    def _write_file(self, workspace: Path, relative: str, content: str | bytes) -> None:
        safe = _safe_relative_path(relative)
        if isinstance(content, bytes):
            if len(content) > MAX_GENERATED_FILE_BYTES:
                raise ValueError("generated App file exceeds size limit")
        elif len(str(content)) > MAX_GENERATED_FILE_CHARS:
            raise ValueError("generated App file exceeds size limit")
        path = resolve_workspace_file(workspace, safe)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            if isinstance(content, bytes):
                temp.write_bytes(content)
            else:
                temp.write_text(str(content), encoding="utf-8", newline="\n")
            os.replace(temp, path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    async def _persist_generated_files(
        self,
        *,
        app_id: str,
        owner_user_id: Any,
        project_id: Any,
        db_session: Any = None,
        workspace: Path,
        files: Mapping[str, Any],
        workspace_root: str | os.PathLike[str] | None,
    ) -> tuple[str, ...]:
        """Write persistent App files through the existing transaction boundary."""

        from uuid import UUID

        from sqlalchemy import select

        from ..memory.database import get_database_manager
        from ..memory.models import App, ProjectApp, User
        from .app_manifest_service import (
            parse_manifest_text,
            sync_manifest_targets_unlocked,
            validate_manifest_workspace,
        )
        from .app_operation_lock import app_operation_lock
        from .app_service import AppAccessError, AppService
        from ..tools.apps import AppWriteTransaction
        from .turn_context import get_turn_context

        try:
            app_uuid = UUID(str(app_id))
            owner_uuid = UUID(str(owner_user_id))
            project_uuid = UUID(str(project_id)) if project_id is not None else None
        except (TypeError, ValueError) as exc:
            raise PermissionError("persistent App scope is invalid") from exc
        active_turn = get_turn_context()
        active_user = str(getattr(active_turn, "user_id", "") or "").strip()
        if not active_user or active_user.casefold() == "default_user" or active_user != str(owner_uuid):
            raise PermissionError("persistent App identity is not bound to the current turn")
        manager = get_database_manager()
        owns_session = db_session is None
        session = db_session or await manager.get_session()
        try:
            app = await session.scalar(select(App).where(App.id == app_uuid).limit(1))
            starter = await session.scalar(select(User).where(User.id == owner_uuid).limit(1))
            if app is None or starter is None or starter.is_active is not True:
                raise PermissionError("persistent App owner is invalid")
            service = AppService(workspace_root=workspace_root)
            try:
                await service.require_permission(
                    session,
                    app,
                    user_id=owner_uuid,
                    required="developer",
                    user_role=str(starter.role or "user"),
                    project_id=project_uuid,
                )
            except (AppAccessError, PermissionError) as exc:
                raise PermissionError("persistent App write access is denied") from exc
            if project_uuid is not None:
                binding = await session.scalar(
                    select(ProjectApp)
                    .where(
                        ProjectApp.project_id == project_uuid,
                        ProjectApp.app_id == app_uuid,
                        ProjectApp.enabled.is_(True),
                    )
                    .limit(1)
                )
                if binding is None:
                    raise PermissionError("persistent App Project binding is disabled")
                if str(binding.binding_mode or "development").casefold() != "development":
                    raise PermissionError("installed App bindings are immutable")
            normalized_files: dict[str, str | bytes] = {}
            for relative, content in list(files.items())[:32]:
                safe_relative = _safe_relative_path(relative)
                if isinstance(content, bytes):
                    normalized_files[safe_relative] = content
                else:
                    normalized_files[safe_relative] = str(content)
            app_workspace = workspace.resolve()
            async with app_operation_lock(app_uuid, workspace_root=workspace_root):
                async with AppWriteTransaction(
                    session,
                    app_uuid,
                    workspace=app_workspace,
                    workspace_root=str(workspace_root) if workspace_root else None,
                ) as transaction:
                    transaction.stash(*normalized_files.keys())
                    for relative, content in normalized_files.items():
                        target = resolve_app_file(app_uuid, relative, workspace_root=workspace_root)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if isinstance(content, bytes):
                            target.write_bytes(content)
                        else:
                            target.write_text(content, encoding="utf-8", newline="\n")
                    manifest_path = resolve_app_file(
                        app_uuid,
                        "aoitalk.app.yaml",
                        workspace_root=workspace_root,
                    )
                    if manifest_path.exists():
                        validate_manifest_workspace(
                            parse_manifest_text(manifest_path.read_text(encoding="utf-8")),
                            app_workspace,
                        )
                        await sync_manifest_targets_unlocked(session, app, app_workspace)
                    if "README.md" in normalized_files:
                        await service.sync_readme_to_node(session, app, owner_uuid)
                    await transaction.commit()
                    transaction.checkpoint("App workflow generated source", actor=owner_uuid)
            return tuple(sorted(normalized_files))
        finally:
            if owns_session:
                await session.close()

    @staticmethod
    def _default_manifest(*, name: str, description: str, entrypoint: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": name[:255] or "AoiTalk App",
            "description": description[:2_000],
            "overview": {"purpose": description[:700] or "Deterministic App/Macro workflow"},
            "targets": {
                "aoitalk": {
                    "display_name": "AoiTalk",
                    "surface": "headless",
                    "runtime": "python",
                    "execution_host": "aoitalk",
                    "entrypoint": entrypoint,
                    "test": {"command": "python -m pytest tests -q"},
                    "run": {"command": f"python {entrypoint}"},
                }
            },
        }

    @staticmethod
    def _default_source(design: AppDesignSpec | None = None) -> str:
        rule_note = ""
        if design is not None:
            safe_rules = [str(item)[:120] for item in design.rules[:8]]
            rule_note = f"\n# Local implementation derived from design rules: {json.dumps(safe_rules, ensure_ascii=True)}\n"
        base = '''"""Generated local App/Macro entrypoint."""from __future__ import annotations\n\nimport json\nimport re\nimport sys\nfrom typing import Any\n\n\n_ERROR_RE = re.compile(r"(?i)\\b(?:error|critical|fail(?:ed|ure)?|fatal|ng|異常|失敗|down|unreachable)\\b")\n_HEALTHY_RE = re.compile(r"(?i)\\b(?:ok|success(?:ful)?|healthy|up|正常|成功|reachable)\\b")\n\n\ndef classify(value: Any) -> dict[str, Any]:\n    """Classify bounded config/log input without executing its contents."""\n    if isinstance(value, dict):\n        parts = []\n        for key in ("text", "log", "config", "content", "message"):\n            if value.get(key) is not None:\n                parts.append(str(value[key]))\n        text = "\\n".join(parts) if parts else json.dumps(value, ensure_ascii=False)\n    else:\n        text = str(value or "")\n    text = text[:120_000]\n    if not text.strip():\n        return {"status": "NG", "reason": "入力が空です"}\n    errors = _ERROR_RE.findall(text)\n    healthy = _HEALTHY_RE.findall(text)\n    if errors:\n        return {"status": "NG", "reason": "エラーまたは異常状態を検出しました", "markers": sorted(set(errors))[:8]}\n    if healthy:\n        return {"status": "OK", "reason": "正常状態を確認しました", "markers": sorted(set(healthy))[:8]}\n    return {"status": "NG", "reason": "正常性を確認できるマーカーがありません"}\n\n\ndef main() -> None:\n    raw = sys.stdin.read()\n    try:\n        value = json.loads(raw) if raw.strip() else {}\n    except json.JSONDecodeError:\n        value = {"text": raw}\n    print(json.dumps(classify(value), ensure_ascii=False))\n\n\nif __name__ == "__main__":\n    main()\n'''
        return base.replace('\"\"\"Generated local App/Macro entrypoint.\"\"\"', '\"\"\"Generated local App/Macro entrypoint.\"\"\"' + rule_note, 1)


    @staticmethod
    def _default_tests() -> str:
        return '''from src.main import classify\n\n\ndef test_ok_log():\n    result = classify({"text": "INFO interface up\\ncheck: OK\\nlatency=12ms"})\n    assert result["status"] == "OK"\n\n\ndef test_ng_log():\n    result = classify({"text": "INFO start\\nERROR peer unreachable\\nretry=2"})\n    assert result["status"] == "NG"\n    assert result["reason"]\n\n\ndef test_empty_input():\n    assert classify({})["status"] == "NG"\n'''

    async def _local_main_implementation(
        self,
        *,
        model: Any,
        problem: AppProblemIR,
        design: AppDesignSpec,
    ) -> Mapping[str, Any] | None:
        """Request bounded source files from the configured local Main model.

        The response is advisory data only until relative paths and sizes are
        validated.  Files are subsequently written through ``_write_file``;
        no model-controlled command or tool is executed.
        """

        if model is None:
            return None
        provider = str(getattr(model, "provider_label", "") or "").strip().casefold()
        if not provider:
            return None
        if provider not in {
            "ollama",
            "openai_compatible_local",
            "llama_cpp",
            "llama-cpp",
            "sglang",
            "local",
            "local-model",
        }:
            return None
        try:
            from .outbound_privacy_service import provider_classification

            if provider_classification(
                provider,
                base_url=str(getattr(model, "base_url", "") or ""),
                trusted_local_hosts=(),
            ) != "local":
                return None
        except Exception:
            return None
        generator = (
            getattr(model, "generate_plain_text_async", None)
            or getattr(model, "generate_response_async", None)
            or getattr(model, "generate_async", None)
        )
        if not callable(generator):
            return None
        prompt = _bounded_json_text(
            {
                "schema": "aoitalk.app_local_source.v1",
                "task": (
                    "Implement the App/Macro design locally. Return JSON only with "
                    "a files mapping of relative source/test paths to UTF-8 text. "
                    "Do not execute tools, include credentials, or embed input evidence."
                ),
                "problem": problem.to_cloud_projection(),
                "design": design.to_dict(),
                "required_files": [design.entrypoint, "tests/test_main.py"],
            },
        )
        try:
            value = generator(prompt)
            if inspect.isawaitable(value):
                value = await asyncio.wait_for(value, timeout=45.0)
            parsed = _json_object(str(value or ""))
            if not isinstance(parsed, Mapping):
                return None
            files = parsed.get("files") or parsed.get("source_files")
            if not isinstance(files, Mapping):
                return None
            bounded: dict[str, str] = {}
            for relative, content in list(files.items())[:16]:
                if not isinstance(relative, str) or not isinstance(content, str):
                    continue
                try:
                    safe_relative = _safe_relative_path(relative)
                except ValueError:
                    continue
                if not safe_relative.endswith((".py", ".js", ".ts", ".json", ".yaml", ".yml", ".md")):
                    continue
                if len(content) > MAX_GENERATED_FILE_CHARS:
                    continue
                bounded[safe_relative] = content
            if design.entrypoint not in bounded:
                return None
            return bounded
        except Exception as exc:
            logger.warning(
                "local Main App implementation failed exception_type=%s",
                type(exc).__name__,
            )
            return None

    async def _implement(
        self,
        *,
        problem: AppProblemIR,
        design: AppDesignSpec,
        workspace: Path,
        callback: Callable[..., Any] | None,
        main_model: Any | None = None,
        app_id: str | None = None,
        session: Any = None,
        project_id: Any = None,
        owner_user_id: Any = None,
        workspace_root: str | os.PathLike[str] | None = None,
        failure: str = "",
        attempt: int = 0,
    ) -> tuple[str, ...]:
        workspace.mkdir(parents=True, exist_ok=True)

        async def write_files(files: Mapping[str, Any]) -> tuple[str, ...]:
            if app_id:
                if owner_user_id is None:
                    raise PermissionError("persistent App owner identity is required")
                return await self._persist_generated_files(
                    app_id=app_id,
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    db_session=session,
                    workspace=workspace,
                    files=files,
                    workspace_root=workspace_root,
                )
            written: list[str] = []
            for relative, content in files.items():
                self._write_file(workspace, str(relative), content)
                written.append(_safe_relative_path(str(relative)))
            return tuple(sorted(set(written)))

        if callback is not None:
            result = await _invoke_callback(
                callback,
                kwargs={
                    "problem": problem,
                    "problem_ir": problem,
                    "design": design,
                    "design_spec": design,
                    "workspace": workspace,
                    "workspace_path": workspace,
                    "failure": failure,
                    "attempt": attempt,
                    "kind": problem.kind,
                },
                positional=(design, workspace),
            )
            files: MutableMapping[str, Any] = {}
            if isinstance(result, Mapping):
                for key in ("files", "source_files"):
                    if isinstance(result.get(key), Mapping):
                        files.update(result[key])
                if isinstance(result.get("manifest"), Mapping):
                    files.setdefault("aoitalk.app.yaml", yaml.safe_dump(result["manifest"], allow_unicode=True, sort_keys=False))
                if result.get("entrypoint_content") is not None:
                    files.setdefault(design.entrypoint, result["entrypoint_content"])
                if result.get("code") is not None:
                    files.setdefault(design.entrypoint, result["code"])
            else:
                object_files = getattr(result, "files", None)
                if isinstance(object_files, Mapping):
                    files.update(object_files)
                object_code = getattr(result, "code", None)
                if object_code is not None:
                    files.setdefault(design.entrypoint, object_code)
            if files:
                return await write_files(files)
            # Callback may have written files itself.
            return tuple(self._workspace_source_files(workspace))

        local_files = await self._local_main_implementation(
            model=main_model,
            problem=problem,
            design=design,
        )
        if local_files:
            manifest = self._default_manifest(
                name=f"AoiTalk {problem.kind.title()}",
                description="System-owned AoiTalk workflow App",
                entrypoint=design.entrypoint,
            )
            generated_files: dict[str, Any] = {
                "aoitalk.app.yaml": yaml.safe_dump(
                    manifest,
                    allow_unicode=True,
                    sort_keys=False,
                ),
                "src/__init__.py": "",
                **dict(local_files),
            }
            if "tests/test_main.py" not in local_files:
                generated_files["tests/test_main.py"] = self._default_tests()
            generated_files["README.md"] = f"# {manifest['name']}\n\nLocal Main implementation\n"
            files_written = await write_files(generated_files)
            self._last_implementation_source = "local_main"
            return files_written

        manifest = self._default_manifest(
            name=f"AoiTalk {problem.kind.title()}",
            description="System-owned AoiTalk workflow App",
            entrypoint=design.entrypoint,
        )
        generated_files = {
            "aoitalk.app.yaml": yaml.safe_dump(
                manifest,
                allow_unicode=True,
                sort_keys=False,
            ),
            # Keep the generated source importable when the existing App test
            # runner executes from the workspace root on Python 3.12.
            "src/__init__.py": "",
            design.entrypoint: self._default_source(design),
            "tests/test_main.py": self._default_tests(),
            "README.md": f"# {manifest['name']}\n\nSystem-owned AoiTalk workflow App\n",
        }
        return await write_files(generated_files)

    @staticmethod
    def _workspace_source_files(workspace: Path) -> list[str]:
        result: list[str] = []
        for path in workspace.rglob("*"):
            if (
                not path.is_file()
                or ".git" in path.parts
                or ".pytest_cache" in path.parts
                or "__pycache__" in path.parts
            ):
                continue
            try:
                relative = path.relative_to(workspace).as_posix()
                if is_private_app_path(relative):
                    continue
            except (OSError, ValueError):
                continue
            result.append(relative)
        return sorted(result)

    @staticmethod
    def _safe_job_result(problem: AppProblemIR, value: Mapping[str, Any] | None) -> dict[str, Any]:
        """Project an existing AppJob row without leaking local paths/input."""

        if not isinstance(value, Mapping):
            return {"ok": False, "status": "unknown"}
        result: dict[str, Any] = {
            "ok": str(value.get("status") or "").casefold() == "succeeded",
            "status": _clip(value.get("status"), 40),
            "job_type": _clip(value.get("job_type"), 20),
            "exit_code": value.get("exit_code"),
            "job_id": _clip(value.get("id"), 80),
        }
        log_path = value.get("log_path")
        if log_path:
            # A basename is enough to correlate the durable job; the absolute
            # workspace path is an implementation detail of the local runner.
            result["log_name"] = Path(str(log_path)).name[:160]
        result_json = value.get("result_json")
        if isinstance(result_json, Mapping):
            result["result"] = _safe_result_mapping(problem, result_json)
        return result

    async def _execute_existing_job(
        self,
        *,
        app_id: str | None,
        project_id: Any,
        owner_user_id: Any,
        target_key: str,
        job_type: str,
        input_json: Mapping[str, Any] | None,
        workspace_root: str | os.PathLike[str] | None,
        problem: AppProblemIR,
    ) -> dict[str, Any] | None:
        """Run a durable AppJob through the existing Apps execution service."""

        if not app_id:
            return None
        if owner_user_id is None:
            return {
                "ok": False,
                "status": "failed",
                "error": "persistent_app_owner_missing",
            }
        if job_type not in {"test", "run"}:
            return {"ok": False, "status": "failed", "error": "unsupported_app_job_type"}
        try:
            from uuid import UUID
            from sqlalchemy import select

            from ..memory.database import get_database_manager
            from ..memory.models import App, AppJob, AppTarget, ProjectApp, User
            from .app_job_service import execute_app_job
            from .app_service import AppAccessError, AppService
            from .turn_context import get_turn_context

            app_uuid = UUID(str(app_id))
            project_uuid = UUID(str(project_id)) if project_id is not None else None
            owner_uuid = UUID(str(owner_user_id))
        except (TypeError, ValueError, ImportError):
            return {"ok": False, "status": "failed", "error": "persistent_app_scope_invalid"}
        active_turn = get_turn_context()
        active_user = str(getattr(active_turn, "user_id", "") or "").strip()
        if not active_user or active_user.casefold() == "default_user" or active_user != str(owner_uuid):
            return {"ok": False, "status": "failed", "error": "persistent_app_identity_mismatch"}

        manager = get_database_manager()
        try:
            session = await manager.get_session()
        except Exception:
            return {"ok": False, "status": "failed", "error": "persistent_app_database_unavailable"}
        job_id: str | None = None
        try:
            app = await session.scalar(select(App).where(App.id == app_uuid).limit(1))
            target = await session.scalar(
                select(AppTarget)
                .where(
                    AppTarget.app_id == app_uuid,
                    AppTarget.target_key == str(target_key or "aoitalk"),
                )
                .limit(1)
            )
            if app is None or target is None:
                return {"ok": False, "status": "failed", "error": "persistent_app_target_missing"}
            starter = await session.scalar(
                select(User).where(User.id == owner_uuid).limit(1)
            )
            if starter is None or starter.is_active is not True:
                return {"ok": False, "status": "failed", "error": "persistent_app_owner_invalid"}
            service = AppService(workspace_root=workspace_root)
            required_permission = "developer" if job_type == "test" else "runner"
            try:
                await service.require_permission(
                    session,
                    app,
                    user_id=owner_uuid,
                    required=required_permission,
                    user_role=str(starter.role or "user"),
                    project_id=project_uuid,
                )
            except (AppAccessError, PermissionError):
                return {"ok": False, "status": "failed", "error": "persistent_app_access_denied"}
            if project_uuid is not None:
                binding = await session.scalar(
                    select(ProjectApp)
                    .where(
                        ProjectApp.project_id == project_uuid,
                        ProjectApp.app_id == app_uuid,
                        ProjectApp.enabled.is_(True),
                    )
                    .limit(1)
                )
                if binding is None:
                    return {"ok": False, "status": "failed", "error": "persistent_app_binding_missing"}
                if str(binding.binding_mode or "development").casefold() != "development":
                    return {"ok": False, "status": "failed", "error": "persistent_app_binding_immutable"}
            job = AppJob(
                app_id=app_uuid,
                target_id=target.id,
                project_id=project_uuid,
                job_type=str(job_type),
                status="queued",
                input_json=dict(input_json or {}),
                started_by=owner_uuid,
            )
            session.add(job)
            await session.commit()
            job_id = str(job.id)
        finally:
            await session.close()
        if not job_id:
            return None

        try:
            result = await execute_app_job(
                manager,
                job_id,
                workspace_root=workspace_root,
                timeout_seconds=self.command_timeout_seconds,
                deployment_config=(
                    self.config.config
                    if hasattr(self.config, "config")
                    and isinstance(getattr(self.config, "config", None), Mapping)
                    else self.config
                ),
            )
        except Exception:
            return {"ok": False, "status": "failed", "error": "persistent_app_job_failed"}
        return self._safe_job_result(problem, result)

    async def _run_test(
        self,
        *,
        workspace: Path,
        problem: AppProblemIR,
        design: AppDesignSpec,
        callback: Callable[..., Any] | None,
        app_id: str | None = None,
        project_id: Any = None,
        owner_user_id: Any = None,
        workspace_root: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        if callback is not None:
            value = await _invoke_callback(
                callback,
                kwargs={"problem": problem, "design": design, "workspace": workspace, "workspace_path": workspace},
                positional=(workspace, design),
            )
            return _safe_result_mapping(problem, _normalize_callback_result(value))
        if app_id:
            persistent = await self._execute_existing_job(
            app_id=app_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            target_key=design.target_key,
            job_type="test",
            input_json={},
            workspace_root=workspace_root,
            problem=problem,
            )
            return persistent or {
                "ok": False,
                "status": "failed",
                "error": "persistent_app_job_unavailable",
            }
        command = [sys.executable, "-m", "pytest", "tests", "-q"]
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=self.command_timeout_seconds,
                check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            return {
                "ok": completed.returncode == 0,
                "status": "succeeded" if completed.returncode == 0 else "failed",
                "exit_code": completed.returncode,
                "output": _safe_failure_text(problem, output),
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "status": "failed", "error": _safe_failure_text(problem, exc)}

    async def _local_main_design(
        self,
        *,
        model: Any,
        problem: AppProblemIR,
        design: AppDesignSpec,
    ) -> AppDesignSpec | None:
        """Optionally let the verified local Main model refine implementation.

        Hosted clients are explicitly excluded.  The prompt is the already
        masked IR, while raw evidence remains available only to the local
        deterministic runner/callback.  A timeout or malformed response keeps
        the validated Cloud/fallback design and never retries another route.
        """

        if model is None:
            return None
        provider = str(getattr(model, "provider_label", "") or "").strip().casefold()
        if not provider:
            return None
        if provider not in {
            "ollama",
            "openai_compatible_local",
            "llama_cpp",
            "llama-cpp",
            "sglang",
            "local",
            "local-model",
        }:
            return None
        try:
            from .outbound_privacy_service import provider_classification

            if provider_classification(
                provider,
                base_url=str(getattr(model, "base_url", "") or ""),
                trusted_local_hosts=(),
            ) != "local":
                return None
        except Exception:
            return None
        generator = (
            getattr(model, "generate_plain_text_async", None)
            or getattr(model, "generate_response_async", None)
            or getattr(model, "generate_async", None)
        )
        if not callable(generator):
            return None
        prompt = _bounded_json_text(
            {
                "schema": "aoitalk.app_local_implementation.v1",
                "task": "Refine this App design for local implementation. Return JSON fields only; do not execute tools or include raw source.",
                "problem": problem.to_cloud_projection(),
                "design": design.to_dict(),
            },
        )
        try:
            value = generator(prompt)
            if inspect.isawaitable(value):
                value = await asyncio.wait_for(value, timeout=30.0)
            parsed = _json_object(str(value or ""))
            if not parsed:
                return None
            refined = AppDesignSpec.from_advisory(parsed, problem)
            return replace(refined, source_of_advice="local_main")
        except Exception as exc:
            logger.warning(
                "local Main App design refinement failed exception_type=%s",
                type(exc).__name__,
            )
            return None

    async def _run_app(
        self,
        *,
        workspace: Path,
        problem: AppProblemIR,
        design: AppDesignSpec,
        callback: Callable[..., Any] | None,
        app_id: str | None = None,
        project_id: Any = None,
        owner_user_id: Any = None,
        workspace_root: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        if callback is not None:
            value = await _invoke_callback(
                callback,
                kwargs={"problem": problem, "design": design, "workspace": workspace, "workspace_path": workspace, "input": problem.local_material()},
                positional=(workspace, design),
            )
            return _safe_result_mapping(problem, _normalize_callback_result(value))
        if app_id:
            persistent = await self._execute_existing_job(
            app_id=app_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            target_key=design.target_key,
            job_type="run",
            input_json={"text": problem.local_material()[:MAX_INPUT_CHARS]},
            workspace_root=workspace_root,
            problem=problem,
            )
            return persistent or {
                "ok": False,
                "status": "failed",
                "error": "persistent_app_job_unavailable",
            }
        input_value = {"text": problem.local_material()[:MAX_INPUT_CHARS]}
        command = [sys.executable, design.entrypoint]
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=str(workspace),
                input=json.dumps(input_value, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=self.command_timeout_seconds,
                check=False,
            )
            output = (completed.stdout or "").strip()
            result: dict[str, Any] = {"ok": completed.returncode == 0, "status": "succeeded" if completed.returncode == 0 else "failed", "exit_code": completed.returncode, "output": _safe_failure_text(problem, output)}
            if output:
                try:
                    parsed = json.loads(output.splitlines()[-1])
                    if isinstance(parsed, Mapping):
                        # Public result metadata must not become a side
                        # channel for a generated App echoing local values.
                        try:
                            result["result"] = json.loads(
                                _safe_failure_text(problem, parsed)
                            )
                        except (TypeError, ValueError, json.JSONDecodeError):
                            result["result"] = {"status": str(parsed.get("status") or "")}
                except (json.JSONDecodeError, IndexError):
                    pass
            return result
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "status": "failed", "error": _safe_failure_text(problem, exc)}

    async def _sync_persistent_manifest(self, *, app: Any, app_id: str | None, session: Any, workspace: Path) -> bool:
        owned_session = False
        if session is None and app_id:
            # Chat's persistent App context adapter may have committed the
            # initial App before source generation. Re-open a short DB
            # transaction to sync the final Manifest/Targets through the
            # existing Apps service boundary rather than leaving stale target
            # rows behind. Failure remains best-effort for pure workspaces.
            try:
                from uuid import UUID

                from ..memory.database import get_database_manager
                from ..memory.models import App

                manager = get_database_manager()
                session = await manager.get_session()
                owned_session = True
                app = await session.get(App, UUID(str(app_id)))
            except Exception:
                session = None
                app = None
        if session is None or app is None:
            # Pure workspace mode has no Manifest boundary to update.  A
            # persistent App without a loaded row must fail closed rather
            # than running against stale scaffold targets.
            return not bool(app_id)
        succeeded = False
        try:
            from .app_manifest_service import sync_manifest_targets

            await sync_manifest_targets(session, app, workspace)
            flush = getattr(session, "flush", None)
            if callable(flush):
                await flush()
            if owned_session:
                commit = getattr(session, "commit", None)
                if callable(commit):
                    await commit()
            succeeded = True
        except Exception as exc:
            # Existing API transaction remains authoritative.  Persistent
            # execution must stop when target sync fails; continuing would run
            # stale/invalid Manifest rows through AppJobService.
            logger.warning("App workflow Manifest target sync failed app=%s: %s", app_id, type(exc).__name__)
        finally:
            if owned_session:
                try:
                    await session.close()
                except Exception:
                    pass
        return succeeded

    async def execute(
        self,
        request: str | None = None,
        *,
        kind: str | None = None,
        inputs: Mapping[str, Any] | Sequence[Any] | None = None,
        files: Mapping[str, Any] | Sequence[Any] | None = None,
        problem: AppProblemIR | Mapping[str, Any] | None = None,
        workspace: str | os.PathLike[str] | None = None,
        app_id: str | None = None,
        session: Any = None,
        owner_user_id: Any = None,
        user_id: Any = None,
        project_id: Any = None,
        session_id: str | None = None,
        main_model: Any | None = None,
        output_dir: str | os.PathLike[str] | None = None,
        workspace_root: str | os.PathLike[str] | None = None,
        allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
        context: Any | None = None,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
        attachments: Mapping[str, Any] | Sequence[Any] | None = None,
        coordinator: Any | None = None,
        cloud_advisor: Any | None = None,
        app_name: str | None = None,
        slug: str | None = None,
        description: str | None = None,
        cloud_consent: bool = False,
        run_tests: bool = True,
        run_app: bool = False,
        test: bool | None = None,
        run: bool | None = None,
        explicit_cloud_consent: bool | None = None,
        raw_inputs: Mapping[str, Any] | Sequence[Any] | None = None,
        implementation_callback: Callable[..., Any] | None = None,
        test_callback: Callable[..., Any] | None = None,
        run_callback: Callable[..., Any] | None = None,
        repair_callback: Callable[..., Any] | None = None,
        max_repair_attempts: int | None = None,
        progress_callback: Callable[[str, Mapping[str, Any]], Any] | None = None,
        **_: Any,
    ) -> AppBuildResult:
        self._last_implementation_source = "deterministic_fallback"
        if inputs is None and raw_inputs is not None:
            inputs = raw_inputs
        if test is not None:
            run_tests = bool(test)
        if run is not None:
            run_app = bool(run)
        if explicit_cloud_consent is not None:
            cloud_consent = bool(explicit_cloud_consent)
        intent = detect_app_workflow_intent(request or "", attachments=files)
        kind_token = str(kind or (intent.kind if intent else "app")).casefold().lstrip("/")
        selected_kind = "macro" if kind_token == "macro" else "app"
        if files is None and attachments is not None:
            files = attachments
        input_mapping: Mapping[str, Any] | None
        input_sequence: Sequence[Any] | None
        if isinstance(inputs, Mapping):
            input_mapping, input_sequence = inputs, None
        elif inputs is None:
            input_mapping, input_sequence = None, None
        else:
            input_mapping, input_sequence = None, inputs
        if problem is None:
            effective_roots = allowed_roots
            if effective_roots is None and workspace_root:
                effective_roots = (workspace_root,)
            problem_ir = AppProblemIR.from_inputs(
                kind=selected_kind,
                goal=request or "",
                inputs=input_mapping,
                files=(files if files is not None else input_sequence),
                allowed_roots=effective_roots,
                base_root=workspace_root,
            )
        elif isinstance(problem, AppProblemIR):
            problem_ir = problem
            selected_kind = problem.kind
        else:
            problem_ir = AppProblemIR.from_dict(problem)
            selected_kind = problem_ir.kind

        # Bind raw evidence to the shared foundation's opaque, turn-scoped
        # references.  The local regex context above creates the conservative
        # text projection; the foundation context adds lifecycle/stale-ID
        # protection without introducing another provider or policy path.
        foundation_context: Any | None = None
        own_foundation_context = False
        try:
            from .workflow_foundation import ProtectedWorkflowContext

            foundation_context = context
            if foundation_context is None:
                foundation_context = ProtectedWorkflowContext(
                    workflow_kind=f"app_{selected_kind}",
                    config=self.config,
                    session_context=session_context,
                    project_metadata=project_metadata,
                    max_nodes=4096,
                    max_items=512,
                )
                own_foundation_context = True
            registration_count = 0
            registration_complete = True
            expected_chunks = 0
            for raw in problem_ir.raw_material[:64]:
                if isinstance(raw, str) and raw:
                    expected_chunks += max(1, (len(raw) + 7_999) // 8_000)
            if len(problem_ir.raw_material) > 64 or expected_chunks > 128:
                registration_complete = False
            for index, raw in enumerate(problem_ir.raw_material[:64], start=1):
                if not isinstance(raw, str) or not raw:
                    continue
                # Foundation bindings are bounded strings.  Chunk long log or
                # config bodies so every segment remains covered by the
                # canonical scrub rather than silently falling back to a raw
                # suffix.
                for chunk_index in range(0, len(raw), 8_000):
                    if registration_count >= 128:
                        break
                    chunk = raw[chunk_index : chunk_index + 8_000]
                    if not chunk:
                        continue
                    try:
                        foundation_context.register(
                            chunk,
                            kind="input",
                            role="app_evidence",
                            stable_key=(
                                f"{problem_ir.workflow_id}:input:{index}:{chunk_index // 8_000}"
                            ),
                            source_kind="app_workflow",
                        )
                        registration_count += 1
                    except Exception:
                        # Unsupported values remain local to the bounded
                        # evidence projection and force Cloud consultation to
                        # fail closed if no canonical refs are available.
                        registration_complete = False
                        continue
                if registration_count >= 128:
                    break
            if registration_count < expected_chunks:
                registration_complete = False
            problem_ir = problem_ir.with_foundation_context(
                foundation_context,
                complete=registration_complete,
            )
        except Exception:
            foundation_context = None

        owner = owner_user_id if owner_user_id is not None else user_id
        if app_id and owner is None:
            if foundation_context is not None and own_foundation_context:
                foundation_context.close()
            return AppBuildResult(
                status="failed",
                kind=selected_kind,
                workflow_id=problem_ir.workflow_id,
                app_id=app_id,
                problem=problem_ir,
                diagnostics=("persistent App owner identity is required",),
                intent=intent,
            )
        # Caller-provided name/slug can contain customer/project identifiers;
        # durable App metadata uses only a workflow-owned label and opaque ID.
        safe_name = f"AoiTalk {selected_kind.title()}"
        safe_slug = (
            f"aoitalk-{selected_kind}-workflow-"
            f"{problem_ir.workflow_id.replace('-', '')[:16]}"
        )

        async def progress(stage: str, status: str = "running", message: str = "") -> None:
            if not callable(progress_callback):
                return
            payload = {
                "workflow": "app",
                "stage": _clip(stage, 80),
                "status": _clip(status, 40),
                "message": _clip(message, 500),
            }
            try:
                callback_result = progress_callback(stage, payload)
                if inspect.isawaitable(callback_result):
                    await callback_result
            except Exception:
                logger.debug("App workflow progress callback failed", exc_info=True)

        await progress("started", "running", "App/Macroを処理しています")
        try:
            selected_workspace, resolved_app_id, app_obj, created_persistent = await self._prepare_workspace(
                workspace=workspace,
                workspace_root=workspace_root,
                app_id=app_id,
                session=session,
                owner_user_id=owner,
                app_name=safe_name,
                slug=safe_slug,
                # User request/source text is local evidence, never durable
                # App metadata.  Keep the persistent domain description
                # system-owned and non-sensitive.
                description="System-owned AoiTalk workflow App",
                project_id=project_id,
            )
        except Exception as exc:
            if foundation_context is not None and own_foundation_context:
                foundation_context.close()
            return AppBuildResult(
                status="failed",
                kind=selected_kind,
                workflow_id=problem_ir.workflow_id,
                app_id=app_id,
                problem=problem_ir,
                diagnostics=(f"workspace preparation failed: {type(exc).__name__}",),
                intent=intent,
            )

        try:
            cloud_status, advisory, detail, consultations = await self._consult_cloud(
                problem_ir,
                phase="design",
                consent=bool(cloud_consent),
                advisor_override=coordinator or cloud_advisor,
            )
        except Exception as exc:
            # An unsafe/ambiguous Cloud projection fails closed at the
            # provider boundary, but local deterministic implementation can
            # still proceed.  Never retry by sending the raw material.
            logger.warning("App workflow Cloud projection rejected: %s", type(exc).__name__)
            cloud_status, advisory, detail, consultations = (
                "privacy_blocked",
                "",
                f"cloud_projection_rejected:{type(exc).__name__}",
                0,
            )
        if advisory:
            design = AppDesignSpec.from_advisory(advisory, problem_ir)
        else:
            design = AppDesignSpec.fallback(problem_ir, source_of_advice="local_fallback")
        if main_model is not None:
            refined = await self._local_main_design(
                model=main_model,
                problem=problem_ir,
                design=design,
            )
            if refined is not None:
                design = refined
        await progress("advisory", "completed" if advisory else "fallback", "設計アドバイスを確認しました")
        diagnostics: list[str] = []
        if detail and detail not in {"disabled", "no_coordinator"}:
            diagnostics.append(detail)

        callback = implementation_callback or self.implementation_callback
        test_hook = test_callback or self.test_callback
        run_hook = run_callback or self.run_callback
        repair_hook = repair_callback or self.repair_callback
        repairs = self.max_repair_attempts if max_repair_attempts is None else max(0, min(int(max_repair_attempts), MAX_REPAIR_ATTEMPTS))
        test_result: Mapping[str, Any] | None = None
        run_result: Mapping[str, Any] | None = None
        files_written: tuple[str, ...] = ()
        attempts = 0
        for attempt in range(repairs + 1):
            attempts = attempt + 1
            try:
                files_written = await self._implement(
                    problem=problem_ir,
                    design=design,
                    workspace=selected_workspace,
                    callback=callback,
                    main_model=main_model,
                    app_id=resolved_app_id,
                    session=session,
                    project_id=project_id,
                    owner_user_id=owner,
                    workspace_root=workspace_root,
                    failure=(diagnostics[-1] if diagnostics else ""),
                    attempt=attempt,
                )
            except Exception as exc:
                test_result = {"ok": False, "status": "failed", "error": _safe_failure_text(problem_ir, exc)}
            else:
                if self._last_implementation_source == "local_main":
                    diagnostics.append("implementation_authority:local_main")
                await progress("implemented", "completed", "App sourceを書き込みました")
                # Refresh the existing App Manifest/Target rows before routing
                # test/run through AppJobService.  The persistent context starts
                # with a scaffold target; generated files must be validated and
                # snapshotted before a durable job can execute them.
                manifest_synced = await self._sync_persistent_manifest(
                    app=app_obj,
                    app_id=resolved_app_id,
                    session=session,
                    workspace=selected_workspace,
                )
                if resolved_app_id and not manifest_synced:
                    test_result = {
                        "ok": False,
                        "status": "failed",
                        "error": "persistent_app_manifest_sync_failed",
                    }
                elif run_tests:
                    await progress("test", "running", "Appのテストを実行しています")
                    test_result = await self._run_test(
                        workspace=selected_workspace,
                        problem=problem_ir,
                        design=design,
                        callback=test_hook,
                        app_id=resolved_app_id,
                        project_id=project_id,
                        owner_user_id=owner,
                        workspace_root=workspace_root,
                    )
                else:
                    test_result = {"ok": True, "status": "skipped"}
                if bool(test_result.get("ok")) and run_app:
                    await progress("run", "running", "Appを実行しています")
                    run_result = await self._run_app(
                        workspace=selected_workspace,
                        problem=problem_ir,
                        design=design,
                        callback=run_hook,
                        app_id=resolved_app_id,
                        project_id=project_id,
                        owner_user_id=owner,
                        workspace_root=workspace_root,
                    )
                    if not bool(run_result.get("ok")):
                        test_result = {"ok": False, "status": "failed", "error": "run_failed", "run": dict(run_result)}
            if bool(test_result and test_result.get("ok")):
                break
            failure = _safe_failure_text(problem_ir, test_result or "implementation failed")
            diagnostics.append(failure)
            if attempt >= repairs:
                break
            repair_status, repair_advisory, repair_detail, repair_used = await self._consult_cloud(
                problem_ir,
                phase="repair",
                failure=failure,
                consent=bool(cloud_consent),
                advisor_override=coordinator or cloud_advisor,
            )
            cloud_status = repair_status if repair_status not in {"not_consulted", "disabled"} else cloud_status
            consultations += repair_used
            if repair_advisory:
                design = AppDesignSpec.from_advisory(repair_advisory, problem_ir).with_revision(
                    [_safe_advisory_text(problem_ir, repair_advisory, limit=MAX_ADVISORY_CHARS)],
                    source_of_advice="cloud_repair",
                )
            elif repair_detail:
                design = design.with_revision(
                    [_safe_advisory_text(problem_ir, repair_detail)],
                    source_of_advice=design.source_of_advice,
                )
            if repair_hook is not None:
                try:
                    repaired = await _invoke_callback(
                        repair_hook,
                        kwargs={"problem": problem_ir, "design": design, "workspace": selected_workspace, "failure": failure, "attempt": attempt + 1},
                        positional=(design, selected_workspace, failure),
                    )
                    if isinstance(repaired, Mapping):
                        design = AppDesignSpec.from_advisory(repaired, problem_ir)
                except Exception as exc:
                    diagnostics.append(f"repair callback failed: {type(exc).__name__}")

        # Keep the DB/Manifest/Git boundary in existing services.  This hook is
        # intentionally best effort for pure workspace mode.
        manifest_synced = await self._sync_persistent_manifest(
            app=app_obj,
            app_id=resolved_app_id,
            session=session,
            workspace=selected_workspace,
        )
        if resolved_app_id and not manifest_synced:
            diagnostics.append("persistent App Manifest sync failed")
        persistence_failed = False
        if created_persistent and session is not None:
            commit = getattr(session, "commit", None)
            if callable(commit):
                try:
                    await commit()
                except Exception as exc:
                    diagnostics.append(f"persistent App transaction failed: {type(exc).__name__}")
                    persistence_failed = True
        if created_persistent and resolved_app_id:
            try:
                from .app_git_service import AppGitService

                AppGitService(
                    workspace_root=workspace_root or self.workspace_root
                ).checkpoint(
                    resolved_app_id,
                    "App workflow generated source",
                    actor=str(owner or "workflow"),
                )
            except Exception as exc:
                # Git is an existing optional integration; source/DB
                # durability remains authoritative when Git is unavailable.
                logger.warning(
                    "App workflow Git checkpoint failed: %s", type(exc).__name__
                )
        status = (
            "succeeded"
            if bool(test_result and test_result.get("ok"))
            and not persistence_failed
            and (not resolved_app_id or manifest_synced)
            else "failed"
        )
        await progress("completed", status, "App/Macroを検証しました" if status == "succeeded" else "App/Macroの検証に失敗しました")
        if foundation_context is not None and own_foundation_context:
            foundation_context.close()
        return AppBuildResult(
            status=status,
            kind=selected_kind,
            workflow_id=problem_ir.workflow_id,
            workspace=str(selected_workspace),
            app_id=resolved_app_id,
            target_key=design.target_key,
            problem=problem_ir,
            design=design,
            cloud_status=cloud_status,
            # Cloud responses are advisory-only and must not be copied into
            # durable/public metadata with local literals restored.  Keep a
            # masked diagnostic projection for callers that need status text.
            cloud_advisory=_safe_advisory_text(
                problem_ir,
                advisory,
                limit=MAX_ADVISORY_CHARS,
            ),
            consultations=consultations,
            attempts=attempts,
            test_result=test_result,
            run_result=run_result,
            diagnostics=tuple(diagnostics[-8:]),
            files=files_written,
            intent=intent,
        )

    async def build(self, request: str | None = None, **kwargs: Any) -> AppBuildResult:
        return await self.execute(request, **kwargs)

    async def run(self, request: str | None = None, **kwargs: Any) -> AppBuildResult:
        # ``run`` is an ergonomic alias for controller integrations; callers
        # can request actual App execution with ``run_app=True``.
        return await self.execute(request, **kwargs)


__all__ = [
    "AppBuildResult",
    "AppWorkflowResult",
    "AppBuildWorkflow",
    "AppDesignSpec",
    "AppIntent",
    "AppProblemIR",
    "AppWorkflowIntent",
    "automatic_app_intent",
    "build_app_problem_ir",
    "detect_app_intent",
    "detect_app_workflow_intent",
    "parse_app_command",
    "parse_app_problem",
]

# Historical/short name used by a few controller prototypes.
AppIntent = AppWorkflowIntent
AppWorkflowResult = AppBuildResult
