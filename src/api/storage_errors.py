"""Translate scoped filesystem failures without changing existing auth errors."""
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute

from ..services.storage_io import StorageError


class StorageErrorRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request: Request):
            try:
                return await original(request)
            except StorageError as error:
                raise HTTPException(status_code=error.status_code, detail=error.detail()) from error
        return handler
