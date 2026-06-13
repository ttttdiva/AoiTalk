"""Shared LLM provider capability metadata and protocol."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Generator, List, Optional, Protocol


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_stream: bool = True
    supports_tools: bool = False
    supports_response_format: bool = False
    supports_model_pull: bool = False
    supports_model_delete: bool = False
    supports_extra_body: bool = False

    def to_dict(self) -> Dict[str, bool]:
        return asdict(self)


class LLMProvider(Protocol):
    """Common surface expected from chat-capable LLM providers."""

    capabilities: ProviderCapabilities

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        ...

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        ...

    def list_models(self) -> List[Dict[str, Any]]:
        ...

    def health_check(self) -> Dict[str, Any]:
        ...

