"""Privacy-aware deterministic document workflow.

The document workflow intentionally keeps Office files local.  A small,
structured projection is optionally sent to :class:`CloudAdvisorCoordinator`
for read-only advice; the resulting text is validated and never receives tool
authority.  The actual workbook mutation is performed locally with
``openpyxl`` so styles, merged cells and workbook print settings survive a
create/update operation.

This module is deliberately self contained.  The shared workflow foundation
(``workflow_foundation``) is loaded lazily so that the service remains usable
while the foundation is being migrated, and a conservative local projection
is used as a fail-closed fallback.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

logger = logging.getLogger(__name__)

SUPPORTED_XLSX_SUFFIXES = frozenset({".xlsx", ".xlsm", ".xltx", ".xltm"})
DEFAULT_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_CELLS = 50_000
DEFAULT_MAX_TEXT_CHARS = 120_000
DEFAULT_MAX_ADVISORY_CHARS = 32_000
MAX_CLOUD_NODES = 24


def _safe_output_name(path: Path | None) -> str:
    """Project an artifact basename without exposing caller-chosen labels."""

    if path is None:
        return ""
    name = Path(path).name
    if re.fullmatch(r"document_[0-9a-f]{10}(?:_[0-9a-f]{10})?_updated\.(?:xlsx|xlsm|xltx|xltm)", name, re.I):
        return name
    digest = hashlib.sha256(name.encode("utf-8", "replace")).hexdigest()[:10]
    suffix = Path(name).suffix.casefold()
    if suffix not in SUPPORTED_XLSX_SUFFIXES:
        suffix = ".xlsx"
    return f"document_{digest}_updated{suffix}"


_PLAN_SENSITIVE_RE = re.compile(
    r"(?i)(?:password|passwd|token|secret|api[_ -]?key|https?://|"
    r"(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}|"
    r"[A-Za-z]:\\|@)"
)


def _safe_plan_fragment(value: Any, *, limit: int = 2_000) -> str:
    text = str(value or "").strip()[:limit]
    if not text:
        return ""
    if _PLAN_SENSITIVE_RE.search(text):
        return "<ADVISORY_REDACTED>"
    if any(ord(char) > 127 for char in text) and len(text) >= 4:
        return "<ADVISORY_REDACTED>"
    return text


class DocumentWorkflowError(RuntimeError):
    """Base error for a document workflow request."""


class AttachmentResolutionError(DocumentWorkflowError):
    """Raised when an attachment is outside the caller's authorized scope."""


class UnsupportedDocumentFormat(DocumentWorkflowError):
    """Raised for formats which this workflow does not implement."""


class DocumentPlanValidationError(DocumentWorkflowError):
    """Raised when untrusted advisory data is not a supported plan."""


@dataclass(frozen=True)
class ParsedDocumentCommand:
    """Normalized slash command metadata shared by ``/document``/``/template``."""

    command: str
    intent: str
    prompt: str = ""
    explicit: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "intent": self.intent,
            "prompt": self.prompt,
            "explicit": self.explicit,
        }

    # A light mapping compatibility seam is useful to HTTP callers which used
    # to receive command dictionaries.
    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


def parse_workflow_command(value: Any) -> ParsedDocumentCommand | None:
    """Parse a document slash command without invoking a Skill.

    Only the two system-owned commands are accepted.  Matching is case
    insensitive and accepts the command as the complete input or followed by
    ordinary user text.
    """

    text = str(value or "").strip()
    if not text.startswith("/"):
        return None
    match = re.match(r"^(?P<token>/[A-Za-z][A-Za-z0-9_-]*)(?:\s+(?P<body>.*))?$", text, re.S)
    if not match:
        return None
    token = match.group("token").casefold()
    if token not in {"/document", "/template"}:
        return None
    return ParsedDocumentCommand(
        command=token,
        intent="template" if token == "/template" else "document",
        prompt=(match.group("body") or "").strip(),
    )


def parse_document_command(value: Any) -> ParsedDocumentCommand | None:
    """Parse ``/document`` (alias for :func:`parse_workflow_command`)."""

    parsed = parse_workflow_command(value)
    return parsed if parsed is not None and parsed.command == "/document" else None


def parse_template_command(value: Any) -> ParsedDocumentCommand | None:
    """Parse ``/template`` (alias for :func:`parse_workflow_command`)."""

    parsed = parse_workflow_command(value)
    return parsed if parsed is not None and parsed.command == "/template" else None


def detect_document_intent(value: Any) -> str | None:
    """Return ``document``/``template`` for explicit or representative auto intent."""

    parsed = parse_workflow_command(value)
    if parsed is not None:
        return parsed.intent
    text = str(value or "").casefold()
    # Keep this router deliberately conservative.  It is only a local hint;
    # the Chat controller remains the authority for selecting a workflow.
    document_markers = (
        "手順書",
        "資料から",
        "資料を",
        "文書を",
        "ドキュメント",
        "document",
        "procedure manual",
        "xlsx",
        "テンプレート",
        "template",
    )
    if not any(marker.casefold() in text for marker in document_markers):
        return None
    return "template" if any(marker in text for marker in ("テンプレート", "template")) else "document"


def is_document_intent(value: Any) -> bool:
    return detect_document_intent(value) is not None


@dataclass(frozen=True)
class DocumentNode:
    """Local node reference; ``value`` is never included in repr/audit output."""

    node_id: str
    sheet: str
    coordinate: str
    role: str = "content"
    value: Any = field(default=None, repr=False, compare=False)

    def safe_dict(self, safe_value: Any = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "node_id": self.node_id,
            "sheet": self.sheet,
            "coordinate": self.coordinate,
            "role": self.role,
        }
        if safe_value is not None:
            result["value"] = safe_value
        return result


def _safe_repr_dict(value: Mapping[str, Any]) -> str:
    return "{" + ", ".join(f"{key}={value[key]!r}" for key in value if key not in {"value", "raw", "content"}) + "}"


@dataclass(frozen=True)
class RawDocumentIR:
    """Local-authority workbook representation.

    Raw cell values are deliberately excluded from the dataclass repr and
    ``to_dict``.  ``nodes`` is still available to the local adapter for
    rebinding and is never sent to a provider directly.
    """

    source_path: Path | None = field(default=None, repr=False)
    nodes: tuple[DocumentNode, ...] = field(default_factory=tuple, repr=False)
    sheets: tuple[str, ...] = ()
    workbook_properties: Mapping[str, Any] = field(default_factory=dict, repr=False)
    text: str = field(default="", repr=False)
    current_values: Mapping[str, str] = field(default_factory=dict, repr=False)
    context: Any = field(default=None, repr=False, compare=False)

    def __repr__(self) -> str:  # pragma: no cover - defensive logging surface
        return f"RawDocumentIR(sheets={len(self.sheets)}, nodes={len(self.nodes)})"

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(node.node_id for node in self.nodes)

    def safe_summary(self) -> dict[str, Any]:
        return {
            "sheets": list(self.sheets),
            "node_count": len(self.nodes),
            "node_ids": list(self.node_ids),
            "source_suffix": self.source_path.suffix.lower() if self.source_path else "",
        }

    def to_dict(self) -> dict[str, Any]:
        return self.safe_summary()


@dataclass(frozen=True)
class CloudProjectionIR:
    """Safe structural projection eligible for Cloud Advisor consultation."""

    nodes: tuple[Mapping[str, Any], ...] = field(default_factory=tuple, repr=False)
    sheets: tuple[str, ...] = ()
    intent: str = "document"
    objective: str = ""
    source_digest: str = field(default="", repr=False)
    context_id: str = field(default="", repr=False)

    def __repr__(self) -> str:  # pragma: no cover - defensive logging surface
        return f"CloudProjectionIR(sheets={len(self.sheets)}, nodes={len(self.nodes)}, intent={self.intent!r})"

    def to_payload(self) -> dict[str, Any]:
        # Keep the provider-bound projection compact and deterministic.  Local
        # rebinding still retains every node; Cloud advice only needs a bounded
        # structural sample and the total count.  This prevents a large
        # workbook from exhausting the local semantic sidecar budget while
        # preserving opaque node IDs for the common/title/metadata cases.
        nodes = [dict(node) for node in self.nodes[:MAX_CLOUD_NODES]]
        return {
            "schema": "aoitalk.document_projection.v1",
            "intent": self.intent,
            "objective": self.objective[:2_000],
            "sheets": list(self.sheets),
            "nodes": nodes,
            "node_count": len(self.nodes),
            "nodes_truncated": len(nodes) < len(self.nodes),
            "source_digest": self.source_digest,
        }


@dataclass(frozen=True)
class DocumentPlan:
    """Validated advisory plan; operations only address known local nodes."""

    operations: tuple[Mapping[str, Any], ...] = field(default_factory=tuple, repr=False)
    missing_facts: tuple[str, ...] = ()
    validation_requirements: tuple[str, ...] = ()
    intent: str = "document"
    schema: str = "aoitalk.document_plan.v1"
    advisory_status: str = "not_requested"
    advisory_text: str = field(default="", repr=False)

    def __repr__(self) -> str:  # pragma: no cover - defensive logging surface
        return f"DocumentPlan(operations={len(self.operations)}, missing_facts={len(self.missing_facts)}, intent={self.intent!r}, advisory_status={self.advisory_status!r})"

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(str(item.get("node_id", "")) for item in self.operations if isinstance(item, Mapping))

    def to_dict(self, *, include_advisory_text: bool = False, include_values: bool = False) -> dict[str, Any]:
        operations: list[dict[str, Any]] = []
        for item in self.operations:
            # Plan values are local-only mutation inputs.  Keep them out of
            # ordinary result/audit projections unless a local caller
            # explicitly asks for them.
            operation = {
                key: item[key]
                for key in ("op", "node_id", "role", "reason")
                if key in item
            }
            if "reason" in operation:
                operation["reason"] = _safe_plan_fragment(operation["reason"])
            if include_values and "value" in item:
                operation["value"] = item["value"]
            operations.append(operation)
        result: dict[str, Any] = {
            "schema": self.schema,
            "intent": self.intent,
            "operations": operations,
            "missing_facts": [_safe_plan_fragment(item) for item in self.missing_facts[:32]],
            "validation_requirements": [
                _safe_plan_fragment(item) for item in self.validation_requirements[:32]
            ],
            "advisory_status": self.advisory_status,
        }
        if include_advisory_text:
            result["advisory_text"] = _safe_plan_fragment(
                self.advisory_text,
                limit=DEFAULT_MAX_ADVISORY_CHARS,
            )
        return result


@dataclass(frozen=True)
class DocumentWorkflowResult:
    """Stable user-facing result with no raw input values in repr/audit fields."""

    status: str
    output_path: Path | None = field(default=None, repr=False)
    plan: DocumentPlan | None = field(default=None, repr=False)
    advisory_status: str = "not_requested"
    warnings: tuple[str, ...] = ()
    changed_nodes: tuple[str, ...] = ()
    artifact_sha256: str = ""
    provenance: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:  # pragma: no cover - defensive logging surface
        return f"DocumentWorkflowResult(status={self.status!r}, changed_nodes={len(self.changed_nodes)}, advisory_status={self.advisory_status!r})"

    @property
    def ok(self) -> bool:
        return self.status in {"completed", "local_fallback", "created"}

    @property
    def artifact(self) -> Path | None:
        return self.output_path

    @property
    def user_message(self) -> str:
        """Bounded display text; never exposes an absolute workspace path."""

        if self.ok and self.output_path is not None:
            output_name = _safe_output_name(self.output_path)
            return (
                "XLSXドキュメントを生成しました: "
                f"{output_name}"
            )
        return "ドキュメントワークフローを完了できませんでした。"

    # ``WorkflowController`` and legacy callback bridges use both spellings.
    message = user_message

    def to_dict(self) -> dict[str, Any]:
        output_name = _safe_output_name(self.output_path) if self.output_path else None
        return {
            "status": self.status,
            # Absolute paths are local implementation details and must not
            # cross chat/audit metadata boundaries.
            "output_path": output_name,
            "output_name": output_name,
            "plan": self.plan.to_dict() if self.plan else None,
            "advisory_status": self.advisory_status,
            "warnings": list(self.warnings),
            "changed_nodes": list(self.changed_nodes),
            "artifact_sha256": self.artifact_sha256,
            "provenance": dict(self.provenance),
        }


_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "customer_name": ("顧客名", "会社名", "顧客", "customer", "client", "company"),
    "customer_code": ("顧客コード", "顧客id", "customer code", "customer_id", "client code"),
    "device_name": ("装置名", "機器名", "ホスト名", "hostname", "device", "device name"),
    "management_ip": ("管理ip", "管理ｉｐ", "管理アドレス", "management ip", "mgmt ip"),
    "peer_ip": ("peer", "対向ip", "接続先ip", "peer ip"),
    "management_url": ("管理url", "管理ＵＲＬ", "管理画面", "management url", "admin url"),
    "contact_email": ("担当", "担当者", "連絡先", "email", "e-mail", "contact"),
    "storage_path": ("設定保管先", "保存先", "ファイルパス", "path", "storage path"),
    "password": ("password", "passwd", "パスワード", "認証情報"),
    "environment": ("環境", "environment", "env"),
    "project_id": ("案件id", "案件ｉｄ", "プロジェクトid", "project id", "project_id"),
}

_CANONICAL_LABELS: dict[str, str] = {
    re.sub(r"[\s_\-]", "", alias.casefold()): key
    for key, aliases in _KEY_ALIASES.items()
    for alias in aliases
}


def _canonical_key(label: Any) -> str | None:
    text = re.sub(r"[\s_\-]", "", str(label or "").strip().casefold())
    if text in _CANONICAL_LABELS:
        return _CANONICAL_LABELS[text]
    # Prefix/suffix matching keeps labels such as ``今回環境`` useful while
    # rejecting arbitrary prose as a key.
    for normalized, key in _CANONICAL_LABELS.items():
        if len(normalized) >= 3 and (text.endswith(normalized) or text.startswith(normalized)):
            return key
    return None


def _text_from_material(material: Any, *, max_chars: int = DEFAULT_MAX_TEXT_CHARS) -> str:
    if material is None:
        return ""
    if isinstance(material, bytes):
        return material[: max_chars * 4].decode("utf-8", errors="replace")[:max_chars]
    if isinstance(material, Path):
        if material.suffix.casefold() in SUPPORTED_XLSX_SUFFIXES:
            return _workbook_text(material, max_chars=max_chars)
        try:
            return material.read_text(encoding="utf-8", errors="replace")[:max_chars]
        except OSError:
            return ""
    if isinstance(material, Mapping):
        for key in ("text", "content", "body", "notes", "value"):
            if key in material and material[key] is not material:
                return _text_from_material(material[key], max_chars=max_chars)
        return ""
    return str(material)[:max_chars]


def extract_current_values(material: Any, *, max_chars: int = DEFAULT_MAX_TEXT_CHARS) -> dict[str, str]:
    """Extract project values from notes using bounded label-aware parsing."""

    text = _text_from_material(material, max_chars=max_chars)
    if not text:
        return {}
    values: dict[str, str] = {}
    # Labels are intentionally constrained to one line.  This avoids turning
    # arbitrary attached prose into a data-bearing instruction channel.
    for line in text.splitlines()[:20_000]:
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^\s*([^:=：]{1,80})\s*[:：=]\s*(.*?)\s*$", line)
        if not match:
            continue
        key = _canonical_key(match.group(1))
        value = match.group(2).strip().strip('"\'')
        if key and value and len(value) <= 2_000:
            values.setdefault(key, value)
    # Also catch common ``password=``/``token=`` forms embedded in a line.
    for key, aliases in _KEY_ALIASES.items():
        for alias in aliases:
            pattern = re.compile(rf"(?im)(?<![\w]){re.escape(alias)}\s*[:：=]\s*([^\s,;]+)")
            found = pattern.search(text)
            if found and found.group(1):
                values.setdefault(key, found.group(1).strip('"\''))
                break
    return values


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _node_id(sheet: str, coordinate: str) -> str:
    return "node_" + _hash_text(f"{sheet}\x00{coordinate}")


def _role_for_cell(sheet: Any, cell: Any) -> str:
    coordinate = str(getattr(cell, "coordinate", ""))
    row = int(getattr(cell, "row", 0) or 0)
    column = int(getattr(cell, "column", 0) or 0)
    value = str(getattr(cell, "value", "") or "")
    label_key = _canonical_key(value.split(":", 1)[0].split("：", 1)[0])
    if row == 1 and column <= 3:
        return "title"
    if label_key:
        return label_key
    if re.match(r"^\d+[.)]", value):
        return "step"
    if "rollback" in value.casefold() or "ロールバック" in value:
        return "rollback"
    if "確認" in value or "verification" in value.casefold():
        return "verification"
    return "content"


def _workbook_text(path: Path, *, max_chars: int = DEFAULT_MAX_TEXT_CHARS) -> str:
    try:
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=False, keep_vba=path.suffix.casefold() in {".xlsm", ".xltm"})
    except Exception:
        return ""
    chunks: list[str] = []
    try:
        for ws in wb.worksheets:
            chunks.append(f"[sheet:{ws.title}]")
            for row in ws.iter_rows():
                for cell in row:
                    value = getattr(cell, "value", None)
                    if value not in (None, ""):
                        chunks.append(str(value))
                        if sum(len(item) + 1 for item in chunks) >= max_chars:
                            return "\n".join(chunks)[:max_chars]
    finally:
        wb.close()
    return "\n".join(chunks)[:max_chars]


def resolve_input_attachment(
    attachment: Any,
    *,
    allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
    max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    suffixes: Iterable[str] = SUPPORTED_XLSX_SUFFIXES,
) -> Path:
    """Resolve one already-authorized local attachment with traversal guards."""

    candidate: Any = attachment
    if isinstance(attachment, Mapping):
        candidate = next((attachment.get(key) for key in ("path", "file_path", "local_path", "resolved_path") if attachment.get(key)), None)
        if candidate is None and isinstance(attachment.get("content"), bytes):
            raise AttachmentResolutionError("in-memory attachments require execute() materialization")
    if not isinstance(candidate, (str, os.PathLike, Path)):
        raise AttachmentResolutionError("attachment path is missing")
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AttachmentResolutionError("attachment does not exist") from exc
    if not resolved.is_file():
        raise AttachmentResolutionError("attachment is not a file")
    allowed = {str(item).casefold() for item in suffixes}
    if resolved.suffix.casefold() not in allowed:
        raise UnsupportedDocumentFormat(f"unsupported document format: {resolved.suffix or '(none)'}")
    if max_bytes > 0:
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            raise AttachmentResolutionError("attachment metadata unavailable") from exc
        if size > max_bytes:
            raise AttachmentResolutionError("attachment exceeds bounded size")
    if allowed_roots is not None:
        roots: list[Path] = []
        for root in allowed_roots:
            try:
                roots.append(Path(root).expanduser().resolve(strict=True))
            except OSError:
                continue
        if not roots or not any(_is_relative_to(resolved, root) for root in roots):
            raise AttachmentResolutionError("attachment is outside authorized roots")
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _attachment_name(item: Any) -> str:
    if isinstance(item, Mapping):
        return str(item.get("name") or item.get("filename") or item.get("path") or "")
    if isinstance(item, (str, os.PathLike, Path)):
        return Path(item).name
    return ""


def _attachment_with_base(attachment: Any, base_root: Any | None) -> Any:
    """Resolve chat-relative attachment paths under the server workspace.

    Browser payloads intentionally carry project-relative paths.  Treating
    those as process-CWD paths either fails legitimate requests or, worse,
    makes a caller-controlled relative path ambiguous.  The server-provided
    workspace root is the only base accepted here; the normal allowed-root
    check still runs afterwards.
    """

    if not base_root or not isinstance(attachment, Mapping):
        return attachment
    candidate_key = next(
        (
            key
            for key in ("path", "file_path", "local_path", "resolved_path")
            if isinstance(attachment.get(key), (str, os.PathLike))
            and str(attachment.get(key)).strip()
        ),
        None,
    )
    if candidate_key is None:
        return attachment
    raw = Path(str(attachment[candidate_key])).expanduser()
    if raw.is_absolute():
        return attachment
    updated = dict(attachment)
    updated[candidate_key] = str(Path(base_root) / raw)
    return updated


def _safe_text(value: Any, *, old_values: Mapping[str, str] | None = None, new_values: Mapping[str, str] | None = None) -> str:
    """Return a structural, non-sensitive projection of one cell value."""

    text = str(value or "")
    # Preserve formulas in the local workbook, but never expose executable
    # formula text to Cloud Advisor.
    if text.lstrip().startswith("="):
        return "<formula>"
    replacements: list[tuple[str, str]] = []
    for key, old in (old_values or {}).items():
        if old and len(old) >= 2:
            replacements.append((old, f"<slot:{key}>"))
    # New values are never sent, even when a caller passes notes as a cell.
    for key, current in (new_values or {}).items():
        if current and len(current) >= 2:
            replacements.append((current, f"<slot:{key}>"))
    for old, marker in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        text = text.replace(old, marker)
    text = re.sub(r"(?i)(\b(?:password|passwd|token|api[_ -]?key|secret)\s*[:=]\s*)[^\s,;]+", r"\1<secret>", text)
    text = re.sub(r"(?<![\w.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|127(?:\.\d{1,3}){3})(?![\w.])", "<private-ip>", text)
    text = re.sub(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", "<email>", text, flags=re.I)
    text = re.sub(r"(?i)https?://(?:[^\s/@:]+(?::[^\s/@]*)?@)?(?:[A-Za-z0-9_-]+\.)*(?:internal|local|localhost|intranet|corp|lan)(?::\d+)?[^\s]*", "<internal-url>", text)
    text = re.sub(r"(?<![\w])[A-Za-z]:\\[^\r\n\t\"']+", "<local-path>", text)
    # Label values are slots even when no corresponding old/new extraction was
    # possible (for example a Japanese company name in a title cell).
    for key, aliases in _KEY_ALIASES.items():
        labels = "|".join(re.escape(alias) for alias in aliases)
        text = re.sub(rf"(?i)({labels})\s*[:：=]\s*([^\r\n,;]+)", rf"\1: <slot:{key}>", text)
    return text[:4_000]


def _context_new(
    config: Any | None = None,
    *,
    session_context: Mapping[str, Any] | None = None,
    project_metadata: Mapping[str, Any] | None = None,
) -> Any:
    try:
        from .workflow_foundation import ProtectedWorkflowContext
    except Exception:
        return None
    for kwargs in (
        {
            "workflow_kind": "document",
            "config": config,
            "session_context": session_context,
            "project_metadata": project_metadata,
            # A realistic XLSX may contain hundreds of bounded cells.  Keep a
            # larger but still finite workflow-local traversal budget so the
            # shared foundation can represent the workbook without falling
            # back to an unstructured/raw path.
            "max_nodes": 4096,
            "max_items": 512,
        },
        {"workflow_kind": "document", "config": config},
        {"workflow_kind": "document"},
        {"scope": "document", "config": config},
        {"config": config},
        {},
    ):
        try:
            return ProtectedWorkflowContext(**kwargs)
        except Exception:
            continue
    return None


def _context_register(context: Any, value: Any, *, role: str, node_id: str) -> str | None:
    if context is None or value in (None, ""):
        return None
    register = getattr(context, "register", None)
    if not callable(register):
        return None
    if isinstance(value, (datetime, date, time)):
        value = value.isoformat()
    elif isinstance(value, Decimal):
        value = str(value)
    elif not isinstance(value, (str, int, float, bool, type(None))):
        # OpenPyXL may expose a small scalar type not accepted by the shared
        # foundation (for example a timedelta).  Convert only that scalar to
        # bounded text; never call repr() on arbitrary objects.
        try:
            value = str(value)
        except Exception:
            return None
    for kwargs in (
        {"kind": "document_cell", "role": role, "stable_key": node_id},
        {"role": role, "node_id": node_id},
        {"role": role},
        {"node_id": node_id},
        {},
    ):
        try:
            alias = register(value, **kwargs)
            if isinstance(alias, str) and alias.strip():
                return alias.strip()
            # The shared foundation returns an immutable WorkflowReference;
            # use only its opaque node/alias ID, never its local raw binding.
            reference_id = getattr(alias, "node_id", None) or getattr(alias, "alias", None)
            if isinstance(reference_id, str) and reference_id.strip():
                return reference_id.strip()
            if isinstance(alias, Mapping):
                for key in ("alias", "safe_value", "placeholder"):
                    if isinstance(alias.get(key), str) and alias[key].strip():
                        return alias[key].strip()
        except TypeError:
            continue
        except Exception:
            logger.debug("workflow context registration failed", exc_info=True)
            return None
    return None


def _context_provenance(context: Any) -> dict[str, Any]:
    if context is None:
        return {}
    for name in ("provenance", "provenance_summary", "safe_provenance"):
        method = getattr(context, name, None)
        if callable(method):
            try:
                result = method()
                if isinstance(result, Mapping):
                    allowed_keys = {
                        "workflow",
                        "workflow_kind",
                        "context_id",
                        "source_kind",
                        "source_ref_hash",
                        "version",
                        "operation",
                        "allowed_operations",
                        "node_count",
                    }
                    safe: dict[str, Any] = {}
                    for key, value in result.items():
                        if str(key) not in allowed_keys:
                            continue
                        if isinstance(value, (str, int, float, bool)) or value is None:
                            safe[str(key)] = value
                        elif isinstance(value, (list, tuple)) and all(isinstance(item, (str, int)) for item in value):
                            safe[str(key)] = list(value)[:128]
                    return safe
            except Exception:
                continue
    return {}


def _load_raw_document(path: Path, *, context: Any, max_cells: int = DEFAULT_MAX_CELLS) -> tuple[Any, RawDocumentIR]:
    try:
        from openpyxl import load_workbook
    except Exception as exc:  # pragma: no cover - dependency is declared
        raise DocumentWorkflowError("openpyxl is unavailable") from exc
    suffix = path.suffix.casefold()
    if suffix not in SUPPORTED_XLSX_SUFFIXES:
        raise UnsupportedDocumentFormat(f"unsupported document format: {suffix or '(none)'}")
    try:
        workbook = load_workbook(path, data_only=False, keep_vba=suffix in {".xlsm", ".xltm"})
    except Exception as exc:
        raise DocumentWorkflowError("failed to open XLSX workbook") from exc
    nodes: list[DocumentNode] = []
    text_chunks: list[str] = []
    cell_count = 0
    try:
        for sheet in workbook.worksheets:
            text_chunks.append(f"[sheet:{sheet.title}]")
            for row in sheet.iter_rows():
                for cell in row:
                    cell_count += 1
                    if cell_count > max_cells:
                        raise DocumentWorkflowError("workbook exceeds bounded cell count")
                    value = cell.value
                    if value in (None, ""):
                        continue
                    value_text = str(value)
                    text_chunks.append(value_text)
                    node = DocumentNode(
                        node_id=_node_id(str(sheet.title), str(cell.coordinate)),
                        sheet=str(sheet.title),
                        coordinate=str(cell.coordinate),
                        role=_role_for_cell(sheet, cell),
                        value=value,
                    )
                    nodes.append(node)
    except Exception:
        workbook.close()
        raise
    old_values = extract_current_values("\n".join(text_chunks))
    properties = {
        "sheet_count": len(workbook.worksheets),
        "merged_ranges": sum(len(sheet.merged_cells.ranges) for sheet in workbook.worksheets),
        "defined_names": len(getattr(workbook, "defined_names", {}) or {}),
    }
    raw = RawDocumentIR(
        source_path=path,
        nodes=tuple(nodes),
        sheets=tuple(str(sheet.title) for sheet in workbook.worksheets),
        workbook_properties=properties,
        text="\n".join(text_chunks)[:DEFAULT_MAX_TEXT_CHARS],
        current_values=old_values,
        context=context,
    )
    return workbook, raw


def build_cloud_projection(
    raw: RawDocumentIR,
    *,
    intent: str = "document",
    objective: str = "",
    current_values: Mapping[str, str] | None = None,
    use_foundation: bool = True,
) -> CloudProjectionIR:
    """Build a safe node/role projection; no workbook binary is included."""

    if use_foundation and raw.context is None:
        # A regex-only fallback is not sufficient for an unlabelled customer
        # value.  Cloud-enabled document work requires the shared canonical
        # masking boundary and fails closed when it is unavailable.
        raise DocumentWorkflowError("document protected projection is unavailable")
    if use_foundation:
        project_method = getattr(raw.context, "project", None)
        references_value = getattr(raw.context, "references", None)
        if not callable(project_method) or references_value is None:
            raise DocumentWorkflowError("document protected context is malformed")
    # Cloud advice is intentionally bounded.  The local raw IR still retains
    # every cell for deterministic rebinding and XLSX mutation, but only a
    # compact structural sample is registered with the workflow foundation.
    # Registering all cells would exceed the foundation's aggregate projection
    # budget for a realistic workbook before ``CloudProjectionIR.to_payload``
    # had a chance to truncate it.
    bounded_nodes = tuple(raw.nodes[:MAX_CLOUD_NODES])
    safe_nodes: list[Mapping[str, Any]] = []
    registered_node_ids: set[str] = set()
    # Sheet titles are user data too (customers often put project names in
    # them).  Keep workbook-local node identity in the local IR, but expose
    # only deterministic opaque sheet labels to Cloud Advisor.
    safe_sheet_names = {
        sheet: f"sheet_{index + 1}_{_hash_text(sheet)[:8]}"
        for index, sheet in enumerate(raw.sheets)
    }
    for node in bounded_nodes:
        alias = _context_register(raw.context, node.value, role=node.role, node_id=node.node_id)
        if use_foundation and node.value not in (None, "") and alias is None:
            raise DocumentWorkflowError("document protected node registration failed")
        registered_node_ids.add(node.node_id)
        safe_value = alias or _safe_text(node.value, old_values=raw.current_values, new_values=current_values)
        safe_node = node.safe_dict(safe_value)
        safe_node["sheet"] = safe_sheet_names.get(node.sheet, f"sheet_{_hash_text(node.sheet)[:8]}")
        safe_nodes.append(safe_node)
    # When available, run the complete projection through the shared
    # workflow foundation's canonical one-way masking boundary as well.  The
    # deterministic local scrub above remains a fail-closed fallback for
    # deployments where semantic redaction is not configured.  We only copy
    # masked values back by registration order; foundation IDs are opaque and
    # are never interpreted as local workbook coordinates.
    if use_foundation and raw.context is not None:
        project = getattr(raw.context, "project", None)
        references = getattr(raw.context, "references", None)
        if callable(project) and references:
            try:
                # A caller may supply a context that already contains other
                # workflow bindings.  Project only the references registered
                # by this document adapter, preserving context isolation and
                # the same bounded order as ``safe_nodes``.
                selected_references = tuple(
                    reference
                    for reference in references
                    if getattr(reference, "node_id", None) in registered_node_ids
                )[:MAX_CLOUD_NODES]
                foundation_projection = project(
                    refs=selected_references,
                    metadata={"purpose": "document", "format": "xlsx", "schema": "v1"},
                )
                foundation_payload = getattr(foundation_projection, "safe_payload", None)
                if foundation_payload is None and isinstance(foundation_projection, Mapping):
                    foundation_payload = foundation_projection
                foundation_nodes = foundation_payload.get("nodes") if isinstance(foundation_payload, Mapping) else None
                if isinstance(foundation_nodes, Sequence) and len(foundation_nodes) == len(safe_nodes):
                    for index, foundation_node in enumerate(foundation_nodes):
                        if isinstance(foundation_node, Mapping) and "value" in foundation_node:
                            safe_nodes[index] = dict(safe_nodes[index])
                            safe_nodes[index]["value"] = foundation_node.get("value")
            except Exception as exc:
                # Canonical boundary errors must not cause raw fallback.  The
                # caller can continue locally, but no Cloud request may be
                # constructed from an ambiguous projection.
                raise DocumentWorkflowError("document protected projection failed") from exc
    digest = _hash_text("\n".join(f"{node['node_id']}={node.get('value', '')}" for node in safe_nodes))
    context_id = ""
    if raw.context is not None:
        context_id = str(getattr(raw.context, "context_id", "") or getattr(raw.context, "id", "") or "")[:128]
    safe_objective = _safe_text(
        objective,
        old_values=raw.current_values,
        new_values=current_values,
    )
    if use_foundation and safe_objective:
        mask_text = getattr(raw.context, "mask_text", None)
        if not callable(mask_text):
            raise DocumentWorkflowError("document objective masking is unavailable")
        try:
            safe_objective = str(mask_text(safe_objective))
        except Exception as exc:
            raise DocumentWorkflowError("document objective masking failed") from exc
    return CloudProjectionIR(
        nodes=tuple(safe_nodes),
        sheets=tuple(safe_sheet_names.get(sheet, f"sheet_{_hash_text(sheet)[:8]}") for sheet in raw.sheets),
        intent=intent,
        objective=safe_objective[:2_000],
        source_digest=digest,
        context_id=context_id,
    )


def load_document_ir(
    attachment: Any,
    *,
    allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
    max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_cells: int = DEFAULT_MAX_CELLS,
    context: Any | None = None,
) -> RawDocumentIR:
    """Load a bounded XLSX into a local-only :class:`RawDocumentIR`.

    The workbook handle is closed before returning; callers that need to
    mutate a workbook should use :meth:`DocumentWorkflow.execute`.
    """

    path = resolve_input_attachment(
        attachment,
        allowed_roots=allowed_roots,
        max_bytes=max_bytes,
    )
    workbook, raw = _load_raw_document(path, context=context, max_cells=max_cells)
    workbook.close()
    return raw


# Descriptive aliases retained for embedding adapters and tests.
read_xlsx_document = load_document_ir
build_raw_document_ir = load_document_ir


_ALLOWED_PLAN_KEYS = frozenset({"schema", "intent", "operations", "replacements", "node_updates", "missing_facts", "validation_requirements", "checks", "notes", "advisory_status"})
_ALLOWED_OPERATION_KEYS = frozenset({"op", "operation", "node_id", "value", "role", "reason"})
_ALLOWED_OPERATIONS = frozenset({"replace", "set", "update"})


def validate_document_plan(value: Any, *, allowed_node_ids: Iterable[str] | None = None, intent: str = "document") -> DocumentPlan:
    """Validate untrusted Cloud JSON against a narrow allow-list."""

    if isinstance(value, DocumentPlan):
        plan = value
    else:
        payload: Any = value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                payload = {}
            else:
                fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.IGNORECASE | re.DOTALL)
                if fenced:
                    text = fenced.group(1).strip()
                try:
                    payload = json.loads(text)
                except Exception as exc:
                    # Plain advisory prose is informational, not an executable
                    # plan.  Treating it as a no-op is safer than guessing.
                    return DocumentPlan(intent=intent, advisory_text=text[:DEFAULT_MAX_ADVISORY_CHARS], advisory_status="text_only")
        if not isinstance(payload, Mapping):
            raise DocumentPlanValidationError("advisory plan must be a JSON object")
        for wrapper_key in ("plan", "document_plan", "advisory_plan"):
            nested = payload.get(wrapper_key)
            if isinstance(nested, Mapping):
                payload = nested
                break
        # Cloud responses commonly include harmless explanatory fields next to
        # the executable plan.  Ignore those fields rather than allowing them
        # to poison an otherwise valid plan; only the explicit allowlisted
        # operation/metadata keys below can influence local mutation.
        payload = {
            key: item for key, item in payload.items() if key in _ALLOWED_PLAN_KEYS
        }
        operations_raw = payload.get("operations", payload.get("node_updates", payload.get("replacements", ())))
        if isinstance(operations_raw, Mapping):
            operations_raw = [dict({"node_id": key, "value": item, "op": "replace"}) for key, item in operations_raw.items()]
        if not isinstance(operations_raw, Sequence) or isinstance(operations_raw, (str, bytes, bytearray)):
            raise DocumentPlanValidationError("operations must be a list")
        operations: list[Mapping[str, Any]] = []
        allowed = set(str(item) for item in (allowed_node_ids or ()))
        for raw_operation in operations_raw:
            if not isinstance(raw_operation, Mapping):
                raise DocumentPlanValidationError("operation must be an object")
            if set(raw_operation) - _ALLOWED_OPERATION_KEYS:
                raise DocumentPlanValidationError("operation contains unsupported fields")
            node_id = str(raw_operation.get("node_id") or "").strip()
            operation = str(raw_operation.get("op", raw_operation.get("operation", "replace")) or "replace").casefold()
            if operation not in _ALLOWED_OPERATIONS or not node_id:
                raise DocumentPlanValidationError("unsupported document operation")
            if allowed and node_id not in allowed:
                raise DocumentPlanValidationError("advisory references unknown document node")
            if "value" not in raw_operation:
                raise DocumentPlanValidationError("document operation is missing value")
            value_for_update = raw_operation.get("value")
            if isinstance(value_for_update, (bytes, bytearray, Mapping, list, tuple, set)):
                raise DocumentPlanValidationError("document operation value must be scalar text")
            # Cloud advice is never allowed to inject a new workbook formula
            # or an executable expression.  Formulas already present in the
            # source workbook remain untouched by the local preservation
            # adapter.
            if isinstance(value_for_update, str) and value_for_update.lstrip().startswith("="):
                raise DocumentPlanValidationError("document operation cannot add formulas")
            if isinstance(value_for_update, str) and _safe_text(value_for_update) != value_for_update:
                raise DocumentPlanValidationError(
                    "document operation contains protected local material"
                )
            if isinstance(value_for_update, str) and (
                value_for_update.startswith("=") or "\x00" in value_for_update
            ):
                # Advisory text must never smuggle a formula or malformed
                # control character into a locally executed workbook.
                raise DocumentPlanValidationError("unsafe document operation value")
            operations.append({
                "op": "replace",
                "node_id": node_id,
                "value": str(value_for_update)[:4_000],
                **({"role": str(raw_operation["role"])[:80]} if raw_operation.get("role") else {}),
            })
        missing = payload.get("missing_facts", ())
        checks = payload.get("validation_requirements", payload.get("checks", ()))
        if isinstance(missing, str):
            missing = [missing]
        if isinstance(checks, str):
            checks = [checks]
        if not isinstance(missing, Sequence) or isinstance(missing, (bytes, bytearray)) or not isinstance(checks, Sequence) or isinstance(checks, (bytes, bytearray)):
            raise DocumentPlanValidationError("plan metadata must be lists")
        schema = str(payload.get("schema") or "aoitalk.document_plan.v1")[:100]
        if schema != "aoitalk.document_plan.v1":
            raise DocumentPlanValidationError("unsupported advisory plan schema")
        normalized_intent = str(payload.get("intent") or intent).casefold()
        if normalized_intent not in {"document", "template"}:
            raise DocumentPlanValidationError("unsupported advisory plan intent")
        plan = DocumentPlan(
            operations=tuple(operations),
            missing_facts=tuple(str(item)[:500] for item in missing[:100]),
            validation_requirements=tuple(str(item)[:500] for item in checks[:100]),
            intent=normalized_intent,
            schema=schema,
            advisory_status=str(payload.get("advisory_status") or "validated")[:80],
            advisory_text=str(payload.get("notes") or "")[:DEFAULT_MAX_ADVISORY_CHARS],
        )
    if allowed_node_ids is not None:
        allowed = set(str(item) for item in allowed_node_ids)
        for operation in plan.operations:
            if str(operation.get("node_id")) not in allowed:
                raise DocumentPlanValidationError("advisory references unknown document node")
    return plan


# Descriptive aliases retained for embedding adapters and older callers.
extract_document_values = extract_current_values
parse_advisory_plan = validate_document_plan
safe_document_projection = build_cloud_projection
resolve_attachment = resolve_input_attachment


def _new_values_from_adjacent_cells(workbook: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for index, cell in enumerate(row):
                key = _canonical_key(cell.value)
                if not key:
                    continue
                neighbors = []
                if index + 1 < len(row):
                    neighbors.append(row[index + 1].value)
                if cell.row < sheet.max_row:
                    neighbors.append(sheet.cell(cell.row + 1, cell.column).value)
                for candidate in neighbors:
                    if candidate not in (None, ""):
                        values.setdefault(key, str(candidate).strip())
                        break
    return values


def _replace_cell_text(text: str, old_values: Mapping[str, str], new_values: Mapping[str, str]) -> str:
    result = text
    for key, old in sorted(old_values.items(), key=lambda item: len(item[1]), reverse=True):
        current = new_values.get(key)
        if old and current and old != current:
            result = result.replace(old, current)
    for key, aliases in _KEY_ALIASES.items():
        current = new_values.get(key)
        if not current:
            continue
        labels = "|".join(re.escape(alias) for alias in aliases)
        # Use a callable replacement so Windows paths/backslashes in the new
        # local value are treated as literal text, not regex replacement
        # escapes (``re.error: bad escape``).
        result = re.sub(
            rf"(?i)({labels})\s*[:：=]\s*([^\r\n,;]+)",
            lambda match, value=current: f"{match.group(1)}: {value}",
            result,
        )
    return result


def apply_document_plan(workbook: Any, raw: RawDocumentIR, plan: DocumentPlan, current_values: Mapping[str, str]) -> tuple[str, ...]:
    """Apply local value rebinding while preserving workbook structures."""

    changed: list[str] = []
    node_map = {node.node_id: node for node in raw.nodes}
    # First update adjacent label/value cells.  This handles the common
    # two-column metadata layout without changing formatting or dimensions.
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for index, cell in enumerate(row):
                key = _canonical_key(cell.value)
                if not key or key not in current_values:
                    continue
                target = row[index + 1] if index + 1 < len(row) else (sheet.cell(cell.row + 1, cell.column) if cell.row < sheet.max_row else None)
                # openpyxl represents all but the anchor of a merged range as
                # read-only ``MergedCell`` instances.  Skip these targets so
                # preservation remains fail-safe rather than unmerging data.
                if target is not None and target.__class__.__name__ == "MergedCell":
                    target = None
                if target is not None and target.value not in (None, "") and target.value != current_values[key]:
                    target.value = current_values[key]
                    changed.append(_node_id(str(sheet.title), str(target.coordinate)))
                elif target is not None and target.value in (None, ""):
                    target.value = current_values[key]
                    changed.append(_node_id(str(sheet.title), str(target.coordinate)))
    for node in raw.nodes:
        cell = workbook[node.sheet][node.coordinate]
        if isinstance(cell.value, str) and not cell.value.startswith("="):
            updated = _replace_cell_text(cell.value, raw.current_values, current_values)
            if updated != cell.value:
                cell.value = updated
                changed.append(node.node_id)
    # Advisory operations are accepted only for known nodes and are limited to
    # scalar text.  They cannot add paths, formulas, or tool instructions.
    for operation in plan.operations:
        node_id = str(operation.get("node_id") or "")
        node = node_map.get(node_id)
        if node is None:
            raise DocumentPlanValidationError("advisory references unknown document node")
        cell = workbook[node.sheet][node.coordinate]
        value = str(operation.get("value") or "")[:4_000]
        if cell.value != value:
            cell.value = value
            changed.append(node.node_id)
    # Keep deterministic order while deduplicating.
    return tuple(dict.fromkeys(changed))


def _plan_contains_local_literal(plan: DocumentPlan, literals: Iterable[str]) -> bool:
    sensitive = [item for item in literals if isinstance(item, str) and len(item) >= 4]
    for operation in plan.operations:
        value = str(operation.get("value") or "")
        if any(literal in value for literal in sensitive):
            return True
    return False


def _rebind_plan_aliases(plan: DocumentPlan, context: Any, current_values: Mapping[str, str]) -> DocumentPlan:
    """Resolve only foundation-issued aliases in advisory operations."""

    if context is None or not plan.operations:
        return plan
    rebind = getattr(context, "rebind", None)
    if not callable(rebind):
        return plan
    operations: list[Mapping[str, Any]] = []
    for operation in plan.operations:
        item = dict(operation)
        value = item.get("value")
        if isinstance(value, str) and value.startswith("wf_"):
            try:
                rebound = rebind(value, operation="read")
            except Exception as exc:
                raise DocumentPlanValidationError("advisory references unknown local alias") from exc
            if isinstance(rebound, (Mapping, list, tuple, set, bytes, bytearray)):
                raise DocumentPlanValidationError("advisory alias resolved to unsupported value")
            item["value"] = str(rebound)
        elif isinstance(value, str) and re.fullmatch(r"<slot:[a-z0-9_]+>", value):
            key = value[6:-1]
            if key not in current_values:
                raise DocumentPlanValidationError("advisory references unknown local slot")
            item["value"] = current_values[key]
        operations.append(item)
    return replace(plan, operations=tuple(operations))


def _artifact_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DocumentWorkflow:
    """System-owned XLSX creation/update workflow."""

    def __init__(
        self,
        config: Any | None = None,
        *,
        coordinator: Any | None = None,
        cloud_advisor: Any | None = None,
        max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
        max_cells: int = DEFAULT_MAX_CELLS,
    ) -> None:
        self.config = config
        self.coordinator = coordinator or cloud_advisor
        self.max_attachment_bytes = max(1, int(max_attachment_bytes))
        self.max_cells = max(100, int(max_cells))

    @staticmethod
    def _config_value(config: Any, key: str, default: Any = None) -> Any:
        if config is None:
            return default
        getter = getattr(config, "get", None)
        if callable(getter):
            try:
                value = getter(key, None)
            except TypeError:
                value = getter(key)
            if value is not None:
                return value
        if isinstance(config, Mapping):
            current: Any = config
            for part in key.split("."):
                if not isinstance(current, Mapping) or part not in current:
                    return default
                current = current[part]
            return current
        return default

    @staticmethod
    def _cloud_enabled(config: Any) -> bool:
        if config is None:
            return False
        getter = getattr(config, "get", None)
        try:
            mode = getter("cloud_advisor.mode", "disabled") if callable(getter) else None
        except TypeError:
            mode = getter("cloud_advisor.mode") if callable(getter) else None
        if mode is None and isinstance(config, Mapping):
            current: Any = config
            for part in "cloud_advisor.mode".split("."):
                if not isinstance(current, Mapping):
                    current = None
                    break
                current = current.get(part)
            mode = current
        return str(mode or "disabled").casefold() in {"manual", "automatic"}

    async def _consult_cloud(
        self,
        projection: CloudProjectionIR,
        *,
        intent: str,
        objective: str,
        coordinator: Any | None,
        consent: bool = False,
    ) -> tuple[str, str]:
        # An injected coordinator is a test/embedding seam, not permission to
        # bypass configured Cloud Advisor mode.  Disabled mode short-circuits
        # before even constructing a request.
        privacy_mode = str(
            self._config_value(self.config, "external_model_privacy.mode", "protected")
            or "protected"
        ).casefold()
        if privacy_mode == "local_only":
            return "", "local_only"
        if privacy_mode not in {"direct", "protected"}:
            return "", "invalid_privacy_mode"
        if self.config is not None and not self._cloud_enabled(self.config):
            return "", "disabled"
        mode_default = "automatic" if self.config is None and coordinator is not None else "disabled"
        mode = str(self._config_value(self.config, "cloud_advisor.mode", mode_default) or mode_default).casefold()
        if mode not in {"disabled", "manual", "automatic"}:
            return "", "invalid_config"
        if mode == "manual" and not consent:
            # A workflow's automatic escalation is not user Cloud consent.
            return "", "manual_required"
        active = coordinator
        if active is None and self._cloud_enabled(self.config):
            try:
                from .cloud_advisor_service import CloudAdvisorCoordinator

                active = CloudAdvisorCoordinator(self.config)
            except Exception:
                active = None
        if active is None:
            return "", "not_requested"
        query = json.dumps({
            "workflow": "document",
            "intent": intent,
            # ``projection.objective`` is the already-scrubbed representation;
            # never put the caller's raw message back into the Cloud query.
            "objective": projection.objective[:2_000],
            "projection": projection.to_payload(),
            "instructions": "Return JSON matching aoitalk.document_plan.v1; identify node_id/role operations only. Do not request raw values or tools.",
        }, ensure_ascii=False, separators=(",", ":"))
        try:
            from .cloud_advisor_service import (
                CloudAdvisorEscalationAssessment,
                CloudAdvisorRequest,
                CloudAdvisorTriggerOrigin,
            )

            origin = getattr(
                CloudAdvisorTriggerOrigin,
                "WORKFLOW_CONTROLLER",
                CloudAdvisorTriggerOrigin.MAIN_AGENT,
            )
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
                    specialist_judgment=True,
                ),
            )
        except Exception as exc:
            # A missing canonical Cloud Advisor contract is a provider error,
            # never a reason to retry with an unstructured/raw query.
            logger.warning(
                "document cloud consultation contract unavailable exception_type=%s",
                type(exc).__name__,
            )
            return "", "provider_error"
        try:
            consult = getattr(active, "consult", None)
            if not callable(consult):
                consult = active
            # A narrow compatibility seam for a callable explicitly declared
            # as ``query``/``text``.  We choose the argument once by
            # signature inspection; runtime TypeError is never retried.
            argument: Any = request
            try:
                parameters = list(inspect.signature(consult).parameters.values())
                if parameters and parameters[0].name in {"query", "text", "prompt"}:
                    argument = query
            except (TypeError, ValueError):
                pass
            result = consult(argument)
        except Exception as exc:
            logger.warning(
                "document cloud consultation failed exception_type=%s",
                type(exc).__name__,
            )
            return "", "provider_error"
        try:
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                result = await result
        except Exception as exc:
            logger.warning("document cloud consultation failed exception_type=%s", type(exc).__name__)
            return "", "provider_error"
        status = str(getattr(result, "status", "ok") or "ok")
        status = getattr(getattr(result, "status", None), "value", status)
        advisory = getattr(result, "advisory_text", None)
        if isinstance(result, str):
            advisory = result
        if advisory is None and isinstance(result, Mapping):
            status = str(result.get("status", status))
            advisory = result.get("advisory_text", result.get("text", ""))
        if status.casefold() not in {"ok", "success", "completed"}:
            return "", status[:80]
        return str(advisory or "")[:DEFAULT_MAX_ADVISORY_CHARS], "ok"

    @staticmethod
    def _output_path(
        source: Path | None,
        output_path: Any,
        output_dir: Any,
        *,
        allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
        workflow_id: str | None = None,
    ) -> Path:
        if output_path:
            path = Path(output_path).expanduser()
        else:
            root = Path(output_dir).expanduser() if output_dir else (source.parent if source else Path.cwd())
            # Do not derive a public artifact filename from a customer/project
            # source basename.  A short digest keeps concurrent outputs
            # deterministic without leaking source naming metadata.
            workflow_suffix = f"_{_hash_text(workflow_id)[:10]}" if workflow_id else ""
            stem = f"document_{_hash_text(source.name)[:10]}{workflow_suffix}" if source else f"document{workflow_suffix}"
            path = root / f"{stem}_updated.xlsx"
        if path.suffix.casefold() not in SUPPORTED_XLSX_SUFFIXES:
            path = path.with_suffix(".xlsx")
        resolved = path.resolve()
        if allowed_roots:
            roots: list[Path] = []
            for root in allowed_roots:
                try:
                    roots.append(Path(root).expanduser().resolve(strict=True))
                except OSError:
                    continue
            if not roots or not any(_is_relative_to(resolved, root) for root in roots):
                raise AttachmentResolutionError("output path is outside authorized roots")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    async def execute(
        self,
        source_attachment: Any = None,
        current_notes: Any = None,
        *,
        command: str = "/document",
        intent: str | None = None,
        attachments: Sequence[Any] | None = None,
        output_path: str | os.PathLike[str] | None = None,
        output_dir: str | os.PathLike[str] | None = None,
        allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
        workspace_root: str | os.PathLike[str] | None = None,
        project_root: str | os.PathLike[str] | None = None,
        context: Any | None = None,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
        coordinator: Any | None = None,
        cloud_advisor: Any | None = None,
        objective: str = "",
        cloud_consent: bool = False,
        progress_callback: Any | None = None,
        **_: Any,
    ) -> DocumentWorkflowResult:
        """Execute one bounded deterministic XLSX workflow.

        ``source_attachment`` may be a path or an attachment mapping.  When
        omitted, the first XLSX in ``attachments`` is used.  ``current_notes``
        may be text, a UTF-8 path, or a mapping containing ``text``/``content``.
        """

        parsed = parse_workflow_command(command) if command else None
        active_intent = (intent or (parsed.intent if parsed else None) or "document").casefold()
        if active_intent not in {"document", "template"}:
            raise DocumentWorkflowError("unsupported document workflow intent")
        attachment_items = list(attachments or ())
        if source_attachment is None and attachment_items:
            source_attachment = next(
                (item for item in attachment_items if _attachment_name(item).casefold().endswith(tuple(SUPPORTED_XLSX_SUFFIXES))),
                None,
            )
        if current_notes is None and attachment_items:
            # Chat attachment payloads commonly carry the XLSX and a plain
            # text/markdown notes file as separate items.  Resolve only the
            # first bounded non-XLSX item; binary Office files are not treated
            # as notes.
            current_notes = next(
                (
                    item
                    for item in attachment_items
                    if item is not source_attachment
                    and not _attachment_name(item).casefold().endswith(tuple(SUPPORTED_XLSX_SUFFIXES))
                ),
                None,
            )
        if allowed_roots is None and (workspace_root or project_root):
            allowed_roots = tuple(root for root in (workspace_root, project_root) if root)
        # Server-verified attachment paths are often workspace-relative.
        # Resolve them against the explicitly supplied workspace root rather
        # than the process CWD, then apply the same containment check.
        attachment_base = Path(workspace_root or project_root).expanduser() if (workspace_root or project_root) else None
        if attachment_base is not None and source_attachment is not None:
            if isinstance(source_attachment, Mapping):
                source_value = next((source_attachment.get(key) for key in ("path", "file_path", "local_path", "resolved_path") if source_attachment.get(key)), None)
                if isinstance(source_value, (str, os.PathLike)) and not Path(source_value).expanduser().is_absolute():
                    source_attachment = {**source_attachment, "path": str(attachment_base / Path(source_value))}
            elif isinstance(source_attachment, (str, os.PathLike)) and not Path(source_attachment).expanduser().is_absolute():
                source_attachment = str(attachment_base / Path(source_attachment))
        temporary_source: Path | None = None
        source_path: Path | None = None
        inline_blob = None
        if isinstance(source_attachment, (bytes, bytearray)):
            inline_blob = bytes(source_attachment)
        elif isinstance(source_attachment, Mapping):
            for blob_key in ("content", "data", "bytes"):
                if isinstance(source_attachment.get(blob_key), (bytes, bytearray)):
                    inline_blob = bytes(source_attachment[blob_key])
                    break
        if inline_blob is not None:
            blob = inline_blob
            if len(blob) > self.max_attachment_bytes:
                raise AttachmentResolutionError("attachment exceeds bounded size")
            suffix = Path(str((source_attachment.get("name") or source_attachment.get("filename")) if isinstance(source_attachment, Mapping) else "document.xlsx")).suffix.casefold()
            if suffix not in SUPPORTED_XLSX_SUFFIXES:
                raise UnsupportedDocumentFormat(f"unsupported document format: {suffix or '(none)'}")
            fd, name = tempfile.mkstemp(prefix="aoitalk-document-", suffix=suffix)
            os.close(fd)
            temporary_source = Path(name)
            temporary_source.write_bytes(blob)
            source_path = temporary_source
        elif source_attachment is not None:
            source_path = resolve_input_attachment(source_attachment, allowed_roots=allowed_roots, max_bytes=self.max_attachment_bytes)
        elif active_intent == "document":
            # Creation from notes is supported with a new workbook.  Update
            # requests without a source fail closed rather than reading an
            # arbitrary project file.
            source_path = None
        context_was_provided = context is not None
        context = context or _context_new(
            self.config,
            session_context=session_context,
            project_metadata=project_metadata,
        )
        own_context = not context_was_provided and context is not None
        workbook = None
        try:
            async def progress(stage: str, status: str = "running", message: str = "") -> None:
                if not callable(progress_callback):
                    return
                payload = {"workflow": "document", "stage": stage[:80], "status": status[:40], "message": message[:500]}
                try:
                    callback_result = progress_callback(stage, payload)
                    if asyncio.iscoroutine(callback_result) or isinstance(callback_result, asyncio.Future):
                        await callback_result
                except Exception:
                    # Progress is observational; never fail document mutation
                    # because a websocket/UI callback disappeared.
                    logger.debug("document workflow progress callback failed", exc_info=True)

            await progress("started", "running", "ドキュメントを処理しています")
            if source_path is None:
                try:
                    from openpyxl import Workbook
                except Exception as exc:
                    raise DocumentWorkflowError("openpyxl is unavailable") from exc
                workbook = Workbook()
                raw = RawDocumentIR(source_path=None, sheets=(workbook.active.title,), context=context)
            else:
                workbook, raw = _load_raw_document(source_path, context=context, max_cells=self.max_cells)
            notes_blob = None
            if isinstance(current_notes, (bytes, bytearray)):
                notes_blob = bytes(current_notes)
            elif isinstance(current_notes, Mapping):
                for blob_key in ("content", "data", "bytes"):
                    if isinstance(current_notes.get(blob_key), (bytes, bytearray)):
                        notes_blob = bytes(current_notes[blob_key])
                        break
            if notes_blob is not None:
                blob = notes_blob
                if len(blob) > self.max_attachment_bytes:
                    raise AttachmentResolutionError("notes attachment exceeds bounded size")
                notes_text = blob[:DEFAULT_MAX_TEXT_CHARS].decode("utf-8", errors="replace")
            elif isinstance(current_notes, Mapping) and current_notes.get("path"):
                notes_path = Path(str(current_notes["path"])).expanduser()
                if not notes_path.is_absolute():
                    notes_path = (attachment_base or Path.cwd()) / notes_path
                try:
                    notes_path = notes_path.resolve(strict=True)
                except OSError as exc:
                    raise AttachmentResolutionError("notes attachment does not exist") from exc
                if allowed_roots and not any(_is_relative_to(notes_path, Path(root).expanduser().resolve()) for root in allowed_roots):
                    raise AttachmentResolutionError("notes attachment is outside authorized roots")
                if notes_path.stat().st_size > self.max_attachment_bytes:
                    raise AttachmentResolutionError("notes attachment exceeds bounded size")
                notes_text = _text_from_material(notes_path)
            elif isinstance(current_notes, (str, os.PathLike, Path)):
                try:
                    notes_candidate = Path(current_notes)
                    notes_exists = notes_candidate.exists()
                except (OSError, ValueError):
                    notes_exists = False
                    notes_candidate = Path(".")
                if not notes_exists:
                    notes_text = _text_from_material(current_notes)
                else:
                    notes_path = notes_candidate.expanduser().resolve(strict=True)
                    if allowed_roots and not any(_is_relative_to(notes_path, Path(root).expanduser().resolve()) for root in allowed_roots):
                        raise AttachmentResolutionError("notes attachment is outside authorized roots")
                    if notes_path.stat().st_size > self.max_attachment_bytes:
                        raise AttachmentResolutionError("notes attachment exceeds bounded size")
                    notes_text = _text_from_material(notes_path)
            else:
                notes_text = _text_from_material(current_notes)
            new_values = extract_current_values(notes_text)
            # Infer adjacent values from a sheet when labels are represented in
            # separate cells and notes use a concise mapping.
            if workbook is not None:
                for key, value in _new_values_from_adjacent_cells(workbook).items():
                    # Notes remain authoritative; workbook values are only old
                    # values and therefore never overwrite a supplied note.
                    raw = replace(raw, current_values={**raw.current_values, key: raw.current_values.get(key, value)})
            safe_objective = _safe_text(objective, old_values=raw.current_values, new_values=new_values) if objective else "Adapt the supplied document to the current project values"
            projection = build_cloud_projection(
                raw,
                intent=active_intent,
                objective=safe_objective,
                current_values=new_values,
                use_foundation=(
                    self._cloud_enabled(self.config)
                    if self.config is not None
                    else bool(coordinator or cloud_advisor or self.coordinator)
                ),
            )
            await progress("advisory", "running", "構造化アドバイスを確認しています")
            advisory_text, advisory_status = await self._consult_cloud(
                projection,
                intent=active_intent,
                # Never use raw notes as a fallback objective: notes may
                # contain credentials, private addresses or internal paths.
                objective=safe_objective,
                coordinator=coordinator or cloud_advisor or self.coordinator,
                consent=bool(cloud_consent),
            )
            try:
                plan = validate_document_plan(advisory_text, allowed_node_ids=raw.node_ids, intent=active_intent) if advisory_text else DocumentPlan(intent=active_intent, advisory_status="not_requested")
                if _plan_contains_local_literal(plan, (*raw.current_values.values(), *new_values.values())):
                    raise DocumentPlanValidationError("advisory operation contains local literal")
                plan = _rebind_plan_aliases(plan, context, new_values)
            except DocumentPlanValidationError as exc:
                logger.warning("document advisory rejected reason=%s", str(exc))
                plan = DocumentPlan(intent=active_intent, advisory_status="invalid", advisory_text=advisory_text[:DEFAULT_MAX_ADVISORY_CHARS])
                advisory_status = "invalid"
            # If the advisor is unavailable or returns informational prose,
            # local rebinding still proceeds using current notes.
            if plan.advisory_status == "not_requested" and advisory_status != "not_requested":
                plan = DocumentPlan(intent=active_intent, advisory_status=advisory_status, advisory_text=advisory_text)
            changed = apply_document_plan(workbook, raw, plan, new_values)
            if source_path is None and new_values:
                sheet = workbook.active
                if sheet["A1"].value in (None, ""):
                    sheet["A1"] = "Document"
                row = max(2, sheet.max_row + 1)
                for key, value in new_values.items():
                    sheet.cell(row=row, column=1, value=key)
                    sheet.cell(row=row, column=2, value=value)
                    row += 1
            target = self._output_path(
                source_path,
                output_path,
                output_dir,
                allowed_roots=allowed_roots,
                workflow_id=(
                    str(getattr(context, "context_id", "") or "")
                    or str(uuid.uuid4())
                ),
            )
            # Avoid mutating an input file unless the caller explicitly chose
            # that exact target.  Save through a temporary sibling to prevent
            # partially written files on crashes.
            temp_target = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            workbook.save(temp_target)
            os.replace(temp_target, target)
            digest = _artifact_digest(target)
            status = "created" if source_path is None else ("completed" if advisory_status == "ok" else "local_fallback")
            await progress("completed", "completed", "ドキュメントを生成しました")
            return DocumentWorkflowResult(
                status=status,
                output_path=target,
                plan=plan,
                advisory_status=advisory_status,
                warnings=tuple(
                    warning for warning in (("cloud_advisory_unavailable" if advisory_status not in {"ok", "not_requested"} else ""),) if warning
                ),
                changed_nodes=changed,
                artifact_sha256=digest,
                provenance={"workflow": "document", "intent": active_intent, "node_count": len(raw.nodes), **_context_provenance(context)},
            )
        except DocumentWorkflowError:
            raise
        except Exception as exc:
            logger.warning("document workflow failed exception_type=%s", type(exc).__name__)
            raise DocumentWorkflowError("document workflow failed") from exc
        finally:
            if workbook is not None:
                try:
                    workbook.close()
                except Exception:
                    pass
            if temporary_source is not None:
                try:
                    temporary_source.unlink(missing_ok=True)
                except OSError:
                    pass
            if own_context and context is not None:
                try:
                    close = getattr(context, "close", None)
                    if callable(close):
                        close()
                except Exception:
                    pass


__all__ = [
    "AttachmentResolutionError",
    "CloudProjectionIR",
    "DocumentNode",
    "DocumentPlan",
    "DocumentPlanValidationError",
    "DocumentWorkflow",
    "DocumentWorkflowError",
    "DocumentWorkflowResult",
    "ParsedDocumentCommand",
    "RawDocumentIR",
    "UnsupportedDocumentFormat",
    "apply_document_plan",
    "build_raw_document_ir",
    "build_cloud_projection",
    "detect_document_intent",
    "extract_document_values",
    "extract_current_values",
    "is_document_intent",
    "load_document_ir",
    "parse_document_command",
    "parse_advisory_plan",
    "parse_template_command",
    "parse_workflow_command",
    "resolve_input_attachment",
    "resolve_attachment",
    "read_xlsx_document",
    "safe_document_projection",
    "validate_document_plan",
]
