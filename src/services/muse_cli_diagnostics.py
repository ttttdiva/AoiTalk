"""Closed-vocabulary diagnostics for the Muse CLI subprocess boundary.

The adapter used to discard everything about a failed ``muse-cli`` process, so a
connect-phase failure and a lost response both ended as ``thread_create_uncertain``.
This module reduces process outcomes to closed labels and bounded numbers. For
an abnormal send exit only, callers may additionally persist a separately
redacted stderr tail (never the raw process text):

* raw stderr/stdout, paths, URLs, hosts, ids, tokens and cookies are never retained;
* diagnostic labels use fixed vocabularies; unknown labels become ``"other"`` or are dropped;
* the bounded stderr tail strips or replaces sensitive data and fails closed if redaction fails.

It has no imports from the rest of the application so it can be used by the
runner, the adapter and the durable binding store without cycles.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# Sub-commands the adapter (and nothing else) may launch.  ``send``'s text argument is
# deliberately never part of a label.
COMMAND_LABELS = frozenset({
    "--version", "--help", "history --help", "send --help", "session-start --help",
    "session-start", "send", "history", "other",
})
CAUSE_CODES = frozenset({
    "muse_error", "unhandled_exception", "invalid_input", "cli_contract_mismatch",
    "cli_unavailable", "credential_unavailable", "remote_target_disabled",
    "remote_authentication_failed", "remote_vm_unbound", "remote_capability_required",
    "remote_local_capability_rejected", "binding_store_unavailable", "thread_binding_missing",
    "thread_binding_mismatch", "thread_binding_unsafe", "thread_create_uncertain",
    "thread_create_failed", "binding_persist_failed", "baseline_history_unavailable",
    "input_missing", "input_too_long", "send_acceptance_unconfirmed",
    "send_response_uncorrelated", "send_cancelled_local", "outbound_privacy_gate_required",
    "outbound_privacy_denied", "dedicated_home_required", "role_context_invalid",
    "role_revision_mismatch", "current_authorization_mismatch", "thread_binding_invalid",
    "remote_gateway_error", "remote_timeout", "invalid_provider_response", "cli_process_failed",
})
STDERR_CLASSES = frozenset({
    "empty", "process_timeout", "python_exception", "auth_check_http", "hatch_token_http", "api_session_http",
    "api_session_non_json", "vm_unavailable", "cookies_missing_or_empty", "cli_timeout", "gateway_stream_reset",
    "gateway_non_json", "gateway_api_error", "gateway_error", "auth_error_other", "usage_error", "other",
})
STAGES = frozenset({
    "api_session_get", "auth_check_post", "hatch_token_post", "ws_connect", "noise_handshake",
    "gateway_init_other", "api_call", "cookies_load", "unmapped",
})
# Stages that all precede the ``session.start`` request (muse_cli/cli.py: connect() runs first).
CONNECT_STAGES = frozenset({
    "api_session_get", "auth_check_post", "hatch_token_post", "ws_connect", "noise_handshake",
    "gateway_init_other", "cookies_load",
})
PHASES = frozenset({"proxy", "DNS", "TLS", "connect", "handshake", "other"})
EXCEPTION_TYPES = frozenset({
    "curl_cffi.curl.CurlError", "CurlError", "AuthError", "GatewayError", "TimeoutError", "ConnectionError",
    "ConnectionResetError", "ConnectionRefusedError", "OSError", "FileNotFoundError", "PermissionError",
    "KeyError", "ValueError", "TypeError", "AssertionError", "RuntimeError", "JSONDecodeError",
    "json.decoder.JSONDecodeError", "ImportError", "ModuleNotFoundError", "UnicodeDecodeError",
    "ssl.SSLError", "SSLError", "BrokenPipeError", "MemoryError", "KeyboardInterrupt", "SystemExit",
})

_FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+), in (\S+)')
_EXCEPTION_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]{0,63}(?:Error|Exception|Exit|Interrupt)):")
_CURL_CODE = re.compile(r"curl: \((\d+)\)")
_CODE = re.compile(r"[a-z0-9_.:]{1,96}")
_MUSE_FILES = frozenset({"gateway.py", "cli.py", "update.py"})
_MAX_TEXT = 32_768
MAX_STDERR_TAIL_CHARS = 2_048
_MAX_STDERR_INPUT_CHARS = 1_000_000
_STDERR_TAIL_TRUNCATION_MARKER = "[stderr tail truncated] "

_ANSI_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])"
)
_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SENSITIVE_KEY_VALUE = re.compile(
    r"(?i)([\"']?(?:[a-z0-9_.-]*?(?:authorization|bearer|cookie|set[-_]?cookie|"
    r"token|secret|api[_-]?key|apikey|password|passwd|credential|session)"
    r"[a-z0-9_.-]*)[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^,;}]+)"
)
_SENSITIVE_HEADER_PREFIX = re.compile(
    r"(?i)\b(?:authorization|cookie|set[-_]?cookie)\s*[:=]"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_JWT = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_UUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_LONG_HEX = re.compile(r"(?i)\b[0-9a-f]{24,}\b")
_LONG_BASE64 = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{24,}={0,2}(?![A-Za-z0-9+/_-])")
_URL = re.compile(r"(?i)\b(?:https?|wss?|ftp)://[^\s<>\"']+")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\\\[^\\/\s]+\\[^\\/\s]+\\)[^\r\n<>\"']*"
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9.])/(?:[^\s<>\"']+)")

_CURL_TLS = frozenset({35, 51, 58, 59, 60, 64, 66, 77, 80, 83, 90, 91})
_CURL_HANDSHAKE = frozenset({1, 8, 52, 55, 56, 61})


def command_label(argv: Sequence[Any]) -> str:
    """Closed label for a muse-cli argv (the executable and any text argument are dropped)."""
    rest = tuple(str(item) for item in tuple(argv)[1:])
    for label in ("--version", "--help", "history --help", "send --help", "session-start --help"):
        if rest == tuple(label.split()):
            return label
    if rest and rest[0] in {"session-start", "send", "history"}:
        return rest[0]
    return "other"


def closed_exception_type(name: Any) -> str:
    value = str(name or "")
    return value if value in EXCEPTION_TYPES else "other"


def _bounded_int(value: Any, low: int, high: int) -> int | None:
    return value if type(value) is int and low <= value <= high else None


def classify_cli_stderr(text: str, *, timed_out: bool = False) -> dict[str, Any]:
    """Reduce a stderr text to closed labels.  The text itself is never returned."""
    if timed_out:
        return {"stderr_class": "process_timeout"}
    body = (text or "")[-_MAX_TEXT:]
    if not body.strip():
        return {"stderr_class": "empty"}
    frames_present = "Traceback (most recent call last)" in body or _FRAME.search(body) is not None
    if frames_present:
        return {"stderr_class": "python_exception", **_classify_traceback(body)}
    for pattern, label in (
        (r"auth/check -> (\d{3})", "auth_check_http"),
        (r"hatch/token -> (\d{3})", "hatch_token_http"),
        (r"api/session -> (\d{3})", "api_session_http"),
    ):
        match = re.search(pattern, body)
        if match:
            return {"stderr_class": label, "http_status": _bounded_int(int(match.group(1)), 100, 599)}
    if "api/session returned non-JSON" in body:
        return {"stderr_class": "api_session_non_json"}
    if "VM unavailable" in body:
        return {"stderr_class": "vm_unavailable"}
    if re.search(r"no cookies at|cookies file .* is empty|could not parse cookies", body):
        return {"stderr_class": "cookies_missing_or_empty"}
    if body.lstrip().startswith("timeout:"):
        return {"stderr_class": "cli_timeout"}
    if "gateway error:" in body:
        match = re.search(r"gateway status=(-?\d+)", body)
        extra = {"gateway_status": _bounded_int(int(match.group(1)), -1, 599)} if match else {}
        for needle, label in (("stream reset", "gateway_stream_reset"), ("non-JSON response", "gateway_non_json"),
                              ("api error:", "gateway_api_error")):
            if needle in body:
                return {"stderr_class": label, **extra}
        return {"stderr_class": "gateway_error", **extra}
    if body.lstrip().startswith("auth error:"):
        return {"stderr_class": "auth_error_other"}
    if re.search(r"^usage:|error: ", body, re.M):
        return {"stderr_class": "usage_error"}
    return {"stderr_class": "other"}


def _classify_traceback(body: str) -> dict[str, Any]:
    lines = body.splitlines()
    frames: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        match = _FRAME.match(line)
        if not match:
            continue
        hint_parts = []
        for follower in lines[index + 1:index + 3]:
            if _FRAME.match(follower) or not follower.strip():
                break
            hint_parts.append(follower.strip())
        frames.append({"file": re.split(r"[\\/]", match.group(1))[-1], "function": match.group(3), "hint": " ".join(hint_parts)})
    out: dict[str, Any] = {}
    muse = [frame for frame in frames if frame["file"] in _MUSE_FILES]
    stage = None
    if muse:
        deepest = muse[-1]
        function, hint = deepest["function"], deepest["hint"]
        if function == "fetch_session_info":
            stage = "api_session_get"
        elif function == "fetch_access_token":
            stage = "auth_check_post"
        elif function == "fetch_hatch_token":
            stage = "hatch_token_post"
        elif deepest["file"] == "gateway.py" and function == "__init__":
            if "ws.connect" in hint:
                stage = "ws_connect"
            elif any(token in hint for token in ("ws.send_bytes", "ws.recv", "noise.")):
                stage = "noise_handshake"
            else:
                stage = "gateway_init_other"
        elif deepest["file"] == "gateway.py" and function in {"request", "call_json", "_read_frame"}:
            stage = "api_call"
        elif function == "load_cookies":
            stage = "cookies_load"
        else:
            stage = "unmapped"
        out["stage"] = stage
    exception_lines = [line.strip() for line in lines if _EXCEPTION_LINE.match(line.strip())]
    if exception_lines:
        out["exception_type"] = closed_exception_type(_EXCEPTION_LINE.match(exception_lines[-1]).group(1))
    curl_lines = [line for line in exception_lines if "CurlError" in line]
    if curl_lines:
        # keyword flags come only from the exception message line, never from frames or paths
        text = curl_lines[0].casefold()
        match = _CURL_CODE.search(curl_lines[0])
        code = _bounded_int(int(match.group(1)), 0, 100_000) if match else None
        if "proxy" in text or code in {5, 97}:
            phase = "proxy"
        elif code == 6 or (code is None and "resolve" in text):
            phase = "DNS"
        elif code in _CURL_TLS or (code is None and any(t in text for t in ("ssl", "tls", "certificate"))):
            phase = "TLS"
        elif code == 7 or (code is None and "refused" in text):
            phase = "connect"
        elif code in _CURL_HANDSHAKE or stage in {"ws_connect", "noise_handshake"}:
            phase = "handshake"
        else:
            phase = "other"
        if code is not None:
            out["curl_code"] = code
        out["phase_class"] = phase
    return out


def sanitize_cause(value: Any) -> dict[str, Any] | None:
    """Re-validate a cause mapping so only closed, bounded values can ever be stored or returned."""
    if not isinstance(value, Mapping):
        return None
    out: dict[str, Any] = {}
    code = str(value.get("cause_code") or "").strip().casefold().replace("-", "_")
    if code in CAUSE_CODES and _CODE.fullmatch(code):
        out["cause_code"] = code
    command = str(value.get("command") or "")
    if command:
        out["command"] = command if command in COMMAND_LABELS else "other"
    for key, vocabulary in (("stderr_class", STDERR_CLASSES), ("stage", STAGES), ("phase_class", PHASES)):
        item = value.get(key)
        if isinstance(item, str):
            out[key] = item if item in vocabulary else "other"
    for key in ("exception_type", "cause_type"):
        item = value.get(key)
        if isinstance(item, str) and item:
            out[key] = closed_exception_type(item)
    for key, low, high in (("returncode", -1_000_000, 1_000_000), ("duration_ms", 0, 86_400_000),
                           ("curl_code", 0, 100_000), ("http_status", 100, 599), ("gateway_status", -1, 599)):
        number = _bounded_int(value.get(key), low, high)
        if number is not None:
            out[key] = number
    if isinstance(value.get("timed_out"), bool):
        out["timed_out"] = value["timed_out"]
    return out or None


def build_cause(
    *,
    cause_code: str,
    command: str | None = None,
    returncode: Any = None,
    duration_ms: Any = None,
    timed_out: Any = None,
    diagnostics: Mapping[str, Any] | None = None,
    cause_type: str | None = None,
) -> dict[str, Any]:
    """Assemble and sanitize one cause record for persistence / the outcome result."""
    merged: dict[str, Any] = {"cause_code": cause_code, "command": command, "returncode": returncode,
                              "duration_ms": duration_ms, "timed_out": timed_out, "cause_type": cause_type}
    if isinstance(diagnostics, Mapping):
        merged.update({key: diagnostics.get(key) for key in (
            "stderr_class", "stage", "phase_class", "curl_code", "http_status", "gateway_status", "exception_type")})
    return sanitize_cause(merged) or (
        {"cause_code": "muse_error"} if cause_code in CAUSE_CODES else {}
    )


def _prompt_echo_grams(prompt: str | None) -> set[str]:
    if not isinstance(prompt, str) or len(prompt) < 20:
        return set()
    return {prompt[index : index + 20] for index in range(len(prompt) - 19)}


def _line_echoes_prompt(line: str, prompt_grams: set[str]) -> bool:
    return any(line[index : index + 20] in prompt_grams for index in range(len(line) - 19))


def _redact_stderr_body(text: str, *, prompt: str | None) -> str:
    """Remove sensitive stderr content before any durable/public projection."""

    if len(text) > _MAX_STDERR_INPUT_CHARS:
        raise ValueError("stderr input exceeds the redaction work bound")
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    body = _ANSI_ESCAPE.sub("", body)
    body = _CONTROL_CHARACTER.sub("", body)
    prompt_grams = _prompt_echo_grams(prompt)
    safe_lines: list[str] = []
    for line in body.split("\n"):
        if prompt_grams and _line_echoes_prompt(line, prompt_grams):
            safe_lines.append("[prompt echo line removed]")
            continue
        if _WINDOWS_ABSOLUTE_PATH.search(line) or _POSIX_ABSOLUTE_PATH.search(line):
            safe_lines.append("[absolute path line removed]")
            continue
        line = _SENSITIVE_KEY_VALUE.sub(r"\1[REDACTED]", line)
        sensitive_header = _SENSITIVE_HEADER_PREFIX.search(line)
        if sensitive_header:
            line = (
                line[: sensitive_header.start()]
                + line[sensitive_header.start() : sensitive_header.end()]
                + "[REDACTED]"
            )
            safe_lines.append(line)
            continue
        line = _BEARER_VALUE.sub("Bearer [REDACTED]", line)
        for pattern, replacement in (
            (_URL, "[URL REDACTED]"),
            (_EMAIL, "[EMAIL REDACTED]"),
            (_JWT, "[JWT REDACTED]"),
            (_UUID, "[UUID REDACTED]"),
            (_LONG_HEX, "[LONG VALUE REDACTED]"),
            (_LONG_BASE64, "[LONG VALUE REDACTED]"),
        ):
            line = pattern.sub(replacement, line)
        if "=" in line:
            key, value = line.split("=", 1)
            if len(value.strip()) >= 24:
                line = key + "=[LONG VALUE REDACTED]"
        safe_lines.append(line)
    # Do not persist newline, tab, or other control characters; line boundaries
    # become a printable delimiter after per-line redaction.
    return " | ".join(safe_lines)


def sanitize_stderr_tail(text: Any, *, prompt: str | None = None) -> dict[str, Any]:
    """Return a redacted, bounded stderr tail, failing closed if scrubbing fails.

    Newlines are retained as separators so prompt-reflection detection can drop
    complete lines. Other control characters and ANSI escapes are removed.
    """

    raw_length: int
    try:
        raw = text if isinstance(text, str) else str(text or "")
        raw_length = len(raw)
    except Exception:
        raw = ""
        raw_length = 0
    try:
        redacted = _redact_stderr_body(raw, prompt=prompt)
        truncated = len(redacted) > MAX_STDERR_TAIL_CHARS
        if truncated:
            keep = MAX_STDERR_TAIL_CHARS - len(_STDERR_TAIL_TRUNCATION_MARKER)
            redacted = _STDERR_TAIL_TRUNCATION_MARKER + redacted[-keep:]
        return {
            "stderr_tail": redacted,
            "stderr_tail_truncated": truncated,
            "stderr_redaction_failed": False,
            "stderr_length": min(raw_length, 2_147_483_647),
        }
    except Exception:
        # Never attach the exception message: the failure may itself contain
        # the original secret or provider output.
        return {
            "stderr_tail": "redaction_failed",
            "stderr_tail_truncated": False,
            "stderr_redaction_failed": True,
            "stderr_length": min(raw_length, 2_147_483_647),
        }


def sanitize_cli_failure_diagnostic(value: Any) -> dict[str, Any] | None:
    """Validate the fixed-key process-failure projection used by state/events."""

    if not isinstance(value, Mapping):
        return None
    returncode = _bounded_int(value.get("returncode"), -1_000_000, 1_000_000)
    send_ordinal = _bounded_int(value.get("send_ordinal"), 1, 10_000)
    if send_ordinal is None:
        return None
    raw_tail = value.get("stderr_tail")
    existing_length = _bounded_int(value.get("stderr_length"), 0, 2_147_483_647)
    if value.get("stderr_redaction_failed") is True:
        tail = {
            "stderr_tail": "redaction_failed",
            "stderr_tail_truncated": False,
            "stderr_redaction_failed": True,
            "stderr_length": existing_length if existing_length is not None else 0,
        }
    else:
        tail = sanitize_stderr_tail(raw_tail if isinstance(raw_tail, str) else "")
        if existing_length is not None:
            tail["stderr_length"] = max(existing_length, tail["stderr_length"])
        tail["stderr_tail_truncated"] = bool(
            tail["stderr_tail_truncated"] or value.get("stderr_tail_truncated") is True
        )
    result: dict[str, Any] = {
        "returncode": returncode,
        "send_ordinal": send_ordinal,
        **tail,
    }
    cause = sanitize_cause(value.get("cause"))
    if cause:
        result["cause"] = cause
    return result
