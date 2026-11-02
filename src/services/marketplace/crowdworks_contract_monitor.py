"""Shared CrowdWorks contract and escrow monitoring."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import false, func, select

from ...memory.models.operations import EngagementOpportunity, ExternalAction
from .bridge_inspection import BridgeInspectionLease, require_crowdworks_url
from .crowdworks_specialists import is_development_kind


_CROWDWORKS_BROWSER_BACKENDS = frozenset({"bridge", "local"})

_ESCROW_CONFIRMED_MARKERS = ("仮払い完了", "仮払い済み", "支払い済み", "支払済み", "エスクロー完了")
_ESCROW_NEGATIVE_MARKERS = ("仮払い待ち", "仮払い未完了", "支払い待ち", "支払待ち", "未払い", "エスクロー待ち")
_CONTRACT_MARKERS = ("契約成立", "契約中")
_ACTIVE_WORK_MARKERS = ("作業中", "業務開始")
_PAYMENT_CONTEXT_MARKERS = ("仮払い", "エスクロー", "支払い", "支払")

def crowdworks_contract_stage(page_text: Any) -> str | None:
    """Classify contract/escrow state without treating contract as payment.

    CrowdWorks' inspector currently returns the main-document text rather than
    a stable structured payment field.  Explicit negative escrow text wins;
    active-work labels are never payment evidence.  Only an explicit
    affirmative escrow marker can confirm payment; ambiguous or generic
    payment-condition text remains pending.
    """

    text = str(page_text or "").replace("\u3000", " ").strip()
    if not text:
        return None
    if any(marker in text for marker in _ESCROW_NEGATIVE_MARKERS):
        return "escrow_pending"
    if any(marker in text for marker in _ESCROW_CONFIRMED_MARKERS):
        return "escrow_confirmed"
    if any(marker in text for marker in _ACTIVE_WORK_MARKERS):
        # "作業中"/"業務開始" and generic payment-condition wording are
        # not affirmative escrow evidence.  The explicit markers above are
        # the sole confirmation path.
        return "escrow_pending"
    if any(marker in text for marker in _CONTRACT_MARKERS):
        return "escrow_pending"
    if any(marker in text for marker in _PAYMENT_CONTEXT_MARKERS):
        # Payment-related context such as terms, instructions, or a method
        # description is not proof that escrow was actually funded.
        return "escrow_pending"
    return None


class ContractMonitorDiagnostics(Protocol):
    last_contract_monitor_target_id: str | None
    last_browser_backend: str | None
    last_browser_reason_code: str | None
    last_browser_skip_code: str | None


class CrowdWorksContractMonitor:
    def __init__(
        self,
        *,
        resolve_reusable_tab: Callable[..., Awaitable[dict[str, Any]]],
        diagnostics: ContractMonitorDiagnostics,
    ) -> None:
        self._resolve_reusable_tab = resolve_reusable_tab
        self._diagnostics = diagnostics

    async def monitor_contracts(
        self,
        session,
        *,
        owner_user_id: str,
        connection_id,
        browser_bridge: Any = None,
        browser_backend: str | None = None,
        max_rows: int = 100,
        check_lease: Callable[[], Awaitable[None]] | None = None,
    ) -> int | None:
        self._diagnostics.last_contract_monitor_target_id = None
        contract_monitor_stopped = func.coalesce(
            EngagementOpportunity.metadata_json["crowdworks_unit_failures"]["contract_monitor"]["stopped"]
            .as_boolean(),
            false(),
        )
        action_exists = select(ExternalAction.id).where(
            ExternalAction.opportunity_id == EngagementOpportunity.id,
            ExternalAction.action_type == "engagement.submit_application",
            ExternalAction.status == "succeeded",
        ).exists()
        rows = (
            await session.scalars(
                select(EngagementOpportunity)
                .where(
                    EngagementOpportunity.connection_id == connection_id,
                    EngagementOpportunity.status.in_(("open", "escrow_pending")),
                    action_exists,
                    ~contract_monitor_stopped,
                )
                .order_by(
                    EngagementOpportunity.metadata_json["crowdworks_contract_last_checked_at"]
                    .as_string().asc().nullsfirst(),
                    EngagementOpportunity.created_at.asc(),
                    EngagementOpportunity.id.asc(),
                )
                .limit(max(1, min(100, int(max_rows))))
            )
        ).all()
        bridge: Any = browser_bridge
        backend = browser_backend
        accepted = 0
        lease_entered = False
        monitor_tab_id: int | None = None
        async with AsyncExitStack() as stack:
            for row in rows:
                self._diagnostics.last_contract_monitor_target_id = str(row.id)
                action = await session.scalar(
                    select(ExternalAction)
                    .where(
                        ExternalAction.opportunity_id == row.id,
                        ExternalAction.action_type == "engagement.submit_application",
                        ExternalAction.status == "succeeded",
                    )
                    .order_by(ExternalAction.created_at.desc())
                    .limit(1)
                )
                if action is None:
                    continue
                if not lease_entered:
                    if (
                        bridge is None
                        or not isinstance(backend, str)
                        or backend not in _CROWDWORKS_BROWSER_BACKENDS
                    ):
                        self._diagnostics.last_browser_backend = (
                            backend
                            if isinstance(backend, str)
                            and backend in _CROWDWORKS_BROWSER_BACKENDS
                            else None
                        )
                        self._diagnostics.last_browser_reason_code = "browser_backend_unavailable"
                        self._diagnostics.last_browser_skip_code = "skipped_browser_unavailable"
                        return None
                    bridge = await stack.enter_async_context(BridgeInspectionLease(bridge))
                    lease_entered = True
                target = await self._resolve_reusable_tab(
                    bridge, row.source_url, tab_id=monitor_tab_id, backend=backend
                )
                monitor_tab_id = target["tab_id"]
                if backend == "local":
                    inspection_url = require_crowdworks_url(row.source_url)
                    navigation = await bridge.request(
                        "navigate", tab_id=monitor_tab_id, url=inspection_url
                    )
                    if (
                        not isinstance(navigation, dict)
                        or navigation.get("ok") is not True
                        or navigation.get("executed") is not True
                        or navigation.get("error")
                        or navigation.get("tab_id", monitor_tab_id) != monitor_tab_id
                    ):
                        raise RuntimeError("crowdworks_navigation_failed")
                    verification = await bridge.request(
                        "verify",
                        tab_id=monitor_tab_id,
                        expected_url=inspection_url,
                    )
                    if (
                        not isinstance(verification, dict)
                        or verification.get("matched") is not True
                    ):
                        raise RuntimeError("crowdworks_detail_url_unverified")
                observation = await bridge.request(
                    "crowdworks_inspect", tab_id=monitor_tab_id
                )
                if check_lease is not None:
                    await check_lease()
                page_text = str(observation.get("text") or "")
                stage = crowdworks_contract_stage(page_text)
                metadata = dict(row.metadata_json or {})
                metadata["crowdworks_contract_last_checked_at"] = datetime.now(timezone.utc).isoformat()
                if stage is None:
                    row.metadata_json = metadata
                    await session.commit()
                    continue
                metadata["workflow_stage"] = stage.upper()
                metadata["escrow_confirmation"] = stage == "escrow_confirmed"
                if stage != "escrow_confirmed":
                    metadata["fulfillment_blocked_reason"] = "escrow_confirmation_required"
                    row.status = "escrow_pending"
                    row.metadata_json = metadata
                    await session.commit()
                    continue
                metadata.pop("fulfillment_blocked_reason", None)
                metadata["workflow_stage"] = "CONTRACT_ACCEPTED"
                row.metadata_json = metadata
                row.status = "contract_accepted"
                if is_development_kind(metadata.get("worker_kind")):
                    from .client_development import ensure_engagement_project

                    # Acceptance, Project and grant must commit together. Otherwise
                    # an accepted row cannot be rediscovered after lease loss.
                    await ensure_engagement_project(
                        session, row, owner_user_id=owner_user_id, commit=False
                    )
                if check_lease is not None:
                    await check_lease()
                await session.commit()
                accepted += 1
        return accepted
