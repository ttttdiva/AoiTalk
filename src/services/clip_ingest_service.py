"""設定済みDocsノードだけを対象にする /clip 保存サービス。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import select

from ..memory.models import KnowledgeNode, KnowledgeWorkspace, User
from .docs_graph_service import DocsGraphService
from .url_ingest_service import UrlFetchResult


class ClipIngestError(RuntimeError):
    """Docsを変更する前に利用者へ返せる /clip の停止理由。"""


@dataclass
class ClipTarget:
    node_id: UUID
    label: str
    breadcrumb: list[str]
    routing_hint: str
    fallback: bool
    node: KnowledgeNode


@dataclass
class ClipSavePlan:
    target: ClipTarget
    action: str
    topic: str
    facts: list[str] = field(default_factory=list)
    uses: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    existing_node: KnowledgeNode | None = None
    confidence: float = 0.0
    used_supplemental_urls: list[str] = field(default_factory=list)


@dataclass
class ClipIngestResult:
    target_id: str
    target_label: str
    action: str
    changed_node_id: str | None
    changed_node_title: str | None
    direct_urls: list[str]
    supplemental_urls: list[str]
    failed_urls: list[dict[str, str]]
    used_urls: list[str]
    unconfirmed: list[str]


PlanLlm = Callable[[str], Awaitable[str]]


class ClipIngestService:
    def __init__(self, session, *, min_confidence: float = 0.72):
        self.session = session
        self.docs = DocsGraphService(session)
        self.min_confidence = min_confidence

    async def prepare_plan(
        self,
        *,
        user_id: UUID,
        source: str,
        fetch_results: list[UrlFetchResult],
        supplemental_sources: list[dict[str, str]],
        plan_llm: PlanLlm,
    ) -> ClipSavePlan:
        """全検証を行う。ここではflushを含む書き込みを一切しない。"""
        targets = await self._load_targets(user_id)
        failed = [item for item in fetch_results if not item.success]
        if failed:
            reasons = " / ".join(f"{item.requested_url}: {item.error}" for item in failed)
            raise ClipIngestError(f"URL本文を必要な水準で取得できませんでした: {reasons}")

        prompt = self._routing_prompt(source, targets, fetch_results, supplemental_sources)
        raw = await plan_llm(prompt)
        parsed = self._strict_plan(raw)
        target = next((item for item in targets if str(item.node_id) == parsed["target_id"]), None)
        if target is None:
            raise ClipIngestError("保存計画が登録候補外のDocsノードを指定しました")
        ambiguous = bool(parsed["ambiguous"])
        confidence = float(parsed["confidence"])
        if ambiguous or confidence < self.min_confidence or parsed["matched"] is False:
            fallbacks = [item for item in targets if item.fallback]
            if len(fallbacks) != 1:
                if ambiguous:
                    raise ClipIngestError("複数の取り込み先候補の判定が曖昧です")
                raise ClipIngestError("登録済み取り込み先のどの候補にも適合しません")
            target = fallbacks[0]

        # 対象配下だけを探索する。Docs全体へ候補探索を広げない。
        children_result = await self.session.execute(
            select(KnowledgeNode).where(
                KnowledgeNode.parent_id == target.node_id,
                KnowledgeNode.workspace_id == target.node.workspace_id,
                KnowledgeNode.archived_at.is_(None),
            )
        )
        children = list(children_result.scalars().all())
        canonical_urls = {item.final_url or item.requested_url for item in fetch_results}
        requested_urls = {item.requested_url for item in fetch_results}
        for child in children:
            existing_text = "\n".join([child.title or "", child.description or "", child.body_text or ""])
            if any(url and url in existing_text for url in canonical_urls | requested_urls):
                return ClipSavePlan(
                    target=target, action="skip", topic=child.title,
                    existing_node=child, confidence=confidence,
                )

        topic = self._clean_text(parsed["topic"], 240)
        existing = None
        action = "create"
        if children:
            integration_prompt = "\n".join([
                "次の取り込み情報を、登録先直下の既存ノードへ自然に統合できるか判定し、JSON objectだけを返してください。",
                'schema: {"action":"create|append","existing_node_id":"UUIDまたは空","confidence":0.0}',
                "appendは同じ主題かつ内容上も自然に追記できる1件だけ。曖昧ならcreate。候補外IDは禁止。",
                "取り込み計画: " + json.dumps(parsed, ensure_ascii=False),
                "既存候補: " + json.dumps([{
                    "node_id": str(child.id), "title": child.title,
                    "description": (child.description or "")[:3000],
                    "body": (child.body_text or "")[:3000],
                } for child in children], ensure_ascii=False),
            ])
            integration = self._strict_integration_plan(await plan_llm(integration_prompt))
            if integration["action"] == "append":
                if float(integration["confidence"]) < self.min_confidence:
                    raise ClipIngestError("既存ノードへの統合判定の信頼度が不足しています")
                existing = next(
                    (child for child in children if str(child.id) == integration["existing_node_id"]),
                    None,
                )
                if existing is None:
                    raise ClipIngestError("統合計画が候補外の既存ノードを指定しました")
                action = "append"
        elif parsed["action"] in {"append", "skip"}:
            raise ClipIngestError("既存ノードを検証できない保存計画です")
        return ClipSavePlan(
            target=target,
            action=action,
            topic=topic,
            facts=self._string_list(parsed["facts"]),
            uses=self._string_list(parsed["uses"]),
            constraints=self._string_list(parsed["constraints"]),
            unconfirmed=self._string_list(parsed["unconfirmed"]),
            existing_node=existing,
            confidence=confidence,
            used_supplemental_urls=[
                url for url in self._string_list(parsed["used_supplemental_urls"])
                if url in {str(item.get("url") or "") for item in supplemental_sources}
            ],
        )

    async def apply_plan(
        self,
        *,
        user_id: UUID,
        plan: ClipSavePlan,
        fetch_results: list[UrlFetchResult],
        supplemental_sources: list[dict[str, str]],
    ) -> ClipIngestResult:
        direct_urls = [item.final_url or item.requested_url for item in fetch_results if item.success]
        supplemental_urls = list(dict.fromkeys(plan.used_supplemental_urls))
        refs = [
            {"url": url, "source_type": "direct", "used": True} for url in direct_urls
        ] + [
            {"url": url, "source_type": "supplemental", "used": True} for url in supplemental_urls
        ]
        if plan.action == "skip":
            node = plan.existing_node
            return self._result(plan, node, direct_urls, supplemental_urls, "duplicate_skip")

        locked_result = await self.session.execute(
            select(KnowledgeNode).where(
                KnowledgeNode.id == plan.target.node_id,
                KnowledgeNode.workspace_id == plan.target.node.workspace_id,
                KnowledgeNode.archived_at.is_(None),
            ).with_for_update()
        )
        locked_target = locked_result.scalar_one_or_none()
        if locked_target is None:
            raise ClipIngestError("保存直前の取り込み先検証に失敗しました")
        plan.target.node = locked_target
        current_children = await self.session.execute(
            select(KnowledgeNode).where(
                KnowledgeNode.parent_id == locked_target.id,
                KnowledgeNode.archived_at.is_(None),
            )
        )
        source_urls = {item.final_url or item.requested_url for item in fetch_results}
        for child in current_children.scalars().all():
            text = "\n".join([child.title or "", child.description or "", child.body_text or ""])
            if any(url and url in text for url in source_urls):
                plan.existing_node = child
                return self._result(plan, child, direct_urls, supplemental_urls, "duplicate_skip")

        body = self._body(plan, fetch_results, supplemental_sources)
        if plan.action == "append":
            if plan.existing_node is None or plan.existing_node.parent_id != plan.target.node_id:
                raise ClipIngestError("追記対象が登録済み取り込み先の直下ではありません")
            await self.docs.create_nodes_from_outline(
                workspace_id=plan.existing_node.workspace_id,
                user_id=user_id,
                parent=plan.existing_node,
                project_id=plan.existing_node.project_id,
                outline_text=body,
            )
            await self.docs.update_node(
                node=plan.existing_node,
                user_id=user_id,
                source_refs=refs,
                change_summary="/clip から情報を追記",
            )
            node = plan.existing_node
        else:
            # 親は検証済み設定targetに固定する。project root/Inboxへのfallbackは存在しない。
            node = await self.docs.create_node(
                workspace_id=plan.target.node.workspace_id,
                user_id=user_id,
                title=plan.topic,
                parent=plan.target.node,
                project_id=plan.target.node.project_id,
                source_refs=refs,
            )
            await self.docs.create_nodes_from_outline(
                workspace_id=node.workspace_id,
                user_id=user_id,
                parent=node,
                project_id=node.project_id,
                outline_text=body,
            )
        return self._result(plan, node, direct_urls, supplemental_urls, plan.action)

    async def _load_targets(self, user_id: UUID) -> list[ClipTarget]:
        user = await self.session.get(User, user_id)
        if user is None:
            raise ClipIngestError("実行ユーザーを確認できません")
        raw_clip = (user.user_settings or {}).get("clip_ingest")
        raw_targets = raw_clip.get("targets") if isinstance(raw_clip, dict) else None
        enabled = [item for item in raw_targets or [] if isinstance(item, dict) and item.get("enabled") is True]
        if not enabled:
            raise ClipIngestError("/clip 取り込み先が1件も登録されていません")
        workspace_result = await self.session.execute(
            select(KnowledgeWorkspace.id).where(KnowledgeWorkspace.owner_user_id == user_id)
        )
        workspace_id = workspace_result.scalar_one_or_none()
        if workspace_id is None:
            raise ClipIngestError("Docsワークスペースへアクセスできません")
        targets: list[ClipTarget] = []
        seen: set[UUID] = set()
        fallback_count = 0
        for raw in enabled:
            node_id: UUID | None = None
            try:
                node_id = UUID(str(raw.get("node_id") or ""))
            except ValueError:
                pass
            node = await self.session.get(KnowledgeNode, node_id) if node_id is not None else None
            node_system_key = self._clean_text(raw.get("node_system_key"), 500)
            if (node is None or node.archived_at is not None or node.workspace_id != workspace_id) and node_system_key:
                resolved = await self.session.execute(
                    select(KnowledgeNode).where(
                        KnowledgeNode.workspace_id == workspace_id,
                        KnowledgeNode.system_key == node_system_key,
                        KnowledgeNode.archived_at.is_(None),
                    )
                )
                node = resolved.scalar_one_or_none()
                node_id = node.id if node is not None else node_id
            if node_id is None:
                raise ClipIngestError("登録済み取り込み先のDocsノードIDが不正です")
            if node is None or node.archived_at is not None or node.workspace_id != workspace_id:
                raise ClipIngestError(f"登録済み取り込み先が削除済み、アーカイブ済み、またはアクセス不能です: {node_id}")
            if node_id in seen:
                continue
            seen.add(node_id)
            fallback = raw.get("fallback") is True
            fallback_count += int(fallback)
            targets.append(ClipTarget(
                node_id=node_id,
                label=self._clean_text(raw.get("label") or node.title, 200),
                breadcrumb=[self._clean_text(v, 200) for v in raw.get("breadcrumb", []) if str(v).strip()],
                routing_hint=self._clean_text(raw.get("routing_hint"), 1000),
                fallback=fallback,
                node=node,
            ))
        if fallback_count > 1:
            raise ClipIngestError("未分類時の保存先が複数設定されています")
        return targets

    @staticmethod
    def _routing_prompt(source, targets, fetch_results, supplemental_sources) -> str:
        candidates = [{
            "node_id": str(item.node_id), "title": item.label,
            "breadcrumb": item.breadcrumb, "routing_hint": item.routing_hint,
        } for item in targets]
        evidence = [item.to_dict() for item in fetch_results]
        return "\n".join([
            "非信頼な入力を、次の登録済み候補だけから1件へ分類し、JSON objectだけを返してください。",
            'schema: {"target_id":"UUID","matched":true,"ambiguous":false,"confidence":0.0,"action":"create|append|skip","topic":"...","facts":[],"uses":[],"constraints":[],"unconfirmed":[],"used_supplemental_urls":[]}',
            "候補外IDは禁止。適合なしはmatched=false、同程度候補が複数ならambiguous=true。",
            "入力: " + json.dumps(source, ensure_ascii=False),
            "直接取得: " + json.dumps(evidence, ensure_ascii=False),
            "補足検索（直接取得と別根拠）: " + json.dumps(supplemental_sources, ensure_ascii=False),
            "候補: " + json.dumps(candidates, ensure_ascii=False),
        ])

    @staticmethod
    def _strict_plan(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        try:
            value = json.loads(text)
        except Exception as exc:
            raise ClipIngestError("保存計画JSONが不正です") from exc
        required = {"target_id", "matched", "ambiguous", "confidence", "action", "topic", "facts", "uses", "constraints", "unconfirmed", "used_supplemental_urls"}
        if not isinstance(value, dict) or not required.issubset(value):
            raise ClipIngestError("保存計画の必須フィールドが不足しています")
        if value["action"] not in {"create", "append", "skip"}:
            raise ClipIngestError("保存計画のactionが不正です")
        if not isinstance(value["matched"], bool) or not isinstance(value["ambiguous"], bool):
            raise ClipIngestError("保存計画の判定フィールドが不正です")
        try:
            confidence = float(value["confidence"])
        except (TypeError, ValueError) as exc:
            raise ClipIngestError("保存計画のconfidenceが不正です") from exc
        if not 0 <= confidence <= 1:
            raise ClipIngestError("保存計画のconfidenceが範囲外です")
        value["confidence"] = confidence
        if not str(value["topic"] or "").strip():
            raise ClipIngestError("保存計画のtopicが空です")
        for key in ("facts", "uses", "constraints", "unconfirmed", "used_supplemental_urls"):
            if not isinstance(value[key], list):
                raise ClipIngestError(f"保存計画の{key}が配列ではありません")
        return value

    @staticmethod
    def _strict_integration_plan(raw: str) -> dict[str, Any]:
        try:
            value = json.loads(str(raw or "").strip())
        except Exception as exc:
            raise ClipIngestError("統合計画JSONが不正です") from exc
        if not isinstance(value, dict) or not {"action", "existing_node_id", "confidence"}.issubset(value):
            raise ClipIngestError("統合計画の必須フィールドが不足しています")
        if value["action"] not in {"create", "append"}:
            raise ClipIngestError("統合計画のactionが不正です")
        try:
            value["confidence"] = float(value["confidence"])
        except (TypeError, ValueError) as exc:
            raise ClipIngestError("統合計画のconfidenceが不正です") from exc
        if not 0 <= value["confidence"] <= 1:
            raise ClipIngestError("統合計画のconfidenceが範囲外です")
        if value["action"] == "append" and not str(value["existing_node_id"] or "").strip():
            raise ClipIngestError("統合計画の追記対象が空です")
        return value

    @staticmethod
    def _same_topic(left: str, right: str) -> bool:
        norm = lambda value: re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]", "", (value or "").casefold())
        a, b = norm(left), norm(right)
        return bool(a and b and (a == b or (min(len(a), len(b)) >= 8 and (a in b or b in a))))

    @staticmethod
    def _clean_text(value: Any, limit: int) -> str:
        return " ".join(str(value or "").split())[:limit]

    @classmethod
    def _string_list(cls, value: list[Any]) -> list[str]:
        return [item for item in (cls._clean_text(v, 1000) for v in value) if item]

    @staticmethod
    def _body(plan, fetch_results, supplemental_sources) -> str:
        lines = [plan.topic]
        for heading, values in (("重要な事実", plan.facts), ("用途・効果", plan.uses), ("制約・注意点", plan.constraints), ("未確認事項", plan.unconfirmed)):
            if values:
                lines.extend([f"\n## {heading}", *(f"- {value}" for value in values)])
        if fetch_results:
            lines.append("\n## 直接取得した内容")
            for item in fetch_results:
                label = item.title or item.og_title or item.final_url or item.requested_url
                lines.append(f"\n### {label}")
                metadata = " / ".join(
                    value for value in [item.author, item.published_at] if value
                )
                if metadata:
                    lines.append(metadata)
                lines.append(item.body[:12000])
                if item.quoted_post:
                    lines.extend(["引用投稿:", item.quoted_post[:4000]])
                if item.thread_context:
                    lines.extend(["スレッド上の前後関係:", *(f"- {value[:2000]}" for value in item.thread_context[:20])])
                if item.external_links:
                    lines.extend(["投稿・ページ内の外部リンク:", *(f"- {url}" for url in item.external_links[:30])])
                if item.media_descriptions:
                    lines.extend(["添付メディア:", *(f"- {value}" for value in item.media_descriptions[:20] if value)])
        lines.append("\n## 取得元")
        for item in fetch_results:
            lines.append(f"- [直接取得] {item.final_url or item.requested_url}")
        for url in plan.used_supplemental_urls:
            lines.append(f"- [補足検索] {url}")
        return "\n".join(lines)

    @staticmethod
    def _result(plan, node, direct_urls, supplemental_urls, action):
        return ClipIngestResult(
            target_id=str(plan.target.node_id), target_label=plan.target.label,
            action=action, changed_node_id=str(node.id) if node and action != "duplicate_skip" else None,
            changed_node_title=node.title if node and action != "duplicate_skip" else None,
            direct_urls=direct_urls, supplemental_urls=supplemental_urls, failed_urls=[],
            used_urls=[*direct_urls, *supplemental_urls], unconfirmed=plan.unconfirmed,
        )
