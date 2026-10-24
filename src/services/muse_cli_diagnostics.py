"""Closed-vocabulary diagnostics for the Muse CLI subprocess boundary.

The adapter used to discard everything about a failed ``muse-cli`` process, so a
connect-phase failure and a lost response both ended as ``thread_create_uncertain``.
This module reduces the process outcome to *labels and numbers only*:

* never the raw stderr/stdout, file paths, URLs, hosts, ids, tokens or cookies;
* every string value comes from a fixed vocabulary or is a validated code;
* anything outside the vocabulary becomes ``"other"`` or is dropped.

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
    if _CODE.fullmatch(code):
        out["cause_code"] = code
    command = str(value.get("command") or "")
    if command:
        out["command"] = command if command in COMMAND_LABELS else "other"
    for key, vocabulary in (("stderr_class", STDERR_CLASSES), ("stage", STAGES), ("phase_class", PHASES)):
        item = value.get(key)
        if isinstance(item, str) and item in vocabulary:
            out[key] = item
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
    return sanitize_cause(merged) or {"cause_code": "muse_error"}
