"""Code-owned MediaOps provider capability policy.

The registry is deliberately a *declaration*, not a credential or provider
health check.  It records what the dated official-source matrix says about a
provider and the safe fallback available to a human operator.  Runtime
credential/account observations remain authoritative in
``PlatformAccountRevision`` and ``MediaPlatformCredential``; an observation
never upgrades an ``unverified`` operation to an executable operation.

No provider payload, token, cookie, request header or response body is accepted
by this module.  The only runtime input accepted by the effective-status
helpers is the already-redacted capability/status projection from the vault.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any


class CapabilityStatus(str, Enum):
    """Closed status vocabulary for a provider operation."""

    AUTOMATABLE = "automatable"
    MANUAL = "manual"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"
    UNAVAILABLE = "unavailable"


ProviderCapabilityStatus = CapabilityStatus


CAPABILITY_REGISTRY_VERSION = "2026-09-01"
CAPABILITY_MATRIX_SOURCE = "docs/media_platform_capability_matrix.md"

CAPABILITY_OPERATIONS: tuple[str, ...] = (
    "identity",
    "oauth",
    "token",
    "cookie",
    "text",
    "image",
    "video",
    "schedule",
    "edit",
    "delete",
    "analytics",
    "revenue",
    "refresh",
    "revoke",
)

_OPERATION_SET = frozenset(CAPABILITY_OPERATIONS)
_PROVIDERS = ("x", "pixiv", "patreon", "youtube", "instagram", "dlsite")
_PROVIDER_SET = frozenset(_PROVIDERS)


def _status(value: CapabilityStatus | str) -> CapabilityStatus:
    if isinstance(value, CapabilityStatus):
        return value
    try:
        return CapabilityStatus(str(value).strip().lower())
    except (TypeError, ValueError):
        return CapabilityStatus.UNAVAILABLE


def _platform(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    rendered = value.strip().lower()
    return rendered if rendered in _PROVIDER_SET else None


def _operation(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    rendered = value.strip().lower()
    return rendered if rendered in _OPERATION_SET else None


@dataclass(frozen=True, slots=True)
class ProviderCapabilityPolicy:
    """Immutable policy for one provider.

    ``operations`` is copied into a read-only mapping during construction.
    ``runtime_verified`` is intentionally always false in the checked-in
    registry: only a bounded credential/account observation can establish
    runtime evidence.
    """

    platform: str
    display_name: str
    operations: Mapping[str, CapabilityStatus]
    adapter_ref: str
    sources: tuple[str, ...]
    adapter_key: str | None = None
    adapter_version: str = "1"
    registry_version: str = CAPABILITY_REGISTRY_VERSION
    runtime_verified: bool = False
    manual_fallback: CapabilityStatus = CapabilityStatus.MANUAL

    def __post_init__(self) -> None:
        platform = _platform(self.platform)
        if platform is None or platform != self.platform:
            raise ValueError("provider platform is not in the closed MediaOps set")
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise ValueError("provider display_name is required")
        if not isinstance(self.adapter_ref, str) or not self.adapter_ref.strip():
            raise ValueError("provider adapter_ref is required")
        adapter_key = self.adapter_key or self.adapter_ref
        if not isinstance(adapter_key, str) or not adapter_key.strip():
            raise ValueError("provider adapter_key is required")
        if not isinstance(self.adapter_version, str) or not self.adapter_version.strip():
            raise ValueError("adapter_version is required")
        if not isinstance(self.registry_version, str) or not self.registry_version.strip():
            raise ValueError("registry_version is required")
        if set(self.operations) != _OPERATION_SET:
            raise ValueError("provider operation policy must cover the closed operation set")
        normalized: dict[str, CapabilityStatus] = {}
        for key, value in self.operations.items():
            if not isinstance(key, str) or key not in _OPERATION_SET:
                raise ValueError("provider operation name is invalid")
            normalized[key] = _status(value)
        object.__setattr__(self, "operations", MappingProxyType(normalized))
        object.__setattr__(self, "sources", tuple(str(item) for item in self.sources))
        object.__setattr__(self, "adapter_key", adapter_key)
        object.__setattr__(self, "manual_fallback", _status(self.manual_fallback))
        # The code-owned registry must never claim a real runtime probe.
        object.__setattr__(self, "runtime_verified", False)

    def status_for(self, operation: str) -> CapabilityStatus:
        """Return a closed status; unknown operation names fail closed."""

        key = _operation(operation)
        if key is None:
            return CapabilityStatus.UNAVAILABLE
        return self.operations[key]

    operation_status = status_for

    def safe_dict(self, *, observation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return a bounded, secret-free policy projection.

        ``observation`` is expected to be the vault's safe capability
        projection.  Unknown keys and all values other than the closed status
        vocabulary are ignored by :func:`effective_operation_status`.
        """

        result: dict[str, Any] = {
            "platform": self.platform,
            "display_name": self.display_name,
            "registry_version": self.registry_version,
            "adapter_ref": self.adapter_ref,
            "adapter_key": self.adapter_key,
            "adapter_version": self.adapter_version,
            "runtime_verified": False,
            "manual_fallback": self.manual_fallback.value,
            "operations": {
                operation: self.operations[operation].value
                for operation in CAPABILITY_OPERATIONS
            },
            "sources": list(self.sources),
        }
        if observation is not None:
            result["effective_operations"] = {
                operation: effective_operation_status(
                    self.platform,
                    operation,
                    observation=observation,
                ).value
                for operation in CAPABILITY_OPERATIONS
            }
        return result


def _operations(**overrides: CapabilityStatus | str) -> Mapping[str, CapabilityStatus]:
    """Build a complete map while making omissions explicit and reviewable."""

    values = {operation: CapabilityStatus.UNAVAILABLE for operation in CAPABILITY_OPERATIONS}
    for operation, value in overrides.items():
        if operation not in _OPERATION_SET:
            raise ValueError(f"unknown provider operation: {operation}")
        values[operation] = _status(value)
    return values


_MATRIX = CAPABILITY_MATRIX_SOURCE


def _policy(
    platform: str,
    display_name: str,
    operations: Mapping[str, CapabilityStatus],
    *,
    source_urls: tuple[str, ...],
) -> ProviderCapabilityPolicy:
    return ProviderCapabilityPolicy(
        platform=platform,
        display_name=display_name,
        operations=operations,
        adapter_ref=f"media-provider/{platform}/capability-policy",
        adapter_key=f"media-provider/{platform}",
        sources=(_MATRIX, *source_urls),
    )


# The matrix is dated design evidence.  A bounded identity probe exists for
# X/YouTube/Patreon, but those providers still have no live sandbox evidence;
# identity therefore remains ``unverified`` in the static registry.  No
# operation is declared executable until a provider-specific adapter contract
# and postcondition are reviewed.  Pixiv and DLsite explicitly stay on the
# human/manual path because a public creator API was not confirmed.
_REGISTRY: Mapping[str, ProviderCapabilityPolicy] = MappingProxyType(
    {
        "x": _policy(
            "x",
            "X",
            _operations(
                identity=CapabilityStatus.UNVERIFIED,
                oauth=CapabilityStatus.UNVERIFIED,
                token=CapabilityStatus.UNVERIFIED,
                cookie=CapabilityStatus.MANUAL,
                text=CapabilityStatus.UNVERIFIED,
                image=CapabilityStatus.UNVERIFIED,
                video=CapabilityStatus.UNVERIFIED,
                schedule=CapabilityStatus.MANUAL,
                edit=CapabilityStatus.UNVERIFIED,
                delete=CapabilityStatus.UNVERIFIED,
                analytics=CapabilityStatus.UNVERIFIED,
                revenue=CapabilityStatus.UNSUPPORTED,
                refresh=CapabilityStatus.UNVERIFIED,
                revoke=CapabilityStatus.UNVERIFIED,
            ),
            source_urls=(
                "https://docs.x.com/x-api/posts/manage-tweets/introduction",
                "https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code",
                "https://docs.x.com/x-api/fundamentals/metrics",
            ),
        ),
        "pixiv": _policy(
            "pixiv",
            "pixiv",
            _operations(
                identity=CapabilityStatus.MANUAL,
                oauth=CapabilityStatus.UNSUPPORTED,
                token=CapabilityStatus.UNSUPPORTED,
                cookie=CapabilityStatus.MANUAL,
                text=CapabilityStatus.MANUAL,
                image=CapabilityStatus.MANUAL,
                video=CapabilityStatus.UNSUPPORTED,
                schedule=CapabilityStatus.MANUAL,
                edit=CapabilityStatus.MANUAL,
                delete=CapabilityStatus.MANUAL,
                analytics=CapabilityStatus.MANUAL,
                revenue=CapabilityStatus.UNSUPPORTED,
                refresh=CapabilityStatus.UNSUPPORTED,
                revoke=CapabilityStatus.UNSUPPORTED,
            ),
            source_urls=(
                "https://www.pixiv.help/hc/ja/articles/235584228-pixiv%E3%81%AB%E5%B0%8F%E8%AA%AC%E3%82%92%E6%8A%95%E7%A8%BF%E3%81%99%E3%82%8B%E6%96%B9%E6%B3%95%E3%82%92%E7%9F%A5%E3%82%8A%E3%81%9F%E3%81%84",
                "https://www.pixiv.help/hc/ja/articles/115003430413-%E6%8A%95%E7%A8%BF%E6%97%A5%E6%99%82%E3%82%92%E6%8C%87%E5%AE%9A%E3%81%97%E3%81%A6%E4%BD%9C%E5%93%81%E3%82%92%E6%8A%95%E7%A8%BF%E3%81%97%E3%81%9F%E3%81%84",
                "https://www.pixiv.help/hc/ja/articles/235645887-pixiv%E3%81%AB%E6%8A%95%E7%A8%BF%E3%81%97%E3%81%9F%E4%BD%9C%E5%93%81%E3%81%AE%E6%83%85%E5%A0%B1%E3%82%92%E5%A4%89%E3%81%88%E3%81%9F%E3%81%84",
            ),
        ),
        "patreon": _policy(
            "patreon",
            "Patreon",
            _operations(
                identity=CapabilityStatus.UNVERIFIED,
                oauth=CapabilityStatus.UNVERIFIED,
                token=CapabilityStatus.UNVERIFIED,
                cookie=CapabilityStatus.MANUAL,
                text=CapabilityStatus.UNVERIFIED,
                image=CapabilityStatus.UNVERIFIED,
                video=CapabilityStatus.UNVERIFIED,
                schedule=CapabilityStatus.MANUAL,
                edit=CapabilityStatus.MANUAL,
                delete=CapabilityStatus.MANUAL,
                analytics=CapabilityStatus.UNVERIFIED,
                revenue=CapabilityStatus.UNVERIFIED,
                refresh=CapabilityStatus.UNVERIFIED,
                revoke=CapabilityStatus.UNVERIFIED,
            ),
            source_urls=(
                "https://docs.patreon.com/",
                "https://support.patreon.com/hc/en-us/articles/360031956632-Scheduled-posts",
                "https://support.patreon.com/hc/en-us/articles/360042841711-Post-Insights",
            ),
        ),
        "youtube": _policy(
            "youtube",
            "YouTube",
            _operations(
                identity=CapabilityStatus.UNVERIFIED,
                oauth=CapabilityStatus.UNVERIFIED,
                token=CapabilityStatus.UNVERIFIED,
                cookie=CapabilityStatus.UNSUPPORTED,
                text=CapabilityStatus.UNVERIFIED,
                image=CapabilityStatus.UNVERIFIED,
                video=CapabilityStatus.UNVERIFIED,
                schedule=CapabilityStatus.UNVERIFIED,
                edit=CapabilityStatus.UNVERIFIED,
                delete=CapabilityStatus.UNVERIFIED,
                analytics=CapabilityStatus.UNVERIFIED,
                revenue=CapabilityStatus.MANUAL,
                refresh=CapabilityStatus.UNVERIFIED,
                revoke=CapabilityStatus.UNVERIFIED,
            ),
            source_urls=(
                "https://developers.google.com/youtube/v3/docs/videos",
                "https://developers.google.com/youtube/v3/docs/videos/insert",
                "https://developers.google.com/youtube/analytics/metrics",
            ),
        ),
        "instagram": _policy(
            "instagram",
            "Instagram",
            _operations(
                identity=CapabilityStatus.UNVERIFIED,
                oauth=CapabilityStatus.UNVERIFIED,
                token=CapabilityStatus.UNVERIFIED,
                cookie=CapabilityStatus.MANUAL,
                text=CapabilityStatus.UNVERIFIED,
                image=CapabilityStatus.UNVERIFIED,
                video=CapabilityStatus.UNVERIFIED,
                schedule=CapabilityStatus.MANUAL,
                edit=CapabilityStatus.MANUAL,
                delete=CapabilityStatus.MANUAL,
                analytics=CapabilityStatus.UNVERIFIED,
                revenue=CapabilityStatus.MANUAL,
                refresh=CapabilityStatus.UNVERIFIED,
                revoke=CapabilityStatus.UNVERIFIED,
            ),
            source_urls=(
                "https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api",
                "https://www.postman.com/meta/instagram/folder/u4g5a2a/instagram-api-with-facebook-login",
            ),
        ),
        "dlsite": _policy(
            "dlsite",
            "DLsite",
            _operations(
                identity=CapabilityStatus.MANUAL,
                oauth=CapabilityStatus.UNSUPPORTED,
                token=CapabilityStatus.UNSUPPORTED,
                cookie=CapabilityStatus.MANUAL,
                text=CapabilityStatus.MANUAL,
                image=CapabilityStatus.MANUAL,
                video=CapabilityStatus.MANUAL,
                schedule=CapabilityStatus.MANUAL,
                edit=CapabilityStatus.MANUAL,
                delete=CapabilityStatus.MANUAL,
                analytics=CapabilityStatus.MANUAL,
                revenue=CapabilityStatus.MANUAL,
                refresh=CapabilityStatus.UNSUPPORTED,
                revoke=CapabilityStatus.UNSUPPORTED,
            ),
            source_urls=(
                "https://cs-circle.dlsite.com/hc/ja/articles/1500003095021-%E3%83%9E%E3%83%B3%E3%82%AC%E7%99%BB%E9%8C%B2%E3%82%AC%E3%82%A4%E3%83%89",
                "https://info.eisys.co.jp/dlsite",
            ),
        ),
    }
)


def get_provider_capability_registry() -> Mapping[str, ProviderCapabilityPolicy]:
    """Return the immutable six-provider registry."""

    return _REGISTRY


def get_provider_capability(platform: str) -> ProviderCapabilityPolicy | None:
    """Return a policy for ``platform`` or ``None`` for an unknown provider."""

    key = _platform(platform)
    return _REGISTRY.get(key) if key is not None else None


def require_provider_capability(platform: str) -> ProviderCapabilityPolicy:
    policy = get_provider_capability(platform)
    if policy is None:
        raise KeyError("provider is not in the closed MediaOps registry")
    return policy


def operation_capability(platform: str, operation: str) -> CapabilityStatus:
    """Return one static operation status, failing closed for unknown input."""

    policy = get_provider_capability(platform)
    if policy is None:
        return CapabilityStatus.UNAVAILABLE
    return policy.status_for(operation)


def _observation_value(observation: Mapping[str, Any] | None, key: str) -> str | None:
    if not isinstance(observation, Mapping):
        return None
    value = observation.get(key)
    return value.strip().lower() if isinstance(value, str) else None


def _observed_capability_key(operation: str) -> str | None:
    if operation == "identity":
        return "identity"
    if operation in {"analytics"}:
        return "analytics"
    if operation in {"text", "image", "video", "publish"}:
        return "media"
    return None


def _static_coarse_status(policy: ProviderCapabilityPolicy, key: str) -> CapabilityStatus:
    """Map the legacy four-key vault projection to operation policy."""

    if key == "identity":
        return policy.status_for("identity")
    if key == "analytics":
        return policy.status_for("analytics")
    if key == "publish":
        operations = ("text", "image", "video")
    elif key == "media":
        operations = ("image", "video")
    else:
        return CapabilityStatus.UNAVAILABLE
    statuses = [policy.status_for(operation) for operation in operations]
    if all(item is CapabilityStatus.UNSUPPORTED for item in statuses):
        return CapabilityStatus.UNSUPPORTED
    if all(item is CapabilityStatus.UNAVAILABLE for item in statuses):
        return CapabilityStatus.UNAVAILABLE
    if any(item is CapabilityStatus.AUTOMATABLE for item in statuses):
        return CapabilityStatus.AUTOMATABLE
    if any(item is CapabilityStatus.UNVERIFIED for item in statuses):
        return CapabilityStatus.UNVERIFIED
    return CapabilityStatus.MANUAL


def bound_credential_capabilities(
    platform: str,
    capabilities: Mapping[str, Any] | None,
    *,
    credential_status: str | None = None,
) -> dict[str, str]:
    """Keep the vault's coarse capability map below static policy authority.

    This helper accepts only the already-redacted four capability fields.  A
    provider adapter cannot turn an unverified/manual/unsupported operation
    into ``available``.  The returned shape intentionally matches the legacy
    ``MediaPlatformCredential`` DTO and contains no provider data.
    """

    policy = get_provider_capability(platform)
    result = {key: "unknown" for key in ("identity", "publish", "media", "analytics")}
    if policy is None or not isinstance(capabilities, Mapping):
        return result
    status = credential_status.strip().lower() if isinstance(credential_status, str) else None
    for key in result:
        value = capabilities.get(key)
        if not isinstance(value, str):
            continue
        rendered = value.strip().lower()
        static = _static_coarse_status(policy, key)
        if rendered == "unsupported":
            if static is CapabilityStatus.UNSUPPORTED:
                result[key] = "unsupported"
            continue
        if rendered != "available":
            continue
        # Identity evidence is useful to the vault even while the provider's
        # operation remains unverified.  It never makes a write operation
        # executable.  Other capabilities require a future explicit
        # automatable policy entry and a verified credential.
        if key == "identity" and static in {
            CapabilityStatus.AUTOMATABLE,
            CapabilityStatus.UNVERIFIED,
        } and status == "verified":
            result[key] = "available"
        elif static is CapabilityStatus.AUTOMATABLE and status == "verified":
            result[key] = "available"
    return result


def effective_operation_status(
    platform: str,
    operation: str,
    *,
    observation: Mapping[str, Any] | None = None,
) -> CapabilityStatus:
    """Combine static policy with a redacted runtime observation.

    Static ``manual``, ``unsupported`` and ``unavailable`` statuses are never
    upgraded.  ``unverified`` is also never upgraded: a provider-specific
    adapter contract and safe postcondition are required before changing code.
    The checked-in six-provider registry has no automatable operation yet;
    future entries must still require a verified vault observation.
    """

    policy = get_provider_capability(platform)
    if policy is None:
        return CapabilityStatus.UNAVAILABLE
    # A revoked/disabled credential makes every operation unavailable, even
    # when the code-owned provider policy is manual or still unverified.  Do
    # this check before the static status return so stale observations cannot
    # be presented as a usable manual/unknown capability.
    credential_status = _observation_value(observation, "status")
    if credential_status in {"disabled", "invalid", "key_unavailable", "unavailable"}:
        return CapabilityStatus.UNAVAILABLE
    static = policy.status_for(operation)
    if static is not CapabilityStatus.AUTOMATABLE:
        return static
    if not isinstance(observation, Mapping):
        return CapabilityStatus.UNVERIFIED
    key = _observed_capability_key(_operation(operation) or "")
    if key is None:
        return CapabilityStatus.UNVERIFIED
    capability = _observation_value(observation, key)
    if capability == "unsupported":
        return CapabilityStatus.UNSUPPORTED
    if credential_status == "verified" and capability == "available":
        return CapabilityStatus.AUTOMATABLE
    return CapabilityStatus.UNVERIFIED


def is_operation_automatable(
    platform: str,
    operation: str,
    *,
    observation: Mapping[str, Any] | None = None,
) -> bool:
    """Return true only for an explicitly automatable, verified operation."""

    return effective_operation_status(
        platform,
        operation,
        observation=observation,
    ) is CapabilityStatus.AUTOMATABLE


def safe_provider_capability_projection(
    platform: str | None = None,
    *,
    observation: Mapping[str, Any] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Return safe policy DTO(s) for API/UI/agent boundaries."""

    if platform is None:
        return [
            _REGISTRY[item].safe_dict(observation=observation)
            for item in _PROVIDERS
        ]
    policy = get_provider_capability(platform)
    if policy is None:
        # Unknown provider output is intentionally non-descriptive and cannot
        # be mistaken for a registered adapter.
        return {
            "platform": str(platform).strip().lower() if isinstance(platform, str) else None,
            "registry_version": CAPABILITY_REGISTRY_VERSION,
            "adapter_ref": None,
            "runtime_verified": False,
            "operations": {
                operation: CapabilityStatus.UNAVAILABLE.value
                for operation in CAPABILITY_OPERATIONS
            },
        }
    return policy.safe_dict(observation=observation)


# Short aliases make the policy convenient for route/service call sites while
# keeping the descriptive names above as the public contract.
get_media_provider_capability = get_provider_capability
get_media_provider_capability_registry = get_provider_capability_registry
provider_operation_status = operation_capability
provider_effective_operation_status = effective_operation_status


__all__ = [
    "CAPABILITY_MATRIX_SOURCE",
    "CAPABILITY_OPERATIONS",
    "CAPABILITY_REGISTRY_VERSION",
    "CapabilityStatus",
    "ProviderCapabilityStatus",
    "ProviderCapabilityPolicy",
    "bound_credential_capabilities",
    "effective_operation_status",
    "get_media_provider_capability",
    "get_media_provider_capability_registry",
    "get_provider_capability",
    "get_provider_capability_registry",
    "is_operation_automatable",
    "operation_capability",
    "provider_effective_operation_status",
    "provider_operation_status",
    "require_provider_capability",
    "safe_provider_capability_projection",
]
