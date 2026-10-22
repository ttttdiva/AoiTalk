"""CrowdWorks scan/evaluate/apply/work workflow on existing Operations primitives."""

from __future__ import annotations

import json
import math
from contextlib import AsyncExitStack
from functools import partial
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from ...memory.models.operations import (
    ApplicationDraft,
    EngagementOpportunity,
    ExternalAction,
    OpportunityEvaluation,
)
from ..agent_model_routing import (
    AgentModelRoute,
    AgentRoutingPins,
    resolve_agent_role_route,
)
from ..ephemeral_llm import (
    EphemeralGenerationStopped,
    agent_work_generation_timeout,
    generate_ephemeral_text_with_llm_client,
)
from ..integration_action_registry import ActionPolicyError
from ..operations_service import OperationsService, canonical_json
from ..operations_text import (
    EVALUATION_COLLECTION_MAX_BYTES, bounded_operation_text, model_text, model_text_list,
)
from ..pc_bridge_service import RemoteControlBridge, get_pc_bridge_hub
from ..session_llm_generation import cleanup_ephemeral_llm_client
from .bridge_inspection import BridgeInspectionLease
from .crowdworks_action import CrowdWorksPolicy
from .crowdworks_source import CrowdWorksSource
from .crowdworks_specialist_assessment import (
    assess_development_opportunities,
    assessment_blocks_application,
)
from .crowdworks_specialists import (
    GENERAL_WORKER_KINDS,
    WORKER_LABELS,
    is_development_kind,
    normalize_worker_kind,
)


_ESCROW_CONFIRMED_MARKERS = ("仮払い完了", "仮払い済み", "支払い済み", "支払済み", "エスクロー完了")
_ESCROW_NEGATIVE_MARKERS = ("仮払い待ち", "仮払い未完了", "支払い待ち", "支払待ち", "未払い", "エスクロー待ち")
_CONTRACT_MARKERS = ("契約成立", "契約中")
_ACTIVE_WORK_MARKERS = ("作業中", "業務開始")
_PAYMENT_CONTEXT_MARKERS = ("仮払い", "エスクロー", "支払い", "支払")
_BATCH_SIZE = 10


# CrowdWorks worker_kind -> shared Operations work role.  The role decides
# which Subagent of the pinned Agent Team does the work (and therefore which
# Execution Profile route / model is used); it is not only a prompt switch.
WORKER_KIND_ROLES = {
    "writing": "writer",
    "research": "researcher",
    "data_entry": "data_worker",
    "code": "code_worker",
    "general": "general_worker",
}


def crowdworks_contract_stage(page_text: Any) -> str | None:
    """Classify contract/escrow state without treating contract as payment.

    CrowdWorks' inspector currently returns the main-document text rather than
    a stable structured payment field.  Explicit negative escrow text wins;
    active-work labels are never payment evidence.  Only an explicit
    affirmative escrow marker can confirm payment; ambiguous or generic
    payment-condition text remains pending.
    """

    text = str(page_text or "").replace("\u3000", " ").strip()
    if not text:
        return None
    if any(marker in text for marker in _ESCROW_NEGATIVE_MARKERS):
        return "escrow_pending"
    if any(marker in text for marker in _ESCROW_CONFIRMED_MARKERS):
        return "escrow_confirmed"
    if any(marker in text for marker in _ACTIVE_WORK_MARKERS):
        # "作業中"/"業務開始" and generic payment-condition wording are
        # not affirmative escrow evidence.  The explicit markers above are
        # the sole confirmation path.
        return "escrow_pending"
    if any(marker in text for marker in _CONTRACT_MARKERS):
        return "escrow_pending"
    if any(marker in text for marker in _PAYMENT_CONTEXT_MARKERS):
        # Payment-related context such as terms, instructions, or a method
        # description is not proof that escrow was actually funded.
        return "escrow_pending"
    return None
# Evaluation / requirement analysis and application proposals are "thinking"
# phases; the final AI review before human review is the reviewer role.
EVALUATION_ROLE = "planner"
APPLICATION_ROLE = "planner"
REVIEW_ROLE = "reviewer"


def _card_summary_lines(
    item: dict[str, Any],
    *,
    summary: str,
    risks: list[Any],
    missing_requirements: list[Any],
    effort: float,
    worker_kind: str,
) -> list[str]:
    raw = item.get("card_summary_lines")
    lines = [
        str(value).replace("\n", " ").strip()[:120]
        for value in (raw if isinstance(raw, list) else [])
        if str(value).strip()
    ][:3]
    fallback = [
        f"仕事内容: {summary[:100]}",
        f"AI実行: {WORKER_LABELS.get(worker_kind, '一般作業')}として約{effort:g}時間を想定",
        "注意点: "
        + str(
            (risks[0] if risks else None)
            or (missing_requirements[0] if missing_requirements else None)
            or "現時点で大きな懸念なし"
        )[:100],
    ]
    while len(lines) < 3:
        lines.append(fallback[len(lines)])
    return lines[:3]


def _extract_json_array(text: str | None) -> list[dict[str, Any]]:
    if not text:
        return []
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "[":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(value, list):
            rows = [item for item in value if isinstance(item, dict)]
            if rows:
                return rows
    return []


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


@dataclass
class WorkflowResult:
    ingested: int = 0
    evaluated: int = 0
    drafted: int = 0
    proposed: int = 0
    accepted: int = 0
    ready_for_review: int = 0
    proposal_deferred: int = 0
    proposal_deferred_reason: str | None = None
    model_routes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ingested": self.ingested,
            "evaluated": self.evaluated,
            "drafted": self.drafted,
            "proposed": self.proposed,
            "accepted": self.accepted,
            "ready_for_review": self.ready_for_review,
            "proposal_deferred": self.proposal_deferred,
            "proposal_deferred_reason": self.proposal_deferred_reason,
        }


@dataclass
class FulfillmentResult:
    artifact: dict[str, Any]
    model_routes: dict[str, dict[str, Any]]


class CrowdWorksWorkflowService:
    def __init__(self, *, config: Any = None, operations: OperationsService | None = None):
        self.config = config
        self.operations = operations or OperationsService()
        self.source = CrowdWorksSource(operations=self.operations)
        self.last_model_generation: dict[str, Any] = {}
        self.proposal_deferred = 0

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
    ) -> str:
        """Generate with an explicit Agent route; never the global Base Model."""

        from ...llm.manager import create_llm_client_for_target

        if not isinstance(route, AgentModelRoute):
            raise RuntimeError("crowdworks_model_route_missing")
        self.last_model_generation = {**route.provenance(), "generation": {"status": "failed"}}
        client = create_llm_client_for_target(
            self.config,
            provider=route.provider,
            model=route.model,
            effort=route.effort or "",
            provider_options={"lightweight_client": True},
        )
        # This client belongs to one call, so concurrent WorkItems never share
        # mutable attribution. The owner/project come from the authorized
        # workflow; the coordinator's task-local Run context stays intact.
        client.session_user_id = str(owner_user_id)
        client.current_project_id = str(project_id) if project_id is not None else None
        client.character_name = route.acting_subagent_id
        generation: dict[str, Any] = {"status": "failed"}
        try:
            value = await generate_ephemeral_text_with_llm_client(
                client,
                prompt,
                system_prompt=system_prompt,
                timeout_seconds=agent_work_generation_timeout(self.config),
                provenance=generation,
            )
            if generation.get("status") in {"timed_out", "cancelled"}:
                raise EphemeralGenerationStopped(generation["status"])
            return str(value or "")
        finally:
            self.last_model_generation = {**route.provenance(), "generation": generation}
            await cleanup_ephemeral_llm_client(client)

    async def fulfill_opportunity(
        self,
        session,
        *,
        owner_user_id: str,
        opportunity_id,
        worker_kind: str,
        pins: AgentRoutingPins | None = None,
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
        )
        metadata["worker_kind"] = selected_kind
        metadata["workflow_stage"] = "READY_FOR_REVIEW"
        metadata["review_artifact_version_id"] = artifact["id"]
        metadata["ai_review_status"] = review_status
        metadata["model_routes"] = {**dict(metadata.get("model_routes") or {}), **model_routes}
        metadata["review_artifact_version_ids"] = [artifact["id"]]
        opportunity.metadata_json = metadata
        opportunity.status = "ready_for_review"
        await session.commit()
        return FulfillmentResult(artifact=artifact, model_routes=model_routes)

    async def _evaluate_batch(
        self,
        session,
        *,
        owner_user_id: str,
        connection_id,
        project_id,
        policy: CrowdWorksPolicy,
        pins: AgentRoutingPins,
    ) -> list[tuple[EngagementOpportunity, dict[str, Any]]]:
        opportunities = (
            await session.scalars(
                select(EngagementOpportunity)
                .where(
                    EngagementOpportunity.connection_id == connection_id,
                    EngagementOpportunity.status == "open",
                )
                .order_by(EngagementOpportunity.created_at.desc())
                .limit(min(1000, policy.max_opportunities_per_scan * 10))
            )
        ).all()
        pending: list[EngagementOpportunity] = []
        scored: list[tuple[EngagementOpportunity, dict[str, Any]]] = []
        for row in opportunities:
            metadata = dict(row.metadata_json or {})
            existing = await session.scalar(
                select(OpportunityEvaluation.id)
                .where(OpportunityEvaluation.opportunity_id == row.id)
                .limit(1)
            )
            if existing is None or metadata.get("workflow_stage") not in {"EVALUATED", "APPLICATION_PROPOSED"}:
                if len(pending) < policy.max_opportunities_per_scan:
                    pending.append(row)
                continue
            # Reuse persisted economics across cycles. Application copy is
            # generated only for the final top-ranked shortlist below.
            if metadata.get("workflow_stage") == "APPLICATION_PROPOSED":
                continue
            scored.append((row, {}))

        actor = {"id": owner_user_id, "actor_type": "system"}
        route = self._phase_route(pins, EVALUATION_ROLE, policy=policy) if pending else None
        for offset in range(0, len(pending), _BATCH_SIZE):
            batch = pending[offset : offset + _BATCH_SIZE]
            items = []
            for row in batch:
                metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
                items.append(
                    {
                        "id": str(row.id),
                        "title": row.title,
                        "url": row.source_url,
                        "listing_text": (row.source_text or "")[:16000],
                        "listed_compensation_yen": metadata.get("compensation_yen"),
                        "compensation_type": metadata.get("compensation_type"),
                    }
                )
            schema_instruction = (
                "出力はJSON配列のみ。各要素は必ず id, estimated_effort_hours, estimated_cost, "
                "estimated_revenue, fit, risks, missing_requirements, summary, card_summary_lines, "
                "worker_kind, confidence, expected_human_minutes, estimated_compute_cost, estimated_api_cost, "
                "estimated_gpu_minutes, automation_reuse_score を持つこと。card_summary_linesは日本語3要素の配列で、"
                "1行目=仕事内容、2行目=AIでの進め方、3行目=注意点を各120文字以内で簡潔に書くこと。"
                "estimated_effort_hoursは0より大きい数値、estimated_revenueは案件全体で見込む売上額、"
                "fitとsummaryは空文字禁止。worker_kindは writing/research/data_entry/web_development/app_development/general のいずれか。"
                "web_developmentはLP・コーポレートサイト・HTML/CSS/JS・React/Next.js等のフロントエンド・WordPress・既存サイト修正・"
                "レスポンシブ対応・フォーム等のWebサイト制作。app_developmentはWebアプリ・業務ツール・API・バックエンド・DB・"
                "デスクトップ/モバイルアプリ・自動化ツール等のプログラム開発。両方にまたがる場合はapp_development。"
                "confidenceは0..1。"
            )
            prompt = (
                "以下のCrowdWorks案件を、AoiTalkで自動化して利益を最大化する観点から一括評価してください。"
                "各案件について、現実的な工数・直接コスト・売上・適合度・リスク・不足条件を推定してください。"
                "まだ応募文は作らないでください。危険/規約違反/情報不足は明示してください。"
                + schema_instruction
                + f"通貨既定値は {policy.currency}。案件: "
                + json.dumps(items, ensure_ascii=False)
            )
            raw = await self._llm(
                prompt,
                system_prompt=(
                    "You evaluate marketplace work economically. Return strict JSON only. "
                    "Never invent client credentials or claim work is completed."
                ),
                route=route,
                owner_user_id=owner_user_id,
                project_id=project_id,
            )
            parsed = _extract_json_array(raw)
            if not parsed:
                retry_prompt = (
                    "前回の出力をJSONとして解釈できませんでした。説明・Markdown・コードフェンスを付けず、"
                    "次の案件だけを再評価してください。" + schema_instruction
                    + f"通貨既定値は {policy.currency}。案件: "
                    + json.dumps(items, ensure_ascii=False)
                )
                raw = await self._llm(
                    retry_prompt,
                    system_prompt="Return one strict JSON array only. No prose or markdown.",
                    route=route,
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                )
                parsed = _extract_json_array(raw)
            if not parsed:
                # Leave this batch pending for the next scheduled cycle instead
                # of dead-lettering the entire marketplace scan.
                continue
            by_id = {str(item.get("id")): item for item in parsed}
            for row in batch:
                item = by_id.get(str(row.id))
                if not item:
                    continue
                effort = _finite(item.get("estimated_effort_hours"))
                cost = _finite(item.get("estimated_cost"), 0.0)
                revenue = _finite(item.get("estimated_revenue"))
                raw_fit = model_text(item.get("fit"))
                raw_summary = model_text(item.get("summary"))
                risks = item.get("risks", [])
                missing_requirements = item.get("missing_requirements", [])
                if not raw_fit or not raw_summary or not all(
                    model_text_list(values) for values in (risks, missing_requirements)
                ):
                    row.metadata_json = {
                        **dict(row.metadata_json or {}),
                        "screening_status": "evaluation_incomplete",
                        "screening_reason": "評価本文・リスク・不足要件の形式が不正なため再評価が必要です",
                        "model_output_error": "evaluation_text_invalid",
                    }
                    await session.commit()
                    continue
                fit = bounded_operation_text(raw_fit, "fit")
                # fit is a free-text label, not an enum or a profitability gate.
                # Keep any explanation/qualifiers in the existing long text.
                summary_input = (
                    f"適合性: {raw_fit}\n\n{raw_summary}"
                    if fit != raw_fit and raw_summary else raw_summary
                )
                summary = bounded_operation_text(summary_input, "summary")
                truncated = [name for name, before, after in (
                    ("fit", raw_fit, fit), ("summary", summary_input, summary)
                ) if before != after]
                confidence = _finite(item.get("confidence"))
                if effort is None or effort <= 0 or revenue is None or not fit or not summary or confidence is None:
                    # Incomplete model rows are not durable evaluations. Keep
                    # the opportunity pending so a later cycle can retry it.
                    continue
                if any(len(canonical_json(values).encode("utf-8")) > EVALUATION_COLLECTION_MAX_BYTES
                       for values in (risks, missing_requirements)):
                    # Dropping a risk could incorrectly make an application
                    # eligible. Keep the entire evaluation pending instead.
                    row.metadata_json = {
                        **dict(row.metadata_json or {}),
                        "screening_status": "evaluation_incomplete",
                        "screening_reason": "リスク・不足要件が保存上限を超えるため再評価が必要です",
                        "model_output_error": "evaluation_collection_too_large",
                    }
                    await session.commit()
                    continue
                worker_kind = normalize_worker_kind(item.get("worker_kind"))
                card_summary_lines = _card_summary_lines(
                    item,
                    summary=summary,
                    risks=risks,
                    missing_requirements=missing_requirements,
                    effort=effort,
                    worker_kind=worker_kind,
                )
                evaluation = await self.operations.create_evaluation(
                    session,
                    actor,
                    row.id,
                    estimated_effort_hours=effort,
                    estimated_cost=cost,
                    estimated_revenue=revenue,
                    fit=fit,
                    risks=risks,
                    missing_requirements=missing_requirements,
                    summary=summary,
                    evidence_refs=[{"source_snapshot_hash": row.source_snapshot_hash}],
                )
                profit = (revenue - (cost or 0.0)) if revenue is not None else None
                pph = profit / effort if profit is not None and effort and effort > 0 else None
                metadata = dict(row.metadata_json or {})
                metadata.pop("model_output_error", None)
                if metadata.get("screening_status") == "evaluation_incomplete":
                    metadata.pop("screening_status", None)
                    metadata.pop("screening_reason", None)
                metadata["model_output_truncations"] = {
                    **dict(metadata.get("model_output_truncations") or {}),
                    "evaluation": truncated,
                }
                metadata.update(
                    {
                        "workflow_stage": "EVALUATED",
                        "expected_profit": profit,
                        "expected_profit_per_hour": pph,
                        "estimated_effort_hours": effort,
                        "fit": fit,
                        "evaluation_summary": summary,
                        "card_summary_lines": card_summary_lines,
                        "confidence": max(0.0, min(1.0, confidence)),
                        "expected_human_minutes": _finite(item.get("expected_human_minutes")),
                        "estimated_compute_cost": _finite(item.get("estimated_compute_cost")),
                        "estimated_api_cost": _finite(item.get("estimated_api_cost")),
                        "estimated_gpu_minutes": _finite(item.get("estimated_gpu_minutes")),
                        "automation_reuse_score": _finite(item.get("automation_reuse_score")),
                        "worker_kind": worker_kind,
                        "latest_evaluation_id": evaluation["id"],
                        "model_routes": {
                            **dict(metadata.get("model_routes") or {}),
                            "evaluation": route.provenance(),
                        },
                    }
                )
                row.metadata_json = metadata
                await session.commit()
                scored.append((row, item))
        return scored

    async def _defer_proposals(self, session, rows) -> None:
        for row in rows:
            row.metadata_json = {
                **dict(row.metadata_json or {}),
                "screening_status": "proposal_deferred",
                "screening_reason": "応募提案の保留枠が上限に達したため、空きができるまで待機します",
                "proposal_deferred_reason": "action_rate_limited",
            }
        self.proposal_deferred += len(rows)
        await session.commit()

    async def _draft_and_propose(
        self,
        session,
        *,
        scored: list[tuple[EngagementOpportunity, dict[str, Any]]],
        owner_user_id: str,
        connection_id,
        project_id,
        policy: CrowdWorksPolicy,
        policy_revision_id: str,
        agent_id: str,
        run_id: str,
        work_item_id: str,
        action_policy_service,
        pins: AgentRoutingPins | None = None,
    ) -> tuple[int, int]:
        self.proposal_deferred = 0
        actor = {"id": owner_user_id, "actor_type": "system"}
        ranked: list[tuple[float, float, EngagementOpportunity, dict[str, Any]]] = []
        for row, item in scored:
            meta = dict(row.metadata_json or {})
            truncations = meta.get("model_output_truncations") or {}
            if "summary" in truncations.get("evaluation", []) or truncations.get("application"):
                meta["screening_status"] = "human_review_required"
                meta["screening_reason"] = "モデル出力の条件を全文保存できないため、人間の確認が必要です"
                row.metadata_json = meta
                continue
            profit = _finite(meta.get("expected_profit"))
            pph = _finite(meta.get("expected_profit_per_hour"))
            assessment = meta.get("specialist_assessment")
            if is_development_kind(meta.get("worker_kind")) and isinstance(assessment, dict):
                # Once the specialist reviewed a development bid, its effort
                # and feasibility are authoritative for ranking and screening.
                blocked = assessment_blocks_application(assessment, policy=policy)
                if blocked is not None:
                    meta["screening_status"], meta["screening_reason"] = blocked
                    row.metadata_json = meta
                    continue
                profit = _finite(assessment.get("expected_profit"))
                pph = _finite(assessment.get("expected_profit_per_hour"))
            if profit is None or pph is None:
                meta["screening_status"] = "evaluation_incomplete"
                meta["screening_reason"] = "採算情報が不足しているため再評価待ち"
                row.metadata_json = meta
                continue
            if profit < policy.min_expected_profit:
                meta["screening_status"] = "below_profit_threshold"
                meta["screening_reason"] = (
                    f"期待利益 {profit:,.0f}円 < 基準 {policy.min_expected_profit:,.0f}円"
                )
                row.metadata_json = meta
                continue
            if pph < policy.min_profit_per_hour:
                meta["screening_status"] = "below_profit_per_hour"
                meta["screening_reason"] = (
                    f"利益/時 {pph:,.0f}円 < 基準 {policy.min_profit_per_hour:,.0f}円"
                )
                row.metadata_json = meta
                continue
            meta.pop("proposal_deferred_reason", None)
            meta["screening_status"] = "qualified"
            meta["screening_reason"] = "採算条件クリア"
            row.metadata_json = meta
            ranked.append((pph, profit, row, item))
        ranked.sort(key=lambda value: (value[0], value[1]), reverse=True)
        shortlist = ranked[: policy.max_applications_per_scan]
        shortlist_ids = {str(value[2].id) for value in shortlist}
        for _pph, _profit, row, _item in ranked:
            meta = dict(row.metadata_json or {})
            if str(row.id) in shortlist_ids:
                meta["screening_status"] = "application_preparing"
                meta["screening_reason"] = "採算条件クリア。応募案を作成中"
            else:
                meta["screening_status"] = "qualified_waiting_rank"
                meta["screening_reason"] = (
                    f"採算条件クリア。今回の上位{policy.max_applications_per_scan}件外・次回候補"
                )
            row.metadata_json = meta
        await session.commit()
        if not shortlist:
            return 0, 0

        existing_drafts: dict[str, ApplicationDraft | None] = {}
        existing_actions: set[str] = set()
        draft_targets: list[EngagementOpportunity] = []
        for _, _, row, _ in shortlist:
            key = str(row.id)
            existing_drafts[key] = await session.scalar(
                select(ApplicationDraft)
                .where(ApplicationDraft.opportunity_id == row.id)
                .order_by(ApplicationDraft.version.desc())
                .limit(1)
            )
            action_id = await session.scalar(
                select(ExternalAction.id)
                .where(
                    ExternalAction.opportunity_id == row.id,
                    ExternalAction.action_type == "engagement.submit_application",
                )
                .limit(1)
            )
            if action_id is not None:
                existing_actions.add(key)
                continue
            if existing_drafts[key] is None:
                draft_targets.append(row)

        # Count existing reservations before spending on specialist reviews or
        # application copy. This is advisory; propose_action rechecks atomically.
        candidates = [entry for entry in shortlist if str(entry[2].id) not in existing_actions]
        if candidates:
            capacity = await action_policy_service.proposal_capacity(
                session, policy_revision_id=policy_revision_id
            )
            deferred = [entry[2] for entry in candidates[capacity:]]
            if deferred:
                await self._defer_proposals(session, deferred)
            shortlist = candidates[:capacity]
            selected_ids = {str(entry[2].id) for entry in shortlist}
            draft_targets = [row for row in draft_targets if str(row.id) in selected_ids]
            if not shortlist:
                return 0, 0

        # Web/App development bids need the assigned specialist's feasibility
        # review before an application is drafted or proposed to a human.
        development_targets = [
            row
            for _, _, row, _ in shortlist
            if str(row.id) not in existing_actions
            and is_development_kind((row.metadata_json or {}).get("worker_kind"))
        ]
        if development_targets:
            pending_assessment = [
                row for row in development_targets
                if not isinstance((row.metadata_json or {}).get("specialist_assessment"), dict)
            ]
            if pending_assessment:
                await assess_development_opportunities(
                    session,
                    rows=pending_assessment,
                    llm=partial(self._llm, owner_user_id=owner_user_id, project_id=project_id),
                    operations=self.operations,
                    actor=actor,
                    config=self.config,
                )
            blocked_ids: set[str] = set()
            for row in development_targets:
                meta = dict(row.metadata_json or {})
                assessment = meta.get("specialist_assessment")
                if not isinstance(assessment, dict):
                    if meta.get("screening_status") != "specialist_unavailable":
                        meta["screening_status"] = "specialist_review_pending"
                        meta["screening_reason"] = "専門担当の実現性評価待ち（次回サイクルで再評価）"
                    blocked_ids.add(str(row.id))
                else:
                    blocked = assessment_blocks_application(assessment, policy=policy)
                    if blocked is not None:
                        meta["screening_status"], meta["screening_reason"] = blocked
                        blocked_ids.add(str(row.id))
                row.metadata_json = meta
            await session.commit()
            if blocked_ids:
                shortlist = [value for value in shortlist if str(value[2].id) not in blocked_ids]
                draft_targets = [row for row in draft_targets if str(row.id) not in blocked_ids]

        suggestions: dict[str, dict[str, Any]] = {}
        application_route: AgentModelRoute | None = None
        if draft_targets:
            application_route = self._phase_route(pins, APPLICATION_ROLE, policy=policy)
            prompt_items = []
            for row in draft_targets:
                meta = dict(row.metadata_json or {})
                evaluation = await session.scalar(
                    select(OpportunityEvaluation)
                    .where(OpportunityEvaluation.opportunity_id == row.id)
                    .order_by(OpportunityEvaluation.version.desc())
                    .limit(1)
                )
                assessment = meta.get("specialist_assessment") if isinstance(meta.get("specialist_assessment"), dict) else None
                prompt_items.append(
                    {
                        "id": str(row.id),
                        "title": row.title,
                        "url": row.source_url,
                        "listing_text": (row.source_text or "")[:16000],
                        "specialist_assessment": (
                            {
                                key: assessment.get(key)
                                for key in (
                                    "deliverable_summary",
                                    "estimated_effort_hours",
                                    "tech_stack",
                                    "client_inputs_required",
                                    "unresolved_requirements",
                                    "feasibility",
                                    "human_tasks",
                                )
                            }
                            if assessment
                            else None
                        ),
                        "expected_profit": meta.get("expected_profit"),
                        "expected_profit_per_hour": meta.get("expected_profit_per_hour"),
                        "fit": evaluation.fit if evaluation else None,
                        "evaluation_summary": evaluation.summary if evaluation else None,
                        "risks": list(evaluation.risks or []) if evaluation else [],
                        "missing_requirements": (
                            list(evaluation.missing_requirements or []) if evaluation else []
                        ),
                    }
                )
            draft_prompt = (
                "以下は採算評価済みのCrowdWorks上位案件です。案件ごとの応募文・提示価格・納期案だけを作ってください。"
                "虚偽の経験・資格・実績は書かず、不足情報は応募文で確認してください。"
                "specialist_assessmentがある開発案件は、その工数・技術・顧客提供物・未確定要件と矛盾しない価格と納期にし、"
                "未確定要件と必要な顧客提供物を応募文で確認してください。"
                "出力はJSON配列のみ。各要素は id, message, offered_price, currency, delivery_estimate を持つこと。"
                f"通貨既定値は {policy.currency}。案件: "
                + json.dumps(prompt_items, ensure_ascii=False)
            )
            raw = await self._llm(
                draft_prompt,
                system_prompt=(
                    "Write concise marketplace application proposals grounded only in the supplied listing "
                    "and evaluation. Return strict JSON only."
                ),
                route=application_route,
                owner_user_id=owner_user_id,
                project_id=project_id,
            )
            parsed = _extract_json_array(raw)
            if not parsed:
                raw = await self._llm(
                    "前回の出力をJSONとして解釈できませんでした。説明なしでJSON配列だけ返してください。" + draft_prompt,
                    system_prompt="Return one strict JSON array only. No prose or markdown.",
                    route=application_route,
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                )
                parsed = _extract_json_array(raw)
            if not parsed:
                parsed = []
            suggestions = {
                str(item.get("id")): item
                for item in parsed
                if isinstance(item, dict) and str(item.get("message") or "").strip()
            }

        drafted = proposed = 0
        for position, (_, _, row, _) in enumerate(shortlist):
            key = str(row.id)
            if key in existing_actions:
                continue
            existing_draft = existing_drafts[key]
            if existing_draft is None:
                suggestion = suggestions.get(key)
                if suggestion is None:
                    continue
                raw_text = {
                    "message": model_text(suggestion.get("message")),
                    "currency": model_text(
                        policy.currency if suggestion.get("currency") in (None, "") else suggestion.get("currency")
                    ),
                    "delivery_estimate": model_text(
                        "" if suggestion.get("delivery_estimate") in (None, "") else suggestion.get("delivery_estimate")
                    ),
                }
                if not raw_text["message"] or any(value is None for value in raw_text.values()):
                    row.metadata_json = {
                        **dict(row.metadata_json or {}),
                        "screening_status": "application_incomplete",
                        "screening_reason": "応募文・提示条件の形式が不正なため再生成が必要です",
                        "model_output_error": "application_text_invalid",
                    }
                    await session.commit()
                    continue
                text = {field: bounded_operation_text(
                    value[:20000] if field == "message" else value, field
                ) for field, value in raw_text.items()}
                truncated = [field for field, value in raw_text.items() if text[field] != value]
                meta = dict(row.metadata_json or {})
                meta.pop("model_output_error", None)
                meta["model_output_truncations"] = {
                    **dict(meta.get("model_output_truncations") or {}), "application": truncated,
                }
                row.metadata_json = meta
                draft = await self.operations.create_draft(
                    session,
                    actor,
                    row.id,
                    message=text["message"],
                    offered_price=_finite(suggestion.get("offered_price")),
                    currency=text["currency"] or None,
                    delivery_estimate=text["delivery_estimate"] or None,
                )
                drafted += 1
                draft_id = draft["id"]
                if truncated:
                    meta["screening_status"] = "human_review_required"
                    meta["screening_reason"] = "応募文・提示条件を全文保存できないため、人間の確認が必要です"
                    row.metadata_json = dict(meta)
                    await session.commit()
                    continue
            else:
                draft_id = str(existing_draft.id)

            draft_row = await session.get(ApplicationDraft, UUID(str(draft_id)))
            payload = {
                "opportunity_id": str(row.id),
                "application_draft_id": str(draft_row.id),
                "source_url": row.source_url,
                "message": draft_row.message,
                "offered_price": draft_row.offered_price,
                "currency": draft_row.currency,
                "delivery_estimate": draft_row.delivery_estimate,
            }
            try:
                action = await action_policy_service.propose_action(
                    session,
                    policy_revision_id=policy_revision_id,
                    automation_rule_revision_id=None,
                    origin_agent_id=agent_id,
                    origin_agent_run_id=run_id,
                    origin_work_item_id=work_item_id,
                    source_event_id=None,
                    action_position=position,
                    payload=payload,
                )
            except ActionPolicyError as exc:
                if exc.code != "action_rate_limited":
                    raise
                # Another serialized proposer may have consumed the last slot.
                # Keep committed evaluations/drafts and stop only proposals.
                await self._defer_proposals(
                    session, [entry[2] for entry in shortlist[position:]
                              if str(entry[2].id) not in existing_actions]
                )
                break
            await session.commit()
            meta = dict(row.metadata_json or {})
            meta.pop("proposal_deferred_reason", None)
            meta["workflow_stage"] = "APPLICATION_PROPOSED"
            meta["external_action_id"] = str(action.id)
            meta["external_action_status"] = str(action.status or "proposed")
            meta["screening_status"] = "decision_ready"
            meta["screening_reason"] = "応募案作成済み。あなたの最終判断待ち"
            if existing_draft is None and application_route is not None:
                meta["model_routes"] = {
                    **dict(meta.get("model_routes") or {}),
                    "application": application_route.provenance(),
                }
            row.metadata_json = meta
            await session.commit()
            proposed += 1
        return drafted, proposed

    async def _monitor_contracts(
        self,
        session,
        *,
        owner_user_id: str,
        connection_id,
        device_id: str | None,
    ) -> int:
        rows = (
            await session.scalars(
                select(EngagementOpportunity)
                .where(
                    EngagementOpportunity.connection_id == connection_id,
                    EngagementOpportunity.status.in_(("open", "escrow_pending")),
                )
                .order_by(EngagementOpportunity.created_at.asc())
                .limit(100)
            )
        ).all()
        bridge: BridgeInspectionLease | None = None
        accepted = 0
        monitor_tab_id: int | None = None
        async with AsyncExitStack() as stack:
            for row in rows:
                action = await session.scalar(
                    select(ExternalAction)
                    .where(
                        ExternalAction.opportunity_id == row.id,
                        ExternalAction.action_type == "engagement.submit_application",
                        ExternalAction.status == "succeeded",
                    )
                    .order_by(ExternalAction.created_at.desc())
                    .limit(1)
                )
                if action is None:
                    continue
                if bridge is None:
                    hub = get_pc_bridge_hub()
                    selected = await hub.store.resolve(owner_user_id, device_id, None)
                    bridge = await stack.enter_async_context(
                        BridgeInspectionLease(RemoteControlBridge(owner_user_id, selected, hub))
                    )
                target = await self.source._resolve_reusable_tab(
                    bridge, row.source_url, tab_id=monitor_tab_id
                )
                monitor_tab_id = target["tab_id"]
                observation = await bridge.request(
                    "crowdworks_inspect", tab_id=monitor_tab_id
                )
                page_text = str(observation.get("text") or "")
                stage = crowdworks_contract_stage(page_text)
                if stage is None:
                    continue
                metadata = dict(row.metadata_json or {})
                metadata["workflow_stage"] = stage.upper()
                metadata["escrow_confirmation"] = stage == "escrow_confirmed"
                if stage != "escrow_confirmed":
                    metadata["fulfillment_blocked_reason"] = "escrow_confirmation_required"
                    row.status = "escrow_pending"
                    row.metadata_json = metadata
                    await session.commit()
                    continue
                metadata.pop("fulfillment_blocked_reason", None)
                metadata["workflow_stage"] = "CONTRACT_ACCEPTED"
                row.metadata_json = metadata
                row.status = "contract_accepted"
                await session.commit()
                if is_development_kind(metadata.get("worker_kind")):
                    from .client_development import ensure_engagement_project

                    # Each accepted development engagement gets its own Project
                    # and an Agent Project grant for the assigned specialist.
                    try:
                        await ensure_engagement_project(session, row, owner_user_id=owner_user_id)
                    except Exception:
                        await session.rollback()
                accepted += 1
        return accepted
    async def run_cycle(
        self,
        session,
        *,
        owner_user_id: str,
        connection,
        policy_revision,
        agent_id: str,
        run_id: str,
        work_item_id: str,
        action_policy_service,
        pins: AgentRoutingPins,
    ) -> WorkflowResult:
        policy = CrowdWorksPolicy.model_validate(policy_revision.constraints_json)
        # Fail closed before touching CrowdWorks when the pinned revision can
        # not route its thinking phase (invalid Team/Profile/Subagent pin).
        planning_route = self._phase_route(pins, EVALUATION_ROLE, policy=policy)
        result = WorkflowResult()
        result.model_routes["evaluation"] = planning_route.provenance()
        result.model_routes["application"] = self._phase_route(
            pins, APPLICATION_ROLE, policy=policy
        ).provenance()
        evaluated_exists = select(OpportunityEvaluation.id).where(
            OpportunityEvaluation.opportunity_id == EngagementOpportunity.id
        ).exists()
        pending_count = int(
            await session.scalar(
                select(func.count())
                .select_from(EngagementOpportunity)
                .where(
                    EngagementOpportunity.connection_id == connection.id,
                    EngagementOpportunity.status == "open",
                    ~evaluated_exists,
                )
            )
            or 0
        )
        readiness: dict[str, Any] = {}
        # When a backlog already fills a full evaluation cycle, do not touch
        # CrowdWorks at all. Drain local durable work first; this prevents tab
        # churn and keeps model spend focused on already-ingested opportunities.
        if pending_count < policy.max_opportunities_per_scan:
            _, readiness = await action_policy_service._ready(session, policy_revision)
            created = await self.source.ingest(
                session,
                owner_user_id=owner_user_id,
                connection_id=connection.id,
                project_id=connection.project_id,
                search_urls=policy.search_urls,
                max_pages=policy.max_pages_per_scan,
                max_items=policy.max_opportunities_per_scan - pending_count,
                device_id=readiness.get("device_id"),
            )
            result.ingested = len(created)
        scored = await self._evaluate_batch(
            session,
            owner_user_id=owner_user_id,
            connection_id=connection.id,
            project_id=connection.project_id,
            policy=policy,
            pins=pins,
        )
        result.evaluated = len(scored)
        result.drafted, result.proposed = await self._draft_and_propose(
            session,
            scored=scored,
            owner_user_id=owner_user_id,
            connection_id=connection.id,
            project_id=connection.project_id,
            policy=policy,
            policy_revision_id=str(policy_revision.id),
            agent_id=agent_id,
            run_id=run_id,
            work_item_id=work_item_id,
            action_policy_service=action_policy_service,
            pins=pins,
        )
        result.proposal_deferred = self.proposal_deferred
        result.proposal_deferred_reason = "action_rate_limited" if self.proposal_deferred else None
        result.accepted = await self._monitor_contracts(
            session,
            owner_user_id=owner_user_id,
            connection_id=connection.id,
            device_id=readiness.get("device_id"),
        )
        return result


__all__ = [
    "CrowdWorksWorkflowService",
    "FulfillmentResult",
    "WORKER_KIND_ROLES",
    "WorkflowResult",
]
