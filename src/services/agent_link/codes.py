"""Closed error vocabulary for native agent links."""

from __future__ import annotations

from enum import StrEnum


class LinkErrorCode(StrEnum):
    MUSE_AUTH_EXPIRED = "muse_auth_expired"
    MUSE_VM_UNAVAILABLE = "muse_vm_unavailable"
    MUSE_HATCH_TOKEN_FAILED = "muse_hatch_token_failed"
    MUSE_CONNECT_FAILED = "muse_connect_failed"
    MUSE_HANDSHAKE_FAILED = "muse_handshake_failed"
    MUSE_HANDSHAKE_UNSUPPORTED = "muse_handshake_unsupported"
    MUSE_TRANSPORT_CORRUPT = "muse_transport_corrupt"
    MUSE_GATEWAY_STATUS = "muse_gateway_status"
    MUSE_SEND_UNCERTAIN = "muse_send_uncertain"
    MUSE_SEND_REJECTED = "muse_send_rejected"
    MUSE_REPLY_PENDING = "muse_reply_pending"
    MUSE_CONTRACT_CHANGED = "muse_contract_changed"
    MUSE_HTTP_FORBIDDEN = "muse_http_forbidden"

    GROK_PROFILE_UNAVAILABLE = "grok_profile_unavailable"
    GROK_DPAPI_FAILED = "grok_dpapi_failed"
    GROK_ACCOUNT_CHANGED = "grok_account_changed"
    GROK_AUTH_EXPIRED = "grok_auth_expired"
    GROK_GATEWAY_REJECTED = "grok_gateway_rejected"
    GROK_SEND_UNCERTAIN = "grok_send_uncertain"
    GROK_SEND_REJECTED = "grok_send_rejected"
    GROK_INSPECT_SERVER_ERROR = "grok_inspect_server_error"
    GROK_THREAD_SERVER_ERROR = "grok_thread_server_error"
    GROK_USAGE_LIMIT_EXCEEDED = "grok_usage_limit_exceeded"
    GROK_REPLY_PENDING = "grok_reply_pending"
    GROK_CONTRACT_CHANGED = "grok_contract_changed"

    LINK_JOURNAL_CONFLICT = "link_journal_conflict"
    LINK_WINDOW_EXPIRED = "link_window_expired"
    LINK_REPLY_UNDELIVERED_EXPIRED = "link_reply_undelivered_expired"
    LINK_CANCELLED_LOCAL = "link_cancelled_local"
    LINK_INTERNAL = "link_internal"


def normalize_code(value: object) -> LinkErrorCode:
    """Return a known fixed code, mapping every unknown value to link_internal."""

    if isinstance(value, LinkErrorCode):
        return value
    try:
        return LinkErrorCode(str(value))
    except (TypeError, ValueError):
        return LinkErrorCode.LINK_INTERNAL


class AgentLinkFailure(RuntimeError):
    """Exception whose message contains only a closed-vocabulary code."""

    def __init__(self, code: object = LinkErrorCode.LINK_INTERNAL) -> None:
        self.code = normalize_code(code)
        super().__init__(self.code.value)
