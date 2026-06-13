"""OGP メタデータ取得ルート (server.py から移設)"""

import logging
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from ..router_helpers import cookie_auth_dependency

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def register_ogp_routes(app: FastAPI, server: "WebChatServer") -> None:
    """OGP メタデータ取得ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    # ── OGP Metadata API ───────────────────────────────────────────────

    @app.get("/api/ogp")
    async def ogp_fetch(url: str = Query(...), _: None = Depends(require_auth)):
        """Fetch OGP metadata from a URL"""
        import httpx
        import re
        from urllib.parse import urlencode, urlparse

        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise HTTPException(
                status_code=503, detail="beautifulsoup4 is not installed"
            )

        def is_x_status_url(value: str) -> bool:
            try:
                parsed = urlparse(value)
            except ValueError:
                return False
            host = parsed.netloc.lower().split("@")[-1].split(":")[0]
            if host not in {
                "x.com",
                "www.x.com",
                "twitter.com",
                "www.twitter.com",
                "mobile.twitter.com",
            }:
                return False
            return re.match(r"^/[^/]+/status(?:es)?/\d+", parsed.path) is not None

        try:
            async with httpx.AsyncClient(
                timeout=5.0, follow_redirects=True
            ) as client:
                if is_x_status_url(url):
                    try:
                        oembed_url = "https://publish.twitter.com/oembed?" + urlencode(
                            {"url": url, "omit_script": "true", "dnt": "true"}
                        )
                        oembed_resp = await client.get(
                            oembed_url,
                            headers={
                                "User-Agent": (
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/124.0 Safari/537.36"
                                )
                            },
                        )
                        oembed_resp.raise_for_status()
                        oembed = oembed_resp.json()
                        embed_html = oembed.get("html")
                        if embed_html:
                            author = oembed.get("author_name")
                            title = f"{author} on X" if author else "X post"
                            return JSONResponse(
                                {
                                    "success": True,
                                    "title": title,
                                    "description": None,
                                    "image": None,
                                    "url": url,
                                    "favicon": "https://abs.twimg.com/favicons/twitter.3.ico",
                                    "embed_type": "x-post",
                                    "embed_html": embed_html,
                                    "provider_name": oembed.get("provider_name")
                                    or "Twitter",
                                }
                            )
                    except Exception as e:
                        logger.warning(f"X oEmbed fetch failed for {url}: {e}")

                resp = await client.get(
                    url, headers={"User-Agent": "AoiTalk/1.0 OGP Fetcher"}
                )
                resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")

            def og(prop: str):
                tag = soup.find("meta", property=f"og:{prop}")
                return tag["content"] if tag and tag.get("content") else None

            title = og("title") or (soup.title.string if soup.title else None)
            description = og("description")
            if not description:
                meta_desc = soup.find("meta", attrs={"name": "description"})
                description = (
                    meta_desc["content"]
                    if meta_desc and meta_desc.get("content")
                    else None
                )
            image = og("image")

            # favicon
            favicon = None
            icon_link = soup.find(
                "link",
                rel=lambda v: v and "icon" in (v if isinstance(v, list) else [v]),
            )
            if icon_link and icon_link.get("href"):
                href = icon_link["href"]
                if href.startswith("//"):
                    favicon = "https:" + href
                elif href.startswith("/"):
                    from urllib.parse import urlparse

                    parsed = urlparse(url)
                    favicon = f"{parsed.scheme}://{parsed.netloc}{href}"
                elif href.startswith("http"):
                    favicon = href
                else:
                    from urllib.parse import urlparse

                    parsed = urlparse(url)
                    favicon = f"{parsed.scheme}://{parsed.netloc}/{href}"

            return JSONResponse(
                {
                    "success": True,
                    "title": title,
                    "description": description,
                    "image": image,
                    "url": url,
                    "favicon": favicon,
                }
            )
        except httpx.TimeoutException:
            return JSONResponse(
                {"success": False, "error": "タイムアウト", "url": url}
            )
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e), "url": url})
