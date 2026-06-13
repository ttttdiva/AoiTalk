"""外部クローラーの状態取得・制御ルート (server.py から移設)"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..router_helpers import cookie_auth_dependency
from .payloads import CrawlerStatusReport

# Import CrawlerStatusChecker (server.py と同じフォールバック付き)
try:
    from ...crawler_status import CrawlerStatusChecker
except ImportError:
    CrawlerStatusChecker = None

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def register_crawler_routes(app: FastAPI, server: "WebChatServer") -> None:
    """クローラー status / report / restart / stop ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    @app.get("/api/crawler/status")
    async def get_crawler_status(_: None = Depends(require_auth)):
        """Get status of external crawlers (DiscordCrawler, EventMonitor, VideoCrawler, HydrusClient)"""
        if CrawlerStatusChecker is None:
            raise HTTPException(
                status_code=503, detail="Crawler status checking is not available"
            )

        try:
            checker = CrawlerStatusChecker()
            status_data = {}

            # Check all crawlers: health check only (Pull), details from Push cache
            for crawler_name in [
                "DiscordCrawler",
                "EventMonitor",
                "HydrusClient",
                "VideoCrawler",
            ]:
                # VideoCrawler and HydrusClient are special: need detailed Pull status
                # VideoCrawler: external service, need HTTP call anyway
                # HydrusClient: doesn't send Push updates
                if crawler_name == "VideoCrawler":
                    detailed_status = (
                        await checker.get_video_crawler_detailed_status()
                    )
                    status_data[crawler_name] = detailed_status
                elif crawler_name == "HydrusClient":
                    detailed_status = (
                        await checker.get_hydrus_client_detailed_status()
                    )
                    status_data[crawler_name] = detailed_status
                else:
                    is_alive = await checker.check_alive(crawler_name)

                    if is_alive and crawler_name in server._crawler_status_cache:
                        # Alive + Push cache available → use detailed status
                        status_data[crawler_name] = server._crawler_status_cache[
                            crawler_name
                        ].copy()
                        status_data[crawler_name]["is_alive"] = True
                    elif is_alive:
                        # Alive but no Push data → just indicate running
                        status_data[crawler_name] = {
                            "status": "running",
                            "details": None,
                            "is_alive": True,
                        }
                    else:
                        # Dead/stopped
                        status_data[crawler_name] = {
                            "status": "stopped",
                            "is_alive": False,
                        }

            # Convert dict to array format expected by frontend and add crawler names
            crawlers_array = []
            for crawler_name, crawler_data in status_data.items():
                crawler_entry = crawler_data.copy()
                crawler_entry["name"] = crawler_name
                # Ensure 'type' field exists for frontend badge display
                if "type" not in crawler_entry:
                    crawler_entry["type"] = (
                        "cloud" if crawler_name == "VideoCrawler" else "local"
                    )
                crawlers_array.append(crawler_entry)

            return JSONResponse(
                {
                    "crawlers": crawlers_array,
                    "timestamp": datetime.now().isoformat(),
                }
            )
        except Exception as e:
            logger.error(f"Failed to get crawler status: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to get crawler status: {e}"
            )

    @app.post("/api/crawler/report")
    async def receive_crawler_status(report: CrawlerStatusReport, request: Request):
        """Receive status push from crawlers"""
        if not server._verify_api_key(request):
            raise HTTPException(status_code=401, detail="Invalid API key")

        # Build details from explicit details field and any extra fields
        # Crawlers may send fields like processed_servers, processed_channels
        # as top-level fields rather than inside 'details'
        details = report.details.copy() if report.details else {}

        # Merge extra fields from Pydantic model (extra='allow' captures these)
        if hasattr(report, "model_extra") and report.model_extra:
            details.update(report.model_extra)
        elif hasattr(report, "__pydantic_extra__") and report.__pydantic_extra__:
            details.update(report.__pydantic_extra__)

        # Store status in cache
        server._crawler_status_cache[report.name] = {
            "status": report.status,
            "details": details if details else None,
            "error": report.error,
            "received_at": datetime.now().isoformat(),
        }

        logger.info(
            f"Received crawler status push: {report.name} - {report.status} (details: {bool(details)})"
        )

        # Broadcast to WebSocket clients
        await server.manager.broadcast(
            {
                "type": "crawler_status_update",
                "data": {
                    "name": report.name,
                    "status": report.status,
                    "details": details if details else None,
                    "error": report.error,
                },
            }
        )

        return JSONResponse({"accepted": True})

    @app.post("/api/crawler/restart/{crawler_name}")
    async def restart_crawler(crawler_name: str, _: None = Depends(require_auth)):
        """Restart a crawler"""
        if CrawlerStatusChecker is None:
            raise HTTPException(
                status_code=503, detail="Crawler control is not available"
            )

        try:
            checker = CrawlerStatusChecker()

            # Route to appropriate restart method
            if crawler_name.lower() == "videocrawler":
                result = await checker.restart_video_crawler()
            elif crawler_name.lower() == "discordcrawler":
                result = await checker.restart_discord_crawler()
            elif crawler_name.lower() == "eventmonitor":
                result = await checker.restart_event_monitor()
            elif crawler_name.lower() == "hydrusclient":
                result = await checker.launch_hydrus_client()
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Restart not supported for {crawler_name}",
                )

            return JSONResponse(result)
        except Exception as e:
            logger.error(f"Failed to restart crawler: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to restart crawler: {e}"
            )

    @app.post("/api/crawler/stop/{crawler_name}")
    async def stop_crawler(crawler_name: str, _: None = Depends(require_auth)):
        """Stop a running crawler"""
        if CrawlerStatusChecker is None:
            raise HTTPException(
                status_code=503, detail="Crawler control is not available"
            )

        try:
            checker = CrawlerStatusChecker()

            # Route to appropriate stop method
            if crawler_name.lower() == "discordcrawler":
                result = await checker.stop_discord_crawler()
            elif crawler_name.lower() == "eventmonitor":
                result = await checker.stop_event_monitor()
            elif crawler_name.lower() == "hydrusclient":
                result = await checker.stop_hydrus_client()
            else:
                raise HTTPException(
                    status_code=400, detail=f"Stop not supported for {crawler_name}"
                )

            return JSONResponse(result)
        except Exception as e:
            logger.error(f"Failed to stop crawler: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to stop crawler: {e}"
            )
