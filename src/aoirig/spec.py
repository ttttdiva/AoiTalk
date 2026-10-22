"""Pydantic record base for owner-scoped rig and production views."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
