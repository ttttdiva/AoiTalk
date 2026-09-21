# MediaOps platform capability matrix

**Research date:** 2026-09-01 (JST)
**Authority:** official provider documentation and help pages linked below. Provider
capabilities, scopes, quotas, review requirements, and account eligibility can
change; this document is a dated design input, not a runtime credential test.

## Status vocabulary

- **IMPLEMENTED — RUNTIME NOT VERIFIED:** AoiTalk has the typed payload, immutable
  revision and manual-operation contract, but no authorized provider sandbox or
  credential was exercised.
- **MANUAL FALLBACK VERIFIED:** the AoiTalk contract can produce an exact,
  copy/download-ready package and record a human attempt/Receipt; this is not a
  provider API success.
- **UNKNOWN:** the provider surface was not confirmed by an official source and
  must not be advertised as available.

The current repository intentionally reports **no real posting provider adapter**
for these six platforms. `OperationsService.get_media_adapter_status` is
explicit that posting provider calls are disabled. Raw password, token, cookie,
browser profile, and API-key material is never stored in ordinary MediaOps rows
or returned in projections. WS02's dedicated credential vault stores only
AES-GCM ciphertext in `media_platform_credentials`; safe DTOs and audit
snapshots exclude the ciphertext, key material and secret payload.

## Matrix

| Platform | Current official facts (dated sources) | AoiTalk typed/manual contract | Real API status / unresolved blocker |
|---|---|---|---|
| **X** | Posts/timelines, post create/edit/delete and metrics are documented. OAuth 2.0 authorization-code (PKCE) and OAuth 1.0a are documented; scopes include `tweet.read`, `tweet.write`, `users.read`, `media.write`, and `offline.access`. Standard posting does not provide a durable draft/schedule surface; X Ads creatives expose draft/scheduled ad workflows. Rate limits are endpoint/app-plan dependent. [Posts](https://docs.x.com/x-api/posts/manage-tweets/introduction), [edit](https://docs.x.com/x-api/fundamentals/edit-posts), [metrics](https://docs.x.com/x-api/fundamentals/metrics), [OAuth](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code), [rate limits](https://docs.x.com/x-api/fundamentals/rate-limits), [Ads creatives](https://docs.x.com/x-ads-api/creatives) | `x_post` and `x_thread` payloads validate text, media/alt text, links, hashtags, sensitivity, ordering and schedule metadata. Manual composer/package is the supported path. | **IMPLEMENTED — RUNTIME NOT VERIFIED.** Requires approved X developer project, scopes, quota and an isolated private/draft-safe target. |
| **pixiv** | Official help documents posting, scheduled posting (Premium), drafts, editing and deletion. A public creator API for these operations was **not confirmed** in the dated research. FANBOX is a separate service and is not treated as pixiv. [post](https://www.pixiv.help/hc/ja/articles/235584228-pixiv%E3%81%AB%E5%B0%8F%E8%AA%AC%E3%82%92%E6%8A%95%E7%A8%BF%E3%81%99%E3%82%8B%E6%96%B9%E6%B3%95%E3%82%92%E7%9F%A5%E3%82%8A%E3%81%9F%E3%81%84), [schedule](https://www.pixiv.help/hc/ja/articles/115003430413-%E6%8A%95%E7%A8%BF%E6%97%A5%E6%99%82%E3%82%92%E6%8C%87%E5%AE%9A%E3%81%97%E3%81%A6%E4%BD%9C%E5%93%81%E3%82%92%E6%8A%95%E7%A8%BF%E3%81%97%E3%81%9F%E3%81%84), [drafts](https://www.pixiv.help/hc/ja/articles/900006162003-%E4%B8%8B%E6%9B%B8%E3%81%8D%E3%81%AB%E3%81%A4%E3%81%84%E3%81%A6), [edit](https://www.pixiv.help/hc/ja/articles/235645887-pixiv%E3%81%AB%E6%8A%95%E7%A8%BF%E3%81%97%E3%81%9F%E4%BD%9C%E5%93%81%E3%81%AE%E6%83%85%E5%A0%B1%E3%82%92%E5%A4%89%E3%81%88%E3%81%9F%E3%81%84), [delete](https://www.pixiv.help/hc/ja/articles/235584468-pixiv%E3%81%AB%E6%8A%95%E7%A8%BF%E3%81%97%E3%81%9F%E4%BD%9C%E5%93%81%E3%82%92%E5%89%8A%E9%99%A4%E3%81%97%E3%81%9F%E3%81%84) | `pixiv_work` validates title, caption, tags, AI/rating/R-18 policy and media references. Manual Dashboard handoff is supported. | **MANUAL FALLBACK VERIFIED; API UNKNOWN.** Do not infer a private/undocumented API or automate browser login. |
| **DLsite** | DLsite is a storefront/product and release-review workflow, not a social feed. Creator registration, review and release/package guidance are documented; a public creator publishing API was not confirmed. [creator registration](https://cs-circle.dlsite.com/hc/ja/articles/1500003095021-%E3%83%9E%E3%83%B3%E3%82%AC%E7%99%BB%E9%8C%B2%E3%82%AC%E3%82%A4%E3%83%89), [DLsite creator information](https://info.eisys.co.jp/dlsite) | `dlsite_release` is a product/release package with title, description, category, age rating, price/sales configuration, preview/thumbnail refs, deliverable package ref and rights checklist. It is never auto-released as a QA side effect. | **MANUAL FALLBACK VERIFIED; API UNKNOWN.** Requires Circle account, package review and human release confirmation. |
| **Patreon** | API v2 documents campaigns, members, posts, tiers and entitlements. Official help documents scheduled posts, editing, paid tiers, access settings and Post Insights; post creation/update scheduling through the API was not confirmed in this research. [API](https://docs.patreon.com/), [scheduled posts](https://support.patreon.com/hc/en-us/articles/360031956632-Scheduled-posts), [editing](https://support.patreon.com/hc/en-gb/articles/204606075-Editing-your-published-posts), [tiers](https://support.patreon.com/hc/en-us/articles/203913559-How-to-set-up-paid-tiers-and-benefits), [insights](https://support.patreon.com/hc/en-us/articles/360042841711-Post-Insights) | `patreon_post` validates public/paid/tier audience, title/body, preview, attachments, tier refs and schedule metadata. Manual Creator Studio handoff is supported. | **IMPLEMENTED — RUNTIME NOT VERIFIED.** Requires OAuth app approval/scopes and isolated creator test target; documented rate limits must be observed. |
| **YouTube** | Data API `videos.insert/update/delete` and `status.publishAt` scheduling are documented; Analytics exposes metrics. OAuth scopes and upload-audit/quota requirements apply. Shorts classification in this matrix is an AoiTalk content-policy inference, not a separate API resource. [videos](https://developers.google.com/youtube/v3/docs/videos), [insert](https://developers.google.com/youtube/v3/docs/videos/insert), [update](https://developers.google.com/youtube/v3/docs/videos/update), [delete](https://developers.google.com/youtube/v3/docs/videos/delete), [Analytics metrics](https://developers.google.com/youtube/analytics/metrics), [OAuth scopes](https://developers.google.com/identity/protocols/oauth2/scopes), [Shorts policy](https://support.google.com/youtube/answer/15424877?hl=ja) | `youtube_video`/`youtube_short` validate title, description, tags, media, thumbnail/captions, visibility, schedule and audience/disclosure fields. Manual Studio package remains available. | **IMPLEMENTED — RUNTIME NOT VERIFIED.** Requires Google OAuth consent, upload-audited project/quota and unlisted/private test video; no public upload was made. |
| **Instagram** | Meta’s official collection documents Professional-account media containers and `/media_publish` for feed image/video, carousel and Reel, plus Insights. Story support, durable native drafts/scheduling, update/delete semantics and Creator eligibility were not all confirmed for the selected login path. [Instagram API](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api), [Facebook Login collection](https://www.postman.com/meta/instagram/folder/u4g5a2a/instagram-api-with-facebook-login) | `instagram_feed`, `instagram_carousel` and `instagram_reel` validate caption, ordered media, accessibility text, cover and schedule metadata. No fake Story tool is exposed. | **IMPLEMENTED — RUNTIME NOT VERIFIED.** Requires Professional account, Meta app review/login scopes and an isolated test account; documented rolling publish limits apply. |

## Interpretation and next verification

“Supported” above means a dated official fact or an AoiTalk typed/manual
contract—not that this checkout has valid credentials. Before enabling any real
adapter, add a provider-specific adapter contract test, a safe private/unlisted
or draft target, rate-limit handling, remote postcondition verification and a
Receipt/reconciliation record. Until then the UI and Agent must show the manual
fallback status and never claim external success.
