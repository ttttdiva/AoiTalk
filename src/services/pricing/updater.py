"""料金カタログの更新（OpenRouter Models API / 手動 JSON 取り込み）。

**OpenAI / Google / Kimi の HTML スクレイピングは実装しない。**
これらは `config/pricing_catalog.json` の編集 + `import_catalog_json` で更新する。

OpenRouter 取得は last-known-good を厳守する。HTTP 失敗・JSON 不正・rules 0件・
必須キー欠落のいずれでも既存料金を一切変更せず、状態テーブルにエラーだけ記録する。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .catalog import (
    CatalogValidationError,
    PricingRuleSnapshot,
    build_snapshots,
    diff_catalog,
)

logger = logging.getLogger(__name__)

__all__ = [
    "OPENROUTER_MODELS_URL",
    "OPENROUTER_TTL_SECONDS",
    "OPENROUTER_SOURCE_KEY",
    "OPENROUTER_PRICING_KEYS",
    "refresh_openrouter_catalog",
    "import_catalog_json",
    "get_pricing_catalog_status",
    "build_openrouter_snapshots",
    "parse_openrouter_models",
    "OpenRouterImport",
]

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_TTL_SECONDS = 24 * 60 * 60
OPENROUTER_SOURCE_KEY = "openrouter"
MANUAL_SOURCE_KEY = "manual_import"

# OpenRouter の pricing は USD per token。per-1M へ変換する。
PER_MILLION = Decimal("1000000")

OPENROUTER_PRICING_KEYS = (
    "prompt",
    "completion",
    "request",
    "image",
    "web_search",
    "internal_reasoning",
    "input_cache_read",
    "input_cache_write",
)

# per-token → per-1M へ換算する（トークン単価）キー
_TOKEN_RATE_MAP = {
    "prompt": "input",
    "completion": "output",
    "input_cache_read": "cached_input",
    "input_cache_write": "cache_write",
}
# 1 回あたりの実額として扱う（換算しない）キー
_TOOL_RATE_KEYS = ("request", "image", "web_search", "internal_reasoning")


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    text = str(value).strip()
    if not text or text in ("-1", "-1.0"):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class OpenRouterImport:
    """OpenRouter Models API レスポンスの解析結果。"""

    snapshots: Tuple[PricingRuleSnapshot, ...]
    ambiguous_slugs: Tuple[str, ...]
    duplicate_ids: Tuple[str, ...]
    # slug が別モデルの id と衝突するため alias にしなかったもの
    reserved_slugs: Tuple[str, ...] = ()

    @property
    def model_count(self) -> int:
        return len(self.snapshots)


def parse_openrouter_models(
    payload: Mapping[str, Any],
    *,
    catalog_version: str,
    effective_from: datetime,
) -> "OpenRouterImport":
    """OpenRouter レスポンスを料金ルールへ変換する（純粋関数・決定的）。

    `canonical_model` には**一意な `id`** を使う。`canonical_slug` は
    `:free` / `:thinking` / プロバイダ違いなどのバリアントで**重複する**ため、
    これをキーにすると upsert キー `(provider, canonical_model, effective_from)`
    が衝突し、API の並び順しだいで無料バリアントが有料モデルの単価を
    上書きしてしまう。実行時に OpenRouter が返す resolved model も `id` 形式。
    """
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise CatalogValidationError("OpenRouter レスポンスに data 配列がありません")

    # 1) エントリを正規化して収集する
    parsed: List[Tuple[str, str, Dict[str, Decimal], Dict[str, Decimal]]] = []
    for entry in data:
        if not isinstance(entry, Mapping):
            continue
        model_id = str(entry.get("id") or "").strip().lower()
        if not model_id:
            continue
        pricing = entry.get("pricing")
        if not isinstance(pricing, Mapping):
            continue

        rates: Dict[str, Decimal] = {}
        for source_key, rate_key in _TOKEN_RATE_MAP.items():
            value = _decimal_or_none(pricing.get(source_key))
            if value is None:
                continue
            # OpenRouter の pricing は USD per token
            rates[rate_key] = value * PER_MILLION
        if "input" not in rates or "output" not in rates:
            continue

        tool_rates: Dict[str, Decimal] = {}
        for key in _TOOL_RATE_KEYS:
            value = _decimal_or_none(pricing.get(key))
            if value is None or value == 0:
                continue
            tool_rates[key] = value

        slug = str(entry.get("canonical_slug") or "").strip().lower()
        parsed.append((model_id, slug, rates, tool_rates))

    # 2) id 昇順に決定的な順序で畳み込む（同一 id の重複は最後の1件に揃える）
    #    同一 id が複数あるとき安定ソートだけでは入力順に依存してしまうため、
    #    内容そのものを tiebreaker に入れて並び順を完全に決定づける。
    def _sort_key(item: Tuple[str, str, Dict[str, Decimal], Dict[str, Decimal]]):
        model_id, slug, rates, tool_rates = item
        content = (
            sorted((k, str(v)) for k, v in rates.items()),
            sorted((k, str(v)) for k, v in tool_rates.items()),
        )
        return (model_id, slug, repr(content))

    parsed.sort(key=_sort_key)
    by_id: Dict[str, Tuple[str, Dict[str, Decimal], Dict[str, Decimal]]] = {}
    duplicate_ids: List[str] = []
    for model_id, slug, rates, tool_rates in parsed:
        if model_id in by_id:
            duplicate_ids.append(model_id)
        by_id[model_id] = (slug, rates, tool_rates)

    # 3) slug → id の対応を作り、複数 id に対応する曖昧な slug は alias にしない
    slug_to_ids: Dict[str, set] = {}
    for model_id, (slug, _rates, _tools) in by_id.items():
        if not slug or slug == model_id:
            continue
        slug_to_ids.setdefault(slug, set()).add(model_id)

    ambiguous_slugs = sorted(slug for slug, ids in slug_to_ids.items() if len(ids) > 1)
    # slug がそれ自体で別モデルの id になっている場合（有料版 id を
    # `:free` / `:batch` / `:thinking` バリアントが canonical_slug に持つ）、
    # alias にすると無料バリアントが有料モデルの id を乗っ取ってしまう。
    reserved_slugs = sorted(
        slug for slug in slug_to_ids if slug in by_id
    )
    # ちょうど1つの id にしか対応せず、かつ他モデルの id でもない slug だけ alias にできる
    unique_slug_by_id: Dict[str, str] = {
        next(iter(ids)): slug
        for slug, ids in slug_to_ids.items()
        if len(ids) == 1 and slug not in by_id
    }

    effective_date = effective_from.date().isoformat()
    snapshots: List[PricingRuleSnapshot] = []
    for model_id in sorted(by_id):
        _slug, rates, tool_rates = by_id[model_id]
        aliases = {model_id}
        unique_slug = unique_slug_by_id.get(model_id)
        if unique_slug:
            aliases.add(unique_slug)
        snapshots.append(
            PricingRuleSnapshot(
                rule_id=f"openrouter:{model_id}:{effective_date}",
                provider="openrouter",
                canonical_model=model_id,
                aliases=tuple(sorted(aliases)),
                pricing_kind="flat_token",
                rates=rates,
                tiers=tuple(),
                long_context=None,
                tool_rates=tool_rates,
                effective_from=effective_from,
                effective_to=None,
                source=OPENROUTER_MODELS_URL,
                catalog_version=catalog_version,
            )
        )

    if not snapshots:
        raise CatalogValidationError("OpenRouter レスポンスから有効な料金を抽出できませんでした")

    return OpenRouterImport(
        snapshots=tuple(snapshots),
        ambiguous_slugs=tuple(ambiguous_slugs),
        duplicate_ids=tuple(sorted(set(duplicate_ids))),
        reserved_slugs=tuple(reserved_slugs),
    )


def build_openrouter_snapshots(
    payload: Mapping[str, Any],
    *,
    catalog_version: str,
    effective_from: datetime,
) -> List[PricingRuleSnapshot]:
    """`parse_openrouter_models` の薄いラッパー（ルール一覧だけが必要なとき用）。"""
    return list(
        parse_openrouter_models(
            payload,
            catalog_version=catalog_version,
            effective_from=effective_from,
        ).snapshots
    )


async def _fetch_openrouter_models(timeout: float) -> Mapping[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            OPENROUTER_MODELS_URL, headers={"Accept": "application/json"}
        )
        response.raise_for_status()
        return response.json()


async def refresh_openrouter_catalog(
    *, force: bool = False, timeout: float = 20.0
) -> dict:
    """OpenRouter Models API から料金を取り込む。失敗時は last-known-good を維持する。"""
    from ...memory.database import get_db_session
    from .catalog import apply_snapshots, record_catalog_state

    result: Dict[str, Any] = {
        "status": "ok",
        "source_key": OPENROUTER_SOURCE_KEY,
        "catalog_version": None,
        "fetched": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "closed": 0,
        "alias_inserted": 0,
        "alias_removed": 0,
        "deactivated": 0,
        "ambiguous_slugs": 0,
        "ambiguous_slug_examples": [],
        "duplicate_ids": 0,
        "reserved_slugs": 0,
        "error": None,
    }

    now = datetime.utcnow()

    # TTL 判定
    if not force:
        try:
            from sqlalchemy import select

            from ...models.ecc_models import PricingCatalogState

            async with await get_db_session() as session:
                state = (
                    await session.execute(
                        select(PricingCatalogState).where(
                            PricingCatalogState.source_key == OPENROUTER_SOURCE_KEY
                        )
                    )
                ).scalars().first()
                last_success = getattr(state, "last_success_at", None)
                if last_success is not None and now - last_success < timedelta(
                    seconds=OPENROUTER_TTL_SECONDS
                ):
                    result["status"] = "skipped"
                    result["catalog_version"] = getattr(state, "catalog_version", None)
                    return result
        except Exception:
            logger.warning("OpenRouter の TTL 判定に失敗しました", exc_info=True)

    catalog_version = f"openrouter-{now.strftime('%Y-%m-%dT%H%M%SZ')}"
    try:
        payload = await _fetch_openrouter_models(timeout)
        parsed = parse_openrouter_models(
            payload,
            catalog_version=catalog_version,
            effective_from=now.replace(hour=0, minute=0, second=0, microsecond=0),
        )
        snapshots = list(parsed.snapshots)
    except Exception as exc:
        logger.warning("OpenRouter 料金の取得に失敗しました: %s", exc)
        result["status"] = "error"
        result["error"] = str(exc)
        try:
            async with await get_db_session() as session:
                await record_catalog_state(
                    session,
                    source_key=OPENROUTER_SOURCE_KEY,
                    catalog_version=None,
                    rule_count=0,
                    status="error",
                    error=str(exc),
                    success=False,
                )
                await session.commit()
        except Exception:
            logger.warning("OpenRouter のエラー状態記録に失敗しました", exc_info=True)
        return result

    result["fetched"] = len(snapshots)
    result["catalog_version"] = catalog_version
    result["ambiguous_slugs"] = len(parsed.ambiguous_slugs)
    result["ambiguous_slug_examples"] = list(parsed.ambiguous_slugs[:10])
    result["duplicate_ids"] = len(parsed.duplicate_ids)
    result["reserved_slugs"] = len(parsed.reserved_slugs)

    try:
        async with await get_db_session() as session:
            # openrouter 以外のプロバイダには触れず、旧 canonical_slug キーで
            # 作られた残骸行だけを is_active=false へ収束させる
            applied = await apply_snapshots(
                session,
                snapshots,
                deactivate_missing=True,
                restrict_to_providers={"openrouter"},
            )
            await record_catalog_state(
                session,
                source_key=OPENROUTER_SOURCE_KEY,
                catalog_version=catalog_version,
                rule_count=len(snapshots),
                status="ok",
                success=True,
            )
            await session.commit()
        for key in (
            "inserted",
            "updated",
            "unchanged",
            "closed",
            "alias_inserted",
            "alias_removed",
            "deactivated",
        ):
            result[key] = applied[key]
    except Exception as exc:
        logger.exception("OpenRouter 料金の DB 反映に失敗しました")
        result["status"] = "error"
        result["error"] = str(exc)
        return result

    try:
        from .engine import get_pricing_engine

        get_pricing_engine().invalidate()
    except Exception:  # pragma: no cover
        logger.warning("料金エンジンのキャッシュ破棄に失敗しました", exc_info=True)

    return result


async def import_catalog_json(
    payload: Mapping[str, Any], *, dry_run: bool = False
) -> dict:
    """カタログ JSON をそのまま取り込む（管理者向け手動更新）。"""
    from ...memory.database import get_db_session
    from .catalog import _load_current_snapshots, apply_snapshots, record_catalog_state

    result: Dict[str, Any] = {
        "status": "ok",
        "dry_run": dry_run,
        "source_key": MANUAL_SOURCE_KEY,
        "catalog_version": None,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "closed": 0,
        "alias_inserted": 0,
        "alias_removed": 0,
        "diff": {},
        "error": None,
    }

    try:
        snapshots = build_snapshots(payload)
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        return result

    result["catalog_version"] = str(payload.get("catalog_version") or "")

    try:
        async with await get_db_session() as session:
            if dry_run:
                current = await _load_current_snapshots(session)
                diff = diff_catalog(current, snapshots)
                result["diff"] = _summarize_diff(diff)
                result.update(diff["counts"])
                return result

            applied = await apply_snapshots(session, snapshots, deactivate_missing=False)
            await record_catalog_state(
                session,
                source_key=MANUAL_SOURCE_KEY,
                catalog_version=result["catalog_version"] or None,
                rule_count=len(snapshots),
                status="ok",
                success=True,
            )
            await session.commit()
        for key in (
            "inserted",
            "updated",
            "unchanged",
            "closed",
            "alias_inserted",
            "alias_removed",
        ):
            result[key] = applied[key]
        result["diff"] = _summarize_diff(applied["diff"])
    except Exception as exc:
        logger.exception("カタログ JSON の取り込みに失敗しました")
        result["status"] = "error"
        result["error"] = str(exc)
        return result

    try:
        from .engine import get_pricing_engine

        get_pricing_engine().invalidate()
    except Exception:  # pragma: no cover
        logger.warning("料金エンジンのキャッシュ破棄に失敗しました", exc_info=True)

    return result


def _summarize_diff(diff: Mapping[str, Any]) -> dict:
    """diff_catalog の結果を JSON 安全な形に落とす。"""

    def _names(items) -> List[str]:
        names = []
        for item in items:
            if isinstance(item, Mapping):
                names.append(f"{item.get('provider')}:{item.get('canonical_model')}")
            else:
                names.append(f"{item.provider}:{item.canonical_model}")
        return names

    return {
        "inserted": _names(diff.get("inserted", [])),
        "updated": _names(diff.get("updated", [])),
        "unchanged_count": len(diff.get("unchanged", [])),
        "closed": _names(diff.get("closed", [])),
        "deactivated": _names(diff.get("deactivated", [])),
        "deleted": [],
    }


async def get_pricing_catalog_status() -> dict:
    """料金カタログの現況（版・件数・各ソースの最終結果）を返す。"""
    from sqlalchemy import func, select

    from ...memory.database import get_db_session
    from ...models.ecc_models import PricingCatalogState, PricingRule
    from .engine import get_pricing_engine

    status: Dict[str, Any] = {
        "catalog_version": None,
        "rule_count": 0,
        "sources": [],
    }

    def _iso(value) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=timezone.utc).isoformat()
        return str(value)

    try:
        async with await get_db_session() as session:
            rule_count = (
                await session.execute(select(func.count()).select_from(PricingRule))
            ).scalar() or 0
            states = (
                await session.execute(select(PricingCatalogState))
            ).scalars().all()
            status["rule_count"] = int(rule_count)
            for state in states:
                status["sources"].append(
                    {
                        "source_key": state.source_key,
                        "catalog_version": state.catalog_version,
                        "last_success_at": _iso(state.last_success_at),
                        "last_attempt_at": _iso(state.last_attempt_at),
                        "last_status": state.last_status,
                        "last_error": state.last_error,
                        "rule_count": int(state.rule_count or 0),
                    }
                )
            for state in states:
                if state.source_key == "catalog_file" and state.catalog_version:
                    status["catalog_version"] = state.catalog_version
                    break
            if status["catalog_version"] is None and states:
                status["catalog_version"] = states[0].catalog_version
    except Exception:
        logger.warning("料金カタログ状態の取得に失敗しました", exc_info=True)

    if status["catalog_version"] is None or status["rule_count"] == 0:
        try:
            from .catalog import load_catalog_file

            document = load_catalog_file()
            status["catalog_version"] = status["catalog_version"] or document.catalog_version
            status["rule_count"] = status["rule_count"] or document.rule_count
        except Exception:
            logger.warning("料金カタログファイルの読み込みに失敗しました", exc_info=True)

    engine = get_pricing_engine()
    if status["catalog_version"] is None:
        status["catalog_version"] = engine.catalog_version

    return status
