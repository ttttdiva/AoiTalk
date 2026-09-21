"""One-way privacy materialization boundary for the ``/masking`` operation.

``OutboundPrivacyGateway`` is primarily an egress gate.  Its normal lifecycle
keeps a request-local alias table so aliases can be restored for local tool
execution or a final assistant response.  Manual masking has intentionally
different semantics: it creates a durable user-facing projection, so aliases
must remain in the result and the source must never be sent to a provider.

This module is the small policy boundary used by :mod:`masking_service` and
file transformation code.  It creates one fresh gateway for one invocation,
forces the existing deterministic + trusted-local semantic path, and exposes
only the resulting masked projection.  No alias map is persisted or returned
as metadata.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from .outbound_privacy_service import (
    OutboundPrivacyGateway,
    PrivacyError,
    PrivacyResult,
    RedactionFinding,
)


class PrivacyMaskingError(PrivacyError):
    """Base failure for an unsafe or incomplete masking projection."""


@dataclass(frozen=True)
class MaskedProjection:
    """Safe one-way projection returned for one text/cell value.

    ``raw`` is deliberately absent.  The gateway's in-memory alias map dies
    with the boundary instance and is never part of a persistence callback.
    """

    value: str = field(repr=False)
    findings: tuple[RedactionFinding, ...] = ()
    semantic_status: str = "disabled"

    @property
    def masked_text(self) -> str:
        return self.value


class PrivacyMaskingBoundary:
    """Fresh, one-way masking boundary scoped to one invocation.

    Construct this object once per ``/masking`` invocation and reuse it for all
    text and file cells in that invocation.  Reuse is intentional: equal raw
    values receive the same placeholder across the text and every attachment,
    while a subsequent invocation gets a new alias scope.
    """

    def __init__(
        self,
        config: Any | None = None,
        *,
        semantic_redactor: Callable[..., Any] | None = None,
        gateway_factory: Callable[..., OutboundPrivacyGateway] | None = None,
    ) -> None:
        self.config = config
        self._gateway_factory = gateway_factory or OutboundPrivacyGateway
        try:
            # Explicit empty policy scopes prevent a previous request's
            # contextvars from downgrading or otherwise retargeting this
            # invocation.  The gateway's materialize_one_way method forces the
            # protected transformation branch independently of configured mode.
            self.gateway = self._gateway_factory(
                config,
                semantic_redactor=semantic_redactor,
                session_context={},
                project_metadata={},
            )
        except TypeError:
            # Small embedding/test factories often accept only ``config`` and
            # the sidecar callback.  Do not broaden this fallback to arbitrary
            # exceptions: constructor failures remain fail-closed.
            self.gateway = self._gateway_factory(
                config,
                semantic_redactor=semantic_redactor,
            )

    async def materialize(
        self,
        value: Any,
        *,
        source_kind: str = "masking",
        semantic_exempt_values: Any | None = None,
    ) -> PrivacyResult:
        """Return a permanently masked value; never restore aliases."""

        try:
            kwargs = {"source_kind": source_kind}
            if semantic_exempt_values is not None:
                kwargs["semantic_exempt_values"] = semantic_exempt_values
            result = self.gateway.materialize_one_way(value, **kwargs)
        except AttributeError as exc:
            # A custom gateway factory must implement the one-way boundary;
            # silently falling back to ``protect`` would reintroduce reversible
            # semantics and could leak raw content.
            raise PrivacyMaskingError(
                "privacy masking gateway lacks one-way materialization"
            ) from exc
        except PrivacyMaskingError:
            raise
        except PrivacyError as exc:
            # Hide provider/sidecar details (which may contain sensitive
            # endpoint or callback text) behind the stable local error type.
            raise PrivacyMaskingError("privacy masking materialization failed") from exc
        if inspect.isawaitable(result):
            try:
                result = await result
            except PrivacyMaskingError:
                raise
            except PrivacyError as exc:
                raise PrivacyMaskingError("privacy masking materialization failed") from exc
        if not isinstance(result, PrivacyResult):
            # Keep an intentionally narrow compatibility seam for test or
            # embedding gateways that return an object with the canonical
            # fields, but never coerce an arbitrary value into a successful
            # projection.
            has_projection = (
                ("payload" in result or "final_payload" in result)
                if isinstance(result, Mapping)
                else hasattr(result, "payload") or hasattr(result, "final_payload")
            )
            if not has_projection:
                raise PrivacyMaskingError("privacy masking gateway returned no projection")
        def _field(name: str, default: Any = None) -> Any:
            if isinstance(result, Mapping):
                return result.get(name, default)
            return getattr(result, name, default)

        semantic_status = str(
            _field("semantic_status", "disabled") or "disabled"
        ).strip().casefold()
        if semantic_status in {"failed", "error", "unavailable"}:
            raise PrivacyMaskingError("semantic privacy masking failed")
        return result

    async def mask_text(
        self,
        text: str,
        *,
        source_kind: str = "masking_text",
    ) -> MaskedProjection:
        if not isinstance(text, str):
            raise PrivacyMaskingError("masking text must be a string")
        result = await self.materialize(text, source_kind=source_kind)
        masked = (
            result.get("final_payload")
            if isinstance(result, Mapping)
            else getattr(result, "final_payload", None)
        )
        if masked is None:
            masked = (
                result.get("payload")
                if isinstance(result, Mapping)
                else getattr(result, "payload", None)
            )
        if not isinstance(masked, str):
            raise PrivacyMaskingError("privacy masking produced no text projection")
        findings = (
            result.get("findings", ())
            if isinstance(result, Mapping)
            else getattr(result, "findings", ())
        )
        semantic_value = (
            result.get("semantic_status", "disabled")
            if isinstance(result, Mapping)
            else getattr(result, "semantic_status", "disabled")
        )
        return MaskedProjection(
            value=masked,
            findings=tuple(findings or ()),
            semantic_status=str(semantic_value or "disabled").strip().casefold(),
        )

    def materialize_sync(
        self,
        value: Any,
        *,
        source_kind: str = "masking",
        semantic_exempt_values: Any | None = None,
    ) -> PrivacyResult:
        """Synchronous counterpart used by the synchronous file transformer."""

        method = getattr(self.gateway, "materialize_one_way_sync", None)
        if not callable(method):
            raise PrivacyMaskingError("privacy masking gateway lacks sync materialization")
        try:
            kwargs = {"source_kind": source_kind}
            if semantic_exempt_values is not None:
                kwargs["semantic_exempt_values"] = semantic_exempt_values
            result = method(value, **kwargs)
        except PrivacyMaskingError:
            raise
        except PrivacyError as exc:
            raise PrivacyMaskingError("privacy masking materialization failed") from exc
        if not isinstance(result, PrivacyResult):
            has_projection = (
                ("payload" in result or "final_payload" in result)
                if isinstance(result, Mapping)
                else hasattr(result, "payload") or hasattr(result, "final_payload")
            )
            if not has_projection:
                raise PrivacyMaskingError("privacy masking gateway returned no projection")
        semantic_value = (
            result.get("semantic_status", "disabled")
            if isinstance(result, Mapping)
            else getattr(result, "semantic_status", "disabled")
        )
        if str(semantic_value or "disabled").strip().casefold() in {
            "failed",
            "error",
            "unavailable",
        }:
            raise PrivacyMaskingError("semantic privacy masking failed")
        return result

    def mask_text_sync(
        self,
        text: str,
        *,
        source_kind: str = "masking_text",
    ) -> MaskedProjection:
        if not isinstance(text, str):
            raise PrivacyMaskingError("masking text must be a string")
        result = self.materialize_sync(text, source_kind=source_kind)
        masked = (
            result.get("final_payload")
            if isinstance(result, Mapping)
            else getattr(result, "final_payload", None)
        )
        if masked is None:
            masked = (
                result.get("payload")
                if isinstance(result, Mapping)
                else getattr(result, "payload", None)
            )
        if not isinstance(masked, str):
            raise PrivacyMaskingError("privacy masking produced no text projection")
        findings = (
            result.get("findings", ())
            if isinstance(result, Mapping)
            else getattr(result, "findings", ())
        )
        semantic_value = (
            result.get("semantic_status", "disabled")
            if isinstance(result, Mapping)
            else getattr(result, "semantic_status", "disabled")
        )
        return MaskedProjection(
            value=masked,
            findings=tuple(findings or ()),
            semantic_status=str(semantic_value or "disabled").strip().casefold(),
        )

    # Domain-language aliases used by backend adapters.  They intentionally
    # point at the same one-way implementation rather than introducing a
    # second lifecycle that might restore aliases.
    materialize_one_way = materialize
    materialize_one_way_sync = materialize_sync


# Short aliases are intentionally exported for integrations that refer to this
# boundary by its domain term rather than the longer class name.
MaskingBoundary = PrivacyMaskingBoundary


def new_masking_boundary(
    config: Any | None = None,
    *,
    semantic_redactor: Callable[..., Any] | None = None,
) -> PrivacyMaskingBoundary:
    """Create one fresh invocation-scoped boundary."""

    return PrivacyMaskingBoundary(
        config,
        semantic_redactor=semantic_redactor,
    )


async def materialize_one_way(
    value: Any,
    *,
    config: Any | None = None,
    semantic_redactor: Callable[..., Any] | None = None,
    source_kind: str = "masking",
) -> PrivacyResult:
    """One-shot helper that guarantees a fresh invocation boundary."""

    boundary = new_masking_boundary(
        config,
        semantic_redactor=semantic_redactor,
    )
    return await boundary.materialize(value, source_kind=source_kind)


__all__ = [
    "MaskedProjection",
    "MaskingBoundary",
    "PrivacyMaskingBoundary",
    "PrivacyMaskingError",
    "materialize_one_way",
    "new_masking_boundary",
]
