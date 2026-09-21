"""Bounded typed planner for Browser Agent goals.

The root chat model supplies only the user goal and start URL. This planner uses
that same configured LLM to compile the goal into a bounded BrowserPlan. Page
content never enters this prompt, so web prompt injection cannot alter the plan
authority. Every generated field is revalidated by BrowserPlan and the runtime
browser/action executor before execution.

A schema-invalid planner response gets one correction attempt. The invalid raw
response is never fed back to the model or logged; only a stable validation code
is supplied, so retries cannot promote generated text into new authority.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit

from .browser_agent_models import BrowserPlan


class BrowserPlannerError(RuntimeError):
    """Stable, secret-free planner failure code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _decode_json_object(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if len(value) > 32_000:
        raise BrowserPlannerError("browser_planner_response_too_large")
    fence = chr(96) * 3
    if value.startswith(fence + "json\n") and value.endswith(fence):
        value = value[8:-3].strip()
    elif value.startswith(fence + "\n") and value.endswith(fence):
        value = value[4:-3].strip()
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        raise BrowserPlannerError("browser_planner_invalid_json") from None
    if not isinstance(decoded, dict):
        raise BrowserPlannerError("browser_planner_invalid_json")
    return decoded


def _same_navigation_url(left: str, right: str) -> bool:
    try:
        a = urlsplit(left)
        b = urlsplit(right)

        def key(value: Any) -> tuple[Any, ...]:
            port = value.port or (443 if value.scheme.lower() == "https" else 80)
            return (
                value.scheme.lower(),
                (value.hostname or "").lower(),
                port,
                value.path or "/",
                value.query,
                value.fragment,
            )

        return key(a) == key(b)
    except (TypeError, ValueError):
        return False


def _validated_plan(
    decoded: dict[str, Any],
    *,
    start_url: str,
    goal: str,
    completion_text: str,
    completion_url: str,
    max_steps: int,
) -> BrowserPlan:
    if set(decoded) - {"steps", "completion_text", "completion_url"}:
        raise BrowserPlannerError("browser_planner_invalid_shape")
    steps = decoded.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > max_steps:
        raise BrowserPlannerError("browser_planner_invalid_shape")

    normalized_steps: list[dict[str, Any]] = []
    for raw_step in steps:
        if not isinstance(raw_step, dict):
            raise BrowserPlannerError("browser_planner_invalid_shape")
        step = dict(raw_step)
        for field in ("value", "url", "expected_text", "expected_url"):
            if field in step and step[field] is None:
                step[field] = ""
        # Observations intentionally exclude form-control values so secrets
        # cannot leak into model state. Therefore an assertion such as
        # expected_text="Aoi" immediately after fill/select/check is
        # structurally unobservable and would always fail. Removing these
        # planner-supplied assertions grants no new action authority; final
        # completion and transition assertions remain server-enforced.
        if step.get("action") in {"type", "select", "check", "uncheck"}:
            step["expected_text"] = ""
            step["expected_url"] = ""
        normalized_steps.append(step)

    # The executor always opens start_url before running plan steps. A planner
    # may redundantly emit the same navigation as step 1 and attach an
    # invented postcondition to it. Dropping only exact-equivalent leading
    # start navigations removes no authority and avoids a duplicate request.
    while (
        len(normalized_steps) > 1
        and normalized_steps[0].get("action") == "navigate"
        and _same_navigation_url(
            str(normalized_steps[0].get("url") or ""),
            start_url,
        )
    ):
        normalized_steps.pop(0)
    if (
        len(normalized_steps) == 1
        and normalized_steps[0].get("action") == "navigate"
        and _same_navigation_url(
            str(normalized_steps[0].get("url") or ""),
            start_url,
        )
    ):
        normalized_steps[0] = {
            "action": "read",
            "instruction": "Read the requested page",
        }

    try:
        plan = BrowserPlan.model_validate(
            {
                "start_url": start_url,
                "goal": goal,
                "steps": normalized_steps,
                "completion_text": completion_text
                or decoded.get("completion_text", ""),
                "completion_url": completion_url or decoded.get("completion_url", ""),
            }
        )
    except Exception:
        raise BrowserPlannerError("browser_planner_invalid_plan") from None
    if len(plan.steps) > max_steps:
        raise BrowserPlannerError("browser_step_budget_exceeded")
    return plan


async def build_browser_plan(
    client: Any,
    *,
    start_url: str,
    goal: str,
    completion_text: str = "",
    completion_url: str = "",
    max_steps: int = 24,
    timeout_seconds: float = 45.0,
) -> BrowserPlan:
    """Compile one user goal into one complete typed plan."""

    method = getattr(client, "generate_plain_text_async", None)
    if not callable(method):
        raise BrowserPlannerError("browser_planner_unavailable")
    if (
        not isinstance(max_steps, int)
        or isinstance(max_steps, bool)
        or not 1 <= max_steps <= 60
    ):
        raise BrowserPlannerError("browser_planner_configuration_invalid")

    request = {
        "start_url": start_url,
        "goal": goal,
        "completion_text_hint": completion_text,
        "completion_url_hint": completion_url,
        "maximum_steps": max_steps,
        "step_examples": [
            {
                "action": "type",
                "instruction": "Search query",
                "value": "the exact text requested by the user",
            },
            {
                "action": "select",
                "instruction": "Category",
                "value": "the exact option label",
            },
            {"action": "check", "instruction": "In stock"},
            {"action": "click", "instruction": "Search button"},
            {"action": "read", "instruction": "Read the requested result"},
        ],
        "actions": [
            "navigate",
            "click",
            "type",
            "select",
            "check",
            "uncheck",
            "scroll",
            "back",
            "wait",
            "read",
            "done",
        ],
    }
    system_prompt = (
        "You are a browser planning compiler. Convert the trusted user goal into one complete "
        "bounded plan for a separate Edge extension executor. The start URL is opened before the "
        "first step, so do not add a navigate step that only repeats start_url. Return only one "
        "JSON object with keys steps, completion_text, completion_url. "
        "steps is an ordered array. Every type step MUST include a value string copied from the user goal, "
        "not merely mention the text in instruction. Each step must use one allowed action and contain instruction; "
        "use value only for type/select/scroll/wait, url only for navigate, and optional "
        "expected_text/expected_url only when they can be checked after that step. Do not set "
        "expected_text or expected_url on type/select/check/uncheck steps because form-control "
        "values are intentionally not observable after entry. "
        "For click/type/select/check/uncheck, describe the visible target by label/role/text; never "
        "emit CSS/XPath, JavaScript, browser code, element IDs, credentials, cookies, tokens, or "
        "passwords from page content. Copy form values exactly from the user goal. Include the entire workflow "
        "in this single plan instead of splitting it into multiple browser runs. Prefer clicking "
        "links/buttons discovered on the page over guessing destination URLs. End with read or done "
        "after the requested result is visible. completion_text/url should use supplied hints when "
        "present; otherwise use a conservative visible label/URL condition only if the goal states "
        "one. Never invent a secret or irreversible action."
    )

    last_validation_error = ""
    for attempt in range(2):
        request_for_attempt = dict(request)
        if attempt and last_validation_error:
            request_for_attempt["correction"] = (
                "The previous response failed validation with code "
                f"{last_validation_error}. Return a fresh JSON object that strictly follows the "
                "schema and field/action constraints. Do not quote or discuss the previous output."
            )
        prompt = json.dumps(request_for_attempt, ensure_ascii=False, allow_nan=False)
        try:
            raw = await asyncio.wait_for(
                method(prompt, system_prompt=system_prompt),
                timeout=timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise BrowserPlannerError("browser_planner_timeout") from None
        except Exception:
            raise BrowserPlannerError("browser_planner_failed") from None

        if not isinstance(raw, str):
            validation_error = BrowserPlannerError("browser_planner_invalid_json")
        else:
            try:
                decoded = _decode_json_object(raw)
                return _validated_plan(
                    decoded,
                    start_url=start_url,
                    goal=goal,
                    completion_text=completion_text,
                    completion_url=completion_url,
                    max_steps=max_steps,
                )
            except BrowserPlannerError as exc:
                validation_error = exc

        if validation_error.code not in {
            "browser_planner_invalid_json",
            "browser_planner_invalid_shape",
            "browser_planner_invalid_plan",
        }:
            raise validation_error
        last_validation_error = validation_error.code

    raise BrowserPlannerError(last_validation_error or "browser_planner_invalid_plan")


__all__ = ["BrowserPlannerError", "build_browser_plan"]
