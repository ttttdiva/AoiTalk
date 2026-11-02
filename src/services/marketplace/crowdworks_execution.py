"""Shared CrowdWorks generation and fulfillment execution."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from ...memory.models.operations import EngagementOpportunity
from ..agent_model_routing import AgentModelRoute, AgentRoutingPins, resolve_agent_role_route
from ..ephemeral_llm import (EphemeralGenerationFailure, EphemeralGenerationStopped, agent_work_generation_timeout, generate_ephemeral_text_with_llm_client)
from ..operations_service import OperationsService
from ..session_llm_generation import cleanup_ephemeral_llm_client
from .crowdworks_action import CrowdWorksPolicy
from .crowdworks_application_qa import REVIEW_ROLE, _crowdworks_evaluation_fallback_settings, _draft_provider_diagnostic
from .crowdworks_specialists import GENERAL_WORKER_KINDS, is_development_kind, normalize_worker_kind


def _crowdworks_budget_fallback_route(
    primary_route: AgentModelRoute,
    *,
    fallback_enabled: bool,
    fallback_model: str,
) -> AgentModelRoute | None:
    if (
        not fallback_enabled
        or getattr(primary_route, "provider", None) != "openai"
        or str(getattr(primary_route, "model", None) or "").strip() != "gpt-5.6-sol"
        or fallback_model == getattr(primary_route, "model", None)
    ):
        return None
    return AgentModelRoute(
        **{
            **primary_route.__dict__,
            "model": fallback_model,
            "notes": (*primary_route.notes, "crowdworks_budget_fallback_luna"),
        }
    )


WORKER_KIND_ROLES = {
    "writing": "writer",
    "research": "researcher",
    "data_entry": "data_worker",
    "code": "code_worker",
    "general": "general_worker",
}


@dataclass
class FulfillmentResult:
    artifact: dict[str, Any]
    model_routes: dict[str, dict[str, Any]]


class CrowdWorksExecutionService:
    def __init__(self, *, config: Any = None, operations: OperationsService | None = None):
        self.config = config
        self.operations = operations or OperationsService()
        self.last_model_generation: dict[str, Any] = {}
        self.last_unit_context: dict[str, str | None] = {"kind": "none", "opportunity_id": None}
        self.last_llm_unit_timeout_callback: Callable[[str], None] | None = None
        self.last_draft_provider_subcode: str | None = None
        self.last_draft_provider_diagnostic: dict[str, Any] | None = None
        self.last_draft_call_context: dict[str, Any] | None = None
        self.last_qa_route_model: str | None = None
        self.last_qa_route_fallback: str | None = None
        self.last_qa_failure_code_counts: dict[str, int] = {}
        self.last_qa_duration_ms = 0
        self.last_qa_attempt_count = 0
        self.last_qa_attempts_this_tick = 0
        self.last_qa_route_provenance: dict[str, Any] | None = None
        self.last_draft_circuit_open_reason_code: str | None = None
        self.last_generation_route: AgentModelRoute | None = None
        self.last_generation_route_fallback = "none"

    def _phase_route(
        self,
        pins: AgentRoutingPins,
        role: str,
        *,
        policy: CrowdWorksPolicy | None = None,
    ) -> AgentModelRoute:
        """Use the exact revision's route, including explicit Main inheritance.

        Legacy policy model fields are never execution authority. Applying
        them after a run snapshot was resolved would invalidate its pins.
        """

        route = resolve_agent_role_route(self.config, pins, role)
        if policy is not None and (policy.llm_provider or policy.llm_model):
            return AgentModelRoute(
                **{**route.__dict__, "notes": (*route.notes, "legacy_policy_override_ignored")}
            )
        return route

    async def _llm(
        self,
        prompt: str,
        *,
        system_prompt: str,
        route: AgentModelRoute,
        owner_user_id: str,
        project_id: Any,
        propagate_errors: bool = False,
        max_output_tokens: int | None = None,
        max_requests: int | None = None,
        track_request_count: bool = False,
    ) -> str:
        """Generate with an explicit Agent route; never the global Base Model."""

        from ...llm.manager import create_llm_client_for_target

        self.last_model_generation = {"generation": {"status": "failed"}}
        if not isinstance(route, AgentModelRoute):
            raise RuntimeError("crowdworks_model_route_missing")
        timeout_callback = self.last_llm_unit_timeout_callback
        unit_kind = self.last_unit_context.get("kind")
        if callable(timeout_callback) and unit_kind in {"evaluation", "draft"}:
            timeout_callback(unit_kind)
        self.last_model_generation = {**route.provenance(), "generation": {"status": "failed"}}
        provider_options = {"lightweight_client": True}
        if max_output_tokens is not None:
            provider_options["max_output_tokens"] = max(1, int(max_output_tokens))
        client = create_llm_client_for_target(
            self.config,
            provider=route.provider,
            model=route.model,
            effort=route.effort or "",
            provider_options=provider_options,
        )
        # This client belongs to one call, so concurrent WorkItems never share
        # mutable attribution. The owner/project come from the authorized
        # workflow; the coordinator's task-local Run context stays intact.
        client.session_user_id = str(owner_user_id)
        client.current_project_id = str(project_id) if project_id is not None else None
        client.character_name = route.acting_subagent_id
        client._capture_ephemeral_usage_for_caller = True
        client._captured_ephemeral_usage = None
        generation: dict[str, Any] = {"status": "failed"}
        try:
            value = await generate_ephemeral_text_with_llm_client(
                client,
                prompt,
                system_prompt=system_prompt,
                timeout_seconds=agent_work_generation_timeout(self.config),
                provenance=generation,
                max_requests=max_requests,
                track_request_count=track_request_count,
                raise_on_error=propagate_errors,
            )
            if generation.get("status") in {"timed_out", "cancelled"}:
                raise EphemeralGenerationStopped(generation["status"])
            return str(value or "")
        finally:
            usage = getattr(client, "_captured_ephemeral_usage", None)
            if isinstance(usage, dict):
                generation.update(usage)
            else:
                generation.update({
                    "usage_present": False,
                    "usage_input_units": None,
                    "usage_output_units": None,
                    "usage_total_units": None,
                    "usage_total_source": None,
                })
            self.last_model_generation = {**route.provenance(), "generation": generation}
            await cleanup_ephemeral_llm_client(client)

    async def _llm_with_crowdworks_budget_fallback(
        self,
        prompt: str,
        *,
        system_prompt: str,
        route: AgentModelRoute,
        owner_user_id: str,
        project_id: Any,
        max_output_tokens: int | None = None,
    ) -> tuple[str, AgentModelRoute, str]:
        """Retry one pinned OpenAI sol call with luna only for Free Team budget denial."""
        started_at = time.perf_counter()
        self.last_generation_route = route
        self.last_generation_route_fallback = "none"
        self.last_draft_provider_diagnostic = None

        def remember_call_context(
            active_route: AgentModelRoute,
            route_fallback: str,
        ) -> None:
            self.last_draft_call_context = {
                "route": active_route,
                "route_fallback": route_fallback,
                "request_char_count": min(
                    max(0, len(prompt) + len(system_prompt)), 1_000_000
                ),
                "max_output_tokens": max_output_tokens,
                "duration_ms": max(
                    0, round((time.perf_counter() - started_at) * 1000)
                ),
            }

        try:
            try:
                value = await self._llm(
                    prompt,
                    system_prompt=system_prompt,
                    route=route,
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    propagate_errors=True,
                    max_output_tokens=max_output_tokens,
                )
                remember_call_context(route, "none")
                return value, route, "none"
            except EphemeralGenerationFailure as exc:
                if (
                    exc.category != "provider_unavailable"
                    or exc.code != "free_team_budget_insufficient"
                ):
                    raise
                enabled, fallback_model = _crowdworks_evaluation_fallback_settings(self.config)
                fallback_route = _crowdworks_budget_fallback_route(
                    route,
                    fallback_enabled=enabled,
                    fallback_model=fallback_model,
                )
                if fallback_route is None:
                    raise
                self.last_generation_route = fallback_route
                self.last_generation_route_fallback = "luna"
                value = await self._llm(
                    prompt,
                    system_prompt=system_prompt,
                    route=fallback_route,
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    propagate_errors=True,
                    max_output_tokens=max_output_tokens,
                )
                remember_call_context(fallback_route, "luna")
                return value, fallback_route, "luna"
        except Exception as exc:
            active_route = self.last_generation_route or route
            active_fallback = self.last_generation_route_fallback
            remember_call_context(active_route, active_fallback)
            self.last_draft_provider_diagnostic = _draft_provider_diagnostic(
                exc,
                prompt=prompt,
                system_prompt=system_prompt,
                route=active_route,
                route_fallback=active_fallback,
                max_output_tokens=max_output_tokens,
                duration_ms=round((time.perf_counter() - started_at) * 1000),
            )
            raise

    async def fulfill_opportunity(
        self,
        session,
        *,
        owner_user_id: str,
        opportunity_id,
        worker_kind: str,
        pins: AgentRoutingPins | None = None,
        work_item_id: str | None = None,
    ) -> "FulfillmentResult":
        opportunity = await session.get(EngagementOpportunity, UUID(str(opportunity_id)))
        if opportunity is None or opportunity.status != "contract_accepted":
            raise RuntimeError("crowdworks_opportunity_not_ready_for_fulfillment")
        metadata = dict(opportunity.metadata_json or {})
        selected_kind = normalize_worker_kind(worker_kind or metadata.get("worker_kind"))
        if is_development_kind(selected_kind):
            # Web/App development is fulfilled by the specialist development
            # lane with a real workspace, never by a Markdown-only draft.
            raise RuntimeError("crowdworks_development_requires_specialist_lane")
        if selected_kind not in GENERAL_WORKER_KINDS:
            selected_kind = "general"
        # Resolve both routes before any provider call so an invalid
        # Team/Profile/Subagent pin fails closed without partial work.
        if pins is None:
            raise RuntimeError("crowdworks_model_route_missing")
        worker_route = self._phase_route(pins, WORKER_KIND_ROLES[selected_kind])
        review_route = self._phase_route(pins, REVIEW_ROLE)
        instructions = {
            "writing": "完成原稿を作成し、見出し・本文・必要なら注記までレビュー可能な形にする。",
            "research": "調査結果、根拠、未確認点、推奨する追加確認を分けて整理する。",
            "data_entry": "入力・変換ルール、処理済みデータ、検算ポイントを再確認可能な形にする。",
            "general": "案件本文に最も適した完成度の高い成果物ドラフトを作る。",
        }
        prompt = (
            "受注済みCrowdWorks案件の成果物を、人間の最終レビュー直前まで作成してください。"
            "最終納品操作は行わず、外部で実行していない事実・テスト・調査結果を捏造しないでください。"
            f"案件種別: {selected_kind}. 指示: {instructions[selected_kind]} "
            f"タイトル: {opportunity.title}. 案件情報: {opportunity.source_text or ''}"
        )
        content = await self._llm(
            prompt,
            system_prompt=(
                "Create a review-ready work product. State missing inputs explicitly. "
                "Do not claim external actions or verification that did not occur."
            ),
            route=worker_route,
            owner_user_id=owner_user_id,
            project_id=opportunity.project_id,
        )
        worker_provenance = self.last_model_generation or worker_route.provenance()
        if not content.strip():
            raise RuntimeError("crowdworks_fulfillment_empty")
        review_prompt = (
            "以下はCrowdWorks案件の成果物ドラフトです。案件要件に照らしてレビューし、"
            "要件の充足状況、誤り・不足、捏造の疑い、人間が最終確認すべき点を箇条書きで簡潔に示してください。"
            "成果物を書き直さず、実行していない検証を実施済みと書かないでください。"
            f"タイトル: {opportunity.title}. 案件情報: {(opportunity.source_text or '')[:16000]}\n"
            f"成果物ドラフト:\n{content[:48000]}"
        )
        review_status = "completed"
        try:
            review = await self._llm(
                review_prompt,
                system_prompt=(
                    "Review a work product against the client's requirements. "
                    "Be concrete and concise. Do not rewrite the deliverable."
                ),
                route=review_route,
                owner_user_id=owner_user_id,
                project_id=opportunity.project_id,
            )
        except Exception:
            review = ""
        if not review.strip():
            review_status = "unavailable"
            review = "AIレビューを取得できませんでした。人間の最終確認で要件充足を確認してください。"
        document = (
            content.rstrip()
            + "\n\n---\n\n## AIレビュー（人間の最終確認前）\n\n"
            + review.strip()
            + "\n"
        )
        model_routes = {
            "fulfillment": worker_provenance,
            "review": self.last_model_generation or review_route.provenance(),
        }
        actor = {"id": owner_user_id, "actor_type": "system"}
        artifact = await self.operations.create_artifact(
            session,
            actor,
            content=document.encode("utf-8"),
            filename=f"crowdworks-{metadata.get('remote_id') or opportunity.id}-review.md",
            mime_type="text/markdown",
            project_id=opportunity.project_id,
            provenance={
                "provider": "crowdworks",
                "opportunity_id": str(opportunity.id),
                "worker_kind": selected_kind,
                "review_status": "READY_FOR_REVIEW",
                "ai_review_status": review_status,
                "model_routes": model_routes,
            },
            _defer_commit=True,
        )
        metadata["worker_kind"] = selected_kind
        metadata["workflow_stage"] = "READY_FOR_REVIEW"
        metadata["review_artifact_version_id"] = artifact["id"]
        metadata["ai_review_status"] = review_status
        metadata["model_routes"] = {**dict(metadata.get("model_routes") or {}), **model_routes}
        metadata["review_artifact_version_ids"] = [artifact["id"]]
        # Publish the artifact, review state and producing Work binding in one
        # transaction. A restart must not expose an orphaned product or make
        # the producing claim fail the source's settlement refresh.
        metadata["review_work_item_id"] = str(work_item_id or "") or None
        opportunity.metadata_json = metadata
        opportunity.status = "ready_for_review"
        await session.commit()
        return FulfillmentResult(artifact=artifact, model_routes=model_routes)
