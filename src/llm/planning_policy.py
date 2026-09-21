"""Provider-independent planning policy definitions."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import re
from collections.abc import Mapping as ABCMapping
from collections.abc import Sequence as ABCSequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from .generation_policy import GenerationPolicy, GenerationProfile


class PlanningPolicy(str, Enum):
    """User-facing planning mode, orthogonal to GenerationProfile."""

    AUTO = "auto"
    PLAN_FIRST = "plan_first"
    DIRECT = "direct"


class PlanningRunPhase(str, Enum):
    """Transient run state while a turn is active."""

    IDLE = "idle"
    PLANNING = "planning"
    AWAITING_USER = "awaiting_user"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Approval previews are intentionally separate from the action digest.  The
# digest binds the server-owned action arguments while the preview is the
# bounded, secret-redacted projection shown to a human reviewer.  Keep these
# limits conservative: an approval envelope must remain small enough for both
# the websocket payload and the durable AgentRun event projection.
MATERIAL_ACTION_PREVIEW_VERSION = 1
MAX_MATERIAL_ACTIONS = 32
MAX_MATERIAL_ACTION_PREVIEW_BYTES = 64 * 1024
MAX_MATERIAL_ACTION_PREVIEW_SCALAR_CHARS = 16 * 1024

# Only these domain mutations have an atomic action lane.  Other existing
# tools remain usable through the normal runtime, but callers that require the
# approved-plan atomic receipt path must fail closed for unsupported tools.
ATOMIC_APPROVED_PLAN_TOOLS = frozenset({"docs_create_nodes", "create_task"})


@dataclass(frozen=True)
class ApprovedPlan:
    """Execution contract passed to the normal agentic runtime after approval."""

    plan_id: str
    revision: int
    objective: str
    constraints: tuple[str, ...] = ()
    approach: str = ""
    # Not a script: goals/constraints/policy for the root agent.
    raw_text: str = ""
    user_feedback: str = ""
    # Durable approval binding.  These fields intentionally live on the
    # existing plan value rather than introducing a second approval/domain
    # table.  Tuple containers keep the value immutable while still allowing
    # JSON-safe projections in AgentRun metadata/events.
    actions: tuple[dict[str, Any], ...] = ()
    context_selection: Mapping[str, Any] | None = None
    evidence_hashes: tuple[str, ...] = ()
    allowed_dynamic_fields: tuple[str, ...] = ()
    canonical_plan_digest: str = ""
    canonical_action_digest: str = ""
    context_selection_hash: str = ""
    evidence_hash_set_digest: str = ""

    def __post_init__(self) -> None:
        # Dataclasses are frequently constructed directly by callers/tests;
        # compute missing digests there as well as in the planning runtime.
        normalized_actions = normalize_material_actions(self.actions)
        object.__setattr__(self, "actions", normalized_actions)
        normalized_evidence = normalize_evidence_hashes(self.evidence_hashes)
        object.__setattr__(self, "evidence_hashes", normalized_evidence)
        dynamic_fields = tuple(
            sorted(
                {
                    str(item).strip()
                    for item in self.allowed_dynamic_fields
                    if str(item).strip()
                }
            )
        )
        object.__setattr__(self, "allowed_dynamic_fields", dynamic_fields)
        context = canonicalize_plan_value(self.context_selection or {})
        object.__setattr__(self, "context_selection", context)
        if not self.canonical_plan_digest:
            object.__setattr__(
                self,
                "canonical_plan_digest",
                canonical_plan_digest(
                    raw_text=self.raw_text,
                    objective=self.objective,
                    constraints=self.constraints,
                    approach=self.approach,
                ),
            )
        if not self.canonical_action_digest:
            object.__setattr__(
                self,
                "canonical_action_digest",
                canonical_action_digest(normalized_actions),
            )
        if not self.context_selection_hash:
            object.__setattr__(
                self,
                "context_selection_hash",
                canonical_digest(context),
            )
        if not self.evidence_hash_set_digest:
            object.__setattr__(
                self,
                "evidence_hash_set_digest",
                canonical_digest(normalized_evidence),
            )

    @property
    def binding(self) -> dict[str, Any]:
        """Return the immutable, JSON-safe approval contract."""

        return plan_binding_for(self)


def canonicalize_plan_value(value: Any) -> Any:
    """Canonicalize values used in a plan binding.

    Plan/action bindings must be deterministic across providers and process
    restarts.  Unknown values are represented by their string form rather than
    allowing a provider object to leak into the durable event payload.
    """

    # Gemini protobuf containers implement the collections ABCs rather than
    # concrete dict/list types.  Canonicalize by protocol shape so bindings
    # stay deterministic and JSON-safe across providers.
    if isinstance(value, ABCMapping):
        return {
            str(key): canonicalize_plan_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, ABCSequence) and not isinstance(value, (str, bytes, bytearray)):
        return [canonicalize_plan_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [
            canonicalize_plan_value(item)
            for item in sorted(value, key=lambda item: str(item))
        ]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def canonical_plan_json(value: Any) -> str:
    return json.dumps(
        canonicalize_plan_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_plan_json(value).encode("utf-8")
    ).hexdigest()


def canonical_plan_digest(
    value: Any = None,
    *,
    raw_text: str = "",
    objective: str = "",
    constraints: Iterable[str] = (),
    approach: str = "",
) -> str:
    """Hash the material plan text/fields, excluding non-material feedback."""

    if value is not None and not (raw_text or objective or constraints or approach):
        payload = value
    else:
        payload = {
            "raw_text": str(raw_text or "").strip(),
            "objective": str(objective or "").strip(),
            "constraints": [str(item).strip() for item in constraints if str(item).strip()],
            "approach": str(approach or "").strip(),
        }
    return canonical_digest(payload)


def normalize_evidence_hashes(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = re.split(r"[,;\\s]+", values)
    if not isinstance(values, (ABCSequence, set, frozenset)) or isinstance(
        values, (bytes, bytearray)
    ):
        return ()
    normalized = {
        str(item).strip()
        for item in values
        if str(item).strip()
    }
    # Evidence locators are opaque hashes.  Keep bounded values and reject
    # whitespace/control-heavy input while remaining compatible with existing
    # non-sha256 evidence identifiers.
    return tuple(sorted(item[:256] for item in normalized if "\n" not in item and "\r" not in item))


def normalize_material_actions(actions: Any) -> tuple[dict[str, Any], ...]:
    """Normalize structured material actions to ``tool`` + canonical args.

    Dynamic fields are explicit per action (``dynamic_fields`` or
    ``allowed_dynamic_fields``); no implicit wildcard is accepted.
    """

    if actions is None or actions == "":
        return ()
    if isinstance(actions, str):
        try:
            actions = json.loads(actions)
        except (TypeError, ValueError):
            return ()
    if isinstance(actions, ABCMapping):
        actions = [actions]
    if not isinstance(actions, ABCSequence) or isinstance(actions, (bytes, bytearray)):
        return ()
    normalized: list[dict[str, Any]] = []
    for item in actions:
        if not isinstance(item, Mapping):
            continue
        tool = str(item.get("tool") or item.get("tool_name") or item.get("name") or "").strip()
        if not tool:
            continue
        raw_args = item.get("args")
        if raw_args is None:
            raw_args = item.get("arguments")
        if not isinstance(raw_args, Mapping):
            raw_args = {}
        raw_dynamic = item.get("dynamic_fields")
        if raw_dynamic is None:
            raw_dynamic = item.get("allowed_dynamic_fields")
        if isinstance(raw_dynamic, str):
            raw_dynamic = re.split(r"[,;\\s]+", raw_dynamic)
        dynamic = sorted(
            {
                str(field).strip()
                for field in (raw_dynamic or [])
                if str(field).strip()
            }
        )
        normalized.append(
            {
                "tool": tool,
                "args": canonicalize_plan_value(raw_args),
                "dynamic_fields": dynamic,
            }
        )
    # Action order is material for execution sequencing, so do not sort the
    # list itself; canonicalize each action's key order instead.
    return tuple(normalized)


def validate_material_actions(actions: Any) -> tuple[dict[str, Any], ...]:
    """Return a strict, non-empty approved action sequence.

    ``normalize_material_actions`` remains the compatibility projection used
    when reading old durable bindings.  Approval submission is an authority
    boundary and therefore uses this stricter validator: every submitted item
    must contain a tool name and an explicit object-valued argument binding.
    Dynamic fields are top-level argument names only.
    """

    source = actions
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except (TypeError, ValueError) as exc:
            raise ValueError("actions_json must be a JSON array") from exc
    if (
        not isinstance(source, ABCSequence)
        or isinstance(source, (bytes, bytearray))
        or not source
    ):
        raise ValueError("at least one structured action is required")
    if len(source) > MAX_MATERIAL_ACTIONS:
        raise ValueError(
            f"at most {MAX_MATERIAL_ACTIONS} structured actions are supported"
        )

    validated: list[dict[str, Any]] = []
    for index, item in enumerate(source):
        if not isinstance(item, ABCMapping):
            raise ValueError(f"action {index} must be an object")
        tool = str(
            item.get("tool") or item.get("tool_name") or item.get("name") or ""
        ).strip()
        if not tool:
            raise ValueError(f"action {index} requires tool")
        raw_args = item.get("args")
        if raw_args is None and "arguments" in item:
            raw_args = item.get("arguments")
        if not isinstance(raw_args, ABCMapping):
            raise ValueError(f"action {index} requires object args")
        # Validate the provider-native value graph before canonicalization;
        # otherwise arbitrary objects/bytes would be stringified and could
        # evade the bounded preview contract.
        try:
            _bounded_preview_value(raw_args)
        except ValueError as exc:
            raise ValueError(f"action {index} contains unsupported/oversized args") from exc
        raw_dynamic = item.get("dynamic_fields")
        if raw_dynamic is None:
            raw_dynamic = item.get("allowed_dynamic_fields")
        if isinstance(raw_dynamic, str):
            raw_dynamic = re.split(r"[,;\\s]+", raw_dynamic)
        if raw_dynamic is None:
            raw_dynamic = []
        if not isinstance(raw_dynamic, (ABCSequence, set, frozenset)) or isinstance(
            raw_dynamic, (bytes, bytearray)
        ):
            raise ValueError(f"action {index} dynamic_fields must be an array")
        dynamic: list[str] = []
        for field_name in raw_dynamic:
            field = str(field_name or "").strip()
            if not field or any(marker in field for marker in (".", "[", "]")):
                raise ValueError(
                    f"action {index} dynamic_fields must name top-level args"
                )
            dynamic.append(field)
        validated.append(
            {
                "tool": tool,
                "args": canonicalize_plan_value(raw_args),
                "dynamic_fields": sorted(set(dynamic)),
            }
        )
    return tuple(validated)


def _bounded_preview_value(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe preview value or fail closed.

    Approval UI payloads are untrusted transport data.  Do not stringify
    arbitrary provider objects here: their repr can contain secrets and is
    not stable across processes.  Gemini protobuf Mapping/Sequence values are
    accepted through the same ABC checks used by the canonicalizer.
    """

    if depth > 16:
        raise ValueError("material action preview nesting is too deep")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > MAX_MATERIAL_ACTION_PREVIEW_SCALAR_CHARS:
            raise ValueError("material action preview scalar is too large")
        if isinstance(value, float) and not (value == value and abs(value) != float("inf")):
            raise ValueError("material action preview contains a non-finite number")
        return value
    if isinstance(value, ABCMapping):
        if len(value) > 256:
            raise ValueError("material action preview object has too many fields")
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if len(key_text) > MAX_MATERIAL_ACTION_PREVIEW_SCALAR_CHARS:
                raise ValueError("material action preview key is too large")
            result[key_text] = _bounded_preview_value(item, depth=depth + 1)
        return result
    if isinstance(value, ABCSequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > 256:
            raise ValueError("material action preview array has too many items")
        return [_bounded_preview_value(item, depth=depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        if len(value) > 256:
            raise ValueError("material action preview set has too many items")
        # Sets are not valid JSON action arguments, but canonicalize them for
        # compatibility with existing callers while retaining a deterministic
        # order in the display projection.
        return [
            _bounded_preview_value(item, depth=depth + 1)
            for item in sorted(value, key=lambda item: str(item))
        ]
    raise ValueError("material action preview contains an unsupported value")


def build_material_action_preview(actions: Any) -> dict[str, Any]:
    """Build the bounded, immutable material-action projection for approval UI.

    The returned object intentionally uses ``arguments`` (rather than the
    internal ``args`` key) and contains no execution metadata.  Secret-like
    values are redacted by the local display helper; the independent
    ``action_digest`` in :func:`plan_binding_for` remains the authority over
    the original server-bound arguments.
    """

    validated = validate_material_actions(actions)
    if len(validated) > MAX_MATERIAL_ACTIONS:
        raise ValueError(f"at most {MAX_MATERIAL_ACTIONS} material actions are supported")
    # Import lazily to keep this policy module independent of provider/runtime
    # imports and avoid a module cycle during application startup.
    try:
        from ..services.outbound_privacy_service import redact_secret_for_local_display
    except Exception as exc:  # pragma: no cover - exercised via import-failure tests
        # The approval envelope is a human-facing boundary.  Continuing with
        # the raw arguments when the display redactor cannot be imported would
        # disclose secrets over the websocket/audit path, so fail closed with a
        # stable, non-sensitive reason that callers can safely persist.
        raise ValueError("material_action_preview_redaction_unavailable") from exc

    preview_actions: list[dict[str, Any]] = []
    for index, action in enumerate(validated):
        # Validate before redaction so bytes/arbitrary objects cannot be hidden
        # behind a marker and accidentally become approvable.
        bounded_arguments = _bounded_preview_value(action.get("args") or {})
        try:
            bounded_arguments = redact_secret_for_local_display(bounded_arguments)
        except Exception as exc:  # noqa: BLE001 - provider redactors are a trust boundary
            # Do not surface the redactor's exception text: adapters may embed
            # the raw argument value in it.  The caller records this stable
            # reason and sends no human interaction when redaction fails.
            raise ValueError("material_action_preview_redaction_failed") from exc
        bounded_arguments = _bounded_preview_value(bounded_arguments)
        preview_actions.append(
            {
                "index": index,
                "tool": _bounded_preview_value(action.get("tool") or ""),
                "arguments": bounded_arguments,
                "dynamic_fields": [
                    _bounded_preview_value(field)
                    for field in action.get("dynamic_fields") or ()
                ],
            }
        )
    preview = {
        "version": MATERIAL_ACTION_PREVIEW_VERSION,
        "action_count": len(preview_actions),
        "actions": preview_actions,
    }
    encoded_size = len(canonical_plan_json(preview).encode("utf-8"))
    if encoded_size > MAX_MATERIAL_ACTION_PREVIEW_BYTES:
        raise ValueError("material action preview exceeds its size limit")
    return preview


def material_action_preview_digest(preview: Any) -> str:
    """Return the stable digest for a previously built preview projection."""

    return canonical_digest(preview)


def materialize_approved_action_arguments(
    action: Mapping[str, Any],
    proposed_arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bind one provider proposal to the server-approved top-level args.

    Fixed values are always supplied from the approved binding.  A provider
    may omit them, but may neither alter them nor add an unapproved key.  Only
    explicitly listed top-level dynamic fields may be materialized from the
    provider proposal.
    """

    expected_raw = action.get("args")
    expected = canonicalize_plan_value(
        expected_raw if isinstance(expected_raw, ABCMapping) else {}
    )
    proposed = canonicalize_plan_value(proposed_arguments or {})
    if not isinstance(expected, dict) or not isinstance(proposed, dict):
        raise ValueError("approved action arguments must be objects")
    dynamic = {str(item) for item in action.get("dynamic_fields") or ()}
    allowed = set(expected) | dynamic
    extra = set(proposed) - allowed
    if extra:
        raise ValueError(
            "provider supplied unapproved arguments: " + ", ".join(sorted(extra))
        )
    for key, value in proposed.items():
        if key not in dynamic and key in expected and value != expected[key]:
            raise ValueError(f"provider changed fixed approved argument: {key}")

    materialized = dict(expected)
    for key in dynamic:
        if key in proposed:
            materialized[key] = proposed[key]
    return canonicalize_plan_value(materialized)


def canonical_action_digest(actions: Any) -> str:
    return canonical_digest(normalize_material_actions(actions))


def plan_binding_for(plan: ApprovedPlan | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(plan, ApprovedPlan):
        revision = max(0, int(plan.revision or 0))
        plan_id = str(plan.plan_id or "")
        plan_digest = plan.canonical_plan_digest
        action_digest = plan.canonical_action_digest
        context_hash = plan.context_selection_hash
        evidence_digest = plan.evidence_hash_set_digest
        actions = plan.actions
        evidence_hashes = plan.evidence_hashes
    else:
        revision = max(0, int(plan.get("revision") or plan.get("plan_revision") or 0))
        plan_id = str(plan.get("plan_id") or "")
        actions = normalize_material_actions(plan.get("actions"))
        evidence_hashes = normalize_evidence_hashes(plan.get("evidence_hashes"))
        plan_digest = str(plan.get("canonical_plan_digest") or plan.get("plan_digest") or "")
        if not plan_digest:
            plan_digest = canonical_plan_digest(plan.get("plan_text") or plan.get("raw_text") or plan)
        action_digest = str(plan.get("canonical_action_digest") or plan.get("actions_digest") or "")
        action_digest = action_digest or canonical_action_digest(actions)
        context_hash = str(plan.get("context_selection_hash") or "")
        context_hash = context_hash or canonical_digest(plan.get("context_selection") or {})
        evidence_digest = str(plan.get("evidence_hash_set_digest") or "")
        evidence_digest = evidence_digest or canonical_digest(evidence_hashes)
    preview_digest = ""
    try:
        preview_digest = material_action_preview_digest(
            build_material_action_preview(actions)
        )
    except ValueError as exc:
        # A preview redactor failure is a privacy boundary failure, not an
        # old/invalid binding that can use the empty-preview compatibility
        # digest.  Propagate the stable error so callers send no interaction
        # and do not persist an unredacted or ambiguous approval envelope.
        if str(exc) in {
            "material_action_preview_redaction_unavailable",
            "material_action_preview_redaction_failed",
        }:
            raise
        # Durable bindings loaded from older runs may not have a material
        # preview.  Preserve a server-supplied digest when available, while
        # approval submission itself remains strict and rejects invalid
        # actions before emitting a request.
        if isinstance(plan, Mapping):
            preview_digest = str(plan.get("material_action_preview_digest") or "")
        if not preview_digest:
            preview_digest = canonical_digest(
                {
                    "version": MATERIAL_ACTION_PREVIEW_VERSION,
                    "action_count": 0,
                    "actions": [],
                }
            )
    if isinstance(plan, ApprovedPlan):
        supplied_preview_digest = ""
    else:
        supplied_preview_digest = str(plan.get("material_action_preview_digest") or "")
    preview_digest = supplied_preview_digest or preview_digest
    return {
        "plan_id": plan_id,
        "revision": revision,
        "plan_digest": plan_digest,
        "action_digest": action_digest,
        "context_selection_hash": context_hash,
        "evidence_hash_set_digest": evidence_digest,
        "material_action_preview_digest": preview_digest,
    }


def plan_binding_matches(
    approved: ApprovedPlan | Mapping[str, Any],
    candidate: ApprovedPlan | Mapping[str, Any],
) -> bool:
    """Strictly compare all material approval binding dimensions."""

    left = plan_binding_for(approved)
    right = plan_binding_for(candidate)
    # plan_id is useful correlation but a candidate generated by a provider may
    # not carry it; the digests/revision are the authority for material data.
    return all(
        left.get(key) == right.get(key)
        for key in (
            "revision",
            "plan_digest",
            "action_digest",
            "context_selection_hash",
            "evidence_hash_set_digest",
            "material_action_preview_digest",
        )
    )


def approved_plan_action_allows_tool(
    plan: ApprovedPlan | Mapping[str, Any] | None,
    action_index: int,
    tool_name: str,
    arguments: Mapping[str, Any] | None = None,
) -> bool:
    """Match only the server cursor's action, never another action in a plan.

    This helper is the authority for provider/tool-policy integrations while a
    plan is executing.  A broad any-action matcher would let a provider call a
    later action early (or reuse a same-named action with different arguments).
    """

    if plan is None:
        return False
    source_actions = plan.actions if isinstance(plan, ApprovedPlan) else plan.get("actions")
    actions = normalize_material_actions(source_actions)
    try:
        index = int(action_index)
    except (TypeError, ValueError):
        return False
    if index < 0 or index >= len(actions):
        return False
    action = actions[index]
    normalized_tool = str(tool_name or "").strip()
    if action.get("tool") != normalized_tool:
        return False
    expected = action.get("args") if isinstance(action.get("args"), ABCMapping) else {}
    dynamic = set(action.get("dynamic_fields") or ())
    supplied = canonicalize_plan_value(arguments or {})
    if not isinstance(supplied, ABCMapping):
        return False
    expected_filtered = {key: value for key, value in expected.items() if key not in dynamic}
    supplied_filtered = {key: value for key, value in supplied.items() if key not in dynamic}
    return expected_filtered == supplied_filtered


def approved_plan_allows_tool(
    plan: ApprovedPlan | Mapping[str, Any] | None,
    tool_name: str,
    arguments: Mapping[str, Any] | None = None,
) -> bool:
    """Pure matcher for ToolPolicy integration.

    A plan with no structured actions intentionally matches no material tool;
    callers may continue to allow read-only tools separately.  A listed action
    must match exact canonical arguments except for its explicit dynamic fields.
    """

    if plan is None:
        return False
    source_actions = plan.actions if isinstance(plan, ApprovedPlan) else plan.get("actions")
    actions = normalize_material_actions(source_actions)
    normalized_tool = str(tool_name or "").strip()
    supplied = canonicalize_plan_value(arguments or {})
    for action in actions:
        if action.get("tool") != normalized_tool:
            continue
        expected = action.get("args") if isinstance(action.get("args"), Mapping) else {}
        dynamic = set(action.get("dynamic_fields") or ())
        expected_filtered = {key: value for key, value in expected.items() if key not in dynamic}
        supplied_filtered = {key: value for key, value in supplied.items() if key not in dynamic} if isinstance(supplied, Mapping) else {}
        if expected_filtered == supplied_filtered:
            return True
    return False


# Backward/forward-compatible aliases used by policy adapters and tests.
approved_plan_matches = plan_binding_matches
canonicalize_plan = canonicalize_plan_value


@dataclass
class PlanningRunState:
    """Mutable planning state for one agent run."""

    phase: PlanningRunPhase = PlanningRunPhase.IDLE
    plan: Optional[ApprovedPlan] = None
    pending_interaction_id: Optional[str] = None
    interaction_revision: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    approval_request_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        repr=False,
    )


DEFAULT_PLANNING_POLICY = PlanningPolicy.AUTO

_current_planning_policy: contextvars.ContextVar[PlanningPolicy] = (
    contextvars.ContextVar(
        "aoitalk_current_planning_policy",
        default=DEFAULT_PLANNING_POLICY,
    )
)
_current_planning_run_state: contextvars.ContextVar[PlanningRunState | None] = (
    contextvars.ContextVar(
        "aoitalk_current_planning_run_state",
        default=None,
    )
)


def resolve_planning_policy(value: Optional[str | PlanningPolicy]) -> PlanningPolicy:
    if isinstance(value, PlanningPolicy):
        return value
    if value is None or str(value).strip() == "":
        return DEFAULT_PLANNING_POLICY
    try:
        return PlanningPolicy(str(value).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in PlanningPolicy)
        raise ValueError(
            f"Invalid planning policy '{value}'. Allowed values: {allowed}"
        ) from exc


def set_current_planning_policy(policy: PlanningPolicy):
    return _current_planning_policy.set(policy)


def reset_current_planning_policy(token) -> None:
    _current_planning_policy.reset(token)


def get_current_planning_policy() -> PlanningPolicy:
    return _current_planning_policy.get()


def set_current_planning_run_state(state: PlanningRunState | None):
    return _current_planning_run_state.set(state)


def reset_current_planning_run_state(token) -> None:
    _current_planning_run_state.reset(token)


def get_current_planning_run_state() -> PlanningRunState | None:
    return _current_planning_run_state.get()


def is_planning_phase_active() -> bool:
    state = get_current_planning_run_state()
    if state is None:
        return False
    return state.phase in {
        PlanningRunPhase.PLANNING,
        PlanningRunPhase.AWAITING_USER,
        PlanningRunPhase.AWAITING_PLAN_APPROVAL,
    }


def is_planning_operator_fanout_forbidden() -> bool:
    """True while a single shared plan/approval gate must cover the whole turn."""
    state = get_current_planning_run_state()
    if state is None:
        return False
    return state.phase in {
        PlanningRunPhase.PLANNING,
        PlanningRunPhase.AWAITING_PLAN_APPROVAL,
    }


def is_planning_cancelled_terminal() -> bool:
    """True when planning was cancelled or timed out and must not resume execution."""
    state = get_current_planning_run_state()
    return state is not None and state.phase == PlanningRunPhase.CANCELLED


def is_direct_planning_forbidden() -> bool:
    return get_current_planning_policy() == PlanningPolicy.DIRECT


_AMBIGUITY_PATTERNS = (
    re.compile(r"\b(or|either|maybe|perhaps|unclear|ambiguous)\b", re.I),
    re.compile(r"(どちら|どっち|どれ|不明|曖昧|迷|未定)"),
)
_CONSEQUENCE_PATTERNS = (
    re.compile(r"\b(delete|drop|deploy|release|production|migrate|refactor)\b", re.I),
    re.compile(r"(削除|本番|リリース|デプロイ|移行|大規模|全面)"),
)
_SCOPE_PATTERNS = (
    re.compile(r"\b(entire|whole|all files|across|multiple modules)\b", re.I),
    re.compile(r"(全体|すべて|複数|横断|一式)"),
)
_PLANNING_COST_LOW_PATTERNS = (
    re.compile(r"\b(fix typo|rename|small|minor|quick)\b", re.I),
    re.compile(r"(タイポ|軽微|ちょっと|少し)"),
)


def should_enter_planning(
    *,
    user_input: str,
    generation_policy: GenerationPolicy,
    planning_policy: PlanningPolicy,
) -> bool:
    """Decide whether to enter a planning phase before agentic execution."""
    if planning_policy == PlanningPolicy.PLAN_FIRST:
        return True
    if planning_policy == PlanningPolicy.DIRECT:
        return False

    text = str(user_input or "").strip()
    if not text:
        return False

    if generation_policy.profile == GenerationProfile.AUTONOMOUS_WORK:
        # autonomous_work should not be stopped for routine complexity alone.
        if any(pattern.search(text) for pattern in _PLANNING_COST_LOW_PATTERNS):
            return False
        if not any(
            pattern.search(text)
            for patterns in (_AMBIGUITY_PATTERNS, _CONSEQUENCE_PATTERNS, _SCOPE_PATTERNS)
            for pattern in patterns
        ):
            return False

    if generation_policy.profile == GenerationProfile.REVIEW:
        return False

    score = 0
    if any(p.search(text) for p in _AMBIGUITY_PATTERNS):
        score += 2
    if any(p.search(text) for p in _CONSEQUENCE_PATTERNS):
        score += 2
    if any(p.search(text) for p in _SCOPE_PATTERNS):
        score += 1
    # Multi-step intent without explicit plan keyword.
    if len(text) > 240:
        score += 1
    if re.search(r"\b(plan|design|architecture|strategy|方針|設計|計画)\b", text, re.I):
        score += 1

    threshold = 3
    if generation_policy.profile == GenerationProfile.CHAT:
        threshold = 4
    return score >= threshold


def build_planning_system_guidance(
    *,
    planning_policy: PlanningPolicy,
    generation_policy: GenerationPolicy,
    approved_plan: ApprovedPlan | None = None,
) -> str:
    """Prompt guidance for planning or post-approval execution."""
    lines = [
        "Planning policy is active for this turn.",
        f"User planning mode: {planning_policy.value}.",
        f"Generation profile: {generation_policy.profile.value}.",
    ]
    if approved_plan is not None:
        lines.extend(
            [
                "An approved plan is in effect. The server executes its structured actions in exact order.",
                "When a tool is required, call only the single tool exposed for the next approved action.",
                f"Plan objective: {approved_plan.objective}",
            ]
        )
        if approved_plan.constraints:
            lines.append(
                "Constraints: " + "; ".join(approved_plan.constraints)
            )
        if approved_plan.approach:
            lines.append(f"Approach: {approved_plan.approach}")
        if approved_plan.user_feedback:
            lines.append(f"User feedback on prior plan: {approved_plan.user_feedback}")
    elif is_planning_phase_active():
        lines.append(
            "You are in planning mode. Gather context with read-only tools only. "
            "Do not mutate files, run destructive commands, or cause external side effects. "
            "When ready, produce a concise plan and request plan approval."
        )
    elif planning_policy == PlanningPolicy.DIRECT:
        lines.append(
            "Direct mode: do not initiate voluntary planning phases. "
            "You may still use ask_user_question or await tool permissions when needed."
        )
    return "\n".join(lines)
