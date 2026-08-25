"""Split an imported mail body into addressable messages.

Outlook ``.msg`` exports often contain the whole quoted conversation in one
body.  The outer message remains the immutable archive; these derived messages
exist so an Inbox summary can cite the exact exchange that supports a claim.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


_HEADER_RE = re.compile(
    r"(?im)^(?:From|差出人)\s*:\s*(?P<from>.+?)\s*$"
    r"\n^(?:Sent|Date|送信日時)\s*:\s*(?P<date>.+?)\s*$"
    r"\n^(?:To|宛先)\s*:\s*(?P<to>.+?)\s*$"
    r"(?:\n^(?:Cc|CC|ＣＣ)\s*:\s*(?P<cc>.*?)\s*$)?"
    r"\n^(?:Subject|件名)\s*:\s*(?P<subject>.+?)\s*$"
)


@dataclass(frozen=True)
class MailThreadMessage:
    source_key: str
    message_id: str
    subject: str
    date: str
    sender: str
    to: str
    cc: str
    body: str


def _text(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _normalize_body(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _source_key(*, subject: str, date: str, sender: str, body: str) -> str:
    canonical = "\n".join(
        [
            " ".join(date.casefold().split()),
            " ".join(sender.casefold().split()),
            " ".join(subject.casefold().split()),
            _normalize_body(body),
        ]
    )
    return "mail-message:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _message(
    *,
    subject: Any,
    date: Any,
    sender: Any,
    to: Any,
    cc: Any,
    body: Any,
    message_id: Any = "",
) -> MailThreadMessage | None:
    clean_body = _normalize_body(body)
    clean_subject = _text(subject) or "（件名なし）"
    clean_date = _text(date)
    clean_sender = _text(sender)
    if not any([clean_body, clean_date, clean_sender]):
        return None
    clean_message_id = _text(message_id).strip("<> ").casefold()
    return MailThreadMessage(
        source_key=(
            f"mail-message-id:{clean_message_id}"
            if clean_message_id
            else _source_key(
                subject=clean_subject,
                date=clean_date,
                sender=clean_sender,
                body=clean_body,
            )
        ),
        message_id=clean_message_id,
        subject=clean_subject,
        date=clean_date,
        sender=clean_sender,
        to=_text(to),
        cc=_text(cc),
        body=clean_body,
    )


def split_mail_thread(mail: dict[str, Any]) -> list[MailThreadMessage]:
    """Return the outer message and quoted messages in chronological order.

    Quoted Outlook blocks are normally newest-to-oldest.  We parse that stable
    structure, de-duplicate repeated quoted copies, then reverse it so synthesis
    sees the business chronology.  If no quoted headers are present, one source
    message is returned.
    """

    body = _normalize_body(mail.get("body"))
    matches = list(_HEADER_RE.finditer(body))
    newest_first: list[MailThreadMessage] = []

    outer_body = body[: matches[0].start()].strip() if matches else body
    outer = _message(
        subject=mail.get("subject"),
        date=mail.get("date"),
        sender=mail.get("sender"),
        to=mail.get("to"),
        cc=mail.get("cc"),
        body=outer_body,
        message_id=mail.get("message_id"),
    )
    if outer is not None:
        newest_first.append(outer)

    raw_references = mail.get("references")
    if isinstance(raw_references, list):
        references = [_text(value).strip("<> ").casefold() for value in raw_references]
    else:
        references = [
            value.strip("<> ").casefold()
            for value in re.findall(r"<[^<>]+>|\S+", _text(raw_references))
        ]
    quoted_message_ids = list(reversed([value for value in references if value]))

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        quoted_body = body[match.end() : end].strip()
        parsed = _message(
            subject=match.group("subject"),
            date=match.group("date"),
            sender=match.group("from"),
            to=match.group("to"),
            cc=match.group("cc") or "",
            body=quoted_body,
            message_id=(
                quoted_message_ids[index]
                if index < len(quoted_message_ids)
                else ""
            ),
        )
        if parsed is not None:
            newest_first.append(parsed)

    unique_newest_first: list[MailThreadMessage] = []
    seen: set[str] = set()
    for message in newest_first:
        if message.source_key in seen:
            continue
        seen.add(message.source_key)
        unique_newest_first.append(message)
    return list(reversed(unique_newest_first))
