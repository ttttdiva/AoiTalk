# AoiTalk browser handoff client contract

This document defines the provider side of the browser handoff from AoiTalk
41 to a separately deployed recipient service. It covers the current planned
recipient set: 41, 57, 60, 86, 73, 74, 77, and 82. 87 and 91 are excluded;
their avatar/runtime integration has a separate owner. No client is registered
by this source-only change.

## Trust boundary

Recipients are explicit operator registrations. `Host`, `Forwarded`, raw
gateway marker values, and caller supplied URLs never identify a client or
justify a local authentication bypass. The signed source gateway proof is a
required transport boundary for the browser authorization route. The provider issuer is
`AOITALK_AUTH_HANDOFF_ISSUER`; it must be one exact canonical HTTPS origin and
defaults to `https://az-vnetspace.com`.

`AOITALK_AUTH_HANDOFF_CLIENTS` is a JSON array whose entries are exactly:

```json
{
  "clientId": "service-id",
  "origin": "https://service.example",
  "callbackPath": "/auth/aoitalk/callback",
  "audience": "urn:aoitalk:handoff:service-id",
  "secretSha256": "<64 lowercase hexadecimal characters>"
}
```

The `origin` is an exact HTTPS origin with no wildcard, userinfo, path,
query, or fragment. The callback is an exact normalized path: it cannot contain
backslash, percent encoding, query/fragment characters, controls, or dot
segments. The audience is an exact service binding. It is either an explicit
`urn:` value or a canonical HTTPS URI (an HTTPS path is allowed); it is not
normalized by the provider. Client IDs, origin/callback pairs, and audiences
are unique. The fixed compatibility client `aoitalk-legacy` and its historical
origin/callback/audience cannot be overridden or aliased.

If the external registry is absent, the historical 41 browser flow remains
available. An invalid registry disables every external client and fails closed;
it never falls back to an external default. The legacy client cannot be used
for either S2S endpoint.

## Protocol

The recipient backend generates a 43-character random `state` and a PKCE
verifier, derives an S256 `code_challenge`, and calls:

```text
POST /at/api/auth/handoff/requests
Authorization: Basic <base64(clientId:secret)>
Content-Type: application/json
```

`<clientId:secret>` is encoded as standard Base64 for the Basic value. This
endpoint and the exchange endpoint are backend-to-backend calls over TLS;
they are not browser CORS APIs.

The JSON body is exactly the following five fields:

```json
{
  "redirect_uri": "https://service.example/auth/aoitalk/callback",
  "audience": "urn:aoitalk:handoff:service-id",
  "state": "<recipient state>",
  "code_challenge": "<S256 challenge>",
  "code_challenge_method": "S256"
}
```

The provider stores only a SHA-256 digest of the opaque request handle and the
recipient state. A successful response is a short-lived `authorization_url`
and `expires_in`. The recipient sends the browser to that URL. The provider
never starts a browser fetch or uses a recipient session cookie.

The browser request must arrive through the source gateway proof at the exact
path `/at/api/auth/handoff/client-authorize`. After the existing source proof
is verified, AoiTalk requires a current active user, the current
`session_version`, and no required password reset. The provider re-resolves
the registry and registration hash at authorization time. Rotation or removal
therefore invalidates an outstanding request. A successful authorization
redirects exactly to the registered callback with exactly `code` and the
recipient's original `state`.

The recipient backend exchanges the result:

```text
POST /at/api/auth/handoff/exchange
Authorization: Basic <base64(clientId:secret)>
Content-Type: application/json
```

The JSON body is exactly `{code,state,redirect_uri,audience,code_verifier}`.
The callback URI, audience, state, client registration, expiry, source user,
and S256 proof must all match. The row and source user are locked in one
transaction; expiry is checked again after lock waits. Logout/session-version
changes, deactivation, and password-reset requirements reject both
authorization and exchange. A failed attempt leaves the code available, while
the successful exchange atomically consumes it.

The successful response is TLS JSON containing only:

```json
{
  "iss": "https://az-vnetspace.com",
  "sub": "<stable AoiTalk user id>",
  "aud": "urn:aoitalk:handoff:service-id",
  "iat": 0,
  "exp": 0
}
```

`exp - iat` is at most 60 seconds and never exceeds the source session's
remaining lifetime.

The provider does not issue an AoiTalk JWT, role/admin claim, recipient token,
or shared signing key. The recipient creates its own local session, stores its
own cookie, and applies its own authorization policy. The provider response is
not a `forward_auth` proof by itself; a recipient backend session-check route
must perform that policy decision before Caddy forwards a request. An identity
assertion never grants an owner/admin role automatically. Same-machine local
input decisions and remote Caddy handling belong to the gateway/recipient
client; this provider has no Host-based exemption.

Request bodies are bounded to 8 KiB, require JSON, reject duplicate or unknown
fields, and produce empty no-store failures. Missing/invalid Basic auth uses a
shared anonymous fixed-window rate-limit bucket; authenticated clients use a
per-client bucket. Rate limits are persistent and fail closed before a request
row is created or consumed. The current fixed window is 60 seconds with 30
request creations per client, 60 exchanges per client, 60 browser
authorizations per verified source user, and 20 anonymous attempts for each
endpoint. The browser authorization bucket is keyed by the verified AoiTalk
user ID; it never uses a client supplied header.

Before activation, an operator must review each exact origin, callback,
audience, recipient-side state/PKCE handling, and secret delivery mechanism.
The recipient should suppress access logs and referrers for callback URLs that
carry `code` and `state`. This source change intentionally performs no
registration, secret generation, deployment, Caddy change, or
cross-application session mapping. Deployment must additionally keep the Next
listener loopback-bound or firewall-restricted behind an HTTPS ingress; this
source-only task does not verify or activate that deployment gate.
