# Bundled Caddy boundary

This directory is retained for the self-contained Enterprise/Docker distribution only.

- `Caddyfile.enterprise` is the Enterprise gateway configuration used by the existing compose/native packaging flow.
- `LICENSE` and `CHANGELOG.md` accompany the bundled Caddy binary.
- The personal/shared Caddy process, Cloudflare Tunnel integration, technical site, search portal, and Worker are maintained in `<external-caddy-gateway-root>` (`ttttdiva/85_CaddyGateway`).

A normal Windows personal start uses `AOITALK_SKIP_CADDY=true`; restarting AoiTalk must not manage or stop the shared gateway.
