"""Provider-independent agent runtime helpers.

Provider adapters should only handle transport details. Tool hints,
tool-result context shaping, and OpenAI-style tool loops live here so OpenAI,
Gemini, Ollama, SGLang, local compatible servers, and CLI adapters can share
the same agent contract.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from types import SimpleNamespace
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable, Mapping, Sequence

from ..tools.registry import ToolRegistry
from .aoi_vocabulary import build_aoi_vocabulary_hint
from .agentic_completion import (
    agentic_max_rounds,
    generation_failure_from_stopped_reason,
    response_looks_like_unfinished_work,
    set_current_compatible_generation_failure,
)
from .generation_policy import GenerationPolicy, GenerationProfile
from .generation_policy import get_current_generation_policy
from .generation_cancellation import AgentDialogueInputPending
from .generation_error import GenerationFailure
from .orchestration import build_orchestration_guidance
from .planning_policy import build_planning_system_guidance, get_current_planning_policy, get_current_planning_run_state
from .tool_packs import LOAD_TOOL_PACK_TOOL_NAME, PROJECT_TABLE_TOOL_NAMES
from .tool_policy import (
    DOCS_MUTATION_TOOL_NAMES,
    FILESYSTEM_MUTATION_TOOL_NAMES,
    FILESYSTEM_READ_TOOL_NAMES,
    FILESYSTEM_TOOL_NAMES,
    PROJECT_COMMAND_CAPABILITIES,
    PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES,
    PROJECT_MANAGEMENT_READ_TOOL_NAMES,
    PROJECT_MANAGEMENT_TOOL_NAMES,
    SEARCH_TOOL_NAMES,
    command_capabilities_from_text,
    looks_like_docs_mutation_request,
    looks_like_filesystem_mutation_request,
    looks_like_managed_workspace_request,
    looks_like_media_request,
    project_progress_review_active,
    looks_like_search_request,
    structured_task_hides_tool,
)
from .unified_turn_runtime import run_openai_compatible_turn_loop

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DialogueBudgetScope:
    """Turn-local budget owner shared by nested specialist calls."""

    client: Any
    ledger: Any
    parent_run_id: str | None = None
    execution_binding: Any | None = None


_CURRENT_DIALOGUE_BUDGET_SCOPE: contextvars.ContextVar[
    DialogueBudgetScope | None
] = contextvars.ContextVar(
    "current_dialogue_budget_scope",
    default=None,
)


def get_current_dialogue_budget_scope() -> DialogueBudgetScope | None:
    return _CURRENT_DIALOGUE_BUDGET_SCOPE.get()

DEFAULT_TOOL_HINT_CONTEXT_CHARS = 12000
PROJECT_PROGRESS_REVIEW_MAX_TOOL_ROUNDS = 120


def dialogue_provider_max_output_tokens(
    value: Any,
    requested: int | None = None,
) -> int | None:
    """Return the server-pinned output cap for one AgentDialogue turn.

    The budget projection is copied from a trusted persisted Run onto the
    request-owned client.  Ordinary Character clients have no projection and
    keep their historical provider defaults.  A caller-provided lower cap is
    preserved; a provider/model route cannot raise the server ceiling.
    """

    raw = getattr(value, "_agent_dialogue_budget", value)
    if not isinstance(raw, Mapping):
        return requested if requested is None or requested > 0 else None
    candidate = raw.get("provider_max_output_tokens")
    if candidate in (None, ""):
        candidate = raw.get("remaining_tokens")
    try:
        cap = int(candidate) if candidate is not None else None
    except (TypeError, ValueError, OverflowError):
        cap = None
    if cap is None or cap <= 0:
        return requested if requested is None or requested > 0 else None
    if requested is None or requested <= 0:
        return cap
    return min(int(requested), cap)


def dialogue_provider_tool_round_limit(value: Any) -> int | None:
    """Return a trusted per-turn tool-loop ceiling, when one is projected."""

    raw = getattr(value, "_agent_dialogue_budget", value)
    if not isinstance(raw, Mapping) and callable(getattr(value, "get", None)):
        raw = {
            "provider_max_tool_rounds": value.get(
                "runtime.agent_dialogue_max_tool_rounds"
            )
        }
    if not isinstance(raw, Mapping):
        return None
    candidate = raw.get("provider_max_tool_rounds")
    try:
        limit = int(candidate) if candidate is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    return max(1, limit) if limit is not None else None


def _dialogue_budget_mapping(value: Any) -> Mapping[str, Any] | None:
    raw = getattr(value, "_agent_dialogue_budget", value)
    return raw if isinstance(raw, Mapping) else None


def _dialogue_estimated_tokens(value: Any) -> int:
    if value in (None, "", [], {}):
        return 0
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        except Exception:
            value = str(value)
    # A tokenizer-independent *upper bound*: one provider token cannot carry
    # fewer than one UTF-8 byte.  Include framing bytes for message/JSON
    # delimiters.  This is intentionally conservative and is not presented as
    # the provider's measured token count.
    return max(1, len(str(value).encode("utf-8")) + 32)


def dialogue_provider_input_components(
    client: Any,
    user_input: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Estimate the request input charged against a finite Dialogue budget.

    This intentionally uses the same bounded character estimate as the
    context snapshot layer and includes the current message, effective system
    prompt, retained history/context bundle, and serialized tool schemas.
    Provider usage remains authoritative at terminal accounting; the estimate
    only prevents a request-local output cap from consuming the whole total
    allowance before those input components are paid.
    """

    components: dict[str, int] = {
        "current_input": _dialogue_estimated_tokens(user_input),
        "system_instructions": 0,
        "history": 0,
        "context": 0,
        "tool_schemas": 0,
    }
    if isinstance(payload, Mapping) and isinstance(payload.get("messages"), (list, tuple)):
        # At the provider boundary the serialized request is authoritative.
        # Include the complete continuation transcript (including tool
        # results), native schemas, and provider context fields.  Counting
        # only client.history here underestimates follow-up requests after a
        # large tool result has been appended.
        components["current_input"] = 0
        components["system_instructions"] = 0
        def payload_tokens(value: Any) -> int:
            if value in (None, "", [], {}):
                return 0
            try:
                if not isinstance(value, str):
                    value = json.dumps(
                        value,
                        ensure_ascii=False,
                        default=str,
                        separators=(",", ":"),
                    )
                # Provider tokenizers vary.  A bounded four-byte estimate
                # keeps small tool-call envelopes admissible while still
                # making a large tool result consume the live remainder.
                return max(1, (len(str(value).encode("utf-8")) + 3) // 4 + 32)
            except Exception:
                return _dialogue_estimated_tokens(value)

        components["history"] = payload_tokens(payload.get("messages"))
        components["context"] = payload_tokens(
            payload.get("context")
            or payload.get("context_bundle")
            or payload.get("extra_body")
            or payload.get("metadata")
        )
        components["tool_schemas"] = payload_tokens(
            payload.get("tools") or payload.get("functions")
        )
        return components
    system = (
        getattr(client, "_current_effective_system_prompt", None)
        or getattr(client, "_isolated_system_prompt_override", None)
        or getattr(client, "system_prompt", None)
        or getattr(client, "custom_system_prompt", None)
    )
    components["system_instructions"] = _dialogue_estimated_tokens(system)
    history_manager = getattr(client, "history_manager", None)
    if history_manager is not None:
        for name in ("get_model_messages", "get_all"):
            getter = getattr(history_manager, name, None)
            if callable(getter):
                try:
                    components["history"] = _dialogue_estimated_tokens(getter())
                except Exception:
                    pass
                if components["history"]:
                    break
    bundle = getattr(client, "_current_context_bundle", None)
    if bundle is not None:
        renderer = getattr(bundle, "render_partitioned_for_prompt", None)
        try:
            components["context"] = _dialogue_estimated_tokens(
                renderer() if callable(renderer) else bundle
            )
        except Exception:
            components["context"] = _dialogue_estimated_tokens(bundle)
    registry = getattr(client, "_tool_registry", None)
    if registry is not None:
        getter = getattr(registry, "get_all", None)
        if callable(getter):
            try:
                schemas = []
                for tool in getter():
                    serializer = getattr(tool, "to_json_schema", None)
                    schemas.append(serializer() if callable(serializer) else str(tool))
                components["tool_schemas"] = _dialogue_estimated_tokens(schemas)
            except Exception:
                # A missing schema projection must not erase the server cap.
                components["tool_schemas"] = 0
    return components


def dialogue_provider_invocation_budget(
    value: Any,
    *,
    user_input: str | None = None,
    requested: int | None = None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute a per-provider-call output cap from the finite total budget."""

    raw = _dialogue_budget_mapping(value)
    if raw is None:
        return {
            "max_output_tokens": requested if requested is None or requested > 0 else None,
            "input_tokens": 0,
            "tool_schema_tokens": 0,
            "reasoning_tokens": 0,
        }
    components = dialogue_provider_input_components(value, user_input, payload)
    input_tokens = sum(components.values())
    try:
        reasoning_reserve = max(
            0,
            int(
                raw.get("reasoning_reserve_tokens")
                or raw.get("provider_reasoning_reserve_tokens")
                or 0
            ),
        )
    except (TypeError, ValueError, OverflowError):
        reasoning_reserve = 0
    total_cap = raw.get("provider_max_output_tokens")
    if total_cap in (None, "") and raw.get("max_tokens") is not None:
        total_cap = raw.get("remaining_tokens")
    try:
        total_cap = max(0, int(total_cap)) if total_cap is not None else None
    except (TypeError, ValueError, OverflowError):
        total_cap = None

    cap_source = "token" if total_cap is not None else None
    input_cost_micros: int | None = None
    output_cost_per_token_micros: int | None = None

    # A cost-only ceiling can still derive a token cap when the server rate
    # card is present.  This path uses the reserved cost for this Run, so
    # sibling reservations cannot silently enlarge the provider allowance.
    if total_cap is None and raw.get("max_cost_micros") is not None:
        rate_card = raw.get("server_rate_card")
        try:
            output_rate = Decimal(str(rate_card.get("output_per_1k")))
            input_rate = Decimal(str(rate_card.get("input_per_1k")))
            reserved_cost = int(
                raw.get("provider_reserved_cost_micros")
                or raw.get("max_cost_micros")
                or 0
            )
            input_cost = (
                Decimal(input_tokens) * input_rate * Decimal(1000)
                / Decimal(1000)  # input_rate is already per 1k currency units
                * Decimal(1_000_000)
                / Decimal(1000)
            )
            output_cost_per_token = output_rate * Decimal(1_000_000) / Decimal(1000)
            input_cost_micros = max(0, int(input_cost.to_integral_value(rounding=ROUND_FLOOR)))
            output_cost_per_token_micros = max(
                0,
                int(output_cost_per_token.to_integral_value(rounding=ROUND_FLOOR)),
            )
            available = Decimal(max(0, reserved_cost)) - input_cost
            if output_cost_per_token <= 0 or available <= 0:
                total_cap = 0
            else:
                total_cap = int(
                    (available / output_cost_per_token).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                )
            cap_source = "cost"
        except (AttributeError, InvalidOperation, TypeError, ValueError, OverflowError):
            total_cap = 0
            cap_source = "cost"

    if total_cap is None:
        output_cap = requested if requested is None or requested > 0 else None
    else:
        # ``total_cap`` is a token ceiling for token budgets, but a
        # rate-derived output-token ceiling for cost-only budgets.  Input
        # tokens have already been charged in the latter calculation and
        # must not be subtracted a second time (1032 input tokens at the
        # example $0.01/$0.02 card leaves 234 output tokens, not zero).
        output_cap = max(
            0,
            total_cap
            - (input_tokens if cap_source != "cost" else 0)
            - reasoning_reserve,
        )
        if requested is not None and requested > 0:
            output_cap = min(output_cap, int(requested))
    return {
        "max_output_tokens": output_cap,
        "input_tokens": input_tokens,
        "tool_schema_tokens": components["tool_schemas"],
        "reasoning_tokens": reasoning_reserve,
        "cap_source": cap_source,
        "input_cost_micros": input_cost_micros,
        "output_cost_per_token_micros": output_cost_per_token_micros,
        "reserved_cost_micros": (
            int(raw.get("provider_reserved_cost_micros") or raw.get("max_cost_micros") or 0)
            if cap_source == "cost"
            else 0
        ),
        "estimate_source": "utf8_byte_upper_bound",
        "tokenizer_verified": False,
    }


def dialogue_provider_wire_max_output_tokens(
    value: Any,
    *,
    user_input: str | None = None,
    requested: int | None = None,
    payload: Mapping[str, Any] | None = None,
) -> int | None:
    """Resolve the exact completion cap to place on a provider payload.

    The adapter wrapper may only see a narrow async signature.  Provider
    implementations call this helper again at their synchronous wire entry so
    a missing ``max_tokens`` argument can never fall back to a 1024-token
    default after the AgentDialogue cap was computed.
    """

    raw = _dialogue_budget_mapping(value)
    if raw is None:
        return requested
    invocation = dialogue_provider_invocation_budget(
        value,
        user_input=user_input,
        requested=requested,
        payload=payload,
    )
    cap = invocation.get("max_output_tokens")
    if cap is None:
        return requested
    try:
        return max(0, int(cap))
    except (TypeError, ValueError, OverflowError):
        return 0


def dialogue_provider_latest_usage(value: Any) -> Mapping[str, Any] | None:
    """Return the latest provider-confirmed usage payload for one call.

    ``_agent_run_usage`` is a cumulative CLI ledger while ``_last_usage`` is
    a per-request provider receipt.  Mixing them (and then adding usage
    records once more) over-counts a turn.  Prefer an explicit per-request
    record, falling back to the latest provider receipt.
    """

    records = getattr(value, "_last_usage_records", None)
    if isinstance(records, (list, tuple)):
        for item in reversed(records):
            if isinstance(item, Mapping):
                return item
    for name in ("_last_usage", "_last_cli_usage"):
        item = getattr(value, name, None)
        if isinstance(item, Mapping) and item:
            return item
    return None


def _dialogue_response_usage(
    response: Any,
    *,
    provider: str | None = None,
    resolved_model: str | None = None,
) -> Mapping[str, Any] | None:
    """Read a response envelope's usage without depending on an SDK type."""

    raw = response.get("usage") if isinstance(response, Mapping) else getattr(response, "usage", None)
    if raw is not None:
        try:
            from .conversation_context import normalize_usage

            normalized_provider = str(provider or "").strip().casefold()
            if normalized_provider in {"openai_compatible_local", "sglang"}:
                # These adapters use the OpenAI completion envelope where
                # ``completion_tokens_details.reasoning_tokens`` is already
                # included in output_tokens.
                normalized_provider = "openai"
            normalized = normalize_usage(
                raw,
                provider=normalized_provider,
                resolved_model=resolved_model,
            )
            if normalized:
                if isinstance(raw, Mapping):
                    for key in ("cost_micros", "cost_currency", "currency"):
                        if raw.get(key) is not None:
                            normalized[key] = raw[key]
                    explicit_flag = raw.get("output_includes_reasoning")
                    if isinstance(explicit_flag, bool):
                        normalized["output_includes_reasoning"] = explicit_flag
                        if normalized.get("total_tokens_source") == "derived":
                            normalized["total_tokens"] = (
                                int(normalized.get("input_tokens") or 0)
                                + int(normalized.get("output_tokens") or 0)
                                + (
                                    int(normalized.get("reasoning_tokens") or 0)
                                    if not explicit_flag
                                    else 0
                                )
                            )
                return normalized
        except Exception:
            # Keep the bounded duck-typed fallback below for lightweight
            # provider test doubles and legacy SDK envelopes.
            pass
    if isinstance(raw, Mapping):
        result = dict(raw)
        aliases = {
            "prompt_tokens": "input_tokens",
            "completion_tokens": "output_tokens",
        }
        for source, target in aliases.items():
            if target not in result and source in result:
                result[target] = result[source]
        return result
    if raw is None:
        return None
    values: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "prompt_tokens",
        "output_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "total_tokens",
        "cost_micros",
    ):
        item = getattr(raw, key, None)
        if item is not None:
            values[key] = item
    if "input_tokens" not in values and "prompt_tokens" in values:
        values["input_tokens"] = values["prompt_tokens"]
    if "output_tokens" not in values and "completion_tokens" in values:
        values["output_tokens"] = values["completion_tokens"]
    return values or None


def dialogue_provider_usage_with_rate_card(
    usage: Mapping[str, Any] | None,
    rate_card: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Attach an exact provider/model rate-card cost when price is omitted."""

    if not isinstance(usage, Mapping):
        return None
    result = dict(usage)
    if result.get("cost_micros") is not None:
        return result
    reported_cost = result.get("cost", result.get("provider_reported_cost"))
    if reported_cost not in (None, ""):
        try:
            result["cost_micros"] = max(
                0,
                int(
                    (Decimal(str(reported_cost)) * Decimal(1_000_000)).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                ),
            )
            result.setdefault("cost_currency", result.get("currency") or "USD")
            return result
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            pass
    if not isinstance(rate_card, Mapping):
        return result
    try:
        input_tokens = result.get("input_tokens", result.get("prompt_tokens"))
        output_tokens = result.get("output_tokens", result.get("completion_tokens"))
        cached_tokens = result.get("cached_tokens", result.get("cache_read_input_tokens", 0))
        if input_tokens is None or output_tokens is None:
            return result
        input_rate = Decimal(str(rate_card.get("input_per_1k")))
        output_rate = Decimal(str(rate_card.get("output_per_1k")))
        cached_rate_raw = rate_card.get("cached_per_1k")
        input_tokens_i = max(0, int(input_tokens))
        output_tokens_i = max(0, int(output_tokens))
        cached_tokens_i = max(0, int(cached_tokens or 0))
        if not input_rate.is_finite() or not output_rate.is_finite():
            return result
        cost = (
            Decimal(input_tokens_i) * input_rate
            + Decimal(output_tokens_i) * output_rate
        ) / Decimal(1000)
        if cached_rate_raw not in (None, "") and cached_tokens_i:
            cached_rate = Decimal(str(cached_rate_raw))
            if not cached_rate.is_finite():
                return result
            cost += Decimal(cached_tokens_i) * cached_rate / Decimal(1000)
        result["cost_micros"] = max(
            0,
            int((cost * Decimal(1_000_000)).to_integral_value(rounding=ROUND_FLOOR)),
        )
        result.setdefault("cost_currency", rate_card.get("currency"))
        return result
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return result


def dialogue_provider_observed_tokens(
    value: Any,
    ledger: Any | None = None,
) -> int | None:
    """Read provider usage available during an in-flight tool loop."""

    def usage_total(item: Mapping[str, Any]) -> int:
        raw_total = item.get("total_tokens")
        if item.get("total_tokens_source") == "derived":
            raw_total = None
        if raw_total is None:
            raw_total = (
                int(item.get("input_tokens") or 0)
                + int(item.get("output_tokens") or 0)
                + (
                    int(item.get("reasoning_tokens") or 0)
                    if item.get("output_includes_reasoning") is not True
                    else 0
                )
            )
        return max(0, int(raw_total))

    if ledger is not None:
        if bool(getattr(ledger, "usage_unknown", False)):
            return None
        usage = getattr(ledger, "usage", None)
        if isinstance(usage, Mapping) and usage:
            try:
                return usage_total(usage)
            except (TypeError, ValueError, OverflowError):
                return None

    # CLI keeps a cumulative AgentRun payload.  It is already the complete
    # total and must not be added to the latest request receipt.
    cumulative = getattr(value, "_agent_run_usage", None)
    if isinstance(cumulative, Mapping) and cumulative:
        item = cumulative
    else:
        records = getattr(value, "_last_usage_records", None)
        if isinstance(records, (list, tuple)) and records:
            # Native runtimes publish one record per provider invocation.
            # Usage keys, when present, provide replay-safe de-duplication;
            # legacy records without one are still independent list entries.
            candidates = [item for item in records if isinstance(item, Mapping)]
            if not candidates:
                return None
            total = 0
            seen = False
            seen_keys: set[str] = set()
            for candidate in candidates:
                key = str(candidate.get("usage_key") or "").strip()
                if key and key in seen_keys:
                    continue
                if key:
                    seen_keys.add(key)
                try:
                    total += usage_total(candidate)
                    seen = True
                except (TypeError, ValueError, OverflowError):
                    continue
            return total if seen else None
        item = dialogue_provider_latest_usage(value)
        if item is None:
            return None
    try:
        return usage_total(item)
    except (TypeError, ValueError, OverflowError):
        return None


def _dialogue_usage_records_snapshot(ledger: Any) -> list[dict[str, Any]]:
    """Return bounded, key-bearing receipts from one request-local ledger."""

    records = getattr(ledger, "usage_records", None)
    if not isinstance(records, Mapping):
        return []
    snapshot: list[dict[str, Any]] = []
    for key, value in records.items():
        if not isinstance(value, Mapping):
            continue
        item = dict(value)
        usage_key = str(key or "").strip()
        if usage_key:
            item["usage_key"] = usage_key[:255]
        snapshot.append(item)
    return snapshot


def _safe_pending_tool_records(ledger: Any) -> list[dict[str, Any]]:
    """Project executed tool names/statuses without carrying model payloads."""

    results = getattr(ledger, "results", None)
    if not isinstance(results, (list, tuple)):
        return []
    safe_records: list[dict[str, Any]] = []
    for result in results:
        call = getattr(result, "call", None)
        if isinstance(call, Mapping):
            name = str(call.get("tool") or call.get("name") or "").strip()
        else:
            name = str(
                getattr(call, "tool", None)
                or getattr(call, "name", None)
                or ""
            ).strip()
        if not name:
            continue
        successful = bool(getattr(result, "success", False))
        safe_records.append(
            {
                "tool": name[:128],
                "arguments": {},
                "result": "",
                "successful": successful,
                "error": None if successful else "tool_execution_failed",
            }
        )
    return safe_records


def _clear_projected_dialogue_usage(client: Any) -> None:
    """Drop request-local usage so an older Run cannot be reused."""

    try:
        setattr(client, "_agent_dialogue_usage", {})
        setattr(client, "_last_usage_records", [])
    except Exception:
        # Lightweight compatibility doubles may expose read-only attributes;
        # accounting remains fail-closed when no projection can be installed.
        return


def _project_dialogue_ledger_to_client(client: Any, ledger: Any) -> None:
    """Publish confirmed cumulative receipts for the owning AgentRun only.

    The unified loop owns provider callbacks and de-duplication.  Expose its
    cumulative snapshot on a budgeted client so terminal AgentRun events can
    account a normal result or a typed question pause.  Missing usage remains
    absent and therefore reaches the existing unknown/fail-closed path.
    """

    if client is None or _dialogue_budget_mapping(client) is None:
        return
    if bool(getattr(ledger, "usage_unknown", False)):
        _clear_projected_dialogue_usage(client)
        return
    usage = getattr(ledger, "usage", None)
    if not isinstance(usage, Mapping):
        _clear_projected_dialogue_usage(client)
        return
    projected: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "total_tokens",
        "cost_micros",
    ):
        value = usage.get(key)
        if value is None:
            continue
        try:
            projected[key] = max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            continue
    if not projected:
        _clear_projected_dialogue_usage(client)
        return
    if isinstance(usage.get("output_includes_reasoning"), bool):
        projected["output_includes_reasoning"] = usage[
            "output_includes_reasoning"
        ]
    if usage.get("total_tokens_source") in {"reported", "derived"}:
        projected["total_tokens_source"] = usage["total_tokens_source"]
    projected["usage_state"] = "known"
    try:
        setattr(client, "_agent_dialogue_usage", projected)
        setattr(client, "_last_usage_records", _dialogue_usage_records_snapshot(ledger))
    except Exception:
        _clear_projected_dialogue_usage(client)


def _attach_pending_ledger_evidence(
    pending: AgentDialogueInputPending,
    ledger: Any,
) -> None:
    """Carry safe usage/tool evidence across the typed question boundary."""

    usage_records = (
        []
        if bool(getattr(ledger, "usage_unknown", False))
        else _dialogue_usage_records_snapshot(ledger)
    )
    tool_records = _safe_pending_tool_records(ledger)
    if not usage_records and not tool_records:
        return
    try:
        if usage_records:
            setattr(pending, "_native_usage_records", usage_records)
        if tool_records:
            setattr(pending, "_native_tool_records", tool_records)
    except Exception:
        # The typed pause remains authoritative if a compatibility exception
        # implementation rejects additive evidence attributes.
        return

PROJECT_PROGRESS_REFRESH_TOOL_NAMES: set[str] = {
    "organize_project_information_from_folder",
    "patch_project_information_doc",
    "attach_project_information_reference",
    "sync_wbs_tasks",
    "sync_issue_table",
    "append_record_rows",
    "update_record_row",
}

PROJECT_CONTEXT_REQUIRED_READ_TOOL_NAMES: tuple[str, ...] = (
    "list_project_information",
    "list_record_tables",
    "get_project_progress",
    "list_tasks",
    "get_project_issues",
    "get_upcoming_wbs_tasks",
    "list_calendar",
    "get_time_report",
    "summarize_project_requests",
    "render_project_diagram",
    "list_project_tasks_changed_since",
)

DIRECT_PROJECT_TOOL_HINT_NAMES: tuple[str, ...] = (
    "get_project_context",
    "list_record_tables",
    "list_project_information",
    "get_project_progress",
    "list_tasks",
    "list_calendar",
    "get_time_report",
    "get_upcoming_wbs_tasks",
    "organize_project_information_from_folder",
    "sync_wbs_tasks",
    "sync_issue_table",
    "create_record_table",
    "create_task",
    "patch_project_information_doc",
    "attach_project_information_reference",
)

DIRECT_SEARCH_TOOL_HINT_NAMES: tuple[str, ...] = (
    "web_search",
    "x_search",
    "grok_x_search",
    "docs_search",
    "docs_query",
    "knowledge_search",
    "knowledge_query",
    "bm25_search",
    "search_past_chats",
    "media_search",
)

KNOWLEDGE_ROUTING_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "media_search",
        "inbox_search_items",
        "docs_search",
        "docs_read",
        "docs_overview",
        "docs_query",
        "knowledge_search",
        "knowledge_query",
        "knowledge_read",
        "bm25_search",
        "search_past_chats",
    }
)

DIRECT_MEMORY_TOOL_HINT_NAMES: tuple[str, ...] = ("search_past_chats",)

DIRECT_FILESYSTEM_TOOL_HINT_NAMES: tuple[str, ...] = (
    "search_files",
    "list_directory",
    "list_workspace_tree",
    "read_file",
)

@dataclass(frozen=True)
class ToolHintRule:
    tool_name: str
    detector: Callable[[str], bool]
    pack_id: str = ""


@dataclass(frozen=True)
class OpenAIToolCallRecord:
    tool: str
    arguments: dict[str, Any]
    result: str
    success: bool | None = None
    # A policy/authority block may have an explicit safe reason even when the
    # legacy error field is empty. Keep this additive and at the end so older
    # positional and field-omitting producers remain compatible.
    blocked_reason: str | None = None
    # Preserve the provider call identity and whether the record represents an
    # executed or synthetic/not-executed result.  These are additive fields so
    # older compatibility records remain constructible.
    tool_call_id: str = ""
    executed: bool | None = None
    synthetic: bool = False
    # Digest of the restored raw intent (not the value) for durable identity.
    arguments_digest: str = ""
    # Safe provenance (code, target digest) of a server-contract argument rejection.
    argument_rejection: dict[str, str] | None = None
    # Safe fixed code for a framework policy refusal before execution.
    policy_denial: dict[str, str] | None = None

    @property
    def call_id(self) -> str:
        """Compatibility alias for unified/native ``call_id`` consumers."""

        return self.tool_call_id

    @property
    def successful(self) -> bool:
        if self.success is not None:
            return bool(self.success)
        lowered = self.result.strip().lower()
        return not (
            lowered.startswith("tool not found:")
            or lowered.startswith("error:")
            or "delegation error" in lowered
            or "requested mutation was not completed" in lowered
        )


@dataclass(frozen=True)
class OpenAIToolCallLoopResult:
    final_output: str
    tool_calls: list[OpenAIToolCallRecord]
    messages: list[dict[str, Any]] = dataclass_field(default_factory=list)
    stopped_reason: str = ""
    rounds: int = 0
    audit_tool_calls: list[OpenAIToolCallRecord] = dataclass_field(
        default_factory=list
    )
    # One normalized receipt per provider request.  The local OpenAI-compatible
    # adapter uses this request-local projection when a Dialogue question
    # aborts the loop before a normal result can be returned.
    usage_records: list[dict[str, Any]] = dataclass_field(default_factory=list)
    # Framework-owned terminal failures (for example an open tool-failure
    # circuit) must reach the owner loop without being inferred from output
    # text or ``stopped_reason`` alone.
    generation_failure: GenerationFailure | None = None


@dataclass(frozen=True)
class MissingToolExecutionClaim:
    tool_name: str
    matched_phrase: str


_VERIFIED_TOOL_EXECUTION_CLAIMS: contextvars.ContextVar[tuple[Any, ...]] = (
    contextvars.ContextVar("verified_tool_execution_claims", default=())
)


def set_verified_tool_execution_claims(
    tool_calls: Sequence[Any],
) -> contextvars.Token:
    current = _VERIFIED_TOOL_EXECUTION_CLAIMS.get()
    return _VERIFIED_TOOL_EXECUTION_CLAIMS.set((*current, *tuple(tool_calls)))


def reset_verified_tool_execution_claims(token: contextvars.Token) -> None:
    _VERIFIED_TOOL_EXECUTION_CLAIMS.reset(token)


TOOL_HINT_RULES: tuple[ToolHintRule, ...] = (
    ToolHintRule(
        tool_name="media_assistant",
        detector=looks_like_media_request,
        pack_id="media",
    ),
)

TOOL_EXECUTION_CLAIM_PATTERNS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "web_search",
        (
            "検索しました",
            "検索した結果",
            "検索したところ",
            "検索して確認",
            "検索で確認",
            "検索結果では",
            "web検索しました",
            "web検索した結果",
            "ウェブ検索しました",
            "ウェブ検索した結果",
            "調べた結果",
            "調査した結果",
            "webで確認",
            "ウェブで確認",
            "searched the web",
            "i searched",
            "looked it up online",
        ),
        (
            "検索していません",
            "検索はしていません",
            "検索できていません",
            "web検索はしていません",
            "ウェブ検索はしていません",
            "did not search",
            "didn't search",
            "have not searched",
            "no search was performed",
            "no web search was performed",
        ),
    ),
    (
        "media_read",
        (
            "原本を確認しました",
            "原本を読みました",
            "原本を参照しました",
            "原本で確認しました",
            "media_readで確認",
            "media_readで読み",
        ),
        (
            "原本を確認していません",
            "原本は確認していません",
            "原本を読んでいません",
            "原本を確認できませんでした",
            "原本を確認できなかった",
            "media_readで確認できませんでした",
            "media_readで確認できなかった",
            "media_readしていません",
        ),
    ),
    (
        "filesystem_tools",
        (
            "ファイルを読みました",
            "ファイルを確認しました",
            "資料を読みました",
            "資料を確認しました",
            "ワークスペースを確認しました",
            "read the file",
            "checked the file",
            "inspected the workspace",
        ),
        (
            "ファイルを読んでいません",
            "ファイルは読んでいません",
            "資料を確認していません",
            "did not read the file",
            "didn't read the file",
            "have not read the file",
        ),
    ),
    (
        "project_tools",
        (
            "dbを更新しました",
            "db更新しました",
            "案件情報dbを更新",
            "プロジェクト情報dbを更新",
            "タスクを作成しました",
            "wbsを同期しました",
            "課題管理表を更新しました",
            "進捗を確認しました",
            "進捗は確認できます",
            "案件の進捗は確認できます",
            "通常タスクを根拠",
            "タスクを根拠に進捗",
            "根拠に進捗算出",
            "updated the database",
            "updated the project database",
            "created the task",
            "checked project progress",
            "project progress is",
        ),
        (
            "dbを更新していません",
            "db更新していません",
            "案件情報dbを更新していません",
            "タスクを作成していません",
            "進捗を確認していません",
            "進捗確認はしていません",
            "did not update the database",
            "didn't update the database",
            "did not create the task",
            "did not check project progress",
        ),
    ),
    (
        "utility_tools",
        (
            "現在時刻を確認しました",
            "天気を確認しました",
            "計算しました",
            "checked the current time",
            "checked the weather",
            "calculated",
        ),
        (
            "現在時刻を確認していません",
            "天気を確認していません",
            "計算していません",
            "did not check the current time",
            "did not check the weather",
        ),
    ),
)


def build_tool_hint_context_sync(
    *,
    user_input: str,
    registry: ToolRegistry,
    policy: GenerationPolicy,
    log_prefix: str = "AgentRuntime",
    max_result_chars: int = DEFAULT_TOOL_HINT_CONTEXT_CHARS,
) -> str:
    """Build compact tool hints for the parent assistant."""
    if not policy.tool_hints_enabled:
        return ""

    matched_rules, deferred_rules = _collect_tool_hint_rules(user_input, registry)

    direct_search_hint = _should_hint_direct_search_tools(user_input, registry)
    direct_memory_hint = _should_hint_direct_memory_tools(user_input, registry)
    direct_filesystem_hint = _should_hint_direct_filesystem_tools(user_input, registry)
    project_attachment_stewardship_hint = _should_hint_project_attachment_stewardship(
        user_input,
        registry,
    )
    available_filesystem_mutation_tools = tuple(
        tool_name
        for tool_name in (
            "create_workspace_directory",
            "move_workspace_item",
            "copy_workspace_item",
        )
        if tool_name in registry
    )
    direct_project_hint = _should_hint_direct_project_tools(user_input, registry)
    project_progress_review_hint = _should_hint_project_progress_review(
        user_input,
        registry,
    )
    project_organizer_hint = "organize_project_information_from_folder" in registry
    knowledge_search_hint = "knowledge_search" in registry
    knowledge_routing_hint = (
        _knowledge_routing_hint(user_input, registry)
        if _should_hint_knowledge_routing(user_input, registry)
        else ""
    )
    aoi_vocabulary_hint = build_aoi_vocabulary_hint(
        user_input,
        inbox_search_available="inbox_search_items" in registry,
    )
    return _build_context_block(
        matched_rules,
        user_input=user_input,
        policy=policy,
        max_result_chars=max_result_chars,
        direct_search_hint=direct_search_hint,
        direct_memory_hint=direct_memory_hint,
        direct_filesystem_hint=direct_filesystem_hint,
        project_attachment_stewardship_hint=project_attachment_stewardship_hint,
        available_filesystem_mutation_tools=available_filesystem_mutation_tools,
        direct_project_hint=direct_project_hint,
        project_progress_review_hint=project_progress_review_hint,
        project_organizer_hint=project_organizer_hint,
        knowledge_search_hint=knowledge_search_hint,
        knowledge_routing_hint=knowledge_routing_hint,
        aoi_vocabulary_hint=aoi_vocabulary_hint,
        deferred_pack_rules=deferred_rules,
        project_tables_pack_hint=_project_tables_pack_hint_needed(
            user_input,
            registry,
        ),
    )


async def build_tool_hint_context_async(
    *,
    user_input: str,
    registry: ToolRegistry,
    policy: GenerationPolicy,
    log_prefix: str = "AgentRuntime",
    max_result_chars: int = DEFAULT_TOOL_HINT_CONTEXT_CHARS,
) -> str:
    """Async variant for native OpenAI-compatible clients."""
    if not policy.tool_hints_enabled:
        return ""

    matched_rules, deferred_rules = _collect_tool_hint_rules(user_input, registry)

    direct_search_hint = _should_hint_direct_search_tools(user_input, registry)
    direct_memory_hint = _should_hint_direct_memory_tools(user_input, registry)
    direct_filesystem_hint = _should_hint_direct_filesystem_tools(user_input, registry)
    project_attachment_stewardship_hint = _should_hint_project_attachment_stewardship(
        user_input,
        registry,
    )
    available_filesystem_mutation_tools = tuple(
        tool_name
        for tool_name in (
            "create_workspace_directory",
            "move_workspace_item",
            "copy_workspace_item",
        )
        if tool_name in registry
    )
    direct_project_hint = _should_hint_direct_project_tools(user_input, registry)
    project_progress_review_hint = _should_hint_project_progress_review(
        user_input,
        registry,
    )
    project_organizer_hint = "organize_project_information_from_folder" in registry
    knowledge_search_hint = "knowledge_search" in registry
    knowledge_routing_hint = (
        _knowledge_routing_hint(user_input, registry)
        if _should_hint_knowledge_routing(user_input, registry)
        else ""
    )
    aoi_vocabulary_hint = build_aoi_vocabulary_hint(
        user_input,
        inbox_search_available="inbox_search_items" in registry,
    )
    return _build_context_block(
        matched_rules,
        user_input=user_input,
        policy=policy,
        max_result_chars=max_result_chars,
        direct_search_hint=direct_search_hint,
        direct_memory_hint=direct_memory_hint,
        direct_filesystem_hint=direct_filesystem_hint,
        project_attachment_stewardship_hint=project_attachment_stewardship_hint,
        available_filesystem_mutation_tools=available_filesystem_mutation_tools,
        direct_project_hint=direct_project_hint,
        project_progress_review_hint=project_progress_review_hint,
        project_organizer_hint=project_organizer_hint,
        knowledge_search_hint=knowledge_search_hint,
        knowledge_routing_hint=knowledge_routing_hint,
        aoi_vocabulary_hint=aoi_vocabulary_hint,
        deferred_pack_rules=deferred_rules,
        project_tables_pack_hint=_project_tables_pack_hint_needed(
            user_input,
            registry,
        ),
    )


def compose_tool_hint_user_message(
    user_input: str,
    tool_hint_context: str,
) -> str:
    """Build the user message seen by the parent LLM with tool hints."""
    if not tool_hint_context:
        return user_input
    return f"{tool_hint_context}\n\nCurrent user request:\n{user_input}"


def project_context_required_read_tool_names(
    registry: ToolRegistry | None = None,
) -> tuple[str, ...]:
    """Return read tools that satisfy the project-context grounding requirement."""
    names = [
        name
        for name in PROJECT_CONTEXT_REQUIRED_READ_TOOL_NAMES
        if name in PROJECT_MANAGEMENT_READ_TOOL_NAMES
    ]
    if registry is None:
        return tuple(names)
    return tuple(name for name in names if name in registry)


def project_context_read_satisfied(tool_results: Sequence[Any]) -> bool:
    required_read_tools = set(project_context_required_read_tool_names())
    return any(
        _tool_result_name(result) in required_read_tools
        and _tool_result_successful(result)
        for result in tool_results
    )


def project_context_read_final_response_check(
    *,
    required: bool,
) -> Callable[[str, Sequence[Any], int], str | None] | None:
    if not required:
        return None

    def _check(
        final_output: str,
        tool_results: Sequence[Any],
        round_index: int,
    ) -> str | None:
        return _project_context_read_continuation_prompt(
            tool_results=tool_results,
            round_index=round_index,
        )

    return _check


def combined_final_response_check(
    *,
    user_input: str | None,
    require_project_context_read: bool,
) -> Callable[[str, Sequence[Any], int], str | None] | None:
    checks = [
        check
        for check in (
            project_context_read_final_response_check(
                required=require_project_context_read,
            ),
            _project_progress_review_final_response_check(user_input),
            _unfinished_work_final_response_check(user_input),
        )
        if check is not None
    ]
    if not checks:
        return None

    def _check(
        final_output: str,
        tool_results: Sequence[Any],
        round_index: int,
    ) -> str | None:
        for check in checks:
            continuation = check(final_output, tool_results, round_index)
            if continuation:
                return continuation
        return None

    return _check


def managed_workspace_evidence_missing(
    user_input: str | None,
    tool_results: Sequence[Any],
) -> tuple[str, ...]:
    """Return the high-level steps still missing for a managed workspace turn."""

    text = str(user_input or "")
    if not looks_like_managed_workspace_request(text):
        return ()
    successful = {
        _tool_result_name(result)
        for result in tool_results
        if _tool_result_successful(result)
    }
    missing: list[str] = []
    if looks_like_filesystem_mutation_request(text):
        if not successful.intersection({"list_workspace_tree", "list_directory"}):
            missing.append("inspect the target Project tree once with `list_workspace_tree`")
        if not successful.intersection(
            {"copy_workspace_item", "move_workspace_item", "docs_place_workspace_file"}
        ):
            missing.append(
                "place the file with `copy_workspace_item`, `move_workspace_item`, "
                "or `docs_place_workspace_file`"
            )
    if looks_like_docs_mutation_request(text) and not successful.intersection(
        {"docs_attach_workspace_file", "docs_place_workspace_file"}
    ):
        missing.append(
            "add the idempotent Docs link with `docs_attach_workspace_file`, or "
            "prefer `docs_place_workspace_file` to place and attach in one call"
        )
    return tuple(missing)


def _unfinished_work_final_response_check(
    user_input: str | None,
) -> Callable[[str, Sequence[Any], int], str | None] | None:
    if not user_input:
        return None

    def _check(
        final_output: str,
        tool_results: Sequence[Any],
        round_index: int,
    ) -> str | None:
        if not response_looks_like_unfinished_work(user_input, final_output):
            return None
        return (
            "Do not return a plan-only final answer. Perform the remaining work "
            "now with the AoiTalk tools already provided, then report the "
            "confirmed result. Do not re-run successful tool calls."
        )

    return _check


def _project_context_read_continuation_prompt(
    *,
    tool_results: Sequence[Any],
    round_index: int,
) -> str | None:
    if project_context_read_satisfied(tool_results):
        return None
    tools = ", ".join(
        f"`{name}`" for name in project_context_required_read_tool_names()
    )
    return (
        "Do not produce the final answer yet. Project context is enabled, but "
        "no successful project DB read tool result is available. Continue with "
        f"tool calls now: call at least one of {tools} first. This requirement "
        "is read-only grounding; after that read succeeds, decide normally "
        "whether more reads or any project DB mutation tools are needed for "
        "the user's request."
    )


def _project_progress_review_final_response_check(
    user_input: str | None,
) -> Callable[[str, Sequence[Any], int], str | None] | None:
    if not user_input or not project_progress_review_active(user_input):
        return None

    def _check(
        final_output: str,
        tool_results: Sequence[Any],
        round_index: int,
    ) -> str | None:
        return _project_progress_review_continuation_prompt(
            final_output=final_output,
            tool_results=tool_results,
            round_index=round_index,
        )

    return _check


def _project_progress_review_continuation_prompt(
    *,
    final_output: str,
    tool_results: Sequence[Any],
    round_index: int,
) -> str | None:
    successful_tools = [
        _tool_result_name(result)
        for result in tool_results
        if _tool_result_successful(result)
    ]
    progress_indices = [
        index
        for index, result in enumerate(tool_results)
        if _tool_result_name(result) == "get_project_progress"
        and _tool_result_successful(result)
    ]
    if not progress_indices:
        return (
            "Do not produce the final answer yet. This is project progress "
            "review mode, and no successful `get_project_progress` result is "
            "available. Continue with tool calls now: call `get_project_progress` "
            "for the selected/current project first, then inspect DB, record "
            "tables, tasks, project files, or web sources as needed before "
            "answering."
        )

    last_progress_index = progress_indices[-1]
    mutation_indices = [
        index
        for index, result in enumerate(tool_results)
        if _tool_result_name(result) in PROJECT_PROGRESS_REFRESH_TOOL_NAMES
        and _tool_result_successful(result)
    ]
    if mutation_indices and mutation_indices[-1] > last_progress_index:
        return (
            "Do not produce the final answer yet. Project information was "
            "updated after the latest `get_project_progress` check. Continue "
            "with tool calls and run `get_project_progress` again so the final "
            "answer is based on the updated DB state."
        )

    progress_payload = _latest_project_progress_payload(tool_results)
    if (
        progress_payload
        and progress_payload.get("can_assess_progress") is False
        and "organize_project_information_from_folder" not in successful_tools
    ):
        if get_current_generation_policy().profile == GenerationProfile.REVIEW:
            return (
                "Do not produce the final answer yet. `get_project_progress` says "
                "stored progress evidence is insufficient. Continue with read-only "
                "tool calls: inspect `list_project_information`, "
                "`list_record_tables`, tasks, relevant project files, and public "
                "web sources when external current facts are required. Do not "
                "modify project information in review mode."
            )
        return (
            "Do not produce the final answer yet. `get_project_progress` says "
            "stored progress evidence is insufficient. Continue with tool calls: "
            "inspect `list_project_information`, `list_record_tables`, tasks, "
            "and relevant project filer files. If the selected project filer may "
            "contain newer source documents, call "
            "`organize_project_information_from_folder` with `folder_path=\"\"` "
            "and `apply=true`, then run `get_project_progress` again. Use web "
            "search only if external current facts are required."
        )

    return None


def _latest_project_progress_payload(tool_results: Sequence[Any]) -> dict[str, Any]:
    for result in reversed(tool_results):
        if _tool_result_name(result) != "get_project_progress":
            continue
        payload = _json_object_from_tool_output(_tool_result_output(result))
        if payload:
            return payload
    return {}


def _json_object_from_tool_output(output: str) -> dict[str, Any]:
    text = str(output or "").strip()
    if not text:
        return {}
    for candidate in (text, text[text.find("{") : text.rfind("}") + 1]):
        candidate = candidate.strip()
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _tool_result_name(result: Any) -> str:
    call = getattr(result, "call", None)
    if call is not None:
        return str(getattr(call, "tool", "") or "")
    return str(getattr(result, "tool", "") or "")


def _tool_result_output(result: Any) -> str:
    if hasattr(result, "model_output"):
        return str(getattr(result, "model_output") or "")
    return str(getattr(result, "result", "") or getattr(result, "output", "") or "")


def _tool_result_successful(result: Any) -> bool:
    if hasattr(result, "success") and not bool(getattr(result, "success")):
        return False
    if hasattr(result, "successful") and not bool(getattr(result, "successful")):
        return False
    raw_output = _tool_result_output(result).strip()
    if raw_output.startswith("{") and raw_output.endswith("}"):
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and payload.get("success") is False:
            return False
    output = raw_output.lower()
    return not (
        output.startswith("error:")
        or output.startswith("tool not found:")
        or output.startswith("tool execution error:")
    )


def _openai_tool_call_record_from_unified(tool_result: Any) -> OpenAIToolCallRecord:
    blocked_reason = str(
        getattr(tool_result, "blocked_reason", "") or ""
    ).strip() or None
    call = getattr(tool_result, "call", None)
    tool_call_id = str(
        getattr(call, "call_id", "")
        or getattr(tool_result, "tool_call_id", "")
        or ""
    ).strip()
    executed = getattr(tool_result, "executed", None)
    synthetic = bool(getattr(tool_result, "synthetic", False))
    if getattr(tool_result, "failure_group", "") == "identity_fence" and tool_call_id:
        # The fenced call never ran. Persist its audit row under a distinct id
        # so it cannot collide with (or be mistaken for) the dispatched call's
        # receipt; the transcript keeps the provider id.
        fenced_digest = str(getattr(tool_result, "intent_digest", "") or "")
        tool_call_id = f"{tool_call_id}#fenced:{fenced_digest[7:19]}"
    return OpenAIToolCallRecord(
        tool=tool_result.call.tool,
        arguments=dict(tool_result.call.arguments),
        result=tool_result.model_output,
        success=bool(tool_result.success),
        blocked_reason=blocked_reason,
        tool_call_id=tool_call_id,
        executed=executed if isinstance(executed, bool) else None,
        synthetic=synthetic,
        arguments_digest=str(getattr(tool_result, "intent_digest", "") or ""),
        argument_rejection=(
            dict(getattr(tool_result, "argument_rejection"))
            if getattr(tool_result, "argument_rejection", None)
            else None
        ),
        policy_denial=(
            dict(getattr(tool_result, "policy_denial"))
            if getattr(tool_result, "policy_denial", None)
            else None
        ),
    )


def run_openai_tool_call_loop(
    *,
    initial_messages: list[dict[str, Any]],
    assistant_message: Any,
    api_kwargs: dict[str, Any],
    registry: ToolRegistry,
    create_completion: Callable[[dict[str, Any]], Any],
    log_prefix: str = "AgentRuntime",
    max_rounds: int = 5,
    return_result: bool = False,
    max_tool_result_chars: int | None = None,
    message_content: Callable[[Any], str] | None = None,
    config: Any | None = None,
    user_input: str | None = None,
    enforce_tool_policy: bool = True,
    require_project_context_read: bool = False,
    skip_final_response_check_on_empty: bool = False,
    event_callback: Callable[[str, dict[str, Any]], Any] | None = None,
    restore_tool_arguments: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    budget_client: Any | None = None,
    turn_ledger: Any | None = None,
    record_provider_usage: bool = True,
    tool_call_identity_check: Callable[[str, str, dict[str, Any]], str | None]
    | None = None,
) -> str | OpenAIToolCallLoopResult:
    """Execute OpenAI-compatible tool calls and re-prompt until text output."""
    effective_user_input = user_input
    if not effective_user_input:
        for message in reversed(initial_messages):
            if message.get("role") == "user":
                effective_user_input = str(message.get("content") or "")
                break
    effective_max_rounds = max_rounds
    current_policy = get_current_generation_policy()
    effective_max_rounds = max(
        effective_max_rounds,
        agentic_max_rounds(
            SimpleNamespace(config=config, generation_policy=current_policy),
            effective_user_input,
        ),
    )
    if effective_user_input and project_progress_review_active(effective_user_input):
        effective_max_rounds = max(
            effective_max_rounds,
            PROJECT_PROGRESS_REVIEW_MAX_TOOL_ROUNDS,
        )
    projected_round_limit = dialogue_provider_tool_round_limit(config)
    if projected_round_limit is not None:
        effective_max_rounds = min(effective_max_rounds, projected_round_limit)
    # The explicit ledger is shared with native/Specialist callers.  A
    # ContextVar fallback keeps older adapters that cannot forward the new
    # keyword on the same turn-local accounting path.
    from .unified_turn_runtime import UnifiedTurnLedger, _ACTIVE_TURN_LEDGER

    ledger = turn_ledger or _ACTIVE_TURN_LEDGER.get() or UnifiedTurnLedger()
    budget_raw = _dialogue_budget_mapping(budget_client)
    # The assistant message is itself the first provider invocation.  Record
    # its receipt before admitting a follow-up so nested/parallel delegates
    # share the full parent cumulative total.  Native SGLang already records
    # this invocation on its explicit ledger, so the usage-key guard avoids a
    # duplicate when that ledger is supplied.
    initial_usage = dialogue_provider_latest_usage(budget_client)
    provider_label = str(
        getattr(budget_client, "provider_label", "")
        or getattr(budget_client, "provider", "")
        or ""
    ).strip().casefold()
    initial_usage_unknown = (
        budget_client is not None
        and provider_label == "openai_compatible_local"
        and initial_usage is None
    )
    if initial_usage is not None and isinstance(budget_raw, Mapping):
        initial_usage = dialogue_provider_usage_with_rate_card(
            initial_usage,
            budget_raw.get("server_rate_card"),
        )
        if (
            budget_raw.get("max_cost_micros") is not None
            and initial_usage.get("cost_micros") is None
        ):
            ledger.mark_usage_unknown()
            initial_usage_unknown = True
    if (
        initial_usage is not None
        and not getattr(ledger, "usage_records", None)
    ):
        ledger.record_usage(
            initial_usage,
            usage_key=ledger.next_provider_usage_key("initial"),
        )
    budget_total = None
    budget_spent = 0
    budget_cost_total = None
    budget_cost_spent = 0
    budget_cost_base = 0
    provider_calls = 0
    budget_usage_unknown = initial_usage_unknown
    active_reservation: str | None = None
    if budget_raw is not None:
        budget_total = budget_raw.get("provider_max_output_tokens")
        if budget_total in (None, "") and budget_raw.get("max_tokens") is not None:
            budget_total = budget_raw.get("remaining_tokens")
        try:
            budget_total = max(0, int(budget_total)) if budget_total is not None else None
        except (TypeError, ValueError, OverflowError):
            budget_total = None
        provider_reserved_cost = budget_raw.get("provider_reserved_cost_micros")
        budget_cost_total = provider_reserved_cost or budget_raw.get("max_cost_micros")
        try:
            budget_cost_total = (
                max(0, int(budget_cost_total))
                if budget_cost_total is not None
                else None
            )
        except (TypeError, ValueError, OverflowError):
            budget_cost_total = None
        if provider_reserved_cost in (None, ""):
            try:
                budget_cost_base = max(0, int(budget_raw.get("cost_micros") or 0))
                if budget_cost_total is not None:
                    budget_cost_total = max(0, int(budget_cost_total) - budget_cost_base)
            except (TypeError, ValueError, OverflowError):
                budget_cost_base = 0
        observed = dialogue_provider_observed_tokens(budget_client, ledger)
        if observed is not None:
            budget_spent = observed
        budget_cost_spent = max(0, int(getattr(ledger, "usage", {}).get("cost_micros", 0) or 0))

    def apply_budget_before_provider(payload: dict[str, Any] | None = None) -> bool:
        """Clamp each follow-up request to the currently observed remainder."""

        nonlocal budget_spent, active_reservation
        if budget_client is None or budget_total is None:
            # Cost-only ceilings have no token total, but still require the
            # same provider-entry gate when a trusted rate card is present.
            if budget_client is None or budget_cost_total is None:
                return True
        if (provider_calls or getattr(ledger, "usage_records", None)) and (
            budget_usage_unknown or ledger.usage_unknown
        ):
            # Missing usage is not zero. A provider that cannot return a
            # receipt cannot safely receive another budgeted invocation.
            return False
        observed = dialogue_provider_observed_tokens(budget_client, ledger)
        if observed is not None:
            budget_spent = max(budget_spent, observed)
        remaining = (
            max(0, budget_total - budget_spent)
            if budget_total is not None
            else None
        )
        if remaining is not None:
            remaining = max(
                0,
                remaining - int(getattr(ledger, "reserved_tokens", 0) or 0),
            )
        invocation = dialogue_provider_invocation_budget(
            budget_client,
            user_input=effective_user_input,
            requested=None,
            payload=payload,
        )
        input_tokens = max(0, int(invocation.get("input_tokens") or 0))
        reasoning_tokens = max(0, int(invocation.get("reasoning_tokens") or 0))
        cap_source = invocation.get("cap_source")
        if cap_source == "cost" and budget_cost_total is not None:
            input_cost = max(0, int(invocation.get("input_cost_micros") or 0))
            output_cost = max(
                0,
                int(invocation.get("output_cost_per_token_micros") or 0),
            )
            cost_remaining = max(
                0,
                budget_cost_total
                - budget_cost_spent
                - int(getattr(ledger, "reserved_cost_micros", 0) or 0)
                - input_cost,
            )
            cap = (
                cost_remaining // output_cost
                if output_cost > 0
                else 0
            )
            requested_cap = invocation.get("max_output_tokens")
            if requested_cap is not None:
                cap = min(cap, max(0, int(requested_cap)))
        else:
            token_cap = invocation.get("max_output_tokens")
            if remaining is None:
                cap = int(token_cap or 0)
            else:
                cap = min(
                    max(0, int(token_cap or 0)),
                    max(0, remaining - input_tokens - reasoning_tokens),
                )
        try:
            cap = int(cap)
        except (TypeError, ValueError, OverflowError):
            cap = 0
        if cap <= 0:
            return False
        reservation_cost = 0
        if cap_source == "cost":
            try:
                reservation_cost = max(
                    0,
                    int(invocation.get("output_cost_per_token_micros") or 0)
                    * cap,
                )
            except (TypeError, ValueError, OverflowError):
                reservation_cost = 0
        active_reservation = ledger.reserve_provider_budget(
            cap,
            total_tokens=budget_total,
            total_cost_micros=budget_cost_total,
            cost_micros=reservation_cost,
        )
        if active_reservation is None:
            return False
        config_value = getattr(budget_client, "config", None)
        setter = getattr(config_value, "set", None)
        if callable(setter):
            setter("runtime.target_max_output_tokens", cap)
        target_payload = payload if isinstance(payload, dict) else api_kwargs
        token_keys = [
            key
            for key in ("max_tokens", "max_completion_tokens")
            if key in target_payload
        ]
        if not token_keys:
            token_keys = ["max_tokens"]
        for key in token_keys:
            if key in target_payload or key == "max_tokens":
                try:
                    current = target_payload.get(key)
                    target_payload[key] = (
                        min(int(current), cap) if current is not None else cap
                    )
                except (TypeError, ValueError, OverflowError):
                    target_payload[key] = cap
        return True

    def update_budget_after_provider(response: Any | None = None) -> None:
        nonlocal budget_spent, budget_cost_spent, provider_calls, active_reservation, budget_usage_unknown
        provider_calls += 1
        provider_name = (
            getattr(budget_client, "provider_label", None)
            or getattr(budget_client, "provider", None)
            or getattr(budget_client, "_provider", None)
        )
        if not provider_name and budget_client is not None:
            class_name = type(budget_client).__name__.casefold()
            if "openai" in class_name:
                provider_name = "openai_compatible_local"
            elif "sglang" in class_name:
                provider_name = "sglang"
            elif "ollama" in class_name:
                provider_name = "ollama"
        resolved_model = (
            getattr(response, "model", None)
            if response is not None and not isinstance(response, Mapping)
            else (response.get("model") if isinstance(response, Mapping) else None)
        )
        usage = (
            _dialogue_response_usage(
                response,
                provider=str(provider_name or ""),
                resolved_model=str(resolved_model or "") or None,
            )
            if response is not None
            else None
        )
        if (
            usage is not None
            and str(provider_name or "").strip().casefold()
            == "openai_compatible_local"
            and not any(
                usage.get(key) is not None
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "cached_tokens",
                    "total_tokens",
                    "cost_micros",
                )
            )
        ):
            usage = None
        if usage is None and response is not None:
            usage = dialogue_provider_latest_usage(budget_client)
        if usage is not None and budget_cost_total is not None:
            usage = dialogue_provider_usage_with_rate_card(
                usage,
                budget_raw.get("server_rate_card") if budget_raw else None,
            )
            if usage.get("cost_micros") is None:
                budget_usage_unknown = True
                ledger.mark_usage_unknown()
        if usage is None:
            budget_usage_unknown = True
            ledger.mark_usage_unknown()
        elif record_provider_usage:
            ledger.record_usage(
                usage,
                usage_key=ledger.next_provider_usage_key("provider"),
            )
        elif usage is not None and usage.get("cost_micros") is not None:
            enrich = getattr(ledger, "enrich_latest_usage_cost", None)
            if callable(enrich):
                try:
                    enrich(int(usage["cost_micros"]))
                except (TypeError, ValueError, OverflowError):
                    ledger.mark_usage_unknown()
        if usage is not None:
            observed = dialogue_provider_observed_tokens(budget_client, ledger)
            if observed is not None:
                budget_spent = max(budget_spent, observed)
            try:
                budget_cost_spent = max(
                    0,
                    int(getattr(ledger, "usage", {}).get("cost_micros", 0) or 0),
                )
            except (TypeError, ValueError, OverflowError):
                budget_cost_spent = 0
        if active_reservation is not None:
            ledger.settle_provider_budget(active_reservation, usage)
            active_reservation = None

    parent_run_id = None
    execution_binding = None
    if budget_client is not None and budget_raw is not None:
        try:
            from ..services.agent_run_service import get_current_agent_run_id

            parent_run_id = str(get_current_agent_run_id() or "").strip() or None
        except Exception:
            parent_run_id = None
        try:
            from ..services.turn_context import get_turn_context

            execution_binding = getattr(
                get_turn_context(),
                "agent_execution_binding",
                None,
            )
        except Exception:
            execution_binding = None
    scope_token = (
        _CURRENT_DIALOGUE_BUDGET_SCOPE.set(
            DialogueBudgetScope(
                client=budget_client,
                ledger=ledger,
                parent_run_id=parent_run_id,
                execution_binding=execution_binding,
            )
        )
        if budget_client is not None and budget_raw is not None
        else None
    )

    def create_completion_with_request_reset(payload: dict[str, Any]) -> Any:
        """Prevent a missing response receipt from reusing the prior call."""

        if budget_client is not None:
            for attribute in ("_last_usage", "_last_cli_usage", "_last_usage_records"):
                if hasattr(budget_client, attribute):
                    try:
                        setattr(
                            budget_client,
                            attribute,
                            [] if attribute == "_last_usage_records" else {},
                        )
                    except Exception:
                        continue
        return create_completion(payload)

    try:
        try:
            result = run_openai_compatible_turn_loop(
                initial_messages=initial_messages,
                assistant_message=assistant_message,
                api_kwargs=api_kwargs,
                registry=registry,
                create_completion=create_completion_with_request_reset,
                log_prefix=log_prefix,
                max_rounds=effective_max_rounds,
                max_tool_result_chars=max_tool_result_chars,
                message_content=message_content,
                config=config,
                user_input=effective_user_input,
                enforce_tool_policy=enforce_tool_policy,
                final_response_check=combined_final_response_check(
                    user_input=effective_user_input,
                    require_project_context_read=require_project_context_read,
                ),
                skip_final_response_check_on_empty=skip_final_response_check_on_empty,
                event_callback=event_callback,
                restore_tool_arguments=restore_tool_arguments,
                turn_ledger=ledger,
                before_provider_call=apply_budget_before_provider,
                after_provider_call=update_budget_after_provider,
                tool_call_identity_check=tool_call_identity_check,
            )
        except AgentDialogueInputPending as pending:
            # The typed pause is durable control flow. Preserve the provider
            # receipts and prior tool status before the ContextVar scope is
            # reset; the caller will terminalize the owning Run/Work without
            # re-entering the provider.
            if budget_usage_unknown:
                ledger.mark_usage_unknown()
            _project_dialogue_ledger_to_client(budget_client, ledger)
            _attach_pending_ledger_evidence(pending, ledger)
            raise
        if budget_usage_unknown:
            ledger.mark_usage_unknown()
        _project_dialogue_ledger_to_client(budget_client, ledger)
    finally:
        if scope_token is not None:
            _CURRENT_DIALOGUE_BUDGET_SCOPE.reset(scope_token)
    result_generation_failure = getattr(result, "generation_failure", None)
    if result_generation_failure is None:
        result_generation_failure = generation_failure_from_stopped_reason(
            getattr(result, "stopped_reason", None)
        )
    # Keep owner-loop recognition task-local.  Compatibility clients still
    # receive the legacy marker below for their existing persistence path, but
    # no continuation decision reads that shared mutable field.
    set_current_compatible_generation_failure(result_generation_failure)
    if budget_client is not None and hasattr(
        budget_client, "_last_generation_failure"
    ):
        # Compatible clients use this request-local marker to avoid entering
        # the owner review loop after a framework-owned terminal failure.  The
        # native path supplies an attempt-local ContextVar provider instead of
        # reading this shared compatibility field during review.
        try:
            budget_client._last_generation_failure = result_generation_failure
        except Exception:
            pass
    if not return_result:
        return result.final_output
    return OpenAIToolCallLoopResult(
        final_output=result.final_output,
        tool_calls=[
            _openai_tool_call_record_from_unified(tool_result)
            for tool_result in result.tool_results
        ],
        messages=[dict(message) for message in result.messages],
        stopped_reason=result.stopped_reason,
        rounds=result.rounds,
        audit_tool_calls=[
            _openai_tool_call_record_from_unified(tool_result)
            for tool_result in result.audit_tool_results
        ],
        usage_records=_dialogue_usage_records_snapshot(ledger),
        generation_failure=result_generation_failure,
    )


def _message_content(message: Any, extractor: Callable[[Any], str] | None) -> str:
    if extractor is not None:
        return str(extractor(message) or "")
    return str(getattr(message, "content", None) or "")


def _collect_tool_hint_rules(
    user_input: str,
    registry: ToolRegistry,
) -> tuple[list[ToolHintRule], list[ToolHintRule]]:
    """公開済みルールと、pack 未ロードで案内だけ出すルールに分ける。"""
    matched: list[ToolHintRule] = []
    deferred: list[ToolHintRule] = []
    load_available = LOAD_TOOL_PACK_TOOL_NAME in registry
    for rule in TOOL_HINT_RULES:
        if not rule.detector(user_input):
            continue
        if rule.tool_name in registry:
            matched.append(rule)
        elif load_available and rule.pack_id:
            deferred.append(rule)
    return matched, deferred


def _project_tables_pack_hint_needed(
    user_input: str,
    registry: ToolRegistry,
) -> bool:
    if LOAD_TOOL_PACK_TOOL_NAME not in registry:
        return False
    # A table/WBS pack is a command capability concern.  Ordinary mentions of
    # "WBS", "DB", or "タスク" must remain available for the model's own
    # selection rather than injecting a pack hint.
    if not command_capabilities_from_text(user_input) & {
        "project_db_update",
        "wbs_sync",
    }:
        return False
    return not any(name in registry for name in PROJECT_TABLE_TOOL_NAMES)


def _should_hint_direct_search_tools(user_input: str, registry: ToolRegistry) -> bool:
    return looks_like_search_request(user_input) and any(
        tool_name in registry for tool_name in DIRECT_SEARCH_TOOL_HINT_NAMES
    )


def _retrieval_user_text(user_input: str) -> str:
    """Unwrap only the server's command context, not markers in ordinary prose."""
    raw = str(user_input or "").replace("\r\n", "\n")
    if command_capabilities_from_text(raw):
        _, marker, request = raw.partition("\nCurrent user request:\n")
        if marker:
            return request
    return raw


def knowledge_retrieval_route(user_input: str) -> str:
    """Resolve the deterministic Knowledge retrieval lane for a request.

    This is intentionally lexical and local.  It only selects prompt guidance;
    the model still chooses the concrete tool and arguments, so no classifier
    model call is added to the turn.
    """

    text = _retrieval_user_text(user_input).casefold()
    if any(
        term in text
        for term in (
            "件数",
            "何件",
            "合計",
            "集計",
            "カウント",
            "平均",
            "グループ別",
            "全件",
        )
    ) or re.search(
        r"\b(?:aggregate|counts?|how many|totals?|averages?|avg|group by|grouping|sum)\b",
        text,
        flags=re.ASCII,
    ):
        return "aggregate"
    if any(
        term in text
        for term in (
            "timeline",
            "time line",
            "chronolog",
            "date range",
            "ordered by date",
            "時系列",
            "タイムライン",
            "履歴",
            "日付順",
            "時系列順",
            "推移",
        )
    ) or re.search(r"(?:いつ.*(?:変わ|変更)|(?:変わ|変更).*いつ|\bwhen\b.*\bchang(?:e|ed)\b)", text):
        return "timeline"
    if any(
        term in text
        for term in (
            "overview",
            "overall picture",
            "landscape",
            "summarize",
            "summarised",
            "summarized",
            "summarise",
            "summary",
            "summary of all",
            "trend",
            "trends",
            "long-term trend",
            "long term trend",
            "全体像",
            "概要",
            "総覧",
            "俯瞰",
            "全体のまとめ",
            "傾向",
            "長期傾向",
            "まとめ",
            "まとめて",
        )
    ):
        return "overview"
    return "lookup"


def _past_chat_lookup_requested(text: str) -> bool:
    return bool(
        re.search(r"\b(?:past|previous|earlier|old)\s+(?:chats?|conversations?)\b", text, flags=re.ASCII)
        or re.search(r"(?:過去|以前|前回|前の|別の)(?:の)?(?:チャット|会話)", text)
    )


def _local_search_requested(text: str) -> bool:
    """The user explicitly scoped the lookup to local (non-Web) data."""
    return "ローカル" in text or bool(re.search(r"\blocal\b", text, flags=re.ASCII))


def _media_search_routing_line(text: str, registry: ToolRegistry) -> str:
    if "media_search" not in registry:
        return ""
    line = (
        "- ローカルのDocs・Knowledge・過去の会話・添付media・User/Project Filesの内容は、まず `media_search` に"
        "テキストのqueryを渡して意味検索し、結果のsource参照を"
        + (" `media_read` で原本確認してください。" if "media_read" in registry else "確認してください。")
    )
    string_tools = [
        name
        for name in ("search_files", "bm25_search")
        if name in registry and not structured_task_hides_tool(name)
    ]
    if string_tools:
        line += (
            "ファイル名・パス・正確な語句が分かる場合は "
            + "/".join(f"`{name}`" for name in string_tools)
            + " の文字列検索を使い、Files索引が未完了（files_fallback_note）のときも併用してください。"
        )
    if _local_search_requested(text) or _past_chat_lookup_requested(text):
        line += "ローカル検索の依頼なのでWeb検索へ切り替えないでください。"
    return line


def _retrieval_requested(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:find|search|look up|lookup|read|show|list|retrieve|count|summari[sz]e|"
            r"summary|overview|timeline|aggregate|how many|group by|grouping|sum|totals?|averages?|avg)\b",
            text, flags=re.ASCII,
        )
        or any(term in text for term in (
            "探し", "探す", "探せ", "検索", "調べ", "調査", "読ん", "読む", "見せ", "一覧", "何件", "件数",
            "集計", "合計", "平均", "まとめ", "概要", "全体像", "時系列", "日付順", "確認", "参照して", "いつ",
        ))
    )


def _should_hint_knowledge_routing(
    user_input: str,
    registry: ToolRegistry,
) -> bool:
    """Gate Knowledge/Docs guidance by local intent and registered tools.

    Natural-language Knowledge/Docs requests do not carry the trusted
    ``web_search`` command capability.  Keep this intent gate independent from
    ``_should_hint_direct_search_tools`` so ordinary Web-search capability
    semantics remain unchanged.
    """

    if not any(name in registry for name in KNOWLEDGE_ROUTING_TOOL_NAMES):
        return False

    text = _retrieval_user_text(user_input).casefold()
    if not _retrieval_requested(text):
        return False

    if _past_chat_lookup_requested(text):
        return "search_past_chats" in registry or "media_search" in registry
    if re.search(r"\b(?:public web|on the web)\b|公開[の]?\s*[Ww][Ee][Bb]", text):
        return False
    if _local_search_requested(text):
        return True

    # A request needs an explicit Knowledge/Docs corpus subject; otherwise a
    # normal Web, filesystem, or chat request would receive Knowledge advice.
    if re.search(
        r"\b(?:docs?|documents?|notes?|memos?|knowledge|workspaces?)\b",
        text,
        flags=re.ASCII,
    ) or any(
        term in text
        for term in (
            "ナレッジ",
            "資料",
            "文書",
            "ドキュメント",
            "ノート",
            "案件",
            "プロジェクト",
            "変更履歴",
            "更新履歴",
            "change history",
            "revision history",
            "changelog",
        )
    ):
        return True
    if re.search(r"メモ(?!リ)", text):
        return True
    # A corpus follow-up can omit its noun. Give conditional coverage advice
    # for this narrow form instead of silently treating it as a top-k lookup.
    return bool(re.search(r"この\s*(?:\d+|[一二三四五六七八九十]+)\s*年間.*(?:傾向|全体|まとめ)", text))


def _knowledge_routing_hint(user_input: str, registry: ToolRegistry) -> str:
    """Return route-specific guidance for available Knowledge/Docs tools."""

    if not any(name in registry for name in KNOWLEDGE_ROUTING_TOOL_NAMES):
        return ""
    text = _retrieval_user_text(user_input).casefold()
    media_line = _media_search_routing_line(text, registry)
    route = knowledge_retrieval_route(user_input)
    structured = _structured_knowledge_hint(route, registry)
    past_chat = _past_chat_lookup_requested(text)

    # A past-chat fact request may contain summary/timeline words, but its
    # evidence still comes from the conversation lane.  Do not let the route
    # classifier turn it into a Docs overview/timeline request.
    if past_chat and route in {"overview", "timeline"} and media_line:
        return media_line

    if past_chat and route == "aggregate":
        if "list_chat_sessions" in registry:
            return (
                "- 過去チャットの件数/一覧は `list_chat_sessions` の count と"
                " total_session_count を確認してください。search_past_chats/media_searchのtop-k結果数を"
                "全体件数として扱わないでください。"
            )
        if media_line:
            return (
                media_line +
                "過去チャットのtop-k検索結果数は全体件数ではないため、exact totalとして扱わないでください。"
            )
        if "search_past_chats" in registry:
            return (
                "- 過去チャットの件数は `search_past_chats` のtop-k結果数を全体件数として扱わず、"
                "一覧取得ツールが利用可能ならそちらで確認してください。"
            )

    # A plain Docs/Knowledge/local/past-chat lookup has one canonical semantic
    # lane.  Keeping the legacy search/docs hint beside it makes the model
    # choose a different source or answer from an unverified top-k result.
    # Structured Docs operations retain their dedicated query/overview
    # guidance below.
    if route == "lookup" and media_line:
        return media_line

    if past_chat:
        # Structured Docs operations may have dedicated guidance above.  When
        # media_search is available, keep it as the only fallback lane instead
        # of presenting the legacy memory tool beside it.  If it is unavailable
        # retain the existing search_past_chats fallback.
        if media_line:
            return structured or media_line
        if "search_past_chats" in registry:
            return "- 過去チャットのlookupには `search_past_chats` を使って該当する会話を確認してください。"
        return "\n".join(line for line in (media_line, structured) if line)

    return structured or media_line


def _structured_knowledge_hint(route: str, registry: ToolRegistry) -> str:
    """Return the Docs/Knowledge lane guidance for one retrieval route."""
    available = {
        name
        for name in (*KNOWLEDGE_ROUTING_TOOL_NAMES, "bm25_search")
        if name in registry
        and name != "search_past_chats"
        and not structured_task_hides_tool(name)
    }
    if route == "lookup":
        lookup_tools = available.intersection(
            {"inbox_search_items", "docs_search", "knowledge_search", "bm25_search"}
        )
        if not lookup_tools:
            return ""
        ordered = [
            name
            for name in (
                "inbox_search_items",
                "docs_search",
                "knowledge_search",
                "bm25_search",
            )
            if name in lookup_tools
        ]
        return (
            "- Knowledge lookupは "
            + ", ".join(f"`{name}`" for name in ordered)
            + " を対象に応じて使い分けてください。"
        )
    if route == "aggregate":
        query_tools = available.intersection({"docs_query", "knowledge_query"})
        if not query_tools:
            return ""
        ordered = [
            name for name in ("docs_query", "knowledge_query") if name in query_tools
        ]
        return (
            "- aggregate/件数は "
            + " または ".join(f"`{name}`" for name in ordered)
            + " を使い、構造化条件で取得してください。top-k検索結果の表示数を件数として数えないでください。"
        )
    if route == "timeline":
        query_tools = available.intersection({"docs_query", "knowledge_query"})
        if not query_tools:
            return ""
        lines = ["- timelineは先に構造化された日付フィルターと並び順を指定してください。"]
        if "docs_query" in query_tools:
            lines.append(
                "`docs_query` の日付は組み込みの created_at / updated_at（作成・更新日時）です。"
                "day_date は日次ノートの日付です。date_from / date_to は order_by で選ぶフィールドの範囲、"
                "order は asc / desc を指定してください。"
                "本文の出来事の日付とは異なるため、出来事の時系列は取得した本文から確認してください。"
            )
        if "knowledge_query" in query_tools:
            lines.append("`knowledge_query` は文書日付の範囲と並び順を指定できます。")
        readers = [name for name in ("docs_read", "knowledge_read", "read_file") if name in registry]
        if readers:
            lines.append("根拠は " + "、".join(f"`{name}`" for name in readers) + " で読み取ってください。")
        if "docs_read" in readers:
            lines.append("Docs本文は `docs_read(view='document')` で取得し、next_cursorとcoverage_reasonsを確認してください。")
        return "".join(lines)
    if not available.intersection({"docs_search", "docs_query", "knowledge_search", "knowledge_query"}):
        return ""
    if "docs_overview" in registry:
        return ("- Docsのoverviewは `docs_overview` で構造化列挙と根拠ページの進捗を管理してください。"
                "1回のtop-k検索で済ませず、next_cursorを辿り、budget_limited/incomplete_records/coverage_completeを確認してください。"
                "取得件数を文書理解の保証として扱わないでください。")
    return (
        "- 対象が保存済みKnowledge/Docsの資料なら、overviewは1回のtop-k検索で済ませず、boundedな反復取得、"
        "または利用可能な構造化列挙で必要な範囲を埋めてください。"
        + ("Docs本文は `docs_read(view='document')` のページを読み、coverage_reasonsに残る不足を明示してください。"
           if "docs_read" in registry else "")
    )


def _should_hint_direct_memory_tools(user_input: str, registry: ToolRegistry) -> bool:
    # There is no natural-language memory capability.  Keep the normal tool
    # catalog untouched and let the model choose `search_past_chats` when the
    # request actually needs it.
    return False


def _should_hint_direct_filesystem_tools(user_input: str, registry: ToolRegistry) -> bool:
    # Files/Docs words alone do not establish that a filesystem tool is needed.
    # Verified Project attachments use the dedicated stewardship hint below.
    return False


def _should_hint_project_attachment_stewardship(
    user_input: str,
    registry: ToolRegistry,
) -> bool:
    """Use only task-local server metadata for attachment stewardship."""
    try:
        from ..services.turn_context import get_turn_context

        if not get_turn_context().verified_project_attachment:
            return False
    except Exception:
        return False
    return any(
        tool_name in registry
        for tool_name in (
            *DIRECT_FILESYSTEM_TOOL_HINT_NAMES,
            *FILESYSTEM_MUTATION_TOOL_NAMES,
        )
    )


def _should_hint_direct_project_tools(user_input: str, registry: ToolRegistry) -> bool:
    capabilities = command_capabilities_from_text(user_input)
    return bool(capabilities & PROJECT_COMMAND_CAPABILITIES) and any(
        tool_name in registry for tool_name in DIRECT_PROJECT_TOOL_HINT_NAMES
    )


def _should_hint_project_progress_review(user_input: str, registry: ToolRegistry) -> bool:
    return project_progress_review_active(user_input) and any(
        tool_name in registry for tool_name in DIRECT_PROJECT_TOOL_HINT_NAMES
    )


def _build_context_block(
    rules: Sequence[ToolHintRule],
    *,
    user_input: str,
    policy: GenerationPolicy,
    max_result_chars: int = DEFAULT_TOOL_HINT_CONTEXT_CHARS,
    direct_search_hint: bool = False,
    direct_memory_hint: bool = False,
    direct_filesystem_hint: bool = False,
    project_attachment_stewardship_hint: bool = False,
    available_filesystem_mutation_tools: Sequence[str] = (),
    direct_project_hint: bool = False,
    project_progress_review_hint: bool = False,
    project_organizer_hint: bool = False,
    knowledge_search_hint: bool = False,
    knowledge_routing_hint: str = "",
    aoi_vocabulary_hint: str = "",
    deferred_pack_rules: Sequence[ToolHintRule] = (),
    project_tables_pack_hint: bool = False,
) -> str:
    autonomous_execution_guidance = (
        "## Autonomous Tool Execution\n"
        "- Use the available tools without asking the user for execution permission. "
        "Ask only when required information or a consequential user choice is missing."
        if policy.permission_policy.value == "auto_approve"
        else ""
    )
    if (
        not rules
        and not deferred_pack_rules
        and not direct_search_hint
        and not direct_memory_hint
        and not direct_filesystem_hint
        and not project_attachment_stewardship_hint
        and not direct_project_hint
        and not project_progress_review_hint
        and not knowledge_routing_hint
        and not aoi_vocabulary_hint
    ):
        return autonomous_execution_guidance
    tool_lines = [f"- Consider `{rule.tool_name}`." for rule in rules]
    for rule in deferred_pack_rules:
        tool_lines.append(
            f"- `{rule.tool_name}` はまだロードされていません。"
            f'`load_tool_pack` に pack="{rule.pack_id}" を渡してロードしてから使ってください。'
        )
    if direct_search_hint:
        if knowledge_search_hint:
            tool_lines.append(
                "- 公開Webや最新情報は `web_search`、X/Twitterはまず `x_search`（Yahooリアルタイム検索）、"
                "不足する場合だけ `grok_x_search`、"
                "Knowledge Sourceは `knowledge_search` を使って確認してください。"
            )
        else:
            tool_lines.append(
                "- 公開Webや最新情報は `web_search`、"
                "X/Twitterはまず `x_search`（Yahooリアルタイム検索）、不足する場合だけ "
                "`grok_x_search` を使って確認してください。"
            )
    if knowledge_routing_hint:
        tool_lines.append(knowledge_routing_hint)
    if direct_memory_hint:
        tool_lines.append(
            "- ユーザーの好み・名前・過去の決定・以前の作業内容など、現在の会話に無い文脈が"
            "必要になったら `search_past_chats` で過去会話を検索してください。"
            "自動で添えられた過去会話の抜粋で足りない場合も `search_past_chats` で掘り下げてください。"
        )
    if direct_filesystem_hint:
        tool_lines.append(
            "- ワークスペースやファイル確認は `search_files`、`list_directory`、"
            "`list_workspace_tree`、`read_file` を使ってください。配置先を選ぶ時は"
            "同じ探索を繰り返さず、`list_workspace_tree` で既存構成を1回確認してください。"
        )
    if project_attachment_stewardship_hint:
        if available_filesystem_mutation_tools:
            mutation_step = (
                "、".join(
                    f"`{tool_name}`"
                    for tool_name in available_filesystem_mutation_tools
                )
                + "で必要最小限の整理を行い、最後に一覧で確認してください。"
            )
        else:
            mutation_step = (
                "このターンではworkspace変更ツールが利用できないため、"
                "分類と配置候補の確認までに留めてください。"
            )
        tool_lines.extend(
            [
                "- Project workspace内の添付を検出しました。これは会話専用の一時置き場ではなく、"
                "継続管理するProject資産の候補です。テンプレート・参照資料・ソース・成果物などは"
                "継続資産、今回だけの入力や一時出力は一時資料として分類してください。",
                "- 継続資産なら、まず `list_workspace_tree` でProject workspaceの"
                "既存構成を確認し、対応するフォルダを再利用してください。対応先がなければ"
                f"{mutation_step}",
                "- 一時資料は `attachments` に残し、無関係な既存ファイルは移動しないでください。"
                "分類に必要な内容は `read_file` で確認し、名前や拡張子だけで推測しないでください。",
            ]
        )
    if direct_project_hint:
        tool_lines.append(
            "- 案件情報は `list_project_information`、台帳は `list_record_tables`、"
            "タスク一覧/未完了/期限は `list_tasks`、予定は `list_calendar`、"
            "作業時間は `get_time_report` を使ってください。"
        )
        tool_lines.append(
            "- 進捗確認や状況確認は `get_project_progress` を起点にし、"
            "必要なら案件情報、台帳、タスク、予定、関連ファイルも確認してください。"
            "進捗は期間内の活動量ではなく案件目標への到達度です。"
        )
        tool_lines.append(
            "- タスク追加依頼は聞き返さず `create_task` を呼び、内容から簡潔なタイトルを作り、"
            "日時は due_date / start_at / end_at、詳細は description に入れてください。"
        )
        tool_lines.append(
            "- 新規タスクの作成前に、選択中Projectの既存タスクを `list_tasks`（必要なら search 付き）で"
            "確認し、parent_task_id 階層を尊重してください。明確な既存の関連root/containerがあれば"
            "そのsubtaskにし、なければ同一目的は1つのrootと実行可能なsubtasksにまとめ、独立成果だけを"
            "別rootにしてください。タイトルの曖昧な類似だけで統合せず、横断的な関連・依存をcontainmentと"
            "混同したり、重複containerを乱立させたりしないでください。"
        )
        tool_lines.append(
            "- 案件情報Docsは正本です。書く前に `list_project_information` で読み、"
            "見出し構造を保ったまま `patch_project_information_doc` で該当箇所だけを更新し、"
            "change_summary と source_refs_json を残してください。"
        )
    if project_tables_pack_hint:
        tool_lines.append(
            "- 台帳 / WBS / 課題管理表の作成・更新・同期ツールは未ロードです。"
            '`load_tool_pack` に pack="project_tables" を渡してロードしてから使ってください。'
        )
    if project_progress_review_hint:
        tool_lines.append(
            "- 進捗レビューでは `get_project_progress` から始め、回答に必要な根拠が揃うまで追加確認してください。"
        )
        evidence_line = (
            "- 根拠が不足、古い、または矛盾している場合は `list_project_information`、"
            "`list_record_tables`、タスク、予定、関連ファイルを確認してください。"
        )
        if project_organizer_hint:
            evidence_line += (
                "選択中プロジェクトの資料が新しい根拠になり得る場合は "
                "`organize_project_information_from_folder` を使ってください。"
            )
        tool_lines.append(evidence_line)
        if project_organizer_hint:
            tool_lines.append(
                "- 案件情報Docsや台帳を更新した場合は、最終回答前に "
                "`get_project_progress` を再実行してください。"
            )
    block = (
        "\n".join(["## Tool Hints", *tool_lines]).strip()
        if tool_lines
        else ""
    )
    if aoi_vocabulary_hint:
        block = (
            f"{block}\n\n{aoi_vocabulary_hint}"
            if block
            else aoi_vocabulary_hint
        )
    if autonomous_execution_guidance:
        block = (
            f"{block}\n\n{autonomous_execution_guidance}"
            if block
            else autonomous_execution_guidance
        )
    matched_tool_names = [rule.tool_name for rule in rules]
    if direct_search_hint:
        matched_tool_names.append("search_tools")
    if direct_memory_hint:
        matched_tool_names.append("memory_tools")
    if direct_filesystem_hint:
        matched_tool_names.append("filesystem_tools")
    if direct_project_hint:
        matched_tool_names.append("project_tools")
    if project_progress_review_hint:
        matched_tool_names.append("project_progress_review")
    guidance = build_orchestration_guidance(
        user_input=user_input,
        matched_tool_names=matched_tool_names,
        policy=policy,
    )
    if guidance:
        block = f"{block}\n\n{guidance}" if block else guidance
    planning_state = get_current_planning_run_state()
    if planning_state is not None:
        planning_guidance = build_planning_system_guidance(
            planning_policy=get_current_planning_policy(),
            generation_policy=policy,
            approved_plan=planning_state.plan,
        )
        if planning_guidance:
            block = f"{block}\n\n{planning_guidance}" if block else planning_guidance
    return _clip_text(block, max_result_chars)


def find_missing_tool_execution_claims(
    response_text: str,
    tool_calls: Sequence[Any],
) -> list[MissingToolExecutionClaim]:
    text = str(response_text or "")
    if not text.strip():
        return []
    available_tool_calls = (
        *_VERIFIED_TOOL_EXECUTION_CLAIMS.get(),
        *tuple(tool_calls),
    )
    missing: list[MissingToolExecutionClaim] = []
    for tool_name, positive_patterns, negative_patterns in TOOL_EXECUTION_CLAIM_PATTERNS:
        matched_phrase = _matched_execution_claim(
            text,
            positive_patterns=positive_patterns,
            negative_patterns=negative_patterns,
        )
        if not matched_phrase:
            continue
        required_tool = tool_name
        if tool_name == "web_search":
            # Explicit Web claims require a public-Web tool.  Generic
            # "検索した/調べた" claims may be grounded by any successful
            # search ledger, including the local semantic lane.
            required_tool = (
                "public_web_search"
                if _is_explicit_public_web_claim(text, matched_phrase)
                else "search_tools"
            )
        if _has_successful_tool_call(available_tool_calls, required_tool):
            continue
        missing.append(
            MissingToolExecutionClaim(
                tool_name=tool_name,
                matched_phrase=matched_phrase,
            )
        )
    return missing


def guard_tool_execution_claims(
    response_text: str,
    tool_calls: Sequence[Any],
) -> str:
    missing = find_missing_tool_execution_claims(response_text, tool_calls)
    if not missing:
        return response_text
    tool_names = ", ".join(f"`{item.tool_name}`" for item in missing)
    logger.warning(
        "Blocked response with unverified tool execution claim: %s",
        ", ".join(f"{item.tool_name}:{item.matched_phrase}" for item in missing),
    )
    return (
        "ツール実行の検証に失敗しました。"
        f"回答は {tool_names} を実行済みとして述べていますが、"
        "対応する成功した tool result が記録されていません。"
        "検索・読取・更新などの実行済み主張は、該当ツールの結果がある場合だけ行えます。"
    )


def _matched_execution_claim(
    text: str,
    *,
    positive_patterns: Sequence[str],
    negative_patterns: Sequence[str],
) -> str:
    lowered = text.casefold()
    if any(pattern.casefold() in lowered for pattern in negative_patterns):
        return ""
    for pattern in positive_patterns:
        if pattern.casefold() in lowered:
            return pattern
    return ""


def _has_successful_tool_call(tool_calls: Sequence[Any], tool_name: str) -> bool:
    aliases = _tool_execution_aliases(tool_name)
    return any(
        _tool_call_name(call) in aliases and _tool_call_successful(call)
        for call in tool_calls
    )


def _is_explicit_public_web_claim(text: str, matched_phrase: str) -> bool:
    lowered = text.casefold()
    return any(
        marker in lowered
        for marker in (
            "web検索",
            "ウェブ検索",
            "webで検索",
            "ウェブで検索",
            "webで確認",
            "ウェブで確認",
            "公開web",
            "公開ウェブ",
            "on the web",
            "searched online",
            "looked it up online",
            "public web",
            "インターネットで検索",
            "ネットで検索",
        )
    ) or any(
        marker in matched_phrase.casefold()
        for marker in ("web", "ウェブ", "online")
    )


def _tool_execution_aliases(tool_name: str) -> set[str]:
    if tool_name == "public_web_search":
        return {"web_search", "x_search", "grok_x_search"}
    if tool_name == "search_tools":
        return (
            set(SEARCH_TOOL_NAMES)
            | {
                "inbox_search_items",
                "docs_search",
                "docs_query",
                "docs_overview",
                "knowledge_query",
                "knowledge_search",
                "knowledge_read",
                "bm25_search",
                "search_files",
            }
        ) - {"media_read"}
    if tool_name == "filesystem_tools":
        return FILESYSTEM_TOOL_NAMES | {
            "media_read",
            "docs_read",
            "knowledge_read",
            "read_chat_session",
        }
    if tool_name == "project_tools":
        return PROJECT_MANAGEMENT_TOOL_NAMES
    if tool_name == "utility_tools":
        return {"get_current_time", "get_weather_info", "calculate"}
    return {tool_name}


def _tool_call_name(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("tool") or call.get("name") or "")
    return str(getattr(call, "tool", "") or getattr(call, "name", "") or "")


def _tool_call_successful(call: Any) -> bool:
    name = _tool_call_name(call)
    if isinstance(call, dict):
        successful = call.get("successful")
        result = call.get("result") or call.get("output") or call.get("model_output") or ""
    else:
        successful = getattr(call, "successful", None)
        result = (
            getattr(call, "result", "")
            or getattr(call, "model_output", "")
            or getattr(call, "output", "")
            or ""
        )
    if isinstance(successful, bool) and not successful:
        return False
    payload = result if isinstance(result, dict) else _json_object_from_tool_output(str(result))
    if isinstance(payload, dict) and payload.get("success") is False:
        return False
    if name == "media_read":
        return isinstance(payload, dict) and payload.get("success") is True
    if isinstance(successful, bool):
        return successful
    lowered = str(result).strip().lower()
    return not (
        lowered.startswith("tool not found:")
        or lowered.startswith("error:")
        or lowered.startswith("tool execution error:")
        or "delegation error" in lowered
        or "requested mutation was not completed" in lowered
    )


def _clip_text(text: str, max_chars: int | None) -> str:
    if not max_chars or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    suffix = "\n... (truncated to fit the model context budget)"
    keep = max(0, max_chars - len(suffix))
    return text[:keep].rstrip() + suffix
