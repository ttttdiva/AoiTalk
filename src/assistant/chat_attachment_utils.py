"""Helpers for Web chat attachment metadata and prompt context."""

import re
from typing import Any, Dict, List, Optional


PROJECT_ATTACHMENT_CONTEXT_MARKER = "[AoiTalk verified Project attachment]"
_ATTACHMENT_REFERENCE_LINE_RE = re.compile(
    r"^\[添付(?:ファイル|画像|動画|音声):[^\]]+\].*$",
    re.IGNORECASE,
)


ALLOWED_ATTACHMENT_KEYS = {
    "name",
    "path",
    "project_relative_path",
    "kind",
    "registered",
    "size",
    "mime_type",
    "upload_failed",
    "error",
    "data_url",
}


def sanitize_chat_attachments(
    attachments: Any,
    *,
    include_binary: bool = True,
) -> List[Dict[str, Any]]:
    if not isinstance(attachments, list):
        return []

    sanitized: List[Dict[str, Any]] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue

        clean: Dict[str, Any] = {"name": name}
        for key in ALLOWED_ATTACHMENT_KEYS - {"name"}:
            if key == "data_url" and not include_binary:
                continue
            if key not in item:
                continue
            value = item.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                clean[key] = value
        sanitized.append(clean)

    return sanitized


def add_project_attachment_context_marker(
    attachment_context: Optional[str],
    attachments: List[Dict[str, Any]],
    project_id: Optional[str],
    *,
    require_registered: bool = True,
) -> Optional[str]:
    """Mark only server-validated Project attachment references for the LLM."""
    context = attachment_context.strip() if isinstance(attachment_context, str) else ""
    verified_items = verified_project_attachment_items(
        attachments,
        project_id,
        require_registered=require_registered,
    )
    has_project_attachment = bool(verified_items)
    if not has_project_attachment:
        return context or None

    verified_references = [
        f"[添付ファイル: {str(item.get('name') or '添付ファイル').replace(chr(10), ' ').replace(chr(13), ' ')}] {path}"
        for item, path in verified_items
    ]
    marker = PROJECT_ATTACHMENT_CONTEXT_MARKER
    verified_block = "\n".join([marker, *verified_references])
    safe_context = "\n".join(
        line
        for line in context.splitlines()
        if not _ATTACHMENT_REFERENCE_LINE_RE.match(line.strip())
    ).strip()
    return f"{safe_context}\n{verified_block}" if safe_context else verified_block


def verified_project_attachment_items(
    attachments: List[Dict[str, Any]],
    project_id: Optional[str],
    *,
    require_registered: bool = False,
) -> List[tuple[Dict[str, Any], str]]:
    """Return attachment paths validated against the selected Project.

    This is deliberately server-side structured state.  Consumers must not
    infer verification from the rendered ``PROJECT_ATTACHMENT_CONTEXT_MARKER``
    in user-visible prompt text, which can be forged by a caller.  The
    ``registered`` is optional metadata from the frontend upload projection,
    not a trust boundary.  Request boundaries must additionally validate
    filesystem existence/ownership before setting turn metadata.
    """

    if not project_id:
        return []
    project_prefix = f"_projects/project_{str(project_id).strip()}/".casefold()
    verified_items: List[tuple[Dict[str, Any], str]] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        if require_registered and item.get("registered") is not True:
            continue
        if item.get("upload_failed") or not isinstance(item.get("path"), str):
            continue
        normalized = item["path"].replace("\\", "/")
        if any(ch in "\r\n" or ord(ch) < 32 for ch in normalized):
            continue
        parts = normalized.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            continue
        if not normalized.casefold().startswith(project_prefix):
            continue
        verified_items.append((item, normalized))
    return verified_items


def has_verified_project_attachment(
    attachments: List[Dict[str, Any]],
    project_id: Optional[str],
    *,
    require_registered: bool = True,
) -> bool:
    """Return whether the server validated at least one Project attachment."""

    return bool(
        verified_project_attachment_items(
            attachments,
            project_id,
            require_registered=require_registered,
        )
    )


def build_message_with_attachment_context(
    message: str,
    attachment_context: Optional[str] = None,
) -> str:
    context = attachment_context.strip() if isinstance(attachment_context, str) else ""
    if not context:
        return message

    base = message.strip() or "添付ファイルを確認してください。"
    return f"{base}\n\n{context}"


def format_media_recognition_block(result: Any) -> str:
    def _value(key: str) -> str:
        if isinstance(result, dict):
            return str(result.get(key) or "")
        return str(getattr(result, key, "") or "")

    name = _value("name")
    model = _value("model")
    provider = _value("provider")
    text = _value("result")
    error = _value("error")
    label = "/".join(part for part in (provider, model) if part) or "未設定"
    body = text.strip() if not error else f"解析に失敗しました: {error}"
    return (
        "[添付解析結果]\n"
        f"以下は補助認識モデル（{label}）による添付「{name or '添付ファイル'}」の解析結果です。\n"
        "あなた自身は元のファイルを直接見ていません。結果に書かれていない詳細を推測で補わないでください。\n"
        "---\n"
        f"{body}\n"
        "[/添付解析結果]"
    )


def inject_media_recognition_results(
    attachment_context: Optional[str],
    results: List[Any],
) -> str:
    context = attachment_context.strip() if isinstance(attachment_context, str) else ""
    blocks = [format_media_recognition_block(result) for result in results]
    if context:
        return f"{context}\n\n" + "\n\n".join(blocks)
    return "\n\n".join(blocks)
