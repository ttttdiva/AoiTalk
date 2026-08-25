# Enterprise secret boundary

Enterprise secrets are read only by the root PID 1 entrypoint from mode `0600`
files (or an approved Docker secret source), materialized into the child process
environment, and their corresponding `*_FILE` variables are unset before
`gosu aoitalk` drops to UID/GID 1000. Secret files are never relaxed to 0644,
placed in Compose YAML/.env as plaintext, or copied into an image/release
archive. `HOME=/home/aoitalk`, `USER=aoitalk`, and `LOGNAME=aoitalk` must be set
before privilege drop so asyncpg and other clients do not search `/root`.

## One schema

The canonical list is `src/security/secret_env.py`. Enterprise entrypoint,
Compose, `deploy-compose.sh` generation/validation, provider API keys, and tests
must be generated from or checked against that list. At minimum the provider
contract covers OpenAI, OpenRouter, Gemini, Kimi/Moonshot, DeepInfra, DeepSeek,
XAI, and `openai_compatible_local`; a provider not supported by the selected
Enterprise backend must not appear in the UI.

Typical required deployment secrets are:

```text
POSTGRES_PASSWORD
NEXTAUTH_SECRET
AOITALK_WEB_AUTH_SECRET
AOITALK_JWT_SECRET
INTERNAL_API_KEY
AOITALK_CADDY_GATE_KEY
AOITALK_BOOTSTRAP_ADMIN_PASSWORD
```

Optional provider/API, field-crypto, and model-registry tokens use the same
secret boundary. Do not paste values into tickets, `docker compose config`,
`docker inspect`, logs, test failures, or diagnostics. Diagnostics may report
only existence, owner, mode, materialization state, and child `*_FILE` absence.

## Runtime acceptance

Use a real container, not source-string assertions, to prove:

* root:root secret files with mode 0600;
* entrypoint materializes values and drops to UID/GID 1000;
* `HOME=/home/aoitalk`, `USER=aoitalk`, `LOGNAME=aoitalk`;
* no `*_FILE` remains in child processes;
* UID 1000 cannot directly read the root-only file;
* Python import and a real asyncpg/PostgreSQL connection succeed;
* no secret appears on stdout/stderr, in pytest failure text, or static inspect
  environment fields.

A mismatch or unavailable secret is a preflight failure. Never solve it by
making files world-readable or by passing the secret as a command argument.

## Handoff archive boundary

The canonical handoff ZIP is checksum-covered but does not require a signing key. The
builder and updater reject `.git`, `.env`, model weights, private keys/certificates,
tokens, data, and reparse paths. Transfer the ZIP through an approved internal
channel; verify `SHA256SUMS` and `bundle-manifest.json` before activation. HF_TOKEN
is supplied only on the target PC for the pinned HTTPS model download, never as an
argv value, log field, ZIP member, or Docker build argument.
