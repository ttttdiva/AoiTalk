"""Backward-compatible import shim for the rebuilt task router."""

from .task_routes import create_task_router

__all__ = ["create_task_router"]
