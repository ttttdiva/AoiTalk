"""Fast CrowdWorks list ingestion from one Edge DOM observation per page."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from sqlalchemy import select

from ...memory.models.operations import EngagementOpportunity
from ..operations_service import OperationsService
from ..pc_bridge_service import RemoteControlBridge, get_pc_bridge_hub
from .bridge_inspection import BridgeInspectionLease, require_crowdworks_url


_JOB_PATH = re.compile(r"^/public/jobs/(\d+)(?:/|$)")
_MONEY = re.compile(r"(?P<amount>[0-9][0-9,]*(?:\.[0-9]+)?)\s*円")


@dataclass(frozen=True)
class CrowdWorksListing:
    remote_id: str
    title: str
    url: str
    context: str
    compensation_text: str | None = None
    compensation_yen: float | None = None
    compensation_type: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "provider": "crowdworks",
            "remote_id": self.remote_id,
            "title": self.title,
            "url": self.url,
            "context": self.context,
            "compensation_text": self.compensation_text,
            "compensation_yen": self.compensation_yen,
            "compensation_type": self.compensation_type,
        }


def _page_url(url: str, page: int) -> str:
    parsed = urlsplit(url)
    items = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "page"]
    if page > 1:
        items.append(("page", str(page)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(items), ""))


def extract_listings(observation: dict[str, Any], *, base_url: str) -> list[CrowdWorksListing]:
    candidates = observation.get("candidates") if isinstance(observation, dict) else {}
    if not isinstance(candidates, dict):
        return []
    grouped: dict[str, dict[str, Any]] = {}
    for candidate in candidates.values():
        if not isinstance(candidate, dict):
            continue
        href = candidate.get("href") or candidate.get("url")
        if not isinstance(href, str) or not href.strip():
            continue
        absolute = urljoin(base_url, href.strip())
        parsed = urlsplit(absolute)
        if (parsed.hostname or "").casefold() not in {"crowdworks.jp", "www.crowdworks.jp"}:
            continue
        match = _JOB_PATH.match(parsed.path)
        if not match:
            continue
        remote_id = match.group(1)
        absolute = f"https://crowdworks.jp/public/jobs/{remote_id}"
        # CrowdWorks cards expose the job anchor text in ``label``.  The generic
        # browser observer's nearest ``heading`` may belong to a neighbouring
        # card, so never let it override the anchor's own label.
        context = " ".join(
            str(candidate.get(key) or "").strip()
            for key in ("context", "label")
            if candidate.get(key)
        )
        title = str(candidate.get("label") or candidate.get("heading") or "").strip()
        if not title:
            title = context.split("  ", 1)[0].strip() or f"CrowdWorks job {remote_id}"
        current = grouped.setdefault(remote_id, {"title": title, "url": absolute, "parts": []})
        if context and context not in current["parts"]:
            current["parts"].append(context)
        if len(title) > len(current["title"]):
            current["title"] = title

    result: list[CrowdWorksListing] = []
    for remote_id, item in grouped.items():
        context = " | ".join(item["parts"])[:16000]
        kind = "hourly" if "時間単価" in context else "fixed" if "固定報酬" in context else None
        # Titles can themselves contain prices (e.g. "5,000円/60分").  Prefer
        # the first amount *after* the compensation-type marker so the listing
        # budget is not confused with a price mentioned in the title.
        money_scope = context
        marker = re.search(r"(?:時間単価制|固定報酬制)", context)
        if marker is not None:
            money_scope = context[marker.end():]
        money = _MONEY.search(money_scope)
        compensation = float(money.group("amount").replace(",", "")) if money else None
        result.append(
            CrowdWorksListing(
                remote_id=remote_id,
                title=item["title"][:500],
                url=item["url"],
                context=context,
                compensation_text=money.group(0) if money else None,
                compensation_yen=compensation,
                compensation_type=kind,
            )
        )
    return result


class CrowdWorksSource:
    def __init__(self, *, operations: OperationsService | None = None):
        self.operations = operations or OperationsService()

    async def _bridge(self, owner_user_id: str, device_id: str | None = None) -> RemoteControlBridge:
        hub = get_pc_bridge_hub()
        selected = await hub.store.resolve(owner_user_id, device_id, None)
        connection = hub.connection(owner_user_id, selected)
        if not connection.edge_connected or "browser" not in connection.capabilities:
            raise RuntimeError("crowdworks_browser_session_unavailable")
        return RemoteControlBridge(owner_user_id, selected, hub)

    async def _resolve_reusable_tab(self, bridge: RemoteControlBridge, url: str, tab_id: int | None = None) -> dict[str, Any]:
        if getattr(bridge, "browser_backend", "extension") == "agent_browser":
            # Resolve only the GUI-selected target. Passing a URL here would
            # navigate before we have verified the user's selected origin.
            params = {"tab_id": tab_id} if tab_id is not None else {}
            target = await bridge.request("resolve_tab", **params)
            require_crowdworks_url(target.get("url"))
            require_crowdworks_url(url)
            selected_tab = target.get("tab_id")
            if type(selected_tab) is not int or (tab_id is not None and selected_tab != tab_id):
                raise RuntimeError("crowdworks_selected_target_changed")
            navigated = await bridge.request("navigate", tab_id=selected_tab, url=url)
            if not isinstance(navigated, dict):
                raise RuntimeError("crowdworks_navigation_failed")
            if navigated.get("ok") is not True or navigated.get("executed") is not True or navigated.get("error"):
                raise RuntimeError(str(navigated.get("error") or "crowdworks_navigation_failed"))
            if navigated.get("tab_id", selected_tab) != selected_tab:
                raise RuntimeError("crowdworks_selected_target_changed")
            return {**target, **navigated, "tab_id": selected_tab}
        if tab_id is not None:
            target = await bridge.request("resolve_tab", tab_id=tab_id, url=url)
            return target
        tabs = await bridge.request("tabs")
        rows = tabs.get("tabs") if isinstance(tabs, dict) else []
        reusable = None
        if isinstance(rows, list):
            for item in rows:
                if not isinstance(item, dict):
                    continue
                parsed = urlsplit(str(item.get("url") or ""))
                if (parsed.hostname or "").casefold() in {"crowdworks.jp", "www.crowdworks.jp"} and parsed.path.startswith("/public/jobs"):
                    reusable = item
                    break
        if reusable and isinstance(reusable.get("tab_id"), int):
            return await bridge.request("resolve_tab", tab_id=reusable["tab_id"], url=url)
        return await bridge.request("resolve_tab", url=url)

    async def fetch_pages(
        self,
        *,
        owner_user_id: str,
        search_url: str,
        max_pages: int = 3,
        max_items: int = 100,
        device_id: str | None = None,
    ) -> list[CrowdWorksListing]:
        raw_bridge = await self._bridge(owner_user_id, device_id)
        tab_id = None
        result: list[CrowdWorksListing] = []
        seen: set[str] = set()
        async with BridgeInspectionLease(raw_bridge) as bridge:
            for page in range(1, max(1, max_pages) + 1):
                url = _page_url(search_url, page)
                target = await self._resolve_reusable_tab(bridge, url, tab_id=tab_id)
                tab_id = target["tab_id"]
                if not bridge.helper_backend and target.get("url") != url:
                    target = await bridge.request("navigate", tab_id=tab_id, url=url)
                    tab_id = target.get("tab_id", tab_id)
                observation = await bridge.request("crowdworks_inspect", tab_id=tab_id)
                page_items = extract_listings(observation, base_url=url)
                if not page_items:
                    break
                for item in page_items:
                    if item.remote_id in seen:
                        continue
                    seen.add(item.remote_id)
                    result.append(item)
                    if len(result) >= max_items:
                        return result
        return result

    async def ingest(
        self,
        session,
        *,
        owner_user_id: str,
        connection_id,
        project_id,
        search_urls: list[str],
        max_pages: int,
        max_items: int,
        device_id: str | None = None,
    ) -> list[dict[str, Any]]:
        actor = {"id": owner_user_id, "actor_type": "system"}
        discovered: list[CrowdWorksListing] = []
        seen: set[str] = set()
        for search_url in search_urls:
            remaining = max_items - len(discovered)
            if remaining <= 0:
                break
            items = await self.fetch_pages(
                owner_user_id=owner_user_id,
                search_url=search_url,
                max_pages=max_pages,
                max_items=remaining,
                device_id=device_id,
            )
            for item in items:
                if item.remote_id not in seen:
                    seen.add(item.remote_id)
                    discovered.append(item)

        created: list[dict[str, Any]] = []
        for item in discovered:
            existing = await session.scalar(
                select(EngagementOpportunity).where(
                    EngagementOpportunity.connection_id == connection_id,
                    EngagementOpportunity.source_url == item.url,
                ).limit(1)
            )
            if existing is not None:
                continue
            snapshot = item.snapshot()
            created.append(
                await self.operations.create_opportunity(
                    session,
                    actor,
                    title=item.title,
                    source_url=item.url,
                    source_text=item.context,
                    source_snapshot=snapshot,
                    connection_id=connection_id,
                    project_id=project_id,
                    metadata={
                        "provider": "crowdworks",
                        "remote_id": item.remote_id,
                        "compensation_yen": item.compensation_yen,
                        "compensation_type": item.compensation_type,
                        "workflow_stage": "INGESTED",
                    },
                )
            )
        return created


__all__ = ["CrowdWorksListing", "CrowdWorksSource", "extract_listings"]
