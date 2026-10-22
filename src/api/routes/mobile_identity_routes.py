"""Hidden native-mobile server identity challenge endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ...services.mobile_server_identity import (
    MobileServerIdentityAliasNotApproved,
    MobileServerIdentityError,
    MobileServerIdentityService,
)

if TYPE_CHECKING:
    from ..server import WebChatServer


IDENTITY_PROBE_HEADER = "x-aoitalk-sdk-gateway"
IDENTITY_PROBE_HEADER_VALUE = "identity-probe-v1"


def register_mobile_identity_routes(app: FastAPI, server: "WebChatServer") -> None:
    """Register the unauthenticated nonce proof used before JWT transport."""

    service = MobileServerIdentityService(
        db_manager=getattr(server, "_db_manager", None),
    )
    setattr(server, "_mobile_server_identity_service", service)

    async def _identity_response(
        request: Request,
        nonce: str,
        target_namespace: str,
    ) -> JSONResponse:
        if request.headers.get("authorization"):
            # Identity discovery must happen before a bearer credential is
            # trusted. Reject accidental auth forwarding so a portal/gateway
            # cannot turn this probe into a credential-bearing request.
            raise HTTPException(
                status_code=400,
                detail="Authorization is not allowed for identity probe",
            )
        # The SDK gateway header is a routing marker, not a credential.  It is
        # accepted only in its bounded identity-probe form so a portal can
        # preserve the marker without forwarding an app JWT to this endpoint.
        supplied_marker = str(request.headers.get(IDENTITY_PROBE_HEADER, "") or "").strip()
        if supplied_marker and supplied_marker != IDENTITY_PROBE_HEADER_VALUE:
            raise HTTPException(status_code=400, detail="Invalid identity probe")
        try:
            proof = await service.challenge(
                nonce,
                target_namespace,
                request=request,
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid identity nonce") from None
        except MobileServerIdentityAliasNotApproved:
            return JSONResponse(
                {"detail": "Identity target namespace is not approved", "code": "identity_target_not_approved"},
                status_code=403,
            )
        except (MobileServerIdentityError, OSError):
            # Do not disclose storage paths, DB names, or key material.
            return JSONResponse(
                {"detail": "Server identity is unavailable", "code": "identity_unavailable"},
                status_code=503,
            )
        return JSONResponse(proof)

    @app.get("/api/mobile/server-identity/challenge", include_in_schema=False)
    async def mobile_server_identity_challenge(
        request: Request,
        nonce: str = Query(..., min_length=1, max_length=256),
        target_namespace: str = Query(..., min_length=1, max_length=2048),
    ) -> JSONResponse:
        return await _identity_response(request, nonce, target_namespace)


__all__ = [
    "IDENTITY_PROBE_HEADER",
    "IDENTITY_PROBE_HEADER_VALUE",
    "register_mobile_identity_routes",
]
