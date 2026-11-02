"""Native, in-process Grok Bot AgentLink client."""

from .client import GrokGatewayClient
from .dpapi import DPAPIUnprotector, WindowsDPAPIUnprotector
from .link import GrokDiagnostic, GrokLink
from .profile import PROFILE_FILES, ProfileReader, WindowsProfileReader
from .route import (
    ENSURE_SANDBOX_URL,
    MAX_RESPONSE_BYTES,
    REQUEST_TIMEOUT_S,
    GatewayRoute,
    GrokLinkFailure,
    GrokRouteProvider,
    HttpResponse,
    HttpTransport,
    HttpxTransport,
    route_from_ensure_response,
    validate_gateway_url,
)
from .secrets import (
    AccountPin,
    GrokCredentials,
    assert_account_pin,
    decrypt_v10_secret,
    read_credentials,
)

__all__ = [
    "AccountPin",
    "DPAPIUnprotector",
    "ENSURE_SANDBOX_URL",
    "GatewayRoute",
    "GrokCredentials",
    "GrokDiagnostic",
    "GrokGatewayClient",
    "GrokLink",
    "GrokLinkFailure",
    "GrokRouteProvider",
    "HttpResponse",
    "HttpTransport",
    "HttpxTransport",
    "MAX_RESPONSE_BYTES",
    "PROFILE_FILES",
    "ProfileReader",
    "REQUEST_TIMEOUT_S",
    "WindowsDPAPIUnprotector",
    "WindowsProfileReader",
    "assert_account_pin",
    "decrypt_v10_secret",
    "read_credentials",
    "route_from_ensure_response",
    "validate_gateway_url",
]
