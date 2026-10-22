"""Redact credential-bearing URL/HTTP diagnostics before any log handler."""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit, urlunsplit


_URL = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
_FIELD = re.compile(r"(?i)\b(access_token|refresh_token|client_secret|authorization_code|code|state|upload_id|session_id)\b([\"']?\s*[:=]\s*[\"']?)([^\s&\"',}\]>]+)")
_AUTH = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")


def redact_http_diagnostic(value: str) -> str:
    def url(match):
        try:
            parsed = urlsplit(match.group())
            host = parsed.hostname or "redacted"
            if ":" in host:
                host = f"[{host}]"
            port = f":{parsed.port}" if parsed.port else ""
            return urlunsplit((parsed.scheme, host + port, parsed.path,
                "[redacted]" if parsed.query else "", "[redacted]" if parsed.fragment else ""))
        except ValueError:
            return "[redacted URL]"
    value = _URL.sub(url, value)
    value = _FIELD.sub(lambda match: match[1] + match[2] + "[redacted]", value)
    return _AUTH.sub(lambda match: match.group().split()[0] + " [redacted]", value)


class HttpSecretLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            # HTTPCore's DEBUG records expose arbitrary response-header bytes
            # (Location/Set-Cookie included). Preserve event diagnostics, never
            # header/body representations, even when DEBUG is requested.
            if record.name.startswith("httpcore") and ("headers" in message or "body" in message):
                message = message.split(" ", 1)[0] + " [HTTP details redacted]"
            record.msg = redact_http_diagnostic(message)
            record.args = ()
            if record.exc_info and record.name.startswith(("httpx", "httpcore")):
                record.exc_info = (RuntimeError, RuntimeError("HTTP transport exception details redacted"), None)
                record.exc_text = None
        except Exception:
            record.msg, record.args = "HTTP diagnostic redacted", ()
            record.exc_info, record.exc_text = None, None
        return True


def install_http_log_redaction() -> None:
    # Logger filters run before propagation to file, console, caplog or other
    # independently attached handlers. Parent filters do not cover children.
    for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11",
                 "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, HttpSecretLogFilter) for item in logger.filters):
            logger.addFilter(HttpSecretLogFilter())
