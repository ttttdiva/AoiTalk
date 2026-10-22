"""Narrow repository API for recovered MediaOps generation receipts.

The common AgentWork coordinator remains the lifecycle owner.  This module
only re-reads the original generation Work/Run graph, checks current
authority, and returns a sealed proof plus bounded settlement references.
It never claims Work, renews a lease, resumes an approval, calls a provider,
or mutates the database.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Callable
from uuid import UUID, uuid4

from sqlalchemy import func, select

from ..memory.database import get_database_manager
from ..memory.models import (
    Agent,
    AgentRevision,
    AgentRun,
    AgentWorkEvent,
    AgentWorkItem,
    ExternalAction,
    ExternalActionApproval,
    GenerationOutput,
    GenerationPlan,
    GenerationRun,
    GenerationRunIntent,
    GenerationRunObservation,
    GenerationWorkspace,
    MediaAssetRevision,
    Persona,
    PersonaRevision,
    Project,
    Space,
)
from .generation_receipt_proof import (
    GenerationReceiptEvidence,
    GenerationReceiptEvidenceError,
    GenerationReceiptProof,
    GenerationReceiptProofError,
    _bind_generation_receipt_proof,
)


class AgentWorkServiceError(RuntimeError):
    """Base error for this internal generation repository boundary."""


class GenerationSettlementRejected(AgentWorkServiceError, ValueError):
    """The Work graph is not eligible for generation receipt settlement."""


class GenerationSettlementAuthorizationError(
    AgentWorkServiceError,
    PermissionError,
):
    """Current Agent authority or scope no longer permits settlement."""


def _uuid(value: Any, field_name: str, *, required: bool = True) -> UUID | None:
    if value in (None, ""):
        if required:
            raise GenerationSettlementRejected(f"{field_name} is required")
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise GenerationSettlementRejected(f"{field_name} is invalid") from exc


def _id(value: Any) -> str | None:
    return str(value) if value is not None else None


def _generation_intent_key(plan_id: UUID, plan_hash: str) -> str:
    material = f"{plan_id}|{plan_hash}|submit".encode("utf-8")
    return "generation:" + hashlib.sha256(material).hexdigest()[:48]


def _field(row: Any, name: str, default: Any = None) -> Any:
    if row is None or isinstance(row, Mapping):
        return default
    return getattr(row, name, default)


def _authority_method(value: Any) -> Callable[..., Any] | None:
    if callable(value):
        return value
    for name in ("resolve", "check", "authorize"):
        method = getattr(value, name, None)
        if callable(method):
            return method
    return None


async def _call_authority(
    resolver: Any,
    *,
    session: Any,
    agent_id: UUID,
    agent_revision_id: UUID,
    project_id: UUID | None,
    space_id: UUID | None,
    persona_id: UUID,
) -> Any:
    method = _authority_method(resolver)
    if method is None:
        raise GenerationSettlementAuthorizationError(
            "generation authority resolver is unavailable"
        )
    kwargs = {
        "agent_id": agent_id,
        "revision_id": agent_revision_id,
        "agent_revision_id": agent_revision_id,
        "project_id": project_id,
        "space_id": space_id,
        "persona_id": persona_id,
        "required_capability": "media",
        "requested_capability": "media",
        "requested_capabilities": ["media"],
        "capabilities": ["media"],
        "tool_capabilities": ["media"],
        "harness_capabilities": ["media"],
        "session": session,
    }
    try:
        result = None
        last_type_error: TypeError | None = None
        # Keep compatibility at this private seam without accepting a boolean
        # caller assertion.  The canonical resolver accepts the second form;
        # small test/deployment resolvers may expose only the first handful of
        # identity/scope fields.
        for candidate in (
            kwargs,
            {
                "agent_id": agent_id,
                "revision_id": agent_revision_id,
                "project_id": project_id,
                "space_id": space_id,
                "persona_id": persona_id,
                "required_capability": "media",
                "tool_capabilities": ["media"],
                "harness_capabilities": ["media"],
            },
            {
                "agent_id": agent_id,
                "revision_id": agent_revision_id,
                "project_id": project_id,
                "space_id": space_id,
                "persona_id": persona_id,
                "required_capability": "media",
            },
        ):
            try:
                result = method(**candidate)
                break
            except TypeError as exc:
                last_type_error = exc
        if result is None and last_type_error is not None:
            raise last_type_error
        if inspect.isawaitable(result):
            result = await result
        return result
    except GenerationSettlementAuthorizationError:
        raise
    except Exception as exc:
        raise GenerationSettlementAuthorizationError(
            "generation authority could not be revalidated"
        ) from exc


def _authority_value(result: Any, key: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(key, default)
    return getattr(result, key, default)


class AgentWorkService:
    """Generation-only receipt repository and canonical settlement API.

    The central runtime may use :meth:`prove_generation_receipt_settlement`
    for a read-only proof, or :meth:`settle_generation_receipt` for the one
    narrow recovery transition.  Other Work sources and generic approval
    flows are intentionally outside this class.
    """

    def __init__(
        self,
        db_manager: Any | None = None,
        *,
        authority_resolver: Any | None = None,
        agent_run_service: Any | None = None,
    ) -> None:
        self._db_manager = db_manager or get_database_manager()
        self.authority_resolver = authority_resolver
        # The parent may inject its canonical Base/AgentRun service.  The
        # fallback is resolved lazily so this narrow repository seam does not
        # import the large run-service graph during module loading.
        self.agent_run_service = agent_run_service

    async def _session(self) -> Any:
        value = self._db_manager.get_session()
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    async def _one(session: Any, statement: Any) -> Any | None:
        result = await session.execute(statement)
        scalar_one = getattr(result, "scalar_one_or_none", None)
        if callable(scalar_one):
            return scalar_one()
        scalar = getattr(result, "scalar", None)
        return scalar() if callable(scalar) else None

    @staticmethod
    async def _many(session: Any, statement: Any) -> list[Any]:
        result = await session.execute(statement)
        scalars = getattr(result, "scalars", None)
        if callable(scalars):
            return list(scalars().all())
        return []

    async def _locked_work(self, session: Any, work_item_id: UUID) -> AgentWorkItem:
        statement = (
            select(AgentWorkItem)
            .where(AgentWorkItem.id == work_item_id)
            .with_for_update()
        )
        work = await self._one(session, statement)
        if work is None:
            raise GenerationSettlementRejected("generation Work row is missing")
        return work

    async def _original_agent_run(
        self,
        session: Any,
        work: AgentWorkItem,
    ) -> AgentRun:
        active_id = _uuid(_field(work, "active_agent_run_id"), "active_agent_run_id", required=False)
        if active_id is not None:
            run = await session.get(AgentRun, active_id)
            if run is None:
                raise GenerationSettlementRejected("original AgentRun is missing")
            return run
        attempt = _field(work, "attempt_count")
        statement = select(AgentRun).where(AgentRun.work_item_id == work.id)
        if attempt is not None:
            statement = statement.where(AgentRun.work_item_attempt == int(attempt))
        rows = await self._many(session, statement.order_by(AgentRun.created_at.desc()))
        if len(rows) != 1:
            raise GenerationSettlementRejected(
                "generation Work does not have exactly one original AgentRun"
            )
        return rows[0]

    async def _assert_no_external_approval(
        self,
        session: Any,
        work_item_id: UUID,
    ) -> None:
        statement = (
            select(ExternalActionApproval.id)
            .join(ExternalAction, ExternalAction.id == ExternalActionApproval.action_id)
            .where(ExternalAction.origin_work_item_id == work_item_id)
            .limit(1)
        )
        approval_id = await self._one(session, statement)
        if approval_id is not None:
            raise GenerationSettlementRejected(
                "generation receipt cannot use an ExternalAction approval"
            )

    async def _assert_live_identity_and_authority(
        self,
        session: Any,
        *,
        work: AgentWorkItem,
        plan: GenerationPlan,
        agent_run: AgentRun,
        workspace: GenerationWorkspace,
    ) -> None:
        agent_id = _uuid(_field(work, "assigned_agent_id"), "work.assigned_agent_id")
        revision_id = _uuid(_field(work, "agent_revision_id"), "work.agent_revision_id")
        persona_id = _uuid(_field(work, "persona_id"), "work.persona_id")
        project_id = _uuid(_field(work, "project_id"), "work.project_id", required=False)
        space_id = _uuid(_field(work, "space_id"), "work.space_id", required=False)
        if _uuid(_field(agent_run, "agent_id"), "agent_run.agent_id") != agent_id:
            raise GenerationSettlementRejected("AgentRun agent pin changed")
        if _uuid(_field(agent_run, "agent_revision_id"), "agent_run.agent_revision_id") != revision_id:
            raise GenerationSettlementRejected("AgentRun revision pin changed")
        if _uuid(_field(plan, "project_id"), "plan.project_id", required=False) != project_id:
            raise GenerationSettlementRejected("generation project pin changed")
        persona_revision_id = _uuid(
            _field(plan, "persona_revision_id"), "plan.persona_revision_id"
        )
        persona_revision = await session.get(PersonaRevision, persona_revision_id)
        if persona_revision is None:
            raise GenerationSettlementRejected("generation PersonaRevision is missing")
        if _uuid(_field(persona_revision, "persona_id"), "persona_revision.persona_id") != persona_id:
            raise GenerationSettlementRejected("generation Persona pin changed")
        if _uuid(_field(persona_revision, "project_id"), "persona_revision.project_id", required=False) != project_id:
            raise GenerationSettlementRejected("generation Persona project scope changed")
        persona = await session.get(Persona, persona_id)
        if persona is None or str(_field(persona, "state", "")) in {"archived", "paused"}:
            raise GenerationSettlementRejected("generation Persona is not live")
        if _uuid(_field(persona, "project_id"), "persona.project_id", required=False) != project_id:
            raise GenerationSettlementRejected("live Persona project scope changed")
        agent = await session.get(Agent, agent_id)
        revision = await session.get(AgentRevision, revision_id)
        if agent is None or str(_field(agent, "state", "")) != "active":
            raise GenerationSettlementAuthorizationError("Agent is no longer active")
        if revision is None or _uuid(_field(revision, "agent_id"), "revision.agent_id") != agent_id:
            raise GenerationSettlementAuthorizationError("AgentRevision is no longer bound")
        if project_id is not None:
            project = await session.get(Project, project_id)
            if project is None or _field(project, "deleted_at") is not None:
                raise GenerationSettlementAuthorizationError("Project is no longer live")
            project_space = _uuid(_field(project, "space_id"), "project.space_id", required=False)
            if project_space != space_id:
                raise GenerationSettlementAuthorizationError("Project/Space scope changed")
            if _uuid(_field(plan, "project_id"), "plan.project_id") != project_id:
                raise GenerationSettlementRejected("plan project scope changed")
        elif space_id is not None:
            space = await session.get(Space, space_id)
            if space is None:
                raise GenerationSettlementAuthorizationError("Space is no longer live")
        resolver = self.authority_resolver
        if resolver is None:
            try:
                from .agent_authority import AgentAuthorityResolver

                resolver = AgentAuthorityResolver(self._db_manager)
            except Exception as exc:
                raise GenerationSettlementAuthorizationError(
                    "generation authority resolver is unavailable"
                ) from exc
        decision = await _call_authority(
            resolver,
            session=session,
            agent_id=agent_id,
            agent_revision_id=revision_id,
            project_id=project_id,
            space_id=space_id,
            persona_id=persona_id,
        )
        if not bool(_authority_value(decision, "allowed", _authority_value(decision, "is_allowed", False))):
            reason = _authority_value(decision, "reason", None) or _authority_value(decision, "deny_reasons", None)
            raise GenerationSettlementAuthorizationError(
                str(reason or "generation Agent authority denied")
            )
        for name, expected in (
            ("agent_id", agent_id),
            ("agent_revision_id", revision_id),
            ("project_id", project_id),
            ("space_id", space_id),
            ("persona_id", persona_id),
        ):
            observed = _authority_value(decision, name, None)
            if observed not in (None, "") and str(observed) != str(expected):
                raise GenerationSettlementAuthorizationError(
                    f"generation authority {name} scope changed"
                )
        if _uuid(_field(work, "agent_revision_id"), "work.agent_revision_id") != _uuid(
            _field(revision, "id"), "revision.id"
        ):
            raise GenerationSettlementRejected("Work AgentRevision row changed")
        if workspace.external_project_id is None:
            raise GenerationSettlementRejected("Generation workspace project scope is missing")

    @staticmethod
    def _assert_selector_pin(
        plan: GenerationPlan,
        evidence: GenerationReceiptEvidence,
    ) -> None:
        spec = _field(plan, "request_spec", {})
        if not isinstance(spec, Mapping):
            raise GenerationSettlementRejected("generation plan request spec is invalid")
        expected_selector = (
            spec.get("model_selection_id")
            or spec.get("image_model_selection_id")
            or spec.get("video_model_selection_id")
        )
        if expected_selector not in (None, ""):
            if evidence.selector_id != str(expected_selector):
                raise GenerationSettlementRejected("generation selector pin changed")
            return
        expected_preset = spec.get("preset_id")
        expected_revision = spec.get("preset_revision_id")
        if not expected_preset or not expected_revision:
            raise GenerationSettlementRejected("generation selector pin is missing")
        if evidence.preset_id != str(expected_preset) or evidence.preset_revision_id != str(expected_revision):
            raise GenerationSettlementRejected("generation preset pin changed")

    @staticmethod
    def _assert_agent_run_receipt_marker(
        agent_run: AgentRun,
        proof: GenerationReceiptProof,
        *,
        expected_proof_hash: str | None = None,
    ) -> None:
        """Require canonical completion refs before accepting a Run replay."""

        if expected_proof_hash not in (None, proof.proof_hash):
            raise GenerationSettlementRejected("canonical AgentRun proof hash changed")
        metadata = _field(agent_run, "run_metadata", {})
        result = _field(agent_run, "result", {})
        if not isinstance(metadata, Mapping) or not isinstance(result, Mapping):
            raise GenerationSettlementRejected(
                "canonical AgentRun receipt metadata is missing"
            )
        expected_outputs = [str(item) for item in proof.output_ids]
        expected_assets = [str(item) for item in proof.asset_revision_ids]
        if metadata.get("receipt_recovered") is not True:
            raise GenerationSettlementRejected(
                "canonical AgentRun receipt marker is missing"
            )
        if metadata.get("generation_receipt_proof_hash") != proof.proof_hash:
            raise GenerationSettlementRejected(
                "canonical AgentRun proof hash marker is missing"
            )
        if metadata.get("work_item_id") != str(proof.work_item_id):
            raise GenerationSettlementRejected(
                "canonical AgentRun Work reference changed"
            )
        if metadata.get("generation_plan_id") != str(proof.plan_id):
            raise GenerationSettlementRejected(
                "canonical AgentRun Plan reference changed"
            )
        if metadata.get("generation_run_id") != str(proof.generation_run_id):
            raise GenerationSettlementRejected(
                "canonical AgentRun generation run reference changed"
            )
        if metadata.get("generation_output_ids") != expected_outputs:
            raise GenerationSettlementRejected(
                "canonical AgentRun output references changed"
            )
        if metadata.get("asset_revision_ids") != expected_assets:
            raise GenerationSettlementRejected(
                "canonical AgentRun asset references changed"
            )
        if result.get("receipt_recovered") is not True:
            raise GenerationSettlementRejected(
                "canonical AgentRun result marker is missing"
            )
        if result.get("proof_hash") != proof.proof_hash:
            raise GenerationSettlementRejected(
                "canonical AgentRun result proof hash changed"
            )
        if result.get("work_item_id") != str(proof.work_item_id):
            raise GenerationSettlementRejected(
                "canonical AgentRun result Work reference changed"
            )
        if result.get("plan_id") != str(proof.plan_id):
            raise GenerationSettlementRejected(
                "canonical AgentRun result Plan reference changed"
            )
        if result.get("generation_run_id") != str(proof.generation_run_id):
            raise GenerationSettlementRejected(
                "canonical AgentRun result generation run reference changed"
            )
        result_outputs = result.get("outputs")
        result_output_ids = (
            [str(item.get("output_id")) for item in result_outputs]
            if isinstance(result_outputs, list)
            and all(isinstance(item, Mapping) for item in result_outputs)
            else None
        )
        if result_output_ids != expected_outputs:
            raise GenerationSettlementRejected(
                "canonical AgentRun result output references changed"
            )
        if result.get("asset_revision_ids") != expected_assets:
            raise GenerationSettlementRejected(
                "canonical AgentRun result asset references changed"
            )

    async def prove_generation_receipt_settlement(
        self,
        session: Any | None = None,
        *,
        work_item_id: UUID | str,
        evidence: GenerationReceiptEvidence,
        expected_proof_hash: str | None = None,
    ) -> GenerationReceiptProof:
        """Revalidate one original generation Work and return a sealed proof.

        The returned object is the only value the parent runtime should pass
        into its canonical terminal settlement.  This method is read-only;
        callers must not treat a success JSON response as a substitute for
        this proof.
        """

        if not isinstance(evidence, GenerationReceiptEvidence) or not evidence.sealed:
            raise GenerationReceiptEvidenceError(
                "generation settlement requires sealed MediaOps evidence"
            )
        own_session = session is None
        active_session = session or await self._session()
        try:
            work_id = _uuid(work_item_id, "work_item_id")
            assert work_id is not None
            work = await self._locked_work(active_session, work_id)
            if _field(work, "source_type") != "media.generation.plan":
                raise GenerationSettlementRejected("only generation Work is eligible")
            settled_replay = (
                str(_field(work, "state")) == "succeeded"
                and str(_field(work, "outcome_classification")) == "succeeded"
            )
            plan_id = _uuid(_field(work, "source_id"), "work.source_id")
            assert plan_id is not None
            plan = await active_session.get(GenerationPlan, plan_id)
            if plan is None:
                raise GenerationSettlementRejected("GenerationPlan is missing")
            if _id(_field(plan, "plan_hash")) != _id(_field(work, "source_revision")):
                raise GenerationSettlementRejected("GenerationPlan hash changed")
            expected_intent_key = _generation_intent_key(
                plan.id,
                str(_field(plan, "plan_hash")),
            )
            if str(_field(work, "intent_key")) != expected_intent_key:
                raise GenerationSettlementRejected("generation Work intent key changed")
            intent = await self._one(
                active_session,
                select(GenerationRunIntent).where(GenerationRunIntent.plan_id == plan.id).limit(1),
            )
            if intent is None:
                raise GenerationSettlementRejected("GenerationRunIntent is missing")
            generation_run = await self._one(
                active_session,
                select(GenerationRun).where(GenerationRun.intent_id == intent.id).limit(1),
            )
            if generation_run is None:
                raise GenerationSettlementRejected("authoritative local GenerationRun is missing")
            agent_run = await self._original_agent_run(active_session, work)
            run_success_replay = (
                not settled_replay
                and str(_field(agent_run, "status") or "").strip().casefold()
                == "succeeded"
            )
            await self._assert_no_external_approval(active_session, work.id)
            workspace = await active_session.get(GenerationWorkspace, plan.workspace_id)
            if workspace is None:
                raise GenerationSettlementRejected("GenerationWorkspace is missing")
            if workspace.external_workspace_id != evidence.workspace_id:
                raise GenerationSettlementRejected(
                    "authoritative receipt workspace scope changed"
                )
            if workspace.external_project_id != evidence.project_id:
                raise GenerationSettlementRejected(
                    "authoritative receipt project scope changed"
                )
            if _field(generation_run, "workspace_id") != plan.workspace_id:
                raise GenerationSettlementRejected("GenerationRun workspace binding changed")
            if _field(generation_run, "owner_user_id") != _field(plan, "owner_user_id"):
                raise GenerationSettlementRejected("GenerationRun owner binding changed")
            if _field(generation_run, "project_id") != _field(plan, "project_id"):
                raise GenerationSettlementRejected("GenerationRun project binding changed")
            if _field(intent, "project_id") != _field(plan, "project_id"):
                raise GenerationSettlementRejected("GenerationRunIntent project binding changed")
            if _field(intent, "owner_user_id") not in (None, _field(plan, "owner_user_id")):
                raise GenerationSettlementRejected("GenerationRunIntent owner binding changed")
            if _field(intent, "external_run_id") not in (None, "", _field(generation_run, "external_run_id")):
                raise GenerationSettlementRejected("GenerationRunIntent external run binding changed")
            self._assert_selector_pin(plan, evidence)
            await self._assert_live_identity_and_authority(
                active_session,
                work=work,
                plan=plan,
                agent_run=agent_run,
                workspace=workspace,
            )
            observations = await self._many(
                active_session,
                select(GenerationRunObservation)
                .where(
                    GenerationRunObservation.generation_run_id == generation_run.id,
                    GenerationRunObservation.status == "succeeded",
                )
                .order_by(GenerationRunObservation.observed_at.desc()),
            )
            observation = next(
                (
                    row
                    for row in observations
                    if _id(_field(row, "observation_hash")) == evidence.response_digest
                ),
                None,
            )
            if observation is None:
                raise GenerationSettlementRejected(
                    "authoritative succeeded observation is missing"
                )
            outputs = await self._many(
                active_session,
                select(GenerationOutput).where(
                    GenerationOutput.generation_run_id == generation_run.id
                ),
            )
            if not outputs:
                raise GenerationSettlementRejected("managed generation outputs are missing")
            plan_owner_id = _field(plan, "owner_user_id")
            plan_project_id = _field(plan, "project_id")
            work_persona_id = _field(work, "persona_id")
            for output in outputs:
                if _field(output, "owner_user_id") != plan_owner_id:
                    raise GenerationSettlementRejected("GenerationOutput owner binding changed")
                if _field(output, "project_id") != plan_project_id:
                    raise GenerationSettlementRejected("GenerationOutput project binding changed")
            asset_revisions = await self._many(
                active_session,
                select(MediaAssetRevision).where(
                    MediaAssetRevision.generation_output_id.in_([row.id for row in outputs])
                ),
            )
            for asset in asset_revisions:
                if _field(asset, "owner_user_id") != plan_owner_id:
                    raise GenerationSettlementRejected("MediaAssetRevision owner binding changed")
                if _field(asset, "project_id") != plan_project_id:
                    raise GenerationSettlementRejected("MediaAssetRevision project binding changed")
                if _field(asset, "persona_id") != work_persona_id:
                    raise GenerationSettlementRejected("MediaAssetRevision Persona binding changed")
            try:
                proof = _bind_generation_receipt_proof(
                    evidence,
                    work=work,
                    agent_run=agent_run,
                    plan=plan,
                    intent=intent,
                    generation_run=generation_run,
                    observation=observation,
                    outputs=outputs,
                    asset_revisions=asset_revisions,
                    allow_settled_replay=settled_replay,
                    allow_run_success_replay=run_success_replay,
                )
            except GenerationReceiptProofError:
                raise
            except Exception as exc:
                raise GenerationSettlementRejected(
                    "generation receipt proof binding failed"
                ) from exc
            if str(_field(agent_run, "status") or "").strip().casefold() == "succeeded":
                self._assert_agent_run_receipt_marker(
                    agent_run,
                    proof,
                    expected_proof_hash=expected_proof_hash,
                )
            elif expected_proof_hash not in (None, ""):
                raise GenerationSettlementRejected(
                    "expected canonical AgentRun proof is not terminal"
                )
            return proof
        finally:
            if own_session:
                try:
                    await active_session.rollback()
                finally:
                    await active_session.close()

    async def _append_generation_success_event(
        self,
        session: Any,
        *,
        work: AgentWorkItem,
        proof: GenerationReceiptProof,
    ) -> AgentWorkEvent:
        """Append one bounded Work success event inside the Work transaction."""

        max_sequence = await session.scalar(
            select(func.max(AgentWorkEvent.sequence)).where(
                AgentWorkEvent.work_item_id == work.id
            )
        )
        refs = proof.to_dict()
        event = AgentWorkEvent(
            id=uuid4(),
            work_item_id=work.id,
            sequence=int(max_sequence or 0) + 1,
            event_type="work.succeeded",
            from_state="awaiting_approval",
            to_state="succeeded",
            status=None,
            actor_kind="service",
            actor_id="aoitalk.media-adapter",
            actor_service_key="aoitalk.media-adapter",
            agent_run_id=proof.agent_run_id,
            causation_id=work.causation_id,
            causal_depth=int(work.causal_depth or 0),
            mutation_fingerprint=proof.proof_hash,
            safe_error_code=None,
            message=None,
            result_summary=(
                "Generation receipt recovered from the authoritative managed ledger"
            ),
            payload_json={
                "classification": "succeeded",
                "receipt_recovered": True,
                "plan_id": str(proof.plan_id),
                "intent_id": str(proof.intent_id),
                "generation_run_id": str(proof.generation_run_id),
                "output_ids": [str(item) for item in proof.output_ids],
                "asset_revision_ids": [
                    str(item) for item in proof.asset_revision_ids
                ],
                "proof_hash": proof.proof_hash,
                "result_refs": refs,
            },
            created_at=datetime.utcnow(),
        )
        session.add(event)
        return event

    async def _canonical_complete_agent_run(
        self,
        proof: GenerationReceiptProof,
    ) -> dict[str, Any]:
        """Complete the same AgentRun through the canonical run service.

        The caller must invoke this after the Work transaction has committed,
        because the canonical service owns a separate session and row lock.
        ``agent_run_service`` is intentionally injectable for the parent
        BaseAgentRunService adapter; no run-service implementation is changed
        in this module.
        """

        service = self.agent_run_service
        if service is None:
            try:
                from .agent_run_service import AgentRunService

                service = AgentRunService(self._db_manager)
            except Exception as exc:
                raise GenerationSettlementRejected(
                    "canonical AgentRun service is unavailable"
                ) from exc
        complete = getattr(service, "complete_run", None)
        if not callable(complete):
            raise GenerationSettlementRejected(
                "canonical AgentRun service has no complete_run method"
            )
        refs = proof.to_dict()
        metadata = {
            "work_item_id": str(proof.work_item_id),
            "classification": "succeeded",
            "domain_ref": str(proof.plan_id),
            "domain_status": "succeeded",
            "receipt_recovered": True,
            "generation_receipt_proof_hash": proof.proof_hash,
            "generation_plan_id": str(proof.plan_id),
            "generation_run_id": str(proof.generation_run_id),
            "generation_output_ids": [str(item) for item in proof.output_ids],
            "asset_revision_ids": [
                str(item) for item in proof.asset_revision_ids
            ],
        }
        try:
            result = complete(
                str(proof.agent_run_id),
                result=refs,
                message=(
                    "Generation receipt recovered from the authoritative managed ledger"
                ),
                metadata=metadata,
            )
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            raise GenerationSettlementRejected(
                "canonical AgentRun completion failed"
            ) from exc
        if not isinstance(result, Mapping):
            raise GenerationSettlementRejected(
                "canonical AgentRun completion returned no durable result"
            )
        status = str(result.get("status") or "").strip().casefold()
        if status != "succeeded":
            raise GenerationSettlementRejected(
                "canonical AgentRun completion did not produce succeeded status"
            )
        return dict(result)

    async def settle_generation_receipt(
        self,
        session: Any | None = None,
        *,
        work_item_id: UUID | str,
        evidence: GenerationReceiptEvidence,
    ) -> dict[str, Any]:
        """Settle one already-proven generation Work and its same AgentRun.

        This is the sole mutation API in this service.  It is generation-only,
        idempotent, preserves all prior events, and never creates a Work item,
        claim, lease, provider call, or human approval.  The initial proof
        transaction commits only to release its row lock; canonical AgentRun
        completion happens first.  A fresh transaction then revalidates the
        same proof/Run metadata before committing Work success.  Therefore a
        canonical completion failure leaves Work awaiting approval, while a
        process stop after Run success can replay the exact proof without
        appending another Work event.
        """

        first_own_session = session is None
        first_session = session or await self._session()
        proof = None
        first_run_status = ""
        first_work_was_settled = False
        try:
            proof = await self.prove_generation_receipt_settlement(
                first_session,
                work_item_id=work_item_id,
                evidence=evidence,
            )
            work = await self._locked_work(
                first_session,
                _uuid(work_item_id, "work_item_id"),
            )
            if any(
                _field(work, name) not in (None, "")
                for name in ("lease_owner", "lease_token", "lease_expires_at")
            ):
                raise GenerationSettlementRejected(
                    "generation Work acquired a lease before settlement"
                )
            agent_run = await self._original_agent_run(first_session, work)
            first_run_status = str(
                _field(agent_run, "status") or ""
            ).strip().casefold()
            work_state = str(_field(work, "state") or "").strip().casefold()
            work_classification = str(
                _field(work, "outcome_classification") or ""
            ).strip().casefold()
            first_work_was_settled = (
                work_state == "succeeded" and work_classification == "succeeded"
            )
            # No Work mutation occurs in this phase. Commit explicitly so a
            # caller-supplied session releases the Work row lock before the
            # canonical AgentRun service opens its own session.
            await first_session.commit()
        except Exception:
            if first_own_session:
                await first_session.rollback()
                await first_session.close()
            raise

        assert proof is not None
        if first_own_session:
            await first_session.close()

        run_result = None
        if first_run_status != "succeeded":
            # Work remains awaiting approval while the canonical Run CAS is
            # attempted. A failed completion therefore leaves a retryable,
            # honest Work state instead of a false success.
            run_result = await self._canonical_complete_agent_run(proof)

        final_session = await self._session()
        final_proof: GenerationReceiptProof | None = None
        final_work_was_settled = False
        final_run_status = ""
        try:
            final_proof = await self.prove_generation_receipt_settlement(
                final_session,
                work_item_id=work_item_id,
                evidence=evidence,
                expected_proof_hash=proof.proof_hash,
            )
            final_work = await self._locked_work(
                final_session,
                _uuid(work_item_id, "work_item_id"),
            )
            final_run = await self._original_agent_run(final_session, final_work)
            final_run_status = str(
                _field(final_run, "status") or ""
            ).strip().casefold()
            if final_run_status != "succeeded":
                raise GenerationSettlementRejected(
                    "canonical AgentRun is not durably succeeded"
                )
            final_work_state = str(
                _field(final_work, "state") or ""
            ).strip().casefold()
            final_work_classification = str(
                _field(final_work, "outcome_classification") or ""
            ).strip().casefold()
            final_work_was_settled = (
                final_work_state == "succeeded"
                and final_work_classification == "succeeded"
            )
            if not final_work_was_settled:
                refs = final_proof.to_dict()
                now = datetime.utcnow()
                final_work.state = "succeeded"
                final_work.outcome_classification = "succeeded"
                final_work.not_before = None
                final_work.next_attempt_at = None
                final_work.lease_owner = None
                final_work.lease_token = None
                final_work.lease_expires_at = None
                final_work.heartbeat_at = now
                final_work.completed_at = now
                final_work.active_agent_run_id = None
                final_work.safe_error_code = None
                final_work.blocker_code = None
                final_work.escalation_reason = None
                final_work.result_summary = (
                    "Generation receipt recovered from the authoritative managed ledger"
                )
                final_work.result_summary_json = refs
                metadata = (
                    dict(final_work.metadata_json)
                    if isinstance(final_work.metadata_json, Mapping)
                    else {}
                )
                metadata["generation_receipt_recovered"] = {
                    "receipt_recovered": True,
                    "plan_id": str(final_proof.plan_id),
                    "intent_id": str(final_proof.intent_id),
                    "generation_run_id": str(final_proof.generation_run_id),
                    "output_ids": [str(item) for item in final_proof.output_ids],
                    "asset_revision_ids": [
                        str(item) for item in final_proof.asset_revision_ids
                    ],
                    "proof_hash": final_proof.proof_hash,
                }
                final_work.metadata_json = metadata
                await self._append_generation_success_event(
                    final_session,
                    work=final_work,
                    proof=final_proof,
                )
            await final_session.commit()
        except Exception:
            await final_session.rollback()
            raise
        finally:
            await final_session.close()

        assert final_proof is not None
        if run_result is None:
            run_result = {
                "id": str(final_proof.agent_run_id),
                "status": "succeeded",
                "result": final_proof.to_dict(),
            }
        return {
            "settled": True,
            "idempotent": bool(
                final_work_was_settled
                and first_work_was_settled
                and first_run_status == "succeeded"
            ),
            "state": "succeeded",
            "work_item_id": str(final_proof.work_item_id),
            "agent_run_id": str(final_proof.agent_run_id),
            "proof": final_proof.to_dict(),
            "result": self.settlement_result(final_proof),
            "run": run_result,
        }

    @staticmethod
    def settlement_result(proof: GenerationReceiptProof) -> dict[str, Any]:
        """Return bounded refs for canonical Work/AgentRun settlement."""

        if (
            not isinstance(proof, GenerationReceiptProof)
            or not proof.sealed
            or not proof.integrity_valid()
        ):
            raise GenerationReceiptProofError("settlement requires a sealed proof")
        refs = proof.to_dict()
        return {
            "classification": "succeeded",
            "status": "succeeded",
            "domain_ref": str(proof.plan_id),
            "domain_status": "succeeded",
            "reason_code": "generation_receipt_recovered",
            "result_summary": "Generation receipt recovered from the authoritative managed ledger",
            "result": refs,
            "evidence": [
                {
                    "kind": "generation_receipt_recovered",
                    "plan_id": str(proof.plan_id),
                    "generation_run_id": str(proof.generation_run_id),
                    "output_ids": [str(item) for item in proof.output_ids],
                    "asset_revision_ids": [str(item) for item in proof.asset_revision_ids],
                    "proof_hash": proof.proof_hash,
                }
            ],
        }


__all__ = [
    "AgentWorkService",
    "AgentWorkServiceError",
    "GenerationSettlementAuthorizationError",
    "GenerationSettlementRejected",
]
