"""Safe structured extraction for user-supplied RFC 822 and Outlook mail files."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from email import policy
from email.headerregistry import AddressHeader
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any


SUPPORTED_MAIL_EXTENSIONS = {".eml", ".msg"}


@dataclass(frozen=True)
class ParsedMail:
    subject: str
    sender: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    date: str
    body: str
    message_id: str
    in_reply_to: str
    references: list[str]

    @property
    def recipients(self) -> list[str]:
        return [*self.to, *self.cc, *self.bcc]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "recipients": self.recipients}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\x00", "").split()).strip()


def _message_body(message: Message) -> str:
    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        try:
            return str(body.get_content()).replace("\x00", "").strip()
        except (LookupError, UnicodeError):
            pass
    if message.is_multipart():
        parts: list[str] = []
        for part in message.walk():
            if part.get_content_maintype() == "text" and not part.get_filename():
                try:
                    parts.append(str(part.get_content()).replace("\x00", "").strip())
                except (LookupError, UnicodeError):
                    continue
        return "\n\n".join(part for part in parts if part)
    try:
        return str(message.get_content()).replace("\x00", "").strip()
    except (LookupError, UnicodeError):
        payload = message.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace").replace("\x00", "").strip()


def _address_values(message: Message, header: str) -> list[str]:
    values: list[str] = []
    for item in message.get_all(header, []):
        if isinstance(item, AddressHeader):
            values.extend(_clean(address) for address in item.addresses if _clean(address))
        elif _clean(item):
            values.append(_clean(item))
    return values


def _message_ids(values: list[Any]) -> list[str]:
    ids: list[str] = []
    for value in values:
        text = _clean(value)
        matches = re.findall(r"<[^<>]+>", text)
        candidates = matches or text.split()
        ids.extend(candidate for candidate in candidates if candidate and candidate not in ids)
    return ids


def _msg_header(message: Any, name: str) -> Any:
    header_dict = getattr(message, "headerDict", None) or {}
    for key, value in header_dict.items():
        if str(key).casefold() == name.casefold():
            return value
    header = getattr(message, "header", None)
    if header is not None and hasattr(header, "get"):
        return header.get(name)
    return None


def parse_eml_bytes(data: bytes) -> ParsedMail:
    message = BytesParser(policy=policy.default).parsebytes(data)
    return ParsedMail(
        subject=_clean(message.get("subject")),
        sender=_clean(message.get("from")),
        to=_address_values(message, "to"),
        cc=_address_values(message, "cc"),
        bcc=_address_values(message, "bcc"),
        date=_clean(message.get("date")),
        body=_message_body(message),
        message_id=_clean(message.get("message-id")),
        in_reply_to=_clean(message.get("in-reply-to")),
        references=_message_ids(message.get_all("references", [])),
    )


def parse_msg_file(path: str | Path) -> ParsedMail:
    try:
        import extract_msg
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise RuntimeError(".msg の解析には extract-msg が必要です") from exc

    message = extract_msg.Message(str(path))
    try:
        to = [_clean(getattr(message, "to", None))] if _clean(getattr(message, "to", None)) else []
        cc = [_clean(getattr(message, "cc", None))] if _clean(getattr(message, "cc", None)) else []
        bcc = [_clean(getattr(message, "bcc", None))] if _clean(getattr(message, "bcc", None)) else []
        return ParsedMail(
            subject=_clean(getattr(message, "subject", None)),
            sender=_clean(getattr(message, "sender", None)),
            to=to,
            cc=cc,
            bcc=bcc,
            date=_clean(getattr(message, "date", None)),
            body=str(getattr(message, "body", None) or "").replace("\x00", "").strip(),
            message_id=_clean(
                getattr(message, "messageId", None)
                or getattr(message, "message_id", None)
                or _msg_header(message, "Message-ID")
            ),
            in_reply_to=_clean(_msg_header(message, "In-Reply-To")),
            references=_message_ids([_msg_header(message, "References")]),
        )
    finally:
        close = getattr(message, "close", None)
        if callable(close):
            close()


def parse_mail_file(path: str | Path) -> ParsedMail:
    source = Path(path)
    extension = source.suffix.casefold()
    if extension not in SUPPORTED_MAIL_EXTENSIONS:
        raise ValueError(f"未対応のメール形式です: {extension or '(拡張子なし)'}")
    if extension == ".eml":
        return parse_eml_bytes(source.read_bytes())
    return parse_msg_file(source)
