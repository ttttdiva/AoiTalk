"""Project color helpers shared by task serializers."""

from __future__ import annotations

from typing import Any, Optional


def extract_project_color(metadata: Any) -> Optional[str]:
    if not isinstance(metadata, dict):
        return None
    color = metadata.get("color")
    return color if isinstance(color, str) and color.strip() else None
