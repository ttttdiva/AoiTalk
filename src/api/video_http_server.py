#!/usr/bin/env python3
"""
HTTP Video Server for Android compatibility

Serves video files over HTTP (without SSL) on a separate port.
This allows Android devices to play videos without certificate issues.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

logger = logging.getLogger(__name__)

# Import media browser functions
try:
    from ..tools.media_browser import (
        get_file_path,
        get_media_mime_type,
    )
    MEDIA_BROWSER_AVAILABLE = True
except ImportError:
    MEDIA_BROWSER_AVAILABLE = False
    get_file_path = None
    get_media_mime_type = None


def create_video_http_app() -> FastAPI:
    """Create a minimal FastAPI app for serving videos over HTTP"""
    app = FastAPI(title="AoiTalk Video Server (HTTP)")
    
    # Add CORS middleware with permissive settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,  # No credentials for HTTP
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
    )
    
    @app.get("/api/media/file")
    async def serve_video_file(path: str, request: Request):
        """Serve a video file with Range request support (no authentication)"""
        if not MEDIA_BROWSER_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Media browser is not available"
            )
        
        file_path = get_file_path(path)
        if file_path is None:
            raise HTTPException(status_code=404, detail="File not found")
        
        mime_type = get_media_mime_type(file_path)
        
        # Only serve video files on this endpoint
        if not mime_type.startswith('video/'):
            raise HTTPException(
                status_code=403,
                detail="Only video files are allowed on this endpoint"
            )
        
        file_size = file_path.stat().st_size
        range_header = request.headers.get('range')
        
        # Handle non-Range requests
        if not range_header:
            return FileResponse(
                path=str(file_path),
                media_type=mime_type,
                headers={"Accept-Ranges": "bytes"}
            )
        
        # Parse Range header (e.g., "bytes=0-1024" or "bytes=0-")
        try:
            range_str = range_header.replace("bytes=", "")
            range_parts = range_str.split("-")
            start = int(range_parts[0]) if range_parts[0] else 0
            end = int(range_parts[1]) if range_parts[1] else file_size - 1
        except (ValueError, IndexError):
            return FileResponse(
                path=str(file_path),
                media_type=mime_type,
                headers={"Accept-Ranges": "bytes"}
            )
        
        # Ensure valid range
        if start >= file_size:
            raise HTTPException(status_code=416, detail="Range not satisfiable")
        end = min(end, file_size - 1)
        content_length = end - start + 1
        
        # Generator function to stream file chunks
        def file_iterator():
            chunk_size = 1024 * 1024  # 1MB chunks
            with open(file_path, 'rb') as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    read_size = min(chunk_size, remaining)
                    data = f.read(read_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data
        
        # RFC 5987 encoded filename for Content-Disposition
        from urllib.parse import quote
        encoded_filename = quote(file_path.name, safe='')
        
        return StreamingResponse(
            file_iterator(),
            status_code=206,
            media_type=mime_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
                "Content-Disposition": f"inline; filename*=UTF-8''{encoded_filename}"
            }
        )
    
    @app.get("/health")
    async def health_check():
        """Health check endpoint"""
        return {"status": "ok", "service": "video-http"}
    
    return app


async def run_video_http_server(host: str, port: int):
    """Run the HTTP video server"""
    import uvicorn
    
    app = create_video_http_app()
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",  # Quieter logging
        access_log=False,
    )
    server = uvicorn.Server(config)
    
    logger.info(f"Starting HTTP video server on http://{host}:{port}")
    await server.serve()
