"""CrowdWorks provider adapter using the selected logged-in Edge session."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from ..browser_agent_service import BrowserAgentService
from ..integration_action_registry import DefinitelyNotSent, IntegrationActionResult
from ..pc_bridge_service import RemoteControlBridge, get_pc_bridge_hub
from .crowdworks_action import CrowdWorksApplicationPayload


_CONFIRMATION_MARKERS = (
    "応募しました",
    "応募が完了",
    "応募完了",
    "応募内容",
    "契約金額の提示",
)


class CrowdWorksBrowserAdapter:
    adapter_key = "crowdworks_browser"
    adapter_version = "1"

    def __init__(self, config: Any = None):
        self.config = config

    async def prepare(self, *, payload, connection, credential):
        return None

    def _browser_client(self, routing):
        """Browser client for the proposing Agent's ``operator`` Subagent.

        An application proposed by an Operations Agent is submitted with the
        route of its exact AgentRevision.  Only an action without an Agent
        origin (legacy/human-created) uses the configured Main model.
        """

        from ...llm.manager import create_llm_client, create_llm_client_for_target
        from ..agent_model_routing import (
            AgentModelRouteError,
            AgentRoutingPins,
            resolve_agent_role_route,
        )

        if routing is None:
            return create_llm_client(self.config)
        try:
            if not isinstance(routing, dict) or routing.get("error"):
                raise AgentModelRouteError("agent_route_snapshot_invalid")
            if not isinstance(routing.get("route_snapshot"), dict):
                raise AgentModelRouteError("agent_route_snapshot_missing")
            pins = AgentRoutingPins(
                agent_id=routing.get("agent_id"),
                agent_revision_id=routing.get("agent_revision_id"),
                agent_team_id=str(routing.get("agent_team_id") or ""),
                execution_profile_id=str(routing.get("execution_profile_id") or ""),
                allowed_subagent_ids=tuple(routing.get("allowed_subagent_ids") or ()),
                route_snapshot=routing["route_snapshot"],
            )
            route = resolve_agent_role_route(self.config, pins, "operator")
        except AgentModelRouteError as exc:
            raise DefinitelyNotSent("crowdworks_model_route_unavailable") from exc
        return create_llm_client_for_target(
            self.config,
            provider=route.provider,
            model=route.model,
            effort=route.effort or "",
            provider_options={"lightweight_client": True},
        )

    async def execute(
        self,
        *,
        action_id,
        attempt_id,
        execution_key,
        payload,
        connection,
        credential,
        quote,
    ):
        data = CrowdWorksApplicationPayload.model_validate(payload)
        owner = str(connection.get("owner_user_id") or "")
        device = str(connection.get("device_id") or "")
        if not owner or not device:
            raise DefinitelyNotSent("crowdworks_bridge_binding_missing")

        hub = get_pc_bridge_hub()
        try:
            bridge = RemoteControlBridge(owner, device, hub)
        except Exception as exc:
            raise DefinitelyNotSent("crowdworks_bridge_offline") from exc

        from ..session_llm_generation import cleanup_ephemeral_llm_client

        client = self._browser_client(connection.get("origin_agent_routing"))
        goal = (
            "CrowdWorksの案件ページで、この案件への応募を一度だけ送信してください。"
            "既に応募済みなら再送信せず、その状態を確認して終了してください。"
            "応募文・金額・納期は以下の値をそのまま使い、推測で別の金額や文面に変更しないでください。\n"
            f"応募文: {data.message}\n"
            f"提示金額: {data.offered_price if data.offered_price is not None else '案件/フォーム既定値を変更しない'}\n"
            f"通貨: {data.currency or 'JPY'}\n"
            f"納期: {data.delivery_estimate or '案件/フォーム既定値を変更しない'}\n"
            "送信ボタンを押した後は、完了表示または応募済み表示を読み取り、同じ送信を繰り返さないでください。"
        )
        try:
            tab_id = None
            try:
                tabs = await bridge.request("tabs")
                rows = tabs.get("tabs") if isinstance(tabs, dict) else []
                if isinstance(rows, list):
                    for item in rows:
                        if not isinstance(item, dict):
                            continue
                        url = str(item.get("url") or "")
                        if url.startswith("https://crowdworks.jp/public/jobs/") or url.startswith("https://www.crowdworks.jp/public/jobs/"):
                            value = item.get("tab_id")
                            if isinstance(value, int):
                                tab_id = value
                                break
            except Exception:
                tab_id = None
            if tab_id is not None:
                await bridge.request("wake_tab", tab_id=tab_id)
            service = BrowserAgentService(self.config, client=client, bridge=bridge)
            result = await service.run_goal(goal=goal, start_url=data.source_url, tab_id=tab_id)
        finally:
            await cleanup_ephemeral_llm_client(client)

        observation = result.get("observation") if isinstance(result, dict) else {}
        observation = observation if isinstance(observation, dict) else {}
        final_text = str(observation.get("text") or "")
        final_url = str(observation.get("url") or data.source_url)
        confirmed = any(marker in final_text for marker in _CONFIRMATION_MARKERS)
        if not confirmed:
            # URL patterns are only secondary evidence. Do not call a generic
            # steps_completed result a provider-confirmed success.
            confirmed = bool(re.search(r"/(contracts?|applications?|proposals?)/[^/?#]+", final_url, re.I))
        if not confirmed:
            return IntegrationActionResult(
                "uncertain",
                provider_attempt_ref=f"cw-attempt:{attempt_id}",
                remote_url=final_url,
                safe_error_code="crowdworks_submission_unconfirmed",
            )

        observed_at = datetime.utcnow()
        receipt_material = f"{action_id}\n{attempt_id}\n{final_url}\n" + "\n".join(
            marker for marker in _CONFIRMATION_MARKERS if marker in final_text
        )
        receipt_ref = "cwobs:" + hashlib.sha256(receipt_material.encode("utf-8")).hexdigest()
        remote_id_match = re.search(r"/(?:contracts?|applications?|proposals?)/([^/?#]+)", final_url, re.I)
        remote_id = remote_id_match.group(1) if remote_id_match else f"opportunity-{data.opportunity_id}"
        return IntegrationActionResult(
            "succeeded",
            provider_attempt_ref=f"cw-attempt:{attempt_id}",
            provider_receipt_ref=receipt_ref,
            remote_resource_id=remote_id,
            remote_url=final_url,
            remote_status="submitted",
            observed_at=observed_at,
        )


__all__ = ["CrowdWorksBrowserAdapter"]
