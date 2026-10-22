# Public gateway boundary

AoiTalk owns its application servers only:

- FastAPI on `AOITALK_WEB_PORT` (default `3000`)
- Next.js on `AOITALK_NEXT_PORT` (default `3002`)
- application authentication, WebSocket endpoints, files, and browser UI

The personal/shared public edge is maintained in `D:\Dev\85_CaddyGateway` and the private repository `ttttdiva/85_CaddyGateway`. That repository owns:

- the long-running shared Caddy process and port `6002`
- Cloudflare Tunnel lifecycle integration
- the technical site and search portal
- the Cloudflare Worker, search gate, and app-prefix routing
- public prefixes such as `/at/` for AoiTalk, and `/hydrus/`, `/hf/`, `/story/` for the
  independent apps that own those features (`57_Hydrus_App`, `60_huggingface-sync`,
  `86_Novel-Graph-Studio`); AoiTalk itself does not implement Hugging Face browsing/sync,
  Hydrus browsing, or Story/Novel/TRPG features, and only links out to those app prefixes
  from its Apps screen

## Personal Windows runtime

`run.bat` defaults to:

```text
AOITALK_SKIP_CADDY=true
AOITALK_PUBLIC_BASE_PATH=/at
NEXT_PUBLIC_BASE_PATH=/at
```

The shared gateway must already be running, or it can be started together with AoiTalk from `85_CaddyGateway\Start-All.bat`. Restarting or exiting AoiTalk must not stop the shared Caddy process.

Browser-visible AoiTalk paths are rooted at `/at/`. The public gateway removes that prefix only for FastAPI routes which require it, and preserves it for Next.js so generated links and static assets remain consistent.

## Browser-facing settings

The following values are deployment inputs rather than hard-coded site identity:

```text
AOITALK_PUBLIC_BASE_PATH=/at
AOITALK_BROWSER_TITLE=Example Portal
AOITALK_BROWSER_ICON=/images/ui/example.svg
AOITALK_BROWSER_BRANDING_ORIGINS=https://example.com
```

OAuth providers must be registered with the browser-visible callback path, for example:

```text
https://example.com/at/api/google-calendar/oauth/callback
https://example.com/at/api/webex/oauth/callback
```

The backend still receives the same application route after the shared gateway removes `/at` for those FastAPI handlers.

## Enterprise boundary

Enterprise/Docker packaging remains self-contained. It uses `caddy/Caddyfile.enterprise` and the existing enterprise compose/native lifecycle. The personal `caddy/Caddyfile`, technical site, Worker deployment, and Wiki publication pipeline are intentionally absent from this repository.
