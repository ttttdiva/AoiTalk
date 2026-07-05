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
