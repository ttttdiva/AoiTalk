"""TRPG資産の読み取り専用参照 API。

プレイ実行・ルーム・WebSocket の経路は廃止したが、独立した TRPG ルール
資産は §11.8 に従って保持する。この router は資料の検索だけを公開し、
書き込みやセッション状態には触れない。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..services.trpg_rule_reference_service import (
    get_rule_reference_stats,
    search_rule_references,
)
from ..services.trpg_rulebook_service import (
    TRPGRulebookError,
    list_ruleset_profiles,
)


class RulesetListResponse(BaseModel):
    rulesets: list[dict[str, Any]] = Field(default_factory=list)
    count: int


class ReferenceBundleResponse(BaseModel):
    rules: list[dict[str, Any]] = Field(default_factory=list)
    creatures: list[dict[str, Any]] = Field(default_factory=list)
    mechanic_links: list[dict[str, Any]] = Field(default_factory=list)
    count: int


def create_trpg_reference_router() -> APIRouter:
    router = APIRouter(prefix="/api/trpg", tags=["trpg-reference"])

    @router.get("/rulesets", response_model=RulesetListResponse)
    async def api_list_rulesets(include_disabled: bool = False):
        try:
            profiles = await list_ruleset_profiles(include_disabled=include_disabled)
            return {"rulesets": profiles, "count": len(profiles)}
        except TRPGRulebookError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    @router.get("/rulesets/{ruleset_key}/references", response_model=ReferenceBundleResponse)
    async def api_search_rule_references(
        ruleset_key: str,
        query: str = "",
        kind: str = "all",
        mechanic_key: str | None = None,
        rule_domain: str | None = None,
        creature_type: str | None = None,
        limit: int = Query(20, ge=1, le=80),
    ):
        normalized_kind = str(kind or "all").strip().lower()
        tome_domains = ["mythos_tomes", "occult_tomes"]
        include_tomes = normalized_kind in {"tomes", "tome", "books", "book"}
        include_rules = normalized_kind in {"all", "rules", "rule"} or include_tomes
        include_creatures = normalized_kind in {"all", "creatures", "creature"}
        if not include_rules and not include_creatures:
            include_rules = True
            include_creatures = True
        rule_domains = [rule_domain] if rule_domain and include_rules else None
        excluded_rule_domains = None
        if include_tomes:
            rule_domains = tome_domains
        elif normalized_kind in {"rules", "rule"}:
            excluded_rule_domains = tome_domains
        bundle = await search_rule_references(
            ruleset_key=ruleset_key,
            query=query,
            mechanic_keys=[mechanic_key] if mechanic_key and include_rules else None,
            rule_domains=rule_domains,
            excluded_rule_domains=excluded_rule_domains,
            creature_types=[creature_type] if creature_type and include_creatures else None,
            include_creatures=include_creatures,
            limit=limit,
        )
        if not include_rules:
            bundle["rules"] = []
            bundle["mechanic_links"] = []
        return {
            **bundle,
            "count": len(bundle.get("rules") or []) + len(bundle.get("creatures") or []),
        }

    @router.get("/rulesets/{ruleset_key}/reference-stats")
    async def api_rule_reference_stats(ruleset_key: str):
        return await get_rule_reference_stats(ruleset_key)

    return router


__all__ = ["create_trpg_reference_router"]
