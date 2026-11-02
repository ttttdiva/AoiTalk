"""CrowdWorks Agentを正規APIで確認・作成し、必要ならChatを実行する。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4


DISPLAY_NAME = "CrowdWorks 担当"
AGENT_SLUG = "crowdworks-chat-agent"
AGENT_IDEMPOTENCY_KEY = "crowdworks-chat-agent-provision-v1"
REVISION_IDEMPOTENCY_KEY = "crowdworks-chat-agent-revision-v10"
CHAT_ROUTE = {"provider": "openai", "model": "gpt-5.6-luna", "effort": "high"}
# CROWDWORKS_AGENT_ROUTE=provider|model[|effort] switches the Agent to another
# route when the default pool is exhausted; each route is its own revision.
_ROUTE_OVERRIDE = os.environ.get("CROWDWORKS_AGENT_ROUTE", "").strip()
if _ROUTE_OVERRIDE:
    _provider, _model, *_effort = _ROUTE_OVERRIDE.split("|")
    CHAT_ROUTE = {"provider": _provider, "model": _model}
    if _effort and _effort[0]:
        CHAT_ROUTE["effort"] = _effort[0]
CAPABILITY_CEILING = ["external_action_propose"]
MODEL_OVERRIDES = {
    "chat": {"routes": [{"kind": "llm", **CHAT_ROUTE}], "allow_paid_fallback": False},
}
CHAT_CHARACTER_FALLBACK = "aoi"
CHAT_WAIT_SECONDS = 20 * 60
CHAT_POLL_SECONDS = 2
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PROTECTED_LEGACY_SLUGS = frozenset({"crowdworks-marketplace"})
SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

MISSION = (
    "CrowdWorks の新着案件から、AI で完結でき、応募者プロフィールの事実で応募条件を満たし、採算が合う案件を選んで、承認待ちの応募案を作る。外部への送信はしない。"
)

OPERATIONAL_INSTRUCTIONS = """1. 依頼に案件の URL か ID があれば、その案件だけを扱う。無ければ crowdworks_list_jobs を 1 回呼び、crowdworks_candidates で候補を得る。
2. 一番の条件は「AI で完結するか」。納品物がファイルや文章で、AI が作れる案件（文章作成、調査とまとめ、データ整理、Web サイト制作、プログラム開発、画像・デザイン制作）だけを選ぶ。crowdworks_candidates は 1 回の依頼で 1 回だけ呼ぶ。題名と報酬から見込みのある順に、最大 4 件を crowdworks_read_job で読む。同じ案件を 2 回読まない。
3. 次の案件は選ばない: 本人が稼働・出演・電話・対面・オンライン面談・営業・テレアポをするもの、アンケートやモニターや体験談の回答者を募るもの、資格者の監修、年齢・性別・居住地などの属性限定、成果報酬だけのもの、期限が過ぎたか未確認のもの。
4. crowdworks_profile のプロフィールを読み、案件の応募条件をプロフィールの事実で満たせるか確かめる。満たせなければ選ばない。
5. 工数（時間）・売上・コスト（円）を見積もる。工数は AI で作業する前提の実時間（AI の生成時間と、人が確認・修正する時間の合計）で、人が手作業でやる場合の時間ではない。売上は案件に書かれた報酬を超えない。見積りを基準に合わせて調整してはいけない。利益が 5,000 円以上、かつ 1 時間あたり 2,500 円以上のものだけ提案する。届かない案件は提出せず次へ進む。
6. 応募文は、依頼内容に直接答え、プロフィールに書かれた事実だけを使う。資格・職歴・年齢・居住地・実績件数は書かない。確認したい点は疑問文にして clarification_questions にだけ入れる。message には質問を書かない（道具が末尾に「確認したい点」として 1 回ずつ付ける）。価格と納期は案件の条件と矛盾させない。
7. crowdworks_submit_proposal で提出する。duplicate_question で拒否されたら、message から質問を消して 1 回だけ出し直す。それ以外で拒否されたら、その案件はやめて次へ進む。
8. 1 回の依頼で作る承認待ちは最大 3 件。4 件読んで条件に合う案件が無ければ、そこで止めて理由を返す。最後に、作った承認待ち（action_id、題名、提示額）と、選ばなかった主な案件と理由を短く返す。承認と送信はしない。利用者には「Operations の承認画面で確認してください。承認すると CrowdWorks へ外部送信されます」と伝える。"""

REVISION_PAYLOAD: dict[str, Any] = {
    "display_name": DISPLAY_NAME,
    "mission": MISSION,
    "operational_instructions": OPERATIONAL_INSTRUCTIONS,
    "capability_ceiling": CAPABILITY_CEILING,
    "model_overrides": MODEL_OVERRIDES,
    "tool_ids": ["crowdworks"],
    "delegate_policy": {"agents": [], "workers": []},
}


def _canonical_digest(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _revision_idempotency_key(
    source_revision_id: str | None,
    desired_payload: dict[str, Any] | None = None,
) -> str:
    """Bind a revision retry key to its source revision and full desired content."""

    content = REVISION_PAYLOAD if desired_payload is None else desired_payload
    source_identity = source_revision_id if source_revision_id is not None else "initial"
    return (
        f"{REVISION_IDEMPOTENCY_KEY}-"
        f"{_canonical_digest({'source_revision_id': source_identity})}-"
        f"{_canonical_digest(content)}"
    )


def _revision_request_payload(source_revision_id: str | None) -> dict[str, Any]:
    payload = dict(REVISION_PAYLOAD)
    payload["idempotency_key"] = _revision_idempotency_key(source_revision_id, REVISION_PAYLOAD)
    return payload


class ProvisionError(Exception):
    """固定コードだけを表に出す安全な処理エラー。"""

    def __init__(self, code: str, *, stage: str, http_status: int | None = None):
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.http_status = http_status

    def public_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "success": False,
            "reason_code": self.code,
            "stage": self.stage,
        }
        if self.http_status is not None:
            result["http_status"] = self.http_status
        return result


def _mapping(value: Any, *, stage: str, code: str = "invalid_response") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProvisionError(code, stage=stage)
    return value


def _envelope(value: dict[str, Any], key: str, *, stage: str) -> dict[str, Any]:
    nested = value.get(key)
    return _mapping(nested if nested is not None else value, stage=stage)


def _safe_id(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", value):
        return None
    return value


async def _request_json(
    client: Any,
    method: str,
    path: str,
    *,
    expected_statuses: set[int],
    stage: str,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonical authenticated HTTP clientを通じてbounded JSONを取得する。"""

    kwargs: dict[str, Any] = {}
    if params is not None:
        kwargs["params"] = params
    if payload is not None:
        kwargs["json"] = payload
    status: int | None = None
    try:
        async with client.http.stream(method, path, **kwargs) as response:
            status = int(response.status_code)
            if status not in expected_statuses:
                raise ProvisionError("http_request_failed", stage=stage, http_status=status)
            body = bytearray()
            async for part in response.aiter_bytes():
                body.extend(part)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ProvisionError("response_too_large", stage=stage)
    except ProvisionError:
        raise
    except Exception as exc:
        error_code = getattr(exc, "code", None)
        http_status = getattr(exc, "http_status", None)
        if not isinstance(error_code, str) or SAFE_CODE_RE.fullmatch(error_code) is None:
            error_code = "request_failed"
        raise ProvisionError(
            error_code,
            stage=stage,
            http_status=http_status if isinstance(http_status, int) else status,
        ) from None
    try:
        result = json.loads(bytes(body)) if body else {}
    except (ValueError, UnicodeError):
        raise ProvisionError("invalid_response", stage=stage, http_status=status) from None
    return _mapping(result, stage=stage)


async def _canonical_session(
    callback: Callable[[Any], Awaitable[dict[str, Any]]],
) -> Any:
    """既存AoiTalk Pluginのcanonical session認証だけを利用する。"""

    module = _load_plugin_tasks_module()
    return await module._session(callback)


def _load_plugin_tasks_module() -> Any:
    plugin_root = Path(__file__).resolve().parents[1] / "plugins" / "AoiTalk_Plugin"
    plugin_root_text = str(plugin_root)
    if plugin_root_text not in sys.path:
        sys.path.insert(0, plugin_root_text)
    from aoitalk_plugin import aoitalk_tasks

    return aoitalk_tasks


async def _list_agents(client: Any) -> list[dict[str, Any]]:
    envelope = await _request_json(
        client,
        "GET",
        "/api/agents",
        expected_statuses={200},
        stage="agent_list",
        params={"limit": 200},
    )
    agents = envelope.get("agents")
    total = envelope.get("total")
    if (
        not isinstance(agents, list)
        or not isinstance(total, int)
        or len(agents) != total
        or len(agents) >= 200
    ):
        raise ProvisionError("agent_list_incomplete", stage="agent_list")
    return [_mapping(item, stage="agent_list") for item in agents]


def _target_agent(agents: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [
        item
        for item in agents
        if item.get("slug") == AGENT_SLUG or item.get("display_name") == DISPLAY_NAME
    ]
    ids = {_safe_id(item.get("id")) for item in matches}
    ids.discard(None)
    if len(ids) > 1:
        raise ProvisionError("agent_identity_ambiguous", stage="agent_lookup")
    if not ids:
        return None
    agent_id = next(iter(ids))
    match = next((item for item in matches if _safe_id(item.get("id")) == agent_id), None)
    if match is None:
        raise ProvisionError("invalid_agent_identity", stage="agent_lookup")
    if match.get("slug") in PROTECTED_LEGACY_SLUGS:
        raise ProvisionError("legacy_crowdworks_agent_protected", stage="agent_lookup")
    return match


async def _list_revisions(client: Any, agent_id: str) -> list[dict[str, Any]]:
    envelope = await _request_json(
        client,
        "GET",
        f"/api/agents/{agent_id}/revisions",
        expected_statuses={200},
        stage="revision_list",
        params={"limit": 200},
    )
    revisions = envelope.get("revisions")
    total = envelope.get("total")
    if (
        not isinstance(revisions, list)
        or not isinstance(total, int)
        or len(revisions) != total
        or len(revisions) >= 200
    ):
        raise ProvisionError("revision_list_incomplete", stage="revision_list")
    result = [_mapping(item, stage="revision_list") for item in revisions]
    return sorted(result, key=lambda item: int(item.get("version") or 0), reverse=True)


def _revision_matches(revision: dict[str, Any]) -> bool:
    return all(revision.get(key) == value for key, value in REVISION_PAYLOAD.items())


async def _inspect(client: Any) -> dict[str, Any]:
    agents = await _list_agents(client)
    agent = _target_agent(agents)
    revisions: list[dict[str, Any]] = []
    if agent is not None:
        agent_id = _safe_id(agent.get("id"))
        if agent_id is None:
            raise ProvisionError("invalid_agent_identity", stage="agent_lookup")
        revisions = await _list_revisions(client, agent_id)
    latest = revisions[0] if revisions else None
    exact = next((item for item in revisions if _revision_matches(item)), None)
    latest_matches = bool(latest is not None and _revision_matches(latest))
    state = str(agent.get("state") or "") if agent is not None else None
    blocked_reason = "agent_retired" if state == "retired" else None
    return {
        "agent": (
            {
                "id": _safe_id(agent.get("id")),
                "slug": agent.get("slug"),
                "display_name": agent.get("display_name"),
                "state": state,
            }
            if agent is not None
            else None
        ),
        "latest_revision": (
            {
                "id": _safe_id(latest.get("id")),
                "version": latest.get("version"),
                "matches_desired": latest_matches,
            }
            if latest is not None
            else None
        ),
        "desired_revision_exists": exact is not None,
        "desired_revision_is_latest": latest_matches,
        "blocked_reason": blocked_reason,
        "capability_ceiling": list(REVISION_PAYLOAD["capability_ceiling"]),
        "capability_ceiling_added": (
            [item for item in REVISION_PAYLOAD["capability_ceiling"]
             if latest is None or item not in (latest.get("capability_ceiling") or [])]
            if blocked_reason is None else []
        ),
        "planned_writes": (
            []
            if blocked_reason is not None
            or (agent is not None and latest_matches and state == "active")
            else [
                *(["POST /api/agents"] if agent is None else []),
                *(["POST /api/agents/{id}/revisions"] if not latest_matches else []),
                *(["PATCH /api/agents/{id}/state"] if state != "active" else []),
            ]
        ),
        "desired_revision": {
            "mission": MISSION,
            "operational_instructions": OPERATIONAL_INSTRUCTIONS,
            "tool_ids": ["crowdworks"],
            "capability_ceiling": CAPABILITY_CEILING,
            "delegate_policy": {"agents": [], "workers": []},
            "model_overrides": MODEL_OVERRIDES,
        },
    }


async def _ensure_agent_and_revision(client: Any) -> dict[str, Any]:
    inspection = await _inspect(client)
    if inspection.get("blocked_reason"):
        raise ProvisionError(
            str(inspection["blocked_reason"]),
            stage="agent_inspection",
        )
    agent = inspection.get("agent")
    created_agent = False
    if agent is None:
        envelope = await _request_json(
            client,
            "POST",
            "/api/agents",
            expected_statuses={200, 201},
            stage="agent_create",
            payload={
                "display_name": DISPLAY_NAME,
                "slug": AGENT_SLUG,
                "idempotency_key": AGENT_IDEMPOTENCY_KEY,
            },
        )
        created = _envelope(envelope, "agent", stage="agent_create")
        agent_id = _safe_id(created.get("id"))
        if agent_id is None:
            raise ProvisionError("agent_create_unverified", stage="agent_create")
        agent = {
            "id": agent_id,
            "slug": created.get("slug"),
            "display_name": created.get("display_name"),
            "state": created.get("state"),
        }
        created_agent = True

    agent_id = _safe_id(agent.get("id"))
    if agent_id is None:
        raise ProvisionError("invalid_agent_identity", stage="agent_create")

    revisions = await _list_revisions(client, agent_id)
    latest = revisions[0] if revisions else None
    latest_matches = bool(latest is not None and _revision_matches(latest))
    created_revision = False
    source_revision_id = _safe_id(latest.get("id")) if latest is not None else None
    if latest is not None and source_revision_id is None:
        raise ProvisionError("invalid_revision_identity", stage="revision_create")
    if not latest_matches:
        envelope = await _request_json(
            client,
            "POST",
            f"/api/agents/{agent_id}/revisions",
            expected_statuses={200, 201},
            stage="revision_create",
            payload=_revision_request_payload(source_revision_id),
        )
        revision = _envelope(envelope, "revision", stage="revision_create")
        revision_id = _safe_id(revision.get("id"))
        if revision_id is None or not _revision_matches(revision):
            raise ProvisionError("revision_create_unverified", stage="revision_create")
        created_revision = True
    else:
        revision_id = _safe_id(latest.get("id"))
        if revision_id is None:
            raise ProvisionError("invalid_revision_identity", stage="revision_create")

    current_state = str(agent.get("state") or "draft")
    activated = current_state != "active"
    if activated:
        state_envelope = await _request_json(
            client,
            "PATCH",
            f"/api/agents/{agent_id}/state",
            expected_statuses={200},
            stage="agent_activate",
            payload={"state": "active", "expected_state": current_state},
        )
        state_agent = _envelope(state_envelope, "agent", stage="agent_activate")
        if str(state_agent.get("state") or "") != "active":
            raise ProvisionError("agent_activation_unverified", stage="agent_activate")

    return {
        "agent_id": agent_id,
        "revision_id": revision_id,
        "state": "active",
        "created_agent": created_agent,
        "created_revision": created_revision,
        "activated": activated,
        "capability_ceiling": CAPABILITY_CEILING,
        "capability_ceiling_added": (
            [item for item in REVISION_PAYLOAD["capability_ceiling"]
             if latest is None or item not in (latest.get("capability_ceiling") or [])]
            if created_revision else []
        ),
    }


def _character_name(data: dict[str, Any]) -> str:
    current = data.get("current")
    if not isinstance(current, str) or not current.strip():
        raise ProvisionError("chat_character_unavailable", stage="chat_character")
    options = data.get("character_options")
    if not isinstance(options, list):
        options = data.get("characters")
    if not isinstance(options, list):
        raise ProvisionError("chat_character_unavailable", stage="chat_character")
    matches = []
    for item in options:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        name = item.get("name")
        if slug == current:
            return slug
        if name == current and isinstance(slug, str):
            matches.append(slug)
    if len(matches) == 1:
        return matches[0]
    if current.casefold() == CHAT_CHARACTER_FALLBACK and any(
        isinstance(item, dict) and item.get("slug") == CHAT_CHARACTER_FALLBACK
        for item in options
    ):
        return CHAT_CHARACTER_FALLBACK
    raise ProvisionError("chat_character_unavailable", stage="chat_character")


def _tool_result_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    if not isinstance(value, dict):
        return {}
    nested = value.get("result")
    if isinstance(nested, dict):
        value = nested
    result: dict[str, Any] = {}
    for key in ("accepted", "reason_code"):
        if key == "accepted" and isinstance(value.get(key), bool):
            result[key] = value[key]
        elif (
            key == "reason_code"
            and isinstance(value.get(key), str)
            and SAFE_CODE_RE.fullmatch(value[key]) is not None
        ):
            result[key] = value[key]
    action_id = _safe_id(value.get("action_id"))
    if action_id is not None:
        result["action_id"] = action_id
    return result


def _tool_calls(run: dict[str, Any] | None) -> list[dict[str, Any]]:
    calls = run.get("tool_calls") if isinstance(run, dict) else None
    if not isinstance(calls, list):
        return []
    result = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("tool_name") or call.get("name")
        if not isinstance(name, str) or not name.startswith("crowdworks_"):
            continue
        item: dict[str, Any] = {"name": name}
        item.update(_tool_result_summary(call.get("result")))
        result.append(item)
    return result


def _provider_failure_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if re.search(r"(?i)(?:error\s+code\s*:\s*402|['\"]code['\"]\s*:\s*402)", value) and re.search(
        r"(?i)(credits|can only afford|budget|usage limit)", value
    ):
        return "provider_budget_insufficient"
    if re.search(r"(?i)(?:error\s+code\s*:\s*429|['\"]code['\"]\s*:\s*429)", value):
        return "provider_rate_limited"
    return None


def _safe_display_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(
        r"(?i)(https?://[^\s?#]*/(?:keys?|tokens?)/)[^\s/?#]+",
        r"\1[redacted]",
        value,
    )
    text = re.sub(
        r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)=)[^&#\s]+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"(?i)\bsk-or-v1-[A-Za-z0-9_-]{8,}", "[redacted]", text)
    text = re.sub(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)\s*[:=]\s*[^,\s]+",
        "[redacted]",
        text,
    )
    return text


def _failed_assistant_message() -> str:
    return "応答生成に失敗しました。承認待ちの応募案は作成されていません。"


async def _work_failure_code(client: Any, work_item_id: str | None) -> str | None:
    if work_item_id is None:
        return None
    try:
        envelope = await _request_json(
            client,
            "GET",
            f"/api/agent-work/{work_item_id}",
            expected_statuses={200},
            stage="chat_work_diagnostic",
        )
    except ProvisionError:
        return None
    item = _envelope(envelope, "item", stage="chat_work_diagnostic")
    for key in ("safe_error_code", "blocker_code"):
        value = item.get(key)
        if isinstance(value, str) and SAFE_CODE_RE.fullmatch(value):
            return value
    events = item.get("events")
    if isinstance(events, list):
        for event in reversed(events):
            if not isinstance(event, dict):
                continue
            for key in ("safe_error_code", "error_code"):
                value = event.get(key)
                if isinstance(value, str) and SAFE_CODE_RE.fullmatch(value):
                    return value
    return None


async def _action_summaries(client: Any, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for call in calls:
        action_id = call.get("action_id")
        if call.get("accepted") is not True or not isinstance(action_id, str):
            continue
        action_title = None
        offered_price = None
        action_status = "proposed"
        try:
            action_envelope = await _request_json(
                client,
                "GET",
                f"/api/operations/actions/{action_id}",
                expected_statuses={200},
                stage="action_summary",
            )
            action = _envelope(action_envelope, "action", stage="action_summary")
            if isinstance(action.get("status"), str):
                action_status = action["status"]
            payload = action.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            offered_price = payload.get("offered_price")
            opportunity_id = _safe_id(
                action.get("opportunity_id") or payload.get("opportunity_id")
            )
            if opportunity_id:
                opportunity_envelope = await _request_json(
                    client,
                    "GET",
                    f"/api/operations/opportunities/{opportunity_id}",
                    expected_statuses={200},
                    stage="opportunity_summary",
                    params={"include_versions": False},
                )
                opportunity = _envelope(
                    opportunity_envelope,
                    "opportunity",
                    stage="opportunity_summary",
                )
                title = opportunity.get("title")
                if isinstance(title, str):
                    action_title = _safe_display_text(title)[:1000]
        except ProvisionError:
            # The proposed Action ID remains authoritative; details are optional.
            pass
        summaries.append(
            {
                "action_id": action_id,
                "title": action_title,
                "offered_price_yen": offered_price,
                "status": action_status,
            }
        )
    return summaries


async def _chat(client: Any, agent_id: str, request_text: str) -> dict[str, Any]:
    if not request_text.strip():
        raise ProvisionError("chat_request_empty", stage="chat_arguments")
    agents = await _list_agents(client)
    agent = next((item for item in agents if _safe_id(item.get("id")) == agent_id), None)
    if agent is None or agent.get("state") != "active":
        raise ProvisionError("agent_not_active", stage="chat_agent")
    revisions = await _list_revisions(client, agent_id)
    latest = revisions[0] if revisions else None
    if latest is None or not _revision_matches(latest):
        raise ProvisionError("agent_revision_not_ready", stage="chat_agent")
    revision_id = _safe_id(latest.get("id"))
    if revision_id is None:
        raise ProvisionError("invalid_revision_identity", stage="chat_agent")

    character_data = await _request_json(
        client,
        "GET",
        "/api/characters",
        expected_statuses={200},
        stage="chat_character",
    )
    character_name = _character_name(character_data)
    main_route = dict(CHAT_ROUTE)
    session_envelope = await _request_json(
        client,
        "POST",
        "/api/conversations",
        expected_statuses={200, 201},
        stage="chat_session_create",
        payload={
            "character_name": character_name,
            "agent_id": agent_id,
            "main_route": main_route,
        },
    )
    session_data = _envelope(session_envelope, "session", stage="chat_session_create")
    session_id = _safe_id(session_data.get("id"))
    if session_id is None or _safe_id(session_data.get("agent_id")) != agent_id:
        raise ProvisionError("chat_agent_binding_unverified", stage="chat_session_create")

    client_message_id = str(uuid4())
    message_envelope = await _request_json(
        client,
        "POST",
        f"/api/conversations/{session_id}/messages",
        expected_statuses={200, 201},
        stage="chat_message_create",
        payload={
            "role": "user",
            "content": request_text,
            "client_message_id": client_message_id,
        },
    )
    message_data = _envelope(message_envelope, "message", stage="chat_message_create")
    user_message_id = _safe_id(message_data.get("id"))
    if user_message_id is None:
        raise ProvisionError("chat_message_unverified", stage="chat_message_create")

    dispatch_envelope = await _request_json(
        client,
        "POST",
        f"/api/conversations/{session_id}/dispatch",
        expected_statuses={200, 202},
        stage="chat_dispatch",
        payload={
            "message": request_text,
            "expected_agent_id": agent_id,
            "client_message_id": client_message_id,
            "skip_user_persistence": True,
            "persisted_user_message_id": user_message_id,
        },
    )
    agent_run_id = _safe_id(dispatch_envelope.get("agent_run_id"))
    if agent_run_id is None:
        raise ProvisionError("chat_dispatch_unverified", stage="chat_dispatch")

    deadline = time.monotonic() + CHAT_WAIT_SECONDS
    last_run: dict[str, Any] | None = None
    generation: dict[str, Any] = {}
    last_progress = time.monotonic()
    last_poll_error: str | None = None
    while time.monotonic() < deadline:
        try:
            generation_envelope = await asyncio.wait_for(
                _request_json(
                    client,
                    "GET",
                    f"/api/conversations/{session_id}/generation/status",
                    expected_statuses={200},
                    stage="chat_generation_status",
                ),
                timeout=max(0.1, deadline - time.monotonic()),
            )
            generation = generation_envelope
            run_envelope = await asyncio.wait_for(
                _request_json(
                    client,
                    "GET",
                    f"/api/agent-runs/{agent_run_id}",
                    expected_statuses={200},
                    stage="chat_agent_run",
                    params={
                        "include_events": False,
                        "include_tool_calls": True,
                        "include_timeline": False,
                    },
                ),
                timeout=max(0.1, deadline - time.monotonic()),
            )
            run = _envelope(run_envelope, "agent_run", stage="chat_agent_run")
            last_run = run
            last_poll_error = None
        except asyncio.TimeoutError:
            last_poll_error = "chat_poll_timeout"
            break
        except ProvisionError as exc:
            retryable = exc.http_status is None or exc.http_status in {404, 429, 500, 502, 503, 504}
            if not retryable:
                raise
            last_poll_error = exc.code
            await asyncio.sleep(min(CHAT_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
            continue
        run_status = str(run.get("status") or "").casefold()
        running = generation.get("running") is True
        if not running and run_status in {
            "succeeded",
            "completed",
            "failed",
            "cancelled",
            "interrupted",
            "blocked",
        }:
            break
        now = time.monotonic()
        if now - last_progress >= 30:
            print(
                json.dumps(
                    {
                        "progress": "chat_waiting",
                        "elapsed_seconds": int(now - (deadline - CHAT_WAIT_SECONDS)),
                        "generation_status": str(generation.get("status") or "unknown"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            last_progress = now
        await asyncio.sleep(min(CHAT_POLL_SECONDS, max(0.0, deadline - time.monotonic())))

    timed_out = time.monotonic() >= deadline
    messages_envelope = await _request_json(
        client,
        "GET",
        f"/api/conversations/{session_id}/messages",
        expected_statuses={200},
        stage="chat_messages",
        params={"limit": 200},
    )
    messages = messages_envelope.get("messages")
    assistant_text = ""
    if isinstance(last_run, dict):
        run_result = last_run.get("result")
        if isinstance(run_result, dict):
            content = run_result.get("assistant_response")
            if isinstance(content, str):
                assistant_text = content.strip()
    if not assistant_text and isinstance(messages, list):
        assistant_messages = [
            item
            for item in messages
            if isinstance(item, dict)
            and item.get("role") == "assistant"
            and isinstance(item.get("content"), str)
            and item["content"].strip()
        ]
        assistant_messages.sort(
            key=lambda item: str(item.get("created_at") or item.get("updated_at") or "")
        )
        if assistant_messages:
            assistant_text = assistant_messages[-1]["content"].strip()

    tool_calls = _tool_calls(last_run)
    run_status = str((last_run or {}).get("status") or "unknown").casefold()
    work_item_id = _safe_id((last_run or {}).get("work_item_id"))
    run_result = (last_run or {}).get("result")
    run_result = run_result if isinstance(run_result, dict) else {}
    failure_reason_code = None
    if timed_out:
        failure_reason_code = "chat_wait_timeout"
    elif run_status != "succeeded":
        raw_code = (last_run or {}).get("error")
        if not isinstance(raw_code, str) or SAFE_CODE_RE.fullmatch(raw_code) is None:
            raw_code = run_result.get("safe_error_code")
        failure_reason_code = (
            raw_code
            if isinstance(raw_code, str) and SAFE_CODE_RE.fullmatch(raw_code)
            else await _work_failure_code(client, work_item_id)
        ) or "run_failed"
        failure_reason_code = _provider_failure_code(assistant_text) or failure_reason_code
        assistant_text = _failed_assistant_message()
    else:
        assistant_text = _safe_display_text(assistant_text)
    return {
        "conversation_id": session_id,
        "agent_id": agent_id,
        "agent_revision_id": revision_id,
        "chat_main_route": main_route,
        "agent_run_id": agent_run_id,
        "work_item_id": work_item_id,
        "run_status": run_status,
        "timed_out": timed_out,
        "last_poll_error": last_poll_error,
        "failure_reason_code": failure_reason_code,
        "success": not timed_out and run_status == "succeeded",
        "assistant_response": assistant_text[:2000],
        "tool_calls": tool_calls,
        "proposed_actions": await _action_summaries(client, tool_calls),
    }


async def run_helper(
    *,
    apply: bool = False,
    chat: str | None = None,
    session_runner: Callable[[Callable[[Any], Awaitable[dict[str, Any]]]], Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """読み取り・API作成・Chatを認証済みセッションで実行する。"""

    runner = session_runner or _canonical_session
    async def callback(client: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if apply:
            result["provision"] = await _ensure_agent_and_revision(client)
            agent_id = result["provision"]["agent_id"]
        else:
            inspection = await _inspect(client)
            result["inspection"] = inspection
            agent = inspection.get("agent")
            agent_id = _safe_id(agent.get("id")) if isinstance(agent, dict) else None
        if chat is not None:
            if agent_id is None:
                raise ProvisionError("agent_not_found", stage="chat_agent")
            result["chat"] = await _chat(client, agent_id, chat)
        if not apply and chat is None:
            result["mode"] = "read_only"
        else:
            result["mode"] = "apply" if apply else "chat"
        result["success"] = (
            result["chat"].get("success") is True
            if isinstance(result.get("chat"), dict)
            else True
        )
        return result

    safe_result = await runner(callback)
    return _mapping(safe_result, stage="session_result")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Agent・revisionをAPIで作成しactiveにする")
    parser.add_argument("--chat", help="作成済みAgentに送るChat依頼文")
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    try:
        result = await run_helper(apply=args.apply, chat=args.chat)
    except ProvisionError as exc:
        print(json.dumps(exc.public_result(), ensure_ascii=False))
        return 2
    except Exception as exc:
        code = getattr(exc, "code", None)
        status = getattr(exc, "http_status", None)
        safe_code = code if isinstance(code, str) and SAFE_CODE_RE.fullmatch(code) else "operation_failed"
        result: dict[str, Any] = {"success": False, "reason_code": safe_code}
        if isinstance(status, int):
            result["http_status"] = status
        print(json.dumps(result, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") is True else 2


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main_async(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
