"""Opt-in DOTS Events composition for the existing dedicated stdio process."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .aoitalk_dots_events import (
    DedicatedPrincipalResolver,
    DotsDialogueEventsExtension,
    DotsEventsCapabilityMiddleware,
)
from .dots_dialogue_dedicated import (
    DedicatedDotsConnectionConfig,
    load_dedicated_dots_connection_config,
)
from .dots_dialogue_http_backend import DedicatedDotsDialogueHttpBackend


@dataclass(frozen=True, slots=True)
class DedicatedStdioDotsComposition:
    server: Any
    extension: DotsDialogueEventsExtension
    backend: DedicatedDotsDialogueHttpBackend
    principal_resolver: DedicatedPrincipalResolver


def build_dedicated_stdio_dots_composition(
    connection_config: DedicatedDotsConnectionConfig,
) -> DedicatedStdioDotsComposition:
    """Build, but never start, the configured stdio server instance."""
    if not isinstance(connection_config, DedicatedDotsConnectionConfig):
        raise ValueError("dedicated DOTS connection config is required")
    backend = DedicatedDotsDialogueHttpBackend(connection_config=connection_config)
    resolver = DedicatedPrincipalResolver(connection_config)
    extension = DotsDialogueEventsExtension(
        backend=backend,
        principal_resolver=resolver,
        enabled=True,
        production_gate=True,
    )
    if extension.enabled is not True:
        raise RuntimeError("dedicated DOTS Events adapter unavailable")
    from .aoitalk_plugin_server import build_mcp_server

    server = build_mcp_server(
        extensions=(extension,),
        middleware=(DotsEventsCapabilityMiddleware(extension),),
    )
    return DedicatedStdioDotsComposition(server, extension, backend, resolver)


__all__ = ["DedicatedStdioDotsComposition", "build_dedicated_stdio_dots_composition"]
