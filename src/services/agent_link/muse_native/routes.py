"""The deliberately small set of Muse methods used by the native link."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote, urlencode

from ..codes import AgentLinkFailure, LinkErrorCode


@dataclass(frozen=True)
class Route:
    verb: str
    path: str
    service: int = 0


ROUTES: Mapping[str, Route] = {
    "sessions.list": Route("GET", "/api/session/list"),
    "session.start": Route("POST", "/session/start"),
    "chat.history": Route("GET", "/chat/history"),
    "chat.stream": Route("POST", "/chat/stream"),
    "identity": Route("GET", "/identity"),
    "vm.health": Route("GET", "/health"),
}

_ALLOWED_QUERY_BY_METHOD = {
    "chat.history": frozenset({"limit", "session_id"}),
    "chat.stream": frozenset({"noiseOnly", "streamMode"}),
}


def resolve_request(
    method: str,
    path_params: Mapping[str, object] | None = None,
    query: Mapping[str, object] | None = None,
) -> tuple[Route, str]:
    try:
        route = ROUTES[method]
    except (KeyError, TypeError):
        raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED) from None
    path = route.path
    for key, value in (path_params or {}).items():
        placeholder = "{" + str(key) + "}"
        if placeholder not in path or value is None:
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
        path = path.replace(placeholder, quote(str(value), safe=""))
    if "{" in path or "}" in path:
        raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
    allowed_query = _ALLOWED_QUERY_BY_METHOD.get(method, frozenset())
    values: list[tuple[str, str]] = []
    for key, value in (query or {}).items():
        if key not in allowed_query or value is None:
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
        if isinstance(value, (dict, list, tuple, set)):
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
        values.append((key, str(value)))
    if values:
        path += "?" + urlencode(values)
    return route, path
