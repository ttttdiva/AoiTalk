"""Shared CrowdWorks scan notification reservation and delivery ledger."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ...task_time import DEFAULT_TASK_TIMEZONE

logger = logging.getLogger(__name__)

_CROWDWORKS_PRECONDITION_SKIP_CODES = frozenset({
    "skipped_browser_unavailable",
    "skipped_browser_login_required",
})
CROWDWORKS_SCAN_NOTIFICATION_REASON_CODES = _CROWDWORKS_PRECONDITION_SKIP_CODES | {
    "crowdworks_unit_failure_limit_reached",
    "crowdworks_agent_scan_failed",
}
# Retain historical reason codes for legacy ledger reconciliation, but never
# reserve or deliver new user alerts for technical scan failures.
_CROWDWORKS_TECHNICAL_FAILURE_CODES = frozenset({
    "crowdworks_unit_failure_limit_reached",
    "crowdworks_agent_scan_failed",
})
_CROWDWORKS_NOTIFICATION_DETAIL_CODES = frozenset({
    "browser_backend_unavailable",
    "browser_login_required",
    "browser_owner_mismatch",
    "browser_unavailable",
    "broker_unavailable",
    "cdp_disconnected",
    "browser_restart_limit",
    "gateway_root_missing",
    "broker_mode_legacy",
    "broker_mode_unknown",
    "broker_start_timeout",
    "broker_executable_missing",
    "broker_start_failed",
    "crowdworks_browser_session_unavailable",
    "bridge_select_a_pc_first",
    "bridge_device_not_found",
    "bridge_device_offline",
    "bridge_disconnected",
    "bridge_browser_command_unavailable",
    "bridge_jev_runtime_required",
})


@dataclass
class ScanNotificationResult:
    notification_sent_at: str | None = None
    notifications_by_reason: dict[str, dict[str, str | None]] = field(default_factory=dict)
    last_notification_code: str | None = None
    last_notification_attempted_at: str | None = None
    last_notification_sent_at: str | None = None
    next_notification_sendable_at: str | None = None
    last_notification_delivery_code: str | None = None


class CrowdWorksScanNotifications:
    @staticmethod
    def _scan_state(connection) -> dict[str, Any]:
        metadata = connection.metadata_json if isinstance(connection.metadata_json, dict) else {}
        state = metadata.get("crowdworks_scan_state")
        return dict(state) if isinstance(state, dict) else {}

    async def _persist_scan_notification(
        self,
        session,
        *,
        connection,
        reason_code: str,
        count: int,
        unit_kind: str | None = None,
        detail_code: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        if (not isinstance(reason_code, str) or reason_code not in CROWDWORKS_SCAN_NOTIFICATION_REASON_CODES
                or reason_code in _CROWDWORKS_TECHNICAL_FAILURE_CODES):
            return False
        try:
            from ..task_management.notifications import persist_crowdworks_scan_notification

            now = now or datetime.now(timezone.utc)
            response = await persist_crowdworks_scan_notification(
                session,
                owner_user_id=str(connection.owner_user_id),
                connection_id=connection.id,
                dedupe_key=(
                    f"crowdworks:{connection.id}:{reason_code}:{int(now.timestamp()) // 3600}"
                ),
                reason_code=reason_code,
                count=(
                    max(0, min(count, 1_000_000))
                    if type(count) is int
                    else 0
                ),
                unit_kind=(
                    unit_kind
                    if isinstance(unit_kind, str)
                    and unit_kind in {
                        "listing", "detail", "evaluation", "draft", "contract_monitor", "tick"
                    }
                    else None
                ),
                scheduled_for=None,
                detail_code=(
                    detail_code
                    if detail_code in _CROWDWORKS_NOTIFICATION_DETAIL_CODES
                    else None
                ),
            )
        except Exception as exc:
            logger.error("CrowdWorks scan notification failed: exception_class=%s", type(exc).__name__)
            return False
        return response is True

    @staticmethod
    def _parse_scan_notification_time(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _notification_delivery_time_utc(
        value: datetime,
        *,
        attempt_time: datetime | None,
        now: datetime,
    ) -> datetime:
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc)
        utc_candidate = value.replace(tzinfo=timezone.utc)
        task_local_candidate = value.replace(
            tzinfo=ZoneInfo(DEFAULT_TASK_TIMEZONE)
        ).astimezone(timezone.utc)
        anchor = attempt_time or now
        return min(
            (utc_candidate, task_local_candidate),
            key=lambda candidate: abs((candidate - anchor).total_seconds()),
        )

    @staticmethod
    def _normalize_legacy_notification_event_time(
        value: Any,
        *,
        now: datetime,
        prior_event: datetime | None = None,
    ) -> datetime | None:
        parsed = CrowdWorksScanNotifications._parse_scan_notification_time(value)
        if parsed is None:
            return None
        looks_like_future_local_time = (
            parsed > prior_event + timedelta(hours=1)
            if prior_event is not None
            else parsed > now + timedelta(hours=1)
        )
        if not looks_like_future_local_time:
            return parsed
        try:
            local_wall_time = parsed.replace(tzinfo=None).replace(
                tzinfo=ZoneInfo(DEFAULT_TASK_TIMEZONE)
            )
            normalized = local_wall_time.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return parsed
        if prior_event is not None:
            if prior_event <= normalized <= prior_event + timedelta(hours=1):
                return normalized
        elif normalized <= now + timedelta(minutes=5):
            return normalized
        return parsed

    def _scan_notification_records(
        self,
        state: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, dict[str, str | None]]:
        raw_records = state.get("notifications_by_reason")
        raw_records = raw_records if isinstance(raw_records, dict) else {}
        legacy_attempts = state.get("last_alert_attempt_at")
        legacy_attempts = legacy_attempts if isinstance(legacy_attempts, dict) else {}
        legacy_sends = state.get("last_alert_sent_at")
        legacy_sends = legacy_sends if isinstance(legacy_sends, dict) else {}
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        records: dict[str, dict[str, str | None]] = {}
        for reason_code in sorted(CROWDWORKS_SCAN_NOTIFICATION_REASON_CODES):
            raw_record = raw_records.get(reason_code)
            raw_record = raw_record if isinstance(raw_record, dict) else {}
            legacy_key = (
                f"skip:{reason_code}"
                if reason_code in _CROWDWORKS_PRECONDITION_SKIP_CODES
                else f"unit_failure:{reason_code}"
            )
            attempted_at = raw_record.get("last_attempt_at")
            sent_at = raw_record.get("last_sent_at")
            next_sendable_at = raw_record.get("next_sendable_at")
            record_delivery_code = raw_record.get("delivery_code")
            if not isinstance(attempted_at, str):
                attempted_at = legacy_attempts.get(legacy_key)
            if not isinstance(sent_at, str):
                sent_at = legacy_sends.get(legacy_key)
            parsed_attempt = self._normalize_legacy_notification_event_time(
                attempted_at, now=now
            )
            parsed_sent = self._normalize_legacy_notification_event_time(
                sent_at, now=now, prior_event=parsed_attempt
            )
            if parsed_attempt is None and parsed_sent is not None:
                parsed_attempt = parsed_sent
                attempted_at = sent_at
            anchor = max(
                (value for value in (parsed_attempt, parsed_sent) if value is not None),
                default=None,
            )
            parsed_next = anchor + timedelta(hours=1) if anchor is not None else None
            if not isinstance(record_delivery_code, str):
                record_delivery_code = None
            if parsed_attempt is None and parsed_sent is None:
                continue
            records[reason_code] = {
                "reason_code": reason_code,
                "last_attempt_at": parsed_attempt.isoformat() if parsed_attempt else None,
                "last_sent_at": parsed_sent.isoformat() if parsed_sent else None,
                "next_sendable_at": parsed_next.isoformat() if parsed_next else None,
                "delivery_code": record_delivery_code,
            }
        return records

    def _sync_scan_notification_summary(
        self,
        connection,
        result: Any,
        *,
        preferred_reason_code: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, str | None]]:
        state = state if isinstance(state, dict) else self._scan_state(connection)
        records = self._scan_notification_records(state)
        result.notifications_by_reason = {
            reason: dict(record) for reason, record in records.items()
        }
        selected = records.get(preferred_reason_code or "")
        if selected is None and records:
            selected = max(
                records.values(),
                key=lambda record: self._parse_scan_notification_time(
                    record.get("last_attempt_at")
                ) or datetime.min.replace(tzinfo=timezone.utc),
            )
        if selected is None:
            result.last_notification_code = None
            result.last_notification_attempted_at = None
            result.last_notification_sent_at = None
            result.next_notification_sendable_at = None
            result.last_notification_delivery_code = None
        else:
            result.last_notification_code = selected.get("reason_code")
            result.last_notification_attempted_at = selected.get("last_attempt_at")
            result.last_notification_sent_at = selected.get("last_sent_at")
            result.next_notification_sendable_at = selected.get("next_sendable_at")
            result.last_notification_delivery_code = selected.get("delivery_code")
        return records

    async def _reconcile_legacy_notification_delivery(
        self,
        session,
        *,
        connection,
        state: dict[str, Any],
        reason_code: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Migrate old alert timestamps and reconcile one exact in-app dedupe key."""
        raw_records = state.get("notifications_by_reason")
        raw_records = raw_records if isinstance(raw_records, dict) else {}
        legacy_attempts = state.get("last_alert_attempt_at")
        legacy_attempts = legacy_attempts if isinstance(legacy_attempts, dict) else {}
        records = self._scan_notification_records(state, now=now)
        reasons = (
            (reason_code,)
            if reason_code in CROWDWORKS_SCAN_NOTIFICATION_REASON_CODES
            else tuple(sorted(CROWDWORKS_SCAN_NOTIFICATION_REASON_CODES))
        )
        changed = False
        for current_reason in reasons:
            # New records are written atomically by the outcome helper. This
            # lookup only repairs legacy attempt-only state.
            if current_reason in raw_records:
                continue
            legacy_key = (
                f"skip:{current_reason}"
                if current_reason in _CROWDWORKS_PRECONDITION_SKIP_CODES
                else f"unit_failure:{current_reason}"
            )
            attempt_at = legacy_attempts.get(legacy_key)
            attempt_time = self._normalize_legacy_notification_event_time(
                attempt_at,
                now=now or datetime.now(timezone.utc),
            )
            if attempt_time is None:
                continue
            record = records.get(current_reason)
            if record is None:
                record = {
                    "reason_code": current_reason,
                    "last_attempt_at": attempt_time.isoformat(),
                    "last_sent_at": None,
                    "next_sendable_at": None,
                    "delivery_code": None,
                }
            if record.get("last_sent_at") is not None or record.get("delivery_code") is not None:
                records[current_reason] = record
                continue
            next_sendable = self._parse_scan_notification_time(
                record.get("next_sendable_at")
            ) or (attempt_time + timedelta(hours=1))
            delivered_at = None
            if callable(getattr(session, "scalar", None)):
                from ...memory.models.notifications import NotificationDelivery

                dedupe_hour = int(attempt_time.timestamp()) // 3600
                legacy_dedupe_key = (
                    f"crowdworks_scan:{connection.id}:crowdworks:{connection.id}:"
                    f"{current_reason}:{dedupe_hour}"
                )
                delivered_at = await session.scalar(
                    select(NotificationDelivery.delivered_at).where(
                        NotificationDelivery.dedupe_key == legacy_dedupe_key,
                        NotificationDelivery.user_id == connection.owner_user_id,
                        NotificationDelivery.channel == "in_app",
                        NotificationDelivery.notification_type == "system_alert",
                        NotificationDelivery.status == "delivered",
                        NotificationDelivery.delivered_at.is_not(None),
                    ).limit(1)
                )
            if isinstance(delivered_at, datetime):
                delivered_at = self._notification_delivery_time_utc(
                    delivered_at,
                    attempt_time=attempt_time,
                    now=now or datetime.now(timezone.utc),
                )
                record["last_sent_at"] = delivered_at.isoformat()
                next_sendable = max(
                    attempt_time,
                    delivered_at,
                ) + timedelta(hours=1)
            record["reason_code"] = current_reason
            record["last_attempt_at"] = attempt_time.isoformat()
            record["next_sendable_at"] = next_sendable.isoformat()
            records[current_reason] = record
            changed = True
        if changed:
            state["notifications_by_reason"] = records
        return changed

    async def _begin_scan_notification_attempt(
        self,
        session,
        *,
        connection,
        reason_code: str,
        now: datetime | None = None,
    ) -> bool:
        if (reason_code not in CROWDWORKS_SCAN_NOTIFICATION_REASON_CODES
                or reason_code in _CROWDWORKS_TECHNICAL_FAILURE_CODES):
            return False
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        state = self._scan_state(connection)
        reconciled = await self._reconcile_legacy_notification_delivery(
            session,
            connection=connection,
            state=state,
            reason_code=reason_code,
            now=now,
        )
        raw_records = state.get("notifications_by_reason")
        raw_records = raw_records if isinstance(raw_records, dict) else {}
        records = self._scan_notification_records(state, now=now)
        record = records.get(reason_code, {
            "reason_code": reason_code,
            "last_attempt_at": None,
            "last_sent_at": None,
            "next_sendable_at": None,
            "delivery_code": None,
        })
        previous_sendable = self._parse_scan_notification_time(
            record.get("next_sendable_at")
        )
        raw_record = raw_records.get(reason_code)
        migrated = reconciled or not isinstance(raw_record, dict) or any(
            raw_record.get(key) != record.get(key)
            for key in (
                "reason_code", "last_attempt_at", "last_sent_at",
                "next_sendable_at", "delivery_code",
            )
        )
        if previous_sendable is not None and now < previous_sendable:
            if migrated:
                state["notifications_by_reason"] = records
                metadata = dict(connection.metadata_json or {})
                metadata["crowdworks_scan_state"] = state
                connection.metadata_json = metadata
                await session.commit()
            return False
        record = {
            **record,
            "reason_code": reason_code,
            "last_attempt_at": now.isoformat(),
            "next_sendable_at": (now + timedelta(hours=1)).isoformat(),
        }
        records[reason_code] = record
        state["notifications_by_reason"] = records
        metadata = dict(connection.metadata_json or {})
        metadata["crowdworks_scan_state"] = state
        connection.metadata_json = metadata
        await session.commit()
        return True

    async def _record_scan_notification_outcome(
        self,
        session,
        *,
        connection,
        result: Any,
        reason_code: str,
        sent: bool,
        now: datetime | None = None,
        update_last_scan: bool = True,
    ) -> None:
        if reason_code not in CROWDWORKS_SCAN_NOTIFICATION_REASON_CODES:
            return
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        state = self._scan_state(connection)
        records = self._scan_notification_records(state, now=now)
        record = records.get(reason_code, {
            "reason_code": reason_code,
            "last_attempt_at": now.isoformat(),
            "last_sent_at": None,
            "next_sendable_at": (now + timedelta(hours=1)).isoformat(),
            "delivery_code": None,
        })
        record = {
            **record,
            "reason_code": reason_code,
            "last_attempt_at": record.get("last_attempt_at") or now.isoformat(),
            "next_sendable_at": record.get("next_sendable_at")
            or (now + timedelta(hours=1)).isoformat(),
            "delivery_code": None if sent else "crowdworks_notification_delivery_failed",
        }
        if sent:
            record["last_sent_at"] = now.isoformat()
            record["next_sendable_at"] = (
                max(
                    now,
                    self._parse_scan_notification_time(record.get("last_attempt_at"))
                    or now,
                )
                + timedelta(hours=1)
            ).isoformat()
            result.notification_sent_at = now.isoformat()
        records[reason_code] = record
        state["notifications_by_reason"] = records
        metadata = dict(connection.metadata_json or {})
        metadata["crowdworks_scan_state"] = state
        connection.metadata_json = metadata
        self._sync_scan_notification_summary(
            connection,
            result,
            preferred_reason_code=reason_code,
            state=state,
        )
        if update_last_scan:
            state["last_scan"] = result.to_dict()
        metadata["crowdworks_scan_state"] = state
        connection.metadata_json = metadata
        await session.commit()
