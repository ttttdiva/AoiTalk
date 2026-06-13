import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import HTTPException, Request


def ecc_cookie_auth_dependency(app_instance: Any):
    def require_auth(request: Request) -> None:
        app_instance._enforce_cookie_auth(request)

    return require_auth


def parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid UUID: {value}") from exc


def parse_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date: {value}") from exc


def model_to_dict(obj: Any) -> dict:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return {key: value for key, value in vars(obj).items() if not key.startswith("_")}
