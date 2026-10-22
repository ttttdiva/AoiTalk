"""Safe structured extraction for user-supplied RFC 822 and Outlook mail files."""

from __future__ import annotations

import codecs
import re
from dataclasses import asdict, dataclass
from email import policy
from email.headerregistry import AddressHeader
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
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


class _MailHTMLText(HTMLParser):
    """Extract text locally without rendering HTML or following any resources."""

    _hidden = {"head", "script", "style", "template", "iframe", "object"}
    _blocks = {"br", "hr", "div", "p", "li", "ul", "ol", "table", "tr", "td", "th",
               "blockquote", "pre", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body" and self.hidden == ["head"]:
            self.hidden.clear()  # HTML permits the closing </head> to be omitted.
        if tag in self._hidden:
            self.hidden.append(tag)
        elif not self.hidden and tag in self._blocks:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.hidden:
            index = len(self.hidden) - 1 - self.hidden[::-1].index(tag)
            del self.hidden[index:]
        elif not self.hidden and tag in self._blocks:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _html_body_text(value: str | bytes, charset: str | None = None) -> str:
    if isinstance(value, bytes):
        # HTML streams have their own encoding; MSG stringEncoding is not a
        # reliable substitute. Never turn bytes into repr text or replace errors.
        encoding = None
        for marker, candidate in [(codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                                  (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
                                  (codecs.BOM_UTF8, "utf-8-sig")]:
            if value.startswith(marker):
                encoding = candidate
                break
        if encoding is None and not charset:
            for meta in re.findall(rb"<meta\b[^>]*>", value, flags=re.IGNORECASE):
                declared = re.search(rb"\bcharset\s*=\s*[\"']?\s*([\w.-]+)", meta, flags=re.IGNORECASE)
                if declared:
                    encoding = declared[1].decode("ascii")
                    break
        try:
            value = value.decode(encoding or charset or "utf-8", errors="strict")
        except (LookupError, UnicodeError) as exc:
            raise ValueError("メールHTML本文の文字コードを解析できません") from exc
    parser = _MailHTMLText()
    parser.feed(value.replace("\x00", ""))
    parser.close()
    # Preserve adjacent inline text, but keep block boundaries searchable.
    return "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip())


def _part_body(part: Message) -> str:
    if part.get_content_type() == "text/html":
        return _html_body_text(part.get_payload(decode=True) or b"", part.get_content_charset())
    return str(part.get_content()).replace("\x00", "").strip()


def _message_body(message: Message) -> str:
    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        try:
            return _part_body(body)
        except (LookupError, UnicodeError):
            pass
    if message.is_multipart():
        parts: list[str] = []
        for part in message.walk():
            if part.get_content_maintype() == "text" and not part.get_filename():
                try:
                    parts.append(_part_body(part))
                except (LookupError, UnicodeError):
                    continue
        return "\n\n".join(part for part in parts if part)
    try:
        return _part_body(message)
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
        body = str(getattr(message, "body", None) or "").replace("\x00", "").strip()
        if not body:
            content_type = Message()
            content_type["Content-Type"] = str(_msg_header(message, "Content-Type") or "text/html")
            body = _html_body_text(getattr(message, "htmlBody", None) or "", content_type.get_content_charset())
        return ParsedMail(
            subject=_clean(getattr(message, "subject", None)),
            sender=_clean(getattr(message, "sender", None)),
            to=to,
            cc=cc,
            bcc=bcc,
            date=_clean(getattr(message, "date", None)),
            body=body,
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
