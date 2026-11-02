"""Native, in-process Muse agent link client with lazy optional-layer imports."""

from importlib import import_module

_EXPORTS = {
    "AuthClient": (".auth", "AuthClient"),
    "AuthSession": (".auth", "AuthSession"),
    "CookieJar": (".auth", "CookieJar"),
    "HttpxTransport": (".auth", "HttpxTransport"),
    "HttpResponse": (".auth", "HttpResponse"),
    "HttpTransport": (".auth", "HttpTransport"),
    "GatewayClient": (".gateway", "GatewayClient"),
    "GatewayFailure": (".gateway", "GatewayFailure"),
    "MuseLink": (".link", "MuseLink"),
    "load_or_create_node_id": (".link", "load_or_create_node_id"),
    "CipherState": (".noise_xx", "CipherState"),
    "NoiseXXInitiator": (".noise_xx", "NoiseXXInitiator"),
    "NoiseXXResponder": (".noise_xx", "NoiseXXResponder"),
}

__all__ = [
    "AuthClient",
    "AuthSession",
    "CipherState",
    "CookieJar",
    "HttpxTransport",
    "GatewayClient",
    "GatewayFailure",
    "HttpResponse",
    "HttpTransport",
    "MuseLink",
    "NoiseXXInitiator",
    "NoiseXXResponder",
    "load_or_create_node_id",
]


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(name) from None
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
