"""Helpers for Web chat attachment metadata and prompt context."""

from typing import Any, Dict, List, Optional


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
}


def sanitize_chat_attachments(attachments: Any) -> List[Dict[str, Any]]:
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
            if key not in item:
                continue
            value = item.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                clean[key] = value
        sanitized.append(clean)

    return sanitized


def build_message_with_attachment_context(
    message: str,
    attachment_context: Optional[str] = None,
) -> str:
    context = attachment_context.strip() if isinstance(attachment_context, str) else ""
    if not context:
        return message

    base = message.strip() or "添付ファイルを確認してください。"
    return f"{base}\n\n{context}"
