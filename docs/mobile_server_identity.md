# Native Mobile Server Identity

Native Mobile uses the hidden `GET /api/mobile/server-identity/challenge`
endpoint before attaching an application credential. The caller sends both a
fresh `nonce` and its complete target namespace in `target_namespace`, including
the scheme, authority, and service prefix such as `/at`.

The server returns protocol version 2 and signs the following domain-separated
payload with its durable Ed25519 key:

```text
AoiTalk-Mobile-Server-Identity/v2\0server_id\0nonce\0target_namespace
```

The response repeats the normalized `target_namespace`. Mobile verifies that
the response namespace exactly matches the target it is about to use, verifies
the public-key digest and signature, and then compares the key with the
operator-pinned primary identity. A verified alias may be added to the same
pin; a server identity cannot be selected from a URL or `server_id` alone.

## Approved aliases

Explicit public, LAN, or portal aliases are configured by the server owner in
`AOITALK_MOBILE_IDENTITY_ALIASES` as a JSON string array. For example:

```text
AOITALK_MOBILE_IDENTITY_ALIASES=["https://portal.example/at","https://aoi.example/at"]
```

The server also derives local HTTP(S) aliases from the ASGI bound server
address and port, loopback addresses, local NIC addresses, and the configured
ASGI `root_path`. It ignores `Host`, `X-Forwarded-Host`, and client supplied
alias headers. A requested namespace is signed only when it is in this
server-owned set. A portal or DDNS alias must therefore be declared by the
server owner (or by the authoritative Gateway deployment that owns that
public configuration); Mobile settings cannot add an alias to the server.

The public Gateway configuration remains owned by the adjacent Gateway
application. AoiTalk reads `config/services.json` from the adjacent
`85_CaddyGateway` by default, or the read-only path in
`AOITALK_GATEWAY_SERVICES_CONFIG_PATH`. It accepts the AoiTalk site origin,
declared transport origins, and an optional `config/profiles/ddns.json` public
host only when the `at` service is enabled and its declared `apiUpstream`
matches the actual AoiTalk ASGI server port and a local interface. The direct
`upstreamOrigin` is excluded as a mobile alias. A malformed config or an
upstream host/port mismatch disables only those Gateway-derived aliases and
keeps the explicit alias setting and local aliases available. AoiTalk never
writes Gateway configuration or treats a client request header as authority.

Protocol v1 identity proofs are unsupported. An older direct server may still
be used through the exact explicitly configured primary URL when the identity
route returns 404; this compatibility path never extends to LAN, portal, or
DDNS aliases and never accepts a v2 proof with an unapproved namespace.

The Ed25519 key remains in the existing durable identity storage and protocol
changes do not generate a new key. On Windows the identity file ACL is
restricted to the current account, Administrators, and SYSTEM before private
key bytes are written.
