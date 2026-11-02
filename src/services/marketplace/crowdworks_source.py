"""Fast CrowdWorks list ingestion from one Edge DOM observation per page."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import Text, and_, case, cast, false, func, or_, select

from ...memory.models.operations import (
    ApplicationDraft,
    EngagementOpportunity,
    ExternalAction,
    OpportunityEvaluation,
)
from ..operations_service import OperationsService
from .bridge_inspection import BridgeInspectionLease, require_crowdworks_job_url, require_crowdworks_url
from .crowdworks_deadline import crowdworks_deadline
from .crowdworks_brief import MINIMUM_CROWDWORKS_DETAIL_CHARS, crowdworks_brief_status
from .crowdworks_browser_provider import BrowserBridgeProvider, BrowserBridgeResolution, resolve_browser_bridge


_JOB_PATH = re.compile(r"^/public/jobs/([0-9]{1,32})(?:/|$)")
_MONEY_NUMBER = r"[0-9][0-9,]*(?:\.[0-9]+)?"
_MONEY_UNIT = r"万円|千円|円"
_MONEY = re.compile(
    rf"(?P<amount>{_MONEY_NUMBER})\s*(?P<unit>{_MONEY_UNIT})"
)
_MONEY_RANGE = re.compile(
    rf"(?P<lower>{_MONEY_NUMBER})(?:\s*(?P<lower_unit>{_MONEY_UNIT}))?"
    rf"\s*(?:〜|～|~|\-|−|―|–|から)\s*"
    rf"(?P<upper>{_MONEY_NUMBER})\s*(?P<upper_unit>{_MONEY_UNIT})"
)
_COMPENSATION_UNIT_RATE = re.compile(
    r"文字単価|文字あたり|1文字(?:につき|あたり)|"
    r"(?:円|千円|万円)\s*[／/]\s*(?:文字|字)|文字単位|"
    r"(?:1項目|1行|1記事|1本|1件).{0,24}(?:につき|あたり|単価)"
)
_HOURLY_COMPENSATION = re.compile(
    r"時給|時間単価|(?:円|千円|万円)\s*[／/]\s*(?:時|時間)"
)
_MIN_PLAUSIBLE_REWARD_DEFAULT = 1_000.0
_DETAIL_INSPECTION_ERRORS = frozenset({
    "crowdworks_detail_url_invalid", "crowdworks_detail_navigation_failed",
    "crowdworks_detail_url_unverified", "crowdworks_detail_inspection_incomplete",
    "crowdworks_detail_text_invalid", "crowdworks_detail_text_overflow",
    "crowdworks_detail_brief_empty", "crowdworks_detail_capture_failed",
    "crowdworks_inspection_incomplete", "crowdworks_inspection_invalid",
    "crowdworks_scope_overflow", "crowdworks_scope_unsupported",
    "crowdworks_browser_session_unavailable", "crowdworks_operation_lock_required",
    "crowdworks_browser_busy", "crowdworks_run_release_unverified",
    "edge_broker_browser_disconnected", "edge_broker_operation_outcome_unknown",
    "agent_browser_session_faulted", "agent_browser_operation_outcome_unknown",
})
_DETAIL_BRIDGE_ERROR_CODES = frozenset({
    "bridge_already_connected", "bridge_authenticated_user_required",
    "bridge_browser_backend_invalid", "bridge_browser_commands_invalid",
    "bridge_browser_command_unavailable", "bridge_capability_unavailable",
    "bridge_command_failed", "bridge_connection_changed",
    "bridge_dedicated_disabled", "bridge_device_not_found",
    "bridge_device_offline", "bridge_disconnected", "bridge_invalid_response",
    "bridge_jev_authority_revoked", "bridge_jev_event_overlap",
    "bridge_jev_runtime_required", "bridge_select_a_pc_first",
    "bridge_saved_action_contract_mismatch", "bridge_saved_action_unavailable",
    "bridge_screenshot_unavailable", "browser_unavailable",
})
_DETAIL_TEXT_MAX_BYTES = 131_072
_EXTENSION_TEXT_HARD_LIMIT = 10_000
_DETAIL_RETRY_ATTEMPTS_PER_OPPORTUNITY = 3
_DETAIL_FRESH_WINDOW = timedelta(days=14)  # 取り込みから14日を超えた案件は募集が終わっていることが多いので、詳細取得を後回しにする。
DETAIL_CANDIDATE_REASON_CODES = frozenset({
    "detail_already_complete",
    "detail_candidate_available",
    "detail_existing_child_record",
    "detail_retry_limit_reached",
    "detail_retry_service_excluded",
    "detail_source_url_missing",
    "detail_source_unsupported",
    "detail_unit_stopped",
})


@dataclass(frozen=True)
class CrowdWorksListing:
    remote_id: str
    title: str
    url: str
    context: str
    compensation_text: str | None = None
    compensation_yen: float | None = None
    compensation_type: str | None = None
    compensation_parse_code: str | None = None
    deadline_at: str | None = None
    deadline_status: str = "unverified"
    detail_status: str = "not_attempted"
    detail_text: str | None = None
    detail_error_code: str | None = None
    retry_opportunity_id: str | None = None
    retry_attempt: int | None = None

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
            "compensation_parse_code": self.compensation_parse_code,
            "deadline_at": self.deadline_at,
            "deadline_status": self.deadline_status,
            "detail_capture_status": self.detail_status,
            "detail_text_chars": len(" ".join(self.detail_text.split())) if isinstance(self.detail_text, str) else 0,
            "detail_text_sha256": (
                hashlib.sha256(" ".join(self.detail_text.split()).encode("utf-8")).hexdigest()
                if isinstance(self.detail_text, str) else None
            ),
        }


def _min_plausible_reward(config: Any = None) -> float:
    from .config_utils import config_get

    value = config_get(
        config,
        "crowdworks.estimate_sanity.min_plausible_reward",
        _MIN_PLAUSIBLE_REWARD_DEFAULT,
    )
    if type(value) not in (int, float) or not math.isfinite(float(value)) or value <= 0:
        return _MIN_PLAUSIBLE_REWARD_DEFAULT
    return float(value)


def _yen_value(number: str, unit: str) -> float:
    amount = float(number.replace(",", ""))
    multiplier = 10_000 if unit == "万円" else 1_000 if unit == "千円" else 1
    return amount * multiplier


def _compensation_evidence(scope: str, start: int, end: int, *, unit_rate: bool) -> str:
    evidence_end = min(len(scope), end + 32) if unit_rate else end
    return " ".join(scope[start:evidence_end].split())[:120]


def parse_compensation(
    context: str,
    *,
    min_plausible_reward: float = _MIN_PLAUSIBLE_REWARD_DEFAULT,
) -> tuple[str | None, float | None, str | None, str]:
    """Parse a total fixed reward or hourly rate from one CrowdWorks card.

    Per-unit rates do not establish a total project ceiling.  They are kept as
    evidence and treated as an unknown total so the estimate-sanity rule, not a
    misleading per-unit cap, decides whether the estimate can proceed.
    """
    text = " ".join(str(context or "").split())
    marker = re.search(r"(?:時間単価(?:制)?|固定報酬(?:制)?)", text)
    scope_start = marker.end() if marker else 0
    scope = text[scope_start:]
    compensation_type = (
        "hourly"
        if (marker and "時間単価" in marker.group(0)) or _HOURLY_COMPENSATION.search(text)
        else "fixed"
        if marker and "固定報酬" in marker.group(0)
        else None
    )
    is_unit_rate = bool(_COMPENSATION_UNIT_RATE.search(scope))

    range_match = _MONEY_RANGE.search(scope)
    if range_match:
        amount = _yen_value(range_match.group("upper"), range_match.group("upper_unit"))
        evidence = _compensation_evidence(
            scope, range_match.start(), range_match.end(), unit_rate=is_unit_rate
        )
        if is_unit_rate:
            return evidence, None, compensation_type, "reward_total_unknown_unit_rate"
        if compensation_type != "hourly" and amount < min_plausible_reward:
            return evidence, None, compensation_type, "reward_implausible_treated_unknown"
        return evidence, amount, compensation_type, "reward_parsed"

    money = _MONEY.search(scope)
    if money is None:
        return None, None, compensation_type, "reward_parse_unavailable_treated_unknown"

    amount = _yen_value(money.group("amount"), money.group("unit"))
    trailing = scope[money.end():money.end() + 16]
    preceding = scope[max(0, money.start() - 4):money.start()]
    evidence = _compensation_evidence(
        scope, money.start(), money.end(), unit_rate=is_unit_rate
    )
    if is_unit_rate:
        return evidence, None, compensation_type, "reward_total_unknown_unit_rate"
    if re.search(r"(?:以上|より上|超)", trailing) or re.search(
        r"(?:〜|～|~|\-|−|―|–|から)\s*$", trailing
    ):
        return evidence, None, compensation_type, "reward_total_unknown_open_range"
    # An amount followed by 以下/まで is an upper bound.  税込/税抜 only
    # qualifies the nominal amount and does not change the parsed yen value.
    if re.search(r"以下|まで|未満", trailing) or re.search(r"(?:〜|～|~|\-|−|―|–)\s*$", preceding):
        if compensation_type != "hourly" and amount < min_plausible_reward:
            return evidence, None, compensation_type, "reward_implausible_treated_unknown"
        return evidence, amount, compensation_type, "reward_parsed"
    if compensation_type != "hourly" and amount < min_plausible_reward:
        return evidence, None, compensation_type, "reward_implausible_treated_unknown"
    return evidence, amount, compensation_type, "reward_parsed"


def _page_url(url: str, page: int) -> str:
    parsed = urlsplit(url)
    items = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "page"]
    if page > 1:
        items.append(("page", str(page)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(items), ""))


def extract_listings(
    observation: dict[str, Any], *, base_url: str, captured_at: datetime | None = None,
    now: datetime | None = None,
    min_plausible_reward: float = _MIN_PLAUSIBLE_REWARD_DEFAULT,
) -> list[CrowdWorksListing]:
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
        # Titles can themselves contain prices (e.g. "5,000円/60分").  Prefer
        # the first amount *after* the compensation-type marker so the listing
        # budget is not confused with a price mentioned in the title.
        compensation_text, compensation, kind, parse_code = parse_compensation(
            context,
            min_plausible_reward=min_plausible_reward,
        )
        deadline_at, deadline_status = crowdworks_deadline(
            source_snapshot={"context": context}, captured_at=captured_at, now=now
        )
        result.append(
            CrowdWorksListing(
                remote_id=remote_id,
                title=item["title"][:500],
                url=item["url"],
                context=context,
                compensation_text=compensation_text,
                compensation_yen=compensation,
                compensation_type=kind,
                compensation_parse_code=parse_code,
                deadline_at=deadline_at,
                deadline_status=deadline_status,
            )
        )
    return result


class CrowdWorksSource:
    def __init__(self, *, operations: OperationsService | None = None, config: Any = None):
        self.operations = operations or OperationsService()
        self.config = config
        self.last_detail_summary = self._empty_detail_summary()
        self.last_scan_summary = self._empty_scan_summary()

    @staticmethod
    def _empty_detail_summary() -> dict[str, Any]:
        return {
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "deferred": 0,
            "last_error_code": None,
            "failure_code_counts": {},
            "candidate_reason_code_counts": {},
            "candidate_rows_loaded": 0,
            "candidate_eligible_count": 0,
        }

    @staticmethod
    def _empty_scan_summary() -> dict[str, Any]:
        return {
            "listing_count": 0,
            "created_count": 0,
            "detail_retry_count": 0,
            "gate_counts": {
                "deadline_unverified": 0,
                "expired": 0,
                "requirement_short": 0,
            },
        }

    async def _bridge(self, owner_user_id: str, device_id: str | None = None) -> BrowserBridgeResolution:
        return await resolve_browser_bridge(
            owner_user_id,
            config=self.config,
            device_id=device_id,
        )

    async def _resolve_reusable_tab(
        self,
        bridge: BrowserBridgeProvider,
        url: str,
        tab_id: int | None = None,
        *,
        backend: str | None = None,
    ) -> dict[str, Any]:
        if backend == "local":
            # LocalBrowser selects its first target by origin; validate the
            # CrowdWorks URL before allowing resolve_tab to open/select it.
            require_crowdworks_url(url)
            if tab_id is None:
                target = await bridge.request("resolve_tab", url=url)
                require_crowdworks_url(target.get("url") if isinstance(target, dict) else None)
                selected_tab = target.get("tab_id") if isinstance(target, dict) else None
                if type(selected_tab) is not int:
                    raise RuntimeError("crowdworks_tab_resolution_failed")
                return target
            target = await bridge.request("resolve_tab", tab_id=tab_id)
            if not isinstance(target, dict):
                raise RuntimeError("crowdworks_tab_resolution_failed")
            require_crowdworks_url(target.get("url"))
            if type(target.get("tab_id")) is not int or target["tab_id"] != tab_id:
                raise RuntimeError("crowdworks_selected_target_changed")
            return target
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
        captured_at: datetime | None = None,
        existing_source_urls: set[str] | None = None,
    ) -> list[CrowdWorksListing]:
        """Read listing cards only; detail pages are separate durable units."""
        if type(max_pages) is not int or not 1 <= max_pages <= 20:
            raise ValueError("crowdworks_listing_page_limit_invalid")
        if type(max_items) is not int or not 1 <= max_items <= 500:
            raise ValueError("crowdworks_listing_item_limit_invalid")

        resolution = await self._bridge(owner_user_id, device_id)
        if not resolution.ready or resolution.bridge is None:
            raise RuntimeError(resolution.reason_code or "browser_backend_unavailable")
        raw_bridge = resolution.bridge
        existing_urls = set(existing_source_urls or ())
        result: list[CrowdWorksListing] = []
        seen: set[str] = set()
        fresh_count = 0
        captured_at = captured_at or datetime.now(timezone.utc)

        async with BridgeInspectionLease(raw_bridge) as bridge:
            tab_id = None
            for page in range(1, max_pages + 1):
                url = _page_url(search_url, page)
                target = await self._resolve_reusable_tab(
                    bridge,
                    url,
                    tab_id=tab_id,
                    backend=resolution.backend,
                )
                tab_id = target["tab_id"]
                if (resolution.backend == "local" or not bridge.helper_backend) and target.get("url") != url:
                    target = await bridge.request("navigate", tab_id=tab_id, url=url)
                    if (not isinstance(target, dict) or target.get("ok") is not True
                            or target.get("executed") is not True or target.get("error")
                            or target.get("tab_id", tab_id) != tab_id):
                        raise RuntimeError("crowdworks_navigation_failed")
                    tab_id = target.get("tab_id", tab_id)
                observation = await bridge.request("crowdworks_inspect", tab_id=tab_id)
                page_items = extract_listings(
                    observation,
                    base_url=url,
                    captured_at=captured_at,
                    min_plausible_reward=_min_plausible_reward(self.config),
                )
                if not page_items:
                    break
                for item in page_items:
                    if item.remote_id in seen:
                        continue
                    seen.add(item.remote_id)
                    result.append(item)
                    if item.url not in existing_urls:
                        fresh_count += 1
                    if fresh_count >= max_items:
                        break
                if fresh_count >= max_items:
                    break

        self.last_scan_summary["listing_count"] = len(result)
        return result

    async def _fetch_detail(
        self,
        bridge: BrowserBridgeProvider,
        tab_id: int,
        listing: CrowdWorksListing,
        *,
        captured_at: datetime | None,
    ) -> CrowdWorksListing:
        url = require_crowdworks_job_url(listing.url, listing.remote_id)
        navigation = await bridge.request("navigate", tab_id=tab_id, url=url)
        if (not isinstance(navigation, dict) or navigation.get("ok") is not True
                or navigation.get("executed") is not True or navigation.get("error")
                or navigation.get("tab_id", tab_id) != tab_id):
            raise RuntimeError("crowdworks_detail_navigation_failed")
        verification = await bridge.request("verify", tab_id=tab_id, expected_url=url)
        if not isinstance(verification, dict) or verification.get("matched") is not True:
            raise RuntimeError("crowdworks_detail_url_unverified")
        observation = await bridge.request("crowdworks_inspect", tab_id=tab_id)
        detail_text = self._detail_observation_text(observation, bridge=bridge)
        # Check the exact canonical path again so a navigation during capture
        # cannot associate another listing's body with this opportunity.
        verification = await bridge.request("verify", tab_id=tab_id, expected_url=url)
        if not isinstance(verification, dict) or verification.get("matched") is not True:
            raise RuntimeError("crowdworks_detail_url_unverified")
        deadline_at, deadline_status = crowdworks_deadline(
            source_snapshot={"deadline_text": detail_text, "context": listing.context},
            source_text=detail_text,
            captured_at=captured_at,
        )
        return replace(
            listing,
            deadline_at=deadline_at,
            deadline_status=deadline_status,
            detail_status="complete",
            detail_text=detail_text,
            detail_error_code=None,
        )

    async def ingest_listings(
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
    ) -> dict[str, Any]:
        """Persist newly listed jobs without opening any job detail page."""

        if type(max_pages) is not int or not 1 <= max_pages <= 20:
            raise ValueError("crowdworks_listing_page_limit_invalid")
        if type(max_items) is not int or not 1 <= max_items <= 500:
            raise ValueError("crowdworks_listing_item_limit_invalid")
        if not isinstance(search_urls, list) or not search_urls:
            raise ValueError("crowdworks_search_urls_invalid")

        self.last_detail_summary = self._empty_detail_summary()
        self.last_scan_summary = self._empty_scan_summary()
        actor = {"id": owner_user_id, "actor_type": "system"}
        owner_uuid = UUID(str(owner_user_id))
        existing_urls = {
            str(value) for value in (await session.scalars(
                select(EngagementOpportunity.source_url).where(
                    EngagementOpportunity.owner_user_id == owner_uuid,
                    EngagementOpportunity.connection_id == connection_id,
                )
            )).all()
            if isinstance(value, str) and value
        }
        seen_urls: set[str] = set()
        created_count = 0
        captured_at = datetime.now(timezone.utc)

        for search_url in search_urls:
            remaining = max_items - created_count
            if remaining <= 0:
                break
            listings = await self.fetch_pages(
                owner_user_id=owner_user_id,
                search_url=search_url,
                max_pages=max_pages,
                max_items=remaining,
                device_id=device_id,
                captured_at=captured_at,
                existing_source_urls=existing_urls | seen_urls,
            )
            for item in listings:
                seen_urls.add(item.url)
                if item.url in existing_urls or created_count >= max_items:
                    continue
                pending = replace(
                    item,
                    deadline_at=None,
                    deadline_status="unverified",
                    detail_status="pending",
                    detail_text=None,
                    detail_error_code=None,
                )
                snapshot = pending.snapshot()
                brief_status = crowdworks_brief_status(snapshot, pending.context)
                metadata = {
                    "provider": "crowdworks",
                    "remote_id": pending.remote_id,
                    "compensation_yen": pending.compensation_yen,
                    "compensation_type": pending.compensation_type,
                    "compensation_parse_code": pending.compensation_parse_code,
                    "deadline_at": None,
                    "deadline_status": "unverified",
                    "detail_capture_status": "pending",
                    "brief_evidence_status": brief_status,
                    "workflow_stage": "DETAIL_PENDING",
                    "screening_status": "deadline_unverified",
                    "screening_reason": "詳細取得と応募期限の確認が完了するまで評価を保留しています",
                }
                await self.operations.create_opportunity(
                    session,
                    actor,
                    title=pending.title,
                    source_url=pending.url,
                    source_text=pending.context,
                    source_snapshot=snapshot,
                    connection_id=connection_id,
                    project_id=project_id,
                    metadata=metadata,
                )
                existing_urls.add(pending.url)
                created_count += 1
                self._record_gate(pending)

        self.last_scan_summary["listing_count"] = min(max_items, len(seen_urls))
        self.last_scan_summary["created_count"] = created_count
        return {
            "listing_count": self.last_scan_summary["listing_count"],
            "created_count": created_count,
            "gate_counts": dict(self.last_scan_summary["gate_counts"]),
            "error_code": None,
        }

    async def fetch_one_pending_detail(
        self,
        session,
        *,
        owner_user_id: str,
        connection_id,
        opportunity_id: str | UUID | None = None,
        device_id: str | None = None,
        min_expected_profit: float = 5000.0,
        min_profit_per_hour: float = 2500.0,
        before_fetch: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Read and persist exactly one eligible incomplete detail page."""

        summary: dict[str, Any] = {
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "deferred": 0,
            "attempt": None,
            "opportunity_id": None,
            "error_code": None,
        }
        self.last_detail_summary = self._empty_detail_summary()
        actor = {"id": owner_user_id, "actor_type": "system"}
        owner_uuid = UUID(str(owner_user_id))
        # created_at は timestamp without time zone で UTC の naive 値を保存しているため。
        fresh_cutoff = (datetime.now(timezone.utc) - _DETAIL_FRESH_WINDOW).replace(tzinfo=None)
        is_fresh = EngagementOpportunity.created_at >= fresh_cutoff

        snapshot_status = EngagementOpportunity.source_snapshot_json["detail_capture_status"].as_string()
        snapshot_deadline = EngagementOpportunity.source_snapshot_json["deadline_status"].as_string()
        metadata_deadline = EngagementOpportunity.metadata_json["deadline_status"].as_string()
        snapshot_chars = EngagementOpportunity.source_snapshot_json["detail_text_chars"].as_integer()
        detail_retry_attempts = EngagementOpportunity.metadata_json["detail_retry_attempts"].as_integer()
        detail_unit_stopped = (
            EngagementOpportunity.metadata_json["crowdworks_unit_failures"]["detail"]["stopped"].as_boolean()
        )
        has_evaluation = select(OpportunityEvaluation.id).where(
            OpportunityEvaluation.opportunity_id == EngagementOpportunity.id,
        ).exists()
        has_draft = select(ApplicationDraft.id).where(
            ApplicationDraft.opportunity_id == EngagementOpportunity.id,
        ).exists()
        has_action = select(ExternalAction.id).where(
            ExternalAction.opportunity_id == EngagementOpportunity.id,
        ).exists()
        incomplete_detail = or_(
            func.coalesce(snapshot_status, "") != "complete",
            func.coalesce(snapshot_deadline, metadata_deadline, "") == "unverified",
            func.coalesce(snapshot_chars, 0) < MINIMUM_CROWDWORKS_DETAIL_CHARS,
            func.length(func.coalesce(EngagementOpportunity.source_text, "")) < MINIMUM_CROWDWORKS_DETAIL_CHARS,
        )
        metadata = EngagementOpportunity.metadata_json
        listed_yen = case(
            (
                metadata["compensation_parse_code"].as_string() == "reward_parsed",
                metadata["compensation_yen"].as_float(),
            ),
            else_=None,
        )
        listed_type = metadata["compensation_type"].as_string()
        listed_hourly_below_rate = func.coalesce(
            and_(listed_type == "hourly", listed_yen < float(min_profit_per_hour)),
            false(),
        )
        listed_fixed_yen = case((listed_type == "fixed", listed_yen), else_=None)
        screening_status = metadata["screening_status"].as_string()
        expected_profit = metadata["expected_profit"].as_float()
        expected_profit_per_hour = metadata["expected_profit_per_hour"].as_float()
        sanity_excluded = func.coalesce(
            or_(
                metadata["crowdworks_estimate_sanity_code"].as_string()
                == "estimate_unreliable_reward_unknown",
                metadata["crowdworks_estimate_rate_sanity_code"].as_string()
                == "estimate_unreliable_rate_too_high",
                screening_status.in_(
                    (
                        "estimate_unreliable_reward_unknown",
                        "estimate_unreliable_rate_too_high",
                    )
                ),
            ),
            false(),
        )
        # Evaluated rows usually carry neither deadline_status nor
        # application_blocking_requirements in metadata: the deadline gate is
        # derived from deadline_at, and blocking requirements move the row to
        # requirements_incomplete. Rows reach this query only while their
        # detail is incomplete, so a missing key means "no blockers".
        no_blocking_requirements = (
            func.coalesce(
                cast(metadata["application_blocking_requirements"], Text), "[]"
            )
            == "[]"
        )
        draft_ready_detail_candidate = and_(
            has_evaluation,
            metadata["workflow_stage"].as_string() == "EVALUATED",
            screening_status.in_(
                ("qualified", "qualified_waiting_rank", "application_preparing")
            ),
            expected_profit >= float(min_expected_profit),
            expected_profit_per_hour >= float(min_profit_per_hour),
            no_blocking_requirements,
            ~sanity_excluded,
        )
        fresh_draft_ready_detail_candidate = and_(
            draft_ready_detail_candidate,
            is_fresh,
        )
        draft_ready_priority = case((fresh_draft_ready_detail_candidate, 1), else_=0)
        draft_ready_profit_per_hour = case(
            (fresh_draft_ready_detail_candidate, expected_profit_per_hour),
            else_=None,
        )
        detail_reason = case(
            (EngagementOpportunity.source_url.is_(None), "detail_source_url_missing"),
            (
                func.coalesce(detail_retry_attempts, 0)
                >= _DETAIL_RETRY_ATTEMPTS_PER_OPPORTUNITY,
                "detail_retry_limit_reached",
            ),
            (detail_unit_stopped.is_(True), "detail_unit_stopped"),
            (~incomplete_detail, "detail_already_complete"),
            (
                ~EngagementOpportunity.source_url.like(
                    "https://crowdworks.jp/public/jobs/%"
                ),
                "detail_source_unsupported",
            ),
            (has_draft | has_action, "detail_existing_child_record"),
            else_="detail_candidate_available",
        )

        async def record_candidate_reason_counts() -> None:
            execute = getattr(session, "execute", None)
            if not callable(execute):
                return
            reason_rows = await execute(
                select(detail_reason, func.count(EngagementOpportunity.id))
                .where(
                    EngagementOpportunity.owner_user_id == owner_uuid,
                    EngagementOpportunity.connection_id == connection_id,
                    EngagementOpportunity.status == "open",
                )
                .group_by(detail_reason)
            )
            counts: dict[str, int] = {}
            for code, amount in reason_rows.all():
                if code in DETAIL_CANDIDATE_REASON_CODES and type(amount) is int and amount > 0:
                    counts[code] = min(1_000_000, amount)
            self.last_detail_summary["candidate_reason_code_counts"] = counts

        candidate_query = select(EngagementOpportunity).where(
            EngagementOpportunity.owner_user_id == owner_uuid,
            EngagementOpportunity.connection_id == connection_id,
        )
        if opportunity_id is not None:
            candidate_query = (
                candidate_query
                .where(
                    EngagementOpportunity.id == UUID(str(opportunity_id)),
                    EngagementOpportunity.source_url.is_not(None),
                    ~has_draft,
                    ~has_action,
                )
                .limit(1)
            )
        else:
            candidate_query = (
                candidate_query
                .where(
                    EngagementOpportunity.status == "open",
                    EngagementOpportunity.source_url.like("https://crowdworks.jp/public/jobs/%"),
                    func.coalesce(detail_retry_attempts, 0) < _DETAIL_RETRY_ATTEMPTS_PER_OPPORTUNITY,
                    detail_unit_stopped.is_not(True),
                    incomplete_detail,
                    ~has_draft,
                    ~has_action,
                )
                .order_by(
                    draft_ready_priority.desc(),
                    draft_ready_profit_per_hour.desc().nullslast(),
                    case((is_fresh, 1), else_=0).desc(),
                    case((and_(is_fresh, listed_hourly_below_rate), 1), else_=0).asc(),  # 新着の中で、時給が基準未満の案件は後ろ。
                    case((is_fresh, listed_fixed_yen), else_=None).desc().nullslast(),  # 新着の中で、固定報酬の高い順。
                    EngagementOpportunity.created_at.desc(),
                    EngagementOpportunity.id.asc(),
                )
                .limit(100)
            )
        rows = (await session.scalars(candidate_query)).all()
        rows = [row for row in rows if isinstance(getattr(row, "source_url", None), str)]
        self.last_detail_summary["candidate_rows_loaded"] = len(rows)
        if not rows:
            await record_candidate_reason_counts()
            return summary

        list_retry = getattr(self.operations, "list_crowdworks_detail_retry_candidates", None)
        begin_retry = getattr(self.operations, "begin_crowdworks_detail_retry", None)
        finish_retry = getattr(self.operations, "finish_crowdworks_detail_retry", None)
        if not callable(list_retry) or not callable(begin_retry) or not callable(finish_retry):
            summary.update({"deferred": 1, "error_code": "crowdworks_detail_retry_unavailable"})
            self.last_detail_summary["deferred"] = 1
            return summary

        if opportunity_id is not None:
            retry_rows = {
                row.source_url: {
                    "opportunity_id": str(row.id),
                    "attempts": (
                        row.metadata_json.get("detail_retry_attempts", 0)
                        if isinstance(row.metadata_json, dict)
                        else 0
                    ),
                    "detail_priority": 0,
                }
                for row in rows
            }
        else:
            retry_rows = await list_retry(
                session,
                actor,
                connection_id=connection_id,
                source_urls=[row.source_url for row in rows],
                max_attempts=_DETAIL_RETRY_ATTEMPTS_PER_OPPORTUNITY,
                min_expected_profit=min_expected_profit,
                min_profit_per_hour=min_profit_per_hour,
            )
        eligible: list[tuple[int, int, Any, Mapping[str, Any]]] = []
        for index, row in enumerate(rows):
            retry_record = retry_rows.get(row.source_url) if isinstance(retry_rows, Mapping) else None
            if not isinstance(retry_record, Mapping):
                continue
            priority = retry_record.get("detail_priority", 1)
            if type(priority) is not int or priority not in {0, 1}:
                priority = 1
            eligible.append((priority, index, row, retry_record))
        if not eligible:
            await record_candidate_reason_counts()
            if rows:
                counts = dict(
                    self.last_detail_summary.get("candidate_reason_code_counts") or {}
                )
                counts["detail_retry_service_excluded"] = min(1_000_000, len(rows))
                self.last_detail_summary["candidate_reason_code_counts"] = counts
            return summary
        self.last_detail_summary["candidate_eligible_count"] = len(eligible)

        _priority, _index, row, retry_record = min(eligible, key=lambda candidate: (candidate[0], candidate[1]))
        opportunity_id = str(retry_record.get("opportunity_id") or getattr(row, "id", "")) or None
        summary["opportunity_id"] = opportunity_id
        self.last_detail_summary["opportunity_id"] = opportunity_id
        snapshot = row.source_snapshot_json if isinstance(getattr(row, "source_snapshot_json", None), dict) else {}
        metadata = row.metadata_json if isinstance(getattr(row, "metadata_json", None), dict) else {}
        remote_id = snapshot.get("remote_id") or metadata.get("remote_id")
        if not isinstance(remote_id, str):
            match = _JOB_PATH.match(urlsplit(row.source_url).path)
            remote_id = match.group(1) if match else ""
        context = str(snapshot.get("context") or "")
        raw_compensation = context or str(snapshot.get("compensation_text") or "")
        compensation_text, compensation_yen, compensation_type, compensation_parse_code = (
            parse_compensation(
                raw_compensation,
                min_plausible_reward=_min_plausible_reward(self.config),
            )
        )
        listing = CrowdWorksListing(
            remote_id=remote_id,
            title=str(getattr(row, "title", "") or ""),
            url=row.source_url,
            context=context,
            compensation_text=compensation_text,
            compensation_yen=compensation_yen,
            compensation_type=compensation_type,
            compensation_parse_code=compensation_parse_code,
            deadline_at=(snapshot.get("deadline_at")
                         if isinstance(snapshot.get("deadline_at"), str) else None),
            deadline_status=str(snapshot.get("deadline_status") or metadata.get("deadline_status") or "unverified"),
            detail_status="pending",
        )

        async def finish(attempt: int, result: CrowdWorksListing) -> bool:
            return await finish_retry(
                session,
                actor,
                opportunity_id=opportunity_id,
                connection_id=connection_id,
                attempt=attempt,
                listing=result,
            )

        try:
            require_crowdworks_job_url(listing.url, listing.remote_id)
        except Exception as exc:
            attempt = await begin_retry(
                session,
                actor,
                opportunity_id=opportunity_id,
                connection_id=connection_id,
                max_attempts=_DETAIL_RETRY_ATTEMPTS_PER_OPPORTUNITY,
            )
            if attempt is None:
                summary["deferred"] = 1
                self.last_detail_summary["deferred"] = 1
                return summary
            code = self._detail_error_code(exc)
            failed = replace(
                listing,
                detail_status="failed",
                detail_error_code=code,
                retry_opportunity_id=opportunity_id,
                retry_attempt=int(attempt),
            )
            await finish(int(attempt), failed)
            summary.update({"attempted": 1, "failed": 1, "attempt": int(attempt), "error_code": code})
            self.last_detail_summary.update({
                "attempted": 1,
                "failed": 1,
                "last_error_code": code,
                "failure_code_counts": {code: 1},
            })
            self._annotate_detail_exception(exc, code, int(attempt))
            raise

        if before_fetch is not None:
            await before_fetch()

        resolution = await self._bridge(owner_user_id, device_id)
        if not resolution.ready or resolution.bridge is None:
            raise RuntimeError(resolution.reason_code or "browser_backend_unavailable")
        raw_bridge = resolution.bridge
        async with BridgeInspectionLease(raw_bridge) as bridge:
            attempt = await begin_retry(
                session,
                actor,
                opportunity_id=opportunity_id,
                connection_id=connection_id,
                max_attempts=_DETAIL_RETRY_ATTEMPTS_PER_OPPORTUNITY,
            )
            if attempt is None:
                summary["deferred"] = 1
                self.last_detail_summary["deferred"] = 1
                return summary

            summary["attempted"] = 1
            summary["attempt"] = int(attempt)
            self.last_detail_summary["attempted"] = 1
            try:
                target = await self._resolve_reusable_tab(
                    bridge,
                    listing.url,
                    backend=resolution.backend,
                )
                tab_id = target.get("tab_id") if isinstance(target, dict) else None
                if type(tab_id) is not int:
                    raise RuntimeError("crowdworks_detail_navigation_failed")
                attempted_listing = replace(
                    listing,
                    retry_opportunity_id=opportunity_id,
                    retry_attempt=int(attempt),
                )
                detailed = await self._fetch_detail(
                    bridge, tab_id, attempted_listing, captured_at=datetime.now(timezone.utc)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = self._detail_error_code(exc)
                failed = replace(
                    listing,
                    detail_status="failed",
                    detail_error_code=code,
                    retry_opportunity_id=opportunity_id,
                    retry_attempt=int(attempt),
                )
                await finish(int(attempt), failed)
                summary.update({"failed": 1, "error_code": code})
                self.last_detail_summary.update({
                    "failed": 1,
                    "last_error_code": code,
                    "failure_code_counts": {code: 1},
                })
                self._annotate_detail_exception(exc, code, int(attempt))
                raise

            applied = await finish(int(attempt), detailed)
            if applied:
                summary["completed"] = 1
                self.last_detail_summary["completed"] = 1
            else:
                summary["deferred"] = 1
                summary["error_code"] = "crowdworks_detail_retry_target_changed"
                self.last_detail_summary["deferred"] = 1
        return summary

    @staticmethod
    def _annotate_detail_exception(exc: Exception, code: str, attempt: int) -> None:
        """Attach bounded CrowdWorks context while preserving the original traceback."""

        try:
            setattr(exc, "crowdworks_error_code", code)
            setattr(exc, "crowdworks_detail_attempt", attempt)
            exc.add_note(f"crowdworks_detail_error_code={code}")
        except Exception:
            # Exception annotations are diagnostic hints; the original error
            # must still be re-raised if a custom exception forbids attributes.
            pass

    @staticmethod
    def _detail_observation_text(observation: Any, *, bridge: BrowserBridgeProvider) -> str:
        if not isinstance(observation, dict) or observation.get("origin") not in {
            "https://crowdworks.jp", "https://www.crowdworks.jp",
        }:
            raise RuntimeError("crowdworks_detail_inspection_incomplete")
        text = observation.get("text")
        if not isinstance(text, str):
            raise RuntimeError("crowdworks_detail_text_invalid")
        try:
            if len(text.encode("utf-8")) > _DETAIL_TEXT_MAX_BYTES:
                raise RuntimeError("crowdworks_detail_text_overflow")
        except UnicodeError:
            raise RuntimeError("crowdworks_detail_text_invalid") from None
        coverage = observation.get("coverage")
        if not isinstance(coverage, dict) or coverage.get("truncated") is not False or coverage.get("unscanned") is not False:
            raise RuntimeError("crowdworks_detail_inspection_incomplete")
        if (getattr(bridge, "browser_backend", "extension") == "agent_browser"
                and (coverage.get("complete") is not True or coverage.get("frame_scope") != "main"
                     or coverage.get("shadow_scope") != "none")):
            raise RuntimeError("crowdworks_detail_inspection_incomplete")
        if (getattr(bridge, "browser_backend", "extension") != "agent_browser"
                and len(text) >= _EXTENSION_TEXT_HARD_LIMIT):
            # The legacy extension projection slices body text at 10,000 chars
            # without a dedicated text-truncated bit; do not call it complete.
            raise RuntimeError("crowdworks_detail_inspection_incomplete")
        normalized = " ".join(text.split())
        if not normalized:
            raise RuntimeError("crowdworks_detail_brief_empty")
        return normalized

    @staticmethod
    def _detail_error_code(exc: Exception) -> str:
        value = getattr(exc, "code", None)
        if not isinstance(value, str):
            value = str(exc)
        allowed_codes = _DETAIL_INSPECTION_ERRORS | _DETAIL_BRIDGE_ERROR_CODES
        return value if value in allowed_codes else "crowdworks_detail_capture_failed"

    def _record_gate(self, item: CrowdWorksListing) -> None:
        if item.deadline_status == "expired":
            self.last_scan_summary["gate_counts"]["expired"] += 1
            return
        if item.deadline_status != "open":
            self.last_scan_summary["gate_counts"]["deadline_unverified"] += 1
            return
        snapshot = item.snapshot()
        if crowdworks_brief_status(snapshot, item.detail_text) != "complete":
            self.last_scan_summary["gate_counts"]["requirement_short"] += 1


__all__ = ["CrowdWorksListing", "CrowdWorksSource", "extract_listings"]
